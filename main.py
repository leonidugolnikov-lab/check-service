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


APP_VERSION = "1.2.0-final-premium-court-age-hidden-risks"
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
        details.append("До аванса нужно проверить карточку банкротного дела, полномочия продавца и позицию финансового управляющего.")
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
        details.append("До аванса нужно сверить объект с материалами банкротного дела и отчетом финансового управляющего.")
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
            summary = f"Найдены арбитражные дела: {len(normalized_cases)}. Надежность совпадений: точных {len(strong)}, вероятных {len(probable)}, слабых {len(weak)}. Требуется анализ предмета спора и роли продавца."
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
            summary = f"Найдены записи в судах общей юрисдикции: {len(normalized_cases)}. Надежность совпадений: точных {len(strong)}, вероятных {len(probable)}, слабых {len(weak)}. Требуется анализ роли, предмета дела и имущественных последствий."
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
    if age is not None and age >= 80:
        age_comment = "Возраст 80+: справки ПНД/НД и оценка понимания сделки критически необходимы до аванса/подписания."
    elif age is not None and age >= 75:
        age_comment = "Возраст 75+: справки ПНД/НД крайне желательно получить до аванса; нотариальная форма и видеофиксация могут снизить риск спора."
    elif age is not None and age >= 70:
        age_comment = "Возраст 70+: справки ПНД/НД желательно запросить, особенно при продаже единственного жилья, занижении цены или давлении родственников."

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
        return {"decision": "Нельзя передавать аванс до устранения рисков", "level": "stop", "comment": "Есть критические признаки. Сначала ручная проверка, документы, условия снятия рисков и безопасная схема расчетов."}
    if score >= 60:
        return {"decision": "Только с жесткими защитными условиями", "level": "strict_conditions", "comment": "Аванс возможен только после ручной проверки и с условиями возврата/ответственности продавца."}
    if score >= 35:
        return {"decision": "Осторожно, после проверки документов", "level": "caution", "comment": "Критический стоп-фактор не подтвержден, но до аванса нужно закрыть ручные проверки и прописать условия в ПДКП."}
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
    score = 0
    factors: List[str] = []

    if age is not None:
        if age >= 80:
            score += 25
            factors.append("Возраст продавца 80+ — высокий риск оспаривания по воле/дееспособности (+25)")
        elif age >= 75:
            score += 20
            factors.append("Возраст продавца 75+ — требуется усиленная проверка дееспособности (+20)")
        elif age >= 70:
            score += 15
            factors.append("Возраст продавца 70+ — повышенный риск, желательны ПНД/НД (+15)")

    for item in checklist:
        title = item.get("title", "Источник")
        status = item.get("status")
        if status == "manual_check":
            pts = 6 if "ГАС" in title else 8
            score += pts
            factors.append(f"{title}: требуется ручная проверка (+{pts})")
        elif status == "risk":
            if "ЕГРН" in title:
                details_text = " ".join(item.get("details") or []).lower()
                pts = 80 if "запрещение регистрации" in details_text or "запрет" in details_text else 60
                score += pts
                factors.append(f"ЕГРН: выявлены ограничения/обременения (+{pts})")
            elif "ФССП" in title:
                actual = ((item.get("data") or {}).get("actual_debt") or 0) if isinstance(item.get("data"), dict) else 0
                if actual > 1_000_000:
                    pts = 45
                elif actual > 300_000:
                    pts = 35
                elif actual > 50_000:
                    pts = 25
                elif actual > 0:
                    pts = 15
                else:
                    pts = 10
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
                cases = item.get("data") if isinstance(item.get("data"), list) else []
                strong = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "точное совпадение"])
                probable = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "вероятное совпадение"])
                weak = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "слабое совпадение"])
                pts = 35 + min(15, strong * 5 + probable * 3)
                score += pts
                factors.append(f"Арбитражные суды: найденные дела, точных {strong}, вероятных {probable}, слабых {weak} (+{pts})")
            elif "ГАС" in title:
                cases = item.get("data") if isinstance(item.get("data"), list) else []
                strong = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "точное совпадение"])
                probable = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "вероятное совпадение"])
                weak = len([c for c in cases if isinstance(c, dict) and c.get("match_level") == "слабое совпадение"])
                pts = 25 + min(15, strong * 5 + probable * 3)
                score += pts
                factors.append(f"ГАС Правосудие: найденные дела, точных {strong}, вероятных {probable}, слабых {weak} (+{pts})")
            elif "Паспорт" in title:
                score += 40
                factors.append("Паспорт МВД: выявлен риск (+40)")
            else:
                score += 25
                factors.append(f"{title}: выявлен риск (+25)")

    fssp_flag = any("ФССП" in x.get("title", "") and x.get("status") == "risk" for x in checklist)
    bankrupt_flag = any("Банкрот" in x.get("title", "") and x.get("status") == "risk" for x in checklist)
    egrn_flag = any("ЕГРН" in x.get("title", "") and x.get("status") == "risk" for x in checklist)

    if fssp_flag and bankrupt_flag:
        score += 20
        factors.append("Комбинация рисков: долги/ФССП + банкротство (+20)")
    if fssp_flag and egrn_flag:
        score += 15
        factors.append("Комбинация рисков: долги/ФССП + ограничения по объекту (+15)")
    if bankrupt_flag and egrn_flag:
        score += 20
        factors.append("Комбинация рисков: банкротство + ограничения ЕГРН (+20)")

    critical = False
    for item in checklist:
        title = item.get("title", "")
        details_text = " ".join(item.get("details") or []).lower()
        if "ЕГРН" in title and ("запрещение регистрации" in details_text or "запрет" in details_text):
            critical = True
        if "Банкрот" in title:
            data = item.get("data") or {}
            if isinstance(data, dict) and data.get("bankruptcy_status") == "active":
                critical = True
        if "ФССП" in title:
            actual = ((item.get("data") or {}).get("actual_debt") or 0) if isinstance(item.get("data"), dict) else 0
            if actual > 500_000:
                critical = True

    if age is not None and age >= 85:
        critical = True

    if critical:
        score = max(score, 85)
        factors.append("Обнаружен критический триггер — аванс и сделка только после ручной юридической проверки")

    score = max(0, min(100, int(score)))
    if score >= 85:
        level = "опасная"
        label = "Опасная при самостоятельной сделке"
        conclusion = "Аванс и сделку нельзя проводить без ручной юридической проверки, устранения ключевых рисков и безопасной схемы расчетов."
    elif score >= 60:
        level = "высокорискованная"
        label = "Высокий риск при самостоятельной сделке"
        conclusion = "Сделку можно рассматривать только после уточнения рисков, проверки документов и жестких защитных условий в авансе/ПДКП."
    elif score >= 35:
        level = "условно рискованная"
        label = "Условно рискованная при самостоятельной сделке"
        conclusion = "Сделку можно рассматривать только после закрытия ручных проверок и внесения защитных условий в документы."
    else:
        level = "допустимая"
        label = "Допустимая к дальнейшему рассмотрению"
        conclusion = "По автоматическим источникам критичных рисков не выявлено, но отчет не заменяет ручную юридическую проверку документов."
    return {"score": score, "max_score": 100, "level": level, "label": label, "conclusion": conclusion, "factors": factors}

