from __future__ import annotations

import asyncio
import io
import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Optional PDF deps. If reportlab is absent, backend still returns JSON.
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
        PageBreak,
    )
    from reportlab.graphics.shapes import Drawing, Rect, String, Polygon, Line
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# Optional GigaChat. Report has a deterministic local fallback.
try:
    from gigachat import GigaChat
    GIGACHAT_AVAILABLE = True
except Exception:
    GIGACHAT_AVAILABLE = False


APP_VERSION = "1.4.2-premium-pdf-gigachat-structured"
NEWDB_URL = "https://api.newdb.net/v2"
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "").strip()

# In-memory storage. For Render free/simple MVP it is OK; after restart reports disappear.
REPORTS: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="Real Estate Seller & Property Check API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CheckRequest(BaseModel):
    # Seller
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    seller_inn: str = ""
    inn_fiz: str = ""
    innfiz: str = ""
    innfl: str = ""

    # Passport
    passport_series: str = ""
    passport_number: str = ""
    seria: str = ""
    series: str = ""
    number: str = ""

    # FSSP
    region: Optional[int] = 0
    regioncode: Optional[int] = 0

    # Property
    cadastral_number: str = ""
    cadnum: str = ""
    cadastral: str = ""
    address: str = ""
    property_query: str = ""

    # Optional switches for economy/debug. By default: full report.
    run_passport: bool = True
    run_fssp: bool = True
    run_bankruptcy: bool = True
    run_arbitr: bool = True
    run_pravosud: bool = True
    run_egrn: bool = True


# ---------- utils ----------

