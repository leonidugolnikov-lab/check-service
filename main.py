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
import base64
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
from urllib.parse import quote

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
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
SHOW_RAW_REGISTRY_DATA = os.getenv("SHOW_RAW_REGISTRY_DATA", "0").strip().lower() in {"1", "true", "yes", "on"}
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
REPORT_FROM_EMAIL = os.getenv("REPORT_FROM_EMAIL", "Ugolnikov SPb <reports@ugolnikovspb.ru>").strip()
REPORT_REPLY_TO_EMAIL = os.getenv("REPORT_REPLY_TO_EMAIL", "").strip()

# Безопасность
ALLOWED_ORIGINS = [
    "https://ugolnikovspb.ru",
    "https://www.ugolnikovspb.ru",
    "null",  # local file:// widget during testing
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
PUBLIC_WIDGET_API_KEY = os.getenv("PUBLIC_WIDGET_API_KEY", "")
ENABLE_DEBUG_NEWDB = os.getenv("ENABLE_DEBUG_NEWDB", "0").lower() in {"1", "true", "yes", "on"}
DEBUG_API_KEY = os.getenv("DEBUG_API_KEY", "")
REPORT_TTL_SECONDS = int(os.getenv("REPORT_TTL_SECONDS", "43200"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW = 3600
MAX_OWNERS = int(os.getenv("MAX_OWNERS", "50"))

# Cloudflare Turnstile (бесплатная капча) — опциональная защита от ботов.
# Регистрация: https://dash.cloudflare.com/?to=/:account/turnstile
# Если TURNSTILE_SECRET не задан — проверка отключена.
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "").strip()
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Кеш ответов NewDB. Снижает расход токенов на повторных проверках.
NEWDB_CACHE_ENABLED = os.getenv("NEWDB_CACHE_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
NEWDB_CACHE_TTL = int(os.getenv("NEWDB_CACHE_TTL", "86400"))  # 24 часа
NEWDB_CACHE_MAX = int(os.getenv("NEWDB_CACHE_MAX", "500"))

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
    is_married: Optional[bool] = None  # Для блока «согласие супруга» (ст.35 СК)
    married_via_object: Optional[bool] = None  # объект приобретался в браке


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
    email: str = ""
    skip_report: bool = False
    turnstile_token: str = ""  # токен Cloudflare Turnstile с фронта
    consent: bool = False       # согласие на обработку ПД (ФЗ-152)


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


def clean_email(value: Any) -> str:
    email = clean_str(value).lower()
    if not email:
        return ""
    if len(email) > 254 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=422, detail="Введите корректный email для отправки отчёта.")
    return email


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


ROSREESTR_CODES = {
    "001001000000": "Собственность",
    "001002000000": "Долевая собственность",
    "001003000000": "Совместная собственность",
    "206001000000": "Нежилое помещение",
    "206002000000": "Жилое помещение",
    "205001000000": "Квартира",
    "205002000000": "Комната",
    "022001000000": "Сервитут",
    "022002000000": "Арест",
    "022003000000": "Запрещение регистрации",
    "022006000000": "Аренда",
    "022007000000": "Ипотека",
    "022008000000": "Ипотека в силу закона",
    "022012000000": "Запрет действий в кадастровом учёте",
    "022098000000": "Иное ограничение / обременение",
    "022099000000": "Иные ограничения / обременения прав",
    "1": "Актуальный",
}


def format_registry_value(value: Any, *, kind: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if value.get("readableAddress"):
            return clean_str(value.get("readableAddress"))
        if value.get("address"):
            return clean_str(value.get("address"))
        parts = [
            value.get("region"),
            value.get("locality") or value.get("city"),
            " ".join(str(x) for x in [value.get("streetType"), value.get("street")] if x),
            " ".join(str(x) for x in [value.get("houseType"), value.get("house")] if x),
            " ".join(str(x) for x in [value.get("buildingType"), value.get("building")] if x),
            " ".join(str(x) for x in [value.get("structureType"), value.get("structure")] if x),
            " ".join(str(x) for x in [value.get("apartmentType"), value.get("apartment")] if x),
        ]
        return ", ".join(clean_str(x) for x in parts if clean_str(x))
    s = clean_str(value)
    if kind == "code" or re.match(r"^\d{12}$", s):
        return f"{ROSREESTR_CODES[s]} ({s})" if s in ROSREESTR_CODES else s
    return s


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
    """Проверяет ошибку 500 ТОЛЬКО на верхнем уровне ответа.
    Не заглядывает в results.*.result — там субметоды могут иметь
    собственные 500 (например fssp_person) не блокируя остальные данные.
    """
    if not isinstance(data, dict):
        return False
    # Верхний уровень: _http_status, state, errors_info
    if data.get("_http_status") == 500:
        return True
    top_state = str(data.get("state") or "").lower()
    if top_state in {"error", "failed"}:
        err_text = flatten_text(data.get("errors_info") or data.get("error") or "").lower()
        if "service is unavailable" in err_text or "parsing failed" in err_text:
            return True
    # Проверяем errors_info на верхнем уровне
    err_text = flatten_text(data.get("errors_info") or "").lower()
    if "service is unavailable" in err_text or "parsing failed" in err_text:
        return True
    return False


def submethod_has_error_500(data: dict, submethod: str) -> bool:
    """Проверяет что конкретный субметод вернул 500 (например fssp_person)."""
    try:
        block = (data.get("results") or {}).get(submethod) or {}
        result = block.get("result") or {}
        return result.get("status") == 500
    except Exception:
        return False


def is_complex_effectively_complete(resp: dict) -> bool:
    """Считает complex_by_passport завершённым если все субметоды кроме fssp готовы.
    Это нужно когда state='in progress' но fssp завис — остальные 10 субметодов уже есть.
    """
    if not isinstance(resp, dict):
        return False
    state = str(resp.get("state") or "").lower()
    if state in {"complete", "done"}:
        return True

    steps = resp.get("steps") or {}
    if not steps:
        return False

    # Все субметоды кроме fssp_person должны быть complete
    KEY_SUBMETHODS = {
        "terrorist", "passport_mvd", "passport_fns", "pledge_person",
        "pravo_search", "egrul_ip", "bankrot_person", "arbitr_person",
        "nalog_debt", "fns_block_person",
    }
    for sm in KEY_SUBMETHODS:
        step = steps.get(sm) or {}
        if str(step.get("status") or "").lower() not in {"complete", "done"}:
            return False  # Ключевой субметод ещё не готов

    results = resp.get("results") or {}
    if not results:
        return False

    logger.info(f"[complex_by_passport] Эффективно завершён: все ключевые субметоды complete, fssp может быть in progress")
    return True


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
    Поддерживает:
    - Стандартный: results.SUBMETHOD.result.data
    - Fallback (синтетический): results.SUBMETHOD напрямую из отдельного запроса
    - Прямой: results.SUBMETHOD.data
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
            if "data" in result:
                return result["data"], result

        # Прямой: block.data
        if "data" in block:
            return block["data"], block

        # Fallback: block сам является блоком results из отдельного запроса
        # Структура: {"result": {"data": [...]}, "state": "complete"}
        if isinstance(block, dict) and block.get("state") in {"complete", "done"}:
            inner = block.get("result") or {}
            if "data" in inner:
                return inner["data"], inner

        # block — список или строка напрямую
        if isinstance(block, (list, str)):
            return block, {}

        return None, None
    except Exception:
        return None, None


# -------------------- Кеш NewDB --------------------
# Простой in-memory LRU-кеш для ответов NewDB. Снижает расход токенов на повторных
# проверках одного и того же человека/объекта в течение TTL (по умолчанию 24ч).
# При перезапуске процесса кеш обнуляется — это допустимо.
import hashlib
from collections import OrderedDict

_NEWDB_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_NEWDB_CACHE_STATS = {"hits": 0, "misses": 0, "saves": 0, "skipped": 0}


def _newdb_cache_key(method: str, params: dict) -> str:
    """Стабильный ключ от method+params (без requestId)."""
    try:
        clean = {k: v for k, v in (params or {}).items() if k != "requestId"}
        norm = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        norm = str(params)
    raw = f"{method}|{norm}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _newdb_cache_get(key: str) -> Optional[dict]:
    if not NEWDB_CACHE_ENABLED:
        return None
    entry = _NEWDB_CACHE.get(key)
    if not entry:
        _NEWDB_CACHE_STATS["misses"] += 1
        return None
    expires_at, data = entry
    if time.time() > expires_at:
        _NEWDB_CACHE.pop(key, None)
        _NEWDB_CACHE_STATS["misses"] += 1
        return None
    # LRU bump
    _NEWDB_CACHE.move_to_end(key)
    _NEWDB_CACHE_STATS["hits"] += 1
    return data


def _newdb_cache_maybe_set(key: str, method: str, resp: dict) -> None:
    """Кешируем ТОЛЬКО полностью успешные ответы.
    НЕ кешируем: ошибки, таймауты, 500 в субметодах, ответы с _fssp_unavailable."""
    if not NEWDB_CACHE_ENABLED or not isinstance(resp, dict):
        return
    state = str(resp.get("state") or "").lower()
    if state not in {"complete", "done"}:
        _NEWDB_CACHE_STATS["skipped"] += 1
        return
    if is_newdb_error(resp) or has_result_status_500(resp):
        _NEWDB_CACHE_STATS["skipped"] += 1
        return
    if resp.get("errors_info"):
        _NEWDB_CACHE_STATS["skipped"] += 1
        return
    # Не кешируем complex с упавшим fssp — при следующей проверке хочется попробовать заново
    if resp.get("_fssp_unavailable"):
        _NEWDB_CACHE_STATS["skipped"] += 1
        return
    # complex_by_passport: проверяем что все субметоды реально complete
    if method == "complex_by_passport":
        steps = resp.get("steps") or {}
        for step_name, step in steps.items():
            if str(step.get("status") or "").lower() not in {"complete", "done"}:
                _NEWDB_CACHE_STATS["skipped"] += 1
                return

    expires_at = time.time() + NEWDB_CACHE_TTL
    try:
        # Снимаем копию чтобы не хранить ссылку которая может измениться
        _NEWDB_CACHE[key] = (expires_at, json.loads(json.dumps(resp)))
    except Exception:
        return
    _NEWDB_CACHE_STATS["saves"] += 1
    # LRU eviction
    while len(_NEWDB_CACHE) > NEWDB_CACHE_MAX:
        _NEWDB_CACHE.popitem(last=False)


def newdb_cache_stats() -> dict:
    return {
        **_NEWDB_CACHE_STATS,
        "size": len(_NEWDB_CACHE),
        "max": NEWDB_CACHE_MAX,
        "ttl": NEWDB_CACHE_TTL,
        "enabled": NEWDB_CACHE_ENABLED,
    }


async def newdb_run(client: httpx.AsyncClient, params: dict, method: str) -> dict:
    """
    1 метод = 1 задача NewDB.
    Отправляем запрос с уникальным requestId.
    Если state != complete — опрашиваем тот же requestId с адаптивным интервалом.
    Не создаём новых задач — только читаем статус одной.
    Использует in-memory кеш (TTL 24ч) для успешных ответов чтобы экономить токены.
    """
    if not params:
        return {"state": "skipped"}

    # ----- Кеш -----
    cache_key = _newdb_cache_key(method, params)
    cached = _newdb_cache_get(cache_key)
    if cached is not None:
        logger.info(f"[{method}] Кеш-попадание (key={cache_key[:12]}...)")
        # Возвращаем КОПИЮ чтобы случайные правки не повредили кеш
        return json.loads(json.dumps(cached))

    timeout_sec = METHOD_TIMEOUTS.get(method, DEFAULT_TIMEOUT)
    request_id = str(uuid.uuid4())
    # NewDB API требует обёртку: {"params": {...}, "requestId": "..."}
    payload = {"params": params, "requestId": request_id}

    def _ret(r):
        """Возвращает ответ и сохраняет в кеш если ответ удачный."""
        _newdb_cache_maybe_set(cache_key, method, r)
        return r

    # Первый запрос — создаём задачу
    resp = await newdb_post_json(client, payload)

    if is_newdb_error(resp) or has_result_status_500(resp):
        err_text = flatten_text(resp)[:400]
        if is_balance_error(resp):
            logger.error(f"[{method}] НЕДОСТАТОЧНО ТОКЕНОВ NEWDB: {err_text}")
        else:
            logger.warning(f"[{method}] Ошибка при первом запросе: {err_text}")
        return _ret(resp)

    state = str(resp.get("state") or "").lower()
    if state in {"complete", "done"}:
        logger.info(f"[{method}] Готово с первого запроса")
        return _ret(resp)

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
            # Логируем если есть errors_info даже при complete
            if resp.get("errors_info"):
                logger.warning(f"[{method}] complete с ошибками: {flatten_text(resp.get('errors_info'))[:300]}")
            return _ret(resp)

        # Для complex_by_passport: принимаем "in progress" если все ключевые субметоды готовы
        if method == "complex_by_passport" and state == "in progress":
            if is_complex_effectively_complete(resp):
                logger.info(f"[{method}] Принят как complete (fssp завис, остальное готово)")
                resp["state"] = "complete"
                resp["_fssp_unavailable"] = True
                return _ret(resp)

        if state in {"error", "failed"} or is_newdb_error(resp):
            err_info = flatten_text(resp.get("errors_info") or resp.get("error") or resp)[:400]
            if is_balance_error(resp):
                logger.error(f"[{method}] НЕДОСТАТОЧНО ТОКЕНОВ: {err_info}")
            else:
                logger.warning(f"[{method}] Ошибка (state={state}): {err_info}")
            return _ret(resp)
        if has_result_status_500(resp):
            logger.warning(f"[{method}] Ошибка 500: {flatten_text(resp)[:200]}")
            return _ret(resp)

    # Таймаут — возвращаем последний ответ с пометкой
    resp["state"] = "timeout"
    resp["error"] = f"Таймаут {timeout_sec}с после {attempt} попыток"
    logger.warning(f"[{method}] Таймаут после {attempt} попыток")
    return _ret(resp)


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
              details: Optional[List[str]] = None, data: Any = None,
              links: Optional[List[Dict[str, str]]] = None) -> dict:
    item = {
        "title": title,
        "source": title,
        "status": status,
        "ui_status": status,
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
        "manual_links": links or [],
    }
    if data is not None:
        item["data"] = data
    return item


def ok_item(title, summary, url, details=None, data=None, links=None):
    return make_item(title, "ok", summary, url, details, data, links)


def risk_item(title, summary, url, details=None, data=None, links=None):
    return make_item(title, "risk", summary, url, details, data, links)


def manual_item(title, summary, url, details=None, data=None, links=None):
    return make_item(title, "manual_check", summary, url, details, data, links)


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


def classify_complex_by_passport(resp: dict, owner: OwnerRequest, fssp_retry_resp: dict = None) -> List[dict]:
    """
    Разбирает ответ complex_by_passport на отдельные чеклист-пункты.
    fssp_retry_resp оставлен для обратной совместимости; отдельный retry ФССП отключён,
    чтобы не расходовать дополнительные токены NewDB.
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
                "Проверка не завершилась в срок.",
                url, ["Рекомендуется повторить или проверить вручную."]))
        return items

    # -----------------------------------------------------------------------
    # 1. Паспорт МВД
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # 2. Паспорт ФНС / ИНН — показываем ПОЛНЫЙ ИНН на экране и в отчёте
    # -----------------------------------------------------------------------
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
            items.append(ok_item(
                title_fns,
                f"ИНН найден: {found_inn}",
                url_fns,
                [f"ИНН: {found_inn}"],
                {"inn": found_inn, "inn_masked": mask_inn(found_inn)},
            ))
        else:
            items.append(manual_item(title_fns, "ИНН не найден по паспорту.", url_fns))

    # -----------------------------------------------------------------------
    # 3. Список террористов / экстремистов (Росфинмониторинг)
    # -----------------------------------------------------------------------
    title_terror = "Реестр террористов (Росфинмониторинг)"
    url_terror = "https://fedsfm.ru/documents/terrorists-catalog-article-6"
    terror_data, _ = extract_submethod_data(resp, "terrorist")
    if terror_data is None:
        items.append(manual_item(title_terror, "Нет данных.", url_terror))
    else:
        suggestions = []
        if isinstance(terror_data, list):
            for item_t in terror_data:
                if isinstance(item_t, dict):
                    suggestions.extend(item_t.get("suggestions") or [])
        if suggestions:
            items.append(risk_item(title_terror,
                f"Найден в реестре террористов: {len(suggestions)} совпадений.",
                url_terror, [str(s)[:200] for s in suggestions[:3]], {"suggestions": suggestions}))
        else:
            items.append(ok_item(title_terror, "В реестре не найден.", url_terror, []))

    # -----------------------------------------------------------------------
    # 4. ФССП — используем retry если основной завис
    # -----------------------------------------------------------------------
    title_fssp = "ФССП"
    url_fssp = "https://fssp.gov.ru/iss/ip"

    # Приоритет: retry > из complex
    fssp_resp_to_use = None
    if fssp_retry_resp and str(fssp_retry_resp.get("state") or "").lower() in {"complete", "done"}:
        fssp_data_raw, _ = result_data(fssp_retry_resp, "fssp_person")
        fssp_data = fssp_data_raw
        logger.info(f"[ФССП] Используем retry результат")
    else:
        fssp_data, _ = extract_submethod_data(resp, "fssp_person")
        # Проверяем что это не 500
        if fssp_data and isinstance(fssp_data, dict) and fssp_data.get("status") == 500:
            fssp_data = None

    if fssp_data is None:
        # Различаем "технически недоступно" vs "просто нет данных"
        is_unavailable = (
            resp.get("_fssp_unavailable")
            or submethod_has_error_500(resp, "fssp_person")
            or (fssp_retry_resp and str(fssp_retry_resp.get("state") or "").lower() in {"timeout", "error", "failed"})
        )
        if is_unavailable:
            # Готовим параметры для прямой ссылки на форму ФССП — заполнено максимум данных
            dob_ru, _ = normalize_dob(owner.dob)
            fssp_search_url = (
                "https://fssp.gov.ru/iss/ip"
                f"?searchType=physical"
                f"&firstname={quote(owner.first or '')}"
                f"&lastname={quote(owner.last or '')}"
                f"&secondname={quote(owner.middle or '')}"
                f"&dateOfBirth={quote(dob_ru or '')}"
            )
            details = [
                "Источник Федеральной службы судебных приставов временно недоступен — "
                "это техническая ошибка на стороне государственного сервиса, а не отсутствие данных.",
                "До сделки обязательно проверьте продавца вручную:",
                f"1. Откройте форму поиска ФССП по ссылке ниже",
                f"2. Введите ФИО продавца ({owner.last} {owner.first} {owner.middle}) "
                f"и дату рождения ({dob_ru})",
                "3. Нажмите «Найти». Результат сохраните или сделайте скриншот.",
                "Это критическая проверка — без неё передавать задаток НЕЛЬЗЯ.",
            ]
            items.append(manual_item(
                title_fssp,
                "⚠️ Источник временно недоступен. Обязательная ручная проверка.",
                url_fssp,
                details,
                {"unavailable": True},
                links=[
                    {"label": "Открыть форму поиска ФССП с заполненными данными", "url": fssp_search_url},
                    {"label": "Главная страница базы ФССП", "url": url_fssp},
                ],
            ))
        else:
            items.append(manual_item(title_fssp, "Нет данных ФССП.", url_fssp,
                                     ["Рекомендуется проверить вручную на сайте ФССП."],
                                     links=[{"label": "Проверить на сайте ФССП", "url": url_fssp}]))
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

        def summarize_fssp_item(x):
            if not isinstance(x, dict):
                return {}
            return {
                "subject": clean_str(x.get("SubjectAndDebtAmount") or x.get("subject") or x.get("debt") or ""),
                "department": clean_str(x.get("Department") or x.get("department") or ""),
                "bailiff": clean_str(x.get("Bailiff") or x.get("bailiff") or ""),
                "ip_number": clean_str(x.get("IpNumber") or x.get("ip_number") or x.get("number") or ""),
                "start_date": clean_str(x.get("StartDate") or x.get("start_date") or x.get("ExcitedDate") or ""),
                "completion": clean_str(x.get("CompletionDateOrReason") or x.get("completion") or ""),
                "sum": round(item_sum(x), 2),
            }

        active = [x for x in fssp_data if isinstance(x, dict) and not clean_str(x.get("CompletionDateOrReason"))]
        closed = [x for x in fssp_data if isinstance(x, dict) and clean_str(x.get("CompletionDateOrReason"))]
        active_sum = sum(item_sum(x) for x in active)
        stats = {
            "all_count": len(fssp_data),
            "active_count": len(active),
            "closed_count": len(closed),
            "actual_debt": round(active_sum, 2),
            "active_items": [summarize_fssp_item(x) for x in active[:20]],
            "closed_items": [summarize_fssp_item(x) for x in closed[:20]],
        }
        if active:
            items.append(risk_item(title_fssp, f"Активные ИП: {rub(active_sum)}.", url_fssp,
                                   [f"Активных: {len(active)}, сумма: {rub(active_sum)}",
                                    f"Закрытых: {len(closed)}"], stats))
        else:
            items.append(ok_item(title_fssp, f"Активных ИП нет. Закрытых: {len(closed)}.", url_fssp,
                                 ["Закрытые ИП не являются текущим долгом, но показываются для полной картины."], stats))

    # -----------------------------------------------------------------------
    # 5. Залоги продавца (ФНП) — это реестр движимого имущества, не ЕГРН
    # -----------------------------------------------------------------------
    title_pledge = "Залоги продавца (ФНП, движимое имущество)"
    url_pledge = "https://www.reestr-zalogov.ru"
    pledge_data, _ = extract_submethod_data(resp, "pledge_person")

    # Извлекаем fnp_urls из ответа (ссылки на уведомления о залоге)
    pledge_fnp_urls = []
    try:
        results_block = resp.get("results") or {}
        pledge_block = results_block.get("pledge_person") or {}
        pledge_result = pledge_block.get("result") or {}
        pledge_raw = pledge_result.get("data") or []
        if isinstance(pledge_raw, list) and pledge_raw:
            first_pledge = pledge_raw[0] if isinstance(pledge_raw[0], dict) else {}
            pledge_fnp_urls = first_pledge.get("fnp_urls") or []
    except Exception:
        pass

    if pledge_data is None:
        items.append(manual_item(title_pledge, "Нет данных ФНП о залогах движимого имущества.", url_pledge))
    elif not pledge_data and not pledge_fnp_urls:
        items.append(ok_item(title_pledge, "Уведомления ФНП о залогах движимого имущества не найдены.", url_pledge, []))
    else:
        # Есть данные залогов или ссылки на уведомления
        pledge_details = []
        pledge_display = []
        pledge_count = 0

        if isinstance(pledge_data, list) and pledge_data:
            pledge_count = len(pledge_data)
            for i, p in enumerate(pledge_data, 1):
                if not isinstance(p, dict):
                    continue
                detail_parts = []
                subj = clean_str(p.get("subject") or p.get("pledgeSubject") or p.get("description") or "")
                if subj: detail_parts.append(f"Предмет: {subj[:150]}")
                cred = clean_str(p.get("pledgeHolder") or p.get("creditor") or p.get("holderName") or "")
                if cred: detail_parts.append(f"Залогодержатель: {cred[:100]}")
                debtor = clean_str(p.get("pledgeGiver") or p.get("debtor") or p.get("giverName") or "")
                if debtor: detail_parts.append(f"Залогодатель: {debtor[:100]}")
                reg_date = clean_str(p.get("registrationDate") or p.get("regDate") or p.get("noticeDate") or "")
                if reg_date: detail_parts.append(f"Дата: {reg_date}")
                notice = clean_str(p.get("noticeNumber") or p.get("number") or p.get("id") or "")
                if notice: detail_parts.append(f"Уведомление: {notice}")
                if detail_parts:
                    pledge_details.append(f"Запись {i}: " + "; ".join(detail_parts))
                pledge_display.append(p)

        if pledge_fnp_urls:
            pledge_details.append("Ссылки для ручной проверки уведомлений ФНП:")
            for url_link in pledge_fnp_urls[:5]:
                pledge_details.append(f"• {url_link}")

        if pledge_fnp_urls:
            pledge_count = max(pledge_count, len(pledge_fnp_urls))
        if not pledge_count and pledge_fnp_urls:
            pledge_details.insert(0, "Найдены уведомления ФНП — требуется ручная сверка залогодателя и предмета залога.")

        pledge_details.insert(0, "Это не обременение объекта недвижимости и не ипотека по ЕГРН.")
        pledge_details.insert(1, "ФНП показывает уведомления о залоге движимого имущества, например автомобиля или оборудования.")

        # Подготавливаем структурированные ссылки для виджета
        pledge_links_list = []
        for u in pledge_fnp_urls[:5]:
            pledge_links_list.append({"label": "Уведомление о залоге (ФНП)", "url": u})
        # Плюс ссылка на сам реестр
        pledge_links_list.append({"label": "Реестр залогов (ручная проверка)", "url": url_pledge})

        items.append(risk_item(
            title_pledge,
            f"Найдены уведомления ФНП: {pledge_count}. Это требует сверки, но не означает залог квартиры.",
            url_pledge,
            pledge_details,
            {"pledges": pledge_display, "count": pledge_count, "fnp_urls": pledge_fnp_urls},
            links=pledge_links_list,
        ))

    # -----------------------------------------------------------------------
    # 6. Банкротство (ЕФРСБ) — со ссылками на федресурс и арбитраж
    # -----------------------------------------------------------------------
    title_bankrot = "Банкротство (ЕФРСБ)"
    url_bankrot = "https://fedresurs.ru"
    bankrot_data, _ = extract_submethod_data(resp, "bankrot_person")

    if bankrot_data is None:
        items.append(manual_item(title_bankrot, "Нет данных ЕФРСБ.", url_bankrot))
    elif not bankrot_data or (isinstance(bankrot_data, list) and not any(
        (d.get("bankruptcy") or []) for d in bankrot_data if isinstance(d, dict)
    )):
        items.append(ok_item(title_bankrot, "Банкротных дел не найдено.", url_bankrot, []))
    else:
        bankrot_details = []
        bankrot_links = []
        bankrot_status_list = []

        for entry in (bankrot_data if isinstance(bankrot_data, list) else [bankrot_data]):
            if not isinstance(entry, dict):
                continue
            for bcase in (entry.get("bankruptcy") or []):
                if not isinstance(bcase, dict):
                    continue
                case_num = clean_str(bcase.get("case_number") or "")
                case_status = clean_str(bcase.get("status") or "")
                case_url = clean_str(bcase.get("case_url") or "")
                bankrot_status_list.append(case_status)

                line = f"Дело {case_num}: {case_status}"
                bankrot_details.append(line)

                if case_url:
                    bankrot_links.append({"label": f"Дело {case_num} на Федресурсе", "url": case_url})
                    bankrot_details.append(f"  → Федресурс: {case_url}")

                # Сообщения о завершении / освобождении от долгов
                for msg in (bcase.get("messages") or [])[:3]:
                    if isinstance(msg, dict):
                        msg_url = clean_str(msg.get("url") or "")
                        msg_type = clean_str(msg.get("type") or "")
                        if msg_url and "освобожд" in msg_type.lower():
                            bankrot_details.append(f"  → {msg_type}: {msg_url}")

        # Ручная проверка
        bankrot_details.append(f"Проверить вручную: {url_bankrot}")

        is_completed = any("завершен" in s.lower() or "завершено" in s.lower()
                          for s in bankrot_status_list)
        is_active = any("реализац" in s.lower() or "наблюден" in s.lower() or "введен" in s.lower()
                       for s in bankrot_status_list)

        if is_active:
            summary = "⚠️ Активное банкротное производство — сделка невозможна до завершения."
            items.append(risk_item(title_bankrot, summary, url_bankrot, bankrot_details,
                                   {"cases": bankrot_status_list, "links": bankrot_links, "active": True},
                                   links=bankrot_links))
        elif is_completed:
            summary = "Банкротство завершено. Проверить дату — сделки до 3 лет могут быть оспорены."
            items.append(risk_item(title_bankrot, summary, url_bankrot, bankrot_details,
                                   {"cases": bankrot_status_list, "links": bankrot_links, "active": False},
                                   links=bankrot_links))
        else:
            items.append(manual_item(title_bankrot, "Найдены банкротные дела — требуется ручная проверка.",
                                     url_bankrot, bankrot_details,
                                     {"cases": bankrot_status_list, "links": bankrot_links},
                                     links=bankrot_links))

    # -----------------------------------------------------------------------
    # 7. Арбитражные дела (КАД Арбитр) — со ссылками на kad.arbitr.ru
    # -----------------------------------------------------------------------
    title_arbitr = "Арбитражные дела (КАД Арбитр)"
    url_arbitr = "https://kad.arbitr.ru"
    arbitr_data, _ = extract_submethod_data(resp, "arbitr_person")

    if arbitr_data is None:
        items.append(manual_item(title_arbitr, "Нет данных арбитража.", url_arbitr))
    elif not arbitr_data or (isinstance(arbitr_data, list) and not arbitr_data):
        items.append(ok_item(title_arbitr, "Арбитражных дел не найдено.", url_arbitr, []))
    else:
        arbitr_details = []
        arbitr_links = []
        arbitr_case_count = 0

        for acase in (arbitr_data if isinstance(arbitr_data, list) else [arbitr_data]):
            if not isinstance(acase, dict):
                continue
            arbitr_case_count += 1
            case_num = clean_str(acase.get("case_number") or "")
            case_status = clean_str(acase.get("status") or "")
            source_url = clean_str(acase.get("source_url") or "")

            line = f"Дело {case_num}: {case_status}"
            arbitr_details.append(line)

            if source_url:
                arbitr_links.append({"label": f"Дело {case_num}", "url": source_url})
                arbitr_details.append(f"  → КАД Арбитр: {source_url}")

            # PDF ссылки на акты
            for act in (acase.get("acts") or [])[:2]:
                if isinstance(act, dict) and act.get("link"):
                    arbitr_details.append(f"  → Акт {act.get('date','')}: {act['link']}")

        arbitr_details.append(f"Проверить вручную: {url_arbitr}")

        # Проверяем тип дел (банкротство или другое)
        all_text = flatten_text(arbitr_data).lower()
        is_bankrupt_arbitr = "банкротств" in all_text or "несостоятел" in all_text

        if is_bankrupt_arbitr:
            summary = f"Арбитражные дела: {arbitr_case_count} (в т.ч. банкротство). Требуется ручная проверка."
            items.append(risk_item(title_arbitr, summary, url_arbitr, arbitr_details,
                                   {"count": arbitr_case_count, "links": arbitr_links},
                                   links=arbitr_links))
        else:
            summary = f"Найдено арбитражных дел: {arbitr_case_count}."
            items.append(manual_item(title_arbitr, summary, url_arbitr, arbitr_details,
                                     {"count": arbitr_case_count, "links": arbitr_links},
                                     links=arbitr_links))

    # -----------------------------------------------------------------------
    # 8. Налоговая задолженность (ФНС)
    # -----------------------------------------------------------------------
    title_nalog = "Налоговая задолженность (ФНС)"
    url_nalog = "https://service.nalog.ru/debt"
    nalog_data, _ = extract_submethod_data(resp, "nalog_debt")

    if nalog_data is None:
        items.append(manual_item(title_nalog, "Нет данных о налоговой задолженности.", url_nalog))
    else:
        nalog_text = flatten_text(nalog_data).lower()
        # Проверяем наличие долга
        has_debt = False
        debt_amount = None
        if isinstance(nalog_data, list):
            for nd in nalog_data:
                if isinstance(nd, dict):
                    debt_info = nd.get("debt") or {}
                    if isinstance(debt_info, dict):
                        amount = debt_info.get("amount") or {}
                        if isinstance(amount, dict) and amount.get("value"):
                            has_debt = True
                            debt_amount = amount.get("value")

        if has_debt:
            items.append(risk_item(title_nalog,
                f"Налоговая задолженность: {debt_amount}.",
                url_nalog, [f"Сумма долга: {debt_amount}"], nalog_data))
        else:
            # Нет долга
            updated_text = ""
            if isinstance(nalog_data, list) and nalog_data:
                debt_block = nalog_data[0].get("debt") or {} if isinstance(nalog_data[0], dict) else {}
                updated_text = clean_str(debt_block.get("updated_text") or "")
            note = f"Долгов нет ({updated_text})" if updated_text else "Долгов нет."
            items.append(ok_item(title_nalog, note, url_nalog, []))

    # -----------------------------------------------------------------------
    # 9. Блокировка счетов ФНС
    # -----------------------------------------------------------------------
    title_block = "Блокировка счетов (ФНС)"
    url_block = "https://service.nalog.ru/zd.do"
    block_data, _ = extract_submethod_data(resp, "fns_block_person")

    if block_data is None:
        items.append(manual_item(title_block, "Нет данных о блокировках.", url_block))
    elif isinstance(block_data, list) and not block_data:
        items.append(ok_item(title_block, "Блокировок счетов не найдено.", url_block, []))
    else:
        block_text = flatten_text(block_data)
        if block_text and block_text != "[]":
            items.append(risk_item(title_block, "Найдена блокировка счетов ФНС.", url_block,
                                   [block_text[:300]], block_data))
        else:
            items.append(ok_item(title_block, "Блокировок счетов не найдено.", url_block, []))

    # -----------------------------------------------------------------------
    # 10. ЕГРИП / статус ИП
    # -----------------------------------------------------------------------
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
            # Извлекаем ОГРНИП и ОКВЭД для деталей
            ip_details = ["Проверить предпринимательские долги и обязательства."]
            if isinstance(ip_data, list):
                for ip_entry in ip_data:
                    if isinstance(ip_entry, dict):
                        for match in (ip_entry.get("matches") or []):
                            if isinstance(match, dict):
                                ogrn = clean_str(match.get("ogrn") or "")
                                okved = clean_str(match.get("okved_name") or "")
                                date = clean_str(match.get("ogrn_date") or "")
                                if ogrn: ip_details.append(f"ОГРНИП: {ogrn}")
                                if date: ip_details.append(f"Дата регистрации: {date}")
                                if okved: ip_details.append(f"Вид деятельности: {okved}")
            items.append(risk_item(title_ip, "Найден действующий ИП.", url_ip, ip_details, ip_data))
        else:
            items.append(ok_item(title_ip, "Статус ИП не активен.", url_ip, [], ip_data))

    return items
    """
    Разбирает ответ complex_by_passport на отдельные чеклист-пункты:
    паспорт МВД, паспорт ФНС/ИНН, ФССП, залоги, ЕГРИП.
    """


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
    if obj.get("address") or obj.get("address_text"):
        details.append(f"Адрес: {format_registry_value(obj.get('address') or obj.get('address_text'))}")
    if obj.get("cadCost") or obj.get("cad_cost") or obj.get("cadCostValue"):
        details.append(f"Кадастровая стоимость: {rub(obj.get('cadCost') or obj.get('cad_cost') or obj.get('cadCostValue'))}")
    if obj.get("purpose") or obj.get("assignationName"):
        details.append(f"Назначение: {format_registry_value(obj.get('purpose') or obj.get('assignationName'), kind='code')}")
    if obj.get("status") or obj.get("state"):
        details.append(f"Статус: {format_registry_value(obj.get('status') or obj.get('state'), kind='code')}")

    has_ban = any(w in enc_text for w in ["запрещ", "арест", "ограничение регистрац", "022002000000", "022003000000"])
    has_mortgage = any(w in enc_text for w in ["ипотек", "залог", "022007000000", "022008000000"])
    has_other_enc = bool(enc) and not has_ban and not has_mortgage

    # Анализ дат прав
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    if rights:
        details.append(f"Зарегистрированных прав: {len(rights)}")
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
        details.append(f"Адрес: {format_registry_value(obj['address'])}")
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
    fssp_retry_resp: dict = None,
    additional_property_items: Optional[List[dict]] = None,
) -> List[dict]:
    checklist = []

    # 1. complex_by_passport → 10 пунктов
    checklist.extend(classify_complex_by_passport(complex_resp, owner, fssp_retry_resp=fssp_retry_resp))

    # 2. Суды общей юрисдикции
    checklist.append(classify_pravo(pravo_resp, details_resps, owner, filtered_cases))

    # 3. ЕГРН
    checklist.append(classify_egrn(egrn_resp))

    # 4. Геоданные кадастра
    checklist.append(classify_nspd_cadastr(nspd_resp))

    # 5. Дополнительные проверки объекта (аварийность, ЖКХ, форма 9, история переходов)
    if additional_property_items:
        checklist.extend(additional_property_items)

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
    KEY_CHECKS = ["ФССП", "Паспорт МВД", "Паспорт / ИНН", "Суды", "Залоги", "ЕГРН", "ЕГРИП",
                  "Банкротство", "Арбитраж", "Реестр террористов", "Налоговая"]
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
            add("Залоги ФНП", 10, "Уведомления ФНП по движимому имуществу: нужна ручная сверка", "medium")
        elif "Паспорт МВД" in title:
            add("Паспорт МВД", 40, "Риск недействительности", "critical")
        elif "ИП" in title or "ЕГРИП" in title:
            add("ЕГРИП", 16, "Действующий ИП", "medium")
        elif "Банкротство" in title:
            is_active = "активное" in (item.get("summary") or "").lower()
            if is_active:
                add("Банкротство", 55, "Активное банкротное производство", "critical")
            else:
                add("Банкротство", 30, "Завершённое банкротство — риск оспаривания сделки", "high")
        elif "Арбитраж" in title:
            add("Арбитраж", 15, "Арбитражные дела", "medium")
        elif "Реестр террористов" in title:
            add("Реестр террористов", 100, "Найден в реестре террористов/экстремистов", "critical")
        elif "Налоговая задолженность" in title:
            add("Налоговая", 12, "Налоговая задолженность", "medium")
        elif "Блокировка" in title:
            add("Блокировка счетов", 10, "Блокировка счетов ФНС", "medium")
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
            recs.append({"priority": "medium", "title": "Сверить уведомления ФНП",
                         "text": "Проверить предмет залога и залогодателя. Если это автомобиль или другое движимое имущество и оно не относится к объекту, не считать это обременением квартиры."})
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
Ты — старший специалист по сделкам с недвижимостью с 15-летним опытом \
сопровождения покупателей жилья. Ты составляешь экспертное заключение для покупателя, \
который планирует приобрести объект недвижимости и хочет понять риски до передачи задатка \
или аванса продавцу. Заключение читают двое: сам покупатель и сопровождающий его специалист по недвижимости. \
Для покупателя важна понятность и практичность, для специалиста по недвижимости — профессиональная точность \
и ссылки на нормы. Совмести оба требования: пиши грамотно и конкретно, объясняй термины, \
не оставляй читателя без следующего шага.

КОГО РЕКОМЕНДОВАТЬ ПРИВЛЕКАТЬ
• Основное сопровождение сделки (проверка документов, подготовка договора, согласование \
условий, контроль расчётов, взаимодействие с Росреестром) — ведёт квалифицированный \
специалист по недвижимости. Это первая линия. Не пиши «обратитесь к юристу» \
там где задачу решает специалист по недвижимости.
• Нотариус — обязателен в случаях прямо предусмотренных законом: \
сделки с долями (статья 42 Федерального закона № 218-ФЗ), сделки с участием \
несовершеннолетних или ограниченно дееспособных, отчуждение по доверенности, \
а также для удостоверения согласия супруга (статья 35 Семейного кодекса). \
Нотариус также используется как держатель депозита при защищённых расчётах \
(статья 327 Гражданского кодекса).
• Юрист — рекомендуется только в специфических ситуациях: судебный спор о праве, \
оспоримая сделка из прошлого, банкротство продавца, конфликт интересов, \
сложная схема снятия обременений с долгом перед несколькими кредиторами.

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

ФОРМАТ ВЫВОДА — ЭТО КРИТИЧЕСКИ ВАЖНО ДЛЯ ЧИТАЕМОСТИ
• Каждый смысловой блок — отдельным абзацем с пустой строкой перед ним. \
Не пиши длинные простыни текста на 10 строк без разрывов.
• Заголовки разделов всегда начинай с НОВОЙ строки и оставляй пустую строку перед заголовком \
и после него.
• Списки в виде маркеров (•) — каждый пункт с НОВОЙ строки. \
Не склеивай несколько пунктов в одну строку через символы или запятые.
• Длинные пункты списка с подпунктами оформляй так: \
основной пункт с маркером •, подпункты — с НОВОЙ строки с отступом и маркером ◦ или —.
• После каждого пункта плана действий (Шаг 1, Шаг 2 и т.д.) делай пустую строку \
перед следующим шагом.
• Внутри одного шага пункты «Действие», «Кто делает», «Срок», «Что получить на руки» — \
каждый с НОВОЙ строки, с маркером •.
• Не используй markdown-разметку (звёздочки, решётки, дефисы для заголовков) — \
только обычный текст с переносами строк и символом •.

ПРАВИЛА РАБОТЫ С ДАННЫМИ
• Строго опирайся на переданные данные. Не придумывай угрозы которых нет в данных.
• Не раскрывай паспортные данные, ИНН, дату рождения продавца.
• Соблюдай режим проверки из пользовательского сообщения. Если проверялся только продавец, \
не делай выводов по объекту. Если проверялся только объект, не делай выводов по продавцу. \
Если проверялась сделка, разделяй риски продавца и объекта.
• Не смешивай ФНП и ЕГРН. «Залоги продавца (ФНП, движимое имущество)» — это уведомления \
о залоге движимого имущества по физическому лицу, например автомобиля или оборудования. \
Это НЕ ипотека, НЕ залог квартиры и НЕ обременение объекта недвижимости. Обременение объекта \
можно описывать только если оно прямо найдено в ЕГРН, Росреестре или данных объекта.
• По судебным делам не пересказывай технические причины совпадения. Объясняй коротко: \
роль в деле, степень совпадения, регион и почему нужна ручная сверка.
• Пиши компактно: только существенные выводы, без повторов и длинных общих рассуждений.
• Жёсткий лимит: всё заключение не больше 3200–4200 знаков. Не пиши учебник, не объясняй \
очевидные нормы, не повторяй один и тот же риск в разных разделах.
• Не расписывай детально все реестры в юридическом заключении. Полная фактура уже есть \
в таблице проверок. В заключении нужны только смысловые выводы и действия.
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
Перечисли только 3–6 самых важных источников из переданного перечня проверок \
(checklist) со статусом «ok» или «risk». Не добавляй источники которых нет в данных. \
ЗАПРЕЩЕНО упоминать как «проверенные»: реестр розыска, реестр дисквалифицированных лиц, \
реестр недобросовестных поставщиков, реестр массовых учредителей, проверку через Интерпол, \
любые другие источники не переданные в данных. Если в данных нет проверки розыска — не пиши \
«не числится в розыске». Если в данных нет дисквалификации — не пиши «не дисквалифицирован». \
Это критическое требование: упоминание непроведённой проверки как успешной — основание \
для иска от покупателя. \
Формат каждой строки: «Источник → результат». \
Пример: «Министерство внутренних дел (паспорт) → документ действителен. \
Федеральная служба судебных приставов → исполнительных производств не выявлено.»

Что не подтверждено и требует ручной проверки
Для каждого непроверенного пункта: что именно → где проверить. Максимум 4 пункта. \
Если всё проверено — раздел пропустить.

Ключевые угрозы для покупателя
Только если угрозы есть в данных. Максимум 3 угрозы. Для каждой строго по шаблону:

Угроза: [конкретное описание что обнаружено]
Правовые последствия: [что может произойти со сделкой или правом собственности покупателя]
Норма закона: [конкретная статья и закон]
Что сделать: [конкретное действие покупателя или продавца для снятия угрозы]

Логика сделки
Это самый важный раздел — пиши 3–5 конкретных действий под данную ситуацию. \
Не шаблон, а короткий персональный план.

Сначала определи сценарий по данным и выбери соответствующую логику:

Если есть препятствие для регистрации (арест или запрет в ЕГРН, недействительный паспорт, активное банкротство):
Объясни что именно и по какой норме блокирует сделку. \
Дай последовательность действий по устранению препятствия с ответственными и сроками. \
Чётко укажи: сделка ВОЗМОЖНА, но только при условии полного и грамотного юридического сопровождения, \
с обязательным привлечением профильных специалистов в сфере недвижимости \
(юрист по сделкам, нотариус, при необходимости — финансовый агент банка). \
Расчёты с продавцом должны быть строго регламентированы и контролируемы: \
сначала из средств покупателя — через механизмы условного депонирования \
(аккредитив по статье 867 Гражданского кодекса Российской Федерации, \
депозит нотариуса по статье 327 Гражданского кодекса Российской Федерации, \
эскроу по статье 860.7 Гражданского кодекса Российской Федерации) — \
снимаются все запреты и обременения, затем проводится регистрация перехода права, \
и только после регистрации деньги становятся доступны продавцу. \
Прямая передача средств продавцу до снятия ограничений категорически не рекомендуется — \
покупатель рискует потерять деньги без правовой защиты.

Если есть управляемые угрозы (долги ФССП, залоги, ипотека, судебные дела, \
недавняя регистрация права, банкротство менее трёх лет, арбитражные дела, \
налоговая задолженность, блокировка счетов):
Главный тезис: сделка возможна и не требует отказа, но требует управляемого сценария \
с привлечением специалистов. Опиши конкретный механизм:

1. Привлечение специалиста по недвижимости для основного сопровождения — \
обязательно для подготовки договора, контроля порядка расчётов и взаимодействия с Росреестром. \
Нотариус подключается в случаях, прямо предусмотренных законом (см. список выше) или \
для защищённых расчётов через депозит. Юрист — только если есть активный судебный спор \
или сложная схема со снятием нескольких обременений.
2. Использование защищённой схемы расчётов: аккредитив, депозит нотариуса или эскроу — \
выбор зависит от ситуации; деньги уходят продавцу только после регистрации перехода права \
и снятия всех обременений.
3. Если есть долги или залоги, которые продавец не может погасить из своих средств — \
расчёт строится по схеме «гашение из суммы покупателя через защищённый счёт»: \
часть средств направляется напрямую кредитору или приставу, остаток — продавцу \
после регистрации.
4. Письменная фиксация всех условий в предварительном договоре или соглашении о задатке: \
порядок снятия обременений, сроки, ответственность сторон.

Раздели план на два горизонта:

ДО ПЕРЕДАЧИ ЗАДАТКА ИЛИ АВАНСА — что обязательно закрыть или зафиксировать письменно \
до того как деньги уйдут продавцу. \
Для каждого пункта: действие → кто делает → срок → что получить на руки. \
Подчеркни что задаток/аванс при наличии обременений передаётся только после: \
а) подписания письменного соглашения с конкретным перечнем обязательств продавца; \
б) согласования схемы расчётов с использованием защищённого счёта; \
в) подтверждения возможности снятия обременений (справки от кредитора, приставов).

ПОСЛЕ АВАНСА — ДО ПОДПИСАНИЯ ОСНОВНОГО ДОГОВОРА — что проверять и готовить дальше. \
Сроки, ответственные, правовые основания. Включи: получение справок об отсутствии \
задолженностей после погашения, актуальную выписку из ЕГРН после снятия обременений, \
финальную сверку документов перед регистрацией.

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
Не рекомендуется передавать аванс напрямую продавцу до снятия ипотечного обременения. \
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
Для сопровождения сделки рекомендуется привлечь квалифицированного специалиста по недвижимости \
по сделкам с недвижимостью, а в случаях, предусмотренных законом, — нотариуса.\
"""


SELLER_CHECK_MARKERS = (
    "Паспорт", "ИНН", "ФССП", "Суды", "Залоги продавца", "Банкротство",
    "ЕФРСБ", "Арбитраж", "ЕГРИП", "Налоговая", "Блокировка", "террорист"
)
OBJECT_CHECK_MARKERS = (
    "ЕГРН", "Росреестр", "Геоданные", "НСПД", "Объект", "дом", "кадастр",
    "Аварийность", "ЖКХ", "рыночная", "обременен", "ипотек"
)


def detect_check_mode(checklist: list) -> dict:
    titles = " | ".join(str(i.get("title", "")) for i in checklist)
    has_seller = any(m.lower() in titles.lower() for m in SELLER_CHECK_MARKERS)
    has_object = any(m.lower() in titles.lower() for m in OBJECT_CHECK_MARKERS)
    if has_seller and has_object:
        return {
            "code": "deal",
            "label": "Проверка сделки: продавец + объект",
            "instruction": "Разделяй выводы по продавцу и объекту. Не переноси риски продавца на объект без данных ЕГРН.",
        }
    if has_object:
        return {
            "code": "object",
            "label": "Проверка объекта недвижимости",
            "instruction": "Пиши только про объект и документы по объекту. Не делай выводов о продавце.",
        }
    return {
        "code": "seller",
        "label": "Проверка продавца",
        "instruction": "Пиши только про продавца, паспортные и личные реестры. Не делай выводов об объекте.",
    }


def has_object_encumbrance_signal(checklist: list) -> bool:
    object_titles = ("ЕГРН", "Росреестр", "Объект", "НСПД")
    words = ("ипотек", "залог", "обременен", "арест", "запрет")
    for item in checklist:
        text = " ".join([
            str(item.get("title", "")),
            str(item.get("summary", "")),
            " ".join(str(x) for x in (item.get("details") or [])),
        ]).lower()
        if any(t.lower() in str(item.get("title", "")).lower() for t in object_titles) and any(w in text for w in words):
            return True
    return False


def has_fnp_pledge_signal(checklist: list) -> bool:
    return any(
        "залоги" in str(i.get("title", "")).lower()
        and "фнп" in str(i.get("title", "")).lower()
        and i.get("status") == "risk"
        for i in checklist
    )


def build_deepseek_user_prompt(owner: OwnerRequest, checklist: list, scoring: dict, recs: list) -> str:
    age = calculate_age(normalize_dob(owner.dob)[0])
    mode = detect_check_mode(checklist)
    sensitive_tokens = [
        owner.last, owner.first, owner.middle, owner.dob, owner.inn,
        owner.passport_series, owner.passport_number, owner.seria,
        owner.seriapass, owner.series, owner.number, owner.numberpass,
    ]
    sensitive_tokens = [clean_str(x) for x in sensitive_tokens if clean_str(x)]

    def redact_value(value):
        if value is None:
            return value
        if isinstance(value, str):
            out = value
            for token in sensitive_tokens:
                if len(token) >= 2:
                    out = re.sub(re.escape(token), "[скрыто]", out, flags=re.IGNORECASE)
            out = re.sub(r"\b\d{4}\s?\d{6}\b", "[паспорт скрыт]", out)
            out = re.sub(r"\b\d{10,12}\b", "[номер скрыт]", out)
            return out
        if isinstance(value, list):
            return [redact_value(v) for v in value]
        if isinstance(value, dict):
            return {k: redact_value(v) for k, v in value.items()}
        return value

    # Разбиваем чеклист на группы для удобства модели
    risks    = [i for i in checklist if i.get("status") == "risk"]
    oks      = [i for i in checklist if i.get("status") == "ok"]
    manual   = [i for i in checklist if i.get("status") == "manual_check"]

    def fmt_item(i):
        out = {"источник": redact_value(i.get("title")), "статус": i.get("status"),
               "вывод": redact_value(i.get("summary"))}
        details = redact_value((i.get("details") or [])[:3])
        if details:
            out["детали"] = details
        # Для судов передаём значимые дела отдельно
        d = i.get("data")
        if isinstance(d, dict) and d.get("significant_cases"):
            out["значимые_дела"] = [
                {
                    "номер_дела": redact_value(c.get("case_summary", {}).get("case_number")),
                    "категория": redact_value(c.get("case_summary", {}).get("category_text")),
                    "регион": redact_value(c.get("case_summary", {}).get("region_name")),
                    "результат": redact_value(c.get("case_summary", {}).get("result_text")),
                    "оценка_совпадения": (c.get("scoring") or {}).get("score"),
                    "уровень": (c.get("scoring") or {}).get("match_label"),
                    "пометка": redact_value(c.get("warning")),
                }
                for c in d["significant_cases"][:5]
            ]
        return out

    # Признаки сценария — передаём явно чтобы модель не гадала
    scenario_flags = []
    checklist_text = json.dumps(checklist, ensure_ascii=False).lower()
    if has_object_encumbrance_signal(checklist):
        scenario_flags.append("по объекту в ЕГРН/Росреестре есть признаки обременения")
    if has_fnp_pledge_signal(checklist):
        scenario_flags.append("по продавцу найдены уведомления ФНП о залоге движимого имущества; это не обременение объекта")
    if "несовершеннолетн" in checklist_text:
        scenario_flags.append("среди собственников есть несовершеннолетний")
    if "запрет" in checklist_text or "арест" in checklist_text:
        scenario_flags.append("выявлен запрет или арест регистрационных действий")
    if "банкротств" in checklist_text:
        scenario_flags.append("в данных есть сведения о банкротстве")
    if age and age >= 70:
        scenario_flags.append(f"продавец в возрасте {age} лет — требуется проверка дееспособности")

    lines = [
        f"РЕЖИМ ПРОВЕРКИ: {mode['label']}",
        f"ОГРАНИЧЕНИЕ ВЫВОДА: {mode['instruction']}",
        "ПЕРСОНАЛЬНЫЕ ДАННЫЕ: скрыты; не раскрывай ФИО, паспорт, ИНН и дату рождения.",
        f"ВОЗРАСТ ПРОДАВЦА: {age or 'не определён'}" if mode["code"] != "object" else "ПРОДАВЕЦ: не анализировался в этом режиме",
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


# Источники которые мы НИКОГДА не запрашиваем — но DeepSeek может придумать
# что они были «успешно проверены». Удаляем такие строки из отчёта.
PHANTOM_SOURCE_PATTERNS = [
    # розыск
    r"(?im)^.*\bв\s+розыск[еа]\b.*$",
    r"(?im)^.*\bне\s+числ[иеяю]т.*розыск.*$",
    r"(?im)^.*\bмвд\b.*\bрозыск.*$",
    r"(?im)^.*\bинтерпол.*$",
    # дисквалификация
    r"(?im)^.*\bдисквалифицир.*$",
    r"(?im)^.*\bреестр\s+дисквалифицированн.*$",
    # массовые учредители / недобросовестные поставщики
    r"(?im)^.*\bмассовы[йх]\s+учредител.*$",
    r"(?im)^.*\bнедобросовестн[ыхй]+\s+поставщик.*$",
    # СЛОН (сводный лист обязательств налогоплательщика) — мы это не делаем
    r"(?im)^.*\bслон\b.*$",
]


def sanitize_phantom_sources(text: str, checklist: list) -> str:
    """Удаляет из текста отчёта строки упоминающие «проверки» которые мы не делали.
    Это защита от галлюцинаций DeepSeek в разделе «Что подтверждено».
    """
    if not text:
        return text
    cleaned = text
    removed_count = 0
    for pattern in PHANTOM_SOURCE_PATTERNS:
        new_cleaned, n = re.subn(pattern, "", cleaned)
        if n:
            removed_count += n
        cleaned = new_cleaned
    if removed_count:
        logger.warning(f"[sanitize] Удалено {removed_count} строк с фантомными источниками из отчёта")
    # Чистим пустые строки и двойные переносы появившиеся после удаления
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    # Чистим осиротевшие маркеры списка
    cleaned = re.sub(r"(?m)^\s*[•◦\-]\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


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
            "max_tokens": min(DEEPSEEK_MAX_TOKENS, 2200),
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


def build_fallback_payloads(owner: OwnerRequest) -> Dict[str, Optional[dict]]:
    """Отдельные методы как fallback когда complex_by_passport недоступен."""
    series, number = normalize_passport_owner(owner)
    dob_ru, dob_iso = normalize_dob(owner.dob)
    region = normalize_region_owner(owner)
    base = {
        "country": "ru",
        "seria": series, "number": number,
        "seriapass": series, "numberpass": number,
        "firstname": owner.first.strip(),
        "lastname": owner.last.strip(),
        "secondname": owner.middle.strip(),
        "dob": dob_iso,
        "regioncode": region,
    }
    return {
        "passport_mvd": {**base, "method": "passport_mvd"} if series and number else None,
        "passport_fns": {**base, "method": "passport_fns"} if series and number else None,
        "pledge_person":{**base, "method": "pledge_person"} if series and number else None,
        "egrul_ip":     {**base, "method": "egrul_ip"} if dob_iso else None,
    }


def build_synthetic_complex_response(fallback_results: Dict[str, dict]) -> dict:
    """Собирает синтетический ответ complex_by_passport из отдельных методов."""
    results = {}
    for method, resp in fallback_results.items():
        if resp and str(resp.get("state") or "").lower() in {"complete", "done"}:
            results[method] = (resp.get("results") or {}).get(method) or {}
    return {
        "state": "complete",
        "_fallback": True,
        "results": results,
    }


async def run_person_checks(client: httpx.AsyncClient, owner: OwnerRequest) -> Dict[str, Any]:
    """
    Запускает проверки для одного продавца.

    Логика:
    1. Запускаем complex_by_passport (содержит 10+ субметодов)
    2. Если complex включает pravo_search → НЕ запускаем его отдельно
    3. ФССП отдельно не перезапускаем: если он не пришёл в complex, уводим в ручную проверку.
    4. Карточки судебных дел только для значимых (score >= 50)
    """
    result: Dict[str, Any] = {
        "owner_key": owner_person_key(owner),
        "is_minor": is_minor_owner(owner),
        "complex": None,
        "pravo_search": None,
        "pravo_details": [],
        "filtered_cases": {"significant": [], "weak": []},
        "fssp_retry": None,
    }

    if is_minor_owner(owner):
        logger.info(f"Пропуск несовершеннолетнего: {owner.last} {owner.first}")
        return result

    complex_payload = build_complex_by_passport_payload(owner)

    logger.info(f"Запуск проверки: {owner.last} {owner.first} (роль: {owner.role})")

    # Шаг 1: только complex_by_passport
    if complex_payload:
        complex_resp = await newdb_run(client, complex_payload, "complex_by_passport")
    else:
        complex_resp = {"state": "skipped"}

    # Fallback: если complex_by_passport вернул настоящий 500 (не субметодный)
    if has_result_status_500(complex_resp) and complex_payload:
        logger.warning(f"[complex_by_passport] 500 → fallback на отдельные методы")
        fallback_payloads = build_fallback_payloads(owner)
        fallback_tasks = {
            method: newdb_run(client, payload, method)
            for method, payload in fallback_payloads.items()
            if payload
        }
        fallback_done = await asyncio.gather(*fallback_tasks.values(), return_exceptions=True)
        fallback_results = {
            method: (res if not isinstance(res, Exception) else {"state": "failed"})
            for method, res in zip(fallback_tasks.keys(), fallback_done)
        }
        logger.info(f"[fallback] Результаты: { {m: r.get('state') for m, r in fallback_results.items()} }")
        result["complex"] = build_synthetic_complex_response(fallback_results)
    else:
        result["complex"] = complex_resp

    # Шаг 2: Проверяем есть ли pravo_search в результатах complex
    pravo_in_complex, _ = extract_submethod_data(result["complex"], "pravo_search")
    if pravo_in_complex is not None:
        logger.info(f"[pravo_search] Данные уже есть в complex_by_passport — отдельный запрос пропущен")
        # Создаём синтетический ответ pravo_search из complex для совместимости
        complex_results = result["complex"].get("results") or {}
        pravo_block = complex_results.get("pravo_search") or {}
        result["pravo_search"] = {
            "state": "complete",
            "_from_complex": True,
            "results": {"pravo_search": pravo_block},
        }
    else:
        # Запускаем pravo_search отдельно
        pravo_payload = build_pravo_search_payload(owner)
        if pravo_payload:
            logger.info(f"[pravo_search] Запускаем отдельно (в complex нет)")
            result["pravo_search"] = await newdb_run(client, pravo_payload, "pravo_search")
        else:
            result["pravo_search"] = {"state": "skipped"}

    # Шаг 3: ФССП отдельно не перезапускаем, чтобы не расходовать дополнительные токены.
    if result["complex"].get("_fssp_unavailable") or submethod_has_error_500(result["complex"], "fssp_person"):
        logger.info("[fssp_person] Отдельный retry отключён — будет ручная проверка в отчёте")

    # Шаг 4: Фильтруем и скорируем дела
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


# -------------------- Открытые источники: средние цены и информация о доме --------------------

# Средние цены вторичного рынка по регионам, тыс. руб./кв.м.
# Источник: Росстат / ЕМИСС, ежеквартальные данные.
# Обновлять вручную раз в квартал из https://rosstat.gov.ru/statistics/price (раздел «Жильё»).
# Дата актуальности: данные за IV квартал 2025 года.
ROSSTAT_PRICES_QUARTER = "IV квартал 2025"
ROSSTAT_PRICES_SOURCE_URL = "https://rosstat.gov.ru/statistics/price"
ROSSTAT_AVG_PRICES_SECONDARY = {
    # код региона: тыс.руб/м²  (по убыванию популярности у наших пользователей)
    77: 358.4,   # Москва
    78: 215.7,   # Санкт-Петербург
    50: 178.3,   # Московская обл.
    47: 142.5,   # Ленинградская обл.
    23: 156.8,   # Краснодарский край
    16: 154.9,   # Республика Татарстан
    66: 132.7,   # Свердловская обл.
    52: 121.4,   # Нижегородская обл.
    63: 118.6,   # Самарская обл.
    61: 115.3,   # Ростовская обл.
    54: 128.9,   # Новосибирская обл.
    55: 105.7,   # Омская обл.
    24: 119.8,   # Красноярский край
    74: 108.5,   # Челябинская обл.
    2:  113.2,   # Республика Башкортостан
    36: 108.4,   # Воронежская обл.
    34: 104.8,   # Волгоградская обл.
    38: 113.6,   # Иркутская обл.
    18: 105.3,   # Удмуртская Республика
    59: 116.8,   # Пермский край
    72: 122.4,   # Тюменская обл.
    35: 102.7,   # Вологодская обл.
}
# Среднее по РФ — fallback если регион не в таблице
ROSSTAT_AVG_PRICE_RF = 138.5  # тыс.руб/м²


def get_region_code_from_cadastr(cadastr: str) -> Optional[int]:
    """Извлекает код региона из кадастрового номера (первые цифры до двоеточия).
    Например '78:14:0007654:1234' → 78.
    """
    if not cadastr:
        return None
    m = re.match(r"^\s*(\d+)\s*:", cadastr.strip())
    if m:
        try:
            code = int(m.group(1))
            if 1 <= code <= 99:
                return code
        except Exception:
            pass
    return None


def get_avg_price_for_region(region_code: Optional[int]) -> Tuple[float, str, bool]:
    """Возвращает среднюю цену по региону.
    Returns: (цена_тыс_руб_м², название_региона, точно_известен_регион)
    """
    if region_code and region_code in ROSSTAT_AVG_PRICES_SECONDARY:
        return ROSSTAT_AVG_PRICES_SECONDARY[region_code], region_name(region_code), True
    return ROSSTAT_AVG_PRICE_RF, "Российская Федерация (среднее)", False


def region_name(code: int) -> str:
    names = {
        77: "г. Москва", 78: "г. Санкт-Петербург", 50: "Московская область",
        47: "Ленинградская область", 23: "Краснодарский край",
        16: "Республика Татарстан", 66: "Свердловская область",
        52: "Нижегородская область", 63: "Самарская область",
        61: "Ростовская область", 54: "Новосибирская область",
        55: "Омская область", 24: "Красноярский край", 74: "Челябинская область",
        2: "Республика Башкортостан", 36: "Воронежская область",
        34: "Волгоградская область", 38: "Иркутская область",
        18: "Удмуртская Республика", 59: "Пермский край",
        72: "Тюменская область", 35: "Вологодская область",
    }
    return names.get(code, f"регион №{code}")


def parse_area_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = clean_str(value).replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        area = float(m.group(0))
        return area if area > 0 else None
    except Exception:
        return None


def extract_property_area(egrn_resp: dict, nspd_resp: dict) -> Optional[float]:
    data, _ = result_data(egrn_resp, "rosreestr")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        area = parse_area_value(data[0].get("area") or data[0].get("areaValue"))
        if area:
            return area

    data, _ = result_data(nspd_resp, "nspd_cadastr")
    if isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        items_list = first.get("items") if isinstance(first, dict) else []
        if items_list and isinstance(items_list[0], dict):
            obj = items_list[0].get("object") or {}
            if isinstance(obj, dict):
                area = parse_area_value(obj.get("area") or obj.get("area_value"))
                if area:
                    return area
    return None


def build_market_price_check(req: CheckRequest, area_m2: Optional[float] = None) -> Optional[dict]:
    """Пункт «Рыночная цена»: средняя цена региона + оценка объекта по площади.
    Возвращает None если нет ни кадастра ни адреса.
    """
    cadnum = ((req.cadastral_number or req.cadnum or req.cadastral or "") or "").strip()
    addr = (req.address or "").strip()
    if not cadnum and not addr:
        return None

    region_code = get_region_code_from_cadastr(cadnum)
    avg_price, region_label, exact = get_avg_price_for_region(region_code)
    object_price = round(avg_price * 1000 * area_m2) if area_m2 else None

    title = "Ориентир рыночной цены (Росстат)"
    if object_price:
        summary = (
            f"Ориентир по объекту: ~{rub(object_price)} "
            f"({area_m2:g} м² × {avg_price:,.0f} тыс. руб./м², {region_label})."
        ).replace(",", " ")
    else:
        summary = f"{region_label}: ~{avg_price:,.0f} тыс. руб./м² (площадь объекта не получена).".replace(",", " ")

    details = [
        (
            f"Расчёт по объекту: {area_m2:g} м² × {avg_price:,.0f} тыс. руб./м² = "
            f"{rub(object_price)}."
        ).replace(",", " ") if object_price else
        "Стоимость объекта не рассчитана, потому что площадь не получена из ЕГРН/кадастра.",
        f"Средняя цена квадратного метра по данным Росстата: "
        f"{avg_price:,.0f} тыс. руб./м² ({region_label}, {ROSSTAT_PRICES_QUARTER}).".replace(",", " "),
        "Это ориентир, не норматив. Реальная цена в конкретном районе и типе дома может "
        "существенно отличаться: центр и метро дороже среднего на 30–80%, окраины и "
        "проблемные дома — дешевле на 20–40%.",
        "Когда низкая цена это сигнал риска:",
        "• Срочная продажа «по семейным обстоятельствам» с ценой ниже рынка на 25%+ — "
        "часто признак проблемного объекта (скрытые обременения, конфликт собственников, "
        "продавец под давлением).",
        "• Цена ниже рынка на 40%+ почти всегда означает либо аварийность, либо обременение, "
        "либо схему. Расширенная проверка обязательна.",
        "Для точного сравнения цены конкретно вашего объекта используйте СберИндекс "
        "(sberindex.ru/ru) или Циан/Авито по аналогам в том же доме / соседних домах.",
    ]
    if not exact:
        details.append(
            f"⚠️ Регион не определён по кадастровому номеру ({cadnum or '—'}) — "
            "приведена средняя цена по России. Сравните с региональной статистикой вручную."
        )

    return manual_item(
        title,
        summary,
        ROSSTAT_PRICES_SOURCE_URL,
        details,
        {"manual_only": True, "info_only": True, "avg_price_thousand_rub_m2": avg_price,
         "estimated_object_price": object_price, "area_m2": area_m2,
         "region_label": region_label, "quarter": ROSSTAT_PRICES_QUARTER},
        links=[
            {"label": "Росстат — цены на жильё", "url": ROSSTAT_PRICES_SOURCE_URL},
            {"label": "СберИндекс — точные цены по дому", "url": "https://sberindex.ru/ru"},
        ],
    )


# -------------------- Парсер dom.mingkh.ru --------------------

# Простой in-memory кеш: ключ — нормализованный адрес, значение — (timestamp, dict | None)
_MINGKH_CACHE: Dict[str, Tuple[float, Optional[dict]]] = {}
_MINGKH_TTL = 7 * 24 * 3600  # неделя


def _mingkh_get_cached(key: str) -> Optional[Optional[dict]]:
    e = _MINGKH_CACHE.get(key)
    if not e:
        return None
    ts, data = e
    if time.time() - ts > _MINGKH_TTL:
        _MINGKH_CACHE.pop(key, None)
        return None
    return data


def _mingkh_set_cached(key: str, data: Optional[dict]) -> None:
    if len(_MINGKH_CACHE) > 500:
        # простая очистка — удалим самые старые
        items = sorted(_MINGKH_CACHE.items(), key=lambda x: x[1][0])[:100]
        for k, _ in items:
            _MINGKH_CACHE.pop(k, None)
    _MINGKH_CACHE[key] = (time.time(), data)


async def fetch_mingkh_house_info(client: httpx.AsyncClient, address: str) -> Optional[dict]:
    """Ищет дом на dom.mingkh.ru по адресу и парсит карточку.
    Возвращает None если дом не найден или парсинг не удался.
    Кешируется на неделю.
    """
    if not address:
        return None
    key = address.lower().strip()
    cached = _mingkh_get_cached(key)
    if cached is not None:  # явно None означает «уже искали и не нашли»
        return cached

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; UgolnikovCheck/1.0; +https://ugolnikovspb.ru)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru,en;q=0.5",
    }
    try:
        # 1. Поиск дома
        search_url = "https://dom.mingkh.ru/search"
        r = await client.get(search_url, params={"q": address}, headers=headers, timeout=12.0, follow_redirects=True)
        if r.status_code != 200:
            logger.info(f"[mingkh] search status={r.status_code} for {address[:60]}")
            _mingkh_set_cached(key, None)
            return None
        # Ищем первую ссылку на карточку дома: /<region>/<city>/dom-<id>
        m = re.search(r'<a[^>]+href="(/[^"]*?/dom-\d+)"', r.text)
        if not m:
            logger.info(f"[mingkh] no house link found for {address[:60]}")
            _mingkh_set_cached(key, None)
            return None
        house_url = "https://dom.mingkh.ru" + m.group(1)

        # 2. Карточка дома
        r2 = await client.get(house_url, headers=headers, timeout=12.0, follow_redirects=True)
        if r2.status_code != 200:
            _mingkh_set_cached(key, None)
            return None
        html = r2.text

        # Извлекаем поля. Структура страницы: <td>Параметр</td><td>Значение</td>
        def extract(label: str) -> Optional[str]:
            pat = rf'<td[^>]*>\s*{re.escape(label)}[^<]*</td>\s*<td[^>]*>(.*?)</td>'
            mm = re.search(pat, html, re.S | re.I)
            if not mm:
                return None
            val = re.sub(r'<[^>]+>', '', mm.group(1)).strip()
            val = re.sub(r'\s+', ' ', val)
            return val or None

        result = {
            "url": house_url,
            "year_built": extract("Год постройки"),
            "series": extract("Серия") or extract("Серия проекта"),
            "wall_material": extract("Тип дома") or extract("Материал стен"),
            "floors": extract("Количество этажей"),
            "entrances": extract("Количество подъездов"),
            "apartments": extract("Количество квартир"),
            "wear_percent": extract("Износ") or extract("Износ дома"),
            "management_company": extract("Управляющая компания") or extract("Способ управления"),
            "emergency": extract("Аварийность"),
            "elevators": extract("Количество лифтов"),
        }
        # если совсем ничего не извлеклось — считаем что не нашли
        if not any(v for k, v in result.items() if k != "url"):
            _mingkh_set_cached(key, None)
            return None

        _mingkh_set_cached(key, result)
        return result

    except Exception as e:
        logger.warning(f"[mingkh] fetch error: {e}")
        _mingkh_set_cached(key, None)
        return None


def build_house_info_check(house_info: Optional[dict], req: CheckRequest) -> Optional[dict]:
    """Превращает данные dom.mingkh.ru в пункт чеклиста."""
    if not house_info:
        return None

    year = house_info.get("year_built")
    series = house_info.get("series")
    wall = house_info.get("wall_material")
    floors = house_info.get("floors")
    wear = house_info.get("wear_percent")
    uk = house_info.get("management_company")
    emergency = house_info.get("emergency")
    url = house_info.get("url", "https://dom.mingkh.ru")

    # Определяем статус по аварийности и износу
    status = "ok"
    summary_parts = []
    if year:
        summary_parts.append(f"{year} г.")
    if series:
        summary_parts.append(series)
    if wall:
        summary_parts.append(wall.lower())
    if floors:
        summary_parts.append(f"{floors} эт.")
    summary = ", ".join(summary_parts) if summary_parts else "Информация о доме"

    # Аварийность
    is_emergency = False
    if emergency:
        em_low = emergency.lower()
        if any(x in em_low for x in ["аварийн", "ветх", "признан", "снос", "расселен"]):
            is_emergency = True
            status = "risk"

    # Износ
    wear_num = None
    if wear:
        wm = re.search(r"(\d+)", wear)
        if wm:
            try:
                wear_num = int(wm.group(1))
            except Exception:
                pass

    # Год постройки → возраст
    year_num = None
    if year:
        ym = re.search(r"(\d{4})", year)
        if ym:
            try:
                year_num = int(ym.group(1))
            except Exception:
                pass

    details = []
    facts = []
    if year:
        facts.append(f"Год постройки: {year}")
    if series:
        facts.append(f"Серия: {series}")
    if wall:
        facts.append(f"Материал стен: {wall}")
    if floors:
        facts.append(f"Этажей: {floors}")
    if house_info.get("entrances"):
        facts.append(f"Подъездов: {house_info['entrances']}")
    if house_info.get("apartments"):
        facts.append(f"Квартир: {house_info['apartments']}")
    if wear:
        facts.append(f"Износ: {wear}")
    if uk:
        facts.append(f"Управляющая компания: {uk}")
    if emergency:
        facts.append(f"Аварийность: {emergency}")

    if facts:
        details.append("Открытые данные о доме (Минжкх):")
        details.extend(facts)

    # Комментарий по рискам
    risk_notes = []
    if is_emergency:
        risk_notes.append(
            "🚨 Дом признан аварийным или включён в программу расселения. Это критический "
            "риск для покупателя: при расселении выплачивается рыночная цена, но процедура "
            "может занять годы, а компенсация — оказаться ниже желаемой. До сделки уточните "
            "статус расселения у управляющей компании и в местной администрации."
        )
    elif wear_num and wear_num >= 60:
        status = "manual_check" if status == "ok" else status
        risk_notes.append(
            f"⚠️ Износ дома {wear_num}% — высокий. Дома с износом 65%+ кандидаты на признание "
            "аварийными в ближайшие 5–10 лет. Запросите информацию о ремонтных работах и плане "
            "капремонта в управляющей компании."
        )
    elif wear_num and wear_num >= 40:
        risk_notes.append(
            f"Износ {wear_num}% — умеренный. Уточните когда планируется капремонт и какие "
            "работы запланированы (фасад, крыша, инженерные сети, лифт)."
        )

    if year_num:
        from datetime import datetime as _dt
        age = _dt.now().year - year_num
        if age >= 60 and not is_emergency:
            status = "manual_check" if status == "ok" else status
            risk_notes.append(
                f"Дому {age} лет. У домов старше 60 лет повышенные риски: устаревшие "
                "инженерные системы, частые поломки, возможное включение в программу "
                "реновации/КРТ. Проверьте региональную программу расселения."
            )
        if 1957 <= year_num <= 1972 and any(x in (wall or "").lower() for x in ["панел", "блоч", "крупно"]):
            risk_notes.append(
                "Хрущёвка — может попадать в программы реновации/КРТ (Москва, Санкт-Петербург "
                "и другие крупные города). Уточните в местной администрации."
            )

    if risk_notes:
        details.append("Что это значит для сделки:")
        details.extend(risk_notes)

    if uk:
        details.append(
            f"Для запроса справки об отсутствии задолженности по ЖКУ обращайтесь в УК «{uk}». "
            "Эта компания обслуживает дом."
        )

    return {
        "id": "house_info",
        "title": "Информация о доме",
        "status": status,
        "summary": summary + (" — АВАРИЙНЫЙ" if is_emergency else ""),
        "details": details,
        "links": [
            {"label": "Открыть карточку дома (МинЖКХ)", "url": url},
            {"label": "Поиск других данных по дому (Реформа ЖКХ)",
             "url": "https://www.reformagkh.ru/search"
                   + (f"?query={quote((req.address or '').strip())}" if req.address else "")},
        ],
    }


def build_additional_property_checks(req: CheckRequest) -> List[dict]:
    """Дополнительные проверки объекта недвижимости которые делаются через ручную проверку
    (программные API недоступны или платные). Каждый возвращает item с manual_links.
    """
    items = []
    cadnum = ((req.cadastral_number or req.cadnum or req.cadastral or "") or "").strip()
    addr = (req.address or "").strip()

    # 1. Аварийные дома (Реформа ЖКХ + ГИС ЖКХ)
    reforma_url = "https://www.reformagkh.ru/search"
    if addr:
        reforma_url = f"https://www.reformagkh.ru/search?query={quote(addr)}"
    items.append(manual_item(
        "Аварийность дома (Реформа ЖКХ)",
        "Проверьте включён ли дом в программу расселения аварийного жилья.",
        reforma_url,
        [
            "Покупка квартиры в доме признанном аварийным несёт риск принудительного "
            "расселения с возмещением ниже рыночной стоимости.",
            "Откройте ссылку, найдите дом по адресу, посмотрите раздел «Признан аварийным» и "
            "«Расселение». Если дом в программе — обязательно учтите это в переговорах "
            "по цене и срокам сделки.",
        ],
        {"manual_only": True},
        links=[
            {"label": "Реформа ЖКХ — поиск по адресу", "url": reforma_url},
            {"label": "ГИС ЖКХ", "url": "https://dom.gosuslugi.ru"},
        ],
    ))

    # 2. Долги по ЖКХ и капремонту
    items.append(manual_item(
        "Долги по ЖКХ и капремонту",
        "Запросите у продавца справку об отсутствии задолженности.",
        "https://dom.gosuslugi.ru",
        [
            "Долг по капитальному ремонту переходит к новому собственнику автоматически "
            "(часть 3 статьи 158 Жилищного кодекса). Долг по текущим платежам — нет, "
            "но управляющая компания может пытаться взыскать с нового собственника.",
            "Что запросить у продавца:",
            "• Единый платёжный документ за последний месяц с пометкой «Задолженность отсутствует»",
            "• Справка из управляющей компании об отсутствии задолженности",
            "• Справка от регионального оператора капремонта об отсутствии задолженности по взносам",
            "• Если есть долг — он должен быть погашен ДО регистрации перехода права, "
            "это закрепляется в договоре купли-продажи.",
        ],
        {"manual_only": True},
        links=[
            {"label": "ГИС ЖКХ — личный кабинет", "url": "https://dom.gosuslugi.ru"},
        ],
    ))

    # 3. Реновация и градостроительные планы (только для Москвы и СПб)
    if cadnum.startswith("77:") or cadnum.startswith("78:") or "москв" in addr.lower() or "санкт-петербург" in addr.lower() or "спб" in addr.lower():
        if cadnum.startswith("77:"):
            renov_link = "https://www.mos.ru/city/projects/renovation/"
            items.append(manual_item(
                "Программа реновации (Москва)",
                "Проверьте включён ли дом в программу реновации г. Москвы.",
                renov_link,
                [
                    "Если дом включён в программу — это меняет экономику сделки: "
                    "владелец получит новую квартиру, но сроки и параметры замены могут не "
                    "совпадать с ожиданиями покупателя.",
                ],
                {"manual_only": True},
                links=[{"label": "Программа реновации Москвы", "url": renov_link}],
            ))

    # 4. История перехода права (выписка из ЕГРН о переходах)
    items.append(manual_item(
        "История перехода права (ЕГРН)",
        "Закажите расширенную выписку из ЕГРН с историей переходов права.",
        "https://rosreestr.gov.ru/eservices/services-egrn/sale-egrn/",
        [
            "Базовая выписка ЕГРН показывает текущего собственника, но НЕ показывает "
            "историю предыдущих сделок. Если объект менял собственника часто (3+ раза за "
            "последние 5 лет) — это тревожный признак. Возможные причины: попытка размыть "
            "цепочку оспоримых сделок, мошенническая схема.",
            "Стоимость расширенной выписки — около 950 рублей через Госуслуги.",
            "Альтернатива — выписка о переходе права через МФЦ.",
        ],
        {"manual_only": True},
        links=[
            {"label": "Заказать выписку из ЕГРН (Госуслуги)", "url": "https://www.gosuslugi.ru/600362/1/info"},
            {"label": "Росреестр — официальный сайт", "url": "https://rosreestr.gov.ru"},
        ],
    ))

    # 5. Зарегистрированные жильцы (форма 9 / архивная)
    items.append(manual_item(
        "Зарегистрированные лица (форма 9)",
        "Запросите у продавца справку о зарегистрированных лицах (форма 9).",
        "",
        [
            "Лица сохраняющие право пользования жильём после продажи (статья 292 "
            "Гражданского кодекса) — это реальный риск: их нельзя выписать без суда.",
            "К таким лицам относятся: бывшие члены семьи отказавшиеся от приватизации, "
            "получатели ренты, лица проживающие по завещательному отказу, несовершеннолетние "
            "под опекой.",
            "Запросите ОБЯЗАТЕЛЬНО:",
            "• Текущая форма 9 (справка о зарегистрированных лицах) — не старше 1 месяца",
            "• Архивная форма 9 — показывает кто был зарегистрирован за всю историю объекта. "
            "Это ключевая справка для выявления «скрытых» жильцов.",
            "В Санкт-Петербурге — заказывается через МФЦ или ЕИРЦ.",
        ],
        {"manual_only": True},
        links=[],
    ))

    return items


def parse_share_fraction(share_str: str) -> Optional[float]:
    """Преобразует строку доли в число от 0 до 1.
    Принимает: '1/2', '50%', '0.5', '1', 'целиком' и т.п.
    Возвращает None если распарсить не удалось.
    """
    if not share_str:
        return None
    s = str(share_str).strip().lower().replace(",", ".")
    if not s:
        return None
    if any(w in s for w in ["целик", "полнос", "все", "100%", "1/1"]):
        return 1.0
    # Дробь "a/b"
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", s)
    if m:
        try:
            num, den = int(m.group(1)), int(m.group(2))
            if den > 0 and 0 < num <= den:
                return num / den
        except Exception:
            return None
    # Процент
    m = re.match(r"^\s*([\d.]+)\s*%\s*$", s)
    if m:
        try:
            v = float(m.group(1)) / 100.0
            if 0 < v <= 1:
                return v
        except Exception:
            return None
    # Дробь как число "0.5", "1"
    try:
        v = float(s)
        if 0 < v <= 1:
            return v
    except Exception:
        pass
    return None


def aggregate_scores_by_share(all_scorings: List[dict], participants: List[dict]) -> Optional[dict]:
    """Взвешенная агрегация скоринга по долям собственников.
    Возвращает None если у кого-то нет валидной доли — тогда вызывающий код
    использует max() как раньше.

    Формула: weighted = sum(score_i * share_i) для всех i.
    Дополнительно: если у крупного собственника (>= 50%) высокий скор —
    итог поднимается до уровня этого собственника, чтобы не размыть сигнал.
    """
    if not all_scorings or not participants:
        return None
    if len(all_scorings) != len(participants):
        return None

    fractions = [p.get("share_fraction") for p in participants]
    if any(f is None for f in fractions):
        return None
    total = sum(fractions)
    if total <= 0 or total > 1.05:  # допускаем округление
        return None

    # Нормализуем (если суммы 0.99/1.01 из-за дробей)
    norm_fractions = [f / total for f in fractions]

    weighted_score = sum(s["score"] * f for s, f in zip(all_scorings, norm_fractions))

    # Если у крупного собственника (>=50%) скор сильно выше — берём его, чтоб не размыть
    final_score = weighted_score
    for s, p, f in zip(all_scorings, participants, fractions):
        if f >= 0.5 and s["score"] > weighted_score + 15:
            final_score = s["score"]
            break

    final_score = round(final_score)

    # Уровень и label берём из «эталонного» скоринга — пересоздавать всю логику не нужно
    # Используем готовую функцию или ближайший
    closest = min(all_scorings, key=lambda s: abs(s["score"] - final_score))
    factor_rows = []
    for i, (s, p) in enumerate(zip(all_scorings, participants)):
        share_pct = round(p.get("share_fraction", 0) * 100)
        factor_rows.append({
            "source": f"{p['label']} (доля {share_pct}%)",
            "points": s["score"],
            "severity": "info",
            "text": f"Скор {s['score']} × доля {share_pct}% = вклад {round(s['score'] * p.get('share_fraction', 0))}",
        })

    return {
        "score": final_score,
        "level": closest.get("level", "допустимая"),
        "label": closest.get("label", ""),
        "conclusion": closest.get("conclusion", ""),
        "factor_rows": factor_rows + (closest.get("factor_rows") or []),
        "_aggregation": "weighted_by_share",
    }


async def send_report_pdf_email(to_email: str, report: dict) -> dict:
    to_email = clean_email(to_email)
    if not to_email:
        return {"sent": False, "reason": "email_empty"}
    if not RESEND_API_KEY:
        return {"sent": False, "reason": "resend_not_configured"}
    if not REPORTLAB_AVAILABLE:
        return {"sent": False, "reason": "pdf_not_available"}

    report_id = str(report.get("report_id") or "")
    pdf_bytes = build_pdf_bytes(report)
    filename = f"real_estate_report_{report_id[:8] or 'report'}.pdf"
    score = ((report.get("risk_scoring") or {}).get("score"))
    decision = ((report.get("advance_decision") or {}).get("decision") or "Отчёт готов")

    html = f"""
        <p>Здравствуйте.</p>
        <p>Ваш отчёт по проверке недвижимости готов и приложен к письму в формате PDF.</p>
        <p><strong>Решение по авансу:</strong> {decision}</p>
        <p><strong>Балл риска:</strong> {score if score is not None else "не рассчитан"} из 100</p>
        <p>Файл отчёта приложен к этому письму.</p>
        <p>Это информационно-аналитическое заключение не заменяет юридическую проверку документов.</p>
    """
    text = (
        "Здравствуйте.\n\n"
        "Ваш отчёт по проверке недвижимости готов и приложен к письму в формате PDF.\n"
        f"Решение по авансу: {decision}\n"
        f"Балл риска: {score if score is not None else 'не рассчитан'} из 100\n\n"
        "Файл отчёта приложен к этому письму.\n"
    )
    payload = {
        "from": REPORT_FROM_EMAIL,
        "to": [to_email],
        "subject": "Ваш отчёт по проверке недвижимости",
        "html": html,
        "text": text,
        "attachments": [{
            "filename": filename,
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
        }],
        "tags": [
            {"name": "type", "value": "real_estate_report"},
            {"name": "report_id", "value": (report_id[:32] or "unknown")},
        ],
    }
    if REPORT_REPLY_TO_EMAIL:
        payload["reply_to"] = REPORT_REPLY_TO_EMAIL

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        logger.warning(f"Resend email error {resp.status_code}: {resp.text[:500]}")
        reason = "resend_error"
        try:
            err_data = resp.json()
            reason = (
                err_data.get("message")
                or err_data.get("error")
                or err_data.get("name")
                or reason
            )
        except Exception:
            pass
        return {"sent": False, "reason": reason, "status_code": resp.status_code}
    data = resp.json()
    return {"sent": True, "id": data.get("id")}


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

    # Доп.проверки объекта (аварийность, ЖКХ, история переходов, форма 9, рыночная цена, информация о доме)
    # Выполняются один раз для объекта, добавляются к чеклисту первого собственника.
    additional_property_items = []
    if ((req.cadastral_number or req.cadnum or req.cadastral or "") or "").strip() or (req.address or "").strip():
        try:
            additional_property_items = build_additional_property_checks(req)
        except Exception as e:
            logger.warning(f"Не удалось построить доп.проверки объекта: {e}")
            additional_property_items = []

        # Рыночная цена кв.м (Росстат) — захардкоженная таблица, без сетевого запроса
        try:
            property_area_m2 = extract_property_area(egrn_resp, nspd_resp)
            market_item = build_market_price_check(req, area_m2=property_area_m2)
            if market_item:
                additional_property_items.insert(0, market_item)
        except Exception as e:
            logger.warning(f"Не удалось построить блок рыночной цены: {e}")

        # Информация о доме (МинЖКХ) — парсинг публичных страниц
        if (req.address or "").strip():
            try:
                async with httpx.AsyncClient() as mingkh_client:
                    house_info = await fetch_mingkh_house_info(mingkh_client, req.address)
                if house_info:
                    house_item = build_house_info_check(house_info, req)
                    if house_item:
                        additional_property_items.insert(0, house_item)
            except Exception as e:
                logger.warning(f"Не удалось получить инфо о доме: {e}")

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

        # Доп.проверки объекта добавляем только в чеклист первого собственника
        is_first = (i == 1)
        checklist = classify_all_v5(
            complex_resp=person_res.get("complex") or {},
            pravo_resp=person_res.get("pravo_search") or {},
            details_resps=person_res.get("pravo_details") or [],
            egrn_resp=egrn_resp,
            nspd_resp=nspd_resp,
            owner=owner,
            filtered_cases=person_res.get("filtered_cases") or {},
            fssp_retry_resp=person_res.get("fssp_retry"),
            additional_property_items=additional_property_items if is_first else None,
        )

        # Добавляем префикс владельца к каждому пункту если несколько собственников.
        # ИСКЛЮЧЕНИЕ: пункты привязанные к объекту, а не к человеку, не префиксуем.
        OBJECT_TITLES_NO_PREFIX = (
            "ЕГРН", "Геоданные", "Аварийность", "ЖКХ", "капремонт", "Реновация",
            "История перехода", "Зарегистрированные лица", "форма 9",
            "Рыночная цена", "Информация о доме",
        )
        if len(owners) > 1:
            for item in checklist:
                title = item.get("title", "")
                if not any(t in title for t in OBJECT_TITLES_NO_PREFIX):
                    item["title"] = f"{label} — {title}"

        scoring = risk_scoring_v5(checklist, age=age)
        recs = build_recommendations_v5(checklist, age=age)
        legal = "" if req.skip_report else await maybe_deepseek_report(owner, checklist, scoring, recs)
        if legal:
            legal = sanitize_phantom_sources(legal, checklist)

        all_checklists.append(checklist)
        all_scorings.append(scoring)
        all_reports.append(legal)

        participants_out.append({
            "label": label,
            "is_minor": False,
            "age": age,
            "score": scoring["score"],
            "level": scoring["level"],
            "share": (owner.share or "").strip(),
            "share_fraction": parse_share_fraction(owner.share),
        })

    # Агрегация скоринга:
    # - Если у всех собственников есть валидные доли — взвешенно, с надбавкой за крупного риск-собственника
    # - Иначе — берём максимальный скор (как было)
    combined_scoring = aggregate_scores_by_share(all_scorings, participants_out)
    if not combined_scoring:
        if all_scorings:
            combined_scoring = max(all_scorings, key=lambda s: s["score"])
        else:
            combined_scoring = {"score": 0, "level": "допустимая", "label": "Нет данных",
                                "conclusion": "Данные не получены.", "factor_rows": []}

    combined_checklist = [item for cl in all_checklists for item in cl]

    # Если несколько собственников — добавляем спецблок про долевую собственность
    if len([o for o in owners if not is_minor_owner(o)]) >= 2:
        share_item = manual_item(
            "Долевая собственность — порядок сделки",
            "Сделка с долями требует нотариального удостоверения.",
            "https://rosreestr.gov.ru",
            [
                "При продаже доли посторонним лицам остальные участники долевой собственности "
                "имеют преимущественное право покупки (статья 250 Гражданского кодекса). "
                "Продавец должен письменно уведомить их за 30 дней до сделки и получить "
                "нотариальные отказы.",
                "Сделка с долей подлежит обязательному нотариальному удостоверению "
                "(часть 1.1 статьи 42 Федерального закона № 218-ФЗ «О государственной "
                "регистрации недвижимости»).",
                "Если продаются ВСЕ доли одновременно одному покупателю — нотариальная форма "
                "не обязательна, но рекомендуется для надёжности.",
                "Что проверить:",
                "• Письменные уведомления о продаже остальным участникам долевой собственности",
                "• Нотариальные отказы или истечение 30-дневного срока",
                "• Согласия органов опеки если среди сособственников есть несовершеннолетний",
            ],
            {"manual_only": True},
            links=[
                {"label": "Росреестр — сделки с долями", "url": "https://rosreestr.gov.ru"},
            ],
        )
        combined_checklist.append(share_item)

    # Если кто-то указал что состоит в браке или объект приобретался в браке — добавляем блок
    married_owners = [o for o in owners if (o.is_married or o.married_via_object) and not is_minor_owner(o)]
    if married_owners:
        names = ", ".join((o.last + " " + o.first).strip() for o in married_owners)
        spouse_item = manual_item(
            "Согласие супруга",
            "Требуется нотариальное согласие супруга на сделку.",
            "",
            [
                f"Указано что в браке состоит: {names}.",
                "Если объект был приобретён в период брака — он является общим имуществом "
                "супругов независимо от того, на кого зарегистрирован (статья 34 Семейного "
                "кодекса). Для сделки требуется НОТАРИАЛЬНОЕ согласие второго супруга "
                "(статья 35 Семейного кодекса).",
                "Что обязательно сделать:",
                "• Получить от продавца нотариально удостоверенное согласие супруга на сделку",
                "• Если супруги в разводе — запросить соглашение о разделе имущества или "
                "решение суда о разделе",
                "• Если объект был куплен ДО брака или получен в дар/наследство — запросить "
                "правоустанавливающий документ подтверждающий это (тогда согласие не нужно)",
                "Без согласия супруга сделка может быть оспорена в течение года с момента, "
                "когда супруг узнал о ней — это серьёзный риск для покупателя.",
            ],
            {"manual_only": True},
            links=[],
        )
        combined_checklist.append(spouse_item)

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

    email_to = clean_email(req.email)
    if email_to:
        try:
            email_status = await send_report_pdf_email(email_to, stored)
            result["email_delivery"] = strip_service_fields(email_status)
            stored["email_delivery"] = strip_service_fields(email_status)
        except Exception as e:
            logger.warning(f"Не удалось отправить отчёт на email: {e}")
            result["email_delivery"] = {"sent": False, "reason": "send_failed"}
            stored["email_delivery"] = result["email_delivery"]

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


def p_links(text):
    """Как p(), но превращает URL в кликабельные ссылки (синие, подчёркнутые).
    reportlab поддерживает <link href="...">текст</link> внутри Paragraph.
    """
    safe = p(text)
    # Находим http(s) URL и заменяем на <link>
    url_pattern = re.compile(r'(https?://[^\s<>"\']+)')
    def replace_url(m):
        url = m.group(1).rstrip('.,;:!?')
        # короткое отображение если URL длинный
        display = url if len(url) <= 60 else url[:57] + "..."
        return f'<link href="{url}" color="#1A4F8F"><u>{display}</u></link>'
    return url_pattern.sub(replace_url, safe)


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

    def add_checked_data_block():
        mode = detect_check_mode(checklist)
        participants = report.get("participants") or []
        rows = [[
            Paragraph("Что проверялось", styles["Z_TableHead"]),
            Paragraph("Данные в отчёте", styles["Z_TableHead"]),
        ]]
        rows.append([
            Paragraph("Режим", styles["Z_TableCell"]),
            Paragraph(p(mode["label"]), styles["Z_TableCell"]),
        ])
        if mode["code"] != "object" and participants:
            seller_lines = []
            for idx, part in enumerate(participants, 1):
                label = clean_str(part.get("label") or f"Продавец {idx}")
                age_txt = f", возраст: {part.get('age')} лет" if part.get("age") else ""
                share_txt = f", доля: {part.get('share')}" if part.get("share") else ""
                seller_lines.append(f"{label}{age_txt}{share_txt}")
            rows.append([
                Paragraph("Продавец", styles["Z_TableCell"]),
                Paragraph(p("\n".join(seller_lines)), styles["Z_TableCell"]),
            ])
        object_bits = []
        for item in checklist:
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            if data.get("cadNumber") or data.get("area") or data.get("objType_text"):
                if data.get("cadNumber"): object_bits.append(f"Кадастровый номер: {data.get('cadNumber')}")
                if data.get("objType_text"): object_bits.append(f"Тип: {data.get('objType_text')}")
                if data.get("area"): object_bits.append(f"Площадь: {data.get('area')} кв.м")
                break
            obj = data.get("object") if isinstance(data.get("object"), dict) else {}
            if obj.get("address") or obj.get("cad_cost"):
                if obj.get("address"): object_bits.append(f"Адрес: {format_registry_value(obj.get('address'))[:120]}")
                if obj.get("cad_cost"): object_bits.append(f"Кадастровая стоимость: {rub(obj.get('cad_cost'))}")
                break
        if object_bits:
            rows.append([
                Paragraph("Объект", styles["Z_TableCell"]),
                Paragraph(p("\n".join(object_bits)), styles["Z_TableCell"]),
            ])
        tbl = Table(rows, colWidths=[48*mm, 126*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor(Palette.DARK_BLUE)),
            ("GRID",(0,0),(-1,-1), 0.3, colors.HexColor("#D9D3C8")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor(Palette.OFF_WHITE), colors.HexColor(Palette.WHITE)]),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 4*mm))

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
    add_checked_data_block()

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
            details = (item.get("details") or [])[:4]
            if details:
                for d in details:
                    summary_cell.append(Paragraph(p_links(f"— {d}"), styles["Z_Detail"]))
        if isinstance(data, dict):
            # ФССП
            if data.get("active_count") is not None:
                summary_cell.append(Paragraph(
                    p(f"Активных: {data.get('active_count',0)}, закрытых: {data.get('closed_count',0)}, долг: {rub(data.get('actual_debt',0))}"),
                    styles["Z_Detail"]
                ))
                for group_title, rows in [
                    ("Активные записи ФССП", data.get("active_items") or []),
                    ("Закрытые записи ФССП", data.get("closed_items") or []),
                ]:
                    if rows:
                        summary_cell.append(Paragraph(p(group_title), styles["Z_Detail"]))
                        for ip in rows[:6]:
                            parts = []
                            if ip.get("ip_number"): parts.append(f"ИП: {ip.get('ip_number')}")
                            if ip.get("subject"): parts.append(f"Предмет: {ip.get('subject')[:90]}")
                            if ip.get("completion"): parts.append(f"Окончание: {ip.get('completion')[:90]}")
                            if parts:
                                summary_cell.append(Paragraph(p(" | ".join(parts)), styles["Z_Detail"]))
            # Залоги — ссылки на уведомления ФНП
            fnp_urls = data.get("fnp_urls") or []
            if fnp_urls:
                summary_cell.append(Paragraph(p("Ссылки на уведомления ФНП:"), styles["Z_Detail"]))
                for u in fnp_urls[:5]:
                    summary_cell.append(Paragraph(p_links(f"  • {u}"), styles["Z_Detail"]))
                summary_cell.append(Paragraph(
                    p("ФНП — движимое имущество. Это не ипотека и не обременение объекта по ЕГРН."),
                    styles["Z_Detail"]
                ))
            # Структурированные ссылки (банкротство Федресурс, арбитраж КАД)
            structured_links = data.get("links") or []
            if structured_links:
                for ln in structured_links[:6]:
                    if isinstance(ln, dict) and ln.get("url"):
                        label = ln.get("label") or ln.get("url")
                        url = ln["url"]
                        line = f'  • <link href="{p(url)}" color="#1A4F8F"><u>{p(label)}</u></link>'
                        summary_cell.append(Paragraph(line, styles["Z_Detail"]))
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
                    egrn_parts.append(f"Зарегистрированных прав: {len(rights)}")
                    for idx, r in enumerate(rights[:8], 1):
                        if isinstance(r, dict):
                            owner_n = clean_str(r.get("rightHolder") or r.get("owner") or "")
                            reg_d = clean_str(r.get("registrationDate") or r.get("regDate") or "")
                            right_t = format_registry_value(r.get("rightType") or r.get("type") or "", kind="code")
                            share = clean_str(r.get("shareText") or r.get("share") or "")
                            reg_num = clean_str(r.get("registrationNumber") or r.get("regNumber") or r.get("number") or "")
                            row = [f"Право {idx}"]
                            if owner_n: row.append(f"правообладатель: {owner_n[:60]}")
                            if right_t: row.append(f"вид: {right_t}")
                            if share: row.append(f"доля: {share}")
                            if reg_d: row.append(f"дата: {reg_d}")
                            if reg_num: row.append(f"номер: {reg_num}")
                            egrn_parts.append(" | ".join(row))
                if data.get("encumbrances"):
                    enc = data["encumbrances"] if isinstance(data["encumbrances"], list) else []
                    egrn_parts.append(f"Обременений: {len(enc)}")
                    for idx, e in enumerate(enc[:8], 1):
                        if isinstance(e, dict):
                            enc_t = format_registry_value(e.get("type") or e.get("encumbranceType") or "", kind="code")
                            enc_h = clean_str(e.get("holder") or e.get("encumbranceHolder") or "")
                            enc_d = clean_str(e.get("startDate") or e.get("date") or e.get("registrationDate") or "")
                            enc_n = clean_str(e.get("number") or e.get("registrationNumber") or "")
                            row = [f"Обременение {idx}"]
                            if enc_t: row.append(f"тип: {enc_t}")
                            if enc_h: row.append(f"держатель: {enc_h[:60]}")
                            if enc_d: row.append(f"дата: {enc_d}")
                            if enc_n: row.append(f"номер: {enc_n}")
                            egrn_parts.append(" | ".join(row))
                for ep in egrn_parts:
                    summary_cell.append(Paragraph(p(ep), styles["Z_Detail"]))
            # Геоданные
            if data.get("geo_center"):
                gc = data["geo_center"]
                obj = data.get("object") or {}
                geo_parts = []
                if obj.get("address"): geo_parts.append(f"Адрес: {format_registry_value(obj.get('address'))[:100]}")
                if obj.get("cad_cost"): geo_parts.append(f"Кад. стоимость: {rub(obj['cad_cost'])}")
                if obj.get("year_built"): geo_parts.append(f"Год постройки: {obj['year_built']}")
                if gc.get("lat") and gc.get("lon"):
                    geo_parts.append(f"Координаты: {gc['lat']}, {gc['lon']}")
                    geo_parts.append(f"Карта: https://yandex.ru/maps/?ll={gc['lon']},{gc['lat']}&z=17&pt={gc['lon']},{gc['lat']},pm2rdm")
                points = data.get("geo_points")
                if isinstance(points, list) and points:
                    geo_parts.append(f"Точек кадастрового контура: {len(points)}")
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
    rendered_blocks = 0
    rendered_bullets = 0
    for block_type, text in pdf_report_blocks(legal_text):
        if not text.strip():
            continue
        if block_type == "h":
            rendered_blocks += 1
            if rendered_blocks > 7:
                continue
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph(p(text), styles["Z_H2"]))
        elif block_type == "bullet":
            rendered_bullets += 1
            if rendered_bullets > 22:
                continue
            story.append(Paragraph(f"• {p(text)}", styles["Z_Body"]))
        else:
            short_text = text if len(text) <= 650 else text[:647].rstrip() + "..."
            story.append(Paragraph(p(short_text), styles["Z_Body"]))

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
        "Рекомендуется привлечь квалифицированного специалиста по недвижимости или нотариуса.",
        Palette.OFF_WHITE
    )

    doc.build(story)
    return buf.getvalue()
  # Красивый PDF через WeasyPrint
