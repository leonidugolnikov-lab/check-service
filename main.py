from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime
import asyncio
import base64
import html
import json
import os
import re
import uuid

import httpx

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None

app = FastAPI(title="Real Estate Seller & Property Check API", version="7.0.0-prod")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip()
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports"))
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=360.0, write=60.0, pool=30.0)

DISCLAIMER = (
    "Отчет носит информационно-аналитический характер, не является гарантией полной юридической "
    "безопасности сделки и не заменяет ручную юридическую проверку документов специалистом."
)
CLIENT_MANUAL_TEXT = "Источник не вернул данные. Требуется ручная проверка."

DEFAULT_MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "arbitr": "https://kad.arbitr.ru",
    "pravosud": "https://sudrf.ru",
    "egrn": "https://rosreestr.gov.ru",
}

REPORT_CACHE: Dict[str, Dict[str, Any]] = {}


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    seller_inn: str = ""
    inn_fiz: str = ""
    innfiz: str = ""
    innfl: str = ""
    region: int | str = 78
    passport_series: str = ""
    passport_seria: str = ""
    passport_number: str = ""
    property_type: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    address: str = ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed and fixed.count("�") == 0:
                text = fixed
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


def only_digits(value: Any) -> str:
    return re.sub(r"\D+", "", clean_text(value))


def normalize_dob_ru(value: Any) -> str:
    value = clean_text(value)
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        y, m, d = value.split("-")
        return f"{d}.{m}.{y}"
    digits = only_digits(value)
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:4]}.{digits[4:]}"
    return value


def normalize_dob_iso(value: Any) -> str:
    value = clean_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    digits = only_digits(value)
    if len(digits) == 8:
        return f"{digits[4:]}-{digits[2:4]}-{digits[:2]}"
    return value


def normalize_region(value: Any) -> int | str:
    s = clean_text(value)
    if not s:
        return 78
    if re.fullmatch(r"0\d", s):
        return s
    try:
        return int(s)
    except Exception:
        return s


def seller_inn(req: CheckRequest) -> str:
    return only_digits(req.inn or req.seller_inn or req.inn_fiz or req.innfiz or req.innfl)


def seller_fio(req: CheckRequest) -> str:
    return " ".join(x for x in [clean_text(req.last), clean_text(req.first), clean_text(req.middle)] if x).strip()


def rub(value: Any) -> str:
    try:
        n = float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        n = 0.0
    if abs(n - int(n)) < 0.005:
        return f"{int(n):,}".replace(",", " ") + " ₽"
    return f"{n:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


