
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime
import asyncio
import base64
import html
import json
import os
import re
import traceback
import uuid

import httpx

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
        PageBreak,
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None


app = FastAPI(title="Real Estate Seller & Property Check API", version="1.0.0-prod-anticrash")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip()
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()

GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()

METHOD_PASSPORT = os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd").strip()
METHOD_FSSP = os.getenv("NEWDB_METHOD_FSSP", "fssp_person").strip()
METHOD_BANKRUPTCY = os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankrot_person").strip()
METHOD_ARBITR = os.getenv("NEWDB_METHOD_ARBITR", "arbitr_person").strip()
METHOD_EGRN = os.getenv("NEWDB_METHOD_EGRN", "rosreestr").strip()
METHOD_PRAVOSUD_PRIMARY = os.getenv("NEWDB_METHOD_PRAVOSUD", "pravo_search").strip()
METHOD_PRAVOSUD_FALLBACK = os.getenv("NEWDB_METHOD_PRAVOSUD_FALLBACK", "pravosudfiz").strip()

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=25.0, read=360.0, write=60.0, pool=30.0)

MANUAL_URLS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "arbitr": "https://kad.arbitr.ru",
    "pravosud": "https://sudrf.ru",
    "egrn": "https://rosreestr.gov.ru",
}

IN_PROGRESS_STATES = {"queued", "queue", "in progress", "progress", "pending", "processing", "wait", "waiting", "restart"}
GOOD_STATES = {"complete", "completed", "done", "success", "finished", "ready", "ok"}
BAD_STATES = {"failed", "fail", "error", "rejected", "denied", "timeout", "manual", "not_configured"}


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
    passport_number: str = ""
    series: str = ""
    number: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    cadnum: str = ""
    address: str = ""