try:
    from pdf_beautiful import build_pdf_bytes as build_pdf_bytes
except ImportError:
    pass


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
        "deepseek_enabled": bool(USE_DEEPSEEK_REPORT and DEEPSEEK_API_KEY),
        "deepseek_model": DEEPSEEK_MODEL if USE_DEEPSEEK_REPORT and DEEPSEEK_API_KEY else None,
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
        "newdb_cache": newdb_cache_stats(),
        "turnstile_enabled": bool(TURNSTILE_SECRET),
    }


async def verify_turnstile(token: str, remote_ip: str = "") -> bool:
    """Проверяет токен Cloudflare Turnstile. Возвращает True если токен валиден.
    Если TURNSTILE_SECRET не задан — пропускает проверку (отключена).
    """
    if not TURNSTILE_SECRET:
        return True  # капча отключена
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(TURNSTILE_VERIFY_URL, data={
                "secret": TURNSTILE_SECRET,
                "response": token,
                "remoteip": remote_ip,
            })
            data = resp.json()
            success = bool(data.get("success"))
            if not success:
                logger.warning(f"Turnstile отклонил токен: {data.get('error-codes')}")
            return success
    except Exception as e:
        logger.warning(f"Turnstile verify error: {e}")
        # При сетевой ошибке — пропускаем (fail-open) чтобы не блокировать клиентов
        return True


