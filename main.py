
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import asyncio
import base64
import html
import json
import os
import re
import tempfile
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(title="Real Estate Seller & Property Check API", version="5.0-final")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# ENV
# ============================================================

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip().rstrip("/")

# По умолчанию методы NEWDB из актуальной документации.
NEWDB_METHOD_PASSPORT = os.getenv("NEWDB_METHOD_PASSPORT", "passport_mvd").strip()
NEWDB_METHOD_FSSP = os.getenv("NEWDB_METHOD_FSSP", "fssp_person").strip()
NEWDB_METHOD_BANKRUPTCY = os.getenv("NEWDB_METHOD_BANKRUPTCY", "bankrot_person").strip()
NEWDB_METHOD_COURTS = os.getenv("NEWDB_METHOD_COURTS", "arbitr_person").strip()
NEWDB_METHOD_EGRN = os.getenv("NEWDB_METHOD_EGRN", "rosreestr").strip()

# Залоги движимого имущества намеренно отключены: для проверки сделки с недвижимостью
# этот блок часто создает шум и не заменяет проверку ЕГРН / запретов регистрации.
ENABLE_PLEDGES = os.getenv("ENABLE_PLEDGES", "0").strip() == "1"

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


# ============================================================
# Models
# ============================================================

class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 78
    passport_series: str = ""
    passport_number: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    address: str = ""


