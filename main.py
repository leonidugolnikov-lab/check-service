from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
except Exception:  # pragma: no cover
    SimpleDocTemplate = None


app = FastAPI(title="Real Estate Seller & Property Check API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -----------------------------
# ENV
# -----------------------------
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip().rstrip("/")

GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
GIGACHAT_VERIFY_SSL_CERTS = os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "true").lower() not in {"0", "false", "no"}

# Имена методов NewDB могут отличаться по тарифу/кабинету.
# Если поддержка NewDB даст точное имя метода — задай его в Environment без изменения кода.
NEWDB_METHOD_PASSPORT = os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd").strip()
NEWDB_METHOD_FSSP = os.getenv("NEWDB_METHOD_FSSP", "fssp").strip()
NEWDB_METHOD_BANKRUPTCY = os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankruptcy").strip()
NEWDB_METHOD_PLEDGES = os.getenv("NEWDB_METHOD_PLEDGES", "pledges").strip()
NEWDB_METHOD_COURTS = os.getenv("NEWDB_METHOD_COURTS", "courts").strip()
NEWDB_METHOD_EGRN = os.getenv("NEWDB_METHOD_EGRN", "rosreestr").strip()
NEWDB_ENABLE_METHOD_FALLBACKS = os.getenv("NEWDB_ENABLE_METHOD_FALLBACKS", "true").lower() not in {"0", "false", "no"}

REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports"))
REPORT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_TTL_SECONDS = int(os.getenv("REPORT_TTL_SECONDS", str(60 * 60 * 24)))

HTTP_TIMEOUT = httpx.Timeout(connect=25.0, read=300.0, write=60.0, pool=30.0)

IN_PROGRESS_STATES = {"queued", "queue", "in progress", "progress", "pending", "processing", "wait", "waiting", "created", "new"}
GOOD_STATES = {"complete", "completed", "done", "success", "finished", "ready", "ok"}
BAD_STATES = {"failed", "fail", "error", "rejected", "denied", "timeout", "not_configured", "manual", "canceled", "cancelled", "skipped"}

MANUAL_URLS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "pledges": "https://www.reestr-zalogov.ru/search",
    "courts": "https://kad.arbitr.ru",
    "egrn": "https://rosreestr.gov.ru",
}

DISCLAIMER = (
    "Отчет носит информационно-аналитический характер, не является гарантией полной юридической "
    "безопасности сделки и не заменяет ручную юридическую проверку документов специалистом."
)


# -----------------------------
# MODELS
# -----------------------------
class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 78
    passport_series: str = ""
    passport_number: str = ""
    property_type: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    address: str = ""


class ChecklistItem(BaseModel):
    source: str
    title: str
    status: str = Field(pattern="^(ok|risk|manual_check)$")
    summary: str
    details: List[str] = []
    manual_url: str = ""
    manual_check_url: str = ""


# -----------------------------
# BASIC HELPERS
# -----------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    # Часто встречается mojibake вида Р”РµР№...
    try:
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed:
                text = fixed
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


def only_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


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
        amount = float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        amount = 0.0
    return f"{amount:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