def clean_text(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    try:
        if "Р" in s or "С" in s:
            fixed = s.encode("latin1").decode("utf-8")
            if fixed:
                s = fixed
    except Exception:
        pass
    return re.sub(r"\s+", " ", s).strip()


def only_digits(v: Any) -> str:
    return re.sub(r"\D+", "", clean_text(v))


def normalize_dob(v: str) -> Tuple[str, str]:
    s = clean_text(v)
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
        d, m, y = s.split(".")
        return s, f"{y}-{m}-{d}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, m, d = s.split("-")
        return f"{d}.{m}.{y}", s
    return s, s


def normalize_inn(req: CheckRequest) -> str:
    raw = req.inn or req.seller_inn or req.inn_fiz or req.innfiz or req.innfl
    digits = only_digits(raw)
    return digits if len(digits) == 12 else ""


def rub(v: Any) -> str:
    try:
        n = float(v or 0)
    except Exception:
        n = 0.0
    s = f"{n:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return s + " ₽"


def state_of(d: Any) -> str:
    if not isinstance(d, dict):
        return ""
    return clean_text(d.get("state") or d.get("status") or "").lower()


def safe_json_dumps(obj: Any, limit: Optional[int] = None) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        s = str(obj)
    return s[:limit] if limit else s


def text_blob(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()


def has_newdb_error(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return True
    if resp.get("errors_info"):
        return True
    if resp.get("_http_status") and int(resp.get("_http_status", 0)) >= 400:
        return True
    st = state_of(resp)
    if st in BAD_STATES:
        return True
    t = text_blob(resp)
    markers = [
        "not enough balance", "insufficient balance", "balance", "payment required",
        "method or country is not valid", "error_code", "unauthorized", "forbidden",
        "x-api-key", "access token", "токен", "недостаточно", "лимит"
    ]
    return any(m in t for m in markers)


def strip_sensitive(obj: Any, keep_debug: bool = False) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in {"token", "api_key", "x-api-key", "authorization"}:
                continue
            if not keep_debug and kl in {"requestid", "request_id", "newdb_qid", "balance", "taskid", "datecreated", "dateupdated", "params"}:
                continue
            if not keep_debug and kl in {"errors_info", "docs_url", "_http_status", "sent_params"}:
                continue
            out[k] = strip_sensitive(v, keep_debug=keep_debug)
        return out
    if isinstance(obj, list):
        return [strip_sensitive(x, keep_debug=keep_debug) for x in obj]
    return obj


def manual_item(title: str, summary: str = "Источник не вернул данные. Требуется ручная проверка.", details: Optional[List[str]] = None, url: str = "") -> dict:
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


def ok_item(title: str, summary: str, details: Optional[List[str]] = None, url: str = "", data: Any = None) -> dict:
    d = {
        "title": title,
        "source": title,
        "status": "ok",
        "ui_status": "ok",
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
        "manual_url": url,
    }
    if data is not None:
        d["data"] = data
    return d


def risk_item(title: str, summary: str, details: Optional[List[str]] = None, url: str = "", data: Any = None) -> dict:
    d = {
        "title": title,
        "source": title,
        "status": "risk",
        "ui_status": "risk",
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
        "manual_url": url,
    }
    if data is not None:
        d["data"] = data
    return d


def get_result_data(resp: Any, method: str) -> Tuple[Optional[Any], Optional[dict]]:
    """Never raises. Returns (data, result_block)."""
    try:
        if not isinstance(resp, dict) or has_newdb_error(resp):
            return None, None
        block = (resp.get("results") or {}).get(method)
        if not isinstance(block, dict):
            # sometimes method name may be different; take first results block if safe
            results = resp.get("results") or {}
            if isinstance(results, dict) and len(results) == 1:
                block = next(iter(results.values()))
            else:
                return None, None
        result = block.get("result") if isinstance(block, dict) else None
        if not isinstance(result, dict):
            return None, result
        if int(result.get("status", 200) or 200) >= 400:
            return None, result
        data = result.get("data")
        return data, result
    except Exception:
        return None, None


def extract_newdb_error_reason(resp: Any) -> str:
    try:
        if isinstance(resp, dict):
            if resp.get("errors_info"):
                msg = resp["errors_info"][0].get("error") if isinstance(resp["errors_info"], list) and resp["errors_info"] else ""
                if msg:
                    if "method or country" in msg:
                        return "Источник не принял метод или страну запроса. Требуется уточнить метод newDB."
                    return clean_text(msg)
            t = text_blob(resp)
            if "balance" in t or "not enough" in t or "insufficient" in t or "лимит" in t:
                return "Вероятно, исчерпан лимит/баланс API newDB или источник временно недоступен."
    except Exception:
        pass
    return "Источник не вернул данные. Требуется ручная проверка."


async def newdb_post(params: dict, max_wait: int = 120, poll_interval: int = 5) -> dict:
    """Anti-crash NewDB client. Never raises."""
    if not NEWDB_TOKEN:
        return {"state": "not_configured", "error": "NEWDB_TOKEN не задан."}

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }
    request_id = str(uuid.uuid4())
    payload = {"params": params, "requestId": request_id}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            r = await client.post(NEWDB_URL, headers=headers, json=payload)
            try:
                data = r.json()
            except Exception:
                data = {"raw_text": r.text}
            if isinstance(data, dict):
                data["_http_status"] = r.status_code
            else:
                data = {"raw": data, "_http_status": r.status_code}
        except Exception as e:
            return {"state": "error", "error": f"Ошибка запроса newDB: {e}"}

        if r.status_code >= 400 or has_newdb_error(data):
            return data

        st = state_of(data)
        if st in GOOD_STATES:
            return data
        if not st and isinstance(data, dict) and data.get("results"):
            return data

        elapsed = 0
        last = data

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            poll_payload = {"params": params, "requestId": request_id}
            try:
                rr = await client.post(NEWDB_URL, headers=headers, json=poll_payload)
                try:
                    polled = rr.json()
                except Exception:
                    polled = {"raw_text": rr.text}
                if isinstance(polled, dict):
                    polled["_http_status"] = rr.status_code
                else:
                    polled = {"raw": polled, "_http_status": rr.status_code}
                last = polled

                if rr.status_code >= 400 or has_newdb_error(polled):
                    return polled

                pst = state_of(polled)
                if pst in GOOD_STATES or (isinstance(polled, dict) and polled.get("results")):
                    return polled
                if pst not in IN_PROGRESS_STATES and pst:
                    return polled
            except Exception as e:
                last = {"state": "error", "error": f"Ошибка polling newDB: {e}"}
                continue

        if isinstance(last, dict):
            last["state"] = "timeout"
            last["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд."
            return last
        return {"state": "timeout", "error": f"Источник не вернул итоговый результат за {max_wait} секунд."}


def build_payloads(req: CheckRequest) -> Tuple[dict, dict]:
    dob_display, dob_iso = normalize_dob(req.dob)
    inn = normalize_inn(req)

    last = clean_text(req.last)
    first = clean_text(req.first)
    middle = clean_text(req.middle)

    ps = only_digits(req.passport_series or req.passport_seria or req.series)
    pn = only_digits(req.passport_number or req.number)

    cad = clean_text(req.cadastral_number or req.cadastre_number or req.cadnum)
    addr = clean_text(req.address)
    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"
    if cad and re.match(cad_pattern, cad):
        prop_query = cad
        prop_type = "cadastral"
    elif addr and re.match(cad_pattern, addr):
        prop_query = addr
        prop_type = "cadastral"
        cad = addr
    else:
        prop_query = addr
        prop_type = "address"

    normalized = {
        "last": last,
        "first": first,
        "middle": middle,
        "dob": dob_display,
        "dob_iso": dob_iso,
        "inn": inn,
        "region": req.region or 78,
        "passport_series": ps,
        "passport_number": pn,
        "property": {
            "query": prop_query,
            "cadastral_number": cad,
            "address": prop_query,
            "type": prop_type,
        },
    }

    fio = " ".join([last, first, middle]).strip()

    payloads = {
        "passport": None,
        "fssp": None,
        "bankruptcy": None,
        "arbitr": None,
        "pravosud": None,
        "egrn": None,
    }

    if ps and pn:
        payloads["passport"] = {
            "seria": ps,
            "number": pn,
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
            "method": METHOD_PASSPORT,
        }

    if last and first and dob_iso:
        payloads["fssp"] = {
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
            "regioncode": int(req.region or 78),
            "country": "ru",
            "method": METHOD_FSSP,
        }

    if inn:
        payloads["bankruptcy"] = {"innfiz": inn, "country": "ru", "method": METHOD_BANKRUPTCY}
        payloads["arbitr"] = {"innfiz": inn, "country": "ru", "method": METHOD_ARBITR}

    if fio:
        payloads["pravosud"] = {
            "method": METHOD_PRAVOSUD_PRIMARY,
            "country": "ru",
            "query": fio,
            "q": fio,
            "fio": fio,
            "lastname": last,
            "firstname": first,
            "secondname": middle,
            "party_name": fio,
            "limit": 50,
        }

    if prop_query:
        payloads["egrn"] = {"address": prop_query, "country": "ru", "method": METHOD_EGRN}

    return normalized, payloads


async def call_source(name: str, payload: Optional[dict], max_wait: int = 120) -> dict:
    if not payload:
        return {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
    try:
        return await newdb_post(payload, max_wait=max_wait)
    except Exception as e:
        return {"state": "error", "error": f"Внутренняя ошибка источника {name}: {e}"}


async def call_pravosud_with_fallback(payload: Optional[dict]) -> dict:
    if not payload:
        return {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
    primary = await call_source("pravosud", payload, max_wait=120)
    if has_newdb_error(primary) and METHOD_PRAVOSUD_FALLBACK and METHOD_PRAVOSUD_FALLBACK != payload.get("method"):
        fallback_payload = dict(payload)
        fallback_payload["method"] = METHOD_PRAVOSUD_FALLBACK
        fallback = await call_source("pravosud_fallback", fallback_payload, max_wait=120)
        if isinstance(fallback, dict):
            fallback["_fallback_tried"] = METHOD_PRAVOSUD_FALLBACK
            fallback["_primary_error"] = strip_sensitive(primary, keep_debug=True)
        return fallback
    return primary


def classify_passport(resp: dict, payload: Optional[dict]) -> dict:
    title = "Паспорт МВД"
    if not payload:
        return manual_item(title, "Паспорт не проверялся автоматически: не переданы серия и номер паспорта.", ["Для автоматической проверки МВД укажите серию и номер паспорта."], MANUAL_URLS["passport"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник не вернул данные по паспорту. Требуется ручная проверка.", [extract_newdb_error_reason(resp)], MANUAL_URLS["passport"])
    data, _ = get_result_data(resp, METHOD_PASSPORT)
    if not data:
        return manual_item(title, "Источник не вернул понятный результат проверки паспорта. Требуется ручная проверка.", [], MANUAL_URLS["passport"])
    t = text_blob(data)
    if "недейств" in t or "invalid" in t or "разыскивается" in t:
        return risk_item(title, "Выявлены признаки проблемы с паспортом.", ["Результат МВД требует ручной проверки."], MANUAL_URLS["passport"], data=data)
    if "действител" in t or "valid" in t:
        details = []
        try:
            if isinstance(data, list) and data and isinstance(data[0], dict):
                details.append(clean_text(data[0].get("doc_status") or "Действительный"))
        except Exception:
            pass
        return ok_item(title, "Паспорт по полученным данным действителен.", details or ["Действительный"], MANUAL_URLS["passport"], data=data)
    return manual_item(title, "Результат проверки паспорта неоднозначный. Требуется ручная проверка.", [], MANUAL_URLS["passport"])


def extract_debt_amount(item: dict, active: bool) -> float:
    if not isinstance(item, dict):
        return 0.0
    text = clean_text(item.get("SubjectAndDebtAmount") or "")
    # Prefer active remainder if active.
    patterns = []
    if active:
        patterns.extend([
            r"Остаток долга[^:]*:\s*([\d\s]+(?:[.,]\d{1,2})?)\s*руб",
            r"Сумма долга:\s*([\d\s]+(?:[.,]\d{1,2})?)\s*руб",
        ])
    else:
        patterns.append(r"Сумма долга:\s*([\d\s]+(?:[.,]\d{1,2})?)\s*руб")
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            raw = m.group(1).replace(" ", "").replace(",", ".")
            try:
                return float(raw)
            except Exception:
                return 0.0
    return 0.0


def is_closed_fssp(item: dict) -> bool:
    txt = text_blob(item)
    completion = clean_text((item or {}).get("CompletionDateOrReason"))
    if completion:
        return True
    closed_words = ["оконч", "прекращ", "закрыт", "заверш", "ст. 46", "статья 46", "closed", "terminated"]
    return any(w in txt for w in closed_words)


def classify_fssp(resp: dict, payload: Optional[dict]) -> dict:
    title = "ФССП"
    if not payload:
        return manual_item(title, "ФССП не проверялся автоматически: не хватает ФИО/даты рождения.", [], MANUAL_URLS["fssp"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник ФССП не вернул данные. Требуется ручная проверка.", [extract_newdb_error_reason(resp)], MANUAL_URLS["fssp"])
    data, _ = get_result_data(resp, METHOD_FSSP)
    if data is None:
        return manual_item(title, "Источник ФССП не вернул понятный результат. Требуется ручная проверка.", [], MANUAL_URLS["fssp"])
    if not isinstance(data, list) or not data:
        item = ok_item(title, "По полученным данным исполнительные производства не найдены.", ["ФССП искался по ФИО, дате рождения и региону. При сомнениях проверьте вручную перед авансом."], MANUAL_URLS["fssp"], data={
            "all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0,
            "total_sum_all": 0, "active_sum": 0, "closed_sum": 0, "unknown_sum": 0,
            "actual_debt": 0, "active_items": [], "closed_items": [], "unknown_items": []
        })
        return item

    active, closed, unknown = [], [], []
    for x in data:
        if not isinstance(x, dict):
            unknown.append(x)
            continue
        if is_closed_fssp(x):
            closed.append(x)
        elif clean_text(x.get("EnforcementProceeding")) or clean_text(x.get("SubjectAndDebtAmount")):
            active.append(x)
        else:
            unknown.append(x)

    active_sum = sum(extract_debt_amount(x, active=True) for x in active if isinstance(x, dict))
    closed_sum = sum(extract_debt_amount(x, active=False) for x in closed if isinstance(x, dict))
    unknown_sum = sum(extract_debt_amount(x, active=True) for x in unknown if isinstance(x, dict))
    total = active_sum + closed_sum + unknown_sum
    actual = active_sum + unknown_sum

    stats = {
        "all_count": len(data),
        "active_count": len(active),
        "closed_count": len(closed),
        "unknown_count": len(unknown),
        "total_sum_all": round(total, 2),
        "active_sum": round(active_sum, 2),
        "closed_sum": round(closed_sum, 2),
        "unknown_sum": round(unknown_sum, 2),
        "actual_debt": round(actual, 2),
        "active_items": active,
        "closed_items": closed,
        "unknown_items": unknown,
    }

    details = [
        f"Всего найдено ИП: {len(data)}",
        f"Активные ИП: {len(active)}",
        f"Закрытые/оконченные ИП: {len(closed)}",
        f"Неоднозначные записи: {len(unknown)}",
        f"Общая сумма всех найденных ИП: {rub(total)}",
        f"Сумма по активным ИП: {rub(active_sum)}",
        f"Сумма по закрытым ИП: {rub(closed_sum)}",
        f"Актуальный долг по активным/неоднозначным ИП: {rub(actual)}",
    ]

    if active or unknown:
        item = risk_item(title, f"Найдены активные или неоднозначные исполнительные производства. Актуальная сумма для ручной оценки: {rub(actual)}.", details, MANUAL_URLS["fssp"], data=stats)
    else:
        item = ok_item(title, f"Найдены только закрытые/оконченные ИП. Актуальный долг по активным ИП: {rub(0)}.", details, MANUAL_URLS["fssp"], data=stats)
    item["fssp_stats"] = stats
    return item


def classify_bankruptcy(resp: dict, payload: Optional[dict]) -> dict:
    title = "Банкротство / Федресурс"
    if not payload:
        return manual_item(title, "Банкротство не проверялось автоматически: не передан 12-значный ИНН физлица.", ["Укажите ИНН физлица из 12 цифр."], MANUAL_URLS["bankruptcy"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник банкротств не вернул данные. Требуется ручная проверка.", [extract_newdb_error_reason(resp)], MANUAL_URLS["bankruptcy"])
    data, _ = get_result_data(resp, METHOD_BANKRUPTCY)
    if data is None:
        return manual_item(title, "Источник банкротств не вернул понятный результат. Требуется ручная проверка.", [], MANUAL_URLS["bankruptcy"])
    t = text_blob(data)
    # Positive empty response usually contains bankruptcy: [], publications: []
    has_records = False
    try:
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                for key in ["bankruptcy", "publications", "encumbrances"]:
                    val = row.get(key)
                    if isinstance(val, list) and len(val) > 0:
                        has_records = True
        elif isinstance(data, dict):
            has_records = any(isinstance(data.get(k), list) and data.get(k) for k in ["bankruptcy", "publications", "encumbrances"])
    except Exception:
        has_records = bool(data)
    if has_records:
        return risk_item(title, "Выявлены сведения, связанные с банкротством или публикациями Федресурса.", [], MANUAL_URLS["bankruptcy"], data=data)
    return ok_item(title, "По полученным данным сведения о банкротстве физлица не выявлены.", [], MANUAL_URLS["bankruptcy"], data=data)


def classify_arbitr(resp: dict, payload: Optional[dict]) -> dict:
    title = "Арбитражные суды"
    if not payload:
        return manual_item(title, "Арбитражные суды не проверялись автоматически: не передан 12-значный ИНН физлица.", ["Укажите ИНН физлица из 12 цифр."], MANUAL_URLS["arbitr"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник арбитражных судов не вернул данные. Требуется ручная проверка.", [extract_newdb_error_reason(resp)], MANUAL_URLS["arbitr"])
    data, _ = get_result_data(resp, METHOD_ARBITR)
    if data is None:
        return manual_item(title, "Источник арбитражных судов не вернул понятный результат. Требуется ручная проверка.", [], MANUAL_URLS["arbitr"])

    cases = []
    found = False
    try:
        rows = data if isinstance(data, list) else [data]
        for r in rows:
            if isinstance(r, dict):
                found = found or bool(r.get("found"))
                if isinstance(r.get("cases"), list):
                    cases.extend(r.get("cases") or [])
    except Exception:
        pass
    if found or cases:
        details = [f"Найдено дел: {len(cases) if cases else 'требуется ручной анализ'}"]
        return risk_item(title, "Найдены арбитражные дела. Требуется анализ предмета спора.", details, MANUAL_URLS["arbitr"], data=cases or data)
    return ok_item(title, "По полученным данным арбитражные дела не выявлены.", [], MANUAL_URLS["arbitr"], data=[])


def classify_pravosud(resp: dict, payload: Optional[dict]) -> dict:
    title = "Суды общей юрисдикции / ГАС Правосудие"
    if not payload:
        return manual_item(title, "ГАС Правосудие не проверялся автоматически: не передано ФИО.", [], MANUAL_URLS["pravosud"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник не вернул данные. Требуется ручная проверка.", [extract_newdb_error_reason(resp)], MANUAL_URLS["pravosud"])

    # Try both possible method names and any single results block
    data = None
    result = None
    for m in [METHOD_PRAVOSUD_PRIMARY, METHOD_PRAVOSUD_FALLBACK, "pravo_search", "pravosudfiz"]:
        data, result = get_result_data(resp, m)
        if data is not None:
            break

    if data is None:
        return manual_item(title, "Источник не вернул понятный результат. Требуется ручная проверка.", [], MANUAL_URLS["pravosud"])

    cases = []
    found = False

    def collect(x):
        nonlocal found, cases
        if isinstance(x, dict):
            if x.get("found") is True:
                found = True
            for key in ["cases", "items", "data", "results", "rows", "list"]:
                val = x.get(key)
                if isinstance(val, list):
                    if key in {"cases", "items", "rows", "list"}:
                        cases.extend(val)
                    else:
                        for z in val:
                            collect(z)
                elif isinstance(val, dict):
                    collect(val)
        elif isinstance(x, list):
            for z in x:
                collect(z)

    collect(data)

    # If data is list of case-like dicts without wrapper
    if isinstance(data, list) and data:
        if cases:
            pass
        else:
            if any(isinstance(x, dict) and any(k in x for k in ["case_number", "caseNumber", "num", "date", "court", "category"]) for x in data):
                cases = data

    if found or cases:
        clean_details = [f"Найдено записей: {len(cases) if cases else 'требуется ручной анализ'}"]
        return risk_item(title, "Найдены дела в судах общей юрисдикции. Требуется анализ предмета спора.", clean_details, MANUAL_URLS["pravosud"], data=cases or data)

    return ok_item(title, "По полученным данным дела в судах общей юрисдикции не выявлены.", [], MANUAL_URLS["pravosud"], data=[])


def classify_egrn(resp: dict, payload: Optional[dict]) -> dict:
    title = "ЕГРН / Росреестр"
    if not payload:
        return manual_item(title, "ЕГРН не проверялся автоматически: не передан адрес или кадастровый номер.", [], MANUAL_URLS["egrn"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник ЕГРН не вернул данные. Требуется ручная проверка.", [extract_newdb_error_reason(resp)], MANUAL_URLS["egrn"])
    data, _ = get_result_data(resp, METHOD_EGRN)
    if not isinstance(data, list) or not data:
        return manual_item(title, "Росреестр не вернул данные объекта. Требуется ручная проверка.", [], MANUAL_URLS["egrn"])
    obj = data[0] if isinstance(data[0], dict) else {}
    if not obj:
        return manual_item(title, "Росреестр вернул непонятный результат. Требуется ручная проверка.", [], MANUAL_URLS["egrn"])

    address = ""
    try:
        address = clean_text((obj.get("address") or {}).get("readableAddress"))
    except Exception:
        pass

    details = []
    cad = clean_text(obj.get("cadNumber"))
    if cad:
        details.append(f"Кадастровый номер: {cad}")
    if address:
        details.append(f"Адрес: {address}")
    if clean_text(obj.get("objType_text")):
        details.append(f"Тип объекта: {clean_text(obj.get('objType_text'))}")
    if clean_text(obj.get("purpose_text")):
        details.append(f"Назначение: {clean_text(obj.get('purpose_text'))}")
    if clean_text(obj.get("area")):
        details.append(f"Площадь: {clean_text(obj.get('area'))} кв.м")
    if clean_text(obj.get("cadCost")):
        details.append(f"Кадастровая стоимость: {rub(obj.get('cadCost'))}")
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    details.append(f"Записей о правах: {len(rights)}")

    enc = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    for e in enc:
        if not isinstance(e, dict):
            continue
        desc = clean_text(e.get("typeDesc")) or f"Тип ограничения: {clean_text(e.get('type'))}"
        num = clean_text(e.get("encumbranceNumber"))
        start = clean_text(e.get("startDate"))
        line = desc
        if num:
            line += f", № {num}"
        if start:
            line += f", дата начала: {start}"
        details.append(line)

    if enc:
        return risk_item(title, "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН.", details, MANUAL_URLS["egrn"], data=obj)

    return ok_item(title, "Данные по объекту получены. Явные признаки ограничений или обременений не выявлены.", details, MANUAL_URLS["egrn"], data=obj)


def build_registry_data(checklist: List[dict]) -> dict:
    out = {}
    mapping = {
        "Паспорт МВД": "passport",
        "ФССП": "fssp",
        "Банкротство / Федресурс": "bankruptcy",
        "Арбитражные суды": "arbitr",
        "Суды общей юрисдикции / ГАС Правосудие": "pravosud",
        "ЕГРН / Росреестр": "egrn",
    }
    for item in checklist:
        key = mapping.get(item.get("title"), item.get("title"))
        base = {
            "title": item.get("title"),
            "status": item.get("status"),
            "summary": item.get("summary"),
            "details": item.get("details", []),
        }
        data = item.get("data")
        if item.get("title") == "ФССП" and isinstance(data, dict):
            base.update(data)
        elif item.get("title") == "ЕГРН / Росреестр" and isinstance(data, dict):
            base["object"] = data
            base["encumbrances"] = data.get("encumbrances", [])
        elif data is not None:
            base["items"] = data
        out[key] = strip_sensitive(base, keep_debug=False)
    return out


def build_risk_scoring(checklist: List[dict]) -> dict:
    score = 0
    factors = []
    fssp_actual = 0.0
    for item in checklist:
        title = item.get("title", "")
        status = item.get("status")
        if title == "ЕГРН / Росреестр" and status == "risk":
            score += 60
            factors.append("ЕГРН: выявлены ограничения/обременения (+60)")
        elif title == "ФССП" and status == "risk":
            data = item.get("data") or {}
            fssp_actual = float(data.get("actual_debt") or 0)
            add = 35 if fssp_actual > 0 else 25
            score += add
            factors.append(f"ФССП: активные/неоднозначные ИП, сумма {rub(fssp_actual)} (+{add})")
        elif title == "Банкротство / Федресурс" and status == "risk":
            score += 45
            factors.append("Банкротство/Федресурс: выявлены сведения (+45)")
        elif title in {"Арбитражные суды", "Суды общей юрисдикции / ГАС Правосудие"} and status == "risk":
            score += 25
            factors.append(f"{title}: найдены дела (+25)")
        elif status == "manual_check":
            add = 6 if "ГАС" in title else 8
            score += add
            factors.append(f"{title}: требуется ручная проверка (+{add})")

    score = min(100, int(round(score)))
    if score >= 75:
        level = "опасная"
        conclusion = "Сделку нельзя выводить на аванс без ручного юридического разбора и устранения выявленных факторов."
    elif score >= 35:
        level = "условно рискованная"
        conclusion = "Сделку можно рассматривать только после уточнения рисков и настройки безопасных условий."
    else:
        level = "условно безопасная"
        conclusion = "По автоматическим данным критические риски не выявлены, но ручная проверка документов обязательна."
    return {"score": score, "max_score": 100, "level": level, "conclusion": conclusion, "factors": factors}


def build_recommendations(checklist: List[dict]) -> List[dict]:
    recs = []
    by_title = {i.get("title"): i for i in checklist}

    egrn = by_title.get("ЕГРН / Росреестр")
    if egrn and egrn.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки", "text": "По ЕГРН выявлены ограничения/обременения. До аванса нужно получить основание ограничения и прописать обязанность продавца снять его в конкретный срок."})
        recs.append({"priority": "critical", "title": "Не передавать деньги напрямую продавцу", "text": "При запрете регистрации использовать нотариальный депозит или аккредитив с раскрытием денег только после снятия ограничения и регистрации перехода права."})

    fssp = by_title.get("ФССП")
    if fssp and fssp.get("status") == "risk":
        actual = ((fssp.get("data") or {}).get("actual_debt") or 0)
        recs.append({"priority": "high", "title": "Закрыть активные ИП до сделки или через контролируемые расчеты", "text": f"Актуальная сумма по активным/неоднозначным ИП: {rub(actual)}. В ПДКП прописать порядок погашения и последствия неснятия ограничений."})

    bank = by_title.get("Банкротство / Федресурс")
    if bank and bank.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Не выходить на сделку без анализа банкротного риска", "text": "Сведения о банкротстве требуют отдельного анализа периода, статуса процедуры и риска оспаривания сделки."})

    for t in ["Арбитражные суды", "Суды общей юрисдикции / ГАС Правосудие"]:
        item = by_title.get(t)
        if item and item.get("status") == "risk":
            recs.append({"priority": "high", "title": f"Разобрать дела: {t}", "text": "Нужно понять предмет спора, сумму требований и связь с недвижимостью, долгами или банкротством."})

    if any(i.get("status") == "manual_check" for i in checklist):
        recs.append({"priority": "medium", "title": "Закрыть ручные проверки до аванса", "text": "Все источники со статусом «требуется ручная проверка» нужно проверить вручную до передачи денег."})

    recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса/задатка и ответственность при неподтверждении данных."})
    return recs


def build_legal_report(req: CheckRequest, normalized: dict, checklist: List[dict], scoring: dict, recommendations: List[dict]) -> str:
    seller = " ".join([normalized.get("last", ""), normalized.get("first", ""), normalized.get("middle", "")]).strip()
    obj = normalized.get("property", {}).get("query") or "по предоставленным данным не указан"
    ok_count = sum(1 for i in checklist if i.get("status") == "ok")
    risk_count = sum(1 for i in checklist if i.get("status") == "risk")
    manual_count = sum(1 for i in checklist if i.get("status") == "manual_check")
    risks = [i for i in checklist if i.get("status") == "risk"]
    oks = [i for i in checklist if i.get("status") == "ok"]

    lines = [
        "1. Краткий вывод",
        f"Сделка оценена как: {scoring['level'].upper()} ({scoring['score']}/100). {scoring['conclusion']}",
        "",
        "2. Что проверено",
        f"Продавец: {seller}. Дата рождения: {normalized.get('dob') or 'не указана'}. ИНН: {'передан' if normalized.get('inn') else 'не передан'}.",
        f"Объект: {obj}.",
        f"Проверено без явных рисков: {ok_count}. Рисков: {risk_count}. Требуют ручной проверки: {manual_count}.",
        "",
        "3. Основные риски",
    ]
    if risks:
        for i in risks:
            lines.append(f"- {i['title']}: {i['summary']}")
    else:
        lines.append("- По автоматическим источникам явные риски не выявлены.")

    lines += ["", "4. Что говорит в пользу сделки"]
    if oks:
        for i in oks:
            lines.append(f"- {i['title']}: {i['summary']}")
    else:
        lines.append("- Нет блоков, которые можно считать полностью подтвержденными без замечаний.")

    lines += ["", "5. Что обязательно сделать до аванса"]
    for r in recommendations:
        lines.append(f"- {r['title']}: {r['text']}")

    lines += [
        "",
        "6. Безопасная схема расчетов",
        "При выявленных долгах, запретах или неполных данных не передавать деньги напрямую продавцу. Использовать нотариальный депозит, аккредитив или иную условную схему с раскрытием денег только после выполнения условий.",
        "",
        "7. Итоговое заключение",
        "Отчет не обещает 100% безопасность сделки. При выявленных ограничениях, активных ИП или судебных делах сделка должна проходить только после ручного юридического анализа документов и условий расчетов.",
    ]
    return "\n".join(lines)


def safe_paragraph(text: Any) -> str:
    s = html.escape(clean_text(text))
    return s.replace("\n", "<br/>")


def priority_label(p: str) -> str:
    return {"critical": "Критично", "high": "Высокий приоритет", "medium": "Средний приоритет", "low": "Низкий приоритет"}.get(p, p)


def register_fonts():
    # Try common Linux fonts for Cyrillic. ReportLab built-ins do not support Cyrillic well.
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if Path(regular).exists():
            try:
                pdfmetrics.registerFont(TTFont("DejaVu", regular))
                if Path(bold).exists():
                    pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
                else:
                    pdfmetrics.registerFont(TTFont("DejaVu-Bold", regular))
                return "DejaVu", "DejaVu-Bold"
            except Exception:
                pass
    return "Helvetica", "Helvetica-Bold"


def generate_pdf(report: dict) -> Optional[Path]:
    if SimpleDocTemplate is None:
        return None

    report_id = report["report_id"]
    path = REPORT_DIR / f"{report_id}.pdf"
    font, font_bold = register_fonts()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Юридический отчет",
    )

    styles = {}
    styles["title"] = ParagraphStyle("title", fontName=font_bold, fontSize=20, leading=24, textColor=colors.HexColor("#0F3D56"), alignment=TA_CENTER, spaceAfter=8)
    styles["subtitle"] = ParagraphStyle("subtitle", fontName=font, fontSize=9, leading=12, textColor=colors.HexColor("#6E7F8D"), alignment=TA_CENTER, spaceAfter=14)
    styles["h2"] = ParagraphStyle("h2", fontName=font_bold, fontSize=13, leading=16, textColor=colors.HexColor("#0F3D56"), spaceBefore=10, spaceAfter=7)
    styles["h3"] = ParagraphStyle("h3", fontName=font_bold, fontSize=10.5, leading=13, textColor=colors.HexColor("#111827"), spaceBefore=6, spaceAfter=4)
    styles["body"] = ParagraphStyle("body", fontName=font, fontSize=9, leading=13, textColor=colors.HexColor("#111827"), spaceAfter=4)
    styles["small"] = ParagraphStyle("small", fontName=font, fontSize=8, leading=11, textColor=colors.HexColor("#3C4853"), spaceAfter=3)
    styles["danger"] = ParagraphStyle("danger", fontName=font_bold, fontSize=12, leading=15, textColor=colors.HexColor("#991B1B"), spaceAfter=4)

    story = []
    story.append(Paragraph("Юридический отчет по проверке продавца и объекта недвижимости", styles["title"]))
    story.append(Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles["subtitle"]))

    scoring = report.get("risk_scoring", {})
    level = scoring.get("level", "")
    score = scoring.get("score", 0)

    color = colors.HexColor("#FEF2F2") if score >= 75 else colors.HexColor("#FFFBEB") if score >= 35 else colors.HexColor("#F0FDF4")
    text_color = colors.HexColor("#991B1B") if score >= 75 else colors.HexColor("#92400E") if score >= 35 else colors.HexColor("#166534")

    risk_table = Table([
        [Paragraph(f"Сделка: {str(level).upper()}", styles["danger"]), Paragraph(f"Риск: {score}/100", styles["danger"])],
        [Paragraph(safe_paragraph(scoring.get("conclusion", "")), styles["body"]), ""],
    ], colWidths=[115 * mm, 45 * mm])
    risk_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("BOX", (0, 0), (-1, -1), 1, text_color),
        ("SPAN", (0, 1), (1, 1)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(risk_table)
    story.append(Spacer(1, 8))

    normalized = report.get("normalized_input", {})
    prop = normalized.get("property", {})
    seller = " ".join([normalized.get("last", ""), normalized.get("first", ""), normalized.get("middle", "")]).strip()

    story.append(Paragraph("1. Данные проверки", styles["h2"]))
    data_table = Table([
        [Paragraph("<b>Продавец</b>", styles["body"]), Paragraph(safe_paragraph(seller), styles["body"])],
        [Paragraph("<b>Дата рождения</b>", styles["body"]), Paragraph(safe_paragraph(normalized.get("dob") or "не указана"), styles["body"])],
        [Paragraph("<b>ИНН</b>", styles["body"]), Paragraph("передан" if normalized.get("inn") else "не передан", styles["body"])],
        [Paragraph("<b>Объект</b>", styles["body"]), Paragraph(safe_paragraph(prop.get("query") or "не указан"), styles["body"])],
    ], colWidths=[45 * mm, 115 * mm])
    data_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F3EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(data_table)

    story.append(Paragraph("2. Чек-лист проверок", styles["h2"]))
    rows = [[Paragraph("<b>Источник</b>", styles["body"]), Paragraph("<b>Статус</b>", styles["body"]), Paragraph("<b>Вывод</b>", styles["body"])]]
    for item in report.get("checklist", []):
        status = item.get("status")
        st = "Риск" if status == "risk" else "Проверено" if status == "ok" else "Ручная проверка"
        rows.append([
            Paragraph(safe_paragraph(item.get("title")), styles["small"]),
            Paragraph(safe_paragraph(st), styles["small"]),
            Paragraph(safe_paragraph(item.get("summary")), styles["small"]),
        ])
    table = Table(rows, colWidths=[43 * mm, 32 * mm, 85 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E5E7EB")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F3D56")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    # FSSP premium block
    reg = report.get("registry_data", {})
    fssp = reg.get("fssp") or {}
    if fssp:
        story.append(Paragraph("3. Исполнительные производства ФССП", styles["h2"]))
        stats_rows = [
            [Paragraph("Всего ИП", styles["body"]), Paragraph(str(fssp.get("all_count", 0)), styles["body"])],
            [Paragraph("Активные", styles["body"]), Paragraph(str(fssp.get("active_count", 0)), styles["body"])],
            [Paragraph("Закрытые/оконченные", styles["body"]), Paragraph(str(fssp.get("closed_count", 0)), styles["body"])],
            [Paragraph("Актуальный долг", styles["body"]), Paragraph(rub(fssp.get("actual_debt", 0)), styles["body"])],
        ]
        stats_table = Table(stats_rows, colWidths=[70 * mm, 90 * mm])
        stats_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E5E7EB")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F3EF")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(stats_table)

    egrn = reg.get("egrn") or {}
    if egrn:
        story.append(Paragraph("4. Объект недвижимости / ЕГРН", styles["h2"]))
        obj = egrn.get("object") or {}
        addr = clean_text(((obj.get("address") or {}).get("readableAddress")) if isinstance(obj.get("address"), dict) else "")
        story.append(Paragraph(f"<b>Кадастровый номер:</b> {safe_paragraph(obj.get('cadNumber') or '')}", styles["body"]))
        if addr:
            story.append(Paragraph(f"<b>Адрес:</b> {safe_paragraph(addr)}", styles["body"]))
        if obj.get("area"):
            story.append(Paragraph(f"<b>Площадь:</b> {safe_paragraph(obj.get('area'))} кв.м", styles["body"]))
        enc = egrn.get("encumbrances") or []
        if enc:
            story.append(Paragraph("Выявленные ограничения / обременения:", styles["h3"]))
            for x in enc:
                if not isinstance(x, dict):
                    continue
                desc = clean_text(x.get("typeDesc")) or f"Тип ограничения: {clean_text(x.get('type'))}"
                num = clean_text(x.get("encumbranceNumber"))
                start = clean_text(x.get("startDate"))
                story.append(Paragraph(f"• {safe_paragraph(desc)}{', № ' + safe_paragraph(num) if num else ''}{', дата начала: ' + safe_paragraph(start) if start else ''}", styles["body"]))

    story.append(Paragraph("5. Рекомендации", styles["h2"]))
    for rec in report.get("recommendations", []):
        story.append(Paragraph(f"<b>{safe_paragraph(priority_label(rec.get('priority')))} — {safe_paragraph(rec.get('title'))}</b>", styles["body"]))
        story.append(Paragraph(safe_paragraph(rec.get("text")), styles["small"]))

    story.append(Paragraph("6. Юридическое заключение", styles["h2"]))
    for part in clean_text(report.get("legal_report", "")).split("\n"):
        if not part.strip():
            story.append(Spacer(1, 4))
        elif re.match(r"^\d+\.", part.strip()):
            story.append(Paragraph(safe_paragraph(part), styles["h3"]))
        else:
            story.append(Paragraph(safe_paragraph(part), styles["body"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Дисклеймер", styles["h2"]))
    story.append(Paragraph("Отчет носит информационно-аналитический характер, не является гарантией полной юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом.", styles["small"]))

    try:
        doc.build(story)
        return path
    except Exception:
        return None


def build_report(req: CheckRequest, normalized: dict, responses: dict, payloads: dict) -> dict:
    try:
        checklist = [
            classify_passport(responses.get("passport", {}), payloads.get("passport")),
            classify_fssp(responses.get("fssp", {}), payloads.get("fssp")),
            classify_bankruptcy(responses.get("bankruptcy", {}), payloads.get("bankruptcy")),
            classify_arbitr(responses.get("arbitr", {}), payloads.get("arbitr")),
            classify_pravosud(responses.get("pravosud", {}), payloads.get("pravosud")),
            classify_egrn(responses.get("egrn", {}), payloads.get("egrn")),
        ]
    except Exception as e:
        checklist = [manual_item("Системная обработка", "Внутренняя ошибка классификации. Требуется ручная проверка.", [str(e)], "")]

    registry_data = build_registry_data(checklist)
    scoring = build_risk_scoring(checklist)
    recommendations = build_recommendations(checklist)
    legal_report = build_legal_report(req, normalized, checklist, scoring, recommendations)

    report_id = str(uuid.uuid4())
    report = {
        "success": True,
        "report_id": report_id,
        "checklist": checklist,
        "classified_checklist": checklist,
        "registry_data": registry_data,
        "risk_scoring": scoring,
        "recommendations": recommendations,
        "legal_report": legal_report,
        "pdf_available": False,
        "pdf_url": "",
        "warnings": [],
        "notes": [
            "Залоги движимого имущества отключены и не участвуют в отчете.",
            "Добавлена проверка судов общей юрисдикции / ГАС Правосудие.",
            "Ни один источник не должен валить backend: ошибки источников переводятся в manual_check.",
        ],
        "normalized_input": normalized,
    }

    pdf_path = generate_pdf(report)
    if pdf_path and pdf_path.exists():
        report["pdf_available"] = True
        report["pdf_url"] = f"/download-pdf/{report_id}"
        try:
            report["pdf_base64"] = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        except Exception:
            report["pdf_base64"] = ""
    else:
        report["warnings"].append("PDF временно не сформирован, но данные проверки получены.")

    try:
        (REPORT_DIR / f"{report_id}.json").write_text(json.dumps(strip_sensitive(report, keep_debug=False), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return strip_sensitive(report, keep_debug=False)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "1.0.0-prod-anticrash",
        "newdb_configured": bool(NEWDB_TOKEN),
        "pdf_engine": bool(SimpleDocTemplate),
        "sources": ["passport", "fssp", "bankruptcy", "arbitr", "pravosud", "egrn"],
    }


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    normalized, payloads = build_payloads(req)
    try:
        responses = {
            "passport": await call_source("passport", payloads.get("passport"), max_wait=120),
            "fssp": await call_source("fssp", payloads.get("fssp"), max_wait=160),
            "bankruptcy": await call_source("bankruptcy", payloads.get("bankruptcy"), max_wait=160),
            "arbitr": await call_source("arbitr", payloads.get("arbitr"), max_wait=160),
            "pravosud": await call_pravosud_with_fallback(payloads.get("pravosud")),
            "egrn": await call_source("egrn", payloads.get("egrn"), max_wait=320),
        }
    except Exception as e:
        # Debug must never become HTML 500.
        return {
            "success": False,
            "stage": "source_calls",
            "error": str(e),
            "trace": traceback.format_exc(),
            "normalized_input": normalized,
            "payloads": payloads,
        }

    try:
        report = build_report(req, normalized, responses, payloads)
    except Exception as e:
        return {
            "success": False,
            "stage": "build_report",
            "error": str(e),
            "trace": traceback.format_exc(),
            "normalized_input": normalized,
            "payloads": payloads,
            "responses": strip_sensitive(responses, keep_debug=True),
        }

    return {
        "success": True,
        "payloads": payloads,
        "responses": strip_sensitive(responses, keep_debug=True),
        "checklist": report.get("checklist", []),
        "classified_checklist": report.get("classified_checklist", []),
        "registry_data": report.get("registry_data", {}),
        "risk_scoring": report.get("risk_scoring", {}),
        "recommendations": report.get("recommendations", []),
        "legal_report": report.get("legal_report", ""),
        "normalized_input": normalized,
        "notes": report.get("notes", []),
    }


@app.post("/check-report")
async def check_report(req: CheckRequest):
    try:
        normalized, payloads = build_payloads(req)
        responses = {
            "passport": await call_source("passport", payloads.get("passport"), max_wait=120),
            "fssp": await call_source("fssp", payloads.get("fssp"), max_wait=160),
            "bankruptcy": await call_source("bankruptcy", payloads.get("bankruptcy"), max_wait=160),
            "arbitr": await call_source("arbitr", payloads.get("arbitr"), max_wait=160),
            "pravosud": await call_pravosud_with_fallback(payloads.get("pravosud")),
            "egrn": await call_source("egrn", payloads.get("egrn"), max_wait=320),
        }
        return build_report(req, normalized, responses, payloads)
    except Exception:
        # Public endpoint: safe, no traceback.
        report_id = str(uuid.uuid4())
        return {
            "success": False,
            "report_id": report_id,
            "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.",
            "checklist": [
                manual_item("Системная ошибка", "Сервис временно не смог обработать проверку. Требуется ручная проверка.", [], "")
            ],
            "registry_data": {},
            "legal_report": "Сервис временно не смог сформировать автоматический отчет. Перед авансом требуется ручная проверка продавца и объекта.",
            "pdf_available": False,
            "warnings": ["Техническая ошибка скрыта от пользователя и не влияет на юридический вывод."],
        }


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    safe_id = re.sub(r"[^a-fA-F0-9-]", "", report_id)
    path = REPORT_DIR / f"{safe_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF отчет не найден или срок хранения истек.")
    return FileResponse(str(path), media_type="application/pdf", filename=f"legal_report_{safe_id}.pdf")