def now_ru() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def digits_only(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def clean_str(value: Any) -> str:
    return str(value or "").strip()


def fio(req: CheckRequest) -> str:
    return " ".join(x for x in [req.last.strip(), req.first.strip(), req.middle.strip()] if x)


def normalize_dob(value: str) -> Tuple[str, str]:
    """Return (ru, iso). Accept dd.mm.yyyy or yyyy-mm-dd."""
    raw = clean_str(value)
    if not raw:
        return "", ""
    m = re.match(r"^(\d{2})[.\-/](\d{2})[.\-/](\d{4})$", raw)
    if m:
        d, mo, y = m.groups()
        return f"{d}.{mo}.{y}", f"{y}-{mo}-{d}"
    m = re.match(r"^(\d{4})[.\-/](\d{2})[.\-/](\d{2})$", raw)
    if m:
        y, mo, d = m.groups()
        return f"{d}.{mo}.{y}", f"{y}-{mo}-{d}"
    return raw, raw


def normalize_inn(req: CheckRequest) -> str:
    for candidate in [req.inn, req.seller_inn, req.inn_fiz, req.innfiz, req.innfl]:
        d = digits_only(candidate)
        if len(d) == 12:
            return d
    return ""


def mask_inn(inn: str) -> str:
    """Public-safe INN display: enough to confirm the value was used, not enough to overexpose it."""
    d = digits_only(inn)
    if len(d) != 12:
        return ""
    return f"{d[:4]}****{d[-4:]}"


def calculate_age(dob_ru: str) -> Optional[int]:
    dt = parse_date_any(dob_ru)
    if not dt:
        return None
    today = datetime.now()
    age = today.year - dt.year - ((today.month, today.day) < (dt.month, dt.day))
    return age if 0 <= age <= 120 else None


def normalize_passport(req: CheckRequest) -> Tuple[str, str]:
    series = digits_only(req.passport_series or req.seria or req.series)[:4]
    number = digits_only(req.passport_number or req.number)[:6]
    return series, number


def normalize_region(req: CheckRequest) -> int:
    try:
        return int(req.regioncode or req.region or 0)
    except Exception:
        return 0


def normalize_property(req: CheckRequest) -> Dict[str, str]:
    query = clean_str(req.cadastral_number or req.cadnum or req.cadastral or req.property_query or req.address)
    is_cad = bool(re.match(r"^\d{2}:\d{2}:\d+", query))
    return {
        "query": query,
        "cadastral_number": query if is_cad else "",
        "address": query,
        "type": "cadastral" if is_cad else "address",
    }


def rub(value: Any) -> str:
    try:
        n = float(value or 0)
    except Exception:
        n = 0.0
    if abs(n - int(n)) < 0.005:
        s = f"{int(round(n)):,}".replace(",", " ")
    else:
        s = f"{n:,.2f}".replace(",", " ")
    return f"{s} ₽"


def flatten_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def strip_service_fields(data: Any) -> Any:
    forbidden = {
        "requestId", "newdb_qid", "taskId", "balance", "_http_status",
        "errors_info", "docs_url", "params", "datecreated", "dateupdated",
        "is_repeat", "tasks",
    }
    if isinstance(data, dict):
        return {k: strip_service_fields(v) for k, v in data.items() if k not in forbidden}
    if isinstance(data, list):
        return [strip_service_fields(x) for x in data]
    return data


# ---------- NewDB ----------

async def newdb_post_json(client: httpx.AsyncClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not NEWDB_TOKEN:
        return {"state": "failed", "errors_info": [{"error": "NEWDB_TOKEN не задан в переменных окружения."}]}
    try:
        r = await client.post(
            NEWDB_URL,
            json=payload,
            headers={"X-API-KEY": NEWDB_TOKEN, "Content-Type": "application/json"},
            timeout=35,
        )
        try:
            data = r.json()
        except Exception:
            data = {"state": "failed", "errors_info": [{"error": r.text[:500]}]}
        data["_http_status"] = r.status_code
        return data
    except Exception as e:
        return {"state": "failed", "errors_info": [{"error": f"Ошибка запроса к newDB: {e}"}]}


def has_result_status_500(data: Dict[str, Any]) -> bool:
    text = flatten_text(data).lower()
    return "service is unavailable" in text or "parsing failed" in text or '"status": 500' in text


def is_newdb_error(data: Dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return True
    if data.get("errors_info"):
        return True
    if data.get("state") == "failed":
        return True
    return False


async def newdb_run(
    client: httpx.AsyncClient,
    params: Optional[Dict[str, Any]],
    *,
    timeout_sec: int = 75,
    poll_interval: float = 3.0,
) -> Dict[str, Any]:
    """Create exactly one NewDB task via {'params': params}; poll only via top-level {'requestId': id}.
    This avoids burning extra tokens during polling.
    """
    if not params:
        return {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}

    first = await newdb_post_json(client, {"params": params})
    if is_newdb_error(first):
        return first

    request_id = first.get("requestId")
    if not request_id:
        return first

    state = str(first.get("state") or "").lower()
    # If provider already returned final result or service 500 inside results, do not wait/restart.
    if state in {"complete", "done", "error", "failed"} or has_result_status_500(first):
        return first

    deadline = asyncio.get_event_loop().time() + timeout_sec
    last = first
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        # CRITICAL: polling only by top-level requestId. No params here.
        last = await newdb_post_json(client, {"requestId": request_id})
        if is_newdb_error(last):
            return last
        st = str(last.get("state") or "").lower()
        if st in {"complete", "done", "error", "failed"} or has_result_status_500(last):
            return last

    last["state"] = "timeout"
    last["error"] = f"Источник не вернул итоговый результат за {timeout_sec} секунд. Требуется ручная проверка."
    return last


async def newdb_run_with_fallback(
    client: httpx.AsyncClient,
    primary: Optional[Dict[str, Any]],
    fallback: Optional[Dict[str, Any]],
    *,
    timeout_sec: int = 75,
) -> Dict[str, Any]:
    res = await newdb_run(client, primary, timeout_sec=timeout_sec)
    # Use fallback only if method/country invalid. Otherwise avoid extra token.
    text = flatten_text(res).lower()
    if fallback and ("method or country is not valid" in text):
        fb = await newdb_run(client, fallback, timeout_sec=timeout_sec)
        fb["_fallback_tried"] = fallback.get("method")
        fb["_primary_error"] = res
        return fb
    return res


def result_data(response: Dict[str, Any], method: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    try:
        block = (response.get("results") or {}).get(method)
        if not block:
            return None, None
        result = block.get("result") or {}
        return result.get("data"), result
    except Exception:
        return None, None


# ---------- payloads ----------

def build_payloads(req: CheckRequest) -> Dict[str, Optional[Dict[str, Any]]]:
    dob_ru, dob_iso = normalize_dob(req.dob)
    inn = normalize_inn(req)
    region = normalize_region(req)
    series, number = normalize_passport(req)
    prop = normalize_property(req)
    full_fio = fio(req)

    payloads: Dict[str, Optional[Dict[str, Any]]] = {}

    payloads["passport"] = None
    if req.run_passport and series and number:
        payloads["passport"] = {
            "seria": series,
            "number": number,
            "firstname": req.first.strip(),
            "lastname": req.last.strip(),
            "secondname": req.middle.strip(),
            "dob": dob_iso,
            "country": "ru",
            "method": "passport_mvd",
        }

    payloads["fssp"] = None
    if req.run_fssp and req.first and req.last and dob_iso and region:
        payloads["fssp"] = {
            "firstname": req.first.strip(),
            "lastname": req.last.strip(),
            "secondname": req.middle.strip(),
            "dob": dob_iso,
            "regioncode": region,
            "country": "ru",
            "method": "fssp_person",
        }

    payloads["bankruptcy"] = None
    if req.run_bankruptcy and inn:
        payloads["bankruptcy"] = {"innfiz": inn, "country": "ru", "method": "bankrot_person"}

    payloads["arbitr"] = None
    if req.run_arbitr and inn:
        payloads["arbitr"] = {"innfiz": inn, "country": "ru", "method": "arbitr_person"}

    payloads["pravosud"] = None
    if req.run_pravosud and full_fio:
        payloads["pravosud"] = {
            "method": "pravo_search",
            "country": "ru",
            "query": full_fio,
            "q": full_fio,
            "fio": full_fio,
            "lastname": req.last.strip(),
            "firstname": req.first.strip(),
            "secondname": req.middle.strip(),
            "party_name": full_fio,
            "limit": 50,
        }

    payloads["egrn"] = None
    if req.run_egrn and prop["address"]:
        payloads["egrn"] = {"address": prop["address"], "country": "ru", "method": "rosreestr"}

    return payloads


def normalized_input(req: CheckRequest, *, expose_full_inn: bool = False) -> Dict[str, Any]:
    dob_ru, dob_iso = normalize_dob(req.dob)
    series, number = normalize_passport(req)
    inn = normalize_inn(req)
    age = calculate_age(dob_ru)
    return {
        "last": req.last.strip(),
        "first": req.first.strip(),
        "middle": req.middle.strip(),
        "dob": dob_ru,
        "dob_iso": dob_iso,
        "age": age,
        "inn": inn if expose_full_inn else mask_inn(inn),
        "inn_provided": bool(inn),
        "inn_masked": mask_inn(inn),
        "region": normalize_region(req),
        "passport_series": series,
        "passport_number": number,
        "property": normalize_property(req),
    }


# ---------- classifiers ----------

def manual_item(title: str, summary: str, url: str, details: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "title": title,
        "source": title,
        "status": "manual_check",
        "ui_status": "manual",
        "summary": summary,
        "details": details or ["Источник не вернул данные. Требуется ручная проверка."],
        "manual_check_url": url,
        "manual_url": url,
    }


def ok_item(title: str, summary: str, url: str, details: Optional[List[str]] = None, data: Any = None) -> Dict[str, Any]:
    item = {
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
        item["data"] = data
    return item


def risk_item(title: str, summary: str, url: str, details: Optional[List[str]] = None, data: Any = None) -> Dict[str, Any]:
    item = {
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
        item["data"] = data
    return item


def generic_error_details(response: Dict[str, Any]) -> List[str]:
    text = flatten_text(response).lower()
    if "баланс" in text or "x-api-key" in text or "token" in text:
        return ["Источник вернул ошибку доступа/баланса API. Требуется ручная проверка."]
    if response.get("state") == "timeout":
        return ["Источник не успел вернуть результат в установленное время. Требуется ручная проверка."]
    if response.get("state") in {"queued", "restart", "in progress"}:
        return ["Источник еще обрабатывает запрос. Требуется повторить позже или проверить вручную."]
    if has_result_status_500(response):
        return ["Источник временно недоступен или парсинг источника не выполнен. Требуется ручная проверка."]
    return ["Источник вернул ошибку. Требуется ручная проверка."]


def classify_passport(resp: Dict[str, Any]) -> Dict[str, Any]:
    title, url = "Паспорт МВД", "https://мвд.рф/сервисы-гувм"
    data, result = result_data(resp, "passport_mvd")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        return manual_item(title, "Источник не вернул данные по паспорту. Требуется ручная проверка.", url, generic_error_details(resp))
    status_text = flatten_text(data).lower()
    if "действител" in status_text and "недейств" not in status_text:
        return ok_item(title, "Паспорт по полученным данным действителен.", url, ["Действительный"], data)
    if "недейств" in status_text or "invalid" in status_text:
        return risk_item(title, "По паспорту выявлен риск: документ может быть недействительным.", url, [flatten_text(data)[:300]], data)
    return manual_item(title, "Источник не вернул понятный результат проверки паспорта. Требуется ручная проверка.", url)


def extract_debt_amount(text: str, prefer_remainder: bool = True) -> float:
    if not text:
        return 0.0
    patterns = []
    if prefer_remainder:
        patterns.append(r"Остаток долга[^:]*:\s*([\d\s]+(?:[,.]\d+)?)\s*руб")
    patterns.append(r"Сумма долга:\s*([\d\s]+(?:[,.]\d+)?)\s*руб")
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).replace(" ", "").replace(",", ".")
            try:
                return float(raw)
            except Exception:
                return 0.0
    return 0.0


def is_closed_fssp(item: Dict[str, Any]) -> bool:
    reason = clean_str(item.get("CompletionDateOrReason"))
    return bool(reason)


def classify_fssp(resp: Dict[str, Any]) -> Dict[str, Any]:
    title, url = "ФССП", "https://fssp.gov.ru/iss/ip"
    data, result = result_data(resp, "fssp_person")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        return manual_item(title, "Источник ФССП не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))
    if not isinstance(data, list):
        return manual_item(title, "Источник ФССП вернул нестандартный ответ. Требуется ручная проверка.", url)
    if not data:
        stats = {
            "all_count": 0, "active_count": 0, "closed_count": 0, "unknown_count": 0,
            "total_sum_all": 0.0, "active_sum": 0.0, "closed_sum": 0.0, "unknown_sum": 0.0,
            "actual_debt": 0.0, "active_items": [], "closed_items": [], "unknown_items": [],
        }
        return ok_item(title, "По полученным данным исполнительные производства не найдены.", url, [], stats)

    active, closed, unknown = [], [], []
    for item in data:
        if not isinstance(item, dict):
            unknown.append(item)
        elif is_closed_fssp(item):
            closed.append(item)
        else:
            active.append(item)

    def item_sum(x: Any) -> float:
        if not isinstance(x, dict):
            return 0.0
        return extract_debt_amount(clean_str(x.get("SubjectAndDebtAmount")))

    active_sum = sum(item_sum(x) for x in active)
    closed_sum = sum(item_sum(x) for x in closed)
    unknown_sum = sum(item_sum(x) for x in unknown)
    total_sum = active_sum + closed_sum + unknown_sum
    actual_debt = active_sum + unknown_sum
    stats = {
        "all_count": len(data),
        "active_count": len(active),
        "closed_count": len(closed),
        "unknown_count": len(unknown),
        "total_sum_all": round(total_sum, 2),
        "active_sum": round(active_sum, 2),
        "closed_sum": round(closed_sum, 2),
        "unknown_sum": round(unknown_sum, 2),
        "actual_debt": round(actual_debt, 2),
        "active_items": active,
        "closed_items": closed,
        "unknown_items": unknown,
    }
    details = [
        f"Всего найдено ИП: {len(data)}",
        f"Активные ИП: {len(active)}",
        f"Закрытые/оконченные ИП: {len(closed)}",
        f"Неоднозначные записи: {len(unknown)}",
        f"Общая сумма всех найденных ИП: {rub(total_sum)}",
        f"Сумма по активным ИП: {rub(active_sum)}",
        f"Сумма по закрытым ИП: {rub(closed_sum)}",
        f"Актуальный долг по активным/неоднозначным ИП: {rub(actual_debt)}",
    ]
    if active or unknown:
        return risk_item(title, f"Найдены активные или неоднозначные исполнительные производства. Актуальная сумма для ручной оценки: {rub(actual_debt)}.", url, details, stats)
    return ok_item(title, f"Найдены только закрытые/оконченные исполнительные производства. Актуальный долг по активным ИП: {rub(0)}.", url, details, stats)




def parse_date_any(value: Any) -> Optional[datetime]:
    """Best-effort date parser for dates returned by NewDB/Fedresurs.
    Keeps the service stable even when the provider changes field names/formats.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Common formats: 27.04.2026, 2026-04-27, with/without time, ISO with T/Z.
    raw = raw.replace("T", " ").replace("Z", "").strip()
    raw = re.sub(r"\s+", " ", raw)
    candidates = [
        raw[:19],
        raw[:10],
    ]
    patterns = [
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ]
    for candidate in candidates:
        for pattern in patterns:
            try:
                return datetime.strptime(candidate, pattern)
            except Exception:
                continue
    return None


def months_between_dates(date_from: Optional[datetime], date_to: Optional[datetime] = None) -> Optional[int]:
    if not date_from:
        return None
    date_to = date_to or datetime.now()
    months = (date_to.year - date_from.year) * 12 + (date_to.month - date_from.month)
    if date_to.day < date_from.day:
        months -= 1
    return max(0, months)


def bankruptcy_deep_flags(data: Any) -> Dict[str, Any]:
    """Extract a softer, legally useful bankruptcy risk profile.

    Important: this function does not decide that a completed bankruptcy blocks the deal.
    It separates active/unclear cases from completed cases and adds manual-check markers.
    """
    text = flatten_text(data).lower()

    completed_words = [
        "заверш", "завершение реализации имущества", "процедура завершена",
        "освобожд", "освободить гражданина", "освобождение гражданина",
        "прекратить производство", "производство по делу завершено",
    ]
    active_words = [
        "введена процедура", "ввести процедуру", "реализация имущества гражданина",
        "реструктуризация долгов", "признан банкротом", "финансовый управляющий",
        "конкурсное производство", "наблюдение",
    ]
    property_words = [
        "оспаривание сделки", "недействительность сделки", "признать сделку недействительной",
        "имущество должника", "конкурсная масса", "торги", "реализация имущества",
        "положение о продаже", "лот", "отчет финансового управляющего",
    ]

    dates: List[datetime] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                # Do not treat seller DOB as a bankruptcy/publication date.
                birth_keys = ["dob", "birth", "birthday", "datebirth", "birth_date", "дата рождения", "рождение"]
                if any(b in key for b in birth_keys):
                    pass
                elif any(w in key for w in ["date", "дата", "published", "publication", "create", "update"]):
                    dt = parse_date_any(v)
                    if dt:
                        dates.append(dt)
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for row in x:
                walk(row)

    walk(data)

    has_completed = any(word in text for word in completed_words)
    has_active = any(word in text for word in active_words)

    if has_completed:
        status = "completed"
    elif has_active:
        status = "active"
    else:
        status = "unknown"

    latest_date = max(dates) if dates else None
    months_after_latest = months_between_dates(latest_date)
    has_property_words = any(word in text for word in property_words)

    details: List[str] = []
    if latest_date:
        details.append(f"Последняя дата в найденных публикациях: {latest_date.strftime('%d.%m.%Y')}")
    else:
        details.append("Автоматически определить даты публикаций/процедуры не удалось.")

    if status == "completed":
        details.append("По тексту публикаций есть признаки завершения процедуры банкротства.")
    elif status == "active":
        details.append("По тексту публикаций есть признаки действующей или незавершенной процедуры банкротства.")
    else:
        details.append("Статус процедуры по автоматическому ответу определить не удалось.")

    if months_after_latest is not None:
        if months_after_latest < 12:
            details.append("После последней значимой публикации прошло менее 1 года — нужна повышенная осторожность.")
        elif months_after_latest < 36:
            details.append("После последней значимой публикации прошло менее 3 лет — требуется проверка банкротного дела до аванса (задатка).")

    if has_property_words:
        details.append("В публикациях есть слова/признаки, связанные с имуществом, торгами, конкурсной массой или оспариванием сделок.")

    return {
        "status": status,
        "latest_date": latest_date.strftime("%d.%m.%Y") if latest_date else "",
        "months_after_latest": months_after_latest,
        "property_related_words": has_property_words,
        "details": details,
    }


def classify_bankruptcy(resp: Dict[str, Any]) -> Dict[str, Any]:
    title, url = "Банкротство / Федресурс", "https://bankrot.fedresurs.ru"
    data, result = result_data(resp, "bankrot_person")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        return manual_item(title, "Источник банкротств не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))

    # Empty known structure from NewDB is usually: [{'bankruptcy': [], 'publications': [], ...}]
    has_bankruptcy = False
    details: List[str] = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                labels = {
                    "bankruptcy": "Банкротные дела",
                    "publications": "Публикации Федресурса",
                    "encumbrances": "Связанные ограничения/обременения",
                }
                for key in ["bankruptcy", "publications", "encumbrances"]:
                    val = row.get(key)
                    if isinstance(val, list) and len(val) > 0:
                        has_bankruptcy = True
                        details.append(f"{labels.get(key, key)}: найдено записей {len(val)}")

    if not has_bankruptcy:
        return ok_item(title, "По полученным данным сведения о банкротстве физлица не выявлены.", url, [], data)

    flags = bankruptcy_deep_flags(data)
    details.extend(flags.get("details") or [])

    status = flags.get("status")
    months = flags.get("months_after_latest")
    property_related_words = bool(flags.get("property_related_words"))

    risk_data = {
        "raw": data,
        "bankruptcy_status": status,
        "latest_publication_date": flags.get("latest_date"),
        "months_after_latest": months,
        "property_related_words": property_related_words,
    }

    if status == "active":
        summary = (
            "Выявлены сведения о банкротстве продавца. По автоматическим данным есть признаки "
            "действующей или незавершенной процедуры. Это высокий риск для самостоятельной сделки."
        )
        details.append("До аванса (задатка) нужно проверить карточку банкротного дела, полномочия продавца и позицию финансового управляющего.")
        return risk_item(title, summary, url, details, risk_data)

    if status == "completed" and months is not None and months < 36:
        summary = (
            "Выявлены сведения о завершенном банкротстве продавца. После последней значимой публикации прошло менее 3 лет. "
            "Само по себе это не запрещает сделку, но требует ручной проверки банкротного дела и истории объекта."
        )
        details.append("Проверить: судебный акт о завершении процедуры, отчет финансового управляющего и перечень имущества должника.")
        details.append("Отдельно проверить: была ли квартира в собственности продавца до банкротства и почему она не реализовывалась.")
        return risk_item(title, summary, url, details, risk_data)

    if status == "completed":
        summary = (
            "Выявлены сведения о завершенном банкротстве продавца. Автоматический стоп-фактор не подтвержден, "
            "но перед авансом нужна проверка завершения процедуры и связи объекта с банкротным делом."
        )
        details.append("Завершенное банкротство не является автоматическим запретом на сделку, но должно быть отражено в юридическом анализе.")
        return risk_item(title, summary, url, details, risk_data)

    if property_related_words:
        summary = (
            "Выявлены сведения о банкротстве продавца и публикации, связанные с имуществом, торгами, "
            "конкурсной массой или оспариванием сделок. Нужна ручная проверка связи объекта с процедурой."
        )
        details.append("До аванса (задатка) нужно сверить объект с материалами банкротного дела и отчетом финансового управляющего.")
        return risk_item(title, summary, url, details, risk_data)

    summary = (
        "Выявлены сведения о банкротстве продавца. Статус процедуры автоматически определить не удалось. "
        "Сделку можно оценивать только после ручной проверки банкротного дела."
    )
    details.append("Нужно вручную проверить дату принятия заявления, дату завершения процедуры и публикации Федресурса.")
    return risk_item(title, summary, url, details, risk_data)


def classify_arbitr(resp: Dict[str, Any], req: Optional[CheckRequest] = None) -> Dict[str, Any]:
    title, url = "Арбитражные суды", "https://kad.arbitr.ru"
    data, result = result_data(resp, "arbitr_person")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        return manual_item(title, "Источник арбитражных судов не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))

    raw_cases = extract_arbitr_cases(data)
    if raw_cases:
        normalized_cases = [normalize_court_case(c, req or CheckRequest(), source="arbitr") for c in raw_cases]
        strong = [c for c in normalized_cases if c.get("match_level") == "точное совпадение"]
        probable = [c for c in normalized_cases if c.get("match_level") == "вероятное совпадение"]
        weak = [c for c in normalized_cases if c.get("match_level") == "слабое совпадение"]
        details = court_details_for_screen(normalized_cases)
        details.insert(0, f"Всего найдено записей: {len(normalized_cases)}")
        details.insert(1, f"Точные совпадения: {len(strong)}; вероятные: {len(probable)}; слабые/однофамильцы: {len(weak)}")
        if strong or probable:
            summary = f"Найдены арбитражные совпадения: {len(normalized_cases)}. Точных совпадений: {len(strong)}, вероятных: {len(probable)}, слабых: {len(weak)}. Без точной идентификации это не считается установленными делами продавца."
            return risk_item(title, summary, url, details, normalized_cases[:30])
        return manual_item(title, "Найдены слабые судебные совпадения по арбитражу. Это не считается установленным фактом по продавцу без ручной проверки.", url, details)

    return ok_item(title, "По полученным данным арбитражные дела не выявлены.", url, [], [])


def classify_pravosud(resp: Dict[str, Any], req: Optional[CheckRequest] = None) -> Dict[str, Any]:
    title, url = "Суды общей юрисдикции / ГАС Правосудие", "https://sudrf.ru"
    data, result = result_data(resp, "pravo_search")
    if data is None:
        data, result = result_data(resp, "pravosudfiz")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        text = flatten_text(resp).lower()
        if "method or country is not valid" in text:
            return manual_item(title, "Источник не вернул данные. Требуется ручная проверка.", url, ["Источник не принял метод или страну запроса. Требуется уточнить метод newDB."])
        return manual_item(title, "Источник судов общей юрисдикции не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))

    raw_cases = extract_pravosud_cases(data)
    if raw_cases:
        normalized_cases = [normalize_court_case(c, req or CheckRequest(), source="pravosud") for c in raw_cases]
        strong = [c for c in normalized_cases if c.get("match_level") == "точное совпадение"]
        probable = [c for c in normalized_cases if c.get("match_level") == "вероятное совпадение"]
        weak = [c for c in normalized_cases if c.get("match_level") == "слабое совпадение"]
        details = court_details_for_screen(normalized_cases)
        details.insert(0, f"Всего найдено записей: {len(normalized_cases)}")
        details.insert(1, f"Точные совпадения: {len(strong)}; вероятные: {len(probable)}; слабые/однофамильцы: {len(weak)}")
        if strong or probable:
            summary = f"Найдены судебные совпадения по ФИО: {len(normalized_cases)}. Точных совпадений: {len(strong)}, вероятных: {len(probable)}, слабых: {len(weak)}. Без точной идентификации это не считается установленными делами продавца."
            return risk_item(title, summary, url, details, normalized_cases[:30])
        return manual_item(title, "Найдены слабые совпадения в судах общей юрисдикции. Это не считается установленным фактом по продавцу без ручной проверки.", url, details)

    return ok_item(title, "По полученным данным дела в судах общей юрисдикции не выявлены.", url, [], [])


def classify_egrn(resp: Dict[str, Any]) -> Dict[str, Any]:
    title, url = "ЕГРН / Росреестр", "https://rosreestr.gov.ru"
    data, result = result_data(resp, "rosreestr")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        return manual_item(title, "Источник ЕГРН не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))
    if not isinstance(data, list) or not data:
        return manual_item(title, "Росреестр не вернул данные по объекту. Требуется ручная проверка.", url)
    obj = data[0] if isinstance(data[0], dict) else {}
    enc = obj.get("encumbrances") if isinstance(obj, dict) else []
    if not isinstance(enc, list):
        enc = []

    addr = (obj.get("address") or {}).get("readableAddress") if isinstance(obj.get("address"), dict) else ""
    details = [
        f"Кадастровый номер: {obj.get('cadNumber') or ''}".strip(),
        f"Адрес: {addr}".strip(),
        f"Тип объекта: {obj.get('objType_text') or ''}".strip(),
        f"Назначение: {obj.get('purpose_text') or ''}".strip(),
        f"Площадь: {obj.get('area') or ''} кв.м".strip(),
    ]
    if obj.get("cadCost"):
        try:
            details.append(f"Кадастровая стоимость: {rub(float(obj.get('cadCost')))}")
        except Exception:
            details.append(f"Кадастровая стоимость: {obj.get('cadCost')}")
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    details.append(f"Записей о правах: {len(rights)}")

    enc_details = []
    for e in enc:
        if not isinstance(e, dict):
            continue
        desc = clean_str(e.get("typeDesc")) or f"Тип ограничения: {e.get('type') or 'не указан'}"
        num = clean_str(e.get("encumbranceNumber"))
        start = clean_str(e.get("startDate"))
        line = desc
        if num:
            line += f", № {num}"
        if start:
            line += f", дата начала: {start}"
        enc_details.append(line)
    details.extend(enc_details)

    if enc:
        return risk_item(title, "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН.", url, [d for d in details if d and not d.endswith(':')], obj)
    return ok_item(title, "Данные по объекту получены. По полученным данным явные ограничения или обременения не выявлены.", url, [d for d in details if d], obj)


def classify_all(responses: Dict[str, Dict[str, Any]], req: Optional[CheckRequest] = None) -> List[Dict[str, Any]]:
    return [
        classify_passport(responses.get("passport") or {}),
        classify_fssp(responses.get("fssp") or {}),
        classify_bankruptcy(responses.get("bankruptcy") or {}),
        classify_arbitr(responses.get("arbitr") or {}, req),
        classify_pravosud(responses.get("pravosud") or {}, req),
        classify_egrn(responses.get("egrn") or {}),
    ]



# ---------- advanced legal risk helpers ----------

def strip_markdown_noise(text: str) -> str:
    """Make LLM/local report look like a clean legal conclusion, not markdown notes."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    s = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", s)
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"__(.*?)__", r"\1", s)
    s = re.sub(r"`{1,3}([^`]+)`{1,3}", r"\1", s)
    s = s.replace("Дисклеймер", "Важно").replace("дисклеймер", "важно")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def build_hidden_risks(req: Optional[CheckRequest] = None) -> List[Dict[str, str]]:
    """Risks that public registries/newDB may not fully confirm automatically."""
    age = calculate_age(normalize_dob(req.dob)[0]) if req else None
    age_comment = "Проверить дееспособность и свободное волеизъявление продавца."
    if age is not None and age >= 75:
        age_comment = "Возраст 75+: справка из ПНД и медицинское освидетельствование до сделки обязательны; дополнительно рекомендована видеофиксация волеизъявления продавца."
    elif age is not None and age >= 70:
        age_comment = "Возраст 70+: справка из ПНД и медицинское освидетельствование до сделки желательны, особенно при срочной продаже, занижении цены, единственном жилье или участии представителей/родственников."

    return [
        {"risk": "Супруг / согласие супруга", "why": "Если квартира приобреталась в браке, требуется проверить режим совместной собственности и согласие супруга.", "law": "ст. 34, 35 СК РФ; ст. 253 ГК РФ"},
        {"risk": "Материнский капитал", "why": "Если использовался маткапитал, должны быть выделены доли детям и супругу; нарушение может привести к оспариванию.", "law": "ФЗ №256-ФЗ; ст. 10 ФЗ №256-ФЗ"},
        {"risk": "Доли и преимущественное право", "why": "При продаже доли нужно проверить уведомления сособственников и соблюдение преимущественного права покупки.", "law": "ст. 250 ГК РФ"},
        {"risk": "Наследство", "why": "Проверить срок после наследования, круг наследников, возможные споры и отказников.", "law": "гл. 61-65 ГК РФ"},
        {"risk": "Приватизация", "why": "Проверить отказников от приватизации и лиц с возможным бессрочным правом пользования.", "law": "Закон РФ №1541-1; ст. 19 ФЗ №189-ФЗ"},
        {"risk": "Несовершеннолетние / опека", "why": "Если собственник или пользователь — ребенок, может требоваться разрешение органов опеки.", "law": "ст. 37 ГК РФ; ст. 60 СК РФ"},
        {"risk": "Зарегистрированные лица", "why": "Проверить, кто зарегистрирован в квартире, сроки снятия с учета и лиц, которых нельзя быстро выселить.", "law": "ЖК РФ; ГК РФ"},
        {"risk": "Доверенность", "why": "Если сделка по доверенности, проверить срок, полномочия, отмену доверенности и личную волю собственника.", "law": "ст. 185-189 ГК РФ"},
        {"risk": "Дееспособность / возраст", "why": age_comment, "law": "ст. 21, 29, 30, 177 ГК РФ"},
        {"risk": "Банкротство и оспаривание", "why": "Проверить процедуру, публикации, конкурсную массу и сделки за подозрительный период.", "law": "ст. 61.2, 61.3 ФЗ №127-ФЗ"},
        {"risk": "Исполнительные производства и запреты", "why": "Долги сами по себе не всегда блокируют сделку, но могут привести к запрету регистрации или оспариванию расчетов.", "law": "ФЗ №229-ФЗ"},
        {"risk": "Обременения ЕГРН", "why": "Ипотека, арест, запрет регистрационных действий, аренда или сервитут требуют отдельной схемы сделки.", "law": "ФЗ №218-ФЗ"},
    ]


def build_advance_decision(scoring: Dict[str, Any]) -> Dict[str, str]:
    score = int(scoring.get("score") or 0)
    if score >= 85:
        return {"decision": "Нельзя передавать аванс (задаток) до устранения рисков", "level": "stop", "comment": "Есть критические признаки. Сначала ручная проверка, документы, условия снятия рисков и безопасная схема расчетов."}
    if score >= 60:
        return {"decision": "Только с жесткими защитными условиями", "level": "strict_conditions", "comment": "Аванс возможен только после ручной проверки и с условиями возврата/ответственности продавца."}
    if score >= 35:
        return {"decision": "Осторожно, после проверки документов", "level": "caution", "comment": "Критический стоп-фактор не подтвержден, но до аванса (задатка) нужно закрыть ручные проверки и прописать условия в ПДКП."}
    return {"decision": "Можно рассматривать, но не без документов", "level": "allowed_with_standard_checks", "comment": "По автоматическим источникам критичных рисков не выявлено, но ручная проверка правоустанавливающих документов обязательна."}


def build_data_reliability(checklist: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    reliability = []
    for item in checklist:
        status = item.get("status")
        if status == "ok":
            level = "источник дал понятный результат"
        elif status == "risk":
            level = "источник вернул значимые сведения, требуется юридическая оценка"
        elif status == "manual_check":
            level = "автоматический источник не дал достаточного результата"
        else:
            level = "статус не определен"
        reliability.append({"source": item.get("title", "Источник"), "status": status or "", "reliability": level, "summary": item.get("summary", "")})
    return reliability


def pick_first_field(data: Dict[str, Any], names: List[str]) -> str:
    for name in names:
        value = data.get(name)
        if value not in (None, "", [], {}):
            if isinstance(value, (dict, list)):
                return flatten_text(value)
            return clean_str(value)
    return ""


def court_case_match_score(case: Dict[str, Any], req: CheckRequest, *, source: str) -> Dict[str, Any]:
    """Heuristic matching to reduce false positives from namesakes. INN is internal only."""
    score = 0
    reasons: List[str] = []
    full_fio = fio(req).lower()
    last = req.last.strip().lower()
    first = req.first.strip().lower()
    middle = req.middle.strip().lower()
    dob_ru, dob_iso = normalize_dob(req.dob)
    inn = normalize_inn(req)
    region = str(normalize_region(req) or "")
    text = flatten_text(case).lower()

    if full_fio and full_fio in text:
        score += 45
        reasons.append("полное ФИО найдено в карточке дела")
    else:
        if last and last in text:
            score += 15
            reasons.append("совпала фамилия")
        if first and first in text:
            score += 12
            reasons.append("совпало имя")
        if middle and middle in text:
            score += 10
            reasons.append("совпало отчество")

    if dob_ru and (dob_ru in text or dob_iso in text):
        score += 35
        reasons.append("совпала дата рождения")

    if inn and inn in text:
        score += 45
        reasons.append("совпал ИНН во внутренних данных")

    if region and re.search(rf"\b{re.escape(region)}\b", text):
        score += 5
        reasons.append("есть совпадение по региональному признаку")

    role_text = pick_first_field(case, ["role_text", "role", "role_code", "party_role", "participant_role"]).lower()
    if any(w in role_text for w in ["ответчик", "должник", "заинтересован", "подсудим", "обвиняем"]):
        score += 10
        reasons.append("роль потенциально значима для риска сделки")
    elif any(w in role_text for w in ["истец", "заявитель"]):
        score += 4
        reasons.append("роль менее критична, но требует анализа предмета дела")

    subject = pick_first_field(case, ["category", "case_category", "subject", "claim", "description", "essence", "type", "dispute_subject"]).lower()
    if any(w in subject for w in ["взыск", "долг", "банкрот", "имуще", "недвиж", "залог", "ипотек", "оспар", "недейств"]):
        score += 10
        reasons.append("предмет дела может быть связан с имущественными рисками")

    score = max(0, min(100, score))
    if score >= 80:
        level = "точное совпадение"
    elif score >= 45:
        level = "вероятное совпадение"
    else:
        level = "слабое совпадение"

    return {"court_match_score": score, "match_level": level, "match_reasons": reasons, "source": source}


def normalize_court_case(case: Dict[str, Any], req: CheckRequest, *, source: str) -> Dict[str, Any]:
    match = court_case_match_score(case, req, source=source)
    normalized = {
        "source": source,
        "case_number": pick_first_field(case, ["case_number", "case_id", "number", "num", "Номер дела"]),
        "court": pick_first_field(case, ["court", "court_name", "sud", "name_sud", "instance", "courtTitle"]),
        "date": pick_first_field(case, ["date", "case_date", "start_date", "reg_date", "published", "publication_date", "Дата"]),
        "role": pick_first_field(case, ["role_text", "role", "role_code", "party_role", "participant_role"]),
        "parties": pick_first_field(case, ["parties", "participants", "persons", "sides", "plaintiff", "defendant"]),
        "category": pick_first_field(case, ["category", "case_category", "type", "subject", "claim", "description", "essence", "dispute_subject"]),
        "status": pick_first_field(case, ["status", "state", "result", "decision", "stage"]),
        "amount": pick_first_field(case, ["amount", "claim_amount", "sum", "debt", "price"]),
        "link": pick_first_field(case, ["url", "link", "case_url", "source_url"]),
        "result": pick_first_field(case, ["result", "decision", "resolution", "act_result", "final_decision"]),
        "raw": case,
    }
    normalized.update(match)
    return normalized


def extract_arbitr_cases(data: Any) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("cases") and isinstance(row.get("cases"), list):
                cases.extend([c for c in row.get("cases") or [] if isinstance(c, dict)])
            elif isinstance(row, dict) and (row.get("case_number") or row.get("case_id") or row.get("number")):
                cases.append(row)
    elif isinstance(data, dict):
        if isinstance(data.get("cases"), list):
            cases.extend([c for c in data.get("cases") or [] if isinstance(c, dict)])
        elif data.get("case_number") or data.get("case_id") or data.get("number"):
            cases.append(data)
    return cases


def extract_pravosud_cases(data: Any) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                if isinstance(row.get("cases"), list):
                    cases.extend([c for c in row.get("cases") or [] if isinstance(c, dict)])
                else:
                    cases.append(row)
    elif isinstance(data, dict):
        if isinstance(data.get("cases"), list):
            cases.extend([c for c in data.get("cases") or [] if isinstance(c, dict)])
        else:
            cases.append(data)
    return cases


def court_details_for_screen(cases: List[Dict[str, Any]], limit: int = 7) -> List[str]:
    details: List[str] = []
    for c in cases[:limit]:
        parts = []
        if c.get("case_number"):
            parts.append(f"№ {c.get('case_number')}")
        if c.get("court"):
            parts.append(str(c.get("court")))
        if c.get("date"):
            parts.append(f"дата: {c.get('date')}")
        if c.get("role"):
            parts.append(f"роль: {c.get('role')}")
        if c.get("category"):
            parts.append(f"предмет: {c.get('category')}")
        if c.get("status"):
            parts.append(f"статус/результат: {c.get('status')}")
        if c.get("match_level"):
            parts.append(f"совпадение: {c.get('match_level')} ({c.get('court_match_score')}/100)")
        details.append("; ".join(parts) if parts else flatten_text(c)[:350])
    return details


# ---------- scoring/report ----------

def risk_scoring(checklist: List[Dict[str, Any]], age: Optional[int] = None) -> Dict[str, Any]:
    """Balanced legal scoring.

    Important rule: the score must reflect confirmed or strongly supported risk,
    not merely the amount of text returned by registries. Completed bankruptcy and
    probable court matches are caution factors, not automatic stop-factors.
    """
    score = 0
    factors: List[str] = []

    if age is not None:
        if age >= 85:
            score += 25
            factors.append("Возраст продавца 85+ — критически важна проверка воли и дееспособности (+25)")
        elif age >= 75:
            score += 18
            factors.append("Возраст продавца 75+ — ПНД и медицинское освидетельствование обязательны (+18)")
        elif age >= 70:
            score += 10
            factors.append("Возраст продавца 70+ — ПНД и медицинское освидетельствование желательны (+10)")

    for item in checklist:
        title = item.get("title", "Источник")
        status = item.get("status")

        if status == "manual_check":
            pts = 4 if ("ГАС" in title or "суд" in title.lower()) else 6
            score += pts
            factors.append(f"{title}: требуется ручная проверка (+{pts})")
            continue

        if status != "risk":
            continue

        if "ЕГРН" in title:
            details_text = " ".join(item.get("details") or []).lower()
            if "запрещение регистрации" in details_text or "запрет" in details_text:
                pts = 85
                factors.append("ЕГРН: выявлен запрет/ограничение регистрационных действий (+85)")
            else:
                pts = 60
                factors.append("ЕГРН: выявлены ограничения/обременения без признака регистрационного запрета (+60)")
            score += pts

        elif "ФССП" in title:
            actual = ((item.get("data") or {}).get("actual_debt") or 0) if isinstance(item.get("data"), dict) else 0
            if actual > 1_000_000:
                pts = 40
            elif actual > 300_000:
                pts = 30
            elif actual > 50_000:
                pts = 20
            elif actual > 0:
                pts = 12
            else:
                pts = 6
            score += pts
            factors.append(f"ФССП: активные/неоднозначные ИП, актуальная сумма {rub(actual)} (+{pts})")

        elif "Банкрот" in title:
            bdata = item.get("data") or {}
            bstatus = bdata.get("bankruptcy_status") if isinstance(bdata, dict) else None
            months = bdata.get("months_after_latest") if isinstance(bdata, dict) else None
            property_words = bool(bdata.get("property_related_words")) if isinstance(bdata, dict) else False

            if bstatus == "active":
                pts = 75
                factors.append("Банкротство: есть признаки действующей/незавершенной процедуры (+75)")
            elif months is not None and months < 12:
                pts = 42
                factors.append("Банкротство: после значимой публикации прошло менее 1 года (+42)")
            elif months is not None and months < 36:
                pts = 32
                factors.append("Банкротство: после значимой публикации прошло менее 3 лет (+32)")
            elif property_words:
                pts = 28
                factors.append("Банкротство: процедура выглядит завершенной, но есть имущественные формулировки в публикациях (+28)")
            elif bstatus == "completed":
                pts = 20
                factors.append("Банкротство: процедура выглядит завершенной, нужен ручной контроль документов (+20)")
            else:
                pts = 24
                factors.append("Банкротство: сведения найдены, статус требует ручной проверки (+24)")
            score += pts

        elif "Арбитраж" in title or "ГАС" in title:
            cases = item.get("data") if isinstance(item.get("data"), list) else []
            strong = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "точное совпадение"])
            probable = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "вероятное совпадение"])
            weak = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "слабое совпадение"])

            if strong:
                pts = 25 + min(20, strong * 7 + probable * 2)
            elif probable:
                pts = 10 + min(12, probable * 2)
            else:
                pts = 4

            score += pts
            source_name = "Арбитражные суды" if "Арбитраж" in title else "ГАС Правосудие"
            factors.append(f"{source_name}: точных {strong}, вероятных {probable}, слабых {weak} (+{pts})")

        elif "Паспорт" in title:
            score += 40
            factors.append("Паспорт МВД: выявлен риск по действительности документа (+40)")
        else:
            score += 18
            factors.append(f"{title}: выявлен риск (+18)")

    fssp_flag = any("ФССП" in x.get("title", "") and x.get("status") == "risk" for x in checklist)
    bankrupt_flag = any("Банкрот" in x.get("title", "") and x.get("status") == "risk" for x in checklist)
    egrn_flag = any("ЕГРН" in x.get("title", "") and x.get("status") == "risk" for x in checklist)

    if fssp_flag and bankrupt_flag:
        score += 10
        factors.append("Комбинация рисков: ФССП + банкротство (+10)")
    if fssp_flag and egrn_flag:
        score += 15
        factors.append("Комбинация рисков: ФССП + ограничения по объекту (+15)")
    if bankrupt_flag and egrn_flag:
        score += 12
        factors.append("Комбинация рисков: банкротство + ограничения ЕГРН (+12)")

    # Critical trigger should be rare. It is not used for completed bankruptcy
    # or probable court matches.
    critical = False
    for item in checklist:
        title = item.get("title", "")
        details_text = " ".join(item.get("details") or []).lower()

        if "ЕГРН" in title and item.get("status") == "risk":
            if "запрещение регистрации" in details_text or "запрет" in details_text:
                critical = True

        if "Банкрот" in title:
            data = item.get("data") or {}
            if isinstance(data, dict) and data.get("bankruptcy_status") == "active":
                critical = True

        if "ФССП" in title:
            actual = ((item.get("data") or {}).get("actual_debt") or 0) if isinstance(item.get("data"), dict) else 0
            # Large debt is critical only together with object restrictions.
            if actual > 500_000 and egrn_flag:
                critical = True

    if age is not None and age >= 85:
        critical = True

    if critical:
        score = max(score, 85)
        factors.append("Обнаружен подтвержденный критический триггер — аванс (задаток) только после устранения риска")

    score = max(0, min(100, int(score)))
    if score >= 85:
        level = "ОПАСНО"
        label = "ОПАСНО при самостоятельной сделке"
        conclusion = "Аванс (задаток) и сделку нельзя проводить без ручной юридической проверки, устранения ключевых рисков и безопасной схемы расчетов."
    elif score >= 60:
        level = "РИСКОВАННО"
        label = "РИСКОВАННО без контроля условий сделки"
        conclusion = "Сделку можно рассматривать только после уточнения рисков, проверки документов и жестких защитных условий в авансе (задатке)/ПДКП."
    elif score >= 35:
        level = "РИСКОВАННО"
        label = "РИСКОВАННО, требуется дополнительная проверка"
        conclusion = "Критический стоп-фактор по автоматическим данным не подтвержден, но до аванса (задатка) нужно закрыть ручные проверки и внести защитные условия в документы."
    else:
        level = "ДОПУСТИМО"
        label = "ДОПУСТИМО к дальнейшему рассмотрению"
        conclusion = "По автоматическим источникам критичных рисков не выявлено, но отчет не заменяет ручную юридическую проверку документов."

    return {"score": score, "max_score": 100, "level": level, "label": label, "conclusion": conclusion, "factors": factors}