# ============================================================
# Basic helpers
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        # Исправление частой mojibake-проблемы в русских ответах.
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed and sum(ch in fixed for ch in "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯабвгдежзийклмнопрстуфхцчшщэюя") > 2:
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
    out = []
    seen = set()
    for x in items:
        x = clean_text(x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
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
    """
    Чистит данные перед frontend/PDF/GigaChat.
    Важно: эту функцию нельзя применять до polling, потому что requestId/newdb_qid нужны внутри backend.
    """
    forbidden = {
        "token", "balance", "authorization", "x-api-key", "api_key",
        "requestid", "request_id", "newdb_qid", "taskid",
        "datecreated", "dateupdated", "is_repeat",
        "sent_params", "_http_status", "http_status",
        "docs_url", "errors_info", "api docs",
    }
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in forbidden:
                continue
            # Не показываем служебные ссылки newDB клиенту.
            if isinstance(v, str) and "newdb.net/docs" in v:
                continue
            cleaned[k] = strip_internal(v)
        return cleaned
    if isinstance(obj, list):
        return [strip_internal(x) for x in obj]
    return obj


def flatten_human_values(obj: Any, limit: int = 80) -> List[str]:
    out: List[str] = []
    skip_keys = {
        "method", "country", "requestid", "request_id", "newdb_qid", "taskid",
        "token", "balance", "datecreated", "dateupdated", "sent_params",
        "_http_status", "http_status", "params", "status",
    }
    skip_values = {"ru", "complete", "completed", "done", "success", "ok", "restart", "queued", "in progress"}

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
        else:
            s = clean_text(x)
            if not s:
                return
            if s.lower() in skip_values:
                return
            if "newdb.net/docs" in s:
                return
            if re.fullmatch(r"[a-f0-9-]{20,}", s.lower()):
                return
            if len(s) <= 300:
                out.append(s)

    walk(obj)
    return unique_keep_order(out)


def status_item(
    title: str,
    status: str,
    summary: str,
    details: Optional[List[str]] = None,
    manual_url: str = "",
    raw_data: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    status: ok | risk | manual_check
    Также добавляем ui_status для старого виджета, который мог ждать manual.
    """
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


def manual_item(title: str, summary: str = "Источник не вернул данные. Требуется ручная проверка.", details: Optional[List[str]] = None, manual_url: str = ""):
    return status_item(title, "manual_check", summary, details or ["Источник не вернул данные. Требуется ручная проверка."], manual_url)


def has_newdb_error(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return True
    txt = lower_blob(resp)
    if "errors_info" in resp:
        return True
    if any(marker in txt for marker in [
        "method or country is not valid",
        "not valid",
        "required",
        "не хватает обязательного",
        "не заполнено значение",
        "unauthorized",
        "forbidden",
        "not enough balance",
        "service is unavailable",
        "parsing failed",
    ]):
        return True
    # NewDB часто кладет ошибку в results.<method>.result.status
    for method, block in (resp.get("results") or {}).items():
        result = block.get("result") if isinstance(block, dict) else None
        if isinstance(result, dict):
            st = result.get("status")
            if isinstance(st, int) and st >= 400:
                return True
            if clean_text(result.get("error")):
                return True
    return False


def response_state(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    return clean_text(resp.get("state") or resp.get("status") or "").lower()


# ============================================================
# Payload builders
# ============================================================

def seller_name(req: CheckRequest) -> str:
    return " ".join(x for x in [clean_text(req.last), clean_text(req.first), clean_text(req.middle)] if x).strip()


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
    last = clean_text(req.last)
    first = clean_text(req.first)
    middle = clean_text(req.middle)
    dob_iso = dob_to_iso(req.dob)
    inn = only_digits(req.inn)
    region = int(req.region or 78)

    ps = only_digits(req.passport_series)
    pn = only_digits(req.passport_number)
    prop = normalize_property(req)

    payloads: Dict[str, Optional[Dict[str, Any]]] = {}

    # Паспорт МВД: только при наличии серии и номера.
    payloads["passport"] = None
    if len(ps) == 4 and len(pn) == 6:
        payloads["passport"] = {
            "method": NEWDB_METHOD_PASSPORT,
            "country": "ru",
            "series": ps,
            "seria": ps,  # совместимость с возможными вариантами newDB
            "number": pn,
            "passport_series": ps,
            "passport_number": pn,
        }

    # ФССП: по ФИО, дате рождения, региону.
    payloads["fssp"] = None
    if first and last and dob_iso:
        payloads["fssp"] = {
            "method": NEWDB_METHOD_FSSP,
            "country": "ru",
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
            "regioncode": region,
        }

    # Банкротство физлица / Федресурс: по 12-значному ИНН физлица.
    payloads["bankruptcy"] = None
    if len(inn) == 12:
        payloads["bankruptcy"] = {
            "method": NEWDB_METHOD_BANKRUPTCY,
            "country": "ru",
            "innfiz": inn,
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
        }

    # Суды / арбитраж: по 12-значному ИНН физлица + ФИО.
    payloads["courts"] = None
    if len(inn) == 12:
        payloads["courts"] = {
            "method": NEWDB_METHOD_COURTS,
            "country": "ru",
            "innfiz": inn,
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
        }

    # ЕГРН / Росреестр: кадастровый номер или адрес всегда в address.
    payloads["egrn"] = None
    if prop["query"]:
        payloads["egrn"] = {
            "method": NEWDB_METHOD_EGRN,
            "country": "ru",
            "address": prop["query"],
        }

    # Залоги намеренно отключены.
    if ENABLE_PLEDGES and first and last:
        payloads["pledges"] = {
            "method": os.getenv("NEWDB_METHOD_PLEDGES", "pledge_person").strip(),
            "country": "ru",
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
        }

    return payloads


# ============================================================
# NewDB client
# ============================================================

async def newdb_request(params: Optional[Dict[str, Any]], max_wait: int = 120, poll_interval: int = 5) -> Dict[str, Any]:
    if not params:
        return {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}

    if not NEWDB_TOKEN:
        return {"state": "not_configured", "error": "NEWDB_TOKEN не задан в Environment."}

    headers = {
        "X-API-KEY": NEWDB_TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    request_id = str(uuid.uuid4())
    payload = {"params": params, "requestId": request_id}

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

            # 4xx — сразу ошибка параметров/доступа.
            if first.status_code >= 400:
                return data

            # Если ответ уже содержит results — возвращаем.
            if isinstance(data.get("results"), dict):
                return data

            # Если нет очереди и нет results — возвращаем как есть.
            st = response_state(data)
            if st not in {"queued", "queue", "in progress", "progress", "pending", "processing", "restart", "wait", "waiting"}:
                return data

            last_data = data
            elapsed = 0

            # ВАЖНО: newDB при повторном использовании того же top-level requestId возвращает результат.
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

                if r.status_code >= 400:
                    return polled

                if isinstance(polled.get("results"), dict):
                    return polled

                pst = response_state(polled)
                if pst not in {"queued", "queue", "in progress", "progress", "pending", "processing", "restart", "wait", "waiting"}:
                    return polled

            if isinstance(last_data, dict):
                last_data["state"] = "timeout"
                last_data["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд."
            return last_data

        except Exception as e:
            return {
                "state": "error",
                "error": clean_text(str(e)),
                "trace": traceback.format_exc(),
            }


async def run_newdb_checks(req: CheckRequest) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    payloads = build_payloads(req)

    tasks = {
        "passport": newdb_request(payloads.get("passport"), max_wait=70),
        "fssp": newdb_request(payloads.get("fssp"), max_wait=120),
        "bankruptcy": newdb_request(payloads.get("bankruptcy"), max_wait=120),
        "courts": newdb_request(payloads.get("courts"), max_wait=160),
        "egrn": newdb_request(payloads.get("egrn"), max_wait=300),
    }

    if ENABLE_PLEDGES and payloads.get("pledges"):
        tasks["pledges"] = newdb_request(payloads.get("pledges"), max_wait=120)

    keys = list(tasks.keys())
    values = await asyncio.gather(*tasks.values())
    return payloads, dict(zip(keys, values))


# ============================================================
# Extractors
# ============================================================

def get_result_block(resp: Dict[str, Any], method: str) -> Optional[Dict[str, Any]]:
    if not isinstance(resp, dict):
        return None
    results = resp.get("results")
    if not isinstance(results, dict):
        return None
    if method in results and isinstance(results[method], dict):
        return results[method].get("result") if isinstance(results[method].get("result"), dict) else results[method]
    # fallback: первый result в results.
    for block in results.values():
        if isinstance(block, dict):
            result = block.get("result")
            if isinstance(result, dict):
                return result
    return None


def get_result_data(resp: Dict[str, Any], method: str) -> Any:
    block = get_result_block(resp, method)
    if isinstance(block, dict):
        return block.get("data")
    return None


def extract_money(item: Any) -> float:
    if not item:
        return 0.0
    txt = json_text(item)
    candidates = []

    # Явные числовые поля.
    if isinstance(item, dict):
        for key in [
            "amount", "sum", "debt", "debt_sum", "debtSum", "total", "balance",
            "ip_sum", "rest", "rest_sum", "amountDue", "execSum",
        ]:
            if key in item:
                try:
                    candidates.append(float(str(item[key]).replace(" ", "").replace(",", ".")))
                except Exception:
                    pass

    # Денежные строки.
    for m in re.findall(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:руб|₽)", txt, flags=re.I):
        try:
            candidates.append(float(m.replace(" ", "").replace(",", ".")))
        except Exception:
            pass

    return max(candidates) if candidates else 0.0


def extract_items_from_data(data: Any) -> List[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ["items", "records", "rows", "list", "results", "data"]:
            value = data.get(key)
            if isinstance(value, list):
                return value
        # если словарь выглядит как одна запись
        meaningful = {k: v for k, v in data.items() if k not in {"status", "error"}}
        if meaningful:
            return [meaningful]
    return []


# ============================================================
# Classifiers
# ============================================================

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

    # Если source ответил пустым списком — это ok, активных ИП не найдено.
    if isinstance(data, list) and len(data) == 0:
        summary = "По полученным данным исполнительные производства не найдены."
        stats = {
            "all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0,
            "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "actual_debt": 0.0,
            "active_items": [], "closed_items": [], "unknown_items": [],
        }
        return status_item(title, "ok", summary, [], MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})

    txt = lower_blob(data or resp)
    if not items and any(x in txt for x in ["не найден", "нет свед", "отсутств", "not found", "nothing found", "no data"]):
        stats = {
            "all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0,
            "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "actual_debt": 0.0,
            "active_items": [], "closed_items": [], "unknown_items": [],
        }
        return status_item(title, "ok", "По полученным данным исполнительные производства не найдены.", [], MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})

    if not items:
        return manual_item(title, "ФССП вернул ответ, но записи не удалось интерпретировать. Требуется ручная проверка.", flatten_human_values(data or resp, 10), MANUAL_LINKS["fssp"])

    active, closed, unknown = [], [], []
    closed_markers = [
        "оконч", "прекращ", "закрыт", "заверш", "исполнено", "ст. 46", "статья 46",
        "п. 3 ч. 1 ст. 46", "п. 4 ч. 1 ст. 46", "terminated", "closed", "completed"
    ]
    active_markers = [
        "возбужден", "актив", "задолженность", "взыскание", "остаток", "на исполнении",
        "active", "in progress"
    ]

    for item in items:
        t = lower_blob(item)
        if any(m in t for m in closed_markers):
            closed.append(item)
        elif any(m in t for m in active_markers):
            active.append(item)
        else:
            unknown.append(item)

    active_sum = sum(extract_money(x) for x in active)
    closed_sum = sum(extract_money(x) for x in closed)
    unknown_sum = sum(extract_money(x) for x in unknown)
    total_sum_all = active_sum + closed_sum + unknown_sum

    stats = {
        "all_count": len(items),
        "active_count": len(active),
        "closed_count": len(closed),
        "unknown_count": len(unknown),
        "total_sum_all": total_sum_all,
        "active_sum": active_sum,
        "closed_sum": closed_sum,
        "unknown_sum": unknown_sum,
        "actual_debt": active_sum,
        "active_items": strip_internal(active),
        "closed_items": strip_internal(closed),
        "unknown_items": strip_internal(unknown),
    }

    details = [
        f"Всего найдено ИП: {len(items)}",
        f"Активные ИП: {len(active)}",
        f"Закрытые/оконченные ИП: {len(closed)}",
        f"Неоднозначные записи: {len(unknown)}",
        f"Общая сумма всех найденных ИП: {rub(total_sum_all)}",
        f"Сумма по активным ИП: {rub(active_sum)}",
        f"Сумма по закрытым ИП: {rub(closed_sum)}",
        f"Актуальный долг по активным ИП: {rub(active_sum)}",
    ]

    if active:
        return status_item(title, "risk", f"Найдены активные исполнительные производства. Актуальная сумма долга: {rub(active_sum)}.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})

    if closed and not active and not unknown:
        return status_item(title, "ok", f"Найдены исполнительные производства. Активных: 0, закрытых: {len(closed)}. Актуальный долг по активным производствам: 0 ₽.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})

    if unknown:
        return status_item(title, "manual_check", "Найдены исполнительные производства с неоднозначным статусом. Требуется ручная проверка.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})

    return status_item(title, "ok", "По полученным данным исполнительные производства не найдены.", details, MANUAL_LINKS["fssp"], stats, {"fssp_stats": stats})


def classify_bankruptcy(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Банкротство / Федресурс"

    if response_state(resp) == "skipped":
        return manual_item(title, "Банкротство не проверялось автоматически: не передан 12-значный ИНН физлица.", ["Укажите ИНН физлица из 12 цифр."], MANUAL_LINKS["bankruptcy"])

    if has_newdb_error(resp):
        return manual_item(title, "Источник банкротства не вернул данные. Требуется ручная проверка.", ["Источник не принял запрос, недоступен или не смог обработать данные."], MANUAL_LINKS["bankruptcy"])

    data = get_result_data(resp, NEWDB_METHOD_BANKRUPTCY)
    items = extract_items_from_data(data)

    if isinstance(data, list) and len(data) == 0:
        return status_item(title, "ok", "По полученным данным сведения о банкротстве не выявлены.", [], MANUAL_LINKS["bankruptcy"], data)

    txt = lower_blob(data or resp)
    if not items and any(x in txt for x in ["не найден", "нет свед", "отсутств", "not found", "nothing found", "no data"]):
        return status_item(title, "ok", "По полученным данным сведения о банкротстве не выявлены.", [], MANUAL_LINKS["bankruptcy"], data)

    if items:
        return status_item(title, "risk", "Выявлены сведения, связанные с банкротством.", flatten_human_values(items, 12), MANUAL_LINKS["bankruptcy"], items)

    return manual_item(title, "Источник банкротства вернул неоднозначный результат. Требуется ручная проверка.", flatten_human_values(data or resp, 10), MANUAL_LINKS["bankruptcy"])


def classify_courts(resp: Dict[str, Any]) -> Dict[str, Any]:
    title = "Суды / арбитраж"

    if response_state(resp) == "skipped":
        return manual_item(title, "Суды не проверялись автоматически: не передан 12-значный ИНН физлица.", ["Укажите ИНН физлица из 12 цифр."], MANUAL_LINKS["courts"])

    if has_newdb_error(resp):
        return manual_item(title, "Источник судебных дел не вернул данные. Требуется ручная проверка.", ["Источник не принял запрос, недоступен или не смог обработать данные."], MANUAL_LINKS["courts"])

    data = get_result_data(resp, NEWDB_METHOD_COURTS)
    items = extract_items_from_data(data)

    if isinstance(data, list) and len(data) == 0:
        return status_item(title, "ok", "По полученным данным судебные дела не выявлены.", [], MANUAL_LINKS["courts"], data)

    txt = lower_blob(data or resp)
    if not items and any(x in txt for x in ["не найден", "нет свед", "отсутств", "not found", "nothing found", "no data"]):
        return status_item(title, "ok", "По полученным данным судебные дела не выявлены.", [], MANUAL_LINKS["courts"], data)

    if items:
        return status_item(title, "risk", "Найдены судебные производства. Требуется анализ предмета спора.", flatten_human_values(items, 12), MANUAL_LINKS["courts"], items)

    return manual_item(title, "Источник судебных дел вернул неоднозначный результат. Требуется ручная проверка.", flatten_human_values(data or resp, 10), MANUAL_LINKS["courts"])


def classify_egrn(resp: Dict[str, Any], prop: Dict[str, str]) -> Dict[str, Any]:
    title = "ЕГРН / Росреестр"

    if response_state(resp) == "skipped":
        return manual_item(title, "ЕГРН не проверялся автоматически: не передан адрес или кадастровый номер.", ["Укажите кадастровый номер или адрес объекта."], MANUAL_LINKS["egrn"])

    if has_newdb_error(resp):
        return manual_item(title, "Источник ЕГРН / Росреестра не вернул данные. Требуется ручная проверка.", ["Источник не принял запрос, недоступен или не смог обработать объект."], MANUAL_LINKS["egrn"])

    data = get_result_data(resp, NEWDB_METHOD_EGRN)

    # КРИТИЧНО: Росреестр успешен только если есть results.rosreestr.result.data и в нем объект.
    if not isinstance(data, list) or len(data) == 0:
        return manual_item(title, "Росреестр не вернул данные объекта. Требуется ручная проверка.", [f"Запрос: {prop.get('query') or ''}"], MANUAL_LINKS["egrn"])

    obj = data[0]
    if not isinstance(obj, dict):
        return manual_item(title, "Росреестр вернул данные в непонятном формате. Требуется ручная проверка.", [f"Запрос: {prop.get('query') or ''}"], MANUAL_LINKS["egrn"])

    encumbrances = obj.get("encumbrances") or []
    rights = obj.get("rights") or []

    readable_address = clean_text(deep_get(obj, ["address", "readableAddress"]) or deep_get(obj, ["address", "address"]) or "")
    cad_number = clean_text(obj.get("cadNumber") or prop.get("cadastral_number") or prop.get("query"))
    area = clean_text(obj.get("area"))
    cad_cost = clean_text(obj.get("cadCost"))
    obj_type = clean_text(obj.get("objType_text"))
    purpose = clean_text(obj.get("purpose_text"))

    details = []
    if cad_number:
        details.append(f"Кадастровый номер: {cad_number}")
    if readable_address:
        details.append(f"Адрес: {readable_address}")
    if obj_type:
        details.append(f"Тип объекта: {obj_type}")
    if purpose:
        details.append(f"Назначение: {purpose}")
    if area:
        details.append(f"Площадь: {area} кв.м")
    if cad_cost:
        details.append(f"Кадастровая стоимость: {rub(cad_cost)}")
    if rights:
        details.append(f"Записей о правах: {len(rights)}")

    enc_details = []
    for enc in encumbrances if isinstance(encumbrances, list) else []:
        if not isinstance(enc, dict):
            continue
        desc = clean_text(enc.get("typeDesc"))
        typ = clean_text(enc.get("type"))
        num = clean_text(enc.get("encumbranceNumber"))
        start = clean_text(enc.get("startDate"))
        label = desc or f"Тип ограничения: {typ}" if typ else "Ограничение / обременение"
        if num:
            label += f", № {num}"
        if start:
            label += f", дата начала: {start}"
        enc_details.append(label)

    raw_text = lower_blob(obj)
    risk_words = ["запрещение регистрации", "запрет", "ограничение", "обременение", "арест", "ипотека", "залог", "рента"]

    if encumbrances or any(w in raw_text for w in risk_words):
        if enc_details:
            details.extend(enc_details)
        else:
            details.append("В ответе Росреестра есть признаки ограничений или обременений.")
        return status_item(
            title,
            "risk",
            "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН.",
            details,
            MANUAL_LINKS["egrn"],
            obj,
            {"egrn_object": strip_internal(obj)}
        )

    details.append("По полученным данным явные признаки ограничений или обременений не выявлены.")
    return status_item(
        title,
        "ok",
        "Данные по объекту получены. По полученным данным явные признаки ограничений или обременений не выявлены.",
        details,
        MANUAL_LINKS["egrn"],
        obj,
        {"egrn_object": strip_internal(obj)}
    )


def build_checklist(req: CheckRequest, responses: Dict[str, Any]) -> List[Dict[str, Any]]:
    prop = normalize_property(req)
    items = [
        classify_passport(responses.get("passport") or {}),
        classify_fssp(responses.get("fssp") or {}),
        classify_bankruptcy(responses.get("bankruptcy") or {}),
        classify_courts(responses.get("courts") or {}),
        classify_egrn(responses.get("egrn") or {}, prop),
    ]

    # Залоги в сделке с недвижимостью не включаем по умолчанию.
    if ENABLE_PLEDGES and "pledges" in responses:
        # Не используем в основном выводе, но можно добавить при включении env.
        pass

    return items


def registry_data_from_checklist(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    mapping = {
        "Паспорт МВД": "passport",
        "ФССП": "fssp",
        "Банкротство / Федресурс": "bankruptcy",
        "Суды / арбитраж": "courts",
        "ЕГРН / Росреестр": "egrn",
    }
    out: Dict[str, Any] = {}
    for item in checklist:
        key = mapping.get(item.get("title"), item.get("title", "unknown"))
        out[key] = {
            "title": item.get("title"),
            "status": item.get("status"),
            "ui_status": item.get("ui_status"),
            "summary": item.get("summary"),
            "details": item.get("details", []),
            "manual_check_url": item.get("manual_check_url", ""),
            "data": item.get("data"),
        }
        if item.get("fssp_stats"):
            out[key]["fssp_stats"] = item.get("fssp_stats")
        if item.get("egrn_object"):
            out[key]["egrn_object"] = item.get("egrn_object")
    return out


# ============================================================
# GigaChat / local report
# ============================================================

def fallback_legal_report(req: CheckRequest, checklist: List[Dict[str, Any]]) -> str:
    risks = [x for x in checklist if x["status"] == "risk"]
    manuals = [x for x in checklist if x["status"] == "manual_check"]
    oks = [x for x in checklist if x["status"] == "ok"]

    seller = seller_name(req) or "по предоставленным данным не указан"
    prop = normalize_property(req)
    obj = prop.get("query") or "по предоставленным данным не указан"

    if risks:
        conclusion = "Предварительный вывод: выявлены факторы риска, сделку нельзя выводить на аванс без ручной юридической проверки."
    elif manuals:
        conclusion = "Предварительный вывод: часть источников не вернула данные, требуется ручная проверка до аванса."
    else:
        conclusion = "Предварительный вывод: по автоматически полученным данным явных рисков не выявлено, но это не заменяет ручную проверку документов."

    lines = [
        "1. Краткий вывод",
        conclusion,
        "",
        "2. Что проверено",
        f"Продавец: {seller}.",
        f"Дата рождения: {normalize_dob(req.dob) or 'по предоставленным данным не указана'}.",
        f"Объект: {obj}.",
        f"Проверок без явных рисков: {len(oks)}. Проверок с рисками: {len(risks)}. Требуют ручной проверки: {len(manuals)}.",
        "",
        "3. Риски по продавцу",
    ]

    seller_items = [x for x in checklist if x["title"] != "ЕГРН / Росреестр"]
    if not seller_items:
        lines.append("По предоставленным данным не проверялось.")
    for item in seller_items:
        prefix = "Риск" if item["status"] == "risk" else "Ручная проверка" if item["status"] == "manual_check" else "Без явных рисков"
        lines.append(f"- {item['title']}: {prefix}. {item['summary']}")

    lines.extend(["", "4. Риски по объекту"])
    obj_items = [x for x in checklist if x["title"] == "ЕГРН / Росреестр"]
    if not obj_items:
        lines.append("По предоставленным данным не проверялось.")
    for item in obj_items:
        prefix = "Риск" if item["status"] == "risk" else "Ручная проверка" if item["status"] == "manual_check" else "Без явных рисков"
        lines.append(f"- {item['title']}: {prefix}. {item['summary']}")
        for d in item.get("details", [])[:8]:
            lines.append(f"  • {d}")

    lines.extend([
        "",
        "5. Что говорит в пользу сделки",
    ])

    if oks:
        for item in oks:
            lines.append(f"- {item['title']}: {item['summary']}")
    else:
        lines.append("В пользу сделки пока нельзя засчитать ни один автоматический источник: нет подтвержденных проверок со статусом ok.")

    lines.extend([
        "",
        "6. Что обязательно проверить до аванса",
    ])

    if manuals or risks:
        for item in manuals + risks:
            if item.get("manual_check_url"):
                lines.append(f"- {item['title']}: требуется ручная проверка. {item['manual_check_url']}")
            else:
                lines.append(f"- {item['title']}: требуется ручная проверка.")
    else:
        lines.append("- Сверить паспорт, правоустанавливающие документы, свежую выписку ЕГРН и условия расчетов.")

    lines.extend([
        "",
        "7. Что прописать в авансовом соглашении / ПДКП",
        "Прописать обязанность продавца подтвердить отсутствие скрытых обременений, арестов, запретов, банкротства, активных исполнительных производств и судебных споров, влияющих на сделку.",
        "Если выявлены ограничения по ЕГРН, отдельно указать срок, порядок и документальное подтверждение их снятия до основной сделки.",
        "",
        "8. Безопасная схема расчетов",
        "При долгах, ограничениях или неполных данных использовать аккредитив, депозит нотариуса или иную контролируемую схему с раскрытием денег только после выполнения условий.",
        "",
        "9. Итоговое заключение",
        "Отчет является предварительным. Не обещает 100% безопасность и не заменяет ручную юридическую проверку документов специалистом.",
    ])

    return "\n".join(lines)


def build_gigachat_payload(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    return {
        "seller": {
            "last": clean_text(req.last),
            "first": clean_text(req.first),
            "middle": clean_text(req.middle),
            "full_name": seller_name(req),
            "dob": normalize_dob(req.dob),
            "inn_provided": bool(only_digits(req.inn)),
            "passport_provided": bool(only_digits(req.passport_series) and only_digits(req.passport_number)),
        },
        "property": normalize_property(req),
        "checklist": checklist,
        "registry_data": registry_data,
        "warnings": warnings,
        "not_checked": [x["title"] for x in checklist if x["status"] == "manual_check"],
    }


async def gigachat_report(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], warnings: List[str]) -> str:
    """
    GigaChat не должен ломать процесс. Если авторизация/модель недоступны — отдаём локальный юридический отчет.
    """
    if not (GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)):
        return fallback_legal_report(req, checklist)

    try:
        credentials = GIGACHAT_CREDENTIALS
        if not credentials:
            raw = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode("utf-8")
            credentials = base64.b64encode(raw).decode("utf-8")

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=GIGACHAT_VERIFY_SSL_CERTS) as client:
            auth_resp = await client.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"scope": GIGACHAT_SCOPE},
            )
            auth_resp.raise_for_status()
            token = auth_resp.json().get("access_token")
            if not token:
                return fallback_legal_report(req, checklist)

            structured = build_gigachat_payload(req, checklist, registry_data, warnings)
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
                "Если данных нет — прямо пиши: «по предоставленным данным не проверялось».\n"
                "Если источник не ответил — пиши: «требуется ручная проверка».\n"
                "Не обещай 100% безопасность.\n"
                "Не называй объект юридически чистым.\n"
                "Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.\n\n"
                "СТРУКТУРИРОВАННЫЕ ДАННЫЕ:\n"
                f"{json.dumps(structured, ensure_ascii=False, indent=2)}"
            )

            chat_resp = await client.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "model": GIGACHAT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 3000,
                },
            )
            chat_resp.raise_for_status()
            data = chat_resp.json()
            content = deep_get(data, ["choices", 0, "message", "content"], "")
            return clean_text(content) if content else fallback_legal_report(req, checklist)

    except Exception:
        warnings.append("GigaChat временно недоступен. Отчет сформирован резервным юридическим шаблоном.")
        return fallback_legal_report(req, checklist)


# ============================================================
# PDF
# ============================================================

def pdf_escape(text: Any) -> str:
    return html.escape(clean_text(text)).replace("\n", "<br/>")


def setup_pdf_font() -> str:
    # Ищем системный шрифт с кириллицей.
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for p in candidates:
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

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="AppTitle", parent=styles["Title"], fontName=font, fontSize=17, leading=22, spaceAfter=10))
    styles.add(ParagraphStyle(name="AppH2", parent=styles["Heading2"], fontName=font, fontSize=12, leading=16, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="AppBody", parent=styles["BodyText"], fontName=font, fontSize=9, leading=13, spaceAfter=4))
    styles.add(ParagraphStyle(name="AppSmall", parent=styles["BodyText"], fontName=font, fontSize=8, leading=11, textColor="#555555", spaceAfter=3))

    story = []
    story.append(Paragraph("Юридический отчет по проверке продавца и объекта недвижимости", styles["AppTitle"]))
    story.append(Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles["AppSmall"]))

    seller = result.get("seller", {})
    prop = result.get("property", {})

    story.append(Paragraph("1. Данные продавца", styles["AppH2"]))
    story.append(Paragraph(f"ФИО: {pdf_escape(seller.get('full_name') or '')}", styles["AppBody"]))
    story.append(Paragraph(f"Дата рождения: {pdf_escape(seller.get('dob') or '')}", styles["AppBody"]))
    story.append(Paragraph(f"ИНН передан: {'да' if seller.get('inn_provided') else 'нет'}", styles["AppBody"]))

    story.append(Paragraph("2. Данные объекта", styles["AppH2"]))
    story.append(Paragraph(f"Запрос: {pdf_escape(prop.get('query') or '')}", styles["AppBody"]))
    if prop.get("cadastral_number"):
        story.append(Paragraph(f"Кадастровый номер: {pdf_escape(prop.get('cadastral_number'))}", styles["AppBody"]))

    story.append(Paragraph("3. Чек-лист проверок", styles["AppH2"]))
    for item in result.get("checklist", []):
        label = "Проверено" if item.get("status") == "ok" else "Риск" if item.get("status") == "risk" else "Требуется ручная проверка"
        story.append(Paragraph(f"<b>{pdf_escape(item.get('title'))}:</b> {label}. {pdf_escape(item.get('summary'))}", styles["AppBody"]))
        for d in item.get("details", [])[:12]:
            story.append(Paragraph(f"• {pdf_escape(d)}", styles["AppSmall"]))

    story.append(Paragraph("4. Данные, полученные из реестров", styles["AppH2"]))
    registry = result.get("registry_data", {})
    for key in ["passport", "fssp", "bankruptcy", "courts", "egrn"]:
        block = registry.get(key)
        if not block:
            continue
        story.append(Paragraph(f"<b>{pdf_escape(block.get('title'))}:</b> {pdf_escape(block.get('summary'))}", styles["AppBody"]))
        for d in block.get("details", [])[:12]:
            story.append(Paragraph(f"• {pdf_escape(d)}", styles["AppSmall"]))

    story.append(Paragraph("5. Юридический отчет", styles["AppH2"]))
    for para in (result.get("legal_report") or "").split("\n"):
        if para.strip():
            story.append(Paragraph(pdf_escape(para), styles["AppBody"]))
        else:
            story.append(Spacer(1, 3))

    story.append(Paragraph("Дисклеймер", styles["AppH2"]))
    story.append(Paragraph(pdf_escape(DISCLAIMER), styles["AppSmall"]))

    doc.build(story)
    return pdf_path


# ============================================================
# Main response assembly
# ============================================================

def make_safe_result(req: CheckRequest, checklist: List[Dict[str, Any]], registry_data: Dict[str, Any], legal_report: str, warnings: List[str], report_id: str) -> Dict[str, Any]:
    prop = normalize_property(req)
    result = {
        "success": True,
        "report_id": report_id,
        "seller": {
            "full_name": seller_name(req),
            "dob": normalize_dob(req.dob),
            "inn_provided": bool(only_digits(req.inn)),
        },
        "property": prop,
        "checklist": checklist,
        "registry_data": registry_data,
        "legal_report": legal_report,
        "pdf_available": False,
        "pdf_url": f"/download-pdf/{report_id}",
        "pdf_base64": None,  # совместимость со старым виджетом
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }
    return result


# ============================================================
# Endpoints
# ============================================================

@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "real-estate-check-api",
        "version": "5.0-final",
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)),
        "pledges_enabled": ENABLE_PLEDGES,
        "methods": {
            "passport": NEWDB_METHOD_PASSPORT,
            "fssp": NEWDB_METHOD_FSSP,
            "bankruptcy": NEWDB_METHOD_BANKRUPTCY,
            "courts": NEWDB_METHOD_COURTS,
            "egrn": NEWDB_METHOD_EGRN,
        },
    }


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    """
    Диагностический endpoint. Не вызывать с клиентского сайта.
    Показывает реальные payloads и raw responses newDB по нужным источникам:
    паспорт, ЕГРН, ФССП, суды, банкротство физлица.
    """
    payloads, responses = await run_newdb_checks(req)
    useful_payloads = {k: payloads.get(k) for k in ["passport", "fssp", "bankruptcy", "courts", "egrn"]}
    useful_responses = {k: responses.get(k) for k in ["passport", "fssp", "bankruptcy", "courts", "egrn"]}

    checklist = build_checklist(req, useful_responses)
    return {
        "success": True,
        "payloads": useful_payloads,
        "responses": useful_responses,
        "classified_checklist": checklist,
        "notes": [
            "Если passport=null — не переданы серия и номер паспорта.",
            "Если ФССП вернул result.status=500 service is unavailable — это сбой источника/newDB, статус должен быть manual_check.",
            "Если ЕГРН содержит results.rosreestr.result.data[0].encumbrances — это риск по объекту.",
            "Залоги движимого имущества отключены и не участвуют в отчете.",
        ],
    }


@app.post("/check-report")
async def check_report(req: CheckRequest):
    warnings: List[str] = []
    report_id = str(uuid.uuid4())

    try:
        payloads, responses = await run_newdb_checks(req)

        checklist = build_checklist(req, responses)
        registry_data = registry_data_from_checklist(checklist)

        legal_report = await gigachat_report(req, checklist, registry_data, warnings)

        result = make_safe_result(req, checklist, registry_data, legal_report, warnings, report_id)

        # PDF не должен ломать основной ответ.
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

        # Сохраняем JSON отчета.
        try:
            (REPORT_DIR / f"{report_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        return result

    except Exception:
        # Клиенту безопасный ответ, debug покажет подробности.
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.",
                "warnings": ["Техническая ошибка скрыта от пользователя. Подробности можно посмотреть через /debug-newdb."],
            },
        )


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", report_id)
    path = REPORT_DIR / f"{safe_id}.pdf"
    if not path.exists():
        return JSONResponse(status_code=404, content={"success": False, "message": "PDF не найден или уже удален."})
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=f"otchet_{datetime.now().strftime('%Y-%m-%d')}.pdf",
    )