def text_blob(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()


def sanitize_for_client(obj: Any) -> Any:
    forbidden = {
        "token", "balance", "api_key", "x-api-key", "authorization", "requestid", "request_id",
        "taskid", "task_id", "newdb_qid", "sent_params", "params", "webhook", "is_repeat", "tasks",
        "datecreated", "dateupdated", "errors_info", "docs_url", "http_status", "raw_text", "_http_status"
    }
    if isinstance(obj, dict):
        return {k: sanitize_for_client(v) for k, v in obj.items() if str(k).lower() not in forbidden}
    if isinstance(obj, list):
        return [sanitize_for_client(x) for x in obj]
    return obj


def sanitize_debug(obj: Any) -> Any:
    forbidden = {"token", "api_key", "x-api-key", "authorization", "balance"}
    if isinstance(obj, dict):
        return {k: sanitize_debug(v) for k, v in obj.items() if str(k).lower() not in forbidden}
    if isinstance(obj, list):
        return [sanitize_debug(x) for x in obj]
    return obj


def flatten_strings(obj: Any, limit: int = 12) -> List[str]:
    out: List[str] = []
    skip_values = {"ru", "complete", "done", "success", "ok", "none", "null", "true", "false"}

    def walk(x: Any):
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in {"params", "requestid", "request_id", "token", "balance", "taskid", "newdb_qid", "page_url", "query_inn"}:
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x not in (None, "", [], {}):
            s = clean_text(x)
            if not s or s.lower() in skip_values:
                return
            if re.fullmatch(r"[a-f0-9-]{20,}", s.lower()):
                return
            if len(s) <= 260:
                out.append(s)

    walk(obj)
    dedup = []
    seen = set()
    for s in out:
        if s not in seen:
            dedup.append(s)
            seen.add(s)
    return dedup[:limit]


def normalize_property(req: CheckRequest) -> Dict[str, str]:
    cadastral = clean_text(req.cadastral_number or req.cadastre_number)
    address = clean_text(req.address)
    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"

    if cadastral and re.match(cad_pattern, cadastral):
        return {"query": cadastral, "cadastral_number": cadastral, "address": cadastral, "type": "cadastral"}
    if address and re.match(cad_pattern, address):
        return {"query": address, "cadastral_number": address, "address": address, "type": "cadastral"}

    address = re.sub(r"\s+", " ", address)
    if address and not re.search(r"(?i)санкт[- ]петербург|ленинградская область", address):
        address = "Санкт-Петербург, " + address
    return {"query": address, "cadastral_number": "", "address": address, "type": "address" if address else "empty"}


def get_state(data: Dict[str, Any]) -> str:
    return clean_text(data.get("state") or data.get("status") or "").lower() if isinstance(data, dict) else ""


def has_api_error(data: Any) -> bool:
    if not isinstance(data, dict):
        return True
    txt = text_blob(data)
    if data.get("_http_status") and int(data.get("_http_status")) >= 400:
        return True
    if get_state(data) in {"error", "failed", "fail", "timeout", "denied", "rejected", "not_configured"}:
        return True
    if "errors_info" in txt or "error_code" in txt:
        return True
    bad = ["method or country is not valid", "not enough balance", "unauthorized", "forbidden", "bad request", "missing required", "не хватает обязательного", "не передан"]
    return any(x in txt for x in bad)


def extract_error_reason(data: Any) -> str:
    txt = text_blob(data)
    if "not_configured" in txt or "newdb_token" in txt:
        return "NEWDB_TOKEN не задан в переменных окружения."
    if "method or country is not valid" in txt:
        return "Источник не принял метод или страну запроса. Требуется уточнить метод newDB."
    if "service is unavailable" in txt or "parsing failed" in txt:
        return "Источник временно недоступен или не смог распарсить данные."
    if "not enough balance" in txt:
        return "Недостаточно баланса newDB."
    if "missing" in txt or "обязатель" in txt:
        return "Источник не принял запрос: не хватает обязательного параметра."
    return CLIENT_MANUAL_TEXT


def get_result_data(raw: Dict[str, Any], method: str) -> Tuple[Optional[int], Any, Dict[str, Any]]:
    try:
        block = raw.get("results", {}).get(method, {})
        result = block.get("result", {})
        status = result.get("status")
        data = result.get("data")
        return status, data, result
    except Exception:
        return None, None, {}


async def newdb_request(params: Dict[str, Any], max_wait: int = 150, poll_interval: int = 5, debug: bool = False) -> Dict[str, Any]:
    if not NEWDB_TOKEN:
        return {"state": "not_configured", "error": "NEWDB_TOKEN не задан."}

    headers = {"Content-Type": "application/json", "Accept": "application/json", "X-API-KEY": NEWDB_TOKEN}
    request_id = str(uuid.uuid4())
    payload = {"params": params, "requestId": request_id}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            r = await client.post(NEWDB_URL, headers=headers, json=payload)
            try:
                data = r.json()
            except Exception:
                data = {"raw_text": r.text}
            data["_http_status"] = r.status_code
        except Exception as e:
            return {"state": "error", "error": f"Ошибка запроса NewDB: {e}"}

        if r.status_code >= 400 or has_api_error(data):
            return sanitize_debug(data) if debug else sanitize_for_client(data)

        # Некоторые методы сразу возвращают complete.
        if get_state(data) in {"complete", "completed", "done", "success", "ready"}:
            return sanitize_debug(data) if debug else sanitize_for_client(data)

        elapsed = 0
        last = data
        poll_payload = {"params": params, "requestId": request_id}

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                rr = await client.post(NEWDB_URL, headers=headers, json=poll_payload)
                try:
                    polled = rr.json()
                except Exception:
                    polled = {"raw_text": rr.text}
                polled["_http_status"] = rr.status_code
                last = polled
            except Exception:
                continue

            if has_api_error(polled):
                return sanitize_debug(polled) if debug else sanitize_for_client(polled)
            if get_state(polled) in {"complete", "completed", "done", "success", "ready"}:
                return sanitize_debug(polled) if debug else sanitize_for_client(polled)

        last["state"] = "timeout"
        last["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд."
        return sanitize_debug(last) if debug else sanitize_for_client(last)


def build_newdb_payloads(req: CheckRequest) -> Dict[str, Optional[Dict[str, Any]]]:
    last = clean_text(req.last)
    first = clean_text(req.first)
    middle = clean_text(req.middle)
    fio = seller_fio(req)
    dob_iso = normalize_dob_iso(req.dob)
    inn = seller_inn(req)
    region = normalize_region(req.region)
    prop = normalize_property(req)
    passport_series = only_digits(req.passport_series or req.passport_seria)
    passport_number = only_digits(req.passport_number)

    payloads: Dict[str, Optional[Dict[str, Any]]] = {}

    payloads["passport"] = ({
        "seria": passport_series,
        "number": passport_number,
        "firstname": first,
        "lastname": last,
        "secondname": middle,
        "dob": dob_iso,
        "country": "ru",
        "method": os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd"),
    } if passport_series and passport_number and last and first else None)

    payloads["fssp"] = ({
        "firstname": first,
        "lastname": last,
        "secondname": middle,
        "dob": dob_iso,
        "regioncode": region,
        "country": "ru",
        "method": os.getenv("NEWDB_METHOD_FSSP", "fssp_person"),
    } if last and first and dob_iso else None)

    payloads["bankruptcy"] = ({
        "innfiz": inn,
        "country": "ru",
        "method": os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankrot_person"),
    } if len(inn) == 12 else None)

    payloads["arbitr"] = ({
        "innfiz": inn,
        "country": "ru",
        "method": os.getenv("NEWDB_METHOD_ARBITR", os.getenv("NEWDB_METHOD_COURTS", "arbitr_person")),
    } if len(inn) == 12 else None)

    # Суды общей юрисдикции / ГАС Правосудие. По документации сценарий pravosudfiz / pravo_search
    # ищет дела по ФИО, участнику, категории, тексту карточки и др. Оставляем метод настраиваемым.
    payloads["pravosud"] = ({
        "method": os.getenv("NEWDB_METHOD_PRAVOSUD", "pravo_search"),
        "country": "ru",
        "query": fio,
        "q": fio,
        "fio": fio,
        "lastname": last,
        "firstname": first,
        "secondname": middle,
        "party_name": fio,
        "limit": int(os.getenv("NEWDB_PRAVOSUD_LIMIT", "50")),
    } if fio else None)

    payloads["egrn"] = ({
        "address": prop["address"],
        "country": "ru",
        "method": os.getenv("NEWDB_METHOD_EGRN", "rosreestr"),
    } if prop.get("address") else None)

    return payloads


async def run_newdb_checks(req: CheckRequest, debug: bool = False) -> Dict[str, Any]:
    payloads = build_newdb_payloads(req)

    async def call(name: str, params: Optional[Dict[str, Any]], wait: int):
        if not params:
            return name, {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
        data = await newdb_request(params, max_wait=wait, poll_interval=5, debug=debug)
        return name, data

    async def call_pravosud(params: Optional[Dict[str, Any]], wait: int):
        if not params:
            return "pravosud", {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}

        tried: List[Dict[str, Any]] = []
        first = await newdb_request(params, max_wait=wait, poll_interval=5, debug=debug)
        tried.append({"method": params.get("method"), "response": first})

        # Страница newDB называется /fiz/pravosudfiz, но технический API-метод на странице указан как pravo_search.
        # Если API не принял метод, пробуем альтернативу автоматически.
        if has_api_error(first) and "method or country is not valid" in text_blob(first):
            for method in ["pravo_search", "pravosudfiz"]:
                if method == params.get("method"):
                    continue
                alt = dict(params)
                alt["method"] = method
                second = await newdb_request(alt, max_wait=wait, poll_interval=5, debug=debug)
                tried.append({"method": method, "response": second})
                if not has_api_error(second):
                    payloads["pravosud"] = alt
                    if debug and isinstance(second, dict):
                        second["_fallback_tried"] = tried
                    return "pravosud", second

        if debug and isinstance(first, dict):
            first["_fallback_tried"] = tried
        return "pravosud", first

    tasks = [
        call("passport", payloads["passport"], 90),
        call("fssp", payloads["fssp"], 180),
        call("bankruptcy", payloads["bankruptcy"], 150),
        call("arbitr", payloads["arbitr"], 150),
        call_pravosud(payloads["pravosud"], 180),
        call("egrn", payloads["egrn"], 420),
    ]
    pairs = await asyncio.gather(*tasks)
    return {"payloads": payloads, "responses": {k: v for k, v in pairs}}
def checklist_item(source: str, status: str, summary: str, details: Optional[List[str]] = None, url: str = "", data: Any = None) -> Dict[str, Any]:
    ui = "manual" if status == "manual_check" else status
    item = {
        "title": source,
        "source": source,
        "status": status,
        "ui_status": ui,
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
        "manual_url": url,
    }
    if data is not None:
        item["data"] = data
    return item


def amount_from_fssp_item(item: Any, prefer_remaining: bool = True) -> float:
    if not isinstance(item, dict):
        return 0.0
    text = clean_text(item.get("SubjectAndDebtAmount") or item.get("subject") or item.get("debt") or "")
    patterns = []
    if prefer_remaining:
        patterns.append(r"Остаток\s+долга[^:]*:\s*([\d\s]+(?:[\.,]\d{1,2})?)\s*руб")
    patterns.append(r"Сумма\s+долга\s*:\s*([\d\s]+(?:[\.,]\d{1,2})?)\s*руб")
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            try:
                return float(m.group(1).replace(" ", "").replace(",", "."))
            except Exception:
                return 0.0
    return 0.0


def is_fssp_closed(item: Dict[str, Any]) -> bool:
    reason = clean_text(item.get("CompletionDateOrReason") or item.get("completion") or "").lower()
    return bool(reason) and any(w in reason for w in ["ст. 46", "оконч", "прекращ", "заверш", "возвращ"])


def get_result_list(raw: Dict[str, Any], method: str) -> Tuple[Optional[int], List[Any], Dict[str, Any]]:
    status, data, result = get_result_data(raw, method)
    if isinstance(data, list):
        return status, data, result
    if data is None:
        return status, [], result
    return status, [data], result


def classify_passport(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "Паспорт МВД"
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT}
    status, items, _ = get_result_list(raw, "passport_mvd")
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Источник не вернул понятный результат проверки паспорта."], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT}
    txt = text_blob(items)
    if any(x in txt for x in ["недейств", "invalid", "разыскивается"]):
        summary = "Выявлены признаки проблемы с паспортом."
        return checklist_item(source, "risk", summary, flatten_strings(items, 6), DEFAULT_MANUAL_LINKS["passport"], sanitize_for_client(items)), {"summary": summary, "items": sanitize_for_client(items)}
    if any(x in txt for x in ["действительный", "действителен", "valid"]):
        summary = "Паспорт по полученным данным действителен."
        return checklist_item(source, "ok", summary, flatten_strings(items, 3), DEFAULT_MANUAL_LINKS["passport"], sanitize_for_client(items)), {"summary": summary, "items": sanitize_for_client(items)}
    return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Результат проверки паспорта неоднозначный."], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT}


def classify_fssp(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "ФССП"
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["fssp"]), {"summary": CLIENT_MANUAL_TEXT}
    status, items, _ = get_result_list(raw, "fssp_person")
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ ФССП не содержит успешного результата."], DEFAULT_MANUAL_LINKS["fssp"]), {"summary": CLIENT_MANUAL_TEXT}
    if not items:
        summary = "По полученным данным исполнительные производства не найдены."
        details = ["ФССП искался по ФИО, дате рождения и региону. При ошибке во входных данных ИП могут не попасть в автоматический ответ."]
        stats = {"all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0, "total_sum_all": 0, "active_sum": 0, "closed_sum": 0, "unknown_sum": 0, "actual_debt": 0, "active_items": [], "closed_items": [], "unknown_items": []}
        return checklist_item(source, "ok", summary, details, DEFAULT_MANUAL_LINKS["fssp"], stats), {"summary": summary, **stats}

    active, closed, unknown = [], [], []
    for it in items:
        if not isinstance(it, dict):
            unknown.append(it)
        elif is_fssp_closed(it):
            closed.append(it)
        elif clean_text(it.get("EnforcementProceeding")):
            active.append(it)
        else:
            unknown.append(it)

    active_sum = sum(amount_from_fssp_item(x, True) for x in active)
    closed_sum = sum(amount_from_fssp_item(x, False) for x in closed)
    unknown_sum = sum(amount_from_fssp_item(x, True) for x in unknown)
    total_sum = active_sum + closed_sum + unknown_sum
    actual_debt = active_sum + unknown_sum
    stats = {
        "all_count": len(items), "active_count": len(active), "closed_count": len(closed), "unknown_count": len(unknown),
        "total_sum_all": total_sum, "active_sum": active_sum, "closed_sum": closed_sum, "unknown_sum": unknown_sum,
        "actual_debt": actual_debt, "active_items": sanitize_for_client(active), "closed_items": sanitize_for_client(closed), "unknown_items": sanitize_for_client(unknown)
    }
    details = [
        f"Всего найдено ИП: {len(items)}", f"Активные ИП: {len(active)}", f"Закрытые/оконченные ИП: {len(closed)}",
        f"Неоднозначные записи: {len(unknown)}", f"Общая сумма всех найденных ИП: {rub(total_sum)}",
        f"Сумма по активным ИП: {rub(active_sum)}", f"Сумма по закрытым ИП: {rub(closed_sum)}",
        f"Актуальный долг по активным/неоднозначным ИП: {rub(actual_debt)}",
    ]
    if active or unknown or actual_debt > 0:
        summary = f"Найдены активные или неоднозначные исполнительные производства. Актуальная сумма для ручной оценки: {rub(actual_debt)}."
        return checklist_item(source, "risk", summary, details, DEFAULT_MANUAL_LINKS["fssp"], stats), {"summary": summary, **stats}
    summary = f"Найдены только закрытые/оконченные ИП. Актуальный долг по активным производствам: {rub(0)}."
    return checklist_item(source, "ok", summary, details, DEFAULT_MANUAL_LINKS["fssp"], stats), {"summary": summary, **stats}


def classify_bankruptcy(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "Банкротство / Федресурс"
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["bankruptcy"]), {"summary": CLIENT_MANUAL_TEXT}
    status, items, _ = get_result_list(raw, "bankrot_person")
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ Федресурса не содержит успешного результата."], DEFAULT_MANUAL_LINKS["bankruptcy"]), {"summary": CLIENT_MANUAL_TEXT}
    meaningful = []
    for it in items:
        if isinstance(it, dict):
            for k in ["bankruptcy", "publications", "encumbrances", "cases", "items"]:
                if isinstance(it.get(k), list) and it.get(k):
                    meaningful.extend(it[k])
        elif it:
            meaningful.append(it)
    if meaningful:
        summary = "Выявлены сведения, связанные с банкротством."
        return checklist_item(source, "risk", summary, flatten_strings(meaningful, 8), DEFAULT_MANUAL_LINKS["bankruptcy"], sanitize_for_client(meaningful)), {"summary": summary, "items": sanitize_for_client(meaningful)}
    summary = "По полученным данным сведения о банкротстве физлица не выявлены."
    return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["bankruptcy"], sanitize_for_client(items)), {"summary": summary, "items": sanitize_for_client(items)}


def classify_arbitr(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "Арбитражные суды"
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["arbitr"]), {"summary": CLIENT_MANUAL_TEXT}
    status, items, _ = get_result_list(raw, "arbitr_person")
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ КАД не содержит успешного результата."], DEFAULT_MANUAL_LINKS["arbitr"]), {"summary": CLIENT_MANUAL_TEXT}
    cases = []
    explicit_empty = False
    for it in items:
        if isinstance(it, dict):
            if it.get("found") is False and not it.get("cases"):
                explicit_empty = True
            if isinstance(it.get("cases"), list) and it.get("cases"):
                cases.extend(it["cases"])
            for k in ["items", "records", "cases_list"]:
                if isinstance(it.get(k), list) and it.get(k):
                    cases.extend(it[k])
    if cases:
        summary = "Найдены арбитражные дела. Требуется анализ предмета спора."
        return checklist_item(source, "risk", summary, flatten_strings(cases, 8), DEFAULT_MANUAL_LINKS["arbitr"], sanitize_for_client(cases)), {"summary": summary, "items": sanitize_for_client(cases)}
    if explicit_empty or not items:
        summary = "По полученным данным арбитражные дела не выявлены."
        return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["arbitr"], []), {"summary": summary, "items": []}
    meaningful = []
    for it in items:
        if isinstance(it, dict):
            copy = {k: v for k, v in it.items() if str(k).lower() not in {"query_inn", "inn", "innfiz", "page_url", "url", "link", "found"} and v not in (None, False, "", [], {})}
            if copy:
                meaningful.append(copy)
    if meaningful:
        summary = "Найдены судебные сведения в арбитраже. Требуется ручной анализ."
        return checklist_item(source, "risk", summary, flatten_strings(meaningful, 8), DEFAULT_MANUAL_LINKS["arbitr"], sanitize_for_client(meaningful)), {"summary": summary, "items": sanitize_for_client(meaningful)}
    summary = "По полученным данным арбитражные дела не выявлены."
    return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["arbitr"], []), {"summary": summary, "items": []}


def classify_pravosud(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "Суды общей юрисдикции / ГАС Правосудие"
    method = os.getenv("NEWDB_METHOD_PRAVOSUD", "pravo_search")
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["pravosud"]), {"summary": CLIENT_MANUAL_TEXT}
    status, data, result = get_result_data(raw, method)
    # Если пользователь переопределил метод на pravo_search, но results вернулись по другому ключу — ищем первый result.
    if status is None and isinstance(raw.get("results"), dict):
        for _, block in raw.get("results", {}).items():
            if isinstance(block, dict) and isinstance(block.get("result"), dict):
                result = block["result"]
                status = result.get("status")
                data = result.get("data")
                break
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ ГАС Правосудие не содержит успешного результата."], DEFAULT_MANUAL_LINKS["pravosud"]), {"summary": CLIENT_MANUAL_TEXT}
    items = data if isinstance(data, list) else ([] if data in (None, {}, "") else [data])
    meta = result.get("meta") if isinstance(result, dict) else {}
    if isinstance(meta, dict) and int(meta.get("count") or 0) == 0 and not items:
        summary = "По полученным данным дела в судах общей юрисдикции не выявлены."
        return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["pravosud"], []), {"summary": summary, "items": []}
    # Явные пустые ответы.
    if not items:
        summary = "По полученным данным дела в судах общей юрисдикции не выявлены."
        return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["pravosud"], []), {"summary": summary, "items": []}
    cases = []
    explicit_empty = False
    for it in items:
        if isinstance(it, dict):
            if it.get("found") is False and not it.get("cases") and not it.get("data"):
                explicit_empty = True
            for k in ["cases", "items", "records", "data", "results"]:
                if isinstance(it.get(k), list) and it.get(k):
                    cases.extend(it[k])
            # Формат pravo_search часто сразу возвращает карточки дел в data[].
            case_indicators = {"case_id", "case_number", "court_url", "judge_name", "category_text", "parties", "acts"}
            if any(k in it and it.get(k) not in (None, "", [], {}) for k in case_indicators):
                cases.append(it)
        elif it:
            cases.append(it)
    if cases:
        summary = "Найдены дела в судах общей юрисдикции. Требуется анализ предмета спора и роли продавца."
        return checklist_item(source, "risk", summary, flatten_strings(cases, 10), DEFAULT_MANUAL_LINKS["pravosud"], sanitize_for_client(cases)), {"summary": summary, "items": sanitize_for_client(cases)}
    if explicit_empty:
        summary = "По полученным данным дела в судах общей юрисдикции не выявлены."
        return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["pravosud"], []), {"summary": summary, "items": []}
    meaningful = []
    for it in items:
        if isinstance(it, dict):
            copy = {k: v for k, v in it.items() if str(k).lower() not in {"query", "q", "fio", "url", "page_url", "found"} and v not in (None, False, "", [], {})}
            if copy:
                meaningful.append(copy)
    if meaningful:
        summary = "Источник вернул судебные сведения. Требуется ручная оценка релевантности."
        return checklist_item(source, "risk", summary, flatten_strings(meaningful, 8), DEFAULT_MANUAL_LINKS["pravosud"], sanitize_for_client(meaningful)), {"summary": summary, "items": sanitize_for_client(meaningful)}
    summary = "По полученным данным дела в судах общей юрисдикции не выявлены."
    return checklist_item(source, "ok", summary, [], DEFAULT_MANUAL_LINKS["pravosud"], []), {"summary": summary, "items": []}