def build_recommendations(checklist: List[Dict[str, Any]], req: Optional[CheckRequest] = None) -> List[Dict[str, str]]:
    recs: List[Dict[str, str]] = []
    if req is not None:
        age = calculate_age(normalize_dob(req.dob)[0])
        if age is not None and age >= 75:
            recs.append({"priority": "critical", "title": "Проверка дееспособности обязательна", "text": "Возраст 75+. До сделки обязательны справка из ПНД и медицинское освидетельствование; рекомендована видеофиксация волеизъявления продавца, чтобы снизить риск последующего спора по ст. 177 ГК РФ."})
        elif age is not None and age >= 70:
            recs.append({"priority": "high", "title": "Проверка дееспособности желательна", "text": "Возраст 70+. До сделки желательно получить справку из ПНД и провести медицинское освидетельствование, особенно если цена ниже рынка, сделка срочная или участвуют представители/родственники."})

    by_title = {x.get("title", ""): x for x in checklist}
    egrn = by_title.get("ЕГРН / Росреестр")
    fssp = by_title.get("ФССП")
    bankruptcy = by_title.get("Банкротство / Федресурс")
    arbitr = by_title.get("Арбитражные суды")
    pravosud = by_title.get("Суды общей юрисдикции / ГАС Правосудие")

    if egrn and egrn.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки", "text": "По ЕГРН выявлены ограничения/обременения. До аванса (задатка) нужно получить основание ограничения и прописать обязанность продавца снять его в конкретный срок."})
        recs.append({"priority": "critical", "title": "Не передавать деньги напрямую продавцу", "text": "При запрете регистрации использовать нотариальный депозит или аккредитив с раскрытием денег только после снятия ограничения и регистрации перехода права."})
    if fssp and fssp.get("status") == "risk":
        actual = ((fssp.get("data") or {}).get("actual_debt") or 0) if isinstance(fssp.get("data"), dict) else 0
        recs.append({"priority": "high", "title": "Закрыть активные ИП до сделки или через контролируемые расчеты", "text": f"Актуальная сумма по активным/неоднозначным ИП: {rub(actual)}. В ПДКП прописать порядок погашения и последствия неснятия ограничений."})
    if bankruptcy and bankruptcy.get("status") == "risk":
        bdata = bankruptcy.get("data") or {}
        bstatus = bdata.get("bankruptcy_status") if isinstance(bdata, dict) else None
        months = bdata.get("months_after_latest") if isinstance(bdata, dict) else None
        if bstatus == "active":
            recs.append({"priority": "critical", "title": "Не вносить аванс без анализа банкротного дела", "text": "По банкротству есть признаки действующей или незавершенной процедуры. Нужно проверить карточку дела, полномочия продавца и позицию финансового управляющего."})
        elif months is not None and months < 36:
            recs.append({"priority": "high", "title": "Проверить завершенное банкротство до аванса (задатка)", "text": "После значимой публикации по банкротству прошло менее 3 лет. Нужно проверить судебный акт о завершении процедуры, отчет финансового управляющего и связь квартиры с конкурсной массой."})
        else:
            recs.append({"priority": "high", "title": "Разобрать банкротный риск", "text": "Нужно проверить даты процедуры, статус завершения, освобождение от обязательств, публикации Федресурса и риск оспаривания сделки."})
    if arbitr and arbitr.get("status") == "risk":
        recs.append({"priority": "high", "title": "Разобрать арбитражные совпадения", "text": "Нужно вручную проверить предмет спора, роль лица и надежность совпадения. Вероятные совпадения не считать установленными делами продавца без идентификации."})
    if pravosud and pravosud.get("status") == "risk":
        recs.append({"priority": "medium", "title": "Разобрать судебные совпадения", "text": "Нужно вручную идентифицировать совпадения по ФИО: сверить дату рождения, регион, стороны, предмет дела и результат. Без точной идентификации не считать записи установленными делами продавца."})
    if any(x.get("status") == "manual_check" for x in checklist):
        recs.append({"priority": "medium", "title": "Закрыть ручные проверки до аванса (задатка)", "text": "Все источники со статусом «требуется ручная проверка» нужно проверить вручную до передачи денег."})
    recs.append({"priority": "high", "title": "Авансовое соглашение / соглашение о задатке делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса (задатка) и ответственность при неподтверждении данных."})
    return recs


