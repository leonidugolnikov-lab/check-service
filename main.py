"""
Real Estate Seller & Property Check API — v5.0
Copyright (c) 2026 Ugolnikov SPb. All rights reserved.

v5.0 — Архитектурный рефакторинг:
- Основной метод проверки продавца: complex_by_passport (1 запрос вместо 8)
  Передаём оба варианта полей паспорта: seria/number + seriapass/numberpass
- Суды: pravo_search → фильтрация по роли + категории → scoring → pravo_cases_details
  только для значимых дел (score >= 50)
- Объект: rosreestr + nspd_cadastr (геоданные и характеристики)
- Умный polling: адаптивные интервалы, разные таймауты по методу
- Итого запросов: 3 на продавца + 2 на объект + N карточек судебных дел
"""

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# -------------------- Логирование --------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# -------------------- PDF --------------------
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable
    )
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False
    logger.warning("ReportLab не доступен. PDF-генерация отключена.")

# -------------------- Настройки --------------------
APP_VERSION = "5.0.0"
NEWDB_URL = "https://api.newdb.net/v2"
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
USE_DEEPSEEK_REPORT = os.getenv("USE_DEEPSEEK_REPORT", "0").strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
SHOW_RAW_REGISTRY_DATA = os.getenv("SHOW_RAW_REGISTRY_DATA", "0").strip().lower() in {"1", "true", "yes", "on"}

