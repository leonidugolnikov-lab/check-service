from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import asyncio
import os
import re
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None

app = FastAPI(title="Real Estate Seller & Property Check API", version="1.0.0-polling-final")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip()

METHOD_PASSPORT = os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd").strip()
METHOD_FSSP = os.getenv("NEWDB_METHOD_FSSP", "fssp_person").strip()
METHOD_BANKRUPTCY = os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankrot_person").strip()
METHOD_ARBITR = os.getenv("NEWDB_METHOD_ARBITR", "arbitr_person").strip()
METHOD_PRAVOSUD = os.getenv("NEWDB_METHOD_PRAVOSUD", "pravo_search").strip()
METHOD_PRAVOSUD_FALLBACK = os.getenv("NEWDB_METHOD_PRAVOSUD_FALLBACK", "pravosudfiz").strip()
METHOD_EGRN = os.getenv("NEWDB_METHOD_EGRN", "rosreestr").strip()

# Polling settings. Render free plan can be slow; Росреестр часто дольше остальных.
NEWDB_POLL_INTERVAL = float(os.getenv("NEWDB_POLL_INTERVAL", "4"))
NEWDB_DEFAULT_MAX_WAIT = int(os.getenv("NEWDB_DEFAULT_MAX_WAIT", "95"))
NEWDB_EGRN_MAX_WAIT = int(os.getenv("NEWDB_EGRN_MAX_WAIT", "180"))
NEWDB_PRAVOSUD_MAX_WAIT = int(os.getenv("NEWDB_PRAVOSUD_MAX_WAIT", "95"))

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=25.0, read=240.0, write=60.0, pool=30.0)

IN_PROGRESS_STATES = {"queued", "queue", "restart", "in progress", "progress", "pending", "processing", "wait", "waiting"}
GOOD_STATES = {"complete", "completed", "done", "success", "finished", "ready", "ok"}
BAD_STATES = {"failed", "fail", "error", "rejected", "denied", "timeout", "manual", "not_configured"}

MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "arbitr": "https://kad.arbitr.ru",
    "pravosud": "https://sudrf.ru",
    "egrn": "https://rosreestr.gov.ru",
}

DISCLAIMER = "Отчет носит информационно-аналитический характер, не является гарантией полной юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом."

class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    seller_inn: str = ""
    inn_fiz: str = ""
    innfiz: str = ""
    innfl: str = ""
    region: int = 78
    passport_series: str = ""
    passport_seria: str = ""
    seria: str = ""
    passport_number: str = ""
    number: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    cadnum: str = ""
    address: str = ""

# ---------- basic helpers ----------

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed:
                text = fixed
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


def only_digits(value: Any) -> str:
    return re.sub(r"\D+", "", clean_text(value))


def normalize_dob(value: str) -> str:
    value = clean_text(value)
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        y, m, d = value.split("-")
        return f"{d}.{m}.{y}"
    return value


def dob_to_iso(value: str) -> str:
    value = clean_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return value


def rub(value: Any) -> str:
    try:
        n = float(value or 0)
        s = f"{n:,.2f}".replace(",", " ")
        if s.endswith(".00"):
            s = s[:-3]
        return s + " ₽"
    except Exception:
        return "0 ₽"


