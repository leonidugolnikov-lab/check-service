from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
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
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None

app = FastAPI(title="Real Estate Seller & Property Check API", version="6.0-commercial-scoring")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip().rstrip("/")
NEWDB_METHOD_PASSPORT = os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd").strip()
NEWDB_METHOD_FSSP = os.getenv("NEWDB_METHOD_FSSP", "fssp_person").strip()
NEWDB_METHOD_BANKRUPTCY = os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankrot_person").strip()
NEWDB_METHOD_COURTS = os.getenv("NEWDB_METHOD_COURTS", "arbitr_person").strip()
NEWDB_METHOD_EGRN = os.getenv("NEWDB_METHOD_EGRN", "rosreestr").strip()

GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
GIGACHAT_VERIFY_SSL_CERTS = os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "false").strip().lower() in {"1", "true", "yes"}

REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports"))
REPORT_DIR.mkdir(parents=True, exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=25.0, read=300.0, write=60.0, pool=30.0)

MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "courts": "https://kad.arbitr.ru",
    "egrn": "https://rosreestr.gov.ru",
}

DISCLAIMER = (
    "Отчет носит информационно-аналитический характер, не является гарантией полной "
    "юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом."
)

class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    innfiz: str = ""
    inn_fiz: str = ""
    seller_inn: str = ""
    region: int = 78
    passport_series: str = ""
    passport_number: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    address: str = ""

    class Config:
        extra = "allow"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            ru_count = sum(ch in fixed for ch in "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯабвгдежзийклмнопрстуфхцчшщэюя")
            if fixed and ru_count > 2:
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
        amount = float(value or 0)
        s = f"{amount:,.2f}".replace(",", " ")
        if s.endswith(".00"):
            s = s[:-3]
        return s + " ₽"
    except Exception:
        return "0 ₽"


def json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def lower_blob(value: Any) -> str:
    return json_text(value).lower()


def unique_keep_order(items: List[str]) -> List[str]:
    out, seen = [], set()
    for x in items:
        s = clean_text(x)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def deep_get(obj: Any, path: List[Any], default=None):
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key, default)
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
    return cur


def strip_internal(obj: Any) -> Any:
    forbidden = {
        "token", "authorization", "x-api-key", "api_key",
        "requestid", "request_id", "newdb_qid", "taskid",
        "datecreated", "dateupdated", "is_repeat",
        "sent_params", "_http_status", "http_status",
        "docs_url", "errors_info", "api docs", "balance",
    }
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in forbidden:
                continue
            if isinstance(v, str) and "newdb.net/docs" in v:
                continue
            cleaned[k] = strip_internal(v)
        return cleaned
    if isinstance(obj, list):
        return [strip_internal(x) for x in obj]
    return obj


def flatten_human_values(obj: Any, limit: int = 40) -> List[str]:
    out: List[str] = []
    skip_keys = {"requestid", "request_id", "newdb_qid", "taskid", "balance", "method", "country", "datecreated", "dateupdated", "status", "state"}
    skip_values = {"ru", "ok", "done", "complete", "completed", "success", "queued", "pending", "processing", "restart"}

    def walk(x: Any):
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in skip_keys:
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            s = clean_text(x)
            if not s or s.lower() in skip_values:
                return
            if "newdb.net/docs" in s:
                return
            if re.fullmatch(r"[a-f0-9-]{20,}", s.lower()):
                return
            if len(s) <= 300:
                out.append(s)
    walk(obj)
    return unique_keep_order(out)