def build_registry_data(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys = ["passport", "fssp", "bankruptcy", "arbitr", "pravosud", "egrn"]
    out: Dict[str, Any] = {}
    for key, item in zip(keys, checklist):
        clean = {
            "title": item.get("title"),
            "status": item.get("status"),
            "summary": item.get("summary"),
            "details": item.get("details") or [],
        }
        if item.get("data") is not None:
            if key == "fssp" and isinstance(item.get("data"), dict):
                clean.update(item["data"])
            elif key == "egrn" and isinstance(item.get("data"), dict):
                clean["object"] = item.get("data")
                clean["encumbrances"] = item.get("data", {}).get("encumbrances") or []
            else:
                clean["items"] = item.get("data")
        out[key] = clean
    return out


def build_screen_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> Dict[str, Any]:
    risks = [x for x in checklist if x.get("status") == "risk"]
    oks = [x for x in checklist if x.get("status") == "ok"]
    manual = [x for x in checklist if x.get("status") == "manual_check"]
    return {
        "headline": scoring.get("label"),
        "score": scoring.get("score"),
        "max_score": 100,
        "conclusion": scoring.get("conclusion"),
        "key_risks": [{"title": x.get("title"), "summary": x.get("summary"), "details": (x.get("details") or [])[:4]} for x in risks],
        "positive_factors": [{"title": x.get("title"), "summary": x.get("summary")} for x in oks],
        "manual_checks": [{"title": x.get("title"), "summary": x.get("summary"), "url": x.get("manual_check_url")} for x in manual],
        "recommendations": recs,
        "advance_decision": build_advance_decision(scoring),
        "data_reliability": build_data_reliability(checklist),
        "hidden_risks": build_hidden_risks(req),
        "seller": {"fio": fio(req), "dob": normalize_dob(req.dob)[0], "age": calculate_age(normalize_dob(req.dob)[0]), "inn_provided": bool(normalize_inn(req))},
        "property": normalize_property(req),
    }


def build_local_legal_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> str:
    risks = [x for x in checklist if x.get("status") == "risk"]
    oks = [x for x in checklist if x.get("status") == "ok"]
    manual = [x for x in checklist if x.get("status") == "manual_check"]
    seller = fio(req)
    dob_ru = normalize_dob(req.dob)[0]
    prop = normalize_property(req)["query"]
    lines = []
    lines.append("1. Краткий вывод")
    lines.append(f"Оценка при самостоятельной сделке без сопровождения: {scoring.get('label')} ({scoring.get('score')}/100). {scoring.get('conclusion')}")
    lines.append("")
    lines.append("2. Что проверено")
    lines.append(f"Продавец: {seller}. Дата рождения: {dob_ru}. ИНН: {'передан' if normalize_inn(req) else 'не передан'}. Объект: {prop}.")
    lines.append(f"Проверено без явных рисков: {len(oks)}. Рисков: {len(risks)}. Требуют ручной проверки: {len(manual)}.")
    lines.append("")
    lines.append("3. Скрытые риски, которые нужно проверить по документам")
    for h in build_hidden_risks(req):
        lines.append(f"- {h.get('risk')}: {h.get('why')} ({h.get('law')})")
    lines.append("")
    lines.append("4. Основные риски")
    if risks:
        for r in risks:
            lines.append(f"- {r.get('title')}: {r.get('summary')}")
    else:
        lines.append("- По автоматическим источникам явные риски не выявлены.")
    lines.append("")
    lines.append("5. Что говорит в пользу сделки")
    if oks:
        for o in oks:
            lines.append(f"- {o.get('title')}: {o.get('summary')}")
    else:
        lines.append("- Нет блоков, которые можно считать полностью подтвержденными без замечаний.")
    lines.append("")
    lines.append("6. Что обязательно сделать до аванса (задатка)")
    for rec in recs:
        lines.append(f"- {rec.get('title')}: {rec.get('text')}")
    lines.append("")
    lines.append("7. Безопасная схема расчетов")
    lines.append("При выявленных долгах, запретах или неполных данных не передавать деньги напрямую продавцу. Использовать нотариальный депозит, аккредитив или иную условную схему с раскрытием денег только после выполнения условий.")
    lines.append("")
    lines.append("8. Итоговое заключение")
    lines.append("Отчет не обещает 100% безопасность сделки. При выявленных ограничениях, активных ИП или судебных делах сделка должна проходить только после ручного юридического анализа документов и условий расчетов.")
    return strip_markdown_noise("\n".join(lines))


def build_gigachat_safe_payload(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> Dict[str, Any]:
    """Send the model facts, not a full personal profile.

    PDF can contain the full INN in its header, but the LLM prompt should not include
    INN/passport/DOB. This reduces refusals while preserving case-specific legal logic.
    """
    dob_ru = normalize_dob(req.dob)[0]
    age = calculate_age(dob_ru)
    prop = normalize_property(req)

    safe_checks: List[Dict[str, Any]] = []
    for item in checklist:
        data = item.get("data")
        compact_data: Dict[str, Any] = {}

        if "ФССП" in str(item.get("title")) and isinstance(data, dict):
            compact_data = {
                "all_count": data.get("all_count"),
                "active_count": data.get("active_count"),
                "closed_count": data.get("closed_count"),
                "unknown_count": data.get("unknown_count"),
                "actual_debt": data.get("actual_debt"),
                "active_sum": data.get("active_sum"),
                "closed_sum": data.get("closed_sum"),
            }
        elif "Банкрот" in str(item.get("title")) and isinstance(data, dict):
            compact_data = {
                "bankruptcy_status": data.get("bankruptcy_status"),
                "latest_publication_date": data.get("latest_publication_date"),
                "months_after_latest": data.get("months_after_latest"),
                "property_related_words": data.get("property_related_words"),
            }
        elif "ЕГРН" in str(item.get("title")) and isinstance(data, dict):
            compact_data = {
                "cadNumber": data.get("cadNumber"),
                "objType_text": data.get("objType_text"),
                "purpose_text": data.get("purpose_text"),
                "area": data.get("area"),
                "encumbrances_count": len(data.get("encumbrances") or []) if isinstance(data.get("encumbrances"), list) else 0,
                "rights_count": len(data.get("rights") or []) if isinstance(data.get("rights"), list) else None,
            }
        elif ("Арбитраж" in str(item.get("title")) or "ГАС" in str(item.get("title"))) and isinstance(data, list):
            compact_data = {
                "cases_count": len(data),
                "exact_matches": len([c for c in data if isinstance(c, dict) and c.get("match_level") == "точное совпадение"]),
                "probable_matches": len([c for c in data if isinstance(c, dict) and c.get("match_level") == "вероятное совпадение"]),
                "weak_matches": len([c for c in data if isinstance(c, dict) and c.get("match_level") == "слабое совпадение"]),
                "cases": [
                    {
                        "case_number": c.get("case_number"),
                        "court": c.get("court"),
                        "date": c.get("date"),
                        "role": c.get("role"),
                        "category": c.get("category"),
                        "status": c.get("status"),
                        "amount": c.get("amount"),
                        "result": c.get("result"),
                        "match_level": c.get("match_level"),
                        "court_match_score": c.get("court_match_score"),
                    }
                    for c in data[:10] if isinstance(c, dict)
                ],
            }

        safe_checks.append({
            "title": item.get("title"),
            "status": item.get("status"),
            "summary": item.get("summary"),
            "details": (item.get("details") or [])[:8],
            "data_summary": compact_data,
        })

    age_factor = "возрастной риск по автоматическим данным не выявлен"
    if age is not None and age >= 75:
        age_factor = "75+: справка из ПНД и медицинское освидетельствование обязательны; рекомендована видеофиксация волеизъявления"
    elif age is not None and age >= 70:
        age_factor = "70+: справка из ПНД и медицинское освидетельствование желательны до сделки"

    return {
        "seller": {
            "fio": fio(req),
            "age": age,
            "age_factor": age_factor,
            "inn": "не передавать в юридический анализ; ИНН использован только для автоматической проверки",
            "passport": "не передавать в юридический анализ",
            "dob": "не передавать в юридический анализ",
        },
        "property": {
            "query": prop.get("query"),
            "type": prop.get("type"),
        },
        "checklist": safe_checks,
        "risk_scoring": scoring,
        "advance_decision": build_advance_decision(scoring),
        "hidden_risks_manual_checklist": build_hidden_risks(req),
        "recommendations": recs,
    }


def is_gigachat_refusal(text: str) -> bool:
    t = (text or "").lower()
    refusal_markers = [
        "чувствительными темами",
        "временно ограничены",
        "не могу помочь",
        "не могу предоставить",
        "во избежание неправильного толкования",
        "я не могу",
        "извините",
    ]
    return any(marker in t for marker in refusal_markers)


def redact_sensitive_from_ai_text(text: str) -> str:
    """Keep the legal conclusion usable even if the model echoes forbidden identifiers."""
    if not text:
        return ""
    text = re.sub(r"\b\d{12}\b", "ИНН использован для проверки", text)
    text = re.sub(r"\b\d{4}\s?\d{6}\b", "паспортные данные скрыты", text)
    return text.strip()



def normalize_legal_report_format(text: str) -> str:
    """Normalize LLM output into readable legal sections before PDF rendering."""
    if not text:
        return ""
    s = strip_markdown_noise(text)

    # Remove literal template arrows if the model echoed instructions instead of using them naturally.
    s = s.replace("Факт → Юридическое значение → Риск для покупателя → Что сделать до аванса", "")
    s = s.replace("Факт -> Юридическое значение -> Риск для покупателя -> Что сделать до аванса", "")

    # Ensure major sections start on separate lines even if GigaChat returned a continuous paragraph.
    section_titles = [
        "Краткий вывод",
        "Надежность полученных данных",
        "Риски продавца",
        "Риски объекта",
        "Скрытые риски, которые нужно проверить документами",
        "Можно ли передавать аванс (задаток)",
        "Что проверить до аванса (задатка)",
        "Что прописать в авансовом соглашении / соглашении о задатке / ПДКП",
        "Безопасная схема расчетов",
        "Итоговое заключение",
        "Важно",
    ]
    for title in section_titles:
        # If a title appears in the middle of a sentence-like line, make it a new paragraph.
        s = re.sub(rf"(?<!^)(?<!\n)(\s*)({re.escape(title)})(\s*)", rf"\n\n\2\n", s)

    # Number unnumbered headings for PDF parser consistency.
    for i, title in enumerate(section_titles, start=1):
        s = re.sub(rf"(?m)^\s*(?:{i}\.\s*)?{re.escape(title)}\s*$", f"{i}. {title}", s)

    # Normalize list markers.
    s = re.sub(r"(?m)^\s*[-–—]\s+", "• ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def enforce_ai_report_text(text: str, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], req: CheckRequest) -> str:
    """Post-process LLM text so it cannot override backend facts."""
    if not text:
        return ""

    text = normalize_legal_report_format(text)

    egrn_ok = any("ЕГРН" in x.get("title", "") and x.get("status") == "ok" for x in checklist)
    fssp_ok = any("ФССП" in x.get("title", "") and x.get("status") == "ok" for x in checklist)
    passport_ok = any("Паспорт" in x.get("title", "") and x.get("status") == "ok" for x in checklist)
    age = calculate_age(normalize_dob(req.dob)[0])

    # Correct common overstatements from generative output.
    replacements = {
        "судебные дела продавца": "судебные совпадения, требующие идентификации",
        "продавец участвовал в судебных делах": "найдены судебные совпадения, требующие идентификации",
        "вероятности участия продавца": "вероятные совпадения по ФИО",
        "предположительно затрагивающих интересы последнего": "требующих ручной проверки роли и предмета",
        "конфликтах имущественного характера": "судебных материалах, требующих ручной идентификации",
        "сделка крайне опасна": "сделка требует усиленной ручной проверки",
        "категорически недопустима": "недопустима без предварительной ручной проверки и защитных условий",
        "убедиться, что жилье приобреталось с использованием средств материнского капитала": "проверить, не приобреталось ли жилье с использованием средств материнского капитала",
        "использовалось с использованием материнского капитала": "не использовалось ли с применением материнского капитала",
        "высокую степень риска данной сделки": "уровень риска данной сделки согласно скорингу",
        "высокая степень риска данной сделки": "уровень риска данной сделки согласно скорингу",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    if egrn_ok:
        # Do not remove the correct sentence "обременения не выявлены"; replace only overstatements.
        bad_patterns = [
            r"имеются\s+обременения",
            r"имеются\s+ограничения",
            r"на\s+объекте\s+зарегистрированы\s+ограничения",
            r"на\s+объекте\s+зарегистрированы\s+обременения",
            r"наличие\s+зарегистрированных\s+ограничений",
            r"наличие\s+зарегистрированных\s+обременений",
            r"доказательства\s+отсутствия\s+обременений\s+и\s+ограничений",
        ]
        for pat in bad_patterns:
            text = re.sub(pat, "по данным ЕГРН явные ограничения и обременения не выявлены", text, flags=re.IGNORECASE)

    if fssp_ok:
        text = re.sub(r"активн\w+\s+исполнительн\w+\s+производств\w+", "активные исполнительные производства по данным проверки не выявлены", text, flags=re.IGNORECASE)
        text = re.sub(r"активн\w+\s+долг\w+", "активные долги по данным ФССП не выявлены", text, flags=re.IGNORECASE)

    if passport_ok:
        text = text.replace("паспортные данные отсутствуют", "реквизиты паспорта не раскрываются в тексте заключения")
        text = text.replace("паспортные данные не проверены", "паспорт проверен через МВД, реквизиты не раскрываются в тексте заключения")

    if age is None or age < 70:
        # Avoid applying age rules to a young seller.
        text = re.sub(r"(?im)^.*ПНД.*$", "", text)
        text = re.sub(r"(?im)^.*медицинск\w+\s+освидетельствован.*$", "", text)
        text = re.sub(r"(?im)^.*видеофиксац.*$", "", text)

    # Backend score is the single source of truth.
    label = str(scoring.get("label") or "")
    score = scoring.get("score")
    if label and score is not None:
        if int(score) < 85:
            text = re.sub(r"(?i)сделка\s+представляет\s+высокий\s+риск", f"сделка имеет оценку: {label.lower()} ({score}/100)", text)
            text = re.sub(r"(?i)относится\s+к\s+категории\s+высокорискованных", f"имеет оценку: {label.lower()} ({score}/100)", text)
            text = re.sub(r"(?i)передача\s+аванса\s+\(задатка\)\s+категорически\s+недопустима", "передача аванса (задатка) недопустима без предварительной ручной проверки и защитных условий", text)
            text = re.sub(r"(?i)передача\s+аванса\s+невозможна", "передача аванса возможна только после ручной проверки и с защитными условиями", text)

    text = normalize_legal_report_format(text)
    return text.strip()



async def maybe_gigachat_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> str:
    fallback = build_local_legal_report(req, checklist, scoring, recs)
    if not (GIGACHAT_AVAILABLE and GIGACHAT_CREDENTIALS):
        return fallback

    payload = build_gigachat_safe_payload(req, checklist, scoring, recs)
    prompt = (
    "Ты практикующий юрист по недвижимости в Санкт-Петербурге с опытом более 20 лет. "
    "Ты готовишь платное юридическое заключение для покупателя квартиры.\n\n"

    "ВАЖНО:\n"
    "- Не придумывай факты\n"
    "- Не усиливай риски без оснований\n"
    "- Не противоречь входным данным\n"
    "- Не делай предположений без пометки, что это проверка\n\n"

    "КЛЮЧЕВОЕ ПРАВИЛО:\n"
    "Ты не оцениваешь риск — риск уже посчитан системой.\n"
    "Ты только объясняешь его.\n\n"

    "СТРОГО ЗАПРЕЩЕНО:\n"
    "- писать 'сделка запрещена', если score < 85\n"
    "- писать 'категорически нельзя', если нет ЕГРН запрета или активного банкротства\n"
    "- называть судебные совпадения установленными фактами\n"
    "- писать обременения, если ЕГРН = ok\n"
    "- усиливать риск сверх указанного score\n\n"

    "ИНТЕРПРЕТАЦИЯ SCORE:\n"
    "0–34 → допустимо\n"
    "35–59 → осторожно\n"
    "60–84 → допустимо только при условиях\n"
    "85–100 → высокий риск, возможен отказ от сделки\n\n"

    "ОСОБО ВАЖНО:\n"
    "- завершенное банкротство не является запретом сделки\n"
    "- судебные совпадения без идентификации — это не факт\n"
    "- отсутствие долгов ФССП снижает общий риск\n"
    "- чистый ЕГРН означает отсутствие обременений\n\n"

    "СТИЛЬ:\n"
    "Пиши как юрист, который объясняет клиенту:\n"
    "- уверенно\n"
    "- спокойно\n"
    "- без воды\n"
    "- без шаблонных фраз\n\n"

    "НЕ ПИШИ СУХО:\n"
    "Не используй формат 'Факт → Риск → Действие'\n"
    "Пиши живыми абзацами, но структурированно\n\n"

    "СТРУКТУРА ОТЧЕТА:\n\n"

    "1. КРАТКИЙ ВЫВОД\n"
    "Коротко объясни ситуацию: можно ли рассматривать сделку и на каких условиях.\n\n"

    "2. ЧТО В ВЫГЛЯДИТ НОРМАЛЬНО\n"
    "Кратко перечисли, что снижает риск.\n\n"

    "3. НА ЧТО ОБРАТИТЬ ВНИМАНИЕ\n"
    "Только реальные риски, без нагнетания.\n\n"

    "4. ЧТО НУЖНО ПРОВЕРИТЬ ДО АВАНСА (ЗАДАТКА)\n"
    "Конкретные действия.\n\n"

    "5. КАК ПРАВИЛЬНО ПЕРЕДАВАТЬ АВАНС (ЗАДАТОК)\n"
    "Безопасная логика.\n\n"

    "6. ИТОГОВОЕ ЗАКЛЮЧЕНИЕ\n"
    "Финальный вывод без противоречий.\n\n"

    "ЮРИДИЧЕСКИЕ ССЫЛКИ:\n"
    "Добавляй статьи закона только там, где это реально усиливает вывод:\n"
    "- ст. 166–181 ГК РФ\n"
    "- ст. 35 СК РФ\n"
    "- ст. 250 ГК РФ\n"
    "- ст. 37 ГК РФ\n"
    "- ст. 61.2–61.3 ФЗ-127\n\n"

    "ДАННЫЕ ДЛЯ АНАЛИЗА:\n"
    f"Score: {scoring.get('score')}/100\n"
    f"Уровень: {scoring.get('label')}\n"
    f"Вывод системы: {scoring.get('conclusion')}\n\n"
    + json.dumps(payload, ensure_ascii=False)
)

    try:
        def _call() -> str:
            with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                resp = giga.chat(prompt)
                return (resp.choices[0].message.content or "").strip()

        text = redact_sensitive_from_ai_text(await asyncio.to_thread(_call))
        text = enforce_ai_report_text(text, checklist, scoring, req)
        if not text or is_gigachat_refusal(text):
            return fallback
        return text
    except Exception:
        return fallback



def pdf_report_blocks(text: str) -> List[Tuple[str, str]]:
    """Return (kind, text) blocks for readable PDF rendering."""
    text = normalize_legal_report_format(text or "")
    text = re.sub(r"(?m)^\s*[-–—]\s+", "• ", text)

    # Additional protection: if model still put heading and body on one line,
    # split after known numbered headings.
    section_titles = [
        "Краткий вывод",
        "Надежность полученных данных",
        "Риски продавца",
        "Риски объекта",
        "Скрытые риски, которые нужно проверить документами",
        "Можно ли передавать аванс (задаток)",
        "Что проверить до аванса (задатка)",
        "Что прописать в авансовом соглашении / соглашении о задатке / ПДКП",
        "Безопасная схема расчетов",
        "Итоговое заключение",
        "Важно",
    ]
    for i, title in enumerate(section_titles, start=1):
        text = re.sub(rf"(?m)^({i}\.\s*{re.escape(title)})\s+(.+)$", rf"\1\n\2", text)

    lines = [ln.strip() for ln in text.splitlines()]
    blocks: List[Tuple[str, str]] = []
    buf: List[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            blocks.append(("p", " ".join(buf).strip()))
            buf = []

    for ln in lines:
        if not ln:
            flush()
            continue
        if re.match(r"^\d{1,2}\.\s+", ln):
            flush()
            blocks.append(("h", ln))
        elif ln.startswith("•"):
            flush()
            blocks.append(("bullet", ln[1:].strip()))
        else:
            buf.append(ln)
    flush()
    return blocks

# ---------- PDF ----------

def register_pdf_font() -> str:
    if not REPORTLAB_AVAILABLE:
        return "Helvetica"
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("AppFont", path))
                return "AppFont"
        except Exception:
            pass
    return "Helvetica"


def p(text: Any) -> str:
    # reportlab basic escaping
    s = str(text or "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")



def build_score_bar(score: int, level: str = "", font_name: str = "Helvetica") -> Any:
    """Premium PDF scoring scale with pointer and score inside the scale."""
    score = max(0, min(100, int(score or 0)))
    width = 180 * mm
    height = 25 * mm
    left = 8 * mm
    bar_y = 9 * mm
    bar_w = 164 * mm
    bar_h = 8 * mm
    x = left + (score / 100) * bar_w

    d = Drawing(width, height)

    # Soft background / border
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor("#FFFFFF"), strokeColor=colors.HexColor("#D8DEE5"), strokeWidth=0.4))

    # Risk zones
    d.add(Rect(left, bar_y, bar_w * 0.35, bar_h, fillColor=colors.HexColor("#2E7D59"), strokeWidth=0))
    d.add(Rect(left + bar_w * 0.35, bar_y, bar_w * 0.25, bar_h, fillColor=colors.HexColor("#D4A373"), strokeWidth=0))
    d.add(Rect(left + bar_w * 0.60, bar_y, bar_w * 0.40, bar_h, fillColor=colors.HexColor("#8B1E1E"), strokeWidth=0))

    # Outer line
    d.add(Rect(left, bar_y, bar_w, bar_h, fillColor=None, strokeColor=colors.HexColor("#0F3D56"), strokeWidth=0.45))

    # Pointer inside the scale
    d.add(Line(x, bar_y - 1.5 * mm, x, bar_y + bar_h + 2.5 * mm, strokeColor=colors.HexColor("#111827"), strokeWidth=1.15))
    d.add(Polygon(
        [x, bar_y + bar_h + 3.4 * mm, x - 2.4 * mm, bar_y + bar_h + 7.2 * mm, x + 2.4 * mm, bar_y + bar_h + 7.2 * mm],
        fillColor=colors.HexColor("#111827"),
        strokeColor=colors.HexColor("#111827"),
        strokeWidth=0,
    ))

    # Score number inside the bar
    marker_w = 14 * mm
    marker_x = min(max(x - marker_w / 2, left), left + bar_w - marker_w)
    d.add(Rect(marker_x, bar_y + 0.8 * mm, marker_w, bar_h - 1.6 * mm, fillColor=colors.HexColor("#FFFFFF"), strokeColor=colors.HexColor("#111827"), strokeWidth=0.45))
    d.add(String(marker_x + 4.5 * mm, bar_y + 2.35 * mm, str(score), fontName=font_name, fontSize=8.5, fillColor=colors.HexColor("#111827")))

    # Labels
    d.add(String(left, 3 * mm, "0", fontName=font_name, fontSize=6.8, fillColor=colors.HexColor("#6E7F8D")))
    d.add(String(left + bar_w - 6 * mm, 3 * mm, "100", fontName=font_name, fontSize=6.8, fillColor=colors.HexColor("#6E7F8D")))
    if level:
        d.add(String(left, 20.2 * mm, str(level).upper(), fontName=font_name, fontSize=9.2, fillColor=colors.HexColor("#0F3D56")))

    return d



def build_pdf_bytes(report: Dict[str, Any]) -> bytes:
    if not REPORTLAB_AVAILABLE:
        return ("PDF generation unavailable: reportlab is not installed.\n\n" + json.dumps(report, ensure_ascii=False, indent=2)).encode("utf-8")

    font = register_pdf_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15*mm,
        rightMargin=15*mm,
        topMargin=14*mm,
        bottomMargin=14*mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleRu", fontName=font, fontSize=21, leading=26, alignment=TA_CENTER, spaceAfter=6, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="SubtitleRu", fontName=font, fontSize=9.5, leading=13, alignment=TA_CENTER, textColor=colors.HexColor("#3C4853")))
    styles.add(ParagraphStyle(name="H1Ru", fontName=font, fontSize=14.2, leading=18, spaceBefore=10, spaceAfter=5, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="H2Ru", fontName=font, fontSize=11.5, leading=15, spaceBefore=5, spaceAfter=3, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="TextRu", fontName=font, fontSize=9.7, leading=14.2, spaceAfter=4.5, textColor=colors.HexColor("#111827")))
    styles.add(ParagraphStyle(name="SmallRu", fontName=font, fontSize=8.4, leading=11.5, textColor=colors.HexColor("#3C4853")))
    styles.add(ParagraphStyle(name="TableHeadRu", fontName=font, fontSize=8.8, leading=11.5, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="BulletRu", fontName=font, fontSize=9.2, leading=13.2, leftIndent=4*mm, firstLineIndent=-2*mm, textColor=colors.HexColor("#111827")))
    styles.add(ParagraphStyle(name="BadgeRu", fontName=font, fontSize=13, leading=16, alignment=TA_CENTER, textColor=colors.white))
    styles.add(ParagraphStyle(name="BoxTitleRu", fontName=font, fontSize=10.2, leading=13, textColor=colors.HexColor("#0F3D56")))

    story: List[Any] = []
    scoring = report.get("risk_scoring") or {}
    checklist = report.get("checklist") or []
    recs = report.get("recommendations") or []
    norm = report.get("normalized_input") or {}
    advance = report.get("advance_decision") or build_advance_decision(scoring)
    hidden_risks = report.get("hidden_risks") or []

    def add_card(title: str, body: Any, *, bg: str = "#FFFFFF", border: str = "#D8DEE5") -> None:
        content: List[Any] = []
        if title:
            content.append(Paragraph(p(title), styles["BoxTitleRu"]))
        if isinstance(body, list):
            for x in body:
                content.append(Paragraph(p(x), styles["SmallRu"]))
        else:
            content.append(Paragraph(p(body), styles["SmallRu"]))
        tbl = Table([[content]], colWidths=[180*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
            ("BOX", (0, 0), (-1, -1), 0.45, colors.HexColor(border)),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 5))

    # Cover / header
    story.append(Paragraph("Комплексный юридический отчет по продавцу и объекту недвижимости", styles["TitleRu"]))
    story.append(Paragraph("проверка продавца, объекта, судебных и имущественных рисков", styles["SubtitleRu"]))
    story.append(Paragraph(f"Дата формирования: {p(report.get('created_at'))}", styles["SubtitleRu"]))
    story.append(Spacer(1, 8))

    score = int(scoring.get("score") or 0)
    level = (scoring.get("level") or "ОЦЕНКА РИСКА").upper()
    label = scoring.get("label") or level
    badge_color = "#8B1E1E" if score >= 85 else ("#B7791F" if score >= 35 else "#1F7A4D")

    story.append(build_score_bar(score, level, font))
    story.append(Spacer(1, 5))

    badge = Table([[Paragraph(f"{p(level)}<br/>{score}/100", styles["BadgeRu"])]], colWidths=[180*mm])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(badge_color)),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(badge_color)),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(badge)
    story.append(Spacer(1, 7))

    add_card(
        "Главный вывод",
        scoring.get("conclusion") or "Итоговый вывод не сформирован.",
        bg="#F5F3EF",
        border="#E4D9C8",
    )
    add_card(
        "Решение по авансу (задатку)",
        f"{advance.get('decision')}. {advance.get('comment')}",
        bg="#FFF8EA" if score >= 35 else "#EDF7F1",
        border="#E8C98A" if score >= 35 else "#B8D9C1",
    )

    # Input data
    story.append(Paragraph("1. Данные проверки", styles["H1Ru"]))
    seller = norm
    prop = norm.get("property") or {}
    seller_fio = " ".join([seller.get("last", ""), seller.get("first", ""), seller.get("middle", "")]).strip()
    info_rows = [
        [Paragraph("Продавец", styles["TableHeadRu"]), Paragraph(p(seller_fio), styles["TextRu"])],
        [Paragraph("Дата рождения", styles["TableHeadRu"]), Paragraph(p(seller.get("dob")), styles["TextRu"])],
        [Paragraph("ИНН", styles["TableHeadRu"]), Paragraph(p(seller.get("inn") or ("передан" if seller.get("inn_provided") else "не передан")), styles["TextRu"])],
        [Paragraph("Объект", styles["TableHeadRu"]), Paragraph(p(prop.get("query")), styles["TextRu"])],
    ]
    info_table = Table(info_rows, colWidths=[42*mm, 138*mm])
    info_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F3EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 5))

    # Checklist as premium matrix
    story.append(Paragraph("2. Карта автоматических проверок", styles["H1Ru"]))
    rows = [[Paragraph("Источник", styles["TableHeadRu"]), Paragraph("Статус", styles["TableHeadRu"]), Paragraph("Вывод", styles["TableHeadRu"])]]
    for item in checklist:
        status = {"ok": "Проверено", "risk": "Требует внимания", "manual_check": "Ручная проверка"}.get(item.get("status"), item.get("status"))
        rows.append([
            Paragraph(p(item.get("title")), styles["SmallRu"]),
            Paragraph(p(status), styles["SmallRu"]),
            Paragraph(p(item.get("summary")), styles["SmallRu"]),
        ])
    tbl = Table(rows, colWidths=[43*mm, 34*mm, 103*mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F5F8")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0F3D56")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)

    # Key risks
    story.append(Paragraph("3. Ключевые юридические риски", styles["H1Ru"]))
    risks = [x for x in checklist if x.get("status") == "risk"]
    if risks:
        for item in risks:
            lines = [item.get("summary") or ""]
            for d in (item.get("details") or [])[:5]:
                lines.append(f"• {d}")
            add_card(item.get("title") or "Риск", lines, bg="#FFFDF8", border="#E6D3A4")
    else:
        add_card("Риски по автоматическим источникам", "Явные риски по автоматическим источникам не выявлены.", bg="#EDF7F1", border="#B8D9C1")

    # Recommendations
    story.append(Paragraph("4. Что сделать до аванса (задатка)", styles["H1Ru"]))
    rec_rows = [[Paragraph("Приоритет", styles["TableHeadRu"]), Paragraph("Действие", styles["TableHeadRu"]), Paragraph("Зачем это нужно", styles["TableHeadRu"])]]
    for rec in recs[:8]:
        priority = {"critical": "Критично", "high": "Важно", "medium": "Проверить"}.get(rec.get("priority"), rec.get("priority") or "Проверить")
        rec_rows.append([
            Paragraph(p(priority), styles["SmallRu"]),
            Paragraph(p(rec.get("title")), styles["SmallRu"]),
            Paragraph(p(rec.get("text")), styles["SmallRu"]),
        ])
    rec_table = Table(rec_rows, colWidths=[25*mm, 55*mm, 100*mm], repeatRows=1)
    rec_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F5F8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(rec_table)

    # Hidden risk matrix
    if hidden_risks:
        story.append(Paragraph("5. Скрытые риски, которые не всегда видны в реестрах", styles["H1Ru"]))
        hidden_rows = [[Paragraph("Риск", styles["TableHeadRu"]), Paragraph("Что проверить", styles["TableHeadRu"]), Paragraph("Правовая опора", styles["TableHeadRu"])]]
        for h in hidden_risks[:10]:
            hidden_rows.append([
                Paragraph(p(h.get("risk")), styles["SmallRu"]),
                Paragraph(p(h.get("why")), styles["SmallRu"]),
                Paragraph(p(h.get("law")), styles["SmallRu"]),
            ])
        hidden_table = Table(hidden_rows, colWidths=[38*mm, 95*mm, 47*mm], repeatRows=1)
        hidden_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F3EF")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(hidden_table)

    # Legal conclusion from GigaChat/local fallback
    story.append(Paragraph("6. Юридическое заключение", styles["H1Ru"]))
    legal_report = normalize_legal_report_format(str(report.get("legal_report") or ""))
    for kind, text in pdf_report_blocks(legal_report):
        if kind == "h":
            story.append(Paragraph(p(text), styles["H2Ru"]))
        elif kind == "bullet":
            story.append(Paragraph(f"• {p(text)}", styles["BulletRu"]))
        else:
            story.append(Paragraph(p(text), styles["TextRu"]))

    if "Важно" not in legal_report:
        add_card(
            "Важно",
            "Отчет носит информационно-аналитический характер, не является гарантией полной юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом.",
            bg="#F5F3EF",
            border="#E4D9C8",
        )

    doc.build(story)
    return buf.getvalue()