def json_blob(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()


def strip_sensitive(obj: Any, debug: bool = False) -> Any:
    # In debug we keep requestId/params because user needs diagnostics, but never token/balance in client response.
    banned = {"token", "api_key", "x-api-key", "authorization", "client_secret", "access_token"}
    if not debug:
        banned |= {"requestId", "request_id", "newdb_qid", "balance", "datecreated", "taskId", "params"}
    else:
        banned |= {"token", "api_key", "x-api-key", "authorization", "client_secret", "access_token"}
    if isinstance(obj, dict):
        return {k: strip_sensitive(v, debug=debug) for k, v in obj.items() if str(k).lower() not in banned}
    if isinstance(obj, list):
        return [strip_sensitive(x, debug=debug) for x in obj]
    return obj


def state_of(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    return clean_text(data.get("state") or data.get("status") or "").lower()


def is_bad_newdb_response(data: Any) -> bool:
    if not isinstance(data, dict):
        return True
    st = state_of(data)
    if st in BAD_STATES:
        return True
    if data.get("errors_info"):
        return True
    try:
        if int(data.get("_http_status") or data.get("http_status") or 0) >= 400:
            return True
    except Exception:
        pass
    txt = json_blob(data)
    markers = [
        "method or country is not valid", "not enough balance", "insufficient balance", "проверьте баланс",
        "unauthorized", "forbidden", "x-api-key", "missing required", "required parameter",
        "не заполнено значение обязательного параметра", "service is unavailable", "parsing failed"
    ]
    return any(m in txt for m in markers)


def result_status_ok(data: Dict[str, Any], method: str) -> bool:
    try:
        res = data.get("results", {}).get(method, {}).get("result", {})
        return int(res.get("status", 0)) == 200
    except Exception:
        return False


def extract_result_data(data: Any, method: str) -> List[Any]:
    if not isinstance(data, dict):
        return []
    try:
        block = data.get("results", {}).get(method, {})
        result = block.get("result", {}) if isinstance(block, dict) else {}
        if int(result.get("status", 0)) != 200:
            return []
        d = result.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
    except Exception:
        return []
    return []


def has_result_block(data: Any, method: str) -> bool:
    return isinstance(data, dict) and isinstance(data.get("results"), dict) and method in data.get("results", {})


def manual_item(title: str, summary: str, details: Optional[List[str]] = None, url: str = "") -> Dict[str, Any]:
    return {
        "title": title,
        "source": title,
        "status": "manual_check",
        "ui_status": "manual",
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
        "manual_url": url,
    }

# ---------- input/payloads ----------

def normalize_request(req: CheckRequest) -> Dict[str, Any]:
    inn = only_digits(req.inn or req.seller_inn or req.inn_fiz or req.innfiz or req.innfl)
    ps = only_digits(req.passport_series or req.passport_seria or req.seria)
    pn = only_digits(req.passport_number or req.number)
    cad = clean_text(req.cadastral_number or req.cadastre_number or req.cadnum)
    addr = clean_text(req.address)
    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"
    if cad and re.fullmatch(cad_pattern, cad):
        prop = {"query": cad, "cadastral_number": cad, "address": cad, "type": "cadastral"}
    elif addr and re.fullmatch(cad_pattern, addr):
        prop = {"query": addr, "cadastral_number": addr, "address": addr, "type": "cadastral"}
    else:
        addr = re.sub(r"\s+", " ", addr)
        if addr and not re.search(r"(?i)санкт[- ]петербург|ленинградская область", addr):
            addr = "Санкт-Петербург, " + addr
        prop = {"query": addr, "cadastral_number": "", "address": addr, "type": "address"}
    return {
        "last": clean_text(req.last),
        "first": clean_text(req.first),
        "middle": clean_text(req.middle),
        "dob": normalize_dob(req.dob),
        "dob_iso": dob_to_iso(req.dob),
        "inn": inn,
        "region": int(req.region or 78),
        "passport_series": ps,
        "passport_number": pn,
        "property": prop,
    }


def make_payloads(n: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    fio = " ".join(x for x in [n["last"], n["first"], n["middle"]] if x).strip()
    payloads = {}
    payloads["passport"] = None
    if n["passport_series"] and n["passport_number"]:
        payloads["passport"] = {
            "seria": n["passport_series"], "number": n["passport_number"],
            "firstname": n["first"], "lastname": n["last"], "secondname": n["middle"],
            "dob": n["dob_iso"], "country": "ru", "method": METHOD_PASSPORT,
        }
    payloads["fssp"] = None
    if n["last"] and n["first"] and n["dob_iso"]:
        payloads["fssp"] = {
            "firstname": n["first"], "lastname": n["last"], "secondname": n["middle"],
            "dob": n["dob_iso"], "regioncode": n["region"], "country": "ru", "method": METHOD_FSSP,
        }
    payloads["bankruptcy"] = None
    payloads["arbitr"] = None
    if len(n["inn"]) == 12:
        payloads["bankruptcy"] = {"innfiz": n["inn"], "country": "ru", "method": METHOD_BANKRUPTCY}
        payloads["arbitr"] = {"innfiz": n["inn"], "country": "ru", "method": METHOD_ARBITR}
    payloads["pravosud"] = None
    if fio:
        payloads["pravosud"] = {
            "method": METHOD_PRAVOSUD, "country": "ru", "query": fio, "q": fio, "fio": fio,
            "lastname": n["last"], "firstname": n["first"], "secondname": n["middle"],
            "party_name": fio, "limit": 50,
        }
    payloads["egrn"] = None
    if n["property"].get("address"):
        payloads["egrn"] = {"address": n["property"]["address"], "country": "ru", "method": METHOD_EGRN}
    return payloads

# ---------- NewDB with real polling ----------

async def newdb_call(client: httpx.AsyncClient, params: Optional[Dict[str, Any]], *, max_wait: int, debug: bool = False) -> Dict[str, Any]:
    if not params:
        return {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
    if not NEWDB_TOKEN:
        return {"state": "not_configured", "error": "NEWDB_TOKEN не задан в переменных окружения."}

    headers = {"Content-Type": "application/json", "Accept": "application/json", "X-API-KEY": NEWDB_TOKEN}
    payload = {"params": params}
    try:
        r = await client.post(NEWDB_URL, headers=headers, json=payload)
        try:
            data = r.json()
        except Exception:
            data = {"raw_text": r.text}
        data["_http_status"] = r.status_code
    except Exception as e:
        return {"state": "error", "error": f"Ошибка запроса к newDB: {e}"}

    # Immediate hard error.
    if is_bad_newdb_response(data) and state_of(data) not in IN_PROGRESS_STATES:
        return data

    # Already complete / has results.
    st = state_of(data)
    if st in GOOD_STATES or data.get("results"):
        return data

    # IMPORTANT: NewDB polling. For queued/restart responses repeat same request with top-level requestId.
    request_id = data.get("requestId") or data.get("request_id")
    if not request_id:
        # fallback qid sometimes appears inside params
        request_id = data.get("params", {}).get("newdb_qid") if isinstance(data.get("params"), dict) else None
    if not request_id:
        data["state"] = "manual"
        data["error"] = "Источник поставил задачу в очередь, но не вернул requestId для polling."
        return data

    last = data
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(NEWDB_POLL_INTERVAL)
        elapsed += NEWDB_POLL_INTERVAL
        poll_payloads = [
            {"requestId": request_id, "params": params},
            {"requestId": request_id},
            {"params": {**params, "requestId": request_id}},
            {"params": {**params, "newdb_qid": data.get("params", {}).get("newdb_qid", "")}} if isinstance(data.get("params"), dict) and data.get("params", {}).get("newdb_qid") else None,
        ]
        for pp in [x for x in poll_payloads if x]:
            try:
                pr = await client.post(NEWDB_URL, headers=headers, json=pp)
                try:
                    polled = pr.json()
                except Exception:
                    polled = {"raw_text": pr.text}
                polled["_http_status"] = pr.status_code
                last = polled
                pst = state_of(polled)
                if polled.get("results") or pst in GOOD_STATES:
                    return polled
                if is_bad_newdb_response(polled) and pst not in IN_PROGRESS_STATES:
                    return polled
            except Exception as e:
                last = {"state": "error", "error": f"Ошибка polling newDB: {e}"}
                continue
    last["state"] = "timeout"
    last["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд. Требуется ручная проверка."
    return last

async def call_source(client, key: str, payload: Optional[Dict[str, Any]], *, max_wait: int, debug: bool = False) -> Dict[str, Any]:
    try:
        return await newdb_call(client, payload, max_wait=max_wait, debug=debug)
    except Exception as e:
        return {"state": "error", "error": f"Источник {key} вызвал ошибку backend: {e}"}

async def call_pravosud_with_fallback(client, payload: Optional[Dict[str, Any]], debug: bool = False) -> Dict[str, Any]:
    if not payload:
        return {"state": "skipped", "error": "Недостаточно входных данных для проверки ГАС Правосудие."}
    primary = await call_source(client, "pravosud", payload, max_wait=NEWDB_PRAVOSUD_MAX_WAIT, debug=debug)
    if not is_bad_newdb_response(primary) or primary.get("results"):
        return primary
    fallback_payload = dict(payload)
    fallback_payload["method"] = METHOD_PRAVOSUD_FALLBACK
    fallback = await call_source(client, "pravosud", fallback_payload, max_wait=NEWDB_PRAVOSUD_MAX_WAIT, debug=debug)
    if debug:
        fallback["_fallback_tried"] = METHOD_PRAVOSUD_FALLBACK
        fallback["_primary_error"] = primary
    return fallback

async def run_all_checks(req: CheckRequest, *, debug: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    n = normalize_request(req)
    payloads = make_payloads(n)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        tasks = {
            "passport": call_source(client, "passport", payloads["passport"], max_wait=NEWDB_DEFAULT_MAX_WAIT, debug=debug),
            "fssp": call_source(client, "fssp", payloads["fssp"], max_wait=NEWDB_DEFAULT_MAX_WAIT, debug=debug),
            "bankruptcy": call_source(client, "bankruptcy", payloads["bankruptcy"], max_wait=NEWDB_DEFAULT_MAX_WAIT, debug=debug),
            "arbitr": call_source(client, "arbitr", payloads["arbitr"], max_wait=NEWDB_DEFAULT_MAX_WAIT, debug=debug),
            "pravosud": call_pravosud_with_fallback(client, payloads["pravosud"], debug=debug),
            "egrn": call_source(client, "egrn", payloads["egrn"], max_wait=NEWDB_EGRN_MAX_WAIT, debug=debug),
        }
        keys = list(tasks.keys())
        results_raw = await asyncio.gather(*tasks.values(), return_exceptions=True)
        responses = {}
        for k, v in zip(keys, results_raw):
            if isinstance(v, Exception):
                responses[k] = {"state": "error", "error": f"Backend exception in {k}: {v}"}
            else:
                responses[k] = v
    return n, payloads, responses

# ---------- classifiers ----------

def classify_passport(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Паспорт МВД"
    if is_bad_newdb_response(resp) or state_of(resp) in IN_PROGRESS_STATES:
        return manual_item(title, "Источник не вернул данные по паспорту. Требуется ручная проверка.", [human_error(resp)], MANUAL_LINKS["passport"])
    items = extract_result_data(resp, METHOD_PASSPORT)
    text = json_blob(items or resp)
    if "действительный" in text or "действителен" in text or "valid" in text:
        return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "Паспорт по полученным данным действителен.", "details": ["Действительный"], "manual_check_url": MANUAL_LINKS["passport"], "manual_url": MANUAL_LINKS["passport"], "data": items}
    if "недейств" in text or "invalid" in text:
        return {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": "Выявлены признаки проблемы с паспортом.", "details": ["Источник МВД вернул признак недействительности/проблемы паспорта."], "manual_check_url": MANUAL_LINKS["passport"], "manual_url": MANUAL_LINKS["passport"], "data": items}
    return manual_item(title, "Источник не вернул понятный результат проверки паспорта. Требуется ручная проверка.", [], MANUAL_LINKS["passport"])


def extract_debt_amount(item: Dict[str, Any]) -> float:
    # Strict: only from debt fields, not from writ numbers, INN or case numbers.
    candidates = []
    if isinstance(item, dict):
        for key in ["SubjectAndDebtAmount", "subject", "debt", "amount", "debt_amount"]:
            if item.get(key):
                candidates.append(clean_text(item.get(key)))
    text = " ".join(candidates)
    patterns = [
        r"Остаток\s+долга[^:\d]*:\s*([\d\s]+(?:[\.,]\d{1,2})?)",
        r"Сумма\s+долга\s*:\s*([\d\s]+(?:[\.,]\d{1,2})?)",
        r"задолженность[^\d]{0,40}([\d\s]+(?:[\.,]\d{1,2})?)\s*руб",
    ]
    vals = []
    for p in patterns:
        for m in re.findall(p, text, flags=re.I):
            try:
                vals.append(float(str(m).replace(" ", "").replace(",", ".")))
            except Exception:
                pass
    if vals:
        # If остаток exists, it appears first due to patterns order. For active IP prefer остаток/sum; max safe for multiple values in same string.
        return max(vals)
    return 0.0


def is_closed_fssp(item: Dict[str, Any]) -> bool:
    text = json_blob(item)
    return any(x in text for x in ["ст. 46", "статья 46", "оконч", "прекращ", "закрыт", "заверш", "completiondateorreason"] ) and bool(clean_text(item.get("CompletionDateOrReason") if isinstance(item, dict) else ""))


def classify_fssp(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "ФССП"
    if is_bad_newdb_response(resp) or state_of(resp) in IN_PROGRESS_STATES:
        return manual_item(title, "Источник ФССП не вернул данные. Требуется ручная проверка.", [human_error(resp)], MANUAL_LINKS["fssp"])
    items = extract_result_data(resp, METHOD_FSSP)
    if not items:
        return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "По полученным данным исполнительные производства не найдены.", "details": ["ФССП искался по ФИО, дате рождения и региону."], "manual_check_url": MANUAL_LINKS["fssp"], "manual_url": MANUAL_LINKS["fssp"], "data": empty_fssp_stats(), "fssp_stats": empty_fssp_stats()}
    active, closed, unknown = [], [], []
    for it in items:
        if is_closed_fssp(it):
            closed.append(it)
        elif isinstance(it, dict):
            active.append(it)
        else:
            unknown.append(it)
    active_sum = sum(extract_debt_amount(x) for x in active if isinstance(x, dict))
    closed_sum = sum(extract_debt_amount(x) for x in closed if isinstance(x, dict))
    unknown_sum = sum(extract_debt_amount(x) for x in unknown if isinstance(x, dict))
    actual_debt = active_sum + unknown_sum
    stats = {
        "all_count": len(items), "active_count": len(active), "closed_count": len(closed), "unknown_count": len(unknown),
        "total_sum_all": round(active_sum + closed_sum + unknown_sum, 2), "active_sum": round(active_sum, 2),
        "closed_sum": round(closed_sum, 2), "unknown_sum": round(unknown_sum, 2), "actual_debt": round(actual_debt, 2),
        "active_items": active, "closed_items": closed, "unknown_items": unknown,
    }
    details = [
        f"Всего найдено ИП: {stats['all_count']}", f"Активные ИП: {stats['active_count']}",
        f"Закрытые/оконченные ИП: {stats['closed_count']}", f"Неоднозначные записи: {stats['unknown_count']}",
        f"Общая сумма всех найденных ИП: {rub(stats['total_sum_all'])}", f"Сумма по активным ИП: {rub(stats['active_sum'])}",
        f"Сумма по закрытым ИП: {rub(stats['closed_sum'])}", f"Актуальный долг по активным/неоднозначным ИП: {rub(stats['actual_debt'])}",
    ]
    if active or unknown:
        return {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": f"Найдены активные или неоднозначные исполнительные производства. Актуальная сумма для ручной оценки: {rub(actual_debt)}.", "details": details, "manual_check_url": MANUAL_LINKS["fssp"], "manual_url": MANUAL_LINKS["fssp"], "data": stats, "fssp_stats": stats}
    return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "Найдены только закрытые/оконченные ИП. Актуальный долг по активным ИП не подтвержден.", "details": details, "manual_check_url": MANUAL_LINKS["fssp"], "manual_url": MANUAL_LINKS["fssp"], "data": stats, "fssp_stats": stats}


def empty_fssp_stats():
    return {"all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0, "total_sum_all": 0, "active_sum": 0, "closed_sum": 0, "unknown_sum": 0, "actual_debt": 0, "active_items": [], "closed_items": [], "unknown_items": []}


def classify_bankruptcy(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Банкротство / Федресурс"
    if is_bad_newdb_response(resp) or state_of(resp) in IN_PROGRESS_STATES:
        return manual_item(title, "Источник банкротств не вернул данные. Требуется ручная проверка.", [human_error(resp)], MANUAL_LINKS["bankruptcy"])
    items = extract_result_data(resp, METHOD_BANKRUPTCY)
    text = json_blob(items)
    has_risk = False
    for it in items:
        if isinstance(it, dict):
            if it.get("bankruptcy") or it.get("publications") or it.get("encumbrances"):
                # empty lists are false
                has_risk = bool(it.get("bankruptcy") or it.get("publications") or it.get("encumbrances"))
    if has_risk:
        return {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": "Выявлены сведения, связанные с банкротством.", "details": ["Требуется ручной анализ статуса процедуры, дат и риска оспаривания сделки."], "manual_check_url": MANUAL_LINKS["bankruptcy"], "manual_url": MANUAL_LINKS["bankruptcy"], "data": items}
    return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "По полученным данным сведения о банкротстве физлица не выявлены.", "details": [], "manual_check_url": MANUAL_LINKS["bankruptcy"], "manual_url": MANUAL_LINKS["bankruptcy"], "data": items}


def classify_arbitr(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Арбитражные суды"
    if is_bad_newdb_response(resp) or state_of(resp) in IN_PROGRESS_STATES:
        return manual_item(title, "Источник арбитражных судов не вернул данные. Требуется ручная проверка.", [human_error(resp)], MANUAL_LINKS["arbitr"])
    items = extract_result_data(resp, METHOD_ARBITR)
    cases = []
    found = False
    for it in items:
        if isinstance(it, dict):
            found = found or bool(it.get("found"))
            if isinstance(it.get("cases"), list):
                cases.extend(it.get("cases"))
    if found or cases:
        return {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": "Найдены арбитражные дела. Требуется анализ предмета спора.", "details": [f"Количество найденных дел: {len(cases) or 'требуется уточнить'}"], "manual_check_url": MANUAL_LINKS["arbitr"], "manual_url": MANUAL_LINKS["arbitr"], "data": cases or items}
    return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "По полученным данным арбитражные дела не выявлены.", "details": [], "manual_check_url": MANUAL_LINKS["arbitr"], "manual_url": MANUAL_LINKS["arbitr"], "data": []}


def classify_pravosud(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Суды общей юрисдикции / ГАС Правосудие"
    method_used = METHOD_PRAVOSUD
    if has_result_block(resp, METHOD_PRAVOSUD_FALLBACK):
        method_used = METHOD_PRAVOSUD_FALLBACK
    if is_bad_newdb_response(resp) or state_of(resp) in IN_PROGRESS_STATES:
        return manual_item(title, "Источник не вернул данные. Требуется ручная проверка.", [human_error(resp)], MANUAL_LINKS["pravosud"])
    items = extract_result_data(resp, method_used)
    # accept many formats: cases/items/data list/found flags
    cases = []
    found = False
    for it in items:
        if isinstance(it, dict):
            found = found or bool(it.get("found"))
            for key in ["cases", "items", "results", "rows", "data"]:
                if isinstance(it.get(key), list):
                    cases.extend(it.get(key))
        elif it:
            cases.append(it)
    if found or cases:
        return {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": "Найдены судебные дела в судах общей юрисдикции. Требуется анализ предмета спора.", "details": [f"Количество найденных записей: {len(cases) or len(items)}"], "manual_check_url": MANUAL_LINKS["pravosud"], "manual_url": MANUAL_LINKS["pravosud"], "data": cases or items}
    return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "По полученным данным дела в судах общей юрисдикции не выявлены.", "details": [], "manual_check_url": MANUAL_LINKS["pravosud"], "manual_url": MANUAL_LINKS["pravosud"], "data": []}


def classify_egrn(resp: Dict[str, Any], n: Dict[str, Any]) -> Dict[str, Any]:
    title = "ЕГРН / Росреестр"
    if is_bad_newdb_response(resp) or state_of(resp) in IN_PROGRESS_STATES:
        return manual_item(title, "Источник ЕГРН не вернул данные. Требуется ручная проверка.", [human_error(resp)], MANUAL_LINKS["egrn"])
    items = extract_result_data(resp, METHOD_EGRN)
    if not items:
        return manual_item(title, "Объект не найден автоматически или источник не вернул понятные данные. Требуется ручная проверка.", [f"Запрос: {n['property'].get('query')}"] , MANUAL_LINKS["egrn"])
    obj = items[0] if isinstance(items[0], dict) else {}
    enc = obj.get("encumbrances") if isinstance(obj, dict) else []
    rights = obj.get("rights") if isinstance(obj, dict) else []
    addr = obj.get("address", {}).get("readableAddress") if isinstance(obj.get("address"), dict) else ""
    details = []
    if obj.get("cadNumber"):
        details.append(f"Кадастровый номер: {obj.get('cadNumber')}")
    if addr:
        details.append(f"Адрес: {addr}")
    if obj.get("objType_text"):
        details.append(f"Тип объекта: {obj.get('objType_text')}")
    if obj.get("purpose_text"):
        details.append(f"Назначение: {obj.get('purpose_text')}")
    if obj.get("area"):
        details.append(f"Площадь: {obj.get('area')} кв.м")
    if obj.get("cadCost"):
        details.append(f"Кадастровая стоимость: {rub(obj.get('cadCost'))}")
    details.append(f"Записей о правах: {len(rights or [])}")
    if enc:
        for e in enc:
            if not isinstance(e, dict):
                continue
            desc = clean_text(e.get("typeDesc")) or f"Тип ограничения: {e.get('type') or 'не указан'}"
            num = clean_text(e.get("encumbranceNumber"))
            date = clean_text(e.get("startDate") or e.get("encumbranceDate"))
            line = desc
            if num:
                line += f", № {num}"
            if date:
                line += f", дата начала: {date}"
            details.append(line)
        return {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН.", "details": details, "manual_check_url": MANUAL_LINKS["egrn"], "manual_url": MANUAL_LINKS["egrn"], "data": obj, "egrn_object": obj}
    return {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": "Данные по объекту получены. По полученным данным явные признаки ограничений или обременений не выявлены.", "details": details, "manual_check_url": MANUAL_LINKS["egrn"], "manual_url": MANUAL_LINKS["egrn"], "data": obj, "egrn_object": obj}


def human_error(resp: Any) -> str:
    if not isinstance(resp, dict):
        return "Источник вернул непонятный ответ. Требуется ручная проверка."
    if resp.get("errors_info"):
        txt = json_blob(resp.get("errors_info"))
        if "method or country" in txt:
            return "Источник не принял метод или страну запроса. Требуется уточнить метод newDB."
        if "balance" in txt or "лимит" in txt or "not enough" in txt:
            return "Вероятно, исчерпан лимит/баланс API newDB."
        return "Источник вернул ошибку. Требуется ручная проверка."
    st = state_of(resp)
    if st in IN_PROGRESS_STATES:
        return "Источник еще обрабатывает запрос. Требуется повторить позже или проверить вручную."
    if st == "timeout":
        return "Источник не успел вернуть результат в установленное время. Требуется ручная проверка."
    if "error" in resp:
        err = clean_text(resp.get("error"))
        if "balance" in err.lower() or "лимит" in err.lower():
            return "Вероятно, исчерпан лимит/баланс API newDB."
        return err[:250]
    return "Источник не вернул данные. Требуется ручная проверка."


def classify_all(responses: Dict[str, Any], n: Dict[str, Any]) -> List[Dict[str, Any]]:
    safe = []
    for fn, args in [
        (classify_passport, (responses.get("passport", {}),)),
        (classify_fssp, (responses.get("fssp", {}),)),
        (classify_bankruptcy, (responses.get("bankruptcy", {}),)),
        (classify_arbitr, (responses.get("arbitr", {}),)),
        (classify_pravosud, (responses.get("pravosud", {}),)),
        (classify_egrn, (responses.get("egrn", {}), n)),
    ]:
        try:
            safe.append(fn(*args))
        except Exception as e:
            safe.append(manual_item("Источник проверки", f"Ошибка обработки результата: {e}. Требуется ручная проверка.", [], ""))
    return safe

# ---------- scoring/recommendations/report/pdf ----------

def score_and_recommend(checklist: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    score = 0
    factors = []
    recs: List[Dict[str, str]] = []
    fssp_stats = {}
    for item in checklist:
        title = item.get("title", "")
        status = item.get("status")
        if status == "manual_check":
            add = 6 if "ГАС" in title else 8
            score += add
            factors.append(f"{title}: требуется ручная проверка (+{add})")
        elif status == "risk":
            if "ЕГРН" in title:
                score += 60
                factors.append("ЕГРН: выявлены ограничения/обременения (+60)")
                recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки", "text": "По ЕГРН выявлены ограничения/обременения. До аванса нужно получить основание ограничения и прописать обязанность продавца снять его в конкретный срок."})
                recs.append({"priority": "critical", "title": "Не передавать деньги напрямую продавцу", "text": "При запрете регистрации использовать нотариальный депозит или аккредитив с раскрытием денег только после снятия ограничения и регистрации перехода права."})
            elif "ФССП" in title:
                stats = item.get("fssp_stats") or item.get("data") or {}
                fssp_stats = stats
                debt = float(stats.get("actual_debt") or 0)
                add = 35 if debt > 0 else 25
                score += add
                factors.append(f"ФССП: активные/неоднозначные ИП, сумма {rub(debt)} (+{add})")
                recs.append({"priority": "high", "title": "Закрыть активные ИП до сделки или через контролируемые расчеты", "text": f"Актуальная сумма по активным/неоднозначным ИП: {rub(debt)}. В ПДКП прописать порядок погашения и последствия неснятия ограничений."})
            elif "банкрот" in title.lower():
                score += 45
                factors.append("Банкротство: выявлены сведения (+45)")
                recs.append({"priority": "critical", "title": "Не выходить на сделку без анализа банкротного риска", "text": "Сведения о банкротстве требуют анализа периода, статуса процедуры и риска оспаривания сделки."})
            elif "суд" in title.lower() or "арбитраж" in title.lower():
                score += 25
                factors.append(f"{title}: найдены судебные дела (+25)")
                recs.append({"priority": "high", "title": "Разобрать судебные дела по предмету спора", "text": "Нужно понять предмет спора, сумму требований и связь с недвижимостью, долгами или банкротством."})
            else:
                score += 25
                factors.append(f"{title}: выявлен риск (+25)")
    score = min(100, int(score))
    if score >= 70:
        level = "опасная"
        conclusion = "Сделку нельзя выводить на аванс без ручного юридического разбора и устранения выявленных факторов."
    elif score >= 30:
        level = "условно рискованная"
        conclusion = "Сделку можно рассматривать только после уточнения рисков и настройки безопасных условий."
    else:
        level = "условно безопасная"
        conclusion = "По автоматическим источникам критические признаки не выявлены, но отчет не заменяет ручную юридическую проверку."
    if any(x.get("status") == "manual_check" for x in checklist):
        recs.append({"priority": "medium", "title": "Закрыть ручные проверки до аванса", "text": "Все источники со статусом «требуется ручная проверка» нужно проверить вручную до передачи денег."})
    recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса/задатка и ответственность при неподтверждении данных."})
    # de-duplicate by title
    seen = set(); unique = []
    for r in recs:
        if r["title"] not in seen:
            seen.add(r["title"]); unique.append(r)
    return {"score": score, "max_score": 100, "level": level, "conclusion": conclusion, "factors": factors}, unique


def registry_data_from_checklist(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    mapping = {"Паспорт МВД": "passport", "ФССП": "fssp", "Банкротство / Федресурс": "bankruptcy", "Арбитражные суды": "arbitr", "Суды общей юрисдикции / ГАС Правосудие": "pravosud", "ЕГРН / Росреестр": "egrn"}
    out = {}
    for item in checklist:
        key = mapping.get(item.get("title", ""), item.get("title", "source"))
        base = {"title": item.get("title"), "status": item.get("status"), "summary": item.get("summary"), "details": item.get("details", [])}
        if item.get("title") == "ФССП" and isinstance(item.get("data"), dict):
            base.update(item.get("data"))
        elif item.get("title") == "ЕГРН / Росреестр" and isinstance(item.get("data"), dict):
            base["object"] = item.get("data")
            base["encumbrances"] = item.get("data", {}).get("encumbrances", [])
        elif "data" in item:
            base["items"] = item.get("data")
        out[key] = strip_sensitive(base, debug=False)
    return out


def build_legal_report(n: Dict[str, Any], checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> str:
    seller = " ".join(x for x in [n["last"], n["first"], n["middle"]] if x).strip()
    obj = n["property"].get("query") or "по предоставленным данным не указан"
    ok = [x for x in checklist if x.get("status") == "ok"]
    risks = [x for x in checklist if x.get("status") == "risk"]
    manuals = [x for x in checklist if x.get("status") == "manual_check"]
    lines = []
    lines.append("1. Краткий вывод")
    lines.append(f"Сделка оценена как: {scoring['level'].upper()} ({scoring['score']}/100). {scoring['conclusion']}")
    lines.append("\n2. Что проверено")
    lines.append(f"Продавец: {seller}. Дата рождения: {n.get('dob') or 'не указана'}. ИНН: {'передан' if n.get('inn') else 'не передан'}. Объект: {obj}.")
    lines.append(f"Проверено без явных рисков: {len(ok)}. Рисков: {len(risks)}. Требуют ручной проверки: {len(manuals)}.")
    lines.append("\n3. Основные риски")
    if risks:
        for r in risks:
            lines.append(f"- {r['title']}: {r['summary']}")
    else:
        lines.append("- По автоматическим источникам явные риски не выявлены.")
    lines.append("\n4. Что говорит в пользу сделки")
    if ok:
        for o in ok:
            lines.append(f"- {o['title']}: {o['summary']}")
    else:
        lines.append("- Нет блоков, которые можно считать полностью подтвержденными без замечаний.")
    lines.append("\n5. Что обязательно сделать до аванса")
    for r in recs:
        lines.append(f"- {r['title']}: {r['text']}")
    lines.append("\n6. Безопасная схема расчетов")
    lines.append("При выявленных долгах, запретах или неполных данных не передавать деньги напрямую продавцу. Использовать нотариальный депозит, аккредитив или иную условную схему с раскрытием денег только после выполнения условий.")
    lines.append("\n7. Итоговое заключение")
    lines.append("Отчет не обещает 100% безопасность сделки. При выявленных ограничениях, активных ИП или судебных делах сделка должна проходить только после ручного юридического анализа документов и условий расчетов.")
    return "\n".join(lines)


def register_pdf_fonts():
    try:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for p in candidates:
            if Path(p).exists():
                pdfmetrics.registerFont(TTFont("BaseSans", p))
                return "BaseSans"
    except Exception:
        pass
    return "Helvetica"


def make_pdf(report_id: str, n: Dict[str, Any], checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], scoring: Dict[str, Any], recs: List[Dict[str, str]], legal_report: str) -> Optional[Path]:
    if SimpleDocTemplate is None:
        return None
    font = register_pdf_fonts()
    path = REPORT_DIR / f"{report_id}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleRu", fontName=font, fontSize=18, leading=22, spaceAfter=12, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="H2Ru", fontName=font, fontSize=12.5, leading=16, spaceBefore=12, spaceAfter=7, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="BodyRu", fontName=font, fontSize=9.5, leading=13, spaceAfter=5))
    styles.add(ParagraphStyle(name="SmallRu", fontName=font, fontSize=8, leading=11, textColor=colors.HexColor("#555555")))
    body = styles["BodyRu"]
    h2 = styles["H2Ru"]
    story = []
    story.append(Paragraph("Юридический отчет по проверке продавца и объекта недвижимости", styles["TitleRu"]))
    story.append(Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles["SmallRu"]))
    story.append(Spacer(1, 8))
    level = scoring.get("level", "").upper()
    score = scoring.get("score", 0)
    score_table = Table([[Paragraph("Сделка", body), Paragraph(level, body)], [Paragraph("Риск", body), Paragraph(f"{score}/100", body)], [Paragraph("Вывод", body), Paragraph(scoring.get("conclusion", ""), body)]], colWidths=[90, 390])
    score_table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#F5F3EF")), ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#DDDDDD")), ("VALIGN", (0,0), (-1,-1), "TOP"), ("FONTNAME", (0,0), (-1,-1), font), ("PADDING", (0,0), (-1,-1), 6)]))
    story.append(score_table)
    story.append(Paragraph("Данные продавца и объекта", h2))
    seller = " ".join(x for x in [n["last"], n["first"], n["middle"]] if x).strip()
    story.append(Paragraph(f"Продавец: {seller}<br/>Дата рождения: {n.get('dob') or 'не указана'}<br/>ИНН: {'передан' if n.get('inn') else 'не передан'}<br/>Объект: {n['property'].get('query')}", body))
    story.append(Paragraph("Чек-лист проверок", h2))
    rows = [[Paragraph("Источник", body), Paragraph("Статус", body), Paragraph("Краткий вывод", body)]]
    for it in checklist:
        status = {"ok": "Проверено", "risk": "Риск", "manual_check": "Ручная проверка"}.get(it.get("status"), it.get("status"))
        rows.append([Paragraph(it.get("title", ""), body), Paragraph(status, body), Paragraph(it.get("summary", ""), body)])
    t = Table(rows, colWidths=[145, 95, 240])
    t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#EAF1F5")), ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#CCCCCC")), ("VALIGN", (0,0), (-1,-1), "TOP"), ("FONTNAME", (0,0), (-1,-1), font), ("PADDING", (0,0), (-1,-1), 5)]))
    story.append(t)
    # Key facts blocks
    fssp = registry_data.get("fssp", {})
    if fssp:
        story.append(Paragraph("ФССП: исполнительные производства", h2))
        story.append(Paragraph(f"Всего ИП: {fssp.get('all_count', 0)}. Активные: {fssp.get('active_count', 0)}. Закрытые: {fssp.get('closed_count', 0)}. Актуальный долг: {rub(fssp.get('actual_debt', 0))}.", body))
    egrn = registry_data.get("egrn", {})
    if egrn:
        story.append(Paragraph("ЕГРН / Росреестр", h2))
        obj = egrn.get("object") or {}
        address = obj.get("address", {}).get("readableAddress") if isinstance(obj.get("address"), dict) else ""
        story.append(Paragraph(f"Кадастровый номер: {obj.get('cadNumber', '')}<br/>Адрес: {address}<br/>Площадь: {obj.get('area', '')} кв.м", body))
        enc = egrn.get("encumbrances") or []
        if enc:
            story.append(Paragraph("Выявленные ограничения/обременения:", body))
            for e in enc:
                desc = clean_text(e.get("typeDesc")) or f"Тип {e.get('type', '')}"
                story.append(Paragraph(f"• {desc}, № {e.get('encumbranceNumber', '')}, дата: {e.get('startDate') or e.get('encumbranceDate') or ''}", body))
    story.append(Paragraph("Рекомендации", h2))
    for r in recs:
        story.append(Paragraph(f"<b>{r.get('title')}</b>: {r.get('text')}", body))
    story.append(Paragraph("Юридическое заключение", h2))
    for para in legal_report.split("\n"):
        if para.strip():
            story.append(Paragraph(para.strip().replace("&", "&amp;"), body))
    story.append(Paragraph("Дисклеймер", h2))
    story.append(Paragraph(DISCLAIMER, styles["SmallRu"]))
    doc.build(story)
    return path

# ---------- endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0-polling-final", "newdb_url": NEWDB_URL, "poll_interval": NEWDB_POLL_INTERVAL, "default_wait": NEWDB_DEFAULT_MAX_WAIT, "egrn_wait": NEWDB_EGRN_MAX_WAIT}

@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    try:
        n, payloads, responses = await run_all_checks(req, debug=True)
        checklist = classify_all(responses, n)
        registry_data = registry_data_from_checklist(checklist)
        scoring, recs = score_and_recommend(checklist)
        legal_report = build_legal_report(n, checklist, scoring, recs)
        return {"success": True, "payloads": payloads, "responses": strip_sensitive(responses, debug=True), "checklist": checklist, "classified_checklist": checklist, "registry_data": registry_data, "risk_scoring": scoring, "recommendations": recs, "legal_report": legal_report, "normalized_input": n, "notes": ["Polling включен: queued/restart/in progress ожидаются до complete.", "ГАС пробует основной метод и fallback; если оба не приняты — manual_check.", "Клиентский /check-report очищает служебные поля newDB."]}
    except Exception as e:
        return {"success": False, "stage": "debug-newdb", "error": str(e)}

@app.post("/check-report")
async def check_report(req: CheckRequest):
    try:
        n, payloads, responses = await run_all_checks(req, debug=False)
        checklist = classify_all(responses, n)
        registry_data = registry_data_from_checklist(checklist)
        scoring, recs = score_and_recommend(checklist)
        legal_report = build_legal_report(n, checklist, scoring, recs)
        report_id = str(uuid.uuid4())
        pdf_path = None
        try:
            pdf_path = make_pdf(report_id, n, checklist, registry_data, scoring, recs, legal_report)
        except Exception:
            pdf_path = None
        result = {"success": True, "report_id": report_id, "checklist": checklist, "registry_data": registry_data, "risk_scoring": scoring, "recommendations": recs, "legal_report": legal_report, "pdf_available": bool(pdf_path and pdf_path.exists()), "pdf_url": f"/download-pdf/{report_id}" if pdf_path else "", "warnings": [], "disclaimer": DISCLAIMER}
        # Compatibility with older widget that expects pdf_base64 is intentionally not used: stable endpoint is /download-pdf/{report_id}.
        meta_path = REPORT_DIR / f"{report_id}.json"
        try:
            meta_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return strip_sensitive(result, debug=False)
    except Exception as e:
        return {"success": False, "message": "Не удалось сформировать отчет. Требуется ручная проверка.", "warnings": [f"Backend error: {clean_text(e)}"]}

@app.get("/download-pdf/{report_id}")
def download_pdf(report_id: str):
    safe = re.sub(r"[^a-fA-F0-9\-]", "", report_id)
    path = REPORT_DIR / f"{safe}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF не найден или срок хранения истек.")
    return FileResponse(str(path), media_type="application/pdf", filename=f"legal_report_{safe}.pdf")
