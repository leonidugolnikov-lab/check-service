
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from pathlib import Path
import asyncio
import base64
import html
import json
import os
import re
import tempfile
import traceback
import uuid

import httpx

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None


# ============================================================
#  Real Estate Legal Check API
#  Боевой main.py + безопасный check-report + диагностический debug-newdb
# ============================================================

app = FastAPI(title="Real Estate Legal Check API", version="5.0.0")

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
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
GIGACHAT_VERIFY_SSL_CERTS = os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "false").lower() in {"1", "true", "yes"}

REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports"))
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)

DEFAULT_MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "pledges": "https://www.reestr-zalogov.ru/search",
    "courts": "https://kad.arbitr.ru",
    "egrn": "https://rosreestr.gov.ru",
}

DISCLAIMER = (
    "Отчет носит информационно-аналитический характер, не является гарантией полной "
    "юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом."
)

CLIENT_MANUAL_TEXT = "Источник не вернул данные. Требуется ручная проверка."


class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int | str = 78
    passport_series: str = ""
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
        # Исправляет частую mojibake-кодировку, если она реально есть.
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
    # Для регионов с ведущим нулем оставляем строку.
    if re.fullmatch(r"0\d", s):
        return s
    try:
        return int(s)
    except Exception:
        return s


def rub(value: Any) -> str:
    try:
        n = float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        n = 0.0
    if abs(n - int(n)) < 0.005:
        return f"{int(n):,}".replace(",", " ") + " ₽"
    return f"{n:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


def safe_json(obj: Any, limit: int = 12000) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = str(obj)
    return text[:limit]