@app.post("/check-report")
async def check_report(req: CheckRequest, request: Request):
    verify_widget_key(request)
    ip = request.client.host if request.client else "unknown"

    # ФЗ-152: согласие на обработку ПД обязательно
    if not req.consent:
        raise HTTPException(
            status_code=422,
            detail="Необходимо подтвердить согласие на обработку персональных данных."
        )
    # Логируем факт согласия (IP + время) — на случай проверки регулятором
    logger.info(f"[consent] accepted ip={ip} owners={len(req.owners or [])}")

    # Капча: проверяем ДО rate-limit и тяжёлой работы
    if TURNSTILE_SECRET:
        ok = await verify_turnstile(req.turnstile_token, ip)
        if not ok:
            raise HTTPException(status_code=403, detail="Не пройдена проверка на бота. Обновите страницу и попробуйте ещё раз.")
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


@app.post("/debug-passport")
async def debug_passport(req: CheckRequest, request: Request):
    """Отладочный эндпоинт — делает только complex_by_passport и возвращает сырой ответ NewDB."""
    verify_debug_key(request)
    owners = owners_from_request(req)
    if not owners:
        return {"error": "Нет данных продавца"}
    owner = owners[0]
    payload = build_complex_by_passport_payload(owner)
    if not payload:
        series, number = normalize_passport_owner(owner)
        dob_ru, dob_iso = normalize_dob(owner.dob)
        return {
            "error": "Payload = None",
            "debug": {
                "last": owner.last, "first": owner.first,
                "passport_series": owner.passport_series,
                "passport_number": owner.passport_number,
                "seriapass": owner.seriapass, "numberpass": owner.numberpass,
                "seria": owner.seria, "number": owner.number,
                "series_extracted": series, "number_extracted": number,
                "dob_raw": owner.dob, "dob_iso": dob_iso,
                "is_minor": is_minor_owner(owner),
            }
        }
    async with httpx.AsyncClient() as client:
        resp = await newdb_run(client, payload, "complex_by_passport")
    return {
        "payload_sent": payload,
        "raw_response": resp,
        "state": resp.get("state"),
        "has_errors_info": bool(resp.get("errors_info")),
        "errors_info": resp.get("errors_info"),
        "results_keys": list((resp.get("results") or {}).keys()),
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