# Безопасность
ALLOWED_ORIGINS = ["https://ugolnikovspb.ru", "https://www.ugolnikovspb.ru"]
PUBLIC_WIDGET_API_KEY = os.getenv("PUBLIC_WIDGET_API_KEY", "")
ENABLE_DEBUG_NEWDB = os.getenv("ENABLE_DEBUG_NEWDB", "0").lower() in {"1", "true", "yes", "on"}
DEBUG_API_KEY = os.getenv("DEBUG_API_KEY", "")
REPORT_TTL_SECONDS = int(os.getenv("REPORT_TTL_SECONDS", "43200"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW = 3600
MAX_OWNERS = int(os.getenv("MAX_OWNERS", "50"))

# Таймауты по методу (секунды)
# Таймауты увеличены — NewDB обрабатывает complex_by_passport и pravo_search до 3-5 минут
# По логам: pravo_search тайм-аутил на 120с (ещё шёл), complex_by_passport на ~180с (ещё шёл)
METHOD_TIMEOUTS = {
    "complex_by_passport": 360,   # до 6 минут — включает 5-10 субпроверок
    "pravo_search": 300,          # до 5 минут — поиск по всем судам
    "pravo_cases_details": 120,
    "rosreestr": 300,
    "nspd_cadastr": 90,
}
DEFAULT_TIMEOUT = 180
POLL_INTERVAL_START = 5.0
POLL_INTERVAL_MAX = 30.0
POLL_INTERVAL_FACTOR = 1.5

REPORTS: Dict[str, Dict[str, Any]] = {}
_REPORT_TIMESTAMPS: Dict[str, float] = {}
_rate_limit_store: Dict[str, List[float]] = defaultdict(list)

app = FastAPI(title="Real Estate Seller & Property Check API v5", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Widget-Key", "X-Debug-Key"],
)

# -------------------- Модели --------------------
class OwnerRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    passport_series: str = ""
    passport_number: str = ""
    seria: str = ""
    seriapass: str = ""
    series: str = ""
    number: str = ""
    numberpass: str = ""
    region: Optional[int] = 0
    regioncode: Optional[int] = 0
    share: str = ""
    role: str = "owner"
    is_minor: Optional[bool] = None
    has_passport: bool = True


class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    passport_series: str = ""
    passport_number: str = ""
    seria: str = ""
    series: str = ""
    number: str = ""
    region: Optional[int] = 0
    regioncode: Optional[int] = 0
    owners: List[OwnerRequest] = Field(default_factory=list)
    representatives: List[OwnerRequest] = Field(default_factory=list)
    cadastral_number: str = ""
    cadnum: str = ""
    cadastral: str = ""
    address: str = ""
    property_query: str = ""
    skip_report: bool = False


# -------------------- Security helpers --------------------
def check_rate_limit(ip: str) -> None:
    now = time.time()
    _rate_limit_store[ip] = [ts for ts in _rate_limit_store[ip] if now - ts < RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте позже.")
    _rate_limit_store[ip].append(now)


def verify_widget_key(request: Request) -> None:
    provided = request.headers.get("X-Widget-Key", "")
    if PUBLIC_WIDGET_API_KEY and provided != PUBLIC_WIDGET_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный API-ключ виджета")


def verify_debug_key(request: Request) -> None:
    if not ENABLE_DEBUG_NEWDB:
        raise HTTPException(status_code=404, detail="Debug endpoint отключен")
    provided = request.headers.get("X-Debug-Key", "")
    if not provided or provided != DEBUG_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный debug-ключ")


def cleanup_expired_reports() -> int:
    now = time.time()
    expired = [rid for rid, ts in _REPORT_TIMESTAMPS.items() if now - ts > REPORT_TTL_SECONDS]
    for rid in expired:
        REPORTS.pop(rid, None)
        _REPORT_TIMESTAMPS.pop(rid, None)
    return len(expired)


def cleanup_rate_limit_store() -> int:
    now = time.time()
    to_delete = []
    for ip, timestamps in _rate_limit_store.items():
        _rate_limit_store[ip] = [ts for ts in timestamps if now - ts < RATE_LIMIT_WINDOW]
        if not _rate_limit_store[ip]:
            to_delete.append(ip)
    for ip in to_delete:
        del _rate_limit_store[ip]
    return len(to_delete)


# -------------------- Утилиты --------------------
def now_ru() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def digits_only(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def clean_str(value: Any) -> str:
    return str(value or "").strip()


def fio(req: CheckRequest) -> str:
    return " ".join(x for x in [req.last.strip(), req.first.strip(), req.middle.strip()] if x)


def normalize_dob(value: str) -> Tuple[str, str]:
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


def mask_inn(inn: str) -> str:
    d = digits_only(inn)
    return f"{d[:4]}****{d[-4:]}" if len(d) == 12 else ""


def calculate_age(dob_ru: str) -> Optional[int]:
    if not dob_ru:
        return None
    try:
        parts = dob_ru.split(".")
        if len(parts) != 3:
            return None
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        dt = datetime(y, m, d)
        today = datetime.now()
        age = today.year - dt.year - ((today.month, today.day) < (dt.month, dt.day))
        return age if 0 <= age <= 120 else None
    except Exception:
        return None


def parse_date_any(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip().replace("T", " ").replace("Z", "").strip()[:19]
    for fmt in ["%d.%m.%Y", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(raw, fmt)
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


def normalize_passport(req: CheckRequest) -> Tuple[str, str]:
    series = digits_only(req.passport_series or req.seria or req.series)[:4]
    number = digits_only(req.passport_number or req.number)[:6]
    return series, number


def normalize_passport_owner(owner: OwnerRequest) -> Tuple[str, str]:
    series = digits_only(owner.passport_series or owner.seriapass or owner.seria or owner.series)[:4]
    number = digits_only(owner.passport_number or owner.numberpass or owner.number)[:6]
    return series, number


def normalize_region(req: CheckRequest) -> int:
    try:
        return int(req.regioncode or req.region or 0)
    except Exception:
        return 0


def normalize_region_owner(owner: OwnerRequest) -> int:
    try:
        return int(owner.regioncode or owner.region or 0)
    except Exception:
        return 0


def normalize_property(req: CheckRequest) -> Dict[str, str]:
    query = clean_str(
        req.cadastral_number or req.cadnum or req.cadastral or req.property_query or req.address
    )
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
    s = (
        f"{int(round(n)):,}".replace(",", " ")
        if abs(n - int(n)) < 0.005
        else f"{n:,.2f}".replace(",", " ")
    )
    return f"{s} ₽"


def flatten_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def is_minor_owner(owner: OwnerRequest) -> bool:
    if owner.is_minor is not None:
        return bool(owner.is_minor)
    age = calculate_age(normalize_dob(owner.dob)[0])
    return bool(age is not None and age < 18)


def participant_label(index: int, owner: OwnerRequest, representative: bool = False) -> str:
    base = "Законный представитель" if representative or owner.role in {"representative", "guardian"} else "Собственник"
    name = " ".join(x for x in [owner.last.strip(), owner.first.strip(), owner.middle.strip()] if x)
    suffix = f": {name}" if name else ""
    return f"{base} {index}{suffix}"


def owners_from_request(req: CheckRequest) -> List[OwnerRequest]:
    if req.owners:
        return req.owners
    if any(clean_str(x) for x in [req.last, req.first, req.middle, req.dob,
                                    req.passport_series, req.passport_number,
                                    req.seria, req.series, req.number]):
        return [OwnerRequest(
            last=req.last, first=req.first, middle=req.middle, dob=req.dob,
            passport_series=req.passport_series, passport_number=req.passport_number,
            seria=req.seria, series=req.series, number=req.number,
            region=req.region, regioncode=req.regioncode, role="owner",
        )]
    return []


def strip_service_fields(data: Any) -> Any:
    forbidden = {
        "requestId", "newdb_qid", "taskId", "balance", "_http_status",
        "errors_info", "params", "datecreated", "dateupdated",
        "is_repeat", "tasks", "req", "raw", "_primary_error",
        "inn", "passport_series", "passport_number", "seria", "series", "number",
        "seriapass", "numberpass",
    }
    if isinstance(data, dict):
        return {k: strip_service_fields(v) for k, v in data.items() if k not in forbidden}
    if isinstance(data, list):
        return [strip_service_fields(x) for x in data]
    return data


# -------------------- NewDB низкоуровневые вызовы --------------------
async def newdb_post_json(client: httpx.AsyncClient, payload: dict) -> dict:
    if not NEWDB_TOKEN:
        return {"state": "failed", "errors_info": [{"error": "NEWDB_TOKEN не задан"}]}
    try:
        r = await client.post(
            NEWDB_URL,
            json=payload,
            headers={"X-API-KEY": NEWDB_TOKEN, "Content-Type": "application/json"},
            timeout=40,
        )
        try:
            data = r.json()
        except Exception:
            data = {"state": "failed", "errors_info": [{"error": r.text[:500]}]}
        data["_http_status"] = r.status_code
        return data
    except Exception as e:
        return {"state": "failed", "errors_info": [{"error": str(e)}]}


def is_newdb_error(data: dict) -> bool:
    return not isinstance(data, dict) or bool(data.get("errors_info")) or data.get("state") in {"failed", "error"}


def is_balance_error(data: dict) -> bool:
    """Проверяет что ошибка связана с нехваткой токенов/баланса NewDB."""
    if not isinstance(data, dict):
        return False
    text = flatten_text(data.get("errors_info") or data.get("error") or "").lower()
    balance_keywords = [
        "insufficient", "balance", "credit", "токен", "лимит", "limit exceeded",
        "not enough", "quota", "funds", "payment", "no credits", "пополни"
    ]
    return any(k in text for k in balance_keywords)


def has_result_status_500(data: dict) -> bool:
    t = flatten_text(data).lower()
    return "service is unavailable" in t or "parsing failed" in t or '"status": 500' in t


def result_data(resp: dict, method: str):
    """Извлекает data и полный result-блок из ответа NewDB.
    Поддерживает несколько вариантов структуры ответа:
    - Стандартный: results.METHOD.result.data
    - complex_by_passport: results.METHOD.result.data (субметоды внутри)
    - Прямой: results.METHOD.data
    """
    try:
        block = (resp.get("results") or {}).get(method)
        if not block:
            return None, None
        # Вариант 1: block.result.data (стандарт)
        result = block.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if data is not None:
                return data, result
        # Вариант 2: block.data (прямой)
        if "data" in block:
            return block["data"], block
        # Вариант 3: block сам является результатом
        if isinstance(block, (list, str)):
            return block, {}
        return None, None
    except Exception:
        return None, None


def extract_submethod_data(complex_resp: dict, submethod: str):
    """Извлекает данные субметода из ответа complex_by_passport.
    Структура: results.SUBMETHOD.result.data
    Также пробует: results.SUBMETHOD.data и results.SUBMETHOD напрямую.
    """
    try:
        results = complex_resp.get("results") or {}
        block = results.get(submethod)
        if not block:
            return None, None
        # Стандарт: block.result.data
        result = block.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if data is not None:
                return data, result
            # Иногда data — пустой список [], это валидный ответ
            if "data" in result:
                return result["data"], result
        # Прямой: block.data
        if "data" in block:
            return block["data"], block
        return None, None
    except Exception:
        return None, None


async def newdb_run(client: httpx.AsyncClient, params: dict, method: str) -> dict:
    """
    1 метод = 1 задача NewDB.
    Отправляем запрос с уникальным requestId.
    Если state != complete — опрашиваем тот же requestId с адаптивным интервалом.
    Не создаём новых задач — только читаем статус одной.
    """
    if not params:
        return {"state": "skipped"}

    timeout_sec = METHOD_TIMEOUTS.get(method, DEFAULT_TIMEOUT)
    request_id = str(uuid.uuid4())
    payload = {"params": params, "requestId": request_id}

    # Первый запрос — создаём задачу
    resp = await newdb_post_json(client, payload)

    if is_newdb_error(resp) or has_result_status_500(resp):
        err_text = flatten_text(resp)[:400]
        if is_balance_error(resp):
            logger.error(f"[{method}] НЕДОСТАТОЧНО ТОКЕНОВ NEWDB: {err_text}")
        else:
            logger.warning(f"[{method}] Ошибка при первом запросе: {err_text}")
        return resp

    state = str(resp.get("state") or "").lower()
    if state in {"complete", "done"}:
        logger.info(f"[{method}] Готово с первого запроса")
        return resp

    # Адаптивный polling — только requestId, не пересоздаём задачу
    deadline = asyncio.get_event_loop().time() + timeout_sec
    interval = POLL_INTERVAL_START
    attempt = 0

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(interval)
        interval = min(interval * POLL_INTERVAL_FACTOR, POLL_INTERVAL_MAX)
        attempt += 1

        poll_payload = {"requestId": request_id}
        resp = await newdb_post_json(client, poll_payload)

        state = str(resp.get("state") or "").lower()
        logger.info(f"[{method}] Попытка {attempt}, state={state}")

        if state in {"complete", "done"}:
            return resp
        if state in {"error", "failed"} or is_newdb_error(resp):
            return resp
        if has_result_status_500(resp):
            return resp

    # Таймаут — возвращаем последний ответ с пометкой
    resp["state"] = "timeout"
    resp["error"] = f"Таймаут {timeout_sec}с после {attempt} попыток"
    logger.warning(f"[{method}] Таймаут после {attempt} попыток")
    return resp


# -------------------- Построение payloads --------------------
def build_complex_by_passport_payload(owner: OwnerRequest) -> Optional[dict]:
    """
    Комбинированный payload для complex_by_passport.
    Передаём оба варианта полей паспорта одновременно:
    seria/number (старый формат) + seriapass/numberpass (новый формат).
    Без этого API возвращает ошибку.
    """
    series, number = normalize_passport_owner(owner)
    if not series or not number:
        logger.warning(
            f"[complex_by_passport] Пропуск — нет паспорта: "
            f"last={owner.last!r} passport_series={owner.passport_series!r} "
            f"seriapass={owner.seriapass!r} seria={owner.seria!r} series={owner.series!r} "
            f"passport_number={owner.passport_number!r} numberpass={owner.numberpass!r} number={owner.number!r}"
        )
        return None

    dob_ru, dob_iso = normalize_dob(owner.dob)
    if not dob_iso:
        logger.warning(f"[complex_by_passport] Пропуск — не распознана дата рождения: dob={owner.dob!r}")
        return None

    logger.info(f"[complex_by_passport] Payload OK: {owner.last} {owner.first}, series={series}, dob_iso={dob_iso}")

    region = normalize_region_owner(owner)

    return {
        "method": "complex_by_passport",
        "country": "ru",
        # Оба варианта полей паспорта — обязательно!
        "seria": series,
        "number": number,
        "seriapass": series,
        "numberpass": number,
        # ФИО и дата рождения
        "firstname": owner.first.strip(),
        "lastname": owner.last.strip(),
        "secondname": owner.middle.strip(),
        "dob": dob_iso,
        "regioncode": region,
    }


def build_pravo_search_payload(owner: OwnerRequest) -> Optional[dict]:
    """Поиск судебных дел по ФИО."""
    full_fio = " ".join(x for x in [owner.last.strip(), owner.first.strip(), owner.middle.strip()] if x)
    if not full_fio:
        return None

    payload = {
        "method": "pravo_search",
        "country": "ru",
        "query": full_fio,
        "lastname": owner.last.strip(),
        "firstname": owner.first.strip(),
        "secondname": owner.middle.strip(),
        "limit": 100,
    }
    return payload


def build_pravo_details_payload(case_id: Any, newdb_qid: str) -> dict:
    """Карточка конкретного судебного дела."""
    return {
        "method": "pravo_cases_details",
        "country": "ru",
        "case_id": str(case_id),
        "newdb_qid": newdb_qid,
    }


def build_rosreestr_payload(req: CheckRequest) -> Optional[dict]:
    prop = normalize_property(req)
    if not prop["address"]:
        return None
    return {
        "method": "rosreestr",
        "country": "ru",
        "address": prop["address"],
    }


def build_nspd_cadastr_payload(req: CheckRequest) -> Optional[dict]:
    prop = normalize_property(req)
    if not prop["cadastral_number"]:
        return None
    return {
        "method": "nspd_cadastr",
        "country": "ru",
        "cad_num": prop["cadastral_number"],
    }


# -------------------- Фильтрация и скоринг судебных дел --------------------

# Роли которые несут риск для продавца
RISK_ROLE_CODES = {"defendant", "debtor", "accused", "convicted"}
RISK_ROLE_TEXTS = {
    "ОТВЕТЧИК", "ДОЛЖНИК", "ОБВИНЯЕМЫЙ", "ПОДСУДИМЫЙ",
    "ОСУЖДЁННЫЙ", "АДМИНИСТРАТИВНЫЙ ОТВЕТЧИК",
}

# Роли которые НЕ несут риска — пропускаем
SAFE_ROLE_CODES = {"plaintiff", "applicant", "representative", "prosecutor", "witness", "third_party"}
SAFE_ROLE_TEXTS = {
    "ИСТЕЦ", "ЗАЯВИТЕЛЬ", "ПРЕДСТАВИТЕЛЬ", "ПРОКУРОР",
    "ТРЕТЬЕ ЛИЦО", "СВИДЕТЕЛЬ", "АДМИНИСТРАТИВНЫЙ ИСТЕЦ",
    "ЗАИНТЕРЕСОВАННОЕ ЛИЦО",
}

# Категории дел, значимые для сделки с недвижимостью
RISK_CATEGORY_PATTERNS = {
    "debt": ["займ", "кредит", "расписк", "взыскани", "долг", "задолженност"],
    "enforce": ["исполнительн", "судебный приказ", "принудительн"],
    "property": ["недвижимост", "право собственност", "выселен", "квартир", "жилищ", "жилое", "жилой"],
    "inherit": ["наследств", "завещан"],
    "bankrupt": ["банкротств", "несостоятельност", "реализация имущества"],
    "criminal": ["мошенничеств", "хищени", "уголовн", "мошенник"],
}

RISK_CATEGORY_LABELS = {
    "debt": "Долги / кредиты / займы",
    "enforce": "Исполнительное производство",
    "property": "Недвижимость / право собственности",
    "inherit": "Наследство / завещание",
    "bankrupt": "Банкротство / несостоятельность",
    "criminal": "Мошенничество / хищение (уголовное)",
}


def detect_risk_category(case: dict) -> Optional[str]:
    """Определяет категорию риска дела. Возвращает ключ или None."""
    text = (
        clean_str(case.get("category_text")) + " " +
        clean_str(case.get("case_info")) + " " +
        clean_str(case.get("case_header"))
    ).lower()

    for cat_key, patterns in RISK_CATEGORY_PATTERNS.items():
        if any(p in text for p in patterns):
            return cat_key
    return None


def get_party_roles_for_person(case: dict, full_fio: str) -> List[dict]:
    """
    Возвращает записи parties[], где party_name похоже на ФИО продавца.
    """
    if not full_fio:
        return []
    fio_lower = full_fio.lower().strip()
    parts = fio_lower.split()
    matches = []
    for party in (case.get("parties") or []):
        pname = clean_str(party.get("party_name")).lower()
        # Проверяем что хотя бы фамилия и имя совпадают
        match_count = sum(1 for p in parts if p in pname)
        if match_count >= 2:
            matches.append(party)
    return matches


def score_court_case(case: dict, owner: OwnerRequest) -> Dict[str, Any]:
    """
    Скоринг совпадения судебного дела с продавцом.
    Возвращает dict с score (0-100), matched_parties, risk_category, match_reasons, match_level.
    """
    score = 0
    reasons = []
    full_fio = " ".join(x for x in [owner.last.strip(), owner.first.strip(), owner.middle.strip()] if x)
    fio_lower = full_fio.lower()
    last_lower = owner.last.strip().lower()

    dob_ru, dob_iso = normalize_dob(owner.dob)
    region = normalize_region_owner(owner)

    # Полный текст дела для поиска
    case_text = flatten_text(case).lower()

    # 1. Проверяем parties[] — наиболее точный источник
    matched_parties = get_party_roles_for_person(case, full_fio)
    is_risk_role = False
    is_safe_role = False
    matched_role_texts = []

    for party in matched_parties:
        role_code = clean_str(party.get("role_code")).lower()
        role_text = clean_str(party.get("role_text")).upper()
        matched_role_texts.append(role_text)

        if role_code in RISK_ROLE_CODES or role_text in RISK_ROLE_TEXTS:
            is_risk_role = True
        elif role_code in SAFE_ROLE_CODES or role_text in SAFE_ROLE_TEXTS:
            is_safe_role = True

    if matched_parties:
        score += 40
        reasons.append(f"ФИО найдено в participants: {', '.join(matched_role_texts) or 'роль не определена'}")
    elif last_lower and last_lower in case_text:
        score += 10
        reasons.append("Фамилия найдена в тексте дела")

    # 2. Роль — риск или безопасная
    if is_risk_role:
        score += 5
        reasons.append("Роль: ответчик/должник/обвиняемый")
    elif is_safe_role and not is_risk_role:
        # Только безопасная роль — снижаем скор
        score = max(0, score - 20)
        reasons.append("Роль: истец/представитель/третье лицо — не несёт риска")

    # 3. Дата рождения в тексте
    if dob_ru and dob_ru in case_text:
        score += 30
        reasons.append("Дата рождения найдена в тексте")
    elif dob_iso and dob_iso in case_text:
        score += 30
        reasons.append("Дата рождения (ISO) найдена в тексте")

    # 4. Регион совпадает
    case_region = clean_str(case.get("region_code"))
    if region and case_region and str(region) == case_region:
        score += 15
        reasons.append(f"Регион совпадает: {case.get('region_name', case_region)}")
    elif region and case_region:
        reasons.append(f"Регион не совпадает: дело из {case.get('region_name', case_region)}, продавец — регион {region}")

    # 5. Категория риска
    risk_cat = detect_risk_category(case)
    if risk_cat:
        score += 10
        reasons.append(f"Категория риска: {RISK_CATEGORY_LABELS.get(risk_cat, risk_cat)}")

    score = max(0, min(100, score))

    # Определяем уровень совпадения
    if score >= 80:
        match_level = "high"
        match_label = "Высокая вероятность совпадения"
    elif score >= 50:
        match_level = "probable"
        match_label = "Вероятное совпадение"
    else:
        match_level = "weak"
        match_label = "Явных совпадений не выявлено"

    return {
        "score": score,
        "match_level": match_level,
        "match_label": match_label,
        "match_reasons": reasons,
        "is_risk_role": is_risk_role,
        "is_safe_role": is_safe_role and not is_risk_role,
        "risk_category": risk_cat,
        "risk_category_label": RISK_CATEGORY_LABELS.get(risk_cat, "") if risk_cat else "",
        "matched_parties": matched_parties,
    }


def filter_and_score_cases(cases: List[dict], owner: OwnerRequest) -> Dict[str, List[dict]]:
    """
    Фильтрует и скорирует все дела.
    Возвращает:
      - significant: score >= 50, нужна карточка
      - weak: score < 50, краткая информация
    """
    significant = []
    weak = []

    for case in cases:
        scoring = score_court_case(case, owner)
        case_with_score = {**case, "_scoring": scoring}

        # Отсекаем дела где только безопасные роли и нет категории риска
        if scoring["is_safe_role"] and not scoring["is_risk_role"] and not scoring["risk_category"]:
            # Безопасная роль без риск-категории — в слабые
            weak.append(case_with_score)
            continue

        if scoring["score"] >= 50:
            significant.append(case_with_score)
        else:
            weak.append(case_with_score)

    # Сортируем по убыванию score
    significant.sort(key=lambda x: x["_scoring"]["score"], reverse=True)
    weak.sort(key=lambda x: x["_scoring"]["score"], reverse=True)

    return {"significant": significant, "weak": weak}


def extract_newdb_qid(resp: dict) -> str:
    """Извлекает newdb_qid из ответа pravo_search для последующего pravo_cases_details."""
    try:
        params = resp.get("params") or {}
        if params.get("newdb_qid"):
            return params["newdb_qid"]
        # Иногда лежит внутри results
        results = resp.get("results") or {}
        for method_block in results.values():
            if isinstance(method_block, dict):
                inner_params = method_block.get("params") or {}
                if inner_params.get("newdb_qid"):
                    return inner_params["newdb_qid"]
        return ""
    except Exception:
        return ""


# -------------------- Классификаторы --------------------

def make_item(title: str, status: str, summary: str, url: str,
              details: Optional[List[str]] = None, data: Any = None) -> dict:
    item = {
        "title": title,
        "source": title,
        "status": status,
        "ui_status": status,
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
    }
    if data is not None:
        item["data"] = data
    return item


def ok_item(title, summary, url, details=None, data=None):
    return make_item(title, "ok", summary, url, details, data)


def risk_item(title, summary, url, details=None, data=None):
    return make_item(title, "risk", summary, url, details, data)


def manual_item(title, summary, url, details=None, data=None):
    return make_item(title, "manual_check", summary, url, details, data)


def skipped_item(title, url):
    return make_item(title, "manual_check", "Проверка не выполнялась.", url,
                     ["Недостаточно данных для запуска."])


def error_item(title, url, resp: dict):
    if resp.get("state") == "timeout":
        details = ["Источник не ответил в срок. Рекомендуется ручная проверка."]
    elif has_result_status_500(resp):
        details = ["Источник временно недоступен."]
    else:
        details = ["Ошибка источника данных."]
    return manual_item(title, "Нет данных.", url, details)


def classify_complex_by_passport(resp: dict, owner: OwnerRequest) -> List[dict]:
    """
    Разбирает ответ complex_by_passport на отдельные чеклист-пункты:
    паспорт МВД, паспорт ФНС/ИНН, ФССП, залоги, ЕГРИП.
    """
    items = []

    if not resp or str(resp.get("state") or "").lower() == "skipped":
        items.append(skipped_item("Комплексная проверка по паспорту", ""))
        return items

    if is_newdb_error(resp) or has_result_status_500(resp):
        if is_balance_error(resp):
            for title, url in [
                ("Паспорт МВД", "https://мвд.рф/сервисы-гувм"),
                ("Паспорт / ИНН ФНС", "https://service.nalog.ru/inn.do"),
                ("ФССП", "https://fssp.gov.ru/iss/ip"),
                ("Залоги (ФНП)", "https://www.reestr-zalogov.ru"),
                ("ЕГРИП / статус ИП", "https://egrul.nalog.ru"),
            ]:
                items.append(manual_item(title,
                    "Проверка недоступна — недостаточно токенов NewDB.",
                    url, ["Пополните баланс NewDB и повторите проверку."]))
        else:
            items.append(error_item("Комплексная проверка по паспорту", "", resp))
        return items

    # Если таймаут — проверяем есть ли частичные данные в results
    # Если results пустой, возвращаем timeout-ошибку для каждого субметода
    is_timeout = resp.get("state") == "timeout"
    has_any_results = bool((resp.get("results") or {}))

    if is_timeout and not has_any_results:
        for title, url in [
            ("Паспорт МВД", "https://мвд.рф/сервисы-гувм"),
            ("Паспорт / ИНН ФНС", "https://service.nalog.ru/inn.do"),
            ("ФССП", "https://fssp.gov.ru/iss/ip"),
            ("Залоги (ФНП)", "https://www.reestr-zalogov.ru"),
            ("ЕГРИП / статус ИП", "https://egrul.nalog.ru"),
        ]:
            items.append(manual_item(title,
                "Проверка не завершилась в срок — NewDB обрабатывает запрос дольше обычного.",
                url, ["Рекомендуется повторить проверку или проверить вручную по ссылке."]))
        return items

    # Используем extract_submethod_data для правильного извлечения из complex_by_passport
    # --- Паспорт МВД ---
    title_mvd = "Паспорт МВД"
    url_mvd = "https://мвд.рф/сервисы-гувм"
    mvd_data, _ = extract_submethod_data(resp, "passport_mvd")
    if mvd_data is None:
        items.append(manual_item(title_mvd, "Нет данных от МВД.", url_mvd))
    else:
        text = flatten_text(mvd_data).lower()
        if "действител" in text and "недейств" not in text:
            items.append(ok_item(title_mvd, "Паспорт действителен.", url_mvd, ["Действительный"], mvd_data))
        elif "недейств" in text or "invalid" in text:
            items.append(risk_item(title_mvd, "Паспорт может быть недействительным.", url_mvd,
                                   [flatten_text(mvd_data)[:300]], mvd_data))
        else:
            items.append(manual_item(title_mvd, "Требуется ручная проверка.", url_mvd,
                                     [flatten_text(mvd_data)[:200]]))

    # --- Паспорт ФНС / ИНН ---
    title_fns = "Паспорт / ИНН ФНС"
    url_fns = "https://service.nalog.ru/inn.do"
    fns_data, _ = extract_submethod_data(resp, "passport_fns")

    def extract_inn_from_fns(data):
        text = flatten_text(data)
        m = re.search(r"\b(\d{12})\b", text)
        return m.group(1) if m else ""

    if fns_data is None:
        items.append(manual_item(title_fns, "ИНН не получен.", url_fns))
    else:
        found_inn = extract_inn_from_fns(fns_data)
        if found_inn:
            items.append(ok_item(title_fns, "ИНН найден по паспорту.", url_fns,
                                 [f"ИНН: {mask_inn(found_inn)}"],
                                 {"inn_masked": mask_inn(found_inn)}))
        else:
            items.append(manual_item(title_fns, "ИНН не найден по паспорту.", url_fns))

    # --- ФССП ---
    title_fssp = "ФССП"
    url_fssp = "https://fssp.gov.ru/iss/ip"
    fssp_data, _ = extract_submethod_data(resp, "fssp_person")

    if fssp_data is None:
        items.append(manual_item(title_fssp, "Нет данных ФССП.", url_fssp))
    elif not isinstance(fssp_data, list):
        items.append(manual_item(title_fssp, "Нестандартный ответ ФССП.", url_fssp))
    elif not fssp_data:
        items.append(ok_item(title_fssp, "Исполнительные производства не найдены.", url_fssp, [],
                             {"all_count": 0, "active_count": 0, "actual_debt": 0}))
    else:
        def item_sum(x):
            if not isinstance(x, dict):
                return 0.0
            t = clean_str(x.get("SubjectAndDebtAmount"))
            m = re.search(r"([\d\s]+(?:[,.]\d+)?)\s*руб", t, flags=re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1).replace(" ", "").replace(",", "."))
                except Exception:
                    pass
            return 0.0

        active = [x for x in fssp_data if isinstance(x, dict) and not clean_str(x.get("CompletionDateOrReason"))]
        closed = [x for x in fssp_data if isinstance(x, dict) and clean_str(x.get("CompletionDateOrReason"))]
        active_sum = sum(item_sum(x) for x in active)
        stats = {
            "all_count": len(fssp_data),
            "active_count": len(active),
            "closed_count": len(closed),
            "actual_debt": round(active_sum, 2),
        }
        if active:
            items.append(risk_item(title_fssp, f"Активные ИП: {rub(active_sum)}.", url_fssp,
                                   [f"Активных: {len(active)}, сумма: {rub(active_sum)}"], stats))
        else:
            items.append(ok_item(title_fssp, "Только закрытые ИП.", url_fssp, [], stats))

    # --- Залоги ---
    title_pledge = "Залоги (ФНП)"
    url_pledge = "https://www.reestr-zalogov.ru"
    pledge_data, _ = extract_submethod_data(resp, "pledge_person")

    if pledge_data is None:
        items.append(manual_item(title_pledge, "Нет данных о залогах.", url_pledge))
    elif not pledge_data:
        items.append(ok_item(title_pledge, "Залоги не найдены.", url_pledge, []))
    else:
        # Извлекаем детали каждого залога для отображения
        pledge_details = ["Проверить предмет залога и кредитора до сделки."]
        pledge_display = []
        for i, p in enumerate(pledge_data if isinstance(pledge_data, list) else [pledge_data], 1):
            if not isinstance(p, dict):
                continue
            detail_parts = []
            # Предмет залога
            subj = clean_str(p.get("subject") or p.get("pledgeSubject") or p.get("description") or "")
            if subj: detail_parts.append(f"Предмет: {subj[:150]}")
            # Залогодержатель (кредитор)
            cred = clean_str(p.get("pledgeHolder") or p.get("creditor") or p.get("holderName") or "")
            if cred: detail_parts.append(f"Залогодержатель: {cred[:100]}")
            # Залогодатель
            debtor = clean_str(p.get("pledgeGiver") or p.get("debtor") or p.get("giverName") or "")
            if debtor: detail_parts.append(f"Залогодатель: {debtor[:100]}")
            # Дата регистрации
            reg_date = clean_str(p.get("registrationDate") or p.get("regDate") or p.get("noticeDate") or "")
            if reg_date: detail_parts.append(f"Дата регистрации: {reg_date}")
            # Номер уведомления
            notice = clean_str(p.get("noticeNumber") or p.get("number") or p.get("id") or "")
            if notice: detail_parts.append(f"Номер уведомления: {notice}")
            # Статус
            status = clean_str(p.get("status") or p.get("state") or "")
            if status: detail_parts.append(f"Статус: {status}")
            if detail_parts:
                pledge_details.append(f"Запись {i}: " + "; ".join(detail_parts))
            pledge_display.append(p)
        items.append(risk_item(
            title_pledge,
            f"Найдено залогов: {len(pledge_data) if isinstance(pledge_data, list) else 1}. Необходима проверка до сделки.",
            url_pledge,
            pledge_details,
            {"pledges": pledge_display, "count": len(pledge_data) if isinstance(pledge_data, list) else 1}
        ))

    # --- ЕГРИП ---
    title_ip = "ЕГРИП / статус ИП"
    url_ip = "https://egrul.nalog.ru"
    ip_data, _ = extract_submethod_data(resp, "egrul_ip")

    if ip_data is None:
        items.append(manual_item(title_ip, "Нет данных ЕГРИП.", url_ip))
    else:
        text = flatten_text(ip_data).lower()
        if any(w in text for w in ["не найден", "отсутств"]):
            items.append(ok_item(title_ip, "Статус ИП не выявлен.", url_ip, [], ip_data))
        elif any(w in text for w in ["действующ", "зарегистрирован", "огрнип"]):
            items.append(risk_item(title_ip, "Найден действующий ИП.", url_ip,
                                   ["Проверить предпринимательские долги."], ip_data))
        else:
            items.append(ok_item(title_ip, "Статус ИП не активен.", url_ip, [], ip_data))

    return items


def classify_pravo(
    pravo_resp: dict,
    details_resps: List[dict],
    owner: OwnerRequest,
    filtered: Dict[str, List[dict]],
) -> dict:
    """
    Формирует чеклист-пункт по судам общей юрисдикции.
    significant → полные карточки с пометкой "вероятное совпадение"
    weak → краткая информация + объяснение почему не считается совпадением
    """
    title = "Суды общей юрисдикции (ГАС Правосудие)"
    url = "https://sudrf.ru"

    if not pravo_resp or str(pravo_resp.get("state") or "").lower() == "skipped":
        return skipped_item(title, url)

    if is_newdb_error(pravo_resp) or has_result_status_500(pravo_resp):
        return error_item(title, url, pravo_resp)

    if pravo_resp.get("state") == "timeout":
        return manual_item(title,
            "Поиск по судебным делам не завершился в срок — NewDB обрабатывает запрос дольше обычного.",
            url, ["Рекомендуется повторить проверку или проверить вручную на портале ГАС Правосудие."])

    data, _ = result_data(pravo_resp, "pravo_search")
    if data is None:
        return manual_item(title, "Нет данных от ГАС Правосудие.", url)

    total_found = len(data) if isinstance(data, list) else 0

    if total_found == 0:
        return ok_item(title, "Судебные дела не найдены.", url, [])

    significant = filtered.get("significant") or []
    weak = filtered.get("weak") or []

    # Прикрепляем полные карточки к значимым делам
    details_by_case_id: Dict[str, dict] = {}
    for dr in (details_resps or []):
        d, _ = result_data(dr, "pravo_cases_details")
        if d and isinstance(d, list):
            for item in d:
                if isinstance(item, dict):
                    case_block = item.get("case") or {}
                    cid = str(case_block.get("case_id") or "")
                    if cid:
                        details_by_case_id[cid] = item

    significant_with_details = []
    for case in significant:
        cid = str(case.get("case_id") or "")
        detail = details_by_case_id.get(cid)
        significant_with_details.append({
            "case_summary": {
                "case_id": case.get("case_id"),
                "case_number": case.get("case_number"),
                "category_text": case.get("category_text"),
                "case_info": case.get("case_info"),
                "region_name": case.get("region_name"),
                "result_text": case.get("result_text"),
                "review_date": case.get("review_date"),
                "case_url": case.get("case_url"),
                "parties": case.get("parties"),
            },
            "scoring": case.get("_scoring"),
            "full_card": detail,
            "display_mode": "full" if detail else "summary",
            "warning": (
                "⚠️ Высокая вероятность совпадения — требуется ручная сверка"
                if (case.get("_scoring") or {}).get("score", 0) >= 80
                else "❓ Вероятное совпадение — требуется ручная сверка"
            ),
        })

    weak_summaries = []
    for case in weak:
        sc = case.get("_scoring") or {}
        reasons = sc.get("match_reasons") or []
        # Формируем короткое объяснение почему не значимое
        why_not = []
        if sc.get("is_safe_role"):
            why_not.append("роль не несёт риска (истец/представитель/третье лицо)")
        if not sc.get("risk_category"):
            why_not.append("категория не относится к значимым рискам сделки")
        if sc.get("score", 0) < 30:
            why_not.append("низкая степень совпадения ФИО")
        explanation = "; ".join(why_not) if why_not else "низкий скор совпадения"

        weak_summaries.append({
            "case_id": case.get("case_id"),
            "case_number": case.get("case_number"),
            "category_text": case.get("category_text"),
            "region_name": case.get("region_name"),
            "result_text": case.get("result_text"),
            "parties_count": len(case.get("parties") or []),
            "scoring": sc,
            "display_mode": "brief",
            "note": "✓ Алгоритм не выявил явных совпадений",
            "reason": explanation,
        })

    # Определяем итоговый статус
    high_score = [c for c in significant if (c.get("_scoring") or {}).get("score", 0) >= 80]
    probable = [c for c in significant if (c.get("_scoring") or {}).get("score", 0) < 80]

    result_data_out = {
        "total_found": total_found,
        "significant_count": len(significant),
        "weak_count": len(weak),
        "significant_cases": significant_with_details,
        "weak_cases": weak_summaries,
    }

    if high_score:
        return risk_item(
            title,
            f"Найдены дела с высокой вероятностью совпадения: {len(high_score)}.",
            url,
            [f"Значимых дел: {len(significant)} из {total_found} найденных. Требуется ручная сверка."],
            result_data_out,
        )
    elif probable:
        return manual_item(
            title,
            f"Найдены вероятные совпадения: {len(probable)}.",
            url,
            [f"Значимых дел: {len(significant)} из {total_found} найденных. Требуется идентификация."],
            result_data_out,
        )
    else:
        return ok_item(
            title,
            f"Явных совпадений не выявлено. Найдено дел по ФИО: {total_found}.",
            url,
            ["Алгоритм не выявил дел с признаками риска для данного продавца."],
            result_data_out,
        )


def classify_egrn(resp: dict) -> dict:
    title = "ЕГРН / Росреестр"
    url = "https://rosreestr.gov.ru"

    if not resp or str(resp.get("state") or "").lower() == "skipped":
        return skipped_item(title, url)
    if is_newdb_error(resp) or has_result_status_500(resp):
        return error_item(title, url, resp)

    data, _ = result_data(resp, "rosreestr")
    if not data or not isinstance(data, list):
        return manual_item(title, "Объект не найден в Росреестре.", url)

    obj = data[0] if isinstance(data[0], dict) else {}
    enc = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    enc_text = flatten_text(enc).lower()

    details = [
        f"Кадастровый номер: {obj.get('cadNumber', '—')}",
        f"Тип: {obj.get('objType_text', '—')}",
        f"Площадь: {obj.get('area', '—')} кв.м",
    ]

    has_ban = any(w in enc_text for w in ["запрещ", "арест", "ограничение регистрац"])
    has_mortgage = any(w in enc_text for w in ["ипотек", "залог"])
    has_other_enc = bool(enc) and not has_ban and not has_mortgage

    # Анализ дат прав
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    right_dates = []
    for r in rights:
        if isinstance(r, dict):
            for dk in ["registrationDate", "dateRegistration", "regDate", "startDate"]:
                dt = parse_date_any(r.get(dk))
                if dt and 1998 <= dt.year <= 2030:
                    right_dates.append(dt)
                    break

    recent_months = None
    if right_dates:
        latest = max(right_dates)
        recent_months = months_between_dates(latest)
        details.append(f"Право зарегистрировано: {latest.strftime('%d.%m.%Y')}")
        if recent_months is not None and recent_months < 12:
            details.append(f"Право зарегистрировано недавно: {recent_months} мес. назад")
        elif recent_months is not None and recent_months >= 36:
            details.append("Право зарегистрировано более 3 лет назад")

    if has_ban:
        details.append("Запрет/арест регистрации.")
        return risk_item(title, "Объект найден. Есть ограничение регистрации.", url, details, obj)
    if has_mortgage:
        details.append("Ипотека/залог.")
        return risk_item(title, "Объект найден. Есть обременение (ипотека/залог).", url, details, obj)
    if has_other_enc:
        details.append("Иное обременение.")
        return risk_item(title, "Объект найден. Есть обременение.", url, details, obj)
    if recent_months is not None and recent_months < 12:
        return risk_item(title, "Объект найден. Право зарегистрировано недавно.", url, details, obj)

    return ok_item(title, "Объект найден. Ограничений не выявлено.", url, details, obj)


def classify_nspd_cadastr(resp: dict) -> dict:
    title = "Геоданные (НСПД / кадастр)"
    url = "https://pkk.rosreestr.ru"

    if not resp or str(resp.get("state") or "").lower() == "skipped":
        return skipped_item(title, url)
    if is_newdb_error(resp) or has_result_status_500(resp):
        return error_item(title, url, resp)

    data, _ = result_data(resp, "nspd_cadastr")
    if not data or not isinstance(data, list):
        return manual_item(title, "Геоданные не найдены.", url)

    # Достаём первый items[0]
    first = data[0] if data else {}
    items_list = first.get("items") if isinstance(first, dict) else []
    if not items_list:
        return manual_item(title, "Объект по кадастровому номеру не найден.", url)

    obj = items_list[0].get("object") or {} if isinstance(items_list[0], dict) else {}
    geo = items_list[0].get("geo") or {} if isinstance(items_list[0], dict) else {}

    details = []
    if obj.get("type"):
        details.append(f"Тип: {obj['type']}")
    if obj.get("address"):
        details.append(f"Адрес: {obj['address']}")
    if obj.get("area"):
        details.append(f"Площадь: {obj['area']} кв.м")
    if obj.get("year_built"):
        details.append(f"Год постройки: {obj['year_built']}")
    if obj.get("cad_cost"):
        details.append(f"Кадастровая стоимость: {rub(obj['cad_cost'])}")
    if obj.get("status"):
        details.append(f"Статус: {obj['status']}")
    if geo.get("center"):
        c = geo["center"]
        details.append(f"Координаты: {c.get('lat')}, {c.get('lon')}")

    return ok_item(title, "Геоданные объекта получены.", url, details, {
        "object": obj,
        "geo_center": geo.get("center"),
        "geo_points": geo.get("points"),
    })


def classify_all_v5(
    complex_resp: dict,
    pravo_resp: dict,
    details_resps: List[dict],
    egrn_resp: dict,
    nspd_resp: dict,
    owner: OwnerRequest,
    filtered_cases: Dict[str, List[dict]],
) -> List[dict]:
    checklist = []

    # 1. complex_by_passport → несколько пунктов
    checklist.extend(classify_complex_by_passport(complex_resp, owner))

    # 2. Суды общей юрисдикции
    checklist.append(classify_pravo(pravo_resp, details_resps, owner, filtered_cases))

    # 3. ЕГРН
    checklist.append(classify_egrn(egrn_resp))

    # 4. Геоданные кадастра
    checklist.append(classify_nspd_cadastr(nspd_resp))

    return checklist


# -------------------- Скоринг --------------------
def risk_scoring_v5(checklist: List[dict], age: Optional[int] = None) -> dict:
    score = 0
    factor_rows = []

    def add(source, pts, text, severity="attention"):
        nonlocal score
        score += pts
        factor_rows.append({"source": source, "points": pts, "severity": severity, "text": text})

    # Возраст
    if age is not None:
        if age >= 75:
            add("Возраст", 18, "75+: ПНД обязательно", "high")
        elif age >= 70:
            add("Возраст", 14, "70+: ПНД обязательно", "high")
        elif age >= 60:
            add("Возраст", 6, "60–69: ПНД желательно", "medium")

    # Считаем сколько ключевых проверок провалилось или не выполнилось
    KEY_CHECKS = ["ФССП", "Паспорт МВД", "Паспорт / ИНН", "Суды", "Залоги", "ЕГРН", "ЕГРИП"]
    failed_key_checks = []
    for item in checklist:
        t = str(item.get("title", ""))
        st = item.get("status", "")
        sm = str(item.get("summary", "")).lower()
        if st == "manual_check" and any(k in t for k in KEY_CHECKS):
            # Нет данных = проверка не выполнилась
            if any(w in sm for w in ["нет данных", "не получен", "не выполнял", "не запуск", "ошибка"]):
                failed_key_checks.append(t)

    # Штраф за непройденные ключевые проверки — нельзя говорить "всё ок" если половина не проверена
    if len(failed_key_checks) >= 4:
        add("Непройденные проверки", 35, f"Не выполнено {len(failed_key_checks)} ключевых проверок — результат неполный", "high")
    elif len(failed_key_checks) >= 2:
        add("Непройденные проверки", 20, f"Не выполнено {len(failed_key_checks)} проверки — результат неполный", "medium")
    elif len(failed_key_checks) == 1:
        add("Непройденные проверки", 10, f"Не выполнена проверка: {failed_key_checks[0]}", "attention")

    for item in checklist:
        title = str(item.get("title", ""))
        status = item.get("status")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}

        if status == "manual_check":
            summary_text = (str(item.get("summary", "")) + " " + " ".join(item.get("details") or [])).lower()
            if "не запуск" in summary_text or "не выполня" in summary_text or "не найден" in summary_text:
                continue
            add(title, 4, "Требуется ручная проверка", "manual")
            continue

        if status != "risk":
            continue

        if "ФССП" in title:
            actual = data.get("actual_debt", 0)
            if actual > 1_000_000:
                add("ФССП", 38, f"Активные ИП: {rub(actual)}", "high")
            elif actual > 300_000:
                add("ФССП", 28, f"Активные ИП: {rub(actual)}", "high")
            elif actual > 50_000:
                add("ФССП", 18, f"Активные ИП: {rub(actual)}", "medium")
            elif actual > 0:
                add("ФССП", 10, f"Активные ИП: {rub(actual)}", "medium")
            else:
                add("ФССП", 5, "ИП найдены", "attention")
        elif "Залоги" in title:
            add("Залоги", 12, "Залоги физлица", "medium")
        elif "Паспорт МВД" in title:
            add("Паспорт МВД", 40, "Риск недействительности", "critical")
        elif "ИП" in title or "ЕГРИП" in title:
            add("ЕГРИП", 16, "Действующий ИП", "medium")
        elif "Суды" in title:
            case_data = item.get("data") or {}
            sig_count = case_data.get("significant_count", 0)
            high_cases = [
                c for c in (case_data.get("significant_cases") or [])
                if (c.get("scoring") or {}).get("score", 0) >= 80
            ]
            if high_cases:
                add("Суды ГАС", 20 + len(high_cases) * 8, f"Значимых совпадений: {len(high_cases)}", "high")
            elif sig_count:
                add("Суды ГАС", 8 + sig_count * 4, f"Вероятных совпадений: {sig_count}", "medium")
            else:
                add("Суды ГАС", 3, "Дела найдены, совпадения неточные", "manual")
        elif "ЕГРН" in title:
            if "арест" in item.get("summary", "").lower() or "запрет" in item.get("summary", "").lower():
                add("ЕГРН", 40, "Запрет/арест регистрации", "high")
            elif "ипотека" in item.get("summary", "").lower() or "залог" in item.get("summary", "").lower():
                add("ЕГРН", 18, "Ипотека/залог", "medium")
            elif "недавно" in item.get("summary", "").lower():
                add("ЕГРН", 18, "Недавняя регистрация права", "medium")
            else:
                add("ЕГРН", 12, "Иной риск по объекту", "medium")

    score = max(0, min(100, score))

    if score >= 85:
        level, label = "опасная", "Опасно при самостоятельной сделке"
        conclusion = "Высокий риск. Несколько критических факторов."
    elif score >= 60:
        level, label = "высокорискованная", "Высокий риск при самостоятельной сделке"
        conclusion = "Значимые вопросы к продавцу или объекту."
    elif score >= 35:
        level, label = "условно рискованная", "Условно рискованно"
        conclusion = "Критичный запрет не подтверждён, но есть вопросы."
    else:
        # Если есть непройденные проверки — нельзя давать зелёный свет
        if failed_key_checks:
            level, label = "неполная проверка", "Результат неполный — есть непройденные проверки"
            conclusion = f"Часть проверок не выполнилась ({', '.join(failed_key_checks[:2])} и др.). Оценка может не отражать реальных рисков. Рекомендуется повторить проверку или проверить вручную."
        else:
            level, label = "допустимая", "Стоп-факторов не выявлено"
            conclusion = "Автоматическая проверка не показала препятствий для сделки. Ручная проверка документов обязательна."

    return {
        "score": score,
        "max_score": 100,
        "level": level,
        "label": label,
        "conclusion": conclusion,
        "factor_rows": factor_rows,
    }


def build_recommendations_v5(checklist: List[dict], age: Optional[int] = None) -> List[dict]:
    recs = []
    if age is not None and age >= 70:
        recs.append({"priority": "critical", "title": "Проверка дееспособности",
                     "text": "Возраст 70+: справки ПНД/НД обязательны."})
    elif age is not None and age >= 60:
        recs.append({"priority": "medium", "title": "Проверка дееспособности",
                     "text": "60–69: ПНД/НД желательны."})

    for item in checklist:
        if item.get("status") != "risk":
            continue
        title = str(item.get("title", ""))
        if "ЕГРН" in title:
            recs.append({"priority": "high", "title": "Проверить обременения ЕГРН",
                         "text": "Получить документ-основание и порядок снятия."})
        elif "ФССП" in title:
            recs.append({"priority": "high", "title": "Закрыть ИП до сделки",
                         "text": "Прописать порядок погашения в соглашении."})
        elif "Залоги" in title:
            recs.append({"priority": "high", "title": "Снять залог до сделки",
                         "text": "Получить справку об отсутствии залогов после снятия."})
        elif "Суды" in title:
            recs.append({"priority": "medium", "title": "Проверить судебные дела",
                         "text": "Запросить карточки дел, уточнить актуальный статус."})
        elif "Паспорт МВД" in title:
            recs.append({"priority": "critical", "title": "Проверить подлинность паспорта",
                         "text": "Нотариальное заверение или оригинал в МВД."})

    recs.append({"priority": "high", "title": "Предварительный договор купли-продажи с задатком",
                 "text": "Закрепить условия возврата и ответственность."})
    return recs


def build_hidden_risks() -> List[dict]:
    return [
        {"category": "обязательно", "risk": "Супруг / согласие",
         "why": "Проверить режим собственности.", "law": "ст. 34, 35 СК РФ"},
        {"category": "обязательно", "risk": "Зарегистрированные лица",
         "why": "Кто прописан и выселение.", "law": "ЖК РФ"},
        {"category": "обязательно", "risk": "Правоустанавливающие документы",
         "why": "Основание приобретения.", "law": "ФЗ №218-ФЗ"},
        {"category": "критично", "risk": "Несовершеннолетние / опека",
         "why": "Разрешение органов опеки.", "law": "ст. 37 ГК РФ"},
        {"category": "критично", "risk": "Доверенность",
         "why": "Проверить срок и полномочия.", "law": "ст. 185-189 ГК РФ"},
    ]


def build_advance_decision(scoring: dict) -> dict:
    score = scoring.get("score", 0)
    level = scoring.get("level", "")
    if score >= 85:
        return {"decision": "Задаток не передавать", "level": "stop",
                "comment": "Сначала устранить ключевые риски."}
    if score >= 60:
        return {"decision": "Задаток только при защищённой схеме расчётов", "level": "strict_conditions",
                "comment": "Необходимо закрыть все выявленные вопросы до передачи денег."}
    if score >= 35:
        return {"decision": "Сначала документы, потом задаток", "level": "caution",
                "comment": "Закрыть выявленные вопросы до передачи задатка."}
    if level == "неполная проверка":
        return {"decision": "Задаток не передавать — проверка неполная", "level": "stop",
                "comment": "Часть проверок не выполнилась. Необходимо проверить вручную или повторить."}
    return {"decision": "Можно переходить к проверке документов", "level": "allowed",
            "comment": "Стоп-факторов не выявлено. Ручная проверка документов обязательна."}


# -------------------- DeepSeek отчёт --------------------
DEEPSEEK_SYSTEM_PROMPT = """\
Ты — старший юрист по сделкам с недвижимостью с 15-летним опытом сопровождения покупателей жилья. \
Ты составляешь экспертное юридическое заключение для покупателя, который планирует приобрести \
объект недвижимости и хочет понять риски до передачи задатка или аванса продавцу. \
Заключение читают двое: сам покупатель и сопровождающий его риелтор. \
Для покупателя важна понятность и практичность, для риелтора — профессиональная точность и ссылки на нормы. \
Совмести оба требования: пиши грамотно и конкретно, объясняй термины, не оставляй читателя \
без следующего шага.

ЯЗЫК И СТИЛЬ
• Пиши исключительно на русском языке. \
Запрещены любые английские слова, латинские аббревиатуры и иностранные термины. \
Конкретные замены: «риск» используй, «скоринг» → «оценка надёжности», \
«стоп-фактор» → «препятствие для сделки», «чек-лист» → «перечень действий», \
«дью дилидженс» → «юридическая проверка», «флаг» → «тревожный признак».
• Тон — профессиональный юридический, но живой и понятный. Без канцелярита.
• Каждый вывод о нарушении или угрозе подкрепляй конкретной нормой: \
полное наименование при первом упоминании, сокращённое при повторном. \
Пример: «статья 61.2 Федерального закона от 26.10.2002 № 127-ФЗ \
«О несостоятельности (банкротстве)»» — далее «статья 61.2 Закона о банкротстве».
• Не используй маркированные списки с тире — только с символом •.
• Не нумеруй разделы. Не пиши пустые разделы — если данных нет, раздел пропускается.
• Пиши конкретно: не «возможны риски», а «сделка может быть оспорена по основаниям \
статьи 61.2 Закона о банкротстве в течение трёх лет с даты её совершения».

ПРАВИЛА РАБОТЫ С ДАННЫМИ
• Строго опирайся на переданные данные. Не придумывай угрозы которых нет в данных.
• Не раскрывай паспортные данные, ИНН, дату рождения продавца.
• Завершённое банкротство не называй действующим. Оцениваю срок: \
менее трёх лет — повышенная осторожность, сделка оспорима по статье 61.2 Закона о банкротстве; \
более трёх лет — срок исковой давности истёк по статье 196 Гражданского кодекса \
Российской Федерации, угроза существенно снижена.
• Судебное дело с пометкой «высокая вероятность совпадения» — описывай как \
установленный факт с оговоркой: «требует финальной проверки по оригиналам документов».
• Судебное дело с пометкой «вероятное совпадение» — описывай как: \
«требует ручной идентификации личности продавца по документам».
• Если по источнику данные не получены — прямо укажи это и дай конкретную рекомендацию \
где проверить вручную с указанием ресурса.

СТРУКТУРА ЗАКЛЮЧЕНИЯ — строго в таком порядке, заголовки не менять

Краткий вывод
Два-три предложения: итоговая оценка надёжности продавца и объекта, \
главное препятствие для сделки если есть, общая рекомендация покупателю. \
Читатель должен понять суть с первых строк не читая остальное.

Что подтверждено автоматическими источниками
Каждый проверенный источник и его результат. \
Формат: «Источник → результат». \
Пример: «Министерство внутренних дел (паспорт) → документ действителен. \
Федеральная служба судебных приставов → исполнительных производств не выявлено.»

Что не подтверждено и требует ручной проверки
Для каждого непроверенного пункта: что именно → где проверить → почему важно для сделки. \
Если всё проверено — раздел пропустить.

Ключевые угрозы для покупателя
Только если угрозы есть в данных. Для каждой строго по шаблону:

Угроза: [конкретное описание что обнаружено]
Правовые последствия: [что может произойти со сделкой или правом собственности покупателя]
Норма закона: [конкретная статья и закон]
Что сделать: [конкретное действие покупателя или продавца для снятия угрозы]

Логика сделки
Это самый важный раздел — пиши его конкретно под данную ситуацию на основе результатов проверки. \
Не шаблон, а персональный план.

Сначала определи сценарий по данным и выбери соответствующую логику:

Если есть препятствие для регистрации (арест или запрет в ЕГРН, недействительный паспорт, активное банкротство):
Объясни что именно и по какой норме блокирует сделку. \
Дай последовательность действий по устранению препятствия с ответственными и сроками. \
Чётко укажи: до устранения препятствия передавать задаток или аванс напрямую продавцу категорически \
не рекомендуется — покупатель рискует потерять деньги без возможности защиты.

Если есть управляемые угрозы (долги ФССП, залоги, судебные дела, недавняя регистрация права, банкротство менее трёх лет):
Раздели план на два горизонта:

ДО ПЕРЕДАЧИ ЗАДАТКА ИЛИ АВАНСА — что обязательно закрыть до того как деньги уйдут продавцу. \
Для каждого пункта: действие → кто делает → срок → что получить на руки. \
Без закрытия этих пунктов аванс не передавать.

ПОСЛЕ АВАНСА — ДО ПОДПИСАНИЯ ОСНОВНОГО ДОГОВОРА — что проверять и готовить дальше. \
Сроки, ответственные, правовые основания.

Поскольку аванс обычно передаётся напрямую продавцу — особо укажи: \
соглашение о задатке (или авансе) должно содержать конкретный перечень документов \
которые продавец обязан предоставить, и санкцию за непредоставление — \
иначе деньги будет крайне сложно вернуть.

Если угроз нет или они незначительны:
Стандартный план с конкретными сроками:
• День 1–3: запросить у продавца полный пакет документов — \
правоустанавливающий документ (договор купли-продажи, дарения, свидетельство о наследстве \
или иное основание), расширенная выписка из Единого государственного реестра недвижимости \
давностью не более пяти рабочих дней, справка о зарегистрированных лицах (форма 9 или архивная), \
нотариально удостоверенное согласие супруга если объект приобретался в браке \
(статья 35 Семейного кодекса Российской Федерации).
• День 3–7: юридическая проверка пакета — цепочка перехода права за последние \
10–15 лет, основания приобретения, отсутствие пороков воли при предыдущих сделках \
(статьи 166–179 Гражданского кодекса Российской Федерации).
• День 7–10: подписание соглашения об авансе или предварительного договора \
с чёткими условиями возврата (статьи 380–381 Гражданского кодекса Российской Федерации \
о задатке, статья 429 об обязательности предварительного договора). \
Аванс передавать только после подписания соглашения — никогда до.
• День 10–30: подготовка основного договора, согласование расчётов \
через аккредитив или депозит нотариуса (статья 327 Гражданского кодекса \
Российской Федерации) — это защищает обе стороны.
• Финальный шаг: подписание основного договора и подача заявления \
о государственной регистрации перехода права \
(статья 551 Гражданского кодекса Российской Федерации, \
Федеральный закон от 13.07.2015 № 218-ФЗ «О государственной регистрации недвижимости»). \
Расчёт производится только после регистрации перехода права на покупателя.

ДОПОЛНИТЕЛЬНЫЕ БЛОКИ — добавляй к любому сценарию если данные это подтверждают:

Если есть несовершеннолетние собственники:
Разрешение органов опеки и попечительства — обязательно до подписания любых договоров, \
включая соглашение о задатке (или авансе). Без разрешения сделка ничтожна \
(статья 37 Гражданского кодекса Российской Федерации, статья 21 Федерального закона \
от 24.04.2008 № 48-ФЗ «Об опеке и попечительстве»). \
Срок рассмотрения органами опеки — от 15 до 30 рабочих дней. \
Это нужно закладывать в план заранее, а не после передачи аванса.

Если объект в ипотеке:
Нельзя передавать аванс напрямую продавцу до снятия ипотечного обременения. \
Схема: покупатель гасит ипотеку продавца из собственных средств через депозит нотариуса \
или аккредитив — только так деньги защищены. Прямая передача наличными \
для погашения чужого долга создаёт реальную угрозу потери средств без возможности \
возврата. После погашения — дождаться снятия обременения в Едином государственном \
реестре недвижимости (срок — 3 рабочих дня по Федеральному закону № 218-ФЗ), \
получить актуальную выписку, и только после этого двигаться к основному договору.

Если альтернативная сделка (цепочка):
Все звенья цепочки должны быть проверены — особенно конечный продавец. \
Аванс передаётся только при наличии письменных договорённостей по всей цепочке. \
Условие о том что задаток/аванс возвращается если сделка не состоится \
по причине разрыва цепочки — обязательно фиксировать письменно. \
Срок экспозиции альтернативной сделки как правило 30–60 дней — \
следить чтобы соглашение о задатке (или авансе) покрывало этот срок.

Если продавцу 70 лет и более:
Справки из психоневрологического и наркологического диспансеров — \
до подписания предварительного договора и передачи аванса. \
Оспаривание сделки по основаниям статьи 177 Гражданского кодекса \
Российской Федерации (неспособность понимать значение своих действий в момент подписания) — \
реальная судебная практика. Без справок покупатель остаётся незащищённым.

Если несколько собственников:
Согласие каждого собственника оформляется отдельно. Проверить семейное положение каждого — \
нотариально удостоверенное согласие супруга обязательно если объект приобретался в браке \
(статья 35 Семейного кодекса Российской Федерации). \
Все собственники подписывают договор купли-продажи либо выдают нотариальную доверенность \
с правом получения денег.

Как передавать задаток / аванс
Конкретные условия для данной ситуации — с учётом того что аванс передаётся напрямую продавцу:
• Форма: письменное соглашение о задатке (или авансе) — обязательно. \
Устная договорённость не имеет юридической силы.
• Что прописать в соглашении: сумму, срок действия, точный адрес и кадастровый номер объекта, \
перечень документов которые продавец обязан предоставить до основного договора, \
срок и условия возврата аванса, ответственность за нарушение срока.
• Разница между авансом и задатком: задаток/аванс возвращается в любом случае \
в той же сумме (статья 380 Гражданского кодекса Российской Федерации); \
задаток при отказе продавца возвращается в двойном размере \
(статья 381 Гражданского кодекса Российской Федерации) — \
задаток выгоднее для покупателя, но требует чёткой формулировки в тексте соглашения.
• Размер: исходя из выявленных угроз — \
если угрозы есть, передавать минимально необходимую сумму. \
Правило: передавать только ту сумму которую покупатель готов \
отстаивать в суде в худшем сценарии.
• Способ: безналичный перевод с назначением платежа «аванс по соглашению от [дата] \
за квартиру по адресу [адрес]» — это доказательство в суде. \
Наличные — только под расписку с полными паспортными данными получателя.

Итоговое заключение
Один-два абзаца. Финальная оценка: можно ли двигаться к сделке, при каких условиях, \
что является абсолютным препятствием. Если явных препятствий нет — дай уверенный вывод, \
не нагнетай там где угроз нет.

Важно
Настоящее заключение носит информационно-аналитический характер и подготовлено \
на основании автоматически полученных данных из открытых государственных источников. \
Заключение не заменяет юридическую проверку правоустанавливающих документов \
и не является юридической консультацией в смысле Федерального закона от 31.05.2002 № 63-ФЗ \
«Об адвокатской деятельности и адвокатуре в Российской Федерации». \
Для сопровождения сделки рекомендуется привлечь квалифицированного юриста или нотариуса.\
"""


def build_deepseek_user_prompt(owner: OwnerRequest, checklist: list, scoring: dict, recs: list) -> str:
    age = calculate_age(normalize_dob(owner.dob)[0])
    fio_str = " ".join(x for x in [owner.last.strip(), owner.first.strip(), owner.middle.strip()] if x)

    # Разбиваем чеклист на группы для удобства модели
    risks    = [i for i in checklist if i.get("status") == "risk"]
    oks      = [i for i in checklist if i.get("status") == "ok"]
    manual   = [i for i in checklist if i.get("status") == "manual_check"]

    def fmt_item(i):
        out = {"источник": i.get("title"), "статус": i.get("status"),
               "вывод": i.get("summary")}
        details = (i.get("details") or [])[:4]
        if details:
            out["детали"] = details
        # Для судов передаём значимые дела отдельно
        d = i.get("data")
        if isinstance(d, dict) and d.get("significant_cases"):
            out["значимые_дела"] = [
                {
                    "номер_дела": c.get("case_summary", {}).get("case_number"),
                    "категория": c.get("case_summary", {}).get("category_text"),
                    "регион": c.get("case_summary", {}).get("region_name"),
                    "результат": c.get("case_summary", {}).get("result_text"),
                    "оценка_совпадения": (c.get("scoring") or {}).get("score"),
                    "уровень": (c.get("scoring") or {}).get("match_label"),
                    "причины": (c.get("scoring") or {}).get("match_reasons"),
                    "пометка": c.get("warning"),
                }
                for c in d["significant_cases"][:5]
            ]
        return out

    # Признаки сценария — передаём явно чтобы модель не гадала
    scenario_flags = []
    checklist_text = json.dumps(checklist, ensure_ascii=False).lower()
    if "ипотек" in checklist_text or "залог" in checklist_text:
        scenario_flags.append("на объекте возможно ипотечное обременение или залог")
    if "несовершеннолетн" in checklist_text:
        scenario_flags.append("среди собственников есть несовершеннолетний")
    if "запрет" in checklist_text or "арест" in checklist_text:
        scenario_flags.append("выявлен запрет или арест регистрационных действий")
    if "банкротств" in checklist_text:
        scenario_flags.append("в данных есть сведения о банкротстве")
    if age and age >= 70:
        scenario_flags.append(f"продавец в возрасте {age} лет — требуется проверка дееспособности")

    lines = [
        f"ПРОДАВЕЦ: {fio_str}, возраст: {age or 'не определён'}",
        "",
        f"ОЦЕНКА НАДЁЖНОСТИ: {scoring.get('score')}/100 — {scoring.get('label')}",
        f"ВЫВОД СИСТЕМЫ: {scoring.get('conclusion')}",
        "",
    ]

    if scenario_flags:
        lines.append("ПРИЗНАКИ СЦЕНАРИЯ СДЕЛКИ:")
        for flag in scenario_flags:
            lines.append(f"• {flag}")
        lines.append("")

    if risks:
        lines.append("ВЫЯВЛЕННЫЕ УГРОЗЫ:")
        lines.append(json.dumps([fmt_item(i) for i in risks], ensure_ascii=False, indent=2))
        lines.append("")

    if manual:
        lines.append("ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ:")
        lines.append(json.dumps([fmt_item(i) for i in manual], ensure_ascii=False, indent=2))
        lines.append("")

    if oks:
        lines.append("ПОДТВЕРЖДЕНО АВТОМАТИЧЕСКИ:")
        lines.append(json.dumps([fmt_item(i) for i in oks], ensure_ascii=False, indent=2))
        lines.append("")

    if recs:
        lines.append("РЕКОМЕНДАЦИИ СИСТЕМЫ:")
        lines.append(json.dumps(recs, ensure_ascii=False, indent=2))

    return "\n".join(lines)


def normalize_legal_report_format(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"(?im)^\s*\d+[.)]\s+", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def is_valid_deepseek_report(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    markers = ["краткий вывод", "ключевые риски", "что проверить до аванса",
               "логика сделки", "итоговое заключение"]
    has_structure = sum(1 for m in markers if m in t) >= 2
    return has_structure and len(t) >= 900


def build_local_legal_report(owner: OwnerRequest, checklist: list, scoring: dict, recs: list) -> str:
    score = scoring.get("score", 0)
    risks = [x for x in checklist if x.get("status") == "risk"]
    oks = [x for x in checklist if x.get("status") == "ok"]
    manual = [x for x in checklist if x.get("status") == "manual_check"]
    lines = (
        ["Краткий вывод", f"Оценка: {scoring.get('label')} ({score}/100). {scoring.get('conclusion')}", "",
         "Что подтверждено автоматическими источниками"]
        + [f"• {x['title']}: {x['summary']}" for x in oks] + [""]
        + ["Что не подтверждено и требует ручной проверки"]
        + [f"• {x['title']}: {x['summary']}" for x in manual] + [""]
        + ["Ключевые риски"]
        + ([f"• {x['title']}: {x['summary']}" for x in risks]
           or ["• Подтверждённых критических рисков автоматическими источниками не выявлено."]) + [""]
        + ["Что проверить до аванса"]
        + [f"• {r['title']}: {r['text']}" for r in recs[:5]] + [""]
        + ["Логика сделки",
           "• Получить документы-основания, выписку ЕГРН, справки по зарегистрированным лицам.",
           "• Закрыть все вопросы из раздела ручной проверки.",
           "• Только после этого подписывать соглашение о задатке (или авансе).", "",
           "Как передавать задаток / аванс",
           "• Передавать аванс только по письменному соглашению с условиями возврата.", "",
           "Итоговое заключение"]
    )
    if score >= 85:
        lines.append("Сначала устранить критические факторы. Аванс без документов не передавать.")
    elif score >= 60:
        lines.append("Сделка возможна только в управляемом сценарии с документальным подтверждением.")
    elif score >= 35:
        lines.append("Умеренный риск. Основная задача — закрыть вопросы до аванса.")
    else:
        lines.append("Можно переходить к стандартной проверке документов.")
    lines += ["", "Важно",
              "Отчёт — аналитический ориентир. Не заменяет ручную юридическую проверку."]
    return normalize_legal_report_format("\n".join(lines))


async def maybe_deepseek_report(owner: OwnerRequest, checklist: list, scoring: dict, recs: list) -> str:
    fallback = build_local_legal_report(owner, checklist, scoring, recs)
    if not (USE_DEEPSEEK_REPORT and DEEPSEEK_API_KEY):
        return fallback
    try:
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT},
                {"role": "user", "content": build_deepseek_user_prompt(owner, checklist, scoring, recs)},
            ],
            "temperature": 0.25,
            "max_tokens": DEEPSEEK_MAX_TOKENS,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code != 200:
                logger.error(f"DeepSeek вернул {resp.status_code}")
                return fallback
            data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            text = re.sub(r"\b\d{12}\b", "ИНН скрыт", text)
            text = re.sub(r"\b\d{4}\s?\d{6}\b", "паспорт скрыт", text)
            text = normalize_legal_report_format(text)
            return text if is_valid_deepseek_report(text) else fallback
    except Exception as e:
        logger.exception(f"Ошибка DeepSeek: {e}")
        return fallback


# -------------------- Pipeline --------------------
async def _skipped() -> dict:
    """Заглушка для пропущенных запросов."""
    return {"state": "skipped"}


def owner_person_key(owner: OwnerRequest) -> str:
    """Уникальный ключ человека для дедупликации проверок."""
    series, number = normalize_passport_owner(owner)
    dob_ru, _ = normalize_dob(owner.dob)
    return "|".join([
        clean_str(owner.last).lower(),
        clean_str(owner.first).lower(),
        clean_str(owner.middle).lower(),
        dob_ru,
        series,
        number,
    ])


async def run_person_checks(client: httpx.AsyncClient, owner: OwnerRequest) -> Dict[str, Any]:
    """
    Запускает проверки для одного продавца.
    Несовершеннолетние пропускаются полностью (is_minor=True).
    Запрос 1: complex_by_passport (только если есть паспорт)
    Запрос 2: pravo_search (по ФИО)
    Запрос 3..N: pravo_cases_details для значимых дел (score >= 50), лимит 10
    """
    result: Dict[str, Any] = {
        "owner_key": owner_person_key(owner),
        "is_minor": is_minor_owner(owner),
        "complex": None,
        "pravo_search": None,
        "pravo_details": [],
        "filtered_cases": {"significant": [], "weak": []},
    }

    # Несовершеннолетние — не проверяем совсем
    if is_minor_owner(owner):
        logger.info(f"Пропуск несовершеннолетнего: {owner.last} {owner.first}")
        return result

    complex_payload = build_complex_by_passport_payload(owner)
    pravo_payload = build_pravo_search_payload(owner)

    logger.info(f"Запуск проверки: {owner.last} {owner.first} (роль: {owner.role})")

    tasks = [
        newdb_run(client, complex_payload, "complex_by_passport") if complex_payload else _skipped(),
        newdb_run(client, pravo_payload, "pravo_search") if pravo_payload else _skipped(),
    ]

    raw = await asyncio.gather(*tasks, return_exceptions=True)
    result["complex"] = raw[0] if not isinstance(raw[0], Exception) else {
        "state": "failed", "errors_info": [{"error": str(raw[0])}]
    }
    result["pravo_search"] = raw[1] if not isinstance(raw[1], Exception) else {
        "state": "failed", "errors_info": [{"error": str(raw[1])}]
    }

    # Фильтруем и скорируем дела
    pravo_data, _ = result_data(result["pravo_search"], "pravo_search")
    if pravo_data and isinstance(pravo_data, list):
        filtered = filter_and_score_cases(pravo_data, owner)
        result["filtered_cases"] = filtered

        significant = filtered.get("significant") or []
        if significant:
            newdb_qid = extract_newdb_qid(result["pravo_search"])
            logger.info(f"Запрос карточек: {len(significant)} значимых дел, qid={newdb_qid}")

            detail_tasks = [
                newdb_run(client, build_pravo_details_payload(c["case_id"], newdb_qid), "pravo_cases_details")
                for c in significant[:10] if c.get("case_id")
            ]
            if detail_tasks:
                detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)
                result["pravo_details"] = [
                    r if not isinstance(r, Exception) else {"state": "failed"}
                    for r in detail_results
                ]

    return result


async def run_property_checks(client: httpx.AsyncClient, req: CheckRequest) -> Dict[str, Any]:
    """Параллельно запускает rosreestr и nspd_cadastr для объекта."""
    egrn_payload = build_rosreestr_payload(req)
    nspd_payload = build_nspd_cadastr_payload(req)
    logger.info("Запуск: rosreestr + nspd_cadastr")
    tasks = [
        newdb_run(client, egrn_payload, "rosreestr") if egrn_payload else _skipped(),
        newdb_run(client, nspd_payload, "nspd_cadastr") if nspd_payload else _skipped(),
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "egrn": raw[0] if not isinstance(raw[0], Exception) else {"state": "failed"},
        "nspd": raw[1] if not isinstance(raw[1], Exception) else {"state": "failed"},
    }


async def build_full_report_v5(req: CheckRequest, include_debug: bool = False) -> dict:
    owners = owners_from_request(req)
    if not owners:
        raise HTTPException(status_code=400, detail="Не переданы данные продавца.")
    if len(owners) > MAX_OWNERS:
        raise HTTPException(status_code=400, detail=f"Слишком много собственников (макс. {MAX_OWNERS})")

    # Дедупликация: представители уже проверенные как собственники не дублируются.
    # Несовершеннолетние помечаются, их проверки пропускаются.
    seen_keys: set = set()
    unique_owners = []
    for owner in owners:
        key = owner_person_key(owner).strip("|")
        if key and key in seen_keys:
            import logging as _log
            _log.getLogger(__name__).info(f"Дедупликация: {owner.last} {owner.first} уже в списке.")
            continue
        if key:
            seen_keys.add(key)
        unique_owners.append(owner)
    owners = unique_owners

    async with httpx.AsyncClient() as client:
        # Все собственники + объект параллельно
        person_tasks = [run_person_checks(client, owner) for owner in owners]
        property_task = run_property_checks(client, req)

        all_results = await asyncio.gather(*person_tasks, property_task, return_exceptions=True)

    person_results = all_results[:-1]
    property_result = all_results[-1] if not isinstance(all_results[-1], Exception) else {"egrn": {"state": "failed"}, "nspd": {"state": "skipped"}}

    egrn_resp = property_result.get("egrn") or {"state": "skipped"}
    nspd_resp = property_result.get("nspd") or {"state": "skipped"}

    # Строим чеклист и скоринг для каждого собственника
    all_checklists = []
    all_scorings = []
    all_reports = []
    participants_out = []

    for i, (owner, person_res) in enumerate(zip(owners, person_results), 1):
        if isinstance(person_res, Exception):
            person_res = {"complex": {"state": "failed"}, "pravo_search": {"state": "failed"},
                          "pravo_details": [], "filtered_cases": {}}

        label = participant_label(i, owner)
        age = calculate_age(normalize_dob(owner.dob)[0])
        is_minor = is_minor_owner(owner)

        if is_minor:
            minor_item = {
                "title": f"{label} — Несовершеннолетний",
                "source": label,
                "status": "manual_check",
                "ui_status": "manual_check",
                "summary": "Проверки не выполнялись.",
                "details": [
                    "Сделка с участием несовершеннолетнего требует разрешения органов опеки.",
                    "Без этого разрешения сделка может быть оспорена.",
                    "Рекомендуется нотариальное удостоверение.",
                ],
                "manual_check_url": "",
            }
            all_checklists.append([minor_item])
            participants_out.append({"label": label, "is_minor": True, "age": age})
            continue

        checklist = classify_all_v5(
            complex_resp=person_res.get("complex") or {},
            pravo_resp=person_res.get("pravo_search") or {},
            details_resps=person_res.get("pravo_details") or [],
            egrn_resp=egrn_resp,
            nspd_resp=nspd_resp,
            owner=owner,
            filtered_cases=person_res.get("filtered_cases") or {},
        )

        # Добавляем префикс владельца к каждому пункту если несколько собственников
        if len(owners) > 1:
            for item in checklist:
                if "ЕГРН" not in item.get("title", "") and "Геоданные" not in item.get("title", ""):
                    item["title"] = f"{label} — {item.get('title', '')}"

        scoring = risk_scoring_v5(checklist, age=age)
        recs = build_recommendations_v5(checklist, age=age)
        legal = "" if req.skip_report else await maybe_deepseek_report(owner, checklist, scoring, recs)

        all_checklists.append(checklist)
        all_scorings.append(scoring)
        all_reports.append(legal)

        participants_out.append({
            "label": label,
            "is_minor": False,
            "age": age,
            "score": scoring["score"],
            "level": scoring["level"],
        })

    # Объединяем если несколько собственников — берём максимальный скор
    if all_scorings:
        combined_scoring = max(all_scorings, key=lambda s: s["score"])
    else:
        combined_scoring = {"score": 0, "level": "допустимая", "label": "Нет данных",
                            "conclusion": "Данные не получены.", "factor_rows": []}

    combined_checklist = [item for cl in all_checklists for item in cl]
    # ЕГРН и геоданные добавляем один раз (они уже в первом чеклисте)
    combined_recs = build_recommendations_v5(combined_checklist)
    combined_legal = all_reports[0] if all_reports else ""

    report_id = str(uuid.uuid4())

    result = {
        "success": True,
        "report_id": report_id,
        "pdf_available": REPORTLAB_AVAILABLE,
        "pdf_url": f"/download-pdf/{report_id}" if REPORTLAB_AVAILABLE else None,
        "created_at": now_ru(),
        "api_version": APP_VERSION,
        "executive_summary": {
            "label": combined_scoring["label"],
            "level": combined_scoring["level"],
            "score": combined_scoring["score"],
            "max_score": 100,
            "conclusion": combined_scoring["conclusion"],
        },
        "checklist": strip_service_fields(combined_checklist),
        "risk_scoring": combined_scoring,
        "recommendations": combined_recs,
        "advance_decision": build_advance_decision(combined_scoring),
        "hidden_risks": build_hidden_risks(),
        "legal_report": combined_legal,
        "participants": participants_out,
        "notes": [f"v{APP_VERSION} — complex_by_passport + pravo_search с scoring + rosreestr + nspd_cadastr"],
        "report_skipped": req.skip_report,
    }

    if SHOW_RAW_REGISTRY_DATA and include_debug:
        result["debug"] = {
            "person_results": [strip_service_fields(pr) for pr in person_results if not isinstance(pr, Exception)],
            "property": strip_service_fields(property_result),
        }

    stored = dict(result)
    REPORTS[report_id] = stored
    _REPORT_TIMESTAMPS[report_id] = time.time()

    return result


# -------------------- PDF (сохранена из v4, без изменений) --------------------
def pdf_report_blocks(text):
    if not text:
        return []
    headings = {
        "Краткий вывод", "Что подтверждено автоматическими источниками",
        "Что не подтверждено и требует ручной проверки", "Ключевые риски",
        "Что проверить до аванса", "Логика сделки", "Как передавать задаток / аванс",
        "Итоговое заключение", "Важно"
    }
    blocks = []
    current_paragraph = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            if current_paragraph:
                blocks.append(("p", " ".join(current_paragraph)))
                current_paragraph = []
            continue
        if stripped in headings:
            if current_paragraph:
                blocks.append(("p", " ".join(current_paragraph)))
                current_paragraph = []
            blocks.append(("h", stripped))
        elif stripped.startswith("•") or stripped.startswith("-"):
            if current_paragraph:
                blocks.append(("p", " ".join(current_paragraph)))
                current_paragraph = []
            blocks.append(("bullet", stripped[1:].strip()))
        else:
            current_paragraph.append(stripped)
    if current_paragraph:
        blocks.append(("p", " ".join(current_paragraph)))
    return blocks


class Palette:
    DARK_BLUE = "#0A1F3F"; WHITE = "#FFFFFF"; OFF_WHITE = "#F8F7F4"
    MID_GRAY = "#6E7F8D"; DARK_TEXT = "#1A1A1A"
    CRITICAL = "#C0392B"; HIGH = "#E67E22"; MEDIUM = "#D4A373"
    LOW = "#2EAD63"; MANUAL = "#7F8C8D"
    CRITICAL_BG = "#FDF0ED"; HIGH_BG = "#FEF7ED"; MEDIUM_BG = "#FEF9F3"
    LOW_BG = "#EDF7F1"; MANUAL_BG = "#F5F3EF"

    @staticmethod
    def for_score(score):
        if score >= 85: return Palette.CRITICAL
        if score >= 60: return Palette.HIGH
        if score >= 35: return Palette.MEDIUM
        return Palette.LOW

    @staticmethod
    def for_severity(sev):
        return {"critical": Palette.CRITICAL, "high": Palette.HIGH, "medium": Palette.MEDIUM,
                "attention": Palette.LOW, "manual": Palette.MANUAL}.get(sev, Palette.MID_GRAY)

    @staticmethod
    def bg_for_severity(sev):
        return {"critical": Palette.CRITICAL_BG, "high": Palette.HIGH_BG, "medium": Palette.MEDIUM_BG,
                "attention": Palette.LOW_BG, "manual": Palette.MANUAL_BG}.get(sev, Palette.OFF_WHITE)


def register_pdf_font():
    if not REPORTLAB_AVAILABLE:
        return "Helvetica"
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("AppFont", path))
                return "AppFont"
            except Exception:
                pass
    return "Helvetica"


def p(text):
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def build_pdf_bytes(report):
    if not REPORTLAB_AVAILABLE:
        return json.dumps({"error": "PDF generation not available"}, ensure_ascii=False).encode("utf-8")
    font = register_pdf_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=13*mm, bottomMargin=14*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Z_Title", fontName=font, fontSize=20, leading=25, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_Subtitle", fontName=font, fontSize=8.5, leading=11, textColor=Palette.MID_GRAY))
    styles.add(ParagraphStyle("Z_H1", fontName=font, fontSize=13, leading=17, spaceBefore=10, spaceAfter=5, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_H2", fontName=font, fontSize=10.5, leading=14, spaceBefore=7, spaceAfter=3, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_Body", fontName=font, fontSize=9.5, leading=14, textColor=Palette.DARK_TEXT, spaceAfter=4))
    styles.add(ParagraphStyle("Z_CardTitle", fontName=font, fontSize=10, leading=13, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_CardText", fontName=font, fontSize=9, leading=13, textColor=Palette.DARK_TEXT))
    styles.add(ParagraphStyle("Z_Detail", fontName=font, fontSize=8.5, leading=12, textColor=Palette.MID_GRAY, leftIndent=8))
    styles.add(ParagraphStyle("Z_ScoreNum", fontName=font, fontSize=26, leading=30, alignment=TA_CENTER, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_ScoreLbl", fontName=font, fontSize=9, leading=12, alignment=TA_CENTER, textColor=Palette.WHITE))
    styles.add(ParagraphStyle("Z_TableHead", fontName=font, fontSize=8.5, leading=11, textColor=Palette.WHITE))
    styles.add(ParagraphStyle("Z_TableCell", fontName=font, fontSize=8.5, leading=12, textColor=Palette.DARK_TEXT))
    styles.add(ParagraphStyle("Z_TableSub", fontName=font, fontSize=7.5, leading=10.5, textColor=Palette.MID_GRAY))

    scoring = report.get("risk_scoring") or {}
    checklist = report.get("checklist") or []
    legal_text = report.get("legal_report") or ""
    score = scoring.get("score", 0)
    risk_color = Palette.for_score(score)
    advance = report.get("advance_decision") or {}

    story = []

    STATUS_LABELS = {"ok": "ОК", "risk": "РИСК", "manual_check": "РУЧНАЯ"}
    STATUS_COLORS = {
        "ok": (Palette.LOW_BG, Palette.LOW),
        "risk": (Palette.HIGH_BG, Palette.CRITICAL),
        "manual_check": (Palette.MANUAL_BG, Palette.MID_GRAY),
    }
    STATUS_ICONS = {"ok": "✓", "risk": "!", "manual_check": "?"}

    def badge_label(lbl):
        return {"Опасно при самостоятельной сделке": "ОПАСНО",
                "Высокий риск при самостоятельной сделке": "ВЫСОКИЙ РИСК",
                "Условно рискованно": "УСЛОВНЫЙ РИСК",
                "Допустимо к рассмотрению": "ДОПУСТИМО"}.get(lbl, str(lbl).upper())

    def add_section_header(title):
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(p(title), styles["Z_H1"]))
        story.append(Spacer(1, 2*mm))

    def add_colored_block(title, body_lines, bg, border="#E0DDD6"):
        content = []
        if title:
            content.append(Paragraph(p(title), styles["Z_CardTitle"]))
            content.append(Spacer(1, 1.5*mm))
        for line in (body_lines if isinstance(body_lines, list) else [body_lines]):
            if line:
                content.append(Paragraph(p(str(line)), styles["Z_CardText"]))
        tbl = Table([[content]], colWidths=[174*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0),(-1,-1), colors.HexColor(bg)),
            ("BOX", (0,0),(-1,-1), 0.5, colors.HexColor(border)),
            ("LEFTPADDING",(0,0),(-1,-1), 9),("RIGHTPADDING",(0,0),(-1,-1), 9),
            ("TOPPADDING",(0,0),(-1,-1), 7),("BOTTOMPADDING",(0,0),(-1,-1), 7),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 2.5*mm))

    # ── ШАПКА ──
    header_left = [
        Paragraph("Комплексная проверка продавца<br/>и объекта недвижимости", styles["Z_Title"]),
        Spacer(1, 2*mm),
        Paragraph(f"Дата формирования: {report.get('created_at', '—')}", styles["Z_Subtitle"]),
    ]
    score_badge = Table([
        [Paragraph(badge_label(str(scoring.get("label",""))), styles["Z_ScoreLbl"])],
        [Paragraph(str(score), styles["Z_ScoreNum"])],
        [Paragraph("из 100", styles["Z_Subtitle"])],
    ], colWidths=[42*mm])
    score_badge.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.HexColor(risk_color)),
        ("BACKGROUND",(0,1),(-1,-1), colors.HexColor(Palette.OFF_WHITE)),
        ("BOX",(0,0),(-1,-1), 0.6, colors.HexColor("#E0DDD6")),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
    ]))
    story.append(Table([[header_left, score_badge]], colWidths=[130*mm, 46*mm]))
    story.append(Spacer(1, 5*mm))

    # ── ВЫВОД И ЗАДАТОК ──
    add_colored_block("Главный вывод", scoring.get("conclusion","—"), Palette.OFF_WHITE)
    adv_bg = Palette.HIGH_BG if score >= 60 else (Palette.MEDIUM_BG if score >= 35 else Palette.LOW_BG)
    add_colored_block(
        "Решение по задатку / авансу",
        [f"{advance.get('decision','—')}.", advance.get('comment','')],
        adv_bg
    )

    # ── КАРТА ПРОВЕРОК С ДЕТАЛЯМИ ──
    add_section_header("Результаты проверок")
    for item in checklist:
        st = item.get("status","")
        bg_col, fg_col = STATUS_COLORS.get(st, (Palette.OFF_WHITE, Palette.MID_GRAY))
        icon = STATUS_ICONS.get(st,"?")
        lbl = STATUS_LABELS.get(st,"?")

        # Заголовок строки
        title_cell = [Paragraph(p(f"{icon}  {item.get('title','')}"), styles["Z_CardTitle"])]
        status_cell = [Paragraph(lbl, styles["Z_CardTitle"])]
        summary_cell = [Paragraph(p(item.get("summary","")), styles["Z_CardText"])]

        # Данные из data (залоги, ФССП и тд) — если есть, детали не дублируем
        data = item.get("data")
        has_rich_data = isinstance(data, dict) and any([
            data.get("active_count") is not None,
            data.get("pledges"),
            data.get("cadNumber") or data.get("area"),
            data.get("geo_center"),
        ])

        # Детали — только если нет расширенных данных
        if not has_rich_data:
            details = (item.get("details") or [])[:6]
            if details:
                for d in details:
                    summary_cell.append(Paragraph(p(f"— {d}"), styles["Z_Detail"]))
        if isinstance(data, dict):
            # ФССП
            if data.get("active_count") is not None:
                summary_cell.append(Paragraph(
                    p(f"Активных: {data.get('active_count',0)}, закрытых: {data.get('closed_count',0)}, долг: {rub(data.get('actual_debt',0))}"),
                    styles["Z_Detail"]
                ))
            # Залоги
            pledges = data.get("pledges")
            if pledges:
                for pl in pledges[:3]:
                    parts = []
                    for k,label in [("subject","Предмет"),("pledgeHolder","Залогодержатель"),
                                    ("pledgeGiver","Залогодатель"),("registrationDate","Дата"),
                                    ("noticeNumber","Номер уведомления")]:
                        v = clean_str(pl.get(k,""))
                        if v: parts.append(f"{label}: {v[:80]}")
                    if parts:
                        summary_cell.append(Paragraph(p(" | ".join(parts)), styles["Z_Detail"]))
            # ЕГРН объект
            if data.get("cadNumber") or data.get("area") or data.get("objType_text"):
                egrn_parts = []
                if data.get("cadNumber"): egrn_parts.append(f"Кад. номер: {data['cadNumber']}")
                if data.get("objType_text"): egrn_parts.append(f"Тип: {data['objType_text']}")
                if data.get("area"): egrn_parts.append(f"Площадь: {data['area']} кв.м")
                if data.get("rights"):
                    rights = data["rights"] if isinstance(data["rights"], list) else []
                    for r in rights[:2]:
                        if isinstance(r, dict):
                            owner_n = clean_str(r.get("rightHolder") or r.get("owner") or "")
                            reg_d = clean_str(r.get("registrationDate") or r.get("regDate") or "")
                            right_t = clean_str(r.get("rightType") or r.get("type") or "")
                            if owner_n: egrn_parts.append(f"Правообладатель: {owner_n[:60]}")
                            if right_t: egrn_parts.append(f"Вид права: {right_t}")
                            if reg_d: egrn_parts.append(f"Дата регистрации: {reg_d}")
                if data.get("encumbrances"):
                    enc = data["encumbrances"] if isinstance(data["encumbrances"], list) else []
                    for e in enc[:2]:
                        if isinstance(e, dict):
                            enc_t = clean_str(e.get("type") or e.get("encumbranceType") or "")
                            enc_h = clean_str(e.get("holder") or e.get("encumbranceHolder") or "")
                            if enc_t: egrn_parts.append(f"Обременение: {enc_t}")
                            if enc_h: egrn_parts.append(f"Держатель: {enc_h[:60]}")
                for ep in egrn_parts:
                    summary_cell.append(Paragraph(p(ep), styles["Z_Detail"]))
            # Геоданные
            if data.get("geo_center"):
                gc = data["geo_center"]
                obj = data.get("object") or {}
                geo_parts = []
                if obj.get("address"): geo_parts.append(f"Адрес: {obj['address'][:100]}")
                if obj.get("cad_cost"): geo_parts.append(f"Кад. стоимость: {rub(obj['cad_cost'])}")
                if obj.get("year_built"): geo_parts.append(f"Год постройки: {obj['year_built']}")
                if gc.get("lat") and gc.get("lon"):
                    geo_parts.append(f"Координаты: {gc['lat']}, {gc['lon']}")
                for gp in geo_parts:
                    summary_cell.append(Paragraph(p(gp), styles["Z_Detail"]))

        row = [[title_cell, status_cell, summary_cell]]
        rt = Table(row, colWidths=[52*mm, 18*mm, 104*mm])
        rt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), colors.HexColor(bg_col)),
            ("GRID",(0,0),(-1,-1), 0.3, colors.HexColor("#E0DDD6")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
        ]))
        story.append(rt)
        story.append(Spacer(1, 1.5*mm))

    # ── ЮРИДИЧЕСКОЕ ЗАКЛЮЧЕНИЕ ──
    add_section_header("Юридическое заключение")
    HEADINGS_PDF = ["Краткий вывод","Что подтверждено автоматическими источниками",
        "Что не подтверждено и требует ручной проверки","Ключевые угрозы для покупателя",
        "Логика сделки","Как передавать задаток / аванс","Как передавать аванс",
        "Итоговое заключение","Важно"]
    for block_type, text in pdf_report_blocks(legal_text):
        if not text.strip():
            continue
        if block_type == "h":
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph(p(text), styles["Z_H2"]))
        elif block_type == "bullet":
            story.append(Paragraph(f"• {p(text)}", styles["Z_Body"]))
        else:
            story.append(Paragraph(p(text), styles["Z_Body"]))

    # ── РЕКОМЕНДАЦИИ ──
    recs = report.get("recommendations") or []
    if recs:
        add_section_header("Рекомендации")
        for rec in recs:
            pri = rec.get("priority","")
            bg = Palette.CRITICAL_BG if pri=="critical" else (Palette.HIGH_BG if pri=="high" else Palette.OFF_WHITE)
            add_colored_block(rec.get("title",""), rec.get("text",""), bg)

    # ── РУЧНАЯ ПРОВЕРКА ──
    hidden = report.get("hidden_risks") or []
    if hidden:
        add_section_header("Также рекомендуется проверить вручную")
        hr_data = [[
            Paragraph("Что проверить", styles["Z_TableHead"]),
            Paragraph("Зачем", styles["Z_TableHead"]),
            Paragraph("Норма", styles["Z_TableHead"]),
        ]]
        for r in hidden:
            hr_data.append([
                Paragraph(p(r.get("risk","")), styles["Z_TableCell"]),
                Paragraph(p(r.get("why","")), styles["Z_TableCell"]),
                Paragraph(p(r.get("law","")), styles["Z_TableSub"]),
            ])
        hr_tbl = Table(hr_data, colWidths=[55*mm, 80*mm, 39*mm], repeatRows=1)
        hr_tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor(Palette.DARK_BLUE)),
            ("GRID",(0,0),(-1,-1), 0.3, colors.HexColor("#E0DDD6")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
            ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor(Palette.OFF_WHITE), colors.HexColor(Palette.WHITE)]),
        ]))
        story.append(hr_tbl)

    # ── ДИСКЛЕЙМЕР ──
    story.append(Spacer(1, 8*mm))
    add_colored_block(None,
        "Настоящее заключение носит информационно-аналитический характер. "
        "Подготовлено на основании данных из открытых государственных реестров. "
        "Не заменяет юридическую проверку правоустанавливающих документов. "
        "Рекомендуется привлечь квалифицированного юриста или нотариуса.",
        Palette.OFF_WHITE
    )

    doc.build(story)
    return buf.getvalue()