def text_blob(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()


def sanitize_for_client(obj: Any) -> Any:
    """Убирает то, что нельзя показывать клиенту в обычном отчете/PDF."""
    forbidden_keys = {
        "token", "balance", "api_key", "x-api-key", "authorization",
        "requestid", "request_id", "taskid", "task_id", "newdb_qid",
        "sent_params", "params", "webhook", "is_repeat", "tasks",
        "datecreated", "dateupdated", "errors_info", "docs_url",
        "http_status", "raw_text"
    }
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in forbidden_keys:
                continue
            out[k] = sanitize_for_client(v)
        return out
    if isinstance(obj, list):
        return [sanitize_for_client(x) for x in obj]
    return obj


def sanitize_debug(obj: Any) -> Any:
    """Диагностика: оставляем requestId/state/results/errors, но убираем токен и баланс."""
    forbidden_keys = {"token", "balance", "api_key", "x-api-key", "authorization"}
    if isinstance(obj, dict):
        return {k: sanitize_debug(v) for k, v in obj.items() if str(k).lower() not in forbidden_keys}
    if isinstance(obj, list):
        return [sanitize_debug(x) for x in obj]
    return obj


def flatten_strings(obj: Any, limit: int = 60) -> List[str]:
    out: List[str] = []
    skip = {"ru", "complete", "done", "success", "ok", "none", "null", "true", "false"}

    def walk(x: Any):
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in {"params", "requestid", "request_id", "token", "balance", "taskid", "newdb_qid"}:
                    continue
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
        else:
            s = clean_text(x)
            if not s or s.lower() in skip:
                return
            if len(s) > 400:
                s = s[:400] + "..."
            if s not in out:
                out.append(s)

    walk(obj)
    return out


def get_state(data: Dict[str, Any]) -> str:
    return clean_text(data.get("state") or data.get("status") or "").lower()


def is_complete(data: Dict[str, Any]) -> bool:
    st = get_state(data)
    if st in {"complete", "completed", "done", "success", "finished", "ready", "ok"}:
        return True
    # NewDB examples use finished=1
    if data.get("finished") == 1 or data.get("finished") is True:
        return True
    return False


def is_pending(data: Dict[str, Any]) -> bool:
    st = get_state(data)
    if st in {"queued", "queue", "in progress", "progress", "pending", "processing", "wait", "waiting"}:
        return True
    # Если есть requestId, но results еще нет — считаем задачей в обработке.
    if (data.get("requestId") or data.get("request_id")) and not data.get("results") and not data.get("errors_info"):
        return True
    return False


def has_api_error(data: Any) -> bool:
    if not isinstance(data, dict):
        return True
    txt = text_blob(data)
    if data.get("errors_info"):
        return True
    if data.get("error") or data.get("detail"):
        return True
    if "method or country is not valid" in txt:
        return True
    if "is not valid" in txt and ("inn" in txt or "method" in txt or "country" in txt):
        return True
    if "не хватает обязательного" in txt or "missing required" in txt or "required" in txt:
        return True
    if "unauthorized" in txt or "forbidden" in txt or "x-api-key" in txt:
        return True
    if "not enough balance" in txt or "insufficient balance" in txt:
        return True
    return False


def get_result_data(data: Dict[str, Any], method: str) -> Tuple[Optional[int], List[Any], Dict[str, Any]]:
    """
    Возвращает (status, data_list, method_block)
    Ожидаемый путь NewDB: results.<method>.result.status и results.<method>.result.data
    """
    if not isinstance(data, dict):
        return None, [], {}
    results = data.get("results")
    if not isinstance(results, dict):
        return None, [], {}
    block = results.get(method)
    if not isinstance(block, dict):
        # Иногда ключ отличается регистром/вариантом — попробуем первый блок.
        for _, b in results.items():
            if isinstance(b, dict) and isinstance(b.get("result"), dict):
                block = b
                break
    if not isinstance(block, dict):
        return None, [], {}
    result = block.get("result")
    if not isinstance(result, dict):
        return None, [], block
    status = result.get("status")
    arr = result.get("data")
    if isinstance(arr, list):
        return status, arr, block
    if isinstance(arr, dict):
        return status, [arr], block
    return status, [], block


def extract_error_reason(data: Any) -> str:
    if not isinstance(data, dict):
        return CLIENT_MANUAL_TEXT
    txts = []
    errors = data.get("errors_info")
    if isinstance(errors, list):
        for e in errors[:3]:
            if isinstance(e, dict):
                err = clean_text(e.get("error") or e.get("message") or e.get("description"))
                if err:
                    txts.append(err)
    for k in ("error", "message", "detail", "description"):
        v = clean_text(data.get(k))
        if v:
            txts.append(v)
    txt = text_blob(data)
    if "method or country is not valid" in txt:
        txts.append("NewDB не принял метод или страну. Проверьте название метода и параметры.")
    if "missing" in txt or "required" in txt or "обязатель" in txt:
        txts.append("NewDB не принял запрос: не хватает обязательного параметра.")
    return txts[0] if txts else CLIENT_MANUAL_TEXT


def checklist_item(source: str, status: str, summary: str, details: Optional[List[str]] = None, manual_url: str = "") -> Dict[str, Any]:
    # Совместимость: frontend может ждать item.status manual, а ТЗ — manual_check.
    frontend_status = "manual" if status == "manual_check" else status
    return {
        "source": source,
        "title": source,
        "status": frontend_status,
        "status_code": status,
        "summary": summary,
        "details": details or [],
        "manual_url": manual_url,
        "manual_check_url": manual_url,
    }


def normalize_property(req: CheckRequest) -> Dict[str, str]:
    cad = clean_text(req.cadastral_number or req.cadastre_number)
    addr = clean_text(req.address)
    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"
    if cad and re.fullmatch(cad_pattern, cad):
        return {"query": cad, "address": cad, "cadastral_number": cad, "type": "cadastral"}
    if addr and re.fullmatch(cad_pattern, addr):
        return {"query": addr, "address": addr, "cadastral_number": addr, "type": "cadastral"}
    if addr:
        return {"query": addr, "address": addr, "cadastral_number": "", "type": "address"}
    return {"query": "", "address": "", "cadastral_number": "", "type": ""}


def make_newdb_request_id(prefix: str = "") -> str:
    # requestId должен быть стабильным в рамках одного метода, но уникальным для нового запуска.
    return str(uuid.uuid4())


async def newdb_request(params: Dict[str, Any], max_wait: int = 120, poll_interval: int = 5, debug: bool = False) -> Dict[str, Any]:
    """
    Важно: NewDB принимает requestId на верхнем уровне, а не внутри params.
    При повторном POST с тем же requestId возвращает старый/готовый результат.
    """
    if not NEWDB_TOKEN:
        return {"state": "error", "error": "NEWDB_TOKEN не задан в Environment.", "params": params}

    request_id = make_newdb_request_id(params.get("method", "check"))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        payload = {"params": params, "requestId": request_id}
        try:
            r = await client.post(NEWDB_URL, headers=headers, json=payload)
            try:
                data = r.json()
            except Exception:
                data = {"raw_text": r.text}
            data["_http_status"] = r.status_code
            if r.status_code >= 400:
                data["state"] = data.get("state") or "error"
                return data
        except Exception as e:
            return {"state": "error", "error": f"Ошибка запроса NewDB: {e}", "params": params}

        if has_api_error(data):
            return data
        if is_complete(data):
            return data

        # Даже если state не complete, но results уже есть — возвращаем, классификаторы сами проверят data.
        if isinstance(data.get("results"), dict):
            return data

        elapsed = 0
        last = data
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                poll_payload = {"params": params, "requestId": request_id}
                pr = await client.post(NEWDB_URL, headers=headers, json=poll_payload)
                try:
                    polled = pr.json()
                except Exception:
                    polled = {"raw_text": pr.text}
                polled["_http_status"] = pr.status_code
                last = polled

                if pr.status_code >= 400:
                    continue
                if has_api_error(polled):
                    return polled
                if is_complete(polled) or isinstance(polled.get("results"), dict):
                    return polled
            except Exception as e:
                last = {"state": "error", "error": f"Ошибка polling NewDB: {e}", "params": params}
                continue

        last["state"] = "timeout"
        last["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд."
        return last


def build_newdb_payloads(req: CheckRequest) -> Dict[str, Optional[Dict[str, Any]]]:
    last = clean_text(req.last)
    first = clean_text(req.first)
    middle = clean_text(req.middle)
    dob_iso = normalize_dob_iso(req.dob)
    inn = only_digits(req.inn)
    region = normalize_region(req.region)
    prop = normalize_property(req)

    passport_series = only_digits(req.passport_series)
    passport_number = only_digits(req.passport_number)

    payloads: Dict[str, Optional[Dict[str, Any]]] = {}

    if passport_series and passport_number and last and first and dob_iso:
        payloads["passport"] = {
            "seria": passport_series,
            "number": passport_number,
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
            "method": os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd"),
        }
    else:
        payloads["passport"] = None

    if last and first and dob_iso:
        payloads["fssp"] = {
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
            "regioncode": region,
            "country": "ru",
            "method": os.getenv("NEWDB_METHOD_FSSP", "fssp_person"),
        }
    else:
        payloads["fssp"] = None

    if len(inn) == 12:
        payloads["bankruptcy"] = {
            "innfiz": inn,
            "country": "ru",
            "method": os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankrot_person"),
        }
        payloads["courts"] = {
            "innfiz": inn,
            "country": "ru",
            "method": os.getenv("NEWDB_METHOD_COURTS", "arbitr_person"),
        }
    else:
        payloads["bankruptcy"] = None
        payloads["courts"] = None

    # pledge_person умеет искать по ФИО + dob и/или ИНН; не отправляем 12-значный inn как innyur.
    if last and first:
        payloads["pledges"] = {
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "country": "ru",
            "method": os.getenv("NEWDB_METHOD_PLEDGES", "pledge_person"),
        }
        if dob_iso:
            payloads["pledges"]["dob"] = dob_iso
        if len(inn) == 12:
            payloads["pledges"]["innfiz"] = inn
    else:
        payloads["pledges"] = None

    if prop["address"]:
        payloads["egrn"] = {
            "address": prop["address"],
            "country": "ru",
            "method": os.getenv("NEWDB_METHOD_EGRN", "rosreestr"),
        }
    else:
        payloads["egrn"] = None

    return payloads


async def run_newdb_checks(req: CheckRequest, debug: bool = False) -> Dict[str, Any]:
    payloads = build_newdb_payloads(req)

    async def call(name: str, params: Optional[Dict[str, Any]], wait: int):
        if not params:
            return name, {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
        data = await newdb_request(params, max_wait=wait, poll_interval=5, debug=debug)
        return name, data

    tasks = [
        call("passport", payloads["passport"], 90),
        call("fssp", payloads["fssp"], 150),
        call("bankruptcy", payloads["bankruptcy"], 120),
        call("pledges", payloads["pledges"], 120),
        call("courts", payloads["courts"], 120),
        call("egrn", payloads["egrn"], 360),
    ]
    results = {}
    for name, data in await asyncio.gather(*tasks):
        results[name] = data
    return {"payloads": payloads, "responses": results}


def amount_from_item(item: Any) -> float:
    text = text_blob(item)
    # Сначала структурные ключи.
    if isinstance(item, dict):
        keys = ["amount", "sum", "debt", "debt_sum", "debtSum", "total", "balance", "residual", "remaining"]
        for k in keys:
            if k in item:
                try:
                    return float(str(item[k]).replace(" ", "").replace(",", "."))
                except Exception:
                    pass
    vals = []
    for m in re.findall(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:руб|₽)", text, flags=re.I):
        try:
            vals.append(float(m.replace(" ", "").replace(",", ".")))
        except Exception:
            pass
    return max(vals) if vals else 0.0


def classify_passport(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "Паспорт МВД"
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT}

    status, data, _ = get_result_data(raw, "passport_mvd")
    if status != 200 or not isinstance(data, list):
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ источника не содержит результата проверки паспорта."], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT}

    txt = text_blob(data)
    if not data:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Источник вернул пустой результат."], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT}
    if any(x in txt for x in ["недейств", "invalid", "разыскивается"]):
        return checklist_item(source, "risk", "Выявлены признаки проблемы с паспортом.", flatten_strings(data, 8), DEFAULT_MANUAL_LINKS["passport"]), {"summary": "Выявлены признаки проблемы с паспортом.", "items": sanitize_for_client(data)}
    if any(x in txt for x in ["действител", "valid"]):
        return checklist_item(source, "ok", "Паспорт по полученным данным действителен.", [], DEFAULT_MANUAL_LINKS["passport"]), {"summary": "Паспорт по полученным данным действителен.", "items": sanitize_for_client(data)}

    return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ источника нельзя уверенно интерпретировать автоматически."], DEFAULT_MANUAL_LINKS["passport"]), {"summary": CLIENT_MANUAL_TEXT, "items": sanitize_for_client(data)}


def classify_fssp(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "ФССП"
    empty_stats = {
        "all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0,
        "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "actual_debt": 0.0,
        "active_items": [], "closed_items": [], "unknown_items": []
    }
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["fssp"]), {"summary": CLIENT_MANUAL_TEXT, "stats": empty_stats}

    status, data, _ = get_result_data(raw, "fssp_person")
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ ФССП не содержит успешного результата."], DEFAULT_MANUAL_LINKS["fssp"]), {"summary": CLIENT_MANUAL_TEXT, "stats": empty_stats}
    if not data:
        return checklist_item(source, "ok", "По полученным данным исполнительные производства не найдены.", [], DEFAULT_MANUAL_LINKS["fssp"]), {"summary": "По полученным данным исполнительные производства не найдены.", "stats": empty_stats}

    # У NewDB data[] может содержать список или вложенные списки. Расплющим осторожно.
    items: List[Any] = []

    def collect(x: Any):
        if isinstance(x, list):
            for i in x:
                collect(i)
        elif isinstance(x, dict):
            # Если словарь содержит список производств под разными ключами.
            list_keys = ["items", "data", "records", "executions", "proceedings", "fssp", "result"]
            expanded = False
            for k in list_keys:
                if isinstance(x.get(k), list):
                    collect(x[k])
                    expanded = True
            if not expanded:
                items.append(x)
        elif x:
            items.append({"value": x})

    collect(data)

    if not items:
        return checklist_item(source, "ok", "По полученным данным исполнительные производства не найдены.", [], DEFAULT_MANUAL_LINKS["fssp"]), {"summary": "По полученным данным исполнительные производства не найдены.", "stats": empty_stats}

    closed_words = [
        "оконч", "прекращ", "закрыт", "заверш", "исполнено", "ст. 46", "статья 46",
        "terminated", "closed", "complete", "completed"
    ]
    active_words = [
        "актив", "возбужден", "взыскание", "остаток", "задолженность", "действующ",
        "active", "open", "opened"
    ]

    active, closed, unknown = [], [], []
    for item in items:
        t = text_blob(item)
        if any(w in t for w in closed_words):
            closed.append(item)
        elif any(w in t for w in active_words):
            active.append(item)
        else:
            # Если есть сумма/предмет взыскания, но нет признака закрытия — лучше считать активным риском, а не терять.
            if amount_from_item(item) > 0:
                active.append(item)
            else:
                unknown.append(item)

    active_sum = sum(amount_from_item(x) for x in active)
    closed_sum = sum(amount_from_item(x) for x in closed)
    unknown_sum = sum(amount_from_item(x) for x in unknown)
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
        "active_items": sanitize_for_client(active),
        "closed_items": sanitize_for_client(closed),
        "unknown_items": sanitize_for_client(unknown),
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

    if active:
        summary = f"Найдены активные исполнительные производства. Актуальная сумма долга: {rub(active_sum)}."
        return checklist_item(source, "risk", summary, details, DEFAULT_MANUAL_LINKS["fssp"]), {"summary": summary, "stats": stats}

    if closed and not active and not unknown:
        summary = f"Найдены исполнительные производства. Активных: 0, закрытых: {len(closed)}. Актуальный долг по активным производствам: 0 ₽."
        return checklist_item(source, "ok", summary, details, DEFAULT_MANUAL_LINKS["fssp"]), {"summary": summary, "stats": stats}

    summary = "Найдены исполнительные производства с неоднозначным статусом. Требуется ручная проверка."
    return checklist_item(source, "manual_check", summary, details, DEFAULT_MANUAL_LINKS["fssp"]), {"summary": summary, "stats": stats}


def classify_simple_records(raw: Dict[str, Any], method: str, source: str, ok_text: str, risk_text: str, manual_url: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], manual_url), {"summary": CLIENT_MANUAL_TEXT}

    status, data, _ = get_result_data(raw, method)
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ источника не содержит успешного результата."], manual_url), {"summary": CLIENT_MANUAL_TEXT}
    if not data:
        return checklist_item(source, "ok", ok_text, [], manual_url), {"summary": ok_text, "items": []}

    # Для bankrot_person в data[0] может быть bankruptcy: []
    meaningful = []
    for item in data:
        if isinstance(item, dict):
            if "bankruptcy" in item and isinstance(item.get("bankruptcy"), list):
                meaningful.extend(item.get("bankruptcy") or [])
            elif method == "pledge_person":
                # Есть fnp/fedresurs списки
                for k in ("fnp", "fedresurs", "items", "records"):
                    if isinstance(item.get(k), list):
                        meaningful.extend(item[k])
                if not any(isinstance(item.get(k), list) for k in ("fnp", "fedresurs", "items", "records")):
                    meaningful.append(item)
            else:
                meaningful.append(item)
        else:
            meaningful.append(item)

    if not meaningful:
        return checklist_item(source, "ok", ok_text, [], manual_url), {"summary": ok_text, "items": []}

    return checklist_item(source, "risk", risk_text, flatten_strings(meaningful, 10), manual_url), {"summary": risk_text, "items": sanitize_for_client(meaningful)}


def classify_egrn(raw: Dict[str, Any], prop: Dict[str, str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = "ЕГРН / Росреестр"
    if not prop.get("query"):
        return checklist_item(source, "manual_check", "Не передан адрес или кадастровый номер объекта. Требуется ручная проверка.", [], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}

    if has_api_error(raw) or get_state(raw) in {"error", "timeout", "skipped"}:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, [extract_error_reason(raw)], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}

    status, data, _ = get_result_data(raw, "rosreestr")
    if status != 200:
        return checklist_item(source, "manual_check", CLIENT_MANUAL_TEXT, ["Ответ Росреестра не содержит успешного результата."], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}

    if not data:
        return checklist_item(source, "manual_check", "Объект не найден автоматически или источник вернул пустой результат. Требуется ручная проверка.", [f"Запрос: {prop.get('query')}"], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT}

    obj = data[0] if isinstance(data[0], dict) else {"value": data[0]}
    # Настоящие данные Росреестра должны содержать хотя бы cadNumber/rights/encumbrances/address/area/regDate.
    meaningful_keys = {"cadNumber", "cad_number", "rights", "encumbrances", "address", "area", "regDate", "objType"}
    if not any(k in obj for k in meaningful_keys):
        return checklist_item(source, "manual_check", "Росреестр не вернул структурированные данные объекта. Требуется ручная проверка.", [f"Запрос: {prop.get('query')}"], DEFAULT_MANUAL_LINKS["egrn"]), {"summary": CLIENT_MANUAL_TEXT, "object": sanitize_for_client(obj)}

    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    enc = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    # Иногда обременения лежат под другими названиями.
    for k in ("restrictions", "limitations", "burdens", "encumbrance"):
        if isinstance(obj.get(k), list):
            enc.extend(obj[k])

    txt = text_blob(obj)
    risk_words = [
        "ипотек", "залог", "арест", "запрет", "огранич", "обремен", "рента",
        "запрещение", "регистрацион", "прочие ограничения"
    ]
    has_risk = bool(enc) or any(w in txt for w in risk_words)

    details = []
    cad = clean_text(obj.get("cadNumber") or obj.get("cad_number") or prop.get("cadastral_number"))
    if cad:
        details.append(f"Кадастровый номер: {cad}")
    area = clean_text(obj.get("area"))
    if area:
        details.append(f"Площадь: {area}")
    reg_date = clean_text(obj.get("regDate"))
    if reg_date:
        details.append(f"Дата постановки/регистрации: {reg_date}")
    if rights:
        details.append(f"Записей о правах: {len(rights)}")
    if enc:
        details.append(f"Записей об ограничениях/обременениях: {len(enc)}")
        details.extend(flatten_strings(enc, 8))

    if has_risk:
        summary = "Данные по объекту получены. Выявлены признаки ограничений, обременений или иных рисков по объекту."
        return checklist_item(source, "risk", summary, details, DEFAULT_MANUAL_LINKS["egrn"]), {"summary": summary, "object": sanitize_for_client(obj), "rights": sanitize_for_client(rights), "encumbrances": sanitize_for_client(enc)}

    summary = "Данные по объекту получены. По полученным данным явные признаки ограничений или обременений не выявлены."
    return checklist_item(source, "ok", summary, details, DEFAULT_MANUAL_LINKS["egrn"]), {"summary": summary, "object": sanitize_for_client(obj), "rights": sanitize_for_client(rights), "encumbrances": []}


def classify_all(req: CheckRequest, newdb: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
    raw = newdb["responses"]
    prop = normalize_property(req)
    warnings: List[str] = []

    passport_item, passport_data = classify_passport(raw.get("passport", {}))
    fssp_item, fssp_data = classify_fssp(raw.get("fssp", {}))
    bank_item, bank_data = classify_simple_records(
        raw.get("bankruptcy", {}),
        "bankrot_person",
        "Банкротство / Федресурс",
        "По полученным данным сведения о банкротстве не выявлены.",
        "Выявлены сведения, связанные с банкротством.",
        DEFAULT_MANUAL_LINKS["bankruptcy"],
    )
    pledge_item, pledge_data = classify_simple_records(
        raw.get("pledges", {}),
        "pledge_person",
        "Залоги движимого имущества",
        "По полученным данным сведения о залогах движимого имущества не выявлены.",
        "Выявлены сведения о залогах или обременениях по физлицу.",
        DEFAULT_MANUAL_LINKS["pledges"],
    )
    court_item, court_data = classify_simple_records(
        raw.get("courts", {}),
        "arbitr_person",
        "Суды / арбитраж",
        "По полученным данным судебные дела не выявлены.",
        "Найдены судебные производства. Требуется анализ предмета спора.",
        DEFAULT_MANUAL_LINKS["courts"],
    )
    egrn_item, egrn_data = classify_egrn(raw.get("egrn", {}), prop)

    checklist = [passport_item, fssp_item, bank_item, pledge_item, court_item, egrn_item]
    registry_data = {
        "passport": {"title": "Паспорт МВД", **passport_data},
        "fssp": {"title": "ФССП", **fssp_data},
        "bankruptcy": {"title": "Банкротство / Федресурс", **bank_data},
        "pledges": {"title": "Залоги движимого имущества", **pledge_data},
        "courts": {"title": "Суды / арбитраж", **court_data},
        "egrn": {"title": "ЕГРН / Росреестр", **egrn_data},
    }
    return checklist, registry_data, warnings


def build_structured_payload_for_ai(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    prop = normalize_property(req)
    return {
        "seller": {
            "last": clean_text(req.last),
            "first": clean_text(req.first),
            "middle": clean_text(req.middle),
            "dob": normalize_dob_ru(req.dob),
            "inn_provided": bool(only_digits(req.inn)),
            "passport_provided": bool(only_digits(req.passport_series) and only_digits(req.passport_number)),
        },
        "property": {
            "query": prop.get("query"),
            "cadastral_number": prop.get("cadastral_number"),
            "address": prop.get("address") if prop.get("type") == "address" else "",
        },
        "checklist": checklist,
        "registry_data": registry_data,
        "warnings": warnings,
        "not_checked": [x["source"] for x in checklist if x.get("status_code") == "manual_check" or x.get("status") == "manual"],
    }


def fallback_legal_report(payload: Dict[str, Any]) -> str:
    checklist = payload["checklist"]
    seller = payload["seller"]
    prop = payload["property"]

    risks = [x for x in checklist if x.get("status") == "risk"]
    manuals = [x for x in checklist if x.get("status") == "manual"]
    oks = [x for x in checklist if x.get("status") == "ok"]

    if risks:
        lead = "Предварительный вывод: выявлены признаки риска, сделку нельзя выводить на аванс без ручной юридической проверки."
    elif manuals:
        lead = "Предварительный вывод: часть источников не вернула данные, требуется ручная проверка до аванса."
    else:
        lead = "Предварительный вывод: по автоматически полученным данным явные риски не выявлены, но отчет не заменяет ручную проверку документов."

    fio = " ".join([seller.get("last", ""), seller.get("first", ""), seller.get("middle", "")]).strip() or "по предоставленным данным не указано"
    obj = prop.get("cadastral_number") or prop.get("address") or prop.get("query") or "по предоставленным данным не указан"

    lines = [
        "1. Краткий вывод",
        lead,
        "",
        "2. Что проверено",
        f"Продавец: {fio}.",
        f"Дата рождения: {seller.get('dob') or 'по предоставленным данным не указана'}.",
        f"Объект: {obj}.",
        f"Проверок без явных рисков: {len(oks)}. Проверок с рисками: {len(risks)}. Требуют ручной проверки: {len(manuals)}.",
        "",
        "3. Риски по продавцу",
    ]

    seller_sources = {"Паспорт МВД", "ФССП", "Банкротство / Федресурс", "Залоги движимого имущества", "Суды / арбитраж"}
    seller_items = [x for x in checklist if x["source"] in seller_sources and x["status"] != "ok"]
    lines.extend([f"- {x['source']}: {x['summary']}" for x in seller_items] or ["По автоматически полученным данным явные риски по продавцу не выявлены."])

    lines += ["", "4. Риски по объекту"]
    obj_items = [x for x in checklist if x["source"] == "ЕГРН / Росреестр"]
    lines.extend([f"- {x['source']}: {x['summary']}" for x in obj_items] or ["По предоставленным данным объект не проверялся."])

    lines += [
        "",
        "5. Что говорит в пользу сделки",
        "В пользу сделки говорят только пункты со статусом «проверено» и реальным ответом источника. Отсутствие ответа источника не считается отсутствием риска.",
        "",
        "6. Что обязательно проверить до аванса",
    ]
    for x in manuals:
        lines.append(f"- {x['source']}: требуется ручная проверка. {x.get('manual_url') or x.get('manual_check_url') or ''}".strip())

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


async def call_gigachat(payload: Dict[str, Any]) -> str:
    # GigaChat не должен ломать весь сервис. Если библиотека/ключи не настроены — fallback.
    if not (GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)):
        return fallback_legal_report(payload)

    prompt = (
        "Ты юрист-эксперт по недвижимости в Санкт-Петербурге.\n\n"
        "На основе переданных структурированных данных сформируй подробный юридический отчет для покупателя недвижимости.\n\n"
        "Строго соблюдай структуру:\n"
        "1. Краткий вывод\n"
        "2. Что проверено\n"
        "3. Риски по продавцу\n"
        "4. Риски по объекту\n"
        "5. Что говорит в пользу сделки\n"
        "6. Что обязательно проверить до аванса\n"
        "7. Что прописать в авансовом соглашении / ПДКП\n"
        "8. Безопасная схема расчетов\n"
        "9. Итоговое заключение\n\n"
        "Не придумывай факты.\n"
        "Если данных нет — прямо пиши: “по предоставленным данным не проверялось”.\n"
        "Если источник не ответил — пиши: “требуется ручная проверка”.\n"
        "Не обещай 100% безопасность.\n"
        "Не называй объект юридически чистым.\n"
        "Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.\n\n"
        "СТРУКТУРИРОВАННЫЕ ДАННЫЕ:\n"
        f"{safe_json(payload, 20000)}"
    )

    try:
        from gigachat import GigaChat
        credentials = GIGACHAT_CREDENTIALS
        # Библиотека GigaChat чаще ожидает credentials (base64 client_id:client_secret).
        if not credentials and GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET:
            credentials = base64.b64encode(f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode()).decode()

        def _sync_call():
            with GigaChat(credentials=credentials, scope=GIGACHAT_SCOPE, model=GIGACHAT_MODEL, verify_ssl_certs=GIGACHAT_VERIFY_SSL_CERTS) as giga:
                res = giga.chat(prompt)
                return getattr(res.choices[0].message, "content", "") or ""

        text = await asyncio.to_thread(_sync_call)
        return text.strip() or fallback_legal_report(payload)
    except Exception:
        return fallback_legal_report(payload)


def make_pdf_html_text(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], legal_report: str) -> List[Tuple[str, str]]:
    seller_fio = " ".join([clean_text(req.last), clean_text(req.first), clean_text(req.middle)]).strip()
    prop = normalize_property(req)

    sections: List[Tuple[str, str]] = []
    sections.append(("Юридический отчет по проверке продавца и объекта недвижимости", f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}"))
    sections.append(("1. Данные продавца", f"ФИО: {seller_fio or 'не указано'}\nДата рождения: {normalize_dob_ru(req.dob) or 'не указана'}\nИНН передан: {'да' if only_digits(req.inn) else 'нет'}"))
    sections.append(("2. Данные объекта", f"Запрос: {prop.get('query') or 'не указан'}\nКадастровый номер: {prop.get('cadastral_number') or 'не указан'}"))

    cl_lines = []
    for item in checklist:
        status_text = {"ok": "Проверено", "risk": "Риск", "manual": "Требуется ручная проверка"}.get(item.get("status"), item.get("status", ""))
        cl_lines.append(f"{item['source']}: {status_text}. {item['summary']}")
        for d in item.get("details", [])[:8]:
            cl_lines.append(f"• {d}")
    sections.append(("3. Чек-лист проверок", "\n".join(cl_lines)))

    reg_lines = []
    for key in ["passport", "fssp", "bankruptcy", "pledges", "courts", "egrn"]:
        block = registry_data.get(key, {})
        reg_lines.append(f"{block.get('title', key)}: {block.get('summary', CLIENT_MANUAL_TEXT)}")
        # Показываем важные структурные данные, но без мусора newDB.
        if key == "fssp" and block.get("stats"):
            reg_lines.append(safe_json(block["stats"], 5000))
        if key == "egrn" and block.get("object"):
            short_obj = block.get("object", {})
            if isinstance(short_obj, dict):
                selected = {k: short_obj.get(k) for k in ["cadNumber", "area", "regDate", "rights", "encumbrances"] if k in short_obj}
                reg_lines.append(safe_json(selected or short_obj, 8000))
        elif block.get("items"):
            reg_lines.append(safe_json(block.get("items"), 5000))
    sections.append(("4. Данные, полученные из реестров", "\n".join(reg_lines)))
    sections.append(("5. Юридический отчет", legal_report))
    sections.append(("Дисклеймер", DISCLAIMER))
    return sections


def create_pdf(report_id: str, req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], legal_report: str) -> Path:
    pdf_path = REPORT_DIR / f"{report_id}.pdf"

    if SimpleDocTemplate is None:
        # Простая заглушка, если reportlab не установился.
        pdf_path.write_bytes(b"%PDF-1.4\n% ReportLab is not installed\n%%EOF")
        return pdf_path

    # Попытка подключить кириллический системный шрифт.
    font_name = "Helvetica"
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for fp in possible_fonts:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont("AppFont", fp))
                font_name = "AppFont"
                break
            except Exception:
                pass

    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=42)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleRu", parent=styles["Title"], fontName=font_name, fontSize=18, leading=22, spaceAfter=14)
    h_style = ParagraphStyle("HeadingRu", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=16, spaceBefore=10, spaceAfter=6)
    p_style = ParagraphStyle("BodyRu", parent=styles["BodyText"], fontName=font_name, fontSize=9.5, leading=13, spaceAfter=6)

    story = []
    sections = make_pdf_html_text(req, checklist, registry_data, legal_report)
    for idx, (heading, body) in enumerate(sections):
        story.append(Paragraph(html.escape(heading), title_style if idx == 0 else h_style))
        for paragraph in str(body).split("\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                story.append(Spacer(1, 5))
            else:
                story.append(Paragraph(html.escape(paragraph), p_style))
        story.append(Spacer(1, 8))
    doc.build(story)
    return pdf_path


def pdf_base64(path: Path) -> str:
    try:
        return base64.b64encode(path.read_bytes()).decode()
    except Exception:
        return ""


def save_report_json(report_id: str, data: Dict[str, Any]) -> None:
    path = REPORT_DIR / f"{report_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "Real Estate Legal Check API",
        "version": "5.0.0",
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)),
        "time": datetime.now().isoformat(),
    }


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    """
    Диагностический endpoint. Не для клиентов.
    Возвращает payloads и ответы NewDB без токена/баланса.
    НЕ вызывает GigaChat и НЕ формирует PDF.
    """
    try:
        payloads = build_newdb_payloads(req)
        newdb = await run_newdb_checks(req, debug=True)
        return {
            "success": True,
            "payloads": sanitize_debug(payloads),
            "responses": sanitize_debug(newdb["responses"]),
            "notes": [
                "Если source содержит errors_info — проблема в параметрах/методе NewDB.",
                "Если state=complete и results.<method>.result.data пустой список — источник ответил, записей нет.",
                "Если по Росреестру нет results.rosreestr.result.data — это не успешная проверка объекта.",
            ],
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "trace": traceback.format_exc(),
            },
        )