def safe_json(value: Any, limit: int = 12000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = str(value)
    return text[:limit]


def text_blob(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()


def state_of(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return clean_text(data.get("state") or data.get("status") or data.get("stage") or "").lower()


def strip_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            key = str(k).lower()
            if key in {
                "balance", "token", "api_key", "x-api-key", "authorization", "access_token",
                "client_secret", "secret", "password", "bearer", "headers",
                "requestid", "request_id", "newdb_qid", "qid", "task_id", "docs_url",
                "api docs", "errors_info", "sent_params", "http_status",
            }:
                continue
            cleaned[k] = strip_sensitive(v)
        return cleaned
    if isinstance(obj, list):
        return [strip_sensitive(x) for x in obj]
    return obj


def public_error(message: str = "Источник не вернул данные. Требуется ручная проверка.") -> str:
    return message


def is_in_progress(data: Any) -> bool:
    return isinstance(data, dict) and state_of(data) in IN_PROGRESS_STATES


def is_bad_response(data: Any) -> bool:
    if not isinstance(data, dict):
        return True
    st = state_of(data)
    txt = text_blob(data)
    try:
        if int(data.get("http_status", 0)) >= 400:
            return True
    except Exception:
        pass
    if st in BAD_STATES:
        return True
    bad_markers = [
        "не заполнено значение обязательного параметра", "не передан", "missing required",
        "required parameter", "bad request", "unauthorized", "forbidden", "доступ запрещ",
        "not enough balance", "insufficient balance", "проверьте баланс", "x-api-key",
        "токен доступа", "access@newdb.net", '"code":400', '"status":400', "traceback",
        "method or country is not valid", "error_code", "errors_info", "not valid",
    ]
    return any(x in txt for x in bad_markers)


def flatten_strings(obj: Any, limit: int = 80) -> List[str]:
    out: List[str] = []
    skip_keys = {
        "requestid", "request_id", "newdb_qid", "qid", "balance", "token", "method", "country",
        "datecreated", "state", "status", "sent_params", "http_status", "params",
    }
    skip_values = {"ru", "queued", "pending", "processing", "in progress", "done", "ok", "success"}

    def walk(x: Any) -> None:
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
            if re.fullmatch(r"[a-f0-9-]{20,}", s.lower()):
                return
            if len(s) <= 350 and s not in out:
                out.append(s)

    walk(obj)
    return out


def extract_error_details(data: Any) -> List[str]:
    # Наружу не отдаем requestId, docs_url, error_code, HTTP 400 и прочую кухню API.
    if not isinstance(data, dict):
        return [public_error()]
    txt = text_blob(data)
    if "method or country is not valid" in txt:
        return ["Источник не принял параметры запроса. Требуется ручная проверка и уточнение метода в newDB."]
    if "inn is not valid" in txt or "innyur" in txt:
        return ["Источник требует другой тип ИНН для этой проверки. Требуется ручная проверка."]
    if "required" in txt or "обязатель" in txt or "missing" in txt:
        return ["Источник не принял запрос: не хватает обязательного параметра. Требуется ручная проверка."]
    if "not enough balance" in txt or "insufficient balance" in txt or "проверьте баланс" in txt:
        return ["Источник не выполнил проверку из-за настроек доступа или баланса. Требуется ручная проверка."]
    details: List[str] = []
    for key in ["error", "message", "detail", "description"]:
        val = clean_text(data.get(key))
        low = val.lower()
        if val and len(val) < 180 and not any(x in low for x in ["traceback", "requestid", "newdb", "docs_url"]):
            details.append(val)
    return list(dict.fromkeys(details))[:3] or [public_error()]


def extract_items(data: Any) -> List[Any]:
    if not isinstance(data, dict) or is_bad_response(data) or is_in_progress(data):
        return []

    keys = [
        "items", "data", "result", "results", "records", "list", "rows", "objects", "response",
        "executions", "proceedings", "cases", "documents", "content", "payload",
    ]
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested
            meaningful = {
                k: v for k, v in value.items()
                if str(k).lower() not in {"state", "status", "requestid", "request_id", "balance", "datecreated", "params"}
            }
            if meaningful and flatten_strings(meaningful, 3):
                return [meaningful]
    return []


def has_negative_empty_marker(data: Any) -> bool:
    txt = text_blob(data)
    markers = ["не найден", "не найдено", "нет свед", "ничего не найдено", "nothing found", "not found", "no data", "empty", "отсутств"]
    return any(x in txt for x in markers)


def has_meaningful_data(data: Any) -> bool:
    if not isinstance(data, dict) or is_bad_response(data) or is_in_progress(data):
        return False
    if extract_items(data):
        return True
    if has_negative_empty_marker(data):
        return False
    useful = {
        k: v for k, v in data.items()
        if str(k).lower() not in {"state", "status", "requestid", "request_id", "balance", "datecreated", "params", "sent_params", "http_status"}
    }
    return bool(flatten_strings(useful, 10))


def make_item(source: str, status: str, summary: str, details: Optional[List[str]] = None, manual_url: str = "") -> Dict[str, Any]:
    # Даем обе пары ключей: source/title и manual_url/manual_check_url — чтобы фронт не ломался.
    return {
        "source": source,
        "title": source,
        "status": status,
        "summary": summary,
        "details": details or [],
        "manual_url": manual_url,
        "manual_check_url": manual_url,
    }


def manual_item(source: str, details: Optional[List[str]] = None, manual_url: str = "") -> Dict[str, Any]:
    return make_item(source, "manual_check", public_error(), details or [public_error()], manual_url)


# -----------------------------
# PROPERTY NORMALIZATION
# -----------------------------
def normalize_property(req: CheckRequest) -> Dict[str, str]:
    cadastral = clean_text(req.cadastral_number or req.cadastre_number)
    address = clean_text(req.address)
    cad_pattern = r"^\d{1,2}:\d{1,2}:\d{5,10}:\d+$"

    if cadastral and re.fullmatch(cad_pattern, cadastral):
        return {"type": "cadastral", "query": cadastral, "cadastral_number": cadastral, "address": cadastral}
    if address and re.fullmatch(cad_pattern, address):
        return {"type": "cadastral", "query": address, "cadastral_number": address, "address": address}

    # Адрес чуть нормализуем, но кадастровый номер не трогаем.
    address = re.sub(r"\s+", " ", address).strip()
    address = re.sub(r"(?i)\bспб\b|санкт[- ]петербург", "Санкт-Петербург", address)
    address = re.sub(r"(?i)\bгород\s+", "г. ", address)
    address = re.sub(r"(?i)\bулица\s+", "ул. ", address)
    address = re.sub(r"(?i)\bпроспект\s+", "пр. ", address)
    address = re.sub(r"(?i)\bдом\s+", "д. ", address)
    address = re.sub(r"(?i)\bквартира\s+", "кв. ", address)
    if address and not re.search(r"(?i)санкт-петербург|ленинградская область", address):
        address = "Санкт-Петербург, " + address
    return {"type": "address", "query": address, "cadastral_number": "", "address": address}


# -----------------------------
# NEWDB CLIENT
# -----------------------------
async def newdb_post(params: Dict[str, Any], max_wait: int = 90, poll_interval: int = 5) -> Dict[str, Any]:
    """
    Отправляет задачу в NewDB и дожидается финального результата.

    requestId/newdb_qid нельзя чистить до polling. Наружу они не попадут:
    чистка выполняется только при формировании публичного ответа/PDF.
    """
    if not NEWDB_TOKEN:
        return {
            "state": "not_configured",
            "error": "Источник не настроен. Требуется ручная проверка.",
            "sent_params": strip_sensitive(params),
        }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
        "Authorization": f"Bearer {NEWDB_TOKEN}",
    }
    payload = {"params": params}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            data = _safe_response_json(first)
            data["http_status"] = first.status_code
            data["sent_params"] = strip_sensitive(params)
        except Exception as e:
            return {
                "state": "error",
                "error": f"Ошибка связи с источником: {e}",
                "sent_params": strip_sensitive(params),
            }

        if first.status_code >= 400 or is_bad_response(data):
            data["state"] = data.get("state") or "error"
            return data

        st = state_of(data)
        if st and st not in IN_PROGRESS_STATES:
            return data
        if not st and has_meaningful_data(data):
            return data

        request_id = find_request_id(data)
        if not request_id:
            data["state"] = "manual"
            data["error"] = "Источник принял задачу, но не вернул идентификатор результата. Требуется ручная проверка."
            return data

        elapsed = 0
        last_data = data
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            poll_attempts = [
                ("GET", f"{NEWDB_URL}/{request_id}", None, None),
                ("GET", f"{NEWDB_URL}/result/{request_id}", None, None),
                ("GET", NEWDB_URL, {"requestId": request_id}, None),
                ("GET", NEWDB_URL, {"newdb_qid": request_id}, None),
                ("POST", NEWDB_URL, None, {"params": {"newdb_qid": request_id}}),
                ("POST", NEWDB_URL, None, {"params": {"requestId": request_id}}),
                ("POST", NEWDB_URL, None, {"params": {**params, "newdb_qid": request_id}}),
                ("POST", NEWDB_URL, None, {"params": {**params, "requestId": request_id}}),
            ]

            for method, url, query, body in poll_attempts:
                try:
                    if method == "GET":
                        resp = await client.get(url, headers=headers, params=query)
                    else:
                        resp = await client.post(url, headers=headers, json=body)

                    if resp.status_code >= 500:
                        continue

                    polled = _safe_response_json(resp)
                    polled["http_status"] = resp.status_code
                    polled["sent_params"] = strip_sensitive(params)
                    last_data = polled

                    if resp.status_code >= 400:
                        continue
                    if is_bad_response(polled):
                        return polled

                    pst = state_of(polled)
                    if pst in IN_PROGRESS_STATES:
                        new_id = find_request_id(polled)
                        if new_id:
                            request_id = new_id
                        continue

                    if pst in GOOD_STATES or has_meaningful_data(polled) or has_negative_empty_marker(polled):
                        return polled
                except Exception:
                    continue

        last_data["state"] = "timeout"
        last_data["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд. Требуется ручная проверка."
        return last_data


def _safe_response_json(resp: httpx.Response) -> Dict[str, Any]:
    try:
        value = resp.json()
        if isinstance(value, dict):
            return value
        return {"result": value}
    except Exception:
        return {"raw_text": resp.text[:1000]}


def find_request_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ["requestId", "request_id", "id", "qid", "newdb_qid", "task_id"]:
        val = data.get(key)
        if val:
            return clean_text(val)
    for key in ["params", "result", "data", "response"]:
        val = data.get(key)
        if isinstance(val, dict):
            nested = find_request_id(val)
            if nested:
                return nested
    return ""



def normalize_inn(value: str) -> Dict[str, str]:
    digits = only_digits(value)
    if len(digits) == 12:
        return {"type": "fl", "value": digits}
    if len(digits) == 10:
        return {"type": "ul", "value": digits}
    if digits:
        return {"type": "invalid", "value": digits}
    return {"type": "empty", "value": ""}


def api_method_invalid(data: Any) -> bool:
    txt = text_blob(data)
    return "method or country is not valid" in txt or "method is not valid" in txt


def skipped_source(reason: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"state": "skipped", "error": reason, "sent_params": strip_sensitive(params or {})}


def public_registry_data(obj: Any) -> Any:
    forbidden = {
        "requestid", "request_id", "newdb_qid", "qid", "task_id", "balance", "token",
        "api_key", "authorization", "x-api-key", "headers", "params", "sent_params",
        "http_status", "docs_url", "api docs", "errors_info", "error_code", "datecreated",
    }
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in forbidden:
                continue
            out[k] = public_registry_data(v)
        return out
    if isinstance(obj, list):
        return [public_registry_data(x) for x in obj]
    return obj


def method_variants(service: str, current: str) -> List[str]:
    variants = {
        "passport": [current, "passport_mvd", "passport"],
        "fssp": [current, "fssp", "fssp_ip", "fssp_fl", "fssp_physical", "fssp_person"],
        "bankruptcy": [current, "bankruptcy", "fedresurs", "bankrot", "efrsb"],
        "pledges": [current, "pledges", "zalog", "reestr_zalogov"],
        "courts": [current, "courts", "court", "kad_arbitr", "arbitr"],
        "egrn": [current, "rosreestr", "egrn"],
    }.get(service, [current])
    seen: List[str] = []
    for item in variants:
        if item and item not in seen:
            seen.append(item)
    return seen


async def newdb_service_post(service: str, params: Dict[str, Any], max_wait: int, poll_interval: int = 5) -> Dict[str, Any]:
    if params.get("__skip__"):
        return skipped_source(params.get("__skip_reason__") or public_error(), params)

    first_result: Optional[Dict[str, Any]] = None
    methods = method_variants(service, clean_text(params.get("method"))) if NEWDB_ENABLE_METHOD_FALLBACKS else [clean_text(params.get("method"))]
    for method in methods:
        attempt = dict(params)
        attempt["method"] = method
        result = await newdb_post(attempt, max_wait=max_wait, poll_interval=poll_interval)
        if first_result is None:
            first_result = result
        if not api_method_invalid(result):
            return result
    return first_result or skipped_source(public_error(), params)

# -----------------------------
# SOURCE PARAMS
# -----------------------------
def build_newdb_params(req: CheckRequest) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    prop = normalize_property(req)
    dob_ru = normalize_dob(req.dob)
    passport_series = only_digits(req.passport_series)
    passport_number = only_digits(req.passport_number)
    inn_info = normalize_inn(req.inn)

    last = clean_text(req.last)
    first = clean_text(req.first)
    middle = clean_text(req.middle)

    # ВАЖНО: не используем один общий person_base для всех методов.
    # По фактическому PDF было видно, что лишние secondname/dob/inn ломают проверки.
    passport_params = {
        "method": NEWDB_METHOD_PASSPORT,
        "lastname": last,
        "firstname": first,
        "middlename": middle,
        "birthdate": dob_ru,
        "country": "ru",
        "series": passport_series,
        "seria": passport_series,
        "number": passport_number,
    }

    fssp_params = {
        "method": NEWDB_METHOD_FSSP,
        "lastname": last,
        "firstname": first,
        "middlename": middle,
        "birthdate": dob_ru,
        "country": "ru",
        "region": int(req.region or 0),
    }

    person_params = {
        "lastname": last,
        "firstname": first,
        "middlename": middle,
        "birthdate": dob_ru,
        "country": "ru",
    }

    bankruptcy_params = {"method": NEWDB_METHOD_BANKRUPTCY, **person_params}
    pledges_params = {"method": NEWDB_METHOD_PLEDGES, **person_params}
    courts_params = {"method": NEWDB_METHOD_COURTS, **person_params}

    # Не отправляем 12-значный ИНН физлица как innyur/inn для юрлица.
    if inn_info["type"] == "ul":
        for block in (bankruptcy_params, pledges_params, courts_params):
            block["inn"] = inn_info["value"]
            block["innyur"] = inn_info["value"]
    elif inn_info["type"] == "fl":
        bankruptcy_params["innfl"] = inn_info["value"]
        courts_params["innfl"] = inn_info["value"]
        pledges_params["__skip__"] = True
        pledges_params["__skip_reason__"] = "Автоматическая проверка залогов по 12-значному ИНН физлица не выполнена. Требуется ручная проверка."
    elif inn_info["type"] == "invalid":
        bankruptcy_params["__skip__"] = True
        bankruptcy_params["__skip_reason__"] = "Передан некорректный ИНН. Требуется ручная проверка банкротства."
        pledges_params["__skip__"] = True
        pledges_params["__skip_reason__"] = "Передан некорректный ИНН. Требуется ручная проверка залогов."
        courts_params["__skip__"] = True
        courts_params["__skip_reason__"] = "Передан некорректный ИНН. Требуется ручная проверка судебных производств."

    egrn_params = {
        "method": NEWDB_METHOD_EGRN,
        "address": prop.get("address") or prop.get("query"),
        "cadnum": prop.get("cadastral_number"),
        "cadastral_number": prop.get("cadastral_number"),
        "country": "ru",
    }

    return {
        "passport": passport_params,
        "fssp": fssp_params,
        "bankruptcy": bankruptcy_params,
        "pledges": pledges_params,
        "courts": courts_params,
        "egrn": egrn_params,
    }, prop

# -----------------------------
# CLASSIFIERS
# -----------------------------
def classify_passport(data: Any) -> Dict[str, Any]:
    source = "Паспорт МВД"
    if is_bad_response(data) or is_in_progress(data):
        return manual_item(source, extract_error_details(data), MANUAL_URLS["passport"])
    txt = text_blob(data)
    if any(x in txt for x in ["недейств", "разыскивается", "invalid", "expired"]):
        return make_item(source, "risk", "Выявлены признаки проблемы с паспортом.", flatten_strings(data, 8), MANUAL_URLS["passport"])
    if any(x in txt for x in ["действителен", "действительный", "valid"]):
        return make_item(source, "ok", "Паспорт по полученным данным действителен.", [], MANUAL_URLS["passport"])
    return manual_item(source, ["Источник вернул ответ, который нельзя уверенно трактовать автоматически."], MANUAL_URLS["passport"])


def extract_amount(item: Any) -> float:
    if isinstance(item, dict):
        for key in [
            "amount", "sum", "debt", "debt_sum", "total", "balance", "debtSum", "ip_end_sum",
            "actual_debt", "outstanding", "remainder", "remain", "sum_debt",
        ]:
            if key in item and item[key] not in [None, ""]:
                try:
                    return float(str(item[key]).replace(" ", "").replace(",", "."))
                except Exception:
                    pass
    txt = text_blob(item)
    matches = re.findall(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:руб|₽)", txt, flags=re.I)
    amounts = []
    for m in matches:
        try:
            amounts.append(float(m.replace(" ", "").replace(",", ".")))
        except Exception:
            pass
    return max(amounts) if amounts else 0.0


def is_closed_fssp(item: Any) -> bool:
    txt = text_blob(item)
    closed_words = [
        "оконч", "прекращ", "закрыт", "заверш", "исполнено", "ст. 46", "статья 46",
        "ст.46", "returned", "terminated", "closed", "completed", "end_date", "date_end",
    ]
    return any(w in txt for w in closed_words)


def is_active_fssp(item: Any) -> bool:
    txt = text_blob(item)
    active_words = [
        "возбужден", "актив", "в процессе", "исполнительное производство", "задолженность", "остаток",
        "взыскание", "active", "in_process", "actual", "current", "department",
    ]
    return any(w in txt for w in active_words) and not is_closed_fssp(item)


def describe_fssp_record(item: Any) -> str:
    if not isinstance(item, dict):
        return clean_text(item)[:300]
    parts = []
    for key in ["number", "ip_number", "case_number", "exec_number", "date", "date_start", "subject", "article", "department", "bailiff"]:
        if item.get(key):
            parts.append(f"{key}: {clean_text(item.get(key))}")
    amount = extract_amount(item)
    if amount:
        parts.append(f"сумма: {rub(amount)}")
    return "; ".join(parts) or "; ".join(flatten_strings(item, 4))


def classify_fssp(data: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "ФССП"
    empty_stats = {
        "all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0,
        "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "actual_debt": 0.0,
        "active_items": [], "closed_items": [], "unknown_items": [],
    }

    if is_bad_response(data) or is_in_progress(data):
        return manual_item(source, extract_error_details(data), MANUAL_URLS["fssp"]), empty_stats

    items = extract_items(data)
    if not items and has_negative_empty_marker(data):
        return make_item(source, "ok", "По полученным данным исполнительные производства не выявлены.", [], MANUAL_URLS["fssp"]), empty_stats
    if not items and not has_meaningful_data(data):
        return manual_item(source, ["Источник вернул пустой или непонятный ответ."], MANUAL_URLS["fssp"]), empty_stats
    if not items:
        items = [data]

    active, closed, unknown = [], [], []
    for item in items:
        if is_closed_fssp(item):
            closed.append(item)
        elif is_active_fssp(item):
            active.append(item)
        else:
            unknown.append(item)

    active_sum = sum(extract_amount(x) for x in active)
    closed_sum = sum(extract_amount(x) for x in closed)
    unknown_sum = sum(extract_amount(x) for x in unknown)
    total_sum = active_sum + closed_sum + unknown_sum

    stats = {
        "all_count": len(items),
        "active_count": len(active),
        "closed_count": len(closed),
        "unknown_count": len(unknown),
        "total_sum_all": total_sum,
        "active_sum": active_sum,
        "closed_sum": closed_sum,
        "unknown_sum": unknown_sum,
        "actual_debt": active_sum,
        "active_items": public_registry_data(active),
        "closed_items": public_registry_data(closed),
        "unknown_items": public_registry_data(unknown),
    }

    details = [
        f"Всего найдено ИП: {len(items)}",
        f"Активные ИП: {len(active)}",
        f"Закрытые/оконченные ИП: {len(closed)}",
        f"Неоднозначные записи: {len(unknown)}",
        f"Общая сумма всех найденных ИП: {rub(total_sum)}",
        f"Сумма по активным ИП: {rub(active_sum)}",
        f"Сумма по закрытым ИП: {rub(closed_sum)}",
        f"Актуальный долг по активным ИП: {rub(active_sum)}",
    ]
    for rec in active[:5]:
        details.append("Активное ИП: " + describe_fssp_record(rec))
    for rec in closed[:5]:
        details.append("Закрытое/оконченное ИП: " + describe_fssp_record(rec))

    if active:
        return make_item(
            source,
            "risk",
            f"Найдены активные исполнительные производства. Актуальная сумма долга: {rub(active_sum)}.",
            details,
            MANUAL_URLS["fssp"],
        ), stats
    if closed and not active and not unknown:
        return make_item(
            source,
            "ok",
            f"Найдены исполнительные производства. Активных: 0, закрытых: {len(closed)}. Актуальный долг по активным производствам: 0 ₽.",
            details,
            MANUAL_URLS["fssp"],
        ), stats
    if unknown:
        return make_item(
            source,
            "manual_check",
            "Найдены исполнительные производства с неоднозначным статусом. Требуется ручная проверка.",
            details,
            MANUAL_URLS["fssp"],
        ), stats
    return make_item(source, "ok", "По полученным данным исполнительные производства не выявлены.", details, MANUAL_URLS["fssp"]), stats


def classify_empty_or_risk(data: Any, source: str, ok_summary: str, risk_summary: str, manual_url: str) -> Tuple[Dict[str, Any], List[Any]]:
    if is_bad_response(data) or is_in_progress(data):
        return manual_item(source, extract_error_details(data), manual_url), []
    items = extract_items(data)
    if not items and has_negative_empty_marker(data):
        return make_item(source, "ok", ok_summary, [], manual_url), []
    if not items and not has_meaningful_data(data):
        return manual_item(source, ["Источник вернул пустой или непонятный ответ."], manual_url), []
    if not items:
        # Не превращаем подозрительное слово из служебного текста в риск. Лучше manual_check.
        return manual_item(source, flatten_strings(data, 8) or ["Источник вернул неоднозначный ответ."], manual_url), []
    return make_item(source, "risk", risk_summary, flatten_strings(items, 12), manual_url), strip_sensitive(items)


def classify_egrn(data: Any, prop: Dict[str, str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "ЕГРН / Росреестр"
    if not prop.get("query"):
        return manual_item(source, ["Не передан кадастровый номер или адрес объекта."], MANUAL_URLS["egrn"]), {}
    if is_bad_response(data) or is_in_progress(data):
        return manual_item(source, extract_error_details(data), MANUAL_URLS["egrn"]), {}
    if has_negative_empty_marker(data) or not has_meaningful_data(data):
        return manual_item(source, [f"Запрос: {prop.get('query')}", "Объект не найден автоматически или ответ нельзя интерпретировать."], MANUAL_URLS["egrn"]), {}

    txt = text_blob(data)
    risk_words = ["арест", "запрет", "ограничение", "ипотека", "залог", "обременение", "рента", "запрещение"]
    found_risks = [w for w in risk_words if w in txt]
    details = []
    if prop.get("cadastral_number"):
        details.append(f"Кадастровый номер: {prop.get('cadastral_number')}")
    if prop.get("address") and prop.get("address") != prop.get("cadastral_number"):
        details.append(f"Адрес/запрос: {prop.get('address')}")
    details.extend(flatten_strings(data, 15))
    details = list(dict.fromkeys(details))[:20]

    if found_risks:
        return make_item(
            source,
            "risk",
            "Данные по объекту получены. В ответе есть признаки ограничений, обременений или иных рисков.",
            details,
            MANUAL_URLS["egrn"],
        ), strip_sensitive(data)
    return make_item(
        source,
        "ok",
        "Данные по объекту получены. По полученным данным явные признаки ограничений или обременений не выявлены.",
        details,
        MANUAL_URLS["egrn"],
    ), strip_sensitive(data)


# -----------------------------
# GIGACHAT
# -----------------------------
def gigachat_credentials() -> str:
    if GIGACHAT_CREDENTIALS:
        return GIGACHAT_CREDENTIALS
    if GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET:
        raw = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")
    return ""


def build_prompt_payload(req: CheckRequest, prop: Dict[str, str], checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    return {
        "seller": {
            "last": clean_text(req.last),
            "first": clean_text(req.first),
            "middle": clean_text(req.middle),
            "dob": normalize_dob(req.dob),
            "inn_provided": bool(only_digits(req.inn)),
            "passport_provided": bool(only_digits(req.passport_series) and only_digits(req.passport_number)),
            "fssp_region": req.region,
        },
        "property": prop,
        "checklist": checklist,
        "registry_data": registry_data,
        "warnings": warnings,
        "not_checked_or_manual": [x for x in checklist if x.get("status") == "manual_check"],
        "disclaimer": DISCLAIMER,
    }


async def generate_gigachat_report(payload: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    credentials = gigachat_credentials()
    if not credentials:
        return build_fallback_legal_report(payload), "GigaChat не настроен: отчет сформирован резервной backend-логикой."

    prompt = """Ты юрист-эксперт по недвижимости в Санкт-Петербурге.

На основе переданных структурированных данных сформируй подробный юридический отчет для покупателя недвижимости.

Строго соблюдай структуру:
1. Краткий вывод
2. Что проверено
3. Риски по продавцу
4. Риски по объекту
5. Что говорит в пользу сделки
6. Что обязательно проверить до аванса
7. Что прописать в авансовом соглашении / ПДКП
8. Безопасная схема расчетов
9. Итоговое заключение

Не придумывай факты.
Если данных нет — прямо пиши: “по предоставленным данным не проверялось”.
Если источник не ответил — пиши: “требуется ручная проверка”.
Не обещай 100% безопасность.
Не называй объект юридически чистым.
Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.

СТРУКТУРИРОВАННЫЕ ДАННЫЕ:
""" + safe_json(payload, 18000)

    try:
        # Используем официальный SDK, если он установлен из requirements.txt.
        from gigachat import GigaChat

        def call_sync() -> str:
            with GigaChat(credentials=credentials, scope=GIGACHAT_SCOPE, verify_ssl_certs=GIGACHAT_VERIFY_SSL_CERTS) as giga:
                resp = giga.chat({
                    "model": GIGACHAT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                })
                return clean_text(resp.choices[0].message.content)

        report = await asyncio.to_thread(call_sync)
        if report:
            return report, None
        return build_fallback_legal_report(payload), "GigaChat вернул пустой ответ: отчет сформирован резервной backend-логикой."
    except Exception as e:
        return build_fallback_legal_report(payload), f"GigaChat недоступен: отчет сформирован резервной backend-логикой. Причина: {clean_text(e)[:180]}"


def build_fallback_legal_report(payload: Dict[str, Any]) -> str:
    seller = payload.get("seller", {})
    prop = payload.get("property", {})
    checklist = payload.get("checklist", [])
    risks = [x for x in checklist if x.get("status") == "risk"]
    manual = [x for x in checklist if x.get("status") == "manual_check"]
    ok = [x for x in checklist if x.get("status") == "ok"]

    seller_name = " ".join([seller.get("last", ""), seller.get("first", ""), seller.get("middle", "")]).strip() or "по предоставленным данным не указан"
    obj = prop.get("query") or "по предоставленным данным не проверялось"

    if risks:
        short = "по автоматическим данным есть признаки риска, сделку нельзя двигать без ручной проверки."
    elif manual:
        short = "часть источников не вернула данные, требуется ручная проверка до аванса."
    else:
        short = "по полученным автоматическим данным явные риски не выделены, но это не гарантия безопасности."

    lines = [
        "1. Краткий вывод",
        f"Предварительный вывод: {short}",
        "",
        "2. Что проверено",
        f"Продавец: {seller_name}.",
        f"Дата рождения: {seller.get('dob') or 'по предоставленным данным не проверялось'}.",
        f"Объект: {obj}.",
        f"Проверок без явных рисков: {len(ok)}. Проверок с рисками: {len(risks)}. Требуют ручной проверки: {len(manual)}.",
        "",
        "3. Риски по продавцу",
    ]
    for item in checklist:
        if item.get("source") != "ЕГРН / Росреестр":
            lines.append(f"- {item.get('source')}: {item.get('summary')}")
    lines += ["", "4. Риски по объекту"]
    egrn = next((x for x in checklist if x.get("source") == "ЕГРН / Росреестр"), None)
    lines.append(f"- ЕГРН / Росреестр: {egrn.get('summary') if egrn else 'по предоставленным данным не проверялось'}")
    lines += [
        "",
        "5. Что говорит в пользу сделки",
        "В пользу сделки говорят только те пункты, где источник реально ответил и backend присвоил статус ok. Отсутствие ответа источника не считается отсутствием риска.",
        "",
        "6. Что обязательно проверить до аванса",
    ]
    if manual:
        for item in manual:
            lines.append(f"- {item.get('source')}: требуется ручная проверка. {item.get('manual_url') or ''}")
    else:
        lines.append("- Актуальная выписка ЕГРН, основание права, семейное положение продавца, полномочия, отсутствие свежих ограничений и долгов.")
    lines += [
        "",
        "7. Что прописать в авансовом соглашении / ПДКП",
        "Прописать обязанность продавца подтвердить отсутствие скрытых обременений, арестов, запретов, банкротства, активных исполнительных производств и судебных споров, влияющих на сделку.",
        "",
        "8. Безопасная схема расчетов",
        "При долгах, ограничениях или неполных данных использовать аккредитив, депозит нотариуса или иную контролируемую схему с раскрытием денег только после выполнения условий.",
        "",
        "9. Итоговое заключение",
        "Отчет является предварительным. Не обещает 100% безопасность и не заменяет ручную юридическую проверку документов специалистом.",
    ]
    return "\n".join(lines)


# -----------------------------
# PDF
# -----------------------------
def save_report_json(report_id: str, data: Dict[str, Any]) -> Path:
    path = REPORT_DIR / f"{report_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report_json(report_id: str) -> Dict[str, Any]:
    path = REPORT_DIR / f"{report_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Отчет не найден или уже удален.")
    return json.loads(path.read_text(encoding="utf-8"))


def cleanup_old_reports() -> None:
    cutoff = time.time() - REPORT_TTL_SECONDS
    for path in REPORT_DIR.glob("*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def generate_pdf_file(report_id: str, report_data: Dict[str, Any]) -> Path:
    pdf_path = REPORT_DIR / f"{report_id}.pdf"
    if SimpleDocTemplate is None:
        raise RuntimeError("reportlab is not installed")

    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=42)
    font_name = "Helvetica"
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]:
        try:
            if Path(font_path).exists():
                pdfmetrics.registerFont(TTFont("ReportFont", font_path))
                font_name = "ReportFont"
                break
        except Exception:
            pass

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleRU", parent=styles["Title"], fontName=font_name, fontSize=18, leading=24, alignment=TA_LEFT, spaceAfter=14)
    h_style = ParagraphStyle("HeadRU", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=18, spaceBefore=10, spaceAfter=6)
    p_style = ParagraphStyle("BodyRU", parent=styles["BodyText"], fontName=font_name, fontSize=10, leading=15, spaceAfter=5)
    small_style = ParagraphStyle("SmallRU", parent=p_style, fontSize=8, leading=12)

    def p(text: Any, style=p_style) -> Paragraph:
        return Paragraph(html.escape(clean_text(text)).replace("\n", "<br/>"), style)

    req = report_data.get("request", {})
    prop = report_data.get("property", {})
    checklist = report_data.get("checklist", [])
    registry_data = report_data.get("registry_data", {})
    legal_report = report_data.get("legal_report", "")

    story = [
        p("Юридический отчет по проверке продавца и объекта недвижимости", title_style),
        p(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", small_style),
        Spacer(1, 8),
        p("1. Данные продавца", h_style),
        p(f"ФИО: {' '.join([req.get('last',''), req.get('first',''), req.get('middle','')]).strip() or 'по предоставленным данным не указано'}"),
        p(f"Дата рождения: {req.get('dob') or 'по предоставленным данным не указана'}"),
        p(f"ИНН передан: {'да' if req.get('inn') else 'нет'}"),
        p("2. Данные объекта", h_style),
        p(f"Запрос: {prop.get('query') or 'по предоставленным данным не указан'}"),
        p(f"Кадастровый номер: {prop.get('cadastral_number') or 'по предоставленным данным не указан'}"),
        p("3. Чек-лист проверок", h_style),
    ]
    for item in checklist:
        status_label = {"ok": "Проверено", "risk": "Выявлен риск", "manual_check": "Требуется ручная проверка"}.get(item.get("status"), "Не определено")
        story.append(p(f"{item.get('source')}: {status_label}. {item.get('summary')}"))
        for d in item.get("details", [])[:12]:
            story.append(p(f"• {d}", small_style))

    story.append(p("4. Данные, полученные из реестров", h_style))
    for key, block in registry_data.items():
        story.append(p(f"{block.get('source') or key}: {block.get('summary','')}"))
        raw = block.get("data") or block.get("stats") or block.get("items")
        if raw:
            for line in safe_json(strip_sensitive(raw), 1800).split("\n")[:35]:
                story.append(p(line, small_style))

    story.append(p("5. Юридический отчет", h_style))
    for paragraph in legal_report.split("\n"):
        story.append(p(paragraph if paragraph.strip() else " ", p_style))

    story.append(p("Дисклеймер", h_style))
    story.append(p(DISCLAIMER, small_style))

    doc.build(story)
    return pdf_path


def absolute_pdf_url(request: Request, report_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/download-pdf/{report_id}"


def pdf_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


# -----------------------------
# ROUTES
# -----------------------------
@app.get("/")
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "success": True,
        "service": "Real Estate Seller & Property Check API",
        "version": "2.1.0",
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(gigachat_credentials()),
        "newdb_methods": {"passport": NEWDB_METHOD_PASSPORT, "fssp": NEWDB_METHOD_FSSP, "bankruptcy": NEWDB_METHOD_BANKRUPTCY, "pledges": NEWDB_METHOD_PLEDGES, "courts": NEWDB_METHOD_COURTS, "egrn": NEWDB_METHOD_EGRN},
        "endpoints": ["/check-report", "/download-pdf/{report_id}", "/health"],
    }


@app.post("/check-report")
async def check_report(req: CheckRequest, request: Request) -> JSONResponse:
    cleanup_old_reports()
    warnings: List[str] = []

    if not clean_text(req.last) or not clean_text(req.first):
        raise HTTPException(status_code=422, detail="Укажите фамилию и имя продавца.")

    params, prop = build_newdb_params(req)

    passport_raw, fssp_raw, bankruptcy_raw, pledges_raw, courts_raw, egrn_raw = await asyncio.gather(
        newdb_service_post("passport", params["passport"], max_wait=75, poll_interval=5),
        newdb_service_post("fssp", params["fssp"], max_wait=100, poll_interval=5),
        newdb_service_post("bankruptcy", params["bankruptcy"], max_wait=100, poll_interval=5),
        newdb_service_post("pledges", params["pledges"], max_wait=90, poll_interval=5),
        newdb_service_post("courts", params["courts"], max_wait=100, poll_interval=5),
        newdb_service_post("egrn", params["egrn"], max_wait=300, poll_interval=10),
    )

    passport_item = classify_passport(passport_raw)
    fssp_item, fssp_stats = classify_fssp(fssp_raw)
    bankruptcy_item, bankruptcy_items = classify_empty_or_risk(
        bankruptcy_raw,
        "Банкротство / Федресурс",
        "По полученным данным сведения о банкротстве не выявлены.",
        "Выявлены сведения, связанные с банкротством.",
        MANUAL_URLS["bankruptcy"],
    )
    pledges_item, pledges_items = classify_empty_or_risk(
        pledges_raw,
        "Залоги движимого имущества",
        "По полученным данным сведения о залогах движимого имущества не выявлены.",
        "Выявлены сведения о залогах. Требуется оценить предмет залога и связь с продавцом.",
        MANUAL_URLS["pledges"],
    )
    courts_item, courts_items = classify_empty_or_risk(
        courts_raw,
        "Суды / арбитраж",
        "По полученным данным судебные дела не выявлены.",
        "Найдены судебные производства. Требуется анализ предмета спора.",
        MANUAL_URLS["courts"],
    )
    egrn_item, egrn_data = classify_egrn(egrn_raw, prop)

    checklist = [passport_item, fssp_item, bankruptcy_item, pledges_item, courts_item, egrn_item]

    registry_data = {
        "passport": {"source": passport_item["source"], "status": passport_item["status"], "summary": passport_item["summary"], "details": passport_item["details"], "data": public_registry_data(passport_raw) if passport_item["status"] != "manual_check" else {}},
        "fssp": {"source": fssp_item["source"], "status": fssp_item["status"], "summary": fssp_item["summary"], "details": fssp_item["details"], "stats": fssp_stats, "data": public_registry_data(fssp_raw) if fssp_item["status"] != "manual_check" else {}},
        "bankruptcy": {"source": bankruptcy_item["source"], "status": bankruptcy_item["status"], "summary": bankruptcy_item["summary"], "details": bankruptcy_item["details"], "items": public_registry_data(bankruptcy_items)},
        "pledges": {"source": pledges_item["source"], "status": pledges_item["status"], "summary": pledges_item["summary"], "details": pledges_item["details"], "items": public_registry_data(pledges_items)},
        "courts": {"source": courts_item["source"], "status": courts_item["status"], "summary": courts_item["summary"], "details": courts_item["details"], "items": public_registry_data(courts_items)},
        "egrn": {"source": egrn_item["source"], "status": egrn_item["status"], "summary": egrn_item["summary"], "details": egrn_item["details"], "data": public_registry_data(egrn_data)},
    }

    for item in checklist:
        if item["status"] == "manual_check":
            warnings.append(f"{item['source']}: требуется ручная проверка.")

    prompt_payload = build_prompt_payload(req, prop, checklist, registry_data, warnings)
    legal_report, giga_warning = await generate_gigachat_report(prompt_payload)
    if giga_warning:
        warnings.append(giga_warning)

    report_id = str(uuid.uuid4())
    response_data: Dict[str, Any] = {
        "success": True,
        "report_id": report_id,
        "created_at": now_iso(),
        "request": strip_sensitive(req.model_dump()),
        "property": prop,
        "checklist": checklist,
        "registry_data": registry_data,
        # Совместимость с ранней версией виджета:
        "registry_results": registry_data,
        "legal_report": legal_report,
        "disclaimer": DISCLAIMER,
        "warnings": warnings,
        "pdf_available": False,
        "pdf_url": "",
        "pdf_base64": "",
    }

    save_report_json(report_id, response_data)
    try:
        pdf_path = generate_pdf_file(report_id, response_data)
        response_data["pdf_available"] = True
        response_data["pdf_url"] = absolute_pdf_url(request, report_id)
        # Оставляю base64 для твоего текущего frontend-виджета, где кнопка скачивания завязана именно на pdf_base64.
        response_data["pdf_base64"] = pdf_base64(pdf_path)
        save_report_json(report_id, response_data)
    except Exception as e:
        warnings.append(f"PDF временно не сформирован: {clean_text(e)[:180]}")
        response_data["warnings"] = warnings
        save_report_json(report_id, response_data)

    # Финальная защита: наружу не уходит token/balance/requestId.
    return JSONResponse(content=strip_sensitive(response_data))


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9-]{36}", report_id):
        raise HTTPException(status_code=400, detail="Некорректный идентификатор отчета.")

    pdf_path = REPORT_DIR / f"{report_id}.pdf"
    if not pdf_path.exists():
        data = load_report_json(report_id)
        try:
            pdf_path = generate_pdf_file(report_id, data)
        except Exception:
            raise HTTPException(status_code=503, detail="PDF временно недоступен. Попробуйте сформировать отчет повторно.")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"legal-real-estate-report-{report_id}.pdf",
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Не отдаем traceback/Render/внутренние детали пользователю.
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.",
            "warnings": ["Техническая ошибка скрыта от пользователя и не влияет на юридический вывод."],
        },
    )