# ---------- pipeline ----------

async def run_checks(req: CheckRequest) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    payloads = build_payloads(req)
    async with httpx.AsyncClient() as client:
        # Run concurrently; each source creates only one paid task, polling by requestId only.
        results = await asyncio.gather(
            newdb_run(client, payloads.get("passport"), timeout_sec=55),
            newdb_run(client, payloads.get("fssp"), timeout_sec=95),
            newdb_run(client, payloads.get("bankruptcy"), timeout_sec=75),
            newdb_run(client, payloads.get("arbitr"), timeout_sec=75),
            newdb_run_with_fallback(
                client,
                payloads.get("pravosud"),
                {**payloads["pravosud"], "method": "pravosudfiz"} if payloads.get("pravosud") else None,
                timeout_sec=75,
            ),
            newdb_run(client, payloads.get("egrn"), timeout_sec=180),
            return_exceptions=True,
        )
    keys = ["passport", "fssp", "bankruptcy", "arbitr", "pravosud", "egrn"]
    responses: Dict[str, Any] = {}
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            responses[key] = {"state": "failed", "errors_info": [{"error": str(res)}]}
        else:
            responses[key] = res
    return payloads, responses


async def build_full_report(req: CheckRequest, include_debug: bool = False) -> Dict[str, Any]:
    payloads, responses = await run_checks(req)
    checklist = classify_all(responses, req)
    age = calculate_age(normalize_dob(req.dob)[0])
    scoring = risk_scoring(checklist, age=age)
    recs = build_recommendations(checklist, req)
    registry = build_registry_data(checklist)
    screen = build_screen_report(req, checklist, scoring, recs)
    legal = await maybe_gigachat_report(req, checklist, scoring, recs)

    report_id = str(uuid.uuid4())
    result = {
        "success": True,
        "report_id": report_id,
        "pdf_available": True,
        "pdf_url": f"/download-pdf/{report_id}",
        "created_at": now_ru(),
        "executive_summary": {
            "label": scoring.get("label"),
            "level": scoring.get("level"),
            "score": scoring.get("score"),
            "max_score": 100,
            "conclusion": scoring.get("conclusion"),
        },
        "screen_report": screen,
        "checklist": strip_service_fields(checklist),
        "classified_checklist": strip_service_fields(checklist),
        "registry_data": strip_service_fields(registry),
        "risk_scoring": scoring,
        "recommendations": recs,
        "advance_decision": build_advance_decision(scoring),
        "data_reliability": build_data_reliability(checklist),
        "hidden_risks": build_hidden_risks(req),
        "legal_report": legal,
        "normalized_input": normalized_input(req, expose_full_inn=False),
        "warnings": [],
        "notes": [
            "Polling выполняется только через top-level requestId, без повторной отправки params.",
            "Оценка риска дана для самостоятельной сделки без сопровождения юриста или риелтора.",
            "Клиентский /check-report очищает служебные поля newDB.",
        ],
    }
    if include_debug:
        result["payloads"] = payloads
        result["responses"] = responses

    # Public API response uses masked INN. Stored PDF version may show full INN in the report header.
    stored_report = dict(result)
    stored_report["normalized_input"] = normalized_input(req, expose_full_inn=True)
    REPORTS[report_id] = stored_report
    return result