@app.post("/check-report")
async def check_report(req: CheckRequest):
    """
    Клиентский endpoint. Не показывает техническую ошибку, но не падает из-за GigaChat/PDF.
    """
    warnings: List[str] = []
    try:
        newdb = await run_newdb_checks(req, debug=False)
        checklist, registry_data, classify_warnings = classify_all(req, newdb)
        warnings.extend(classify_warnings)

        ai_payload = build_structured_payload_for_ai(req, checklist, registry_data, warnings)

        try:
            legal_report = await call_gigachat(ai_payload)
        except Exception:
            legal_report = fallback_legal_report(ai_payload)
            warnings.append("AI-отчет сформирован резервным способом.")

        report_id = str(uuid.uuid4())
        pdf_available = False
        pdf_url = f"/download-pdf/{report_id}"
        pdf_b64 = ""

        try:
            pdf_path = create_pdf(report_id, req, checklist, registry_data, legal_report)
            pdf_available = pdf_path.exists() and pdf_path.stat().st_size > 20
            pdf_b64 = pdf_base64(pdf_path) if pdf_available else ""
        except Exception:
            warnings.append("PDF временно недоступен. Основной отчет сформирован.")

        result = {
            "success": True,
            "report_id": report_id,
            "checklist": checklist,
            "registry_data": registry_data,
            "legal_report": legal_report,
            "pdf_available": pdf_available,
            "pdf_url": pdf_url if pdf_available else "",
            "pdf_base64": pdf_b64,  # Совместимость со старым виджетом.
            "warnings": warnings,
            "disclaimer": DISCLAIMER,
        }

        # Сохраняем только безопасный клиентский результат.
        try:
            save_report_json(report_id, {k: v for k, v in result.items() if k != "pdf_base64"})
        except Exception:
            pass

        return result

    except Exception:
        # Клиенту — безопасно, но debug-newdb покажет настоящую ошибку.
        return {
            "success": False,
            "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.",
            "warnings": ["Техническая ошибка скрыта от пользователя. Для диагностики используйте /debug-newdb."],
        }


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    # Защита от path traversal.
    if not re.fullmatch(r"[a-f0-9-]{20,80}", report_id):
        raise HTTPException(status_code=400, detail="Некорректный report_id")

    pdf_path = REPORT_DIR / f"{report_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF не найден или срок хранения истек")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"otchet_{datetime.now().strftime('%Y-%m-%d')}.pdf",
    )


@app.get("/")
async def root():
    return {"ok": True, "docs": "/docs", "health": "/health"}