def status_item(title: str, status: str, summary: str, details: Optional[List[str]] = None, manual_url: str = "", raw_data: Any = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    status = status if status in {"ok", "risk", "manual_check"} else "manual_check"
    item = {
        "title": title,
        "source": title,
        "status": status,
        "ui_status": "manual" if status == "manual_check" else status,
        "summary": summary,
        "details": unique_keep_order(details or []),
        "manual_check_url": manual_url,
        "manual_url": manual_url,
    }
    if raw_data is not None:
        item["data"] = strip_internal(raw_data)
    if extra:
        item.update(extra)
    return item


def manual_item(title: str, summary: str = "Источник не вернул данные. Требуется ручная проверка.", details: Optional[List[str]] = None, manual_url: str = "") -> Dict[str, Any]:
    return status_item(title, "manual_check", summary, details or ["Источник не вернул данные. Требуется ручная проверка."], manual_url)


def response_state(resp: Any) -> str:
    return clean_text(resp.get("state") if isinstance(resp, dict) else "").lower()


def has_newdb_error(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return True
    txt = lower_blob(resp)
    if "errors_info" in resp:
        return True
    markers = [
        "method or country is not valid", "not valid", "required", "не хватает обязательного",
        "не заполнено значение", "unauthorized", "forbidden", "not enough balance",
        "service is unavailable", "parsing failed",
    ]
    if any(m in txt for m in markers):
        return True
    for block in (resp.get("results") or {}).values():
        result = block.get("result") if isinstance(block, dict) else None
        if isinstance(result, dict):
            st = result.get("status")
            if isinstance(st, int) and st >= 400:
                return True
            if clean_text(result.get("error")):
                return True
    return False


def seller_name(req: CheckRequest) -> str:
    return " ".join(x for x in [clean_text(req.last), clean_text(req.first), clean_text(req.middle)] if x).strip()


def seller_inn(req: CheckRequest) -> str:
    candidates = [req.inn, req.innfiz, req.inn_fiz, req.seller_inn]
    for name in ["innfiz", "inn_fiz", "sellerInn", "seller_inn", "inn"]:
        try:
            candidates.append(getattr(req, name, ""))
        except Exception:
            pass
    for c in candidates:
        d = only_digits(c)
        if d:
            return d
    return ""


def normalize_property(req: CheckRequest) -> Dict[str, str]:
    cadastral = clean_text(req.cadastral_number or req.cadastre_number)
    address = clean_text(req.address)
    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"
    if cadastral and re.fullmatch(cad_pattern, cadastral):
        return {"query": cadastral, "cadastral_number": cadastral, "address": cadastral, "type": "cadastral"}
    if address and re.fullmatch(cad_pattern, address):
        return {"query": address, "cadastral_number": address, "address": address, "type": "cadastral"}
    address = re.sub(r"\s+", " ", address)
    if address and not re.search(r"(?i)санкт[- ]петербург|ленинградская область|москва", address):
        address = "Санкт-Петербург, " + address
    return {"query": address, "cadastral_number": "", "address": address, "type": "address"}


def build_payloads(req: CheckRequest) -> Dict[str, Optional[Dict[str, Any]]]:
    last, first, middle = clean_text(req.last), clean_text(req.first), clean_text(req.middle)
    dob_iso = dob_to_iso(req.dob)
    inn = seller_inn(req)
    region = int(req.region or 78)
    ps, pn = only_digits(req.passport_series), only_digits(req.passport_number)
    prop = normalize_property(req)

    payloads: Dict[str, Optional[Dict[str, Any]]] = {}

    payloads["passport"] = None
    if len(ps) == 4 and len(pn) == 6:
        payloads["passport"] = {
            "method": NEWDB_METHOD_PASSPORT, "country": "ru",
            "series": ps, "seria": ps, "number": pn,
        }

    payloads["fssp"] = None
    if first and last and dob_iso:
        payloads["fssp"] = {
            "method": NEWDB_METHOD_FSSP, "country": "ru",
            "firstname": first, "lastname": last, "secondname": middle,
            "dob": dob_iso, "regioncode": region,
        }

    payloads["bankruptcy"] = None
    if len(inn) == 12:
        payloads["bankruptcy"] = {
            "method": NEWDB_METHOD_BANKRUPTCY, "country": "ru",
            "innfiz": inn, "firstname": first, "lastname": last,
            "secondname": middle, "dob": dob_iso,
        }

    payloads["courts"] = None
    if len(inn) == 12:
        payloads["courts"] = {
            "method": NEWDB_METHOD_COURTS, "country": "ru",
            "innfiz": inn, "firstname": first, "lastname": last,
            "secondname": middle, "dob": dob_iso,
        }

    payloads["egrn"] = None
    if prop["query"]:
        payloads["egrn"] = {"method": NEWDB_METHOD_EGRN, "country": "ru", "address": prop["query"]}

    return payloads


async def newdb_request(params: Optional[Dict[str, Any]], max_wait: int = 120, poll_interval: int = 5) -> Dict[str, Any]:
    if not params:
        return {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
    if not NEWDB_TOKEN:
        return {"state": "not_configured", "error": "NEWDB_TOKEN не задан в Environment."}
    headers = {"X-API-KEY": NEWDB_TOKEN, "Accept": "application/json", "Content-Type": "application/json"}
    request_id = str(uuid.uuid4())
    payload = {"params": params, "requestId": request_id}
    in_progress = {"queued", "queue", "in progress", "progress", "pending", "processing", "restart", "wait", "waiting"}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=True) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            try:
                data = first.json()
            except Exception:
                data = {"raw_text": first.text}
            if isinstance(data, dict):
                data["_http_status"] = first.status_code
            else:
                data = {"raw": data, "_http_status": first.status_code}
            if first.status_code >= 400 or isinstance(data.get("results"), dict):
                return data
            if response_state(data) not in in_progress:
                return data
            last_data = data
            elapsed = 0
            while elapsed < max_wait:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                r = await client.post(NEWDB_URL, headers=headers, json=payload)
                try:
                    polled = r.json()
                except Exception:
                    polled = {"raw_text": r.text}
                if isinstance(polled, dict):
                    polled["_http_status"] = r.status_code
                else:
                    polled = {"raw": polled, "_http_status": r.status_code}
                last_data = polled
                if r.status_code >= 400 or isinstance(polled.get("results"), dict):
                    return polled
                if response_state(polled) not in in_progress:
                    return polled
            if isinstance(last_data, dict):
                last_data["state"] = "timeout"
                last_data["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд."
            return last_data
        except Exception as e:
            return {"state": "error", "error": clean_text(str(e)), "trace": traceback.format_exc()}


async def run_newdb_checks(req: CheckRequest) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    payloads = build_payloads(req)
    tasks = {
        "passport": newdb_request(payloads.get("passport"), 70),
        "fssp": newdb_request(payloads.get("fssp"), 140),
        "bankruptcy": newdb_request(payloads.get("bankruptcy"), 140),
        "courts": newdb_request(payloads.get("courts"), 180),
        "egrn": newdb_request(payloads.get("egrn"), 320),
    }
    keys = list(tasks.keys())
    values = await asyncio.gather(*tasks.values())
    return payloads, dict(zip(keys, values))


def get_result_block(resp: Dict[str, Any], method: str) -> Optional[Dict[str, Any]]:
    if not isinstance(resp, dict):
        return None
    results = resp.get("results")
    if not isinstance(results, dict):
        return None
    if method in results and isinstance(results[method], dict):
        result = results[method].get("result")
        return result if isinstance(result, dict) else results[method]
    for block in results.values():
        if isinstance(block, dict) and isinstance(block.get("result"), dict):
            return block["result"]
    return None


def get_result_data(resp: Dict[str, Any], method: str) -> Any:
    block = get_result_block(resp, method)
    return block.get("data") if isinstance(block, dict) else None


def extract_items_from_data(data: Any) -> List[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ["items", "records", "rows", "list", "results", "data"]:
            if isinstance(data.get(key), list):
                return data[key]
        meaningful = {k: v for k, v in data.items() if k not in {"status", "error"}}
        if meaningful:
            return [meaningful]
    return []


def extract_money(item: Any) -> float:
    if not item:
        return 0.0
    txt = json_text(item)
    candidates: List[float] = []
    if isinstance(item, dict):
        for key in ["amount", "sum", "debt", "debt_sum", "debtSum", "total", "balance", "ip_sum", "rest", "rest_sum", "amountDue", "execSum"]:
            if key in item:
                try:
                    candidates.append(float(str(item[key]).replace(" ", "").replace(",", ".")))
                except Exception:
                    pass
    for m in re.findall(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:руб|₽)", txt, flags=re.I):
        try:
            candidates.append(float(m.replace(" ", "").replace(",", ".")))
        except Exception:
            pass
    return max(candidates) if candidates else 0.0


def classify_passport(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Паспорт МВД"
    if response_state(resp) == "skipped":
        return manual_item(title, "Паспорт не проверялся автоматически: не переданы серия и номер паспорта.", ["Для автоматической проверки МВД укажите серию и номер паспорта."], MANUAL_LINKS["passport"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник не вернул данные по паспорту. Требуется ручная проверка.", ["Источник не вернул понятный результат проверки паспорта."], MANUAL_LINKS["passport"])
    data = get_result_data(resp, NEWDB_METHOD_PASSPORT)
    txt = lower_blob(data or resp)
    if any(x in txt for x in ["недейств", "разыскивается", "invalid"]):
        return status_item(title, "risk", "Выявлены признаки проблемы с паспортом.", flatten_human_values(data or resp, 10), MANUAL_LINKS["passport"], data)
    if any(x in txt for x in ["действителен", "действительный", "valid"]):
        return status_item(title, "ok", "Паспорт по полученным данным действителен.", [], MANUAL_LINKS["passport"], data)
    return manual_item(title, "Источник вернул неоднозначный результат по паспорту. Требуется ручная проверка.", flatten_human_values(data or resp, 8), MANUAL_LINKS["passport"])


def classify_fssp(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "ФССП"
    if response_state(resp) == "skipped":
        return manual_item(title, "ФССП не проверялся автоматически: недостаточно данных.", ["Для ФССП нужны ФИО, дата рождения и регион."], MANUAL_LINKS["fssp"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник ФССП не вернул данные. Требуется ручная проверка.", ["Источник ФССП недоступен, не принял запрос или не смог обработать данные."], MANUAL_LINKS["fssp"])
    data = get_result_data(resp, NEWDB_METHOD_FSSP)
    items = extract_items_from_data(data)
    empty_warning = "ФССП искался по ФИО, дате рождения и региону. Если дата рождения или регион указаны неверно, найденные ИП могут не попасть в автоматический ответ."
    if isinstance(data, list) and len(data) == 0:
        stats = {"all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0, "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "unknown_sum": 0.0, "actual_debt": 0.0, "active_items": [], "closed_items": [], "unknown_items": []}
        return status_item(title, "ok", "По полученным данным исполнительные производства не найдены.", [empty_warning], MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})
    txt = lower_blob(data or resp)
    if not items and any(x in txt for x in ["не найден", "нет свед", "отсутств", "not found", "nothing found", "no data"]):
        stats = {"all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0, "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "unknown_sum": 0.0, "actual_debt": 0.0, "active_items": [], "closed_items": [], "unknown_items": []}
        return status_item(title, "ok", "По полученным данным исполнительные производства не найдены.", [empty_warning], MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})
    if not items:
        return manual_item(title, "Источник ФССП вернул неоднозначный результат. Требуется ручная проверка.", flatten_human_values(data or resp, 10), MANUAL_LINKS["fssp"])

    closed_markers = ["оконч", "прекращ", "закрыт", "заверш", "ст. 46", "статья 46", "terminated", "closed", "completed"]
    active_markers = ["возбужден", "актив", "исполнительное производство", "задолженность", "остаток", "взыскание", "active"]
    active, closed, unknown = [], [], []
    for item in items:
        t = lower_blob(item)
        if any(m in t for m in closed_markers):
            closed.append(item)
        elif any(m in t for m in active_markers):
            active.append(item)
        else:
            unknown.append(item)
    active_sum, closed_sum, unknown_sum = sum(extract_money(x) for x in active), sum(extract_money(x) for x in closed), sum(extract_money(x) for x in unknown)
    total = active_sum + closed_sum + unknown_sum
    stats = {"all_count": len(items), "active_count": len(active), "closed_count": len(closed), "unknown_count": len(unknown), "total_sum_all": total, "active_sum": active_sum, "closed_sum": closed_sum, "unknown_sum": unknown_sum, "actual_debt": active_sum, "active_items": strip_internal(active), "closed_items": strip_internal(closed), "unknown_items": strip_internal(unknown)}
    details = [f"Всего найдено ИП: {len(items)}", f"Активные ИП: {len(active)}", f"Закрытые/оконченные ИП: {len(closed)}", f"Неоднозначные записи: {len(unknown)}", f"Общая сумма всех найденных ИП: {rub(total)}", f"Сумма по активным ИП: {rub(active_sum)}", f"Сумма по закрытым ИП: {rub(closed_sum)}", f"Актуальный долг по активным ИП: {rub(active_sum)}"]
    if active:
        return status_item(title, "risk", f"Найдены активные исполнительные производства. Актуальная сумма долга: {rub(active_sum)}.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})
    if closed and not active and not unknown:
        return status_item(title, "ok", f"Найдены исполнительные производства. Активных: 0, закрытых: {len(closed)}. Актуальный долг по активным производствам: 0 ₽.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})
    if unknown:
        return status_item(title, "manual_check", "Найдены исполнительные производства с неоднозначным статусом. Требуется ручная проверка.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})
    return status_item(title, "ok", "По полученным данным исполнительные производства не найдены.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})


def classify_bankruptcy(resp: Dict[str, Any], inn: str = "") -> Dict[str, Any]:
    title = "Банкротство / Федресурс"
    if response_state(resp) == "skipped":
        return manual_item(title, "Банкротство не проверялось автоматически: не передан 12-значный ИНН физлица.", ["Укажите ИНН физлица из 12 цифр."], MANUAL_LINKS["bankruptcy"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник банкротства не вернул данные. Требуется ручная проверка.", ["Источник не принял запрос, недоступен или не смог обработать данные."], MANUAL_LINKS["bankruptcy"])
    data = get_result_data(resp, NEWDB_METHOD_BANKRUPTCY)
    items = extract_items_from_data(data)
    if isinstance(data, list) and len(data) == 0:
        return status_item(title, "ok", "По полученным данным сведения о банкротстве не выявлены.", [f"Проверка выполнена по ИНН физлица: {inn}" if inn else "Проверка выполнена по ИНН физлица."], MANUAL_LINKS["bankruptcy"], data)
    txt = lower_blob(data or resp)
    if not items and any(x in txt for x in ["не найден", "нет свед", "отсутств", "not found", "nothing found", "no data"]):
        return status_item(title, "ok", "По полученным данным сведения о банкротстве не выявлены.", [], MANUAL_LINKS["bankruptcy"], data)
    if items:
        return status_item(title, "risk", "Выявлены сведения, связанные с банкротством.", flatten_human_values(items, 14), MANUAL_LINKS["bankruptcy"], items)
    return manual_item(title, "Источник банкротства вернул неоднозначный результат. Требуется ручная проверка.", flatten_human_values(data or resp, 10), MANUAL_LINKS["bankruptcy"])


def classify_courts(resp: Dict[str, Any], inn: str = "") -> Dict[str, Any]:
    title = "Суды / арбитраж"
    if response_state(resp) == "skipped":
        return manual_item(title, "Суды не проверялись автоматически: не передан 12-значный ИНН физлица.", ["Укажите ИНН физлица из 12 цифр."], MANUAL_LINKS["courts"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник судебных дел не вернул данные. Требуется ручная проверка.", ["Источник не принял запрос, недоступен или не смог обработать данные."], MANUAL_LINKS["courts"])
    data = get_result_data(resp, NEWDB_METHOD_COURTS)
    items = extract_items_from_data(data)
    if isinstance(data, list) and len(data) == 0:
        return status_item(title, "ok", "По полученным данным судебные дела не выявлены.", [f"Проверка выполнена по ИНН физлица: {inn}" if inn else "Проверка выполнена по ИНН физлица."], MANUAL_LINKS["courts"], data)
    txt = lower_blob(data or resp)
    if not items and any(x in txt for x in ["не найден", "нет свед", "отсутств", "not found", "nothing found", "no data"]):
        return status_item(title, "ok", "По полученным данным судебные дела не выявлены.", [], MANUAL_LINKS["courts"], data)
    if items:
        return status_item(title, "risk", "Найдены судебные производства. Требуется анализ предмета спора.", flatten_human_values(items, 14), MANUAL_LINKS["courts"], items)
    return manual_item(title, "Источник судебных дел вернул неоднозначный результат. Требуется ручная проверка.", flatten_human_values(data or resp, 10), MANUAL_LINKS["courts"])


def readable_address(obj: Dict[str, Any]) -> str:
    address = obj.get("address")
    if isinstance(address, dict):
        return clean_text(address.get("readableAddress") or address.get("address"))
    return clean_text(address)


def classify_egrn(resp: Dict[str, Any], prop: Dict[str, str]) -> Dict[str, Any]:
    title = "ЕГРН / Росреестр"
    if not prop.get("query"):
        return manual_item(title, "Не передан адрес или кадастровый номер объекта. Проверка ЕГРН не выполнена.", ["Укажите кадастровый номер или адрес объекта."], MANUAL_LINKS["egrn"])
    if response_state(resp) == "skipped":
        return manual_item(title, "ЕГРН не проверялся автоматически: не передан объект.", ["Укажите кадастровый номер или адрес объекта."], MANUAL_LINKS["egrn"])
    if has_newdb_error(resp):
        return manual_item(title, "Источник ЕГРН / Росреестра не вернул данные. Требуется ручная проверка.", ["Росреестр недоступен, не принял запрос или не смог обработать данные."], MANUAL_LINKS["egrn"])
    data = get_result_data(resp, NEWDB_METHOD_EGRN)
    if not isinstance(data, list) or not data:
        return manual_item(title, "Источник ЕГРН / Росреестра не вернул данные объекта. Требуется ручная проверка.", [f"Запрос: {prop.get('query')}"], MANUAL_LINKS["egrn"])
    obj = data[0] if isinstance(data[0], dict) else {}
    if not obj:
        return manual_item(title, "ЕГРН вернул пустой или непонятный объект. Требуется ручная проверка.", [f"Запрос: {prop.get('query')}"], MANUAL_LINKS["egrn"])
    encumbrances = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    details: List[str] = []
    if obj.get("cadNumber"):
        details.append(f"Кадастровый номер: {obj.get('cadNumber')}")
    addr = readable_address(obj)
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
    details.append(f"Записей о правах: {len(rights)}")
    raw_text = lower_blob(obj)
    risk_words = ["запрещение регистрации", "запрет", "ограничение", "обременение", "арест", "ипотека", "залог", "рента"]
    if encumbrances or any(w in raw_text for w in risk_words):
        for enc in encumbrances:
            if not isinstance(enc, dict):
                continue
            desc = clean_text(enc.get("typeDesc")) or f"Тип ограничения: {clean_text(enc.get('type'))}"
            num = clean_text(enc.get("encumbranceNumber"))
            start = clean_text(enc.get("startDate"))
            parts = [desc]
            if num:
                parts.append(f"№ {num}")
            if start:
                parts.append(f"дата начала: {start}")
            details.append(", ".join(parts))
        return status_item(title, "risk", "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН.", details, MANUAL_LINKS["egrn"], obj, {"egrn_object": strip_internal(obj)})
    details.append("По полученным данным явные признаки ограничений или обременений не выявлены.")
    return status_item(title, "ok", "Данные по объекту получены. По полученным данным явные признаки ограничений или обременений не выявлены.", details, MANUAL_LINKS["egrn"], obj, {"egrn_object": strip_internal(obj)})


def build_checklist(req: CheckRequest, responses: Dict[str, Any]) -> List[Dict[str, Any]]:
    inn = seller_inn(req)
    prop = normalize_property(req)
    return [
        classify_passport(responses.get("passport") or {}),
        classify_fssp(responses.get("fssp") or {}),
        classify_bankruptcy(responses.get("bankruptcy") or {}, inn),
        classify_courts(responses.get("courts") or {}, inn),
        classify_egrn(responses.get("egrn") or {}, prop),
    ]


def registry_data_from_checklist(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    mapping = {"Паспорт МВД": "passport", "ФССП": "fssp", "Банкротство / Федресурс": "bankruptcy", "Суды / арбитраж": "courts", "ЕГРН / Росреестр": "egrn"}
    out: Dict[str, Any] = {}
    for item in checklist:
        key = mapping.get(item.get("title"), item.get("title", "unknown"))
        out[key] = {"title": item.get("title"), "status": item.get("status"), "ui_status": item.get("ui_status"), "summary": item.get("summary"), "details": item.get("details", []), "manual_check_url": item.get("manual_check_url", ""), "data": item.get("data")}
        if item.get("fssp_stats"):
            out[key]["fssp_stats"] = item.get("fssp_stats")
        if item.get("egrn_object"):
            out[key]["egrn_object"] = item.get("egrn_object")
    return out


def build_risk_scoring(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    score = 0
    factors: List[str] = []
    manual_count = 0
    for item in checklist:
        title = item.get("title", "")
        status = item.get("status")
        details = " ".join(item.get("details", []))
        if status == "manual_check":
            manual_count += 1
            score += 8
            factors.append(f"{title}: источник требует ручной проверки (+8)")
        if title == "ЕГРН / Росреестр" and status == "risk":
            add = 45
            if "Запрещение регистрации" in details or "запрет" in details.lower():
                add = 60
            score += add
            factors.append(f"ЕГРН: выявлены ограничения/обременения (+{add})")
        elif title == "ФССП" and status == "risk":
            stats = item.get("fssp_stats") or {}
            add = 25
            if (stats.get("active_sum") or 0) >= 300000:
                add = 35
            score += add
            factors.append(f"ФССП: активные исполнительные производства (+{add})")
        elif title == "Банкротство / Федресурс" and status == "risk":
            score += 55
            factors.append("Банкротство: выявлены сведения, связанные с банкротством (+55)")
        elif title == "Суды / арбитраж" and status == "risk":
            score += 25
            factors.append("Суды: найдены судебные производства (+25)")
        elif title == "Паспорт МВД" and status == "risk":
            score += 60
            factors.append("Паспорт: выявлена проблема с паспортом (+60)")
    score = min(100, int(score))
    if score >= 70:
        level = "опасная"
        conclusion = "Сделку нельзя выводить на аванс без ручного юридического разбора и устранения выявленных факторов."
    elif score >= 35:
        level = "условно рискованная"
        conclusion = "Сделка возможна только после ручной проверки, закрытия спорных вопросов и безопасной схемы расчетов."
    elif manual_count:
        level = "условно безопасная, но неполная проверка"
        conclusion = "Явных автоматических рисков немного, но часть источников не подтверждена — перед авансом нужна ручная проверка."
    else:
        level = "условно безопасная"
        conclusion = "По автоматическим источникам критичных рисков не выявлено, но отчет не заменяет ручную юридическую проверку."
    return {"score": score, "max_score": 100, "level": level, "conclusion": conclusion, "factors": factors}


def build_recommendations(checklist: List[Dict[str, Any]], scoring: Dict[str, Any]) -> List[Dict[str, str]]:
    recs: List[Dict[str, str]] = []
    by_title = {x.get("title"): x for x in checklist}
    egrn = by_title.get("ЕГРН / Росреестр")
    fssp = by_title.get("ФССП")
    bankruptcy = by_title.get("Банкротство / Федресурс")
    courts = by_title.get("Суды / арбитраж")
    passport = by_title.get("Паспорт МВД")
    if egrn and egrn.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки", "text": "По ЕГРН выявлены ограничения/обременения. До аванса нужно получить документальное подтверждение основания ограничения и прописать обязанность продавца снять его в конкретный срок."})
        recs.append({"priority": "critical", "title": "Не передавать деньги напрямую продавцу", "text": "При запрете регистрации использовать нотариальный депозит, аккредитив или иную условную схему, где раскрытие денег происходит только после снятия ограничения и регистрации перехода права."})
    if fssp and fssp.get("status") == "risk":
        recs.append({"priority": "high", "title": "Закрыть активные ИП до сделки или через контролируемые расчеты", "text": "Если есть активные исполнительные производства, актуальный долг должен быть погашен с подтверждением от ФССП. В ПДКП прописать порядок погашения и последствия неснятия ограничений."})
    if fssp and fssp.get("status") == "ok":
        recs.append({"priority": "medium", "title": "Сверить ФССП вручную перед авансом", "text": "Автоматический ответ ФССП пустой, но поиск чувствителен к дате рождения и региону. Перед авансом нужно повторить ручную проверку по сайту ФССП."})
    if bankruptcy and bankruptcy.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Не выходить на сделку без анализа банкротного риска", "text": "Сведения о банкротстве требуют отдельного анализа периодов, статуса процедуры и риска оспаривания сделки."})
    if courts and courts.get("status") == "risk":
        recs.append({"priority": "high", "title": "Разобрать судебные дела по предмету спора", "text": "Наличие дел само по себе не всегда блокирует сделку, но нужно понять предмет спора, сумму требований и связь с недвижимостью/долгами."})
    if passport and passport.get("status") == "manual_check":
        recs.append({"priority": "medium", "title": "Проверить паспорт вручную", "text": "До аванса проверить действительность паспорта МВД и сверить данные с правоустанавливающими документами."})
    if scoring.get("score", 0) >= 35:
        recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса/задатка и ответственность при неподтверждении чистоты сделки."})
    return recs


def fallback_legal_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recommendations: List[Dict[str, str]]) -> str:
    risks = [x for x in checklist if x["status"] == "risk"]
    manuals = [x for x in checklist if x["status"] == "manual_check"]
    oks = [x for x in checklist if x["status"] == "ok"]
    seller = seller_name(req) or "по предоставленным данным не указан"
    prop = normalize_property(req)
    obj = prop.get("query") or "по предоставленным данным не указан"
    lines = [
        "1. Краткий вывод",
        f"Предварительная оценка: {scoring['level']}. Риск-скоринг: {scoring['score']} из 100.",
        scoring["conclusion"],
        "",
        "2. Что проверено",
        f"Продавец: {seller}.",
        f"Дата рождения: {normalize_dob(req.dob) or 'по предоставленным данным не указана'}.",
        f"ИНН физлица: {'передан' if seller_inn(req) else 'по предоставленным данным не передан'}.",
        f"Объект: {obj}.",
        f"Проверок без явных рисков: {len(oks)}. Проверок с рисками: {len(risks)}. Требуют ручной проверки: {len(manuals)}.",
        "",
        "3. Риски по продавцу",
    ]
    for item in [x for x in checklist if x["title"] != "ЕГРН / Росреестр"]:
        prefix = "Риск" if item["status"] == "risk" else "Ручная проверка" if item["status"] == "manual_check" else "Без явных рисков"
        lines.append(f"- {item['title']}: {prefix}. {item['summary']}")
    lines.extend(["", "4. Риски по объекту"])
    for item in [x for x in checklist if x["title"] == "ЕГРН / Росреестр"]:
        prefix = "Риск" if item["status"] == "risk" else "Ручная проверка" if item["status"] == "manual_check" else "Без явных рисков"
        lines.append(f"- {item['title']}: {prefix}. {item['summary']}")
        for d in item.get("details", [])[:10]:
            lines.append(f"  • {d}")
    lines.extend(["", "5. Что говорит в пользу сделки"])
    if oks:
        for item in oks:
            lines.append(f"- {item['title']}: {item['summary']}")
    else:
        lines.append("Пока нет достаточного набора автоматических подтверждений, которые можно уверенно зачесть в пользу сделки.")
    lines.extend(["", "6. Что обязательно проверить до аванса"])
    for item in manuals + risks:
        lines.append(f"- {item['title']}: {item['summary']} {item.get('manual_check_url') or ''}")
    lines.extend(["", "7. Что прописать в авансовом соглашении / ПДКП"])
    if recommendations:
        for r in recommendations:
            lines.append(f"- {r['title']}: {r['text']}")
    else:
        lines.append("Сверить паспорт, правоустанавливающие документы, свежую ЕГРН и условия расчетов.")
    lines.extend(["", "8. Безопасная схема расчетов"])
    if any(x.get("title") == "ЕГРН / Росреестр" and x.get("status") == "risk" for x in checklist):
        lines.append("При ограничениях по ЕГРН деньги нельзя передавать напрямую. Использовать нотариальный депозит, аккредитив или иную схему с раскрытием после снятия ограничения и регистрации перехода права.")
    else:
        lines.append("Использовать контролируемую схему расчетов: аккредитив, депозит нотариуса или безопасную банковскую ячейку с условиями раскрытия.")
    lines.extend(["", "9. Итоговое заключение", "Отчет показывает предварительную картину по автоматическим источникам. Он не обещает 100% безопасность и не заменяет ручную юридическую проверку документов специалистом."])
    return "\n".join(lines)


def build_gigachat_payload(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], scoring: Dict[str, Any], recommendations: List[Dict[str, str]], warnings: List[str]) -> Dict[str, Any]:
    return {
        "seller": {"full_name": seller_name(req), "dob": normalize_dob(req.dob), "inn_provided": bool(seller_inn(req)), "passport_provided": bool(only_digits(req.passport_series) and only_digits(req.passport_number))},
        "property": normalize_property(req),
        "risk_scoring": scoring,
        "recommendations": recommendations,
        "checklist": checklist,
        "registry_data": registry_data,
        "warnings": warnings,
        "not_checked": [x["title"] for x in checklist if x["status"] == "manual_check"],
    }


async def gigachat_report(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], scoring: Dict[str, Any], recommendations: List[Dict[str, str]], warnings: List[str]) -> str:
    if not (GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)):
        return fallback_legal_report(req, checklist, scoring, recommendations)
    try:
        credentials = GIGACHAT_CREDENTIALS
        if not credentials:
            credentials = base64.b64encode(f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode()).decode()
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=GIGACHAT_VERIFY_SSL_CERTS) as client:
            auth_resp = await client.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={"Authorization": f"Basic {credentials}", "RqUID": str(uuid.uuid4()), "Content-Type": "application/x-www-form-urlencoded"},
                data={"scope": GIGACHAT_SCOPE},
            )
            auth_resp.raise_for_status()
            token = auth_resp.json().get("access_token")
            if not token:
                return fallback_legal_report(req, checklist, scoring, recommendations)
            structured = build_gigachat_payload(req, checklist, registry_data, scoring, recommendations, warnings)
            prompt = (
                "Ты юрист-эксперт по недвижимости в Санкт-Петербурге.\n\n"
                "На основе структурированных данных сформируй продающий, но честный юридический отчет для покупателя недвижимости.\n"
                "Строго соблюдай структуру: 1. Краткий вывод 2. Что проверено 3. Риски по продавцу 4. Риски по объекту 5. Что говорит в пользу сделки 6. Что обязательно проверить до аванса 7. Что прописать в авансовом соглашении / ПДКП 8. Безопасная схема расчетов 9. Итоговое заключение.\n"
                "Обязательно используй риск-скоринг, рекомендации и не называй объект юридически чистым. Не придумывай факты. Если источник не ответил — пиши 'требуется ручная проверка'.\n\n"
                f"ДАННЫЕ:\n{json.dumps(structured, ensure_ascii=False, indent=2)}"
            )
            chat_resp = await client.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"model": GIGACHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 3500},
            )
            chat_resp.raise_for_status()
            content = deep_get(chat_resp.json(), ["choices", 0, "message", "content"], "")
            return clean_text(content) if content else fallback_legal_report(req, checklist, scoring, recommendations)
    except Exception:
        warnings.append("GigaChat временно недоступен. Отчет сформирован резервным юридическим шаблоном.")
        return fallback_legal_report(req, checklist, scoring, recommendations)


def pdf_escape(text: Any) -> str:
    return html.escape(clean_text(text)).replace("\n", "<br/>")


def setup_pdf_font() -> str:
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]:
        if Path(p).exists():
            try:
                pdfmetrics.registerFont(TTFont("AppFont", p))
                return "AppFont"
            except Exception:
                pass
    return "Helvetica"


def build_pdf(report_id: str, result: Dict[str, Any]) -> Optional[Path]:
    if SimpleDocTemplate is None:
        return None
    pdf_path = REPORT_DIR / f"{report_id}.pdf"
    font = setup_pdf_font()
    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="AppTitle", parent=styles["Title"], fontName=font, fontSize=17, leading=22, spaceAfter=10))
    styles.add(ParagraphStyle(name="AppH2", parent=styles["Heading2"], fontName=font, fontSize=12, leading=16, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="AppBody", parent=styles["BodyText"], fontName=font, fontSize=9, leading=13, spaceAfter=4))
    styles.add(ParagraphStyle(name="AppSmall", parent=styles["BodyText"], fontName=font, fontSize=8, leading=11, textColor="#555555", spaceAfter=3))
    story = [Paragraph("Юридический отчет по проверке продавца и объекта недвижимости", styles["AppTitle"]), Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles["AppSmall"])]
    seller, prop = result.get("seller", {}), result.get("property", {})
    story.append(Paragraph("1. Риск-скоринг", styles["AppH2"]))
    sc = result.get("risk_scoring", {})
    story.append(Paragraph(f"Оценка: <b>{pdf_escape(sc.get('score'))}/100</b>. Уровень: <b>{pdf_escape(sc.get('level'))}</b>.", styles["AppBody"]))
    story.append(Paragraph(pdf_escape(sc.get("conclusion", "")), styles["AppBody"]))
    story.append(Paragraph("2. Данные продавца", styles["AppH2"]))
    story.append(Paragraph(f"ФИО: {pdf_escape(seller.get('full_name'))}", styles["AppBody"]))
    story.append(Paragraph(f"Дата рождения: {pdf_escape(seller.get('dob'))}", styles["AppBody"]))
    story.append(Paragraph(f"ИНН передан: {'да' if seller.get('inn_provided') else 'нет'}", styles["AppBody"]))
    story.append(Paragraph("3. Данные объекта", styles["AppH2"]))
    story.append(Paragraph(f"Запрос: {pdf_escape(prop.get('query'))}", styles["AppBody"]))
    story.append(Paragraph("4. Чек-лист проверок", styles["AppH2"]))
    for item in result.get("checklist", []):
        label = "Проверено" if item.get("status") == "ok" else "Риск" if item.get("status") == "risk" else "Требуется ручная проверка"
        story.append(Paragraph(f"<b>{pdf_escape(item.get('title'))}:</b> {label}. {pdf_escape(item.get('summary'))}", styles["AppBody"]))
        for d in item.get("details", [])[:12]:
            story.append(Paragraph(f"• {pdf_escape(d)}", styles["AppSmall"]))
    story.append(Paragraph("5. Рекомендации", styles["AppH2"]))
    for r in result.get("recommendations", []):
        story.append(Paragraph(f"<b>{pdf_escape(r.get('title'))}:</b> {pdf_escape(r.get('text'))}", styles["AppBody"]))
    story.append(Paragraph("6. Юридический отчет", styles["AppH2"]))
    for para in (result.get("legal_report") or "").split("\n"):
        story.append(Paragraph(pdf_escape(para), styles["AppBody"]) if para.strip() else Spacer(1, 3))
    story.append(Paragraph("Дисклеймер", styles["AppH2"]))
    story.append(Paragraph(pdf_escape(DISCLAIMER), styles["AppSmall"]))
    doc.build(story)
    return pdf_path


def make_safe_result(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], legal_report: str, warnings: List[str], report_id: str, scoring: Dict[str, Any], recommendations: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "success": True,
        "report_id": report_id,
        "seller": {"full_name": seller_name(req), "dob": normalize_dob(req.dob), "inn_provided": bool(seller_inn(req))},
        "property": normalize_property(req),
        "risk_scoring": scoring,
        "recommendations": recommendations,
        "checklist": checklist,
        "registry_data": registry_data,
        "legal_report": legal_report,
        "pdf_available": False,
        "pdf_url": f"/download-pdf/{report_id}",
        "pdf_base64": None,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


@app.get("/health")
async def health():
    return {"ok": True, "service": "real-estate-check-api", "version": "6.0-commercial-scoring", "newdb_configured": bool(NEWDB_TOKEN), "gigachat_configured": bool(GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)), "methods": {"passport": NEWDB_METHOD_PASSPORT, "fssp": NEWDB_METHOD_FSSP, "bankruptcy": NEWDB_METHOD_BANKRUPTCY, "courts": NEWDB_METHOD_COURTS, "egrn": NEWDB_METHOD_EGRN}}


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    payloads, responses = await run_newdb_checks(req)
    checklist = build_checklist(req, responses)
    scoring = build_risk_scoring(checklist)
    recommendations = build_recommendations(checklist, scoring)
    return {"success": True, "normalized_input": {"seller_inn": seller_inn(req), "dob_iso": dob_to_iso(req.dob), "property": normalize_property(req)}, "payloads": payloads, "responses": responses, "classified_checklist": checklist, "risk_scoring": scoring, "recommendations": recommendations, "notes": ["Залоги движимого имущества отключены и не участвуют в отчете.", "Если ФССП вернул пустой список, но вручную есть ИП — проверьте точность даты рождения и регион поиска.", "Банкротство и суды запускаются только при 12-значном ИНН физлица."]}


@app.post("/check-report")
async def check_report(req: CheckRequest):
    warnings: List[str] = []
    report_id = str(uuid.uuid4())
    try:
        payloads, responses = await run_newdb_checks(req)
        checklist = build_checklist(req, responses)
        registry_data = registry_data_from_checklist(checklist)
        scoring = build_risk_scoring(checklist)
        recommendations = build_recommendations(checklist, scoring)
        legal_report = await gigachat_report(req, checklist, registry_data, scoring, recommendations, warnings)
        result = make_safe_result(req, checklist, registry_data, legal_report, warnings, report_id, scoring, recommendations)
        try:
            pdf_path = build_pdf(report_id, result)
            if pdf_path and pdf_path.exists():
                result["pdf_available"] = True
                try:
                    result["pdf_base64"] = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
                except Exception:
                    result["pdf_base64"] = None
            else:
                warnings.append("PDF временно недоступен, но отчет сформирован.")
        except Exception:
            warnings.append("PDF временно недоступен, но отчет сформирован.")
        try:
            (REPORT_DIR / f"{report_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return result
    except Exception:
        return {"success": False, "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.", "warnings": ["Техническая ошибка скрыта от пользователя и не влияет на юридический вывод."]}


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    safe_id = re.sub(r"[^a-fA-F0-9-]", "", report_id)
    pdf_path = REPORT_DIR / f"{safe_id}.pdf"
    if not pdf_path.exists():
        return {"success": False, "message": "PDF не найден или срок хранения отчета истек."}
    return FileResponse(str(pdf_path), media_type="application/pdf", filename=f"otchet_{safe_id}.pdf")