# ---------- endpoints ----------

@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "version": APP_VERSION,
        "newdb_token": bool(NEWDB_TOKEN),
        "gigachat_credentials": bool(GIGACHAT_CREDENTIALS),
        "reportlab": REPORTLAB_AVAILABLE,
    }


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest) -> Dict[str, Any]:
    try:
        return await build_full_report(req, include_debug=True)
    except Exception as e:
        return {"success": False, "stage": "debug-newdb", "error": str(e)}


@app.post("/check-report")
async def check_report(req: CheckRequest) -> Dict[str, Any]:
    try:
        report = await build_full_report(req, include_debug=False)
        # Do not expose heavy internal data on client endpoint.
        return report
    except Exception as e:
        return {
            "success": False,
            "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.",
            "warnings": ["Техническая ошибка скрыта от пользователя и не влияет на юридический вывод."],
        }


@app.get("/download-pdf/{report_id}")
def download_pdf(report_id: str):
    report = REPORTS.get(report_id)
    if not report:
        content = b"Report not found or server was restarted. Please run check again."
        return StreamingResponse(io.BytesIO(content), media_type="text/plain")
    try:
        pdf = build_pdf_bytes(report)
    except Exception as e:
        pdf = ("PDF generation error: " + str(e)).encode("utf-8")
        return StreamingResponse(io.BytesIO(pdf), media_type="text/plain")
    filename = f"real_estate_report_{report_id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