def classify_egrn(raw: Dict[str, Any], prop: Dict[str, str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "ЕГРН / Росреестр"
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}
    status, objects, _ = get_result_list(raw, "rosreestr")
    if status != 200 or not objects:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Росреестр не вернул реальные данные объекта."], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}
    obj = objects[0] if isinstance(objects[0], dict) else {}
    if not obj or not (obj.get("cadNumber") or obj.get("address") or obj.get("rights") or obj.get("encumbrances")):
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ Росреестра не содержит карточку объекта."], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}
    addr = obj.get("address") or {}
    readable = addr.get("readableAddress") if isinstance(addr, dict) else ""
    details = []
    if obj.get("cadNumber"):
        details.append(f"Кадастровый номер: {obj.get('cadNumber')}")
    if readable:
        details.append(f"Адрес: {readable}")
    if obj.get("objType_text"):
        details.append(f"Тип объекта: {obj.get('objType_text')}")
    if obj.get("purpose_text"):
        details.append(f"Назначение: {obj.get('purpose_text')}")
    if obj.get("area"):
        details.append(f"Площадь: {obj.get('area')} кв.м")
    if obj.get("cadCost"):
        details.append(f"Кадастровая стоимость: {rub(obj.get('cadCost'))}")
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    if rights:
        details.append(f"Записей о правах: {len(rights)}")
    enc = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    for e in enc:
        if isinstance(e, dict):
            desc = clean_text(e.get("typeDesc")) or f"Тип ограничения: {clean_text(e.get('type'))}"
            num = clean_text(e.get("encumbranceNumber"))
            start = clean_text(e.get("startDate"))
            row = desc
            if num:
                row += f", № {num}"
            if start:
                row += f", дата начала: {start}"
            details.append(row)
    if enc:
        summary = "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН."
        return checklist_item(source, "risk", summary, details, DEFAULT_MANUAL_LINKS["egrn"], sanitize_for_client(obj)), {"summary": summary, "object": sanitize_for_client(obj), "encumbrances": sanitize_for_client(enc)}
    summary = "Данные по объекту получены. По полученным данным явные признаки ограничений или обременений не выявлены."
    return checklist_item(source, "ok", summary, details, DEFAULT_MANUAL_LINKS["egrn"], sanitize_for_client(obj)), {"summary": summary, "object": sanitize_for_client(obj), "encumbrances": []}


def classify_all(req: CheckRequest, newdb: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
    raw = newdb.get("responses", {})
    prop = normalize_property(req)
    warnings: List[str] = []
    passport_item, passport_data = classify_passport(raw.get("passport", {}))
    fssp_item, fssp_data = classify_fssp(raw.get("fssp", {}))
    bank_item, bank_data = classify_bankruptcy(raw.get("bankruptcy", {}))
    arbitr_item, arbitr_data = classify_arbitr(raw.get("arbitr", {}))
    pravosud_item, pravosud_data = classify_pravosud(raw.get("pravosud", {}))
    egrn_item, egrn_data = classify_egrn(raw.get("egrn", {}), prop)
    checklist = [passport_item, fssp_item, bank_item, arbitr_item, pravosud_item, egrn_item]
    registry_data = {
        "passport": {"title": "Паспорт МВД", **passport_data},
        "fssp": {"title": "ФССП", **fssp_data},
        "bankruptcy": {"title": "Банкротство / Федресурс", **bank_data},
        "arbitr": {"title": "Арбитражные суды", **arbitr_data},
        "pravosud": {"title": "Суды общей юрисдикции / ГАС Правосудие", **pravosud_data},
        "egrn": {"title": "ЕГРН / Росреестр", **egrn_data},
    }
    return checklist, registry_data, warnings


def risk_scoring(checklist: List[Dict[str, Any]], registry: Dict[str, Any]) -> Dict[str, Any]:
    score = 0
    factors = []
    by_source = {x["source"]: x for x in checklist}

    def add(points: int, text: str):
        nonlocal score
        score += points
        factors.append(f"{text} (+{points})")

    if by_source.get("Паспорт МВД", {}).get("status") == "risk":
        add(25, "Паспорт: выявлена проблема")
    elif by_source.get("Паспорт МВД", {}).get("status") == "manual_check":
        add(8, "Паспорт: требуется ручная проверка")

    fssp = registry.get("fssp", {})
    if by_source.get("ФССП", {}).get("status") == "risk":
        add(35 if float(fssp.get("actual_debt") or 0) > 0 else 20, f"ФССП: активные/неоднозначные ИП, сумма {rub(fssp.get('actual_debt') or 0)}")
    elif by_source.get("ФССП", {}).get("status") == "manual_check":
        add(8, "ФССП: требуется ручная проверка")

    if by_source.get("Банкротство / Федресурс", {}).get("status") == "risk":
        add(45, "Банкротство: выявлены сведения")
    elif by_source.get("Банкротство / Федресурс", {}).get("status") == "manual_check":
        add(8, "Банкротство: требуется ручная проверка")

    if by_source.get("Арбитражные суды", {}).get("status") == "risk":
        add(15, "Арбитраж: найдены дела")
    elif by_source.get("Арбитражные суды", {}).get("status") == "manual_check":
        add(6, "Арбитраж: требуется ручная проверка")

    if by_source.get("Суды общей юрисдикции / ГАС Правосудие", {}).get("status") == "risk":
        add(20, "ГАС Правосудие: найдены дела")
    elif by_source.get("Суды общей юрисдикции / ГАС Правосудие", {}).get("status") == "manual_check":
        add(6, "ГАС Правосудие: требуется ручная проверка")

    if by_source.get("ЕГРН / Росреестр", {}).get("status") == "risk":
        add(60, "ЕГРН: выявлены ограничения/обременения")
    elif by_source.get("ЕГРН / Росреестр", {}).get("status") == "manual_check":
        add(12, "ЕГРН: требуется ручная проверка")

    score = min(100, score)
    if score >= 80:
        level = "опасная"
        conclusion = "Сделку нельзя выводить на аванс без ручного юридического разбора и устранения выявленных факторов."
    elif score >= 40:
        level = "условно рискованная"
        conclusion = "Сделку можно рассматривать только после уточнения рисков и фиксации защитных условий."
    else:
        level = "условно безопасная"
        conclusion = "По автоматическим данным критические риски не выявлены, но нужна ручная сверка документов перед авансом."
    return {"score": score, "max_score": 100, "level": level, "conclusion": conclusion, "factors": factors}


def build_recommendations(checklist: List[Dict[str, Any]], registry: Dict[str, Any]) -> List[Dict[str, str]]:
    recs: List[Dict[str, str]] = []
    by = {x["source"]: x for x in checklist}
    if by.get("ЕГРН / Росреестр", {}).get("status") == "risk":
        recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки", "text": "По ЕГРН выявлены ограничения/обременения. До аванса нужно получить основание ограничения и прописать обязанность продавца снять его в конкретный срок."})
        recs.append({"priority": "critical", "title": "Не передавать деньги напрямую продавцу", "text": "При запрете регистрации использовать нотариальный депозит или аккредитив с раскрытием денег только после снятия ограничения и регистрации перехода права."})
    fssp = registry.get("fssp", {})
    if by.get("ФССП", {}).get("status") == "risk":
        recs.append({"priority": "high", "title": "Закрыть активные ИП до сделки или через контролируемые расчеты", "text": f"Актуальная сумма по активным/неоднозначным ИП: {rub(fssp.get('actual_debt') or 0)}. В ПДКП прописать порядок погашения и последствия неснятия ограничений."})
    if by.get("Банкротство / Федресурс", {}).get("status") == "risk":
        recs.append({"priority": "critical", "title": "Не выходить на сделку без анализа банкротного риска", "text": "Сведения о банкротстве требуют анализа периода, статуса процедуры и риска оспаривания сделки."})
    if by.get("Арбитражные суды", {}).get("status") == "risk" or by.get("Суды общей юрисдикции / ГАС Правосудие", {}).get("status") == "risk":
        recs.append({"priority": "high", "title": "Разобрать судебные дела по предмету спора", "text": "Нужно понять роль продавца, предмет спора, сумму требований и связь с недвижимостью, долгами или банкротством."})
    if by.get("Паспорт МВД", {}).get("status") != "ok":
        recs.append({"priority": "medium", "title": "Проверить паспорт вручную", "text": "До аванса проверить действительность паспорта МВД и сверить данные с правоустанавливающими документами."})
    recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса/задатка и ответственность при неподтверждении данных."})
    return recs


def build_legal_report(req: CheckRequest, checklist: List[Dict[str, Any]], registry: Dict[str, Any], scoring: Dict[str, Any], recommendations: List[Dict[str, str]]) -> str:
    fio = seller_fio(req) or "не указан"
    prop = normalize_property(req)
    risks = [x for x in checklist if x.get("status") == "risk"]
    oks = [x for x in checklist if x.get("status") == "ok"]
    manuals = [x for x in checklist if x.get("status") == "manual_check"]
    lines = [
        "1. Краткий вывод",
        f"Сделка оценена как: {scoring['level'].upper()} ({scoring['score']}/100). {scoring['conclusion']}",
        "",
        "2. Что проверено",
        f"Продавец: {fio}. Дата рождения: {normalize_dob_ru(req.dob) or 'не указана'}. ИНН: {'передан' if seller_inn(req) else 'не передан'}.",
        f"Объект: {prop.get('cadastral_number') or prop.get('address') or 'не указан'}.",
        f"Проверено без явных рисков: {len(oks)}. Рисков: {len(risks)}. Требуют ручной проверки: {len(manuals)}.",
        "",
        "3. Основные риски",
    ]
    lines.extend([f"- {x['source']}: {x['summary']}" for x in risks] or ["- По автоматическим данным критические риски не выявлены."])
    lines += ["", "4. Что говорит в пользу сделки"]
    lines.extend([f"- {x['source']}: {x['summary']}" for x in oks] or ["- Нет источников, по которым можно уверенно сделать положительный вывод."])
    lines += ["", "5. Что обязательно сделать до аванса"]
    for r in recommendations:
        lines.append(f"- {r['title']}: {r['text']}")
    lines += [
        "", "6. Безопасная схема расчетов",
        "При выявленных долгах, запретах или неполных данных не передавать деньги напрямую продавцу. Использовать нотариальный депозит, аккредитив или иную условную схему с раскрытием денег только после выполнения условий.",
        "", "7. Итоговое заключение",
        "Отчет не обещает 100% безопасность сделки. При выявленных ограничениях, активных ИП или судебных делах сделка должна проходить только после ручного юридического анализа документов и условий расчетов.",
    ]
    return "\n".join(lines)


def status_label(status: str) -> str:
    return {"ok": "Проверено", "risk": "Риск", "manual_check": "Ручная проверка", "manual": "Ручная проверка"}.get(status, status)


def pdf_escape(text: Any) -> str:
    return html.escape(clean_text(text)).replace("\n", "<br/>")


def pdf_styles():
    base_font = "Helvetica"
    bold_font = "Helvetica-Bold"
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        if Path(font_path).exists():
            pdfmetrics.registerFont(TTFont("DejaVu", font_path))
            base_font = "DejaVu"
        if Path(bold_path).exists():
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold_path))
            bold_font = "DejaVu-Bold"
    except Exception:
        pass
    return {
        "title": ParagraphStyle("Title", fontName=bold_font, fontSize=18, leading=22, textColor=colors.HexColor("#0F3D56"), alignment=TA_CENTER, spaceAfter=8),
        "h2": ParagraphStyle("H2", fontName=bold_font, fontSize=12.5, leading=16, textColor=colors.HexColor("#0F3D56"), spaceBefore=8, spaceAfter=5),
        "body": ParagraphStyle("Body", fontName=base_font, fontSize=9.2, leading=13, textColor=colors.HexColor("#111827"), spaceAfter=4),
        "small": ParagraphStyle("Small", fontName=base_font, fontSize=8, leading=11, textColor=colors.HexColor("#4B5563")),
        "risk": ParagraphStyle("Risk", fontName=bold_font, fontSize=13, leading=17, textColor=colors.HexColor("#991B1B"), alignment=TA_CENTER),
        "ok": ParagraphStyle("Ok", fontName=bold_font, fontSize=13, leading=17, textColor=colors.HexColor("#166534"), alignment=TA_CENTER),
    }