def build_recommendations(checklist: List[Dict[str, Any]], req: Optional[CheckRequest] = None) -> List[Dict[str, str]]:
    recs: List[Dict[str, str]] = []
    if req is not None:
        age = calculate_age(normalize_dob(req.dob)[0])
        if age is not None and age >= 80:
            recs.append({"priority": "critical", "title": "Проверка дееспособности обязательна", "text": "Возраст 80+. До аванса и сделки критически необходимо получить справки ПНД и НД, убедиться в понимании продавцом условий сделки и исключить давление третьих лиц."})
        elif age is not None and age >= 75:
            recs.append({"priority": "high", "title": "Проверка дееспособности крайне желательна", "text": "Возраст 75+. Желательно получить справки ПНД и НД до аванса; при сомнениях использовать нотариальную форму и фиксировать волю продавца."})
        elif age is not None and age >= 70:
            recs.append({"priority": "medium", "title": "Проверка дееспособности желательна", "text": "Возраст 70+. Рекомендуется запросить справки ПНД и НД, особенно если цена ниже рынка, сделка срочная или участвуют родственники/представители."})

    by_title = {x.get("title", ""): x for x in checklist}
    egrn = by_title.get("ЕГРН / Росреестр")
    fssp = by_title.get("ФССП")
    bankruptcy = by_title.get("Банкротство / Федресурс")
    arbitr = by_title.get("Арбитражные суды")
    pravosud = by_title.get("Суды общей юрисдикции / ГАС Правосудие")

    if egrn and egrn.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки", "text": "По ЕГРН выявлены ограничения/обременения. До аванса нужно получить основание ограничения и прописать обязанность продавца снять его в конкретный срок."})
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
    recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями", "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса/задатка и ответственность при неподтверждении данных."})
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
    lines.append("6. Что обязательно сделать до аванса")
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

    age_factor = "не выявлен"
    if age is not None and age >= 80:
        age_factor = "высокая осторожность: проверить дееспособность, волю продавца и отсутствие давления"
    elif age is not None and age >= 70:
        age_factor = "повышенная осторожность: желательно проверить понимание условий сделки и отсутствие давления"

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


