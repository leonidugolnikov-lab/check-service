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
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# Optional GigaChat. Report has a deterministic local fallback.
try:
    from gigachat import GigaChat
    GIGACHAT_AVAILABLE = True
except Exception:
    GIGACHAT_AVAILABLE = False


APP_VERSION = "1.1.1-premium-soft-bankruptcy-risk"
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


def clean_markdown(text: Any) -> str:
    """Remove Markdown artifacts before sending text to ReportLab/PDF."""
    if text is None:
        return ""
    s = str(text)
    s = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).replace("```", ""), s)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"__(.*?)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s.strip()


def public_deal_status(level: Any, label: Any = "") -> str:
    """Short status label for UI/PDF."""
    raw = f"{level or ''} {label or ''}".lower()
    if "опас" in raw:
        return "Опасно"
    if "риск" in raw:
        return "Рискованно"
    if "допуст" in raw:
        return "Допустимо"
    return str(label or level or "Оценка дана")


def parse_date_any(value: Any) -> Optional[datetime]:
    """Best-effort parser for dates returned by NewDB/Fedresurs/EGRN."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace("T", " ").replace("Z", "").strip()
    raw = re.sub(r"\s+", " ", raw)
    candidates = [raw[:19], raw[:10]]
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
    return max(0, (date_to.year - date_from.year) * 12 + (date_to.month - date_from.month))


def calc_age(dob_value: str) -> Optional[int]:
    dob_ru, dob_iso = normalize_dob(dob_value)
    if not dob_iso:
        return None
    try:
        birth = datetime.strptime(dob_iso, "%Y-%m-%d")
        today = datetime.now()
        return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    except Exception:
        return None


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


def normalized_input(req: CheckRequest) -> Dict[str, Any]:
    dob_ru, dob_iso = normalize_dob(req.dob)
    series, number = normalize_passport(req)
    return {
        "last": req.last.strip(),
        "first": req.first.strip(),
        "middle": req.middle.strip(),
        "dob": dob_ru,
        "dob_iso": dob_iso,
        "age": calc_age(req.dob),
        "inn": normalize_inn(req),
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
                if any(w in key for w in ["date", "дата", "published", "publication", "create", "update"]):
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
            details.append("После последней значимой публикации прошло менее 3 лет — требуется проверка банкротного дела до аванса.")

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
                for key in ["bankruptcy", "publications", "encumbrances"]:
                    val = row.get(key)
                    if isinstance(val, list) and len(val) > 0:
                        has_bankruptcy = True
                        details.append(f"{key}: найдено записей {len(val)}")

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


def classify_arbitr(resp: Dict[str, Any]) -> Dict[str, Any]:
    title, url = "Арбитражные суды", "https://kad.arbitr.ru"
    data, result = result_data(resp, "arbitr_person")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        return manual_item(title, "Источник арбитражных судов не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))
    cases: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("cases") and isinstance(row.get("cases"), list):
                cases.extend(row.get("cases") or [])
            elif isinstance(row, dict) and row.get("case_number"):
                cases.append(row)
    if cases:
        details = []
        for c in cases[:5]:
            num = c.get("case_number") or "дело без номера"
            status = c.get("status") or ""
            details.append(f"{num}: {status}".strip())
        return risk_item(title, f"Найдены арбитражные дела: {len(cases)}. Требуется анализ предмета спора.", url, details, cases[:10])
    return ok_item(title, "По полученным данным арбитражные дела не выявлены.", url, [], [])


def score_pravosud_match(row: Dict[str, Any], seller: Dict[str, Any]) -> int:
    """Estimate whether a court record belongs to the seller or is only a namesake hit.
    NewDB/GAS often returns broad FIO matches, so weak matches must not be treated as confirmed risk.
    """
    text = flatten_text(row).lower()
    score = 0

    for key in ["last", "first", "middle"]:
        part = clean_str(seller.get(key)).lower()
        if part and part in text:
            score += 1

    dob_ru = clean_str(seller.get("dob"))
    dob_iso = clean_str(seller.get("dob_iso"))
    if dob_ru and dob_ru.lower() in text:
        score += 5
    if dob_iso and dob_iso.lower() in text:
        score += 5

    inn = digits_only(seller.get("inn"))
    if inn and inn in digits_only(text):
        score += 8

    return score


def classify_pravosud(resp: Dict[str, Any], seller: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    title, url = "Суды общей юрисдикции / ГАС Правосудие", "https://sudrf.ru"
    seller = seller or {}
    data, result = result_data(resp, "pravo_search")
    if data is None:
        data, result = result_data(resp, "pravosudfiz")
    if has_result_status_500(resp) or is_newdb_error(resp) or data is None:
        text = flatten_text(resp).lower()
        if "method or country is not valid" in text:
            return manual_item(title, "Источник не вернул данные. Требуется ручная проверка.", url, ["Источник не принял метод или страну запроса. Требуется уточнить метод newDB."])
        return manual_item(title, "Источник судов общей юрисдикции не вернул данные. Требуется ручная проверка.", url, generic_error_details(resp))

    if not isinstance(data, list) or not data:
        return ok_item(title, "По полученным данным дела в судах общей юрисдикции не выявлены.", url, [], [])

    confirmed: List[Dict[str, Any]] = []
    probable: List[Dict[str, Any]] = []
    weak: List[Dict[str, Any]] = []

    for row in data:
        if not isinstance(row, dict):
            weak.append({"raw": row, "match_score": 0})
            continue
        match_score = score_pravosud_match(row, seller)
        row_copy = dict(row)
        row_copy["match_score"] = match_score
        if match_score >= 8:
            confirmed.append(row_copy)
        elif match_score >= 3:
            probable.append(row_copy)
        else:
            weak.append(row_copy)

    data_pack = {
        "confirmed_count": len(confirmed),
        "probable_count": len(probable),
        "weak_count": len(weak),
        "confirmed_cases": confirmed[:10],
        "probable_cases": probable[:10],
        "weak_cases": weak[:10],
    }

    details: List[str] = [
        f"Всего найдено записей по похожим ФИО: {len(data)}",
        f"Подтвержденные совпадения: {len(confirmed)}",
        f"Вероятные совпадения: {len(probable)}",
        f"Слабые совпадения/однофамильцы: {len(weak)}",
    ]

    sample = (confirmed or probable or weak)[:5]
    for row in sample:
        if isinstance(row, dict):
            role = row.get("role_text") or row.get("role_code") or "участник"
            case_id = row.get("case_id") or row.get("case_number") or "дело"
            details.append(f"{case_id}: {role}")

    if confirmed:
        return risk_item(
            title,
            f"Найдены судебные записи с сильным совпадением по продавцу: {len(confirmed)}. Требуется анализ роли, предмета дела и имущественных последствий.",
            url,
            details,
            data_pack,
        )

    if probable:
        item = manual_item(
            title,
            f"Найдены вероятные совпадения в судах общей юрисдикции: {len(probable)}. Без точных идентификаторов нельзя считать их подтвержденным риском продавца.",
            url,
            details,
        )
        item["data"] = data_pack
        return item

    item = manual_item(
        title,
        f"Найдены слабые совпадения по похожим ФИО: {len(weak)}. Вероятны однофамильцы; в риск сделки автоматически не включается.",
        url,
        details,
    )
    item["data"] = data_pack
    item["ui_status"] = "manual"
    return item


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


def add_hidden_legal_risks() -> Dict[str, Any]:
    """Legal traps that cannot be reliably confirmed by automatic registries.
    They are shown in the report as mandatory manual checks, but the scoring engine
    does not treat them as established negative facts.
    """
    return {
        "title": "Скрытые юридические риски",
        "source": "Юридический анализ",
        "status": "manual_check",
        "ui_status": "warning",
        "summary": "Часть рисков невозможно достоверно проверить автоматически. Перед внесением аванса (задатка) требуется ручная проверка.",
        "details": [
            "Супруг продавца: проверить, состоял ли продавец в браке на дату приобретения квартиры. При необходимости получить нотариальное согласие супруга (ст. 34–35 СК РФ).",
            "Несовершеннолетние и материнский капитал: проверить, использовался ли материнский капитал и не возникала ли обязанность выделить доли детям.",
            "Наследство: если объект получен по наследству, проверить круг наследников, возможных пропущенных наследников, завещание, отказы и судебные споры.",
            "Приватизация: проверить лиц, которые имели право на участие в приватизации, но отказались. У таких лиц может сохраняться право проживания.",
            "Зарегистрированные лица: проверить форму 9 / архивную форму 9. Особое внимание — несовершеннолетние, отказники от приватизации, временно выбывшие лица.",
            "Доверенность: если сделка проводится через представителя, проверить действительность доверенности и объем полномочий (ст. 182–183 ГК РФ).",
            "Цена сделки: не соглашаться на занижение стоимости в договоре. Это увеличивает риск споров и усложняет возврат средств (ст. 170 ГК РФ).",
        ],
        "manual_check_url": "",
        "manual_url": "",
        "no_score": True,
    }


def add_age_capacity_risk(req: CheckRequest) -> Optional[Dict[str, Any]]:
    age = calc_age(req.dob)
    if age is None or age < 70:
        return None

    if age >= 80:
        summary = f"Возраст продавца: {age} лет. Требуется усиленная проверка дееспособности, понимания сделки и отсутствия давления."
        status = "risk"
        ui_status = "risk"
    else:
        summary = f"Возраст продавца: {age} лет. Сам по себе возраст не является риском, но требует аккуратной проверки обстоятельств сделки."
        status = "manual_check"
        ui_status = "warning"

    return {
        "title": "Возраст / дееспособность / давление",
        "source": "Юридический анализ",
        "status": status,
        "ui_status": ui_status,
        "summary": summary,
        "details": [
            "Возраст сам по себе не ограничивает право продавца распоряжаться недвижимостью.",
            "До аванса (задатка) (задатка) проверить, понимает ли продавец смысл сделки и действует ли добровольно.",
            "При сомнениях использовать нотариальную форму, медицинское подтверждение состояния и прозрачную фиксацию расчетов.",
            "Юридическая база риска оспаривания: ст. 177 ГК РФ.",
        ],
        "manual_check_url": "",
        "manual_url": "",
        "data": {"age": age},
    }


def extract_dates_deep(value: Any) -> List[datetime]:
    dates: List[datetime] = []

    def walk(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if any(w in key for w in ["date", "дата", "registration", "reg"]):
                    dt = parse_date_any(v)
                    if dt:
                        dates.append(dt)
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(value)
    return dates


def add_property_history_risk(egrn_item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not egrn_item or not isinstance(egrn_item, dict):
        return None
    obj = egrn_item.get("data")
    if not isinstance(obj, dict):
        return None

    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    dates = extract_dates_deep(rights)
    latest_right_date = max(dates) if dates else None
    months = months_between_dates(latest_right_date) if latest_right_date else None

    details: List[str] = [
        "Проверяется история права по данным, которые удалось получить автоматически.",
        "Частые переходы права или недавнее владение требуют анализа основания приобретения и предыдущих сделок.",
    ]

    score_data = {
        "rights_count": len(rights),
        "latest_right_date": latest_right_date.strftime("%d.%m.%Y") if latest_right_date else "",
        "months_after_latest_right": months,
    }

    if len(rights) >= 3:
        details.append(f"В ЕГРН/ответе источника обнаружено несколько записей о правах: {len(rights)}.")
        return risk_item(
            "История перехода права",
            "Выявлено несколько записей о правах. Требуется проверить частоту переходов собственности и основания предыдущих сделок.",
            "",
            details,
            score_data,
        )

    if months is not None and months < 12:
        details.append(f"Последняя значимая дата права: {latest_right_date.strftime('%d.%m.%Y')}. Владение менее 1 года.")
        return risk_item(
            "Недавнее владение объектом",
            "По автоматическим данным владение может быть недавним. Это не запрещает сделку, но требует проверки основания права.",
            "",
            details,
            score_data,
        )

    if months is not None and months < 36:
        details.append(f"Последняя значимая дата права: {latest_right_date.strftime('%d.%m.%Y')}. Владение менее 3 лет.")
        item = manual_item(
            "Недавнее владение объектом",
            "По автоматическим данным владение может быть менее 3 лет. Требуется проверить основание права и историю предыдущей сделки.",
            "",
            details,
        )
        item["data"] = score_data
        item["ui_status"] = "warning"
        return item

    return None


def classify_all(responses: Dict[str, Dict[str, Any]], req: Optional[CheckRequest] = None) -> List[Dict[str, Any]]:
    seller = normalized_input(req) if req else {}
    egrn_item = classify_egrn(responses.get("egrn") or {})

    items: List[Dict[str, Any]] = [
        classify_passport(responses.get("passport") or {}),
        classify_fssp(responses.get("fssp") or {}),
        classify_bankruptcy(responses.get("bankruptcy") or {}),
        classify_arbitr(responses.get("arbitr") or {}),
        classify_pravosud(responses.get("pravosud") or {}, seller),
        egrn_item,
    ]

    history_item = add_property_history_risk(egrn_item)
    if history_item:
        items.append(history_item)

    if req:
        age_item = add_age_capacity_risk(req)
        if age_item:
            items.append(age_item)

    items.append(add_hidden_legal_risks())
    return items


# ---------- scoring/report ----------

def risk_scoring(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    score = 0
    factors: List[str] = []

    for item in checklist:
        title = item.get("title", "Источник")
        status = item.get("status")

        # Blocks that are mandatory legal reminders, not established negative facts.
        if item.get("no_score"):
            factors.append(f"{title}: обязательная ручная проверка без начисления риска")
            continue

        if status == "manual_check":
            if "Скрытые" in title:
                pts = 0
            elif "ГАС" in title:
                pdata = item.get("data") or {}
                probable = pdata.get("probable_count", 0) if isinstance(pdata, dict) else 0
                weak = pdata.get("weak_count", 0) if isinstance(pdata, dict) else 0
                pts = 10 if probable else 0
                if pts:
                    factors.append(f"ГАС Правосудие: вероятные совпадения требуют ручной проверки (+{pts})")
                else:
                    factors.append(f"ГАС Правосудие: слабые совпадения/однофамильцы не включены в риск ({weak})")
                score += pts
                continue
            elif "Недавнее владение" in title:
                pts = 10
            elif "Возраст" in title:
                pts = 8
            else:
                pts = 8
            score += pts
            if pts:
                factors.append(f"{title}: требуется ручная проверка (+{pts})")

        elif status == "risk":
            if "ЕГРН" in title:
                details_text = " ".join(item.get("details") or []).lower()
                pts = 80 if "запрещение регистрации" in details_text or "запрет" in details_text else 60
                score += pts
                factors.append(f"ЕГРН: выявлены ограничения/обременения (+{pts})")
            elif "ФССП" in title:
                actual = ((item.get("data") or {}).get("actual_debt") or 0) if isinstance(item.get("data"), dict) else 0
                pts = 35 if actual else 25
                score += pts
                factors.append(f"ФССП: активные/неоднозначные ИП, сумма {rub(actual)} (+{pts})")
            elif "Банкрот" in title:
                bdata = item.get("data") or {}
                bstatus = bdata.get("bankruptcy_status") if isinstance(bdata, dict) else None
                months = bdata.get("months_after_latest") if isinstance(bdata, dict) else None
                property_words = bool(bdata.get("property_related_words")) if isinstance(bdata, dict) else False

                if bstatus == "active":
                    pts = 70
                    factors.append("Банкротство: есть признаки незавершенной процедуры (+70)")
                elif months is not None and months < 12:
                    pts = 45
                    factors.append("Банкротство: завершенная/значимая процедура, прошло менее 1 года (+45)")
                elif months is not None and months < 36:
                    pts = 35
                    factors.append("Банкротство: после завершения/значимой публикации прошло менее 3 лет (+35)")
                elif property_words:
                    pts = 40
                    factors.append("Банкротство: есть публикации, связанные с имуществом/торгами/конкурсной массой (+40)")
                else:
                    pts = 25
                    factors.append("Банкротство: сведения найдены, требуется ручная проверка (+25)")
                score += pts
            elif "Арбитраж" in title:
                pts = 35
                score += pts
                factors.append("Арбитражные суды: найдены дела (+35)")
            elif "ГАС" in title:
                pdata = item.get("data") or {}
                confirmed = pdata.get("confirmed_count", 0) if isinstance(pdata, dict) else 0
                probable = pdata.get("probable_count", 0) if isinstance(pdata, dict) else 0
                if confirmed:
                    pts = 25
                    score += pts
                    factors.append(f"ГАС Правосудие: подтвержденные совпадения по продавцу ({confirmed}) (+{pts})")
                elif probable:
                    pts = 10
                    score += pts
                    factors.append(f"ГАС Правосудие: вероятные совпадения ({probable}) (+{pts})")
            elif "Паспорт" in title:
                pts = 40
                score += pts
                factors.append("Паспорт МВД: выявлен риск (+40)")
            elif "История перехода права" in title or "Недавнее владение" in title:
                pts = 25
                score += pts
                factors.append(f"{title}: требуется анализ истории права (+{pts})")
            elif "Возраст" in title:
                pts = 15
                score += pts
                factors.append("Возраст/дееспособность: требуется усиленная проверка обстоятельств сделки (+15)")
            else:
                pts = 25
                score += pts
                factors.append(f"{title}: выявлен риск (+{pts})")

    score = max(0, min(100, int(score)))
    if score >= 80:
        level = "опасная"
        label = "Опасно"
        conclusion = "Сделку не рекомендуется проводить самостоятельно без юридического сопровождения. Сделка может быть реализуема только после устранения ключевых рисков и настройки безопасной схемы расчетов."
    elif score >= 35:
        level = "условно рискованная"
        label = "Рискованно"
        conclusion = "Сделку можно рассматривать только после уточнения рисков, ручной проверки документов и защитных условий в авансе (задатке) / ПДКП."
    else:
        level = "допустимая"
        label = "Допустимо"
        conclusion = "По автоматическим источникам критичных рисков не выявлено, но отчет не заменяет ручную юридическую проверку документов."

    return {
        "score": score,
        "max_score": 100,
        "level": level,
        "label": label,
        "public_status": public_deal_status(level, label),
        "conclusion": conclusion,
        "factors": factors,
    }


def build_recommendations(checklist: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    recs: List[Dict[str, str]] = []
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
            recs.append({"priority": "high", "title": "Проверить завершенное банкротство до аванса", "text": "После значимой публикации по банкротству прошло менее 3 лет. Нужно проверить судебный акт о завершении процедуры, отчет финансового управляющего и связь квартиры с конкурсной массой."})
        else:
            recs.append({"priority": "high", "title": "Разобрать банкротный риск", "text": "Нужно проверить даты процедуры, статус завершения, освобождение от обязательств, публикации Федресурса и риск оспаривания сделки."})
    if arbitr and arbitr.get("status") == "risk":
        recs.append({"priority": "high", "title": "Разобрать арбитражные дела", "text": "Нужно понять предмет спора, связь с долгами/банкротством и возможное влияние на сделку."})
    if pravosud and pravosud.get("status") == "risk":
        recs.append({"priority": "medium", "title": "Разобрать дела судов общей юрисдикции", "text": "Нужно проверить роль продавца в делах, предмет спора и возможные имущественные последствия."})
    if any(x.get("status") == "manual_check" for x in checklist):
        recs.append({"priority": "medium", "title": "Закрыть ручные проверки до аванса", "text": "Все источники со статусом «требуется ручная проверка» нужно проверить вручную до передачи денег."})
    recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса (задатка) и ответственность при неподтверждении данных."})
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
        "seller": {"fio": fio(req), "dob": normalize_dob(req.dob)[0], "inn_provided": bool(normalize_inn(req))},
        "property": normalize_property(req),
    }


def build_local_legal_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> str:
    risks = [x for x in checklist if x.get("status") == "risk"]
    oks = [x for x in checklist if x.get("status") == "ok"]
    manual = [x for x in checklist if x.get("status") == "manual_check"]
    seller = fio(req) or "по предоставленным данным не указано"
    dob_ru = normalize_dob(req.dob)[0] or "по предоставленным данным не указано"
    inn = normalize_inn(req) or "не указан"
    prop = normalize_property(req)["query"] or "по предоставленным данным не указан"

    lines: List[str] = []
    lines.append("1. Краткий вывод")
    lines.append(f"Оценка сделки: {scoring.get('label')} ({scoring.get('score')}/100). {scoring.get('conclusion')}")
    lines.append("Отчет не подтверждает абсолютную юридическую безопасность сделки и не заменяет ручной анализ документов.")

    lines.append("")
    lines.append("2. Что проверено")
    lines.append(f"Продавец: {seller}. Дата рождения: {dob_ru}. ИНН: {inn}. Объект: {prop}.")
    lines.append(f"Проверено без явных рисков: {len(oks)}. Выявленных рисков: {len(risks)}. Требуют ручной проверки: {len(manual)}.")

    lines.append("")
    lines.append("3. Риски по продавцу")
    seller_risks = [r for r in risks if any(w in r.get("title", "") for w in ["ФССП", "Банкрот", "Арбитраж", "ГАС", "Паспорт", "Возраст"])]
    if seller_risks:
        for r in seller_risks:
            lines.append(f"- {r.get('title')}: {r.get('summary')}")
    else:
        lines.append("- По автоматическим источникам подтвержденные критичные риски по продавцу не выявлены.")

    lines.append("")
    lines.append("4. Риски по объекту")
    object_risks = [r for r in risks if any(w in r.get("title", "") for w in ["ЕГРН", "История", "Недавнее"])]
    if object_risks:
        for r in object_risks:
            lines.append(f"- {r.get('title')}: {r.get('summary')}")
    else:
        lines.append("- По автоматическим источникам подтвержденные критичные риски по объекту не выявлены.")

    lines.append("")
    lines.append("5. Скрытые юридические риски")
    hidden = next((x for x in checklist if x.get("title") == "Скрытые юридические риски"), None)
    if hidden:
        for d in hidden.get("details", []):
            lines.append(f"- {d}")
    else:
        lines.append("- По предоставленным данным скрытые риски автоматически не проверялись.")

    lines.append("")
    lines.append("6. Что обязательно проверить до аванса (задатка)")
    for rec in recs:
        lines.append(f"- {rec.get('title')}: {rec.get('text')}")
    if not recs:
        lines.append("- Получить актуальную выписку ЕГРН, документы-основания, сведения о зарегистрированных лицах и семейном положении продавца.")

    lines.append("")
    lines.append("7. Что прописать в авансе (задатке) / ПДКП")
    lines.append("- Гарантии продавца об отсутствии скрытых обременений, нераскрытых судебных споров, банкротных рисков и прав третьих лиц.")
    lines.append("- Условия возврата аванса (задатка), сроки устранения выявленных рисков и ответственность за недостоверные сведения.")
    lines.append("- При задатке учитывать правовые последствия ст. 380–381 ГК РФ.")

    lines.append("")
    lines.append("8. Безопасная схема расчетов")
    lines.append("При выявленных долгах, запретах или неполных данных не передавать деньги напрямую продавцу. Использовать аккредитив, нотариальный депозит или иную условную схему с раскрытием денег только после выполнения условий и регистрации перехода права.")

    lines.append("")
    lines.append("9. Итоговое заключение")
    lines.append("Сделку можно оценивать только после закрытия выявленных рисков и ручной проверки документов. При наличии ограничений, активных ИП, банкротных признаков или подтвержденных судебных споров сделка должна проходить с юридическим сопровождением.")

    return clean_markdown("\n".join(lines))


async def maybe_gigachat_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> str:
    fallback = build_local_legal_report(req, checklist, scoring, recs)
    if not (GIGACHAT_AVAILABLE and GIGACHAT_CREDENTIALS):
        return clean_markdown(fallback)

    payload = {
        "seller": normalized_input(req),
        "checklist": strip_service_fields(checklist),
        "risk_scoring": scoring,
        "recommendations": recs,
    }
    prompt = (
        "Ты юрист-эксперт по недвижимости в Санкт-Петербурге. Сформируй комплексный юридический отчет "
        "для покупателя недвижимости на основе структурированных данных.\n\n"
        "КРИТИЧЕСКИЕ ПРАВИЛА:\n"
        "1. Не придумывай факты. Если данных нет — пиши: «по предоставленным данным не проверялось».\n"
        "2. Не называй объект юридически чистым и не обещай 100% безопасность.\n"
        "3. Используй формулировку «аванс (задаток)», потому что в сделках применяются оба варианта.\n"
        "4. Если описываешь правовые последствия задатка — кратко ссылайся на ст. 380–381 ГК РФ.\n"
        "5. Завершенное банкротство продавца не является автоматическим запретом сделки. Пиши: риск повышенный и требуется проверка банкротного дела, отчета финансового управляющего, истории объекта и даты права. Критический вывод допустим только при активной процедуре, неясном статусе, связи объекта с конкурсной массой, активных долгах/судах/ограничениях или иных подтвержденных фактах. Ссылки: ст. 61.2–61.3 ФЗ «О несостоятельности (банкротстве)».\n"
        "6. По судам общей юрисдикции не считай все найденные дела подтвержденными рисками продавца. Если нет даты рождения, ИНН, адреса, паспорта или иных точных идентификаторов — пиши «возможные совпадения/однофамильцы, требуется ручная сверка».\n"
        "7. По скрытым рискам пиши как обязательные ручные проверки: супруг (ст. 34–35 СК РФ), несовершеннолетние/маткапитал, наследство, приватизация, зарегистрированные лица, доверенность (ст. 182–183 ГК РФ), занижение цены (ст. 170 ГК РФ).\n"
        "8. По возрасту/дееспособности не делай дискриминационных выводов: возраст сам по себе не риск, но при пожилом продавце нужна проверка осознанности и отсутствия давления; ссылка ст. 177 ГК РФ.\n"
        "9. Пиши без Markdown-разметки: не используй **, ###, таблицы markdown и кодовые блоки.\n\n"
        "СТРУКТУРА ОТЧЕТА:\n"
        "1. Краткий вывод\n"
        "2. Что проверено\n"
        "3. Риски по продавцу\n"
        "4. Риски по объекту\n"
        "5. Скрытые юридические риски\n"
        "6. Что обязательно проверить до аванса (задатка)\n"
        "7. Что прописать в авансе (задатке) / ПДКП\n"
        "8. Безопасная схема расчетов\n"
        "9. Итоговое заключение\n\n"
        "Стиль: дорого, экспертно, короткие абзацы, юридически точно, без воды.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        def _call() -> str:
            with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                resp = giga.chat(prompt)
                return (resp.choices[0].message.content or "").strip()
        result_text = await asyncio.to_thread(_call)
        return clean_markdown(result_text or fallback)
    except Exception:
        return clean_markdown(fallback)


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
    # ReportLab basic escaping + Markdown cleanup.
    s = clean_markdown(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def build_pdf_bytes(report: Dict[str, Any]) -> bytes:
    if not REPORTLAB_AVAILABLE:
        return ("PDF generation unavailable: reportlab is not installed.\n\n" + json.dumps(report, ensure_ascii=False, indent=2)).encode("utf-8")

    font = register_pdf_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Комплексный юридический отчет",
        author="Юридическая проверка недвижимости",
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle",
        fontName=font,
        fontSize=20,
        leading=24,
        alignment=TA_CENTER,
        spaceAfter=6,
        textColor=colors.HexColor("#0B2E3F"),
    ))
    styles.add(ParagraphStyle(
        name="ReportSubtitle",
        fontName=font,
        fontSize=8.5,
        leading=11,
        alignment=TA_CENTER,
        spaceAfter=12,
        textColor=colors.HexColor("#6E7F8D"),
    ))
    styles.add(ParagraphStyle(
        name="Section",
        fontName=font,
        fontSize=13,
        leading=16,
        spaceBefore=10,
        spaceAfter=6,
        textColor=colors.HexColor("#0B2E3F"),
    ))
    styles.add(ParagraphStyle(
        name="Body",
        fontName=font,
        fontSize=9,
        leading=12.5,
        spaceAfter=4,
        textColor=colors.HexColor("#111827"),
    ))
    styles.add(ParagraphStyle(
        name="Small",
        fontName=font,
        fontSize=7.8,
        leading=10,
        textColor=colors.HexColor("#3C4853"),
    ))
    styles.add(ParagraphStyle(
        name="Tiny",
        fontName=font,
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#6E7F8D"),
    ))
    styles.add(ParagraphStyle(
        name="Badge",
        fontName=font,
        fontSize=15,
        leading=18,
        alignment=TA_CENTER,
        textColor=colors.white,
    ))
    styles.add(ParagraphStyle(
        name="BadgeSmall",
        fontName=font,
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#E8EEF2"),
    ))
    styles.add(ParagraphStyle(
        name="WhiteHead",
        fontName=font,
        fontSize=8.5,
        leading=10,
        alignment=TA_CENTER,
        textColor=colors.white,
    ))

    story: List[Any] = []
    scoring = report.get("risk_scoring") or {}
    screen = report.get("screen_report") or {}
    checklist = report.get("checklist") or []
    recs = report.get("recommendations") or []
    norm = report.get("normalized_input") or {}

    status_text = public_deal_status(scoring.get("level"), scoring.get("label"))
    score = scoring.get("score", 0) or 0
    badge_color = (
        colors.HexColor("#8B1E1E") if score >= 80
        else colors.HexColor("#B7791F") if score >= 35
        else colors.HexColor("#1F7A4D")
    )

    story.append(Paragraph("Комплексный юридический отчет<br/>по проверке продавца и объекта недвижимости", styles["ReportTitle"]))
    story.append(Paragraph(f"Дата формирования: {p(report.get('created_at') or now_ru())} • Автоматическая проверка + юридический анализ", styles["ReportSubtitle"]))

    # Premium status strip
    status_table = Table([
        [
            Paragraph(f"{p(status_text)}<br/><font size='8'>{score}/100</font>", styles["Badge"]),
            Paragraph(p(scoring.get("conclusion") or ""), styles["Body"]),
        ]
    ], colWidths=[42 * mm, 132 * mm])
    status_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), badge_color),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D8DEE5")),
        ("LINEAFTER", (0, 0), (0, 0), 0.6, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(status_table)
    story.append(Spacer(1, 8))

    story.append(Paragraph("1. Данные проверки", styles["Section"]))
    seller = norm
    prop = norm.get("property") or {}
    seller_fio = " ".join([seller.get("last", ""), seller.get("first", ""), seller.get("middle", "")]).strip() or "не указан"
    inn_value = seller.get("inn") or "не указан"
    age_value = seller.get("age")
    dob_line = p(seller.get("dob") or "не указана")
    if age_value is not None:
        dob_line += f" ({age_value} лет)"

    info_table = Table([
        [Paragraph("Продавец", styles["Small"]), Paragraph(p(seller_fio), styles["Body"])],
        [Paragraph("Дата рождения", styles["Small"]), Paragraph(dob_line, styles["Body"])],
        [Paragraph("ИНН", styles["Small"]), Paragraph(p(inn_value), styles["Body"])],
        [Paragraph("Объект", styles["Small"]), Paragraph(p(prop.get("query") or "не указан"), styles["Body"])],
    ], colWidths=[43 * mm, 131 * mm])
    info_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F3EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(info_table)

    story.append(Paragraph("2. Ключевые риски и ручные проверки", styles["Section"]))
    risks = [x for x in checklist if x.get("status") == "risk"]
    manuals = [x for x in checklist if x.get("status") == "manual_check"]
    if risks:
        for item in risks[:10]:
            story.append(Paragraph(f"<b>{p(item.get('title'))}</b>: {p(item.get('summary'))}", styles["Body"]))
            for d in (item.get("details") or [])[:4]:
                story.append(Paragraph(f"• {p(d)}", styles["Small"]))
    else:
        story.append(Paragraph("По автоматическим источникам явные критичные риски не выявлены.", styles["Body"]))

    if manuals:
        story.append(Paragraph("<b>Требуют ручной проверки:</b>", styles["Body"]))
        for item in manuals[:8]:
            story.append(Paragraph(f"• {p(item.get('title'))}: {p(item.get('summary'))}", styles["Small"]))

    story.append(Paragraph("3. Чек-лист проверок", styles["Section"]))
    rows = [[
        Paragraph("Источник", styles["WhiteHead"]),
        Paragraph("Статус", styles["WhiteHead"]),
        Paragraph("Вывод", styles["WhiteHead"]),
    ]]
    status_names = {
        "ok": "Проверено",
        "risk": "Риск",
        "manual_check": "Ручная проверка",
        "manual": "Ручная проверка",
    }
    for item in checklist:
        status = status_names.get(item.get("status"), item.get("status") or "")
        rows.append([
            Paragraph(p(item.get("title") or item.get("source") or ""), styles["Small"]),
            Paragraph(p(status), styles["Small"]),
            Paragraph(p(item.get("summary") or ""), styles["Small"]),
        ])

    tbl = Table(rows, colWidths=[43 * mm, 34 * mm, 97 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B2E3F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), font),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)

    if recs:
        story.append(Paragraph("4. Рекомендации юриста", styles["Section"]))
        rec_rows = []
        for rec in recs[:12]:
            priority = rec.get("priority") or ""
            label = "Критично" if priority == "critical" else ("Важно" if priority in {"high", "medium"} else "Рекомендация")
            rec_rows.append([
                Paragraph(p(label), styles["Small"]),
                Paragraph(f"<b>{p(rec.get('title'))}</b><br/>{p(rec.get('text'))}", styles["Small"]),
            ])
        rec_table = Table(rec_rows, colWidths=[30 * mm, 144 * mm])
        rec_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F3EF")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(rec_table)

    story.append(Paragraph("5. Юридическое заключение", styles["Section"]))
    legal_text = clean_markdown(report.get("legal_report") or "")
    for block in re.split(r"\n\s*\n", legal_text):
        block = block.strip()
        if not block:
            continue
        story.append(Paragraph(p(block), styles["Body"]))

    story.append(Spacer(1, 8))
    important = Table([
        [Paragraph("ВАЖНО", styles["WhiteHead"])],
        [Paragraph("Отчет носит информационно-аналитический характер, не является гарантией полной юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом. Перед внесением аванса (задатка) необходимо проверить оригиналы документов, актуальную ЕГРН, основания права, зарегистрированных лиц, семейное положение продавца и условия расчетов.", styles["Small"])],
    ], colWidths=[174 * mm])
    important.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#0B2E3F")),
        ("TEXTCOLOR", (0, 0), (0, 0), colors.white),
        ("BACKGROUND", (0, 1), (0, 1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DEE5")),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(important)

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
    scoring = risk_scoring(checklist)
    recs = build_recommendations(checklist)
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
        "legal_report": legal,
        "normalized_input": normalized_input(req),
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
    REPORTS[report_id] = result
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