# -------------------- Endpoints --------------------
@app.on_event("startup")
async def startup():
    async def cleanup_loop():
        while True:
            await asyncio.sleep(600)
            expired = cleanup_expired_reports()
            cleaned = cleanup_rate_limit_store()
            if expired > 0 or cleaned > 0:
                logger.info(f"Очистка: отчётов={expired}, rate_limit={cleaned}")
    asyncio.create_task(cleanup_loop())


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "newdb_token": bool(NEWDB_TOKEN),
        "deepseek_key": bool(DEEPSEEK_API_KEY),
        "reportlab": REPORTLAB_AVAILABLE,
        "max_owners": MAX_OWNERS,
    }


@app.get("/health/deep")
def health_deep():
    return {
        "ok": True,
        "version": APP_VERSION,
        "active_reports": len(REPORTS),
        "rate_limit_store_size": sum(len(v) for v in _rate_limit_store.values()),
        "ttl_seconds": REPORT_TTL_SECONDS,
        "rate_limit_max": RATE_LIMIT_MAX,
        "max_owners": MAX_OWNERS,
    }


@app.post("/check-report")
async def check_report(req: CheckRequest, request: Request):
    verify_widget_key(request)
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip)
    cleanup_expired_reports()
    try:
        return await build_full_report_v5(req, include_debug=False)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Ошибка формирования отчёта: {e}")
        return {
            "success": False,
            "message": "Ошибка формирования отчёта.",
            "error": str(e) if ENABLE_DEBUG_NEWDB else None,
        }


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest, request: Request):
    verify_debug_key(request)
    try:
        return await build_full_report_v5(req, include_debug=True)
    except Exception as e:
        logger.exception(f"Ошибка debug: {e}")
        return {"success": False, "error": str(e)}


@app.get("/download-pdf/{report_id}")
def download_pdf(report_id: str):
    report = REPORTS.get(report_id)
    if not report:
        return StreamingResponse(
            io.BytesIO(b"Report not found or expired (12h TTL). Please run the check again."),
            media_type="text/plain",
            status_code=404,
        )
    if not REPORTLAB_AVAILABLE:
        return StreamingResponse(
            io.BytesIO(json.dumps({"error": "PDF not available"}).encode()),
            media_type="application/json",
            status_code=503,
        )
    try:
        pdf = build_pdf_bytes(report)
        filename = f"real_estate_report_{report_id[:8]}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as e:
        logger.exception(f"Ошибка генерации PDF: {e}")
        return StreamingResponse(
            io.BytesIO(f"PDF error: {str(e)}".encode()),
            media_type="text/plain",
            status_code=500,
        )


# -------------------- Запуск --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