async def maybe_gigachat_report(req: CheckRequest, checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recs: List[Dict[str, str]]) -> str:
    fallback = build_local_legal_report(req, checklist, scoring, recs)
    if not (GIGACHAT_AVAILABLE and GIGACHAT_CREDENTIALS):
        return fallback
    payload = build_gigachat_safe_payload(req, checklist, scoring, recs)
    prompt = (
        "Ты практикующий юрист по недвижимости в Санкт-Петербурге с опытом более 15 лет. "
        "На основе структурированных данных сформируй подробное юридическое заключение для покупателя квартиры "
        "уровня платной правовой экспертизы стоимостью около 30 000 рублей. "
        "Пиши уверенно, дорого, конкретно и по делу: не общими словами, а с объяснением, как каждый риск влияет на аванс, ПДКП, расчеты и регистрацию. "
        "Ссылайся на нормы права, когда это уместно: ГК РФ, СК РФ, ЖК РФ, ФЗ №218-ФЗ, ФЗ №127-ФЗ, ФЗ №229-ФЗ, ФЗ №256-ФЗ. "
        "Не придумывай факты. Если данных нет — прямо пиши: 'по предоставленным данным не проверялось'. "
        "Не называй объект юридически чистым и не обещай 100% безопасность. "
        "Не указывай ИНН, серию/номер паспорта и полную дату рождения. "
        "Если ИНН был использован для проверки, пиши только: 'ИНН был передан для автоматической проверки'. "
        "По судебным делам без надежного совпадения используй формулировку 'вероятные совпадения' или 'слабые совпадения', а не утверждай, что дело точно относится к продавцу. "
        "Возрастной риск формулируй практично: 70+ — справки ПНД/НД желательно; 75+ — крайне желательно; 80+ — критически необходимо до аванса/сделки. "
        "Не используй markdown-разметку: не ставь ###, **, --- и кодовые блоки. "
        "Вместо слова 'Дисклеймер' используй только слово 'Важно'. "
        "Структура строго:\n"
        "1. Краткий вывод\n"
        "2. Надежность полученных данных\n"
        "3. Риски продавца\n"
        "4. Риски объекта\n"
        "5. Скрытые риски, которые не всегда видны в реестрах\n"
        "6. Можно ли передавать аванс\n"
        "7. Что проверить до аванса\n"
        "8. Что прописать в авансовом соглашении / ПДКП\n"
        "9. Безопасная схема расчетов\n"
        "10. Итоговое заключение\n"
        "11. Важно\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        def _call() -> str:
            with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
                resp = giga.chat(prompt)
                return (resp.choices[0].message.content or "").strip()
        text = strip_markdown_noise(redact_sensitive_from_ai_text(await asyncio.to_thread(_call)))
        if not text or is_gigachat_refusal(text):
            return fallback
        return text
    except Exception:
        return fallback


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


def build_pdf_bytes(report: Dict[str, Any]) -> bytes:
    if not REPORTLAB_AVAILABLE:
        return ("PDF generation unavailable: reportlab is not installed.\n\n" + json.dumps(report, ensure_ascii=False, indent=2)).encode("utf-8")

    font = register_pdf_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleRu", fontName=font, fontSize=22, leading=28, alignment=TA_CENTER, spaceAfter=10, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="H1Ru", fontName=font, fontSize=15, leading=19, spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="TextRu", fontName=font, fontSize=9.5, leading=13, spaceAfter=4, textColor=colors.HexColor("#111827")))
    styles.add(ParagraphStyle(name="SmallRu", fontName=font, fontSize=8, leading=10, textColor=colors.HexColor("#3C4853")))
    styles.add(ParagraphStyle(name="BadgeRu", fontName=font, fontSize=13, leading=16, alignment=TA_CENTER, textColor=colors.white))

    story: List[Any] = []
    scoring = report.get("risk_scoring") or {}
    screen = report.get("screen_report") or {}
    checklist = report.get("checklist") or []
    recs = report.get("recommendations") or []
    norm = report.get("normalized_input") or {}

    story.append(Paragraph("Юридический отчет по проверке продавца и объекта недвижимости", styles["TitleRu"]))
    story.append(Paragraph(f"Дата формирования: {p(report.get('created_at'))}", styles["SmallRu"]))
    story.append(Spacer(1, 8))

    label = scoring.get("label") or scoring.get("level") or "Оценка риска"
    score = scoring.get("score", 0)
    badge_color = colors.HexColor("#8B1E1E") if score >= 80 else (colors.HexColor("#B7791F") if score >= 35 else colors.HexColor("#1F7A4D"))
    badge = Table([[Paragraph(f"{p(label).upper()}<br/>{score}/100", styles["BadgeRu"])]], colWidths=[170*mm])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), badge_color),
        ("BOX", (0, 0), (-1, -1), 0.5, badge_color),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(badge)
    story.append(Spacer(1, 8))
    story.append(Paragraph(p(scoring.get("conclusion")), styles["TextRu"]))

    story.append(Paragraph("1. Данные проверки", styles["H1Ru"]))
    seller = norm
    prop = norm.get("property") or {}
    info_table = Table([
        [Paragraph("Продавец", styles["SmallRu"]), Paragraph(p(" ".join([seller.get("last", ""), seller.get("first", ""), seller.get("middle", "")]).strip()), styles["TextRu"])],
        [Paragraph("Дата рождения", styles["SmallRu"]), Paragraph(p(seller.get("dob")), styles["TextRu"])],
        [Paragraph("ИНН", styles["SmallRu"]), Paragraph(p(seller.get("inn") or ("передан" if seller.get("inn_provided") else "не передан")), styles["TextRu"])],
        [Paragraph("Объект", styles["SmallRu"]), Paragraph(p(prop.get("query")), styles["TextRu"])],
    ], colWidths=[45*mm, 125*mm])
    info_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F3EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(info_table)

    story.append(Paragraph("2. Ключевые риски", styles["H1Ru"]))
    risks = [x for x in checklist if x.get("status") == "risk"]
    if risks:
        for item in risks:
            story.append(Paragraph(f"<b>{p(item.get('title'))}</b>: {p(item.get('summary'))}", styles["TextRu"]))
            for d in (item.get("details") or [])[:8]:
                story.append(Paragraph(f"• {p(d)}", styles["SmallRu"]))
    else:
        story.append(Paragraph("По автоматическим источникам явные риски не выявлены.", styles["TextRu"]))

    story.append(Paragraph("3. Чек-лист проверок", styles["H1Ru"]))
    rows = [[Paragraph("Источник", styles["SmallRu"]), Paragraph("Статус", styles["SmallRu"]), Paragraph("Вывод", styles["SmallRu"])] ]
    for item in checklist:
        status = {"ok": "Проверено", "risk": "Риск", "manual_check": "Ручная проверка"}.get(item.get("status"), item.get("status"))
        rows.append([Paragraph(p(item.get("title")), styles["SmallRu"]), Paragraph(p(status), styles["SmallRu"]), Paragraph(p(item.get("summary")), styles["SmallRu"])])
    tbl = Table(rows, colWidths=[45*mm, 32*mm, 93*mm])
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F3D56")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)

    story.append(Paragraph("4. Рекомендации юриста", styles["H1Ru"]))
    for rec in recs:
        story.append(Paragraph(f"<b>{p(rec.get('title'))}</b>", styles["TextRu"]))
        story.append(Paragraph(p(rec.get("text")), styles["SmallRu"]))

    story.append(Paragraph("5. Юридическое заключение", styles["H1Ru"]))
    for block in str(report.get("legal_report") or "").split("\n\n"):
        if block.strip():
            story.append(Paragraph(p(block.strip()), styles["TextRu"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Важно", styles["H1Ru"]))
    story.append(Paragraph("Отчет носит информационно-аналитический характер, не является гарантией полной юридической безопасности сделки и не заменяет ручную юридическую проверку документов специалистом.", styles["SmallRu"]))

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