def create_pdf(report_id: str, req: CheckRequest, checklist: List[Dict[str, Any]], registry: Dict[str, Any], scoring: Dict[str, Any], recommendations: List[Dict[str, str]], legal_report: str) -> Path:
    if SimpleDocTemplate is None:
        raise RuntimeError("reportlab не установлен. Добавьте reportlab в requirements.txt")
    path = REPORT_DIR / f"{report_id}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=15*mm, leftMargin=15*mm, topMargin=14*mm, bottomMargin=14*mm)
    s = pdf_styles()
    story: List[Any] = []

    story.append(Paragraph("Юридический отчет по проверке продавца и объекта недвижимости", s["title"]))
    story.append(Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", s["small"]))
    story.append(Spacer(1, 5))

    level_style = s["risk"] if scoring.get("level") in {"опасная", "условно рискованная"} else s["ok"]
    risk_table = Table([
        [Paragraph("Оценка сделки", s["small"]), Paragraph("Риск", s["small"]), Paragraph("Вывод", s["small"])],
        [Paragraph(scoring.get("level", "" ).upper(), level_style), Paragraph(f"{scoring.get('score', 0)}/100", level_style), Paragraph(pdf_escape(scoring.get("conclusion", "")), s["body"])],
    ], colWidths=[43*mm, 25*mm, 100*mm])
    risk_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#F5F3EF")),
        ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor("#D4A373")),
        ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 6), ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(risk_table)
    story.append(Spacer(1, 8))

    story.append(Paragraph("1. Данные продавца и объекта", s["h2"]))
    prop = normalize_property(req)
    seller_rows = [
        [Paragraph("ФИО", s["small"]), Paragraph(pdf_escape(seller_fio(req) or "не указано"), s["body"])],
        [Paragraph("Дата рождения", s["small"]), Paragraph(pdf_escape(normalize_dob_ru(req.dob) or "не указана"), s["body"])],
        [Paragraph("ИНН", s["small"]), Paragraph("передан" if seller_inn(req) else "не передан", s["body"])],
        [Paragraph("Объект", s["small"]), Paragraph(pdf_escape(prop.get("cadastral_number") or prop.get("address") or "не указан"), s["body"])],
    ]
    t = Table(seller_rows, colWidths=[38*mm, 130*mm])
    t.setStyle(TableStyle([("BOX", (0,0), (-1,-1), 0.4, colors.HexColor("#E5E7EB")), ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E5E7EB")), ("VALIGN", (0,0), (-1,-1), "TOP"), ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#F9FAFB")), ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5), ("TOPPADDING", (0,0), (-1,-1), 4), ("BOTTOMPADDING", (0,0), (-1,-1), 4)]))
    story.append(t)

    story.append(Paragraph("2. Чек-лист проверок", s["h2"]))
    rows = [[Paragraph("Источник", s["small"]), Paragraph("Статус", s["small"]), Paragraph("Краткий вывод", s["small"])]]
    for it in checklist:
        rows.append([Paragraph(pdf_escape(it["source"]), s["body"]), Paragraph(status_label(it.get("status", "")), s["body"]), Paragraph(pdf_escape(it.get("summary", "")), s["body"])])
    table = Table(rows, colWidths=[48*mm, 28*mm, 92*mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0F3D56")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("BOX", (0,0), (-1,-1), 0.4, colors.HexColor("#CBD5E1")), ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E5E7EB")), ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 4), ("RIGHTPADDING", (0,0), (-1,-1), 4), ("TOPPADDING", (0,0), (-1,-1), 4), ("BOTTOMPADDING", (0,0), (-1,-1), 4)]))
    story.append(table)

    story.append(Paragraph("3. Ключевые найденные данные", s["h2"]))
    fssp = registry.get("fssp", {})
    story.append(Paragraph(f"ФССП: всего ИП — {fssp.get('all_count', 0)}, активные — {fssp.get('active_count', 0)}, закрытые — {fssp.get('closed_count', 0)}, актуальный долг — {rub(fssp.get('actual_debt') or 0)}.", s["body"]))
    egrn = registry.get("egrn", {})
    if egrn.get("encumbrances"):
        story.append(Paragraph("ЕГРН: выявлены ограничения/обременения:", s["body"]))
        for e in egrn.get("encumbrances", [])[:8]:
            if isinstance(e, dict):
                txt = clean_text(e.get("typeDesc")) or f"Тип {clean_text(e.get('type'))}"
                if e.get("encumbranceNumber"):
                    txt += f", № {e.get('encumbranceNumber')}"
                if e.get("startDate"):
                    txt += f", дата начала: {e.get('startDate')}"
                story.append(Paragraph("• " + pdf_escape(txt), s["body"]))
    else:
        story.append(Paragraph("ЕГРН: явные ограничения/обременения по автоматическому ответу не выявлены.", s["body"]))

    story.append(Paragraph("4. Рекомендации", s["h2"]))
    for r in recommendations:
        story.append(Paragraph(f"<b>{pdf_escape(r.get('title'))}</b>: {pdf_escape(r.get('text'))}", s["body"]))

    story.append(Paragraph("5. Юридическое заключение", s["h2"]))
    for para in legal_report.split("\n"):
        p = clean_text(para)
        if not p:
            story.append(Spacer(1, 3))
            continue
        if re.match(r"^\d+\.\s", p):
            story.append(Paragraph(pdf_escape(p), s["h2"]))
        else:
            story.append(Paragraph(pdf_escape(p), s["body"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Дисклеймер", s["h2"]))
    story.append(Paragraph(pdf_escape(DISCLAIMER), s["small"]))
    doc.build(story)
    return path


def build_response(req: CheckRequest, newdb: Dict[str, Any], debug: bool = False) -> Dict[str, Any]:
    checklist, registry_data, warnings = classify_all(req, newdb)
    scoring = risk_scoring(checklist, registry_data)
    recommendations = build_recommendations(checklist, registry_data)
    legal_report = build_legal_report(req, checklist, registry_data, scoring, recommendations)
    return {
        "success": True,
        "payloads": sanitize_debug(newdb.get("payloads", {})) if debug else None,
        "responses": sanitize_debug(newdb.get("responses", {})) if debug else None,
        "checklist": checklist,
        "classified_checklist": checklist,
        "registry_data": registry_data,
        "risk_scoring": scoring,
        "recommendations": recommendations,
        "legal_report": legal_report,
        "warnings": warnings,
        "notes": [
            "Залоги движимого имущества отключены и не участвуют в отчете.",
            "Добавлена проверка судов общей юрисдикции / ГАС Правосудие.",
            "Клиентский /check-report очищает служебные поля newDB, debug показывает больше технической информации.",
        ],
    }


@app.get("/health")
def health():
    return {"ok": True, "version": "7.0.0-prod-pravosud-pdf", "newdb_configured": bool(NEWDB_TOKEN)}


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    newdb = await run_newdb_checks(req, debug=True)
    result = build_response(req, newdb, debug=True)
    result["normalized_input"] = {
        "last": clean_text(req.last), "first": clean_text(req.first), "middle": clean_text(req.middle),
        "dob": normalize_dob_ru(req.dob), "dob_iso": normalize_dob_iso(req.dob), "inn": seller_inn(req),
        "region": normalize_region(req.region), "passport_series": only_digits(req.passport_series or req.passport_seria),
        "passport_number": only_digits(req.passport_number), "property": normalize_property(req),
    }
    return result


@app.post("/check-report")
async def check_report(req: CheckRequest):
    try:
        newdb = await run_newdb_checks(req, debug=False)
        result = build_response(req, newdb, debug=False)
        report_id = str(uuid.uuid4())
        pdf_available = False
        pdf_base64 = ""
        pdf_url = f"/download-pdf/{report_id}"
        try:
            pdf_path = create_pdf(report_id, req, result["checklist"], result["registry_data"], result["risk_scoring"], result["recommendations"], result["legal_report"])
            pdf_available = True
            try:
                pdf_base64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
            except Exception:
                pdf_base64 = ""
        except Exception as e:
            result.setdefault("warnings", []).append(f"PDF временно не сформирован: {e}")
        REPORT_CACHE[report_id] = {"created_at": datetime.now().isoformat(), "result": result}
        result.update({"report_id": report_id, "pdf_available": pdf_available, "pdf_url": pdf_url, "pdf_base64": pdf_base64})
        # Не показываем технические payloads/responses клиенту.
        result.pop("payloads", None)
        result.pop("responses", None)
        return result
    except Exception:
        return {"success": False, "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.", "warnings": ["Техническая ошибка скрыта от пользователя и не влияет на юридический вывод."]}


@app.get("/download-pdf/{report_id}")
def download_pdf(report_id: str):
    path = REPORT_DIR / f"{report_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF отчет не найден или уже удален.")
    return FileResponse(str(path), media_type="application/pdf", filename=f"legal_report_{report_id}.pdf")
