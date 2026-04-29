"""
Real Estate Seller & Property Check API — v4pro (DeepSeek)
Copyright (c) 2026 Ugolnikov SPb. All rights reserved.

Исправления v4.1:
- Логирование ошибок
- Проверка HTTP-статуса DeepSeek
- Улучшенный судебный match score (жёстче критерии)
- Детальный анализ банкротства (сроки, даты)
- ЕГРН: давность права, частые переходы
- Усиленное предупреждение для несовершеннолетних
- Текстовые статусы в PDF вместо эмодзи
- Логирование ошибок в /check-report
"""

import asyncio, io, json, logging, os, re, time, uuid
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

# -------------------- Настройки --------------------
APP_VERSION = "4.1.0-pro-deepseek"
NEWDB_URL = "https://api.newdb.net/v2"
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
USE_DEEPSEEK_REPORT = os.getenv("USE_DEEPSEEK_REPORT", "0").strip().lower() in {"1", "true", "yes", "on"}

# Безопасность
ALLOWED_ORIGINS = ["https://ugolnikovspb.ru", "https://www.ugolnikovspb.ru"]
PUBLIC_WIDGET_API_KEY = os.getenv("PUBLIC_WIDGET_API_KEY", "pwk_7Fh29LmQx8VdR3sKzYp4TnU6Bc1WeXa")
ENABLE_DEBUG_NEWDB = os.getenv("ENABLE_DEBUG_NEWDB", "0").lower() in {"1", "true", "yes", "on"}
DEBUG_API_KEY = os.getenv("DEBUG_API_KEY", "debug-key-change-me")
REPORT_TTL_SECONDS = int(os.getenv("REPORT_TTL_SECONDS", "43200"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW = 3600

REPORTS: Dict[str, Dict[str, Any]] = {}
_REPORT_TIMESTAMPS: Dict[str, float] = {}
_rate_limit_store: Dict[str, List[float]] = defaultdict(list)

app = FastAPI(title="Real Estate Seller & Property Check API v4pro", version=APP_VERSION)
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
    seller_inn: str = ""
    inn_fiz: str = ""
    innfiz: str = ""
    innfl: str = ""
    passport_series: str = ""
    passport_number: str = ""
    seria: str = ""
    series: str = ""
    number: str = ""
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
    seller_inn: str = ""
    inn_fiz: str = ""
    innfiz: str = ""
    innfl: str = ""
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
    run_passport: bool = True
    run_passport_fns: bool = True
    run_fssp: bool = True
    run_bankruptcy: bool = True
    run_arbitr: bool = True
    run_pravosud: bool = True
    run_egrn: bool = True
    run_nalog_debt: bool = True
    run_egrul_ip: bool = True

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
    if not raw: return "", ""
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
        if len(d) == 12: return d
    return ""

def mask_inn(inn: str) -> str:
    d = digits_only(inn)
    return f"{d[:4]}****{d[-4:]}" if len(d) == 12 else ""

def calculate_age(dob_ru: str) -> Optional[int]:
    if not dob_ru: return None
    try:
        parts = dob_ru.split(".")
        if len(parts) != 3: return None
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        dt = datetime(y, m, d)
        today = datetime.now()
        age = today.year - dt.year - ((today.month, today.day) < (dt.month, dt.day))
        return age if 0 <= age <= 120 else None
    except Exception:
        return None

def parse_date_any(value: Any) -> Optional[datetime]:
    if not value: return None
    raw = str(value).strip().replace("T", " ").replace("Z", "").strip()[:19]
    for fmt in ["%d.%m.%Y", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try: return datetime.strptime(raw, fmt)
        except Exception: continue
    return None

def months_between_dates(date_from: Optional[datetime], date_to: Optional[datetime] = None) -> Optional[int]:
    if not date_from: return None
    date_to = date_to or datetime.now()
    months = (date_to.year - date_from.year) * 12 + (date_to.month - date_from.month)
    if date_to.day < date_from.day: months -= 1
    return max(0, months)

def normalize_passport(req: CheckRequest) -> Tuple[str, str]:
    series = digits_only(req.passport_series or req.seria or req.series)[:4]
    number = digits_only(req.passport_number or req.number)[:6]
    return series, number

def normalize_region(req: CheckRequest) -> int:
    try: return int(req.regioncode or req.region or 0)
    except Exception: return 0

def normalize_property(req: CheckRequest) -> Dict[str, str]:
    query = clean_str(req.cadastral_number or req.cadnum or req.cadastral or req.property_query or req.address)
    is_cad = bool(re.match(r"^\d{2}:\d{2}:\d+", query))
    return {"query": query, "cadastral_number": query if is_cad else "", "address": query, "type": "cadastral" if is_cad else "address"}

def rub(value: Any) -> str:
    try: n = float(value or 0)
    except Exception: n = 0.0
    s = f"{int(round(n)):,}".replace(",", " ") if abs(n - int(n)) < 0.005 else f"{n:,.2f}".replace(",", " ")
    return f"{s} ₽"

def flatten_text(value: Any) -> str:
    try: return json.dumps(value, ensure_ascii=False)
    except Exception: return str(value)

# -------------------- Совместимость моделей --------------------
def owner_to_check_request(owner: OwnerRequest, base: Optional[CheckRequest] = None) -> CheckRequest:
    base = base or CheckRequest()
    return CheckRequest(
        last=owner.last, first=owner.first, middle=owner.middle, dob=owner.dob,
        inn=owner.inn, seller_inn=owner.seller_inn, inn_fiz=owner.inn_fiz, innfiz=owner.innfiz, innfl=owner.innfl,
        passport_series=owner.passport_series, passport_number=owner.passport_number,
        seria=owner.seria, series=owner.series, number=owner.number,
        region=owner.region, regioncode=owner.regioncode,
        cadastral_number=base.cadastral_number, cadnum=base.cadnum, cadastral=base.cadastral,
        address=base.address, property_query=base.property_query,
        run_passport=base.run_passport, run_passport_fns=base.run_passport_fns,
        run_fssp=base.run_fssp, run_bankruptcy=base.run_bankruptcy,
        run_arbitr=base.run_arbitr, run_pravosud=base.run_pravosud,
        run_egrn=base.run_egrn, run_nalog_debt=base.run_nalog_debt, run_egrul_ip=base.run_egrul_ip,
    )

def owners_from_request(req: CheckRequest) -> List[OwnerRequest]:
    if req.owners: return req.owners
    if any(clean_str(x) for x in [req.last, req.first, req.middle, req.dob, req.passport_series, req.passport_number, req.seria, req.series, req.number, req.inn, req.seller_inn, req.inn_fiz, req.innfiz, req.innfl]):
        return [OwnerRequest(last=req.last, first=req.first, middle=req.middle, dob=req.dob, inn=req.inn, seller_inn=req.seller_inn, inn_fiz=req.inn_fiz, innfiz=req.innfiz, innfl=req.innfl, passport_series=req.passport_series, passport_number=req.passport_number, seria=req.seria, series=req.series, number=req.number, region=req.region, regioncode=req.regioncode, role="owner")]
    return []

def person_key_from_owner(owner: OwnerRequest) -> str:
    req = owner_to_check_request(owner)
    series, number = normalize_passport(req)
    dob_ru, _ = normalize_dob(owner.dob)
    return "|".join([clean_str(owner.last).lower(), clean_str(owner.first).lower(), clean_str(owner.middle).lower(), dob_ru, series, number])

def unique_representatives(req: CheckRequest, owners: List[OwnerRequest]) -> List[OwnerRequest]:
    checked = {person_key_from_owner(o) for o in owners if person_key_from_owner(o).strip("|")}
    result = []
    for rep in req.representatives or []:
        key = person_key_from_owner(rep)
        if key and key in checked: continue
        if key: checked.add(key)
        result.append(rep)
    return result

def is_minor_owner(owner: OwnerRequest) -> bool:
    if owner.is_minor is not None: return bool(owner.is_minor)
    age = calculate_age(normalize_dob(owner.dob)[0])
    return bool(age is not None and age < 18)

def participant_label(index: int, owner: OwnerRequest, representative: bool = False) -> str:
    base = "Законный представитель" if representative or owner.role in {"representative", "guardian"} else "Собственник"
    name = " ".join(x for x in [owner.last.strip(), owner.first.strip(), owner.middle.strip()] if x)
    suffix = f": {name}" if name else ""
    return f"{base} {index}{suffix}"

def max_relevant_age(req: Optional[CheckRequest]) -> Optional[int]:
    if not req: return None
    owners = owners_from_request(req)
    reps = unique_representatives(req, owners)
    ages = []
    for owner in owners + reps:
        age = calculate_age(normalize_dob(owner.dob)[0])
        if age is not None and age >= 18: ages.append(age)
    return max(ages) if ages else calculate_age(normalize_dob(req.dob)[0])

# -------------------- Очистка данных --------------------
def strip_service_fields(data: Any) -> Any:
    forbidden = {
        "requestId", "newdb_qid", "taskId", "balance", "_http_status",
        "errors_info", "docs_url", "params", "datecreated", "dateupdated",
        "is_repeat", "tasks", "req", "raw", "_primary_error", "_fallback_tried",
        "inn", "seller_inn", "inn_fiz", "innfiz", "innfl", "final_inn",
        "manual_inn", "fns_inn", "passport_series", "passport_number",
        "seria", "series", "number",
    }
    if isinstance(data, dict):
        return {k: strip_service_fields(v) for k, v in data.items() if k not in forbidden}
    if isinstance(data, list):
        return [strip_service_fields(x) for x in data]
    return data

def short_registry_data(data: Any, limit: int = 8) -> Any:
    data = strip_service_fields(data)
    if isinstance(data, list): return [short_registry_data(x, limit) for x in data[:limit]]
    if not isinstance(data, dict): return data
    allowed = {"amount", "actual_debt", "active_sum", "closed_sum", "unknown_sum", "all_count", "active_count", "closed_count", "unknown_count", "bankruptcy_status", "latest_publication_date", "months_after_latest", "property_related_words", "court_match_score", "match_level", "case_number", "court", "date", "role", "category", "status", "result", "summary", "inn_status", "inn_masked", "cadNumber", "objType_text", "purpose_text", "area", "cadCost", "_egrn_risk_profile"}
    out = {}
    for k in allowed:
        if k in data and data.get(k) not in (None, "", [], {}):
            out[k] = short_registry_data(data.get(k), limit)
    if isinstance(data.get("address"), dict):
        addr = data.get("address") or {}
        if addr.get("readableAddress"): out["address"] = {"readableAddress": addr.get("readableAddress")}
    if isinstance(data.get("encumbrances"), list):
        encs = [{"typeDesc": e.get("typeDesc"), "encumbranceNumber": e.get("encumbranceNumber"), "startDate": e.get("startDate")} for e in data.get("encumbrances")[:limit] if isinstance(e, dict)]
        if encs: out["encumbrances"] = encs
    for list_key in ["active_items", "closed_items", "unknown_items"]:
        if isinstance(data.get(list_key), list):
            items = [{"subject": i.get("SubjectAndDebtAmount") or i.get("subject"), "completion": i.get("CompletionDateOrReason"), "department": i.get("Department") or i.get("department")} for i in data.get(list_key)[:limit] if isinstance(i, dict)]
            if items: out[list_key] = items
    return out

def public_participants_summary(participants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for p in participants or []:
        meta = p.get("meta") or {}
        inn_res = strip_service_fields(p.get("inn_resolution") or {})
        inn_res.pop("final_inn", None)
        out.append({
            "label": p.get("label"),
            "role": "representative" if meta.get("representative") else (meta.get("role") or "owner"),
            "share": meta.get("share"), "is_minor": bool(meta.get("is_minor")),
            "age": meta.get("age"), "representative": bool(meta.get("representative")),
            "final_inn_used": p.get("final_inn_used") or "", "inn_resolution": inn_res,
            "skipped_due_to_minor": bool(p.get("skipped_due_to_minor")),
        })
    return out

def normalized_input(req: CheckRequest, expose_full_inn: bool = False) -> Dict[str, Any]:
    owners = owners_from_request(req); reps = unique_representatives(req, owners)
    def person_public(o: OwnerRequest, representative=False):
        dob_ru, _ = normalize_dob(o.dob); inn = normalize_inn(owner_to_check_request(o, req))
        series, number = normalize_passport(owner_to_check_request(o, req))
        return {
            "fio": " ".join(x for x in [o.last.strip(), o.first.strip(), o.middle.strip()] if x),
            "role": "representative" if representative else (o.role or "owner"),
            "share": o.share, "is_minor": is_minor_owner(o), "age": calculate_age(dob_ru),
            "inn_provided": bool(inn), "inn": inn if expose_full_inn else mask_inn(inn),
            "passport_provided": bool(series and number),
            "region": int(o.regioncode or o.region or 0) if str(o.regioncode or o.region or "").isdigit() else 0,
        }
    return {
        "owners_count": len(owners), "representatives_count": len(reps),
        "owners": [person_public(o) for o in owners],
        "representatives": [person_public(r, representative=True) for r in reps],
        "property": normalize_property(req),
    }

# -------------------- NewDB --------------------
async def newdb_post_json(client: httpx.AsyncClient, payload: dict) -> dict:
    if not NEWDB_TOKEN: return {"state": "failed", "errors_info": [{"error": "NEWDB_TOKEN не задан"}]}
    try:
        r = await client.post(NEWDB_URL, json=payload, headers={"X-API-KEY": NEWDB_TOKEN, "Content-Type": "application/json"}, timeout=35)
        try: data = r.json()
        except Exception: data = {"state": "failed", "errors_info": [{"error": r.text[:500]}]}
        data["_http_status"] = r.status_code
        return data
    except Exception as e:
        return {"state": "failed", "errors_info": [{"error": str(e)}]}

def is_newdb_error(data: dict) -> bool:
    return (not isinstance(data, dict) or data.get("errors_info") or data.get("state") in {"failed", "error"})

def has_result_status_500(data: dict) -> bool:
    t = flatten_text(data).lower()
    return "service is unavailable" in t or "parsing failed" in t or '"status": 500' in t

async def newdb_run(client, params, timeout_sec=75, poll_interval=3.0):
    if not params: return {"state": "skipped"}
    first = await newdb_post_json(client, {"params": params})
    if is_newdb_error(first) or has_result_status_500(first): return first
    request_id = first.get("requestId")
    if not request_id: return first
    if str(first.get("state") or "").lower() in {"complete", "done", "error", "failed"}: return first
    deadline = asyncio.get_event_loop().time() + timeout_sec
    last = first
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        last = await newdb_post_json(client, {"requestId": request_id})
        if is_newdb_error(last) or has_result_status_500(last): return last
        if str(last.get("state") or "").lower() in {"complete", "done", "error", "failed"}: return last
    last["state"] = "timeout"; last["error"] = f"Таймаут {timeout_sec}с"
    return last

def result_data(resp, method):
    try:
        block = (resp.get("results") or {}).get(method)
        if not block: return None, None
        result = block.get("result") or {}
        return result.get("data"), result
    except: return None, None

# -------------------- Сбор payloads --------------------
def build_payloads(req: CheckRequest):
    dob_ru, dob_iso = normalize_dob(req.dob); inn = normalize_inn(req)
    region = normalize_region(req); series, number = normalize_passport(req)
    prop = normalize_property(req); full_fio = fio(req)
    p = {}
    p["passport"] = {"seria": series, "number": number, "firstname": req.first.strip(), "lastname": req.last.strip(), "secondname": req.middle.strip(), "dob": dob_iso, "country": "ru", "method": "passport_mvd"} if req.run_passport and series and number else None
    p["passport_fns"] = {"seria": series, "number": number, "firstname": req.first.strip(), "lastname": req.last.strip(), "secondname": req.middle.strip(), "dob": dob_iso, "country": "ru", "method": "passport_fns"} if req.run_passport_fns and series and number else None
    p["fssp"] = {"firstname": req.first.strip(), "lastname": req.last.strip(), "secondname": req.middle.strip(), "dob": dob_iso, "regioncode": region, "country": "ru", "method": "fssp_person"} if req.run_fssp and req.first and req.last and dob_iso and region else None
    p["bankruptcy"] = {"innfiz": inn, "country": "ru", "method": "bankrot_person"} if req.run_bankruptcy and inn else None
    p["arbitr"] = {"innfiz": inn, "country": "ru", "method": "arbitr_person"} if req.run_arbitr and inn else None
    p["nalog_debt"] = {"innfiz": inn, "inn": inn, "country": "ru", "method": "nalog_debt"} if req.run_nalog_debt and inn else None
    p["egrul_ip"] = {"innfiz": inn, "inn": inn, "country": "ru", "method": "egrul_ip"} if req.run_egrul_ip and inn else None
    p["pravosud"] = {"method": "pravo_search", "country": "ru", "query": full_fio, "q": full_fio, "fio": full_fio, "lastname": req.last.strip(), "firstname": req.first.strip(), "secondname": req.middle.strip(), "party_name": full_fio, "limit": 50} if req.run_pravosud and full_fio else None
    p["egrn"] = {"address": prop["address"], "country": "ru", "method": "rosreestr"} if req.run_egrn and prop["address"] else None
    return p

# -------------------- ИНН из ФНС --------------------
def extract_inn_from_any(value):
    if value is None: return ""
    if isinstance(value, dict):
        for k in ["inn", "innfiz", "inn_fiz", "innfl", "ИНН", "taxpayer_inn"]:
            if k in value:
                d = digits_only(value.get(k))
                if len(d) == 12: return d
        for v in value.values():
            found = extract_inn_from_any(v)
            if found: return found
    elif isinstance(value, list):
        for row in value:
            found = extract_inn_from_any(row)
            if found: return found
    else:
        m = re.search(r"\b\d{12}\b", str(value or ""))
        if m: return m.group(0)
    return ""

def extract_passport_fns_inn(resp): return extract_inn_from_any(result_data(resp, "passport_fns")[0])

def resolve_final_inn(manual_inn, passport_fns_resp):
    manual = digits_only(manual_inn)
    if len(manual) != 12: manual = ""
    fns = extract_passport_fns_inn(passport_fns_resp)
    if manual and not fns: return {"final_inn": manual, "manual_inn_masked": mask_inn(manual), "fns_inn_masked": "", "status": "manual_used", "summary": "ИНН из ручного ввода."}
    if not manual and fns: return {"final_inn": fns, "manual_inn_masked": "", "fns_inn_masked": mask_inn(fns), "status": "fns_found", "summary": "ИНН найден по паспорту."}
    if manual and fns and manual == fns: return {"final_inn": manual, "manual_inn_masked": mask_inn(manual), "fns_inn_masked": mask_inn(fns), "status": "matched", "summary": "ИНН совпал."}
    if manual and fns and manual != fns: return {"final_inn": fns, "manual_inn_masked": mask_inn(manual), "fns_inn_masked": mask_inn(fns), "status": "mismatch_fns_used", "summary": "ИНН не совпал. Использован ИНН ФНС."}
    return {"final_inn": "", "manual_inn_masked": "", "fns_inn_masked": "", "status": "missing", "summary": "ИНН не найден."}

def with_final_inn(req, final_inn):
    data = req.model_dump(); data["inn"] = final_inn; data["seller_inn"] = data["inn_fiz"] = data["innfiz"] = data["innfl"] = ""
    return CheckRequest(**data)

# -------------------- Классификаторы --------------------
def manual_item(title, summary, url, details=None):
    return {"title": title, "source": title, "status": "manual_check", "ui_status": "manual", "summary": summary, "details": details or ["Требуется ручная проверка."], "manual_check_url": url}

def ok_item(title, summary, url, details=None, data=None):
    item = {"title": title, "source": title, "status": "ok", "ui_status": "ok", "summary": summary, "details": details or [], "manual_check_url": url}
    if data is not None: item["data"] = data
    return item

def risk_item(title, summary, url, details=None, data=None):
    item = {"title": title, "source": title, "status": "risk", "ui_status": "risk", "summary": summary, "details": details or [], "manual_check_url": url}
    if data is not None: item["data"] = data
    return item

def skipped_check_item(title, summary, url):
    return manual_item(title, summary, url, ["Проверка не выполнялась."])

def generic_error_details(resp):
    if resp.get("state") == "timeout": return ["Таймаут источника."]
    if has_result_status_500(resp): return ["Источник временно недоступен."]
    return ["Ошибка источника."]

def is_skipped_response(resp):
    return isinstance(resp, dict) and str(resp.get("state") or "").lower() == "skipped"

# -- Паспорт --
def classify_passport(resp):
    title, url = "Паспорт МВД", "https://мвд.рф/сервисы-гувм"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "passport_mvd")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    text = flatten_text(data).lower()
    if "действител" in text and "недейств" not in text: return ok_item(title, "Паспорт действителен.", url, ["Действительный"], data)
    if "недейств" in text or "invalid" in text: return risk_item(title, "Паспорт может быть недействительным.", url, [flatten_text(data)[:300]], data)
    return manual_item(title, "Неясный результат.", url)

def classify_passport_fns(resp, inn_resolution=None):
    title, url = "Паспорт / ИНН ФНС", "https://service.nalog.ru/inn.do"
    inn_resolution = inn_resolution or {}
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "passport_fns")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    found = extract_passport_fns_inn(resp)
    if found:
        status = inn_resolution.get("status")
        summary = inn_resolution.get("summary") or "ИНН найден."
        if status == "mismatch_fns_used": return risk_item(title, summary, url, ["Несовпадение ручного ИНН и ИНН ФНС."], {"inn_status": status, "inn_masked": mask_inn(found)})
        return ok_item(title, summary, url, [summary], {"inn_status": status or "fns_found", "inn_masked": mask_inn(found)})
    return manual_item(title, "ИНН не найден.", url)

def classify_fssp(resp):
    title, url = "ФССП", "https://fssp.gov.ru/iss/ip"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "fssp_person")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    if not isinstance(data, list): return manual_item(title, "Нестандартный ответ.", url)
    if not data: return ok_item(title, "Исполнительные производства не найдены.", url, [], {"all_count": 0, "active_count": 0, "actual_debt": 0})
    def item_sum(x):
        if not isinstance(x, dict): return 0.0
        t = clean_str(x.get("SubjectAndDebtAmount"))
        m = re.search(r"([\d\s]+(?:[,.]\d+)?)\s*руб", t, flags=re.IGNORECASE)
        if m:
            try: return float(m.group(1).replace(" ", "").replace(",", "."))
            except: pass
        return 0.0
    active = [x for x in data if isinstance(x, dict) and not clean_str(x.get("CompletionDateOrReason"))]
    closed = [x for x in data if isinstance(x, dict) and clean_str(x.get("CompletionDateOrReason"))]
    unknown = [x for x in data if not isinstance(x, dict)]
    active_sum = sum(item_sum(x) for x in active)
    actual_debt = active_sum + sum(item_sum(x) for x in unknown)
    stats = {"all_count": len(data), "active_count": len(active), "closed_count": len(closed), "unknown_count": len(unknown), "actual_debt": round(actual_debt, 2), "active_sum": round(active_sum, 2)}
    if active or unknown: return risk_item(title, f"Активные ИП: {rub(actual_debt)}.", url, [f"Активных: {len(active)}, сумма: {rub(actual_debt)}"], stats)
    return ok_item(title, "Только закрытые ИП.", url, [], stats)

def bankruptcy_deep_flags(data):
    text = flatten_text(data).lower()
    active_words = ["введена процедура", "реструктуризация долгов", "конкурсное производство", "наблюдение", "процедура продолжа", "дело не заверш", "назначено судебное"]
    completed_words = ["заверш", "освобожд", "прекратить производство", "завершить процедуру", "освободить гражданина", "процедура завершена"]
    has_active = any(w in text for w in active_words)
    has_completed = any(w in text for w in completed_words)
    status = "active" if has_active else ("completed" if has_completed else "unknown")
    dates = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if any(b in key for b in ["birth", "dob", "рожд", "birthday"]): continue
                if any(w in key for w in ["date", "дата", "published", "publication", "create", "update", "заверш", "прекращ", "решен", "судебн"]):
                    dt = parse_date_any(v)
                    if dt and 1990 <= dt.year <= 2030: dates.append(dt)
                if isinstance(v, (dict, list)): walk(v)
        elif isinstance(x, list):
            for row in x: walk(row)
    walk(data)
    valid_dates = sorted(set(dates))
    latest_date = valid_dates[-1] if valid_dates else None
    months_after_latest = months_between_dates(latest_date) if latest_date else None
    property_words = ["оспаривание сделки", "недействительность сделки", "имущество должника", "конкурсная масса", "торги", "реализация имущества", "положение о продаже"]
    has_property = any(w in text for w in property_words)
    details = []
    if latest_date: details.append(f"Последняя значимая дата: {latest_date.strftime('%d.%m.%Y')}")
    if months_after_latest is not None:
        if months_after_latest < 12: details.append(f"После последней публикации прошло менее 1 года ({months_after_latest} мес.)")
        elif months_after_latest < 36: details.append(f"После последней публикации прошло менее 3 лет ({months_after_latest} мес.)")
        else: details.append(f"После последней публикации прошло более 3 лет ({months_after_latest} мес.)")
    return {"status": status, "latest_date": latest_date.strftime("%d.%m.%Y") if latest_date else "", "months_after_latest": months_after_latest, "property_related_words": has_property, "details": details}

def classify_bankruptcy(resp):
    title, url = "Банкротство / Федресурс", "https://bankrot.fedresurs.ru"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "bankrot_person")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    has_records = False
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                for key in ["bankruptcy", "publications", "encumbrances"]:
                    if isinstance(row.get(key), list) and len(row.get(key)) > 0: has_records = True; break
    if not has_records: return ok_item(title, "Сведения о банкротстве не найдены.", url, [], data)
    flags = bankruptcy_deep_flags(data)
    status = flags["status"]; months = flags.get("months_after_latest"); has_property = flags.get("property_related_words"); details = flags.get("details", [])
    risk_data = {"raw": data, "bankruptcy_status": status, "latest_publication_date": flags.get("latest_date"), "months_after_latest": months, "property_related_words": has_property}
    if status == "active":
        details.append("Признаки действующей или незавершённой процедуры банкротства.")
        return risk_item(title, "Выявлены признаки действующего банкротства.", url, details, risk_data)
    if status == "completed":
        if months is not None and months < 12:
            details.append("Процедура завершена менее 1 года назад — требуется повышенная осторожность.")
            return risk_item(title, "Завершённое банкротство (менее 1 года).", url, details, risk_data)
        elif months is not None and months < 36:
            details.append("Процедура завершена менее 3 лет назад — требуется проверка.")
            return risk_item(title, "Завершённое банкротство (менее 3 лет).", url, details, risk_data)
        else:
            details.append("Процедура завершена более 3 лет назад.")
            return ok_item(title, "Банкротство завершено более 3 лет назад.", url, details, risk_data)
    if has_property:
        details.append("В публикациях есть слова, связанные с имуществом/торгами.")
        return risk_item(title, "Сведения о банкротстве с имущественными признаками.", url, details, risk_data)
    details.append("Статус процедуры автоматически не определён.")
    return manual_item(title, "Сведения о банкротстве найдены. Требуется ручная проверка.", url, details)

def court_case_match_score(case, req, source=""):
    score = 0; hard_identifier = False; reasons = []
    full_fio = fio(req).lower(); last = req.last.strip().lower(); first = req.first.strip().lower()
    dob_ru, dob_iso = normalize_dob(req.dob); inn = normalize_inn(req)
    region = str(normalize_region(req) or ""); text = flatten_text(case).lower()
    if full_fio and full_fio in text: score += 38; reasons.append("полное ФИО")
    elif last and last in text: score += 8; reasons.append("фамилия")
    if first and first in text: score += 4
    if dob_ru and (dob_ru in text or dob_iso in text): score += 50; hard_identifier = True; reasons.append("дата рождения")
    if inn and inn in text: score += 60; hard_identifier = True; reasons.append("ИНН")
    if region and region != "0":
        if any(re.search(pat, text) for pat in [rf"\b{re.escape(region)}\b", rf"регион.*{re.escape(region)}", rf"код.*{re.escape(region)}"]): score += 8; reasons.append("совпадение региона")
    role_text = flatten_text(case.get("role_text") or case.get("role") or "").lower()
    if any(w in role_text for w in ["ответчик", "должник", "заинтересован"]): score += 8; reasons.append("роль ответчика")
    score = max(0, min(100, score))
    if hard_identifier and score >= 85: level = "точное совпадение"
    elif full_fio in text and score >= 50: level = "вероятное совпадение"
    elif score >= 55: level = "вероятное совпадение"
    else: level = "слабое совпадение"
    return {"court_match_score": score, "match_level": level, "match_reasons": reasons, "source": source}

def normalize_court_case(case, req, source=""):
    match = court_case_match_score(case, req, source)
    def pick(keys):
        for k in keys:
            v = case.get(k)
            if v not in (None, "", [], {}): return clean_str(v) if isinstance(v, str) else flatten_text(v)
        return ""
    return {"case_number": pick(["case_number", "case_id", "number"]), "court": pick(["court", "court_name"]), "date": pick(["date", "case_date", "published"]), "role": pick(["role_text", "role"]), "category": pick(["category", "case_category", "type", "subject"]), "status": pick(["status", "state", "result", "decision"]), "amount": pick(["amount", "claim_amount", "sum"]), **match, "raw": case}

def extract_arbitr_cases(data):
    cases = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                if isinstance(row.get("cases"), list): cases.extend([c for c in row["cases"] if isinstance(c, dict)])
                elif row.get("case_number") or row.get("case_id"): cases.append(row)
    return cases

def extract_pravosud_cases(data):
    cases = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                if isinstance(row.get("cases"), list): cases.extend([c for c in row["cases"] if isinstance(c, dict)])
                else: cases.append(row)
    return cases

def classify_arbitr(resp, req=None):
    title, url = "Арбитражные суды", "https://kad.arbitr.ru"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "arbitr_person")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    raw = extract_arbitr_cases(data)
    if not raw: return ok_item(title, "Арбитражные дела не найдены.", url, [])
    req = req or CheckRequest()
    cases = [normalize_court_case(c, req, "arbitr") for c in raw]
    strong = [c for c in cases if c["match_level"] == "точное совпадение"]
    probable = [c for c in cases if c["match_level"] == "вероятное совпадение"]
    if strong: return risk_item(title, f"Точных совпадений: {len(strong)}.", url, [f"Точные совпадения: {len(strong)}"], cases[:30])
    if probable: return manual_item(title, f"Вероятных совпадений: {len(probable)}.", url, ["Требуется идентификация."])
    return manual_item(title, "Только слабые совпадения.", url, ["Вероятно, однофамильцы."])

def classify_pravosud(resp, req=None):
    title, url = "Суды общей юрисдикции", "https://sudrf.ru"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "pravo_search")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    raw = extract_pravosud_cases(data)
    if not raw: return ok_item(title, "Дела не найдены.", url, [])
    req = req or CheckRequest()
    cases = [normalize_court_case(c, req, "pravosud") for c in raw]
    strong = [c for c in cases if c["match_level"] == "точное совпадение"]
    probable = [c for c in cases if c["match_level"] == "вероятное совпадение"]
    if strong: return risk_item(title, f"Точных совпадений: {len(strong)}.", url, [f"Точные совпадения: {len(strong)}"], cases[:30])
    if probable: return manual_item(title, f"Вероятных совпадений: {len(probable)}.", url, ["Требуется идентификация."])
    return manual_item(title, "Только слабые совпадения.", url, ["Вероятно, однофамильцы."])

def classify_nalog_debt(resp):
    title, url = "Налоговая задолженность", "https://www.nalog.gov.ru"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "nalog_debt")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    text = flatten_text(data).lower()
    if any(w in text for w in ["отсутств", "не найден", "нет задолж", "no debt"]): return ok_item(title, "Задолженность не выявлена.", url, [], {"amount": 0})
    m = re.search(r"([\d\s]+(?:[,.]\d+)?)\s*(?:руб|₽)", text, flags=re.IGNORECASE)
    if m:
        try: amount = float(m.group(1).replace(" ", "").replace(",", ".")); return risk_item(title, f"Задолженность: {rub(amount)}.", url, [f"Сумма: {rub(amount)}"], {"amount": amount})
        except: pass
    return manual_item(title, "Неоднозначный ответ.", url, [flatten_text(data)[:300]])

def classify_egrul_ip(resp):
    title, url = "ЕГРИП / статус ИП", "https://egrul.nalog.ru"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "egrul_ip")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    text = flatten_text(data).lower()
    if any(w in text for w in ["не найден", "отсутств"]): return ok_item(title, "Статус ИП не выявлен.", url, [], data)
    if any(w in text for w in ["действующ", "зарегистрирован", "огрнип"]): return risk_item(title, "Найден действующий ИП.", url, ["Проверить предпринимательские долги."], data)
    return ok_item(title, "Статус ИП не активен.", url, [], data)

def egrn_deep_flags(obj):
    base = {"registration_ban": False, "manageable_encumbrance": False, "other_encumbrance": False, "recent_right_months": None, "recent_right_date": "", "ownership_over_3_years": False, "frequent_transitions": False, "points_hint": 0, "details": []}
    if not isinstance(obj, dict): return base
    enc = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    enc_text = flatten_text(enc).lower()
    base["registration_ban"] = any(w in enc_text for w in ["запрещ", "арест", "ограничение регистрац"])
    base["manageable_encumbrance"] = any(w in enc_text for w in ["ипотек", "залог"])
    base["other_encumbrance"] = bool(enc) and not base["registration_ban"] and not base["manageable_encumbrance"]
    if base["registration_ban"]: base["points_hint"] = 32; base["details"].append("Запрет/арест регистрации.")
    elif base["manageable_encumbrance"]: base["points_hint"] = 14; base["details"].append("Ипотека/залог.")
    elif base["other_encumbrance"]: base["points_hint"] = 12; base["details"].append("Иное обременение.")
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []
    right_dates = []
    for r in rights:
        if isinstance(r, dict):
            for dk in ["registrationDate", "dateRegistration", "regDate", "startDate"]:
                dt = parse_date_any(r.get(dk))
                if dt and 1998 <= dt.year <= 2030: right_dates.append(dt); break
    if not right_dates:
        all_dates = collect_dates_recursive(obj)
        right_dates = [d for d in all_dates if 1998 <= d.year <= 2030]
    if right_dates:
        latest = max(right_dates); months = months_between_dates(latest)
        base["recent_right_months"] = months; base["recent_right_date"] = latest.strftime("%d.%m.%Y")
        if months is not None:
            if months < 3: base["details"].append(f"Право зарегистрировано недавно: {months} мес. назад"); base["points_hint"] += 20
            elif months < 12: base["details"].append(f"Право зарегистрировано менее 1 года назад"); base["points_hint"] += 12
            elif months < 36: base["details"].append(f"Право зарегистрировано менее 3 лет назад"); base["points_hint"] += 5
            else: base["ownership_over_3_years"] = True; base["details"].append("Право зарегистрировано более 3 лет назад")
        recent_12m = [d for d in right_dates if months_between_dates(d) is not None and months_between_dates(d) < 12]
        if len(recent_12m) >= 2: base["frequent_transitions"] = True; base["details"].append(f"Частые регистрационные события: {len(recent_12m)} за 12 мес."); base["points_hint"] += 18
    base["points_hint"] = min(base["points_hint"], 45)
    return base

def collect_dates_recursive(value, skip_birth=True):
    dates = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if skip_birth and any(b in key for b in ["birth", "dob", "рожд"]): continue
                if any(w in key for w in ["date", "дата", "reg", "registr"]):
                    dt = parse_date_any(v)
                    if dt: dates.append(dt)
                if isinstance(v, (dict, list)): walk(v)
        elif isinstance(x, list):
            for row in x: walk(row)
    walk(value)
    return dates

def classify_egrn(resp):
    title, url = "ЕГРН / Росреестр", "https://rosreestr.gov.ru"
    if is_skipped_response(resp): return skipped_check_item(title, "Проверка не запускалась.", url)
    data, _ = result_data(resp, "rosreestr")
    if is_newdb_error(resp) or has_result_status_500(resp) or data is None: return manual_item(title, "Нет данных.", url, generic_error_details(resp))
    if not isinstance(data, list) or not data: return manual_item(title, "Объект не найден.", url)
    obj = data[0] if isinstance(data[0], dict) else {}
    profile = egrn_deep_flags(obj)
    details = [f"Кадастровый номер: {obj.get('cadNumber', '—')}", f"Тип: {obj.get('objType_text', '—')}", f"Площадь: {obj.get('area', '—')} кв.м"]
    details.extend(profile["details"])
    obj_with_profile = dict(obj); obj_with_profile["_egrn_risk_profile"] = profile
    if profile["registration_ban"]: return risk_item(title, "Объект найден. Есть ограничение регистрации.", url, details, obj_with_profile)
    if profile["manageable_encumbrance"] or profile["other_encumbrance"]: return risk_item(title, "Объект найден. Есть обременение.", url, details, obj_with_profile)
    return ok_item(title, "Объект найден. Ограничения не выявлены.", url, details, obj_with_profile)

# -------------------- Сборка чеклиста --------------------
def classify_all(responses, req=None):
    if "participants" not in responses:
        return [
            classify_passport(responses.get("passport") or {}),
            classify_passport_fns(responses.get("passport_fns") or {}, responses.get("inn_resolution")),
            classify_fssp(responses.get("fssp") or {}),
            classify_bankruptcy(responses.get("bankruptcy") or {}),
            classify_arbitr(responses.get("arbitr") or {}, req),
            classify_pravosud(responses.get("pravosud") or {}, req),
            classify_nalog_debt(responses.get("nalog_debt") or {}),
            classify_egrul_ip(responses.get("egrul_ip") or {}),
            classify_egrn(responses.get("egrn") or {}),
        ]
    out = []
    for p in responses["participants"]:
        person_req = p.get("req") or CheckRequest()
        label = p.get("label") or "Собственник"
        meta = p.get("meta") or {}
        def add_prefix(item):
            item = dict(item); item["title"] = f"{label} — {item.get('title','Источник')}"; item["person"] = meta; return item
        if meta.get("is_minor"):
            out.append(add_prefix(manual_item("Несовершеннолетний", "Проверки не выполнялись.", "", [
                "Сделка с участием несовершеннолетнего требует обязательного разрешения органов опеки и попечительства.",
                "Без этого разрешения сделка может быть оспорена и признана недействительной.",
                "Необходимо проверить: постановление опеки, условия встречной покупки, зачисление денег на счёт ребёнка.",
                "Рекомендуется нотариальное удостоверение сделки."
            ])))
            continue
        out.append(add_prefix(classify_passport(p.get("passport") or {})))
        out.append(add_prefix(classify_passport_fns(p.get("passport_fns") or {}, p.get("inn_resolution"))))
        out.append(add_prefix(classify_fssp(p.get("fssp") or {})))
        out.append(add_prefix(classify_bankruptcy(p.get("bankruptcy") or {})))
        out.append(add_prefix(classify_arbitr(p.get("arbitr") or {}, person_req)))
        out.append(add_prefix(classify_pravosud(p.get("pravosud") or {}, person_req)))
        out.append(add_prefix(classify_nalog_debt(p.get("nalog_debt") or {})))
        out.append(add_prefix(classify_egrul_ip(p.get("egrul_ip") or {})))
    out.append(classify_egrn(responses.get("egrn") or {}))
    return out

# -------------------- Скоринг --------------------
def risk_scoring(checklist, age=None):
    score = 0; factor_rows = []
    def add(source, pts, text, severity="attention"):
        nonlocal score; score += pts; factor_rows.append({"source": source, "points": pts, "severity": severity, "text": text})
    if age is not None:
        if age >= 75: add("Возраст", 18, "75+: ПНД обязательно", "high")
        elif age >= 70: add("Возраст", 14, "70+: ПНД обязательно", "high")
        elif age >= 60: add("Возраст", 6, "60–69: ПНД желательно", "medium")
    for item in checklist:
        title = str(item.get("title", "")); status = item.get("status")
        if status == "manual_check":
            add(title, 2 if any(w in title.lower() for w in ["арбитраж", "суд"]) else 5, "Требуется ручная проверка", "manual"); continue
        if status != "risk": continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if "ЕГРН" in title:
            profile = data.get("_egrn_risk_profile", {})
            pts = int(profile.get("points_hint", 12))
            if profile.get("registration_ban"): add("ЕГРН", min(pts,45), "Запрет/арест регистрации", "high")
            elif profile.get("manageable_encumbrance"): add("ЕГРН", min(pts,24), "Ипотека/залог", "medium")
            else: add("ЕГРН", min(pts,28), "Обременение", "medium")
        elif "ФССП" in title:
            actual = data.get("actual_debt", 0)
            if actual > 1_000_000: add("ФССП", 38, f"Активные ИП: {rub(actual)}", "high")
            elif actual > 300_000: add("ФССП", 28, f"Активные ИП: {rub(actual)}", "high")
            elif actual > 50_000: add("ФССП", 18, f"Активные ИП: {rub(actual)}", "medium")
            elif actual > 0: add("ФССП", 10, f"Активные ИП: {rub(actual)}", "medium")
            else: add("ФССП", 5, "ИП найдены", "attention")
        elif "Банкрот" in title:
            bdata = data if isinstance(data, dict) else {}
            bstatus = bdata.get("bankruptcy_status"); months = bdata.get("months_after_latest"); has_property = bdata.get("property_related_words")
            if bstatus == "active": add("Банкротство", 75, "Признаки действующей процедуры", "critical")
            elif bstatus == "completed":
                if months is not None and months < 12: add("Банкротство", 38, f"Завершено < 1 года назад ({months} мес.)", "high")
                elif months is not None and months < 36: add("Банкротство", 22, f"Завершено < 3 лет назад ({months} мес.)", "medium")
                else: add("Банкротство", 8, "Завершено > 3 лет назад", "attention")
            elif has_property: add("Банкротство", 22, "Имущественные признаки в публикациях", "medium")
            else: add("Банкротство", 22, "Сведения найдены, статус неясен", "medium")
        elif "Арбитраж" in title or "Суды общей" in title:
            cases = item.get("data") if isinstance(item.get("data"), list) else []
            strong = sum(1 for c in cases if isinstance(c, dict) and c.get("match_level") == "точное совпадение")
            if strong: add(title, 18 + strong*6, f"Точных совпадений: {strong}", "medium")
            else: add(title, 3, "Только вероятные/слабые", "manual")
        elif "Паспорт МВД" in title: add("Паспорт МВД", 40, "Риск недействительности", "critical")
        elif "Паспорт / ИНН" in title: add("Паспорт / ИНН ФНС", 12, "Несовпадение ручного и ФНС ИНН", "medium")
        elif "ИП" in title: add("ЕГРИП", 16, "Действующий ИП", "medium")
        elif "Налог" in title: add("Налоговая", 16, "Задолженность", "medium")
    score = max(0, min(100, score))
    if score >= 85: level, label = "опасная", "Опасно при самостоятельной сделке"; conclusion = "Высокий риск. Несколько критических факторов."
    elif score >= 60: level, label = "высокорискованная", "Высокий риск при самостоятельной сделке"; conclusion = "Значимые вопросы к продавцу или объекту."
    elif score >= 35: level, label = "условно рискованная", "Условно рискованно"; conclusion = "Критичный запрет не подтверждён, но есть вопросы."
    else: level, label = "допустимая", "Допустимо к рассмотрению"; conclusion = "Автоматическая проверка не показала стоп-факторов."
    return {"score": score, "max_score": 100, "level": level, "label": label, "conclusion": conclusion, "factor_rows": factor_rows}

def build_recommendations(checklist, req=None):
    recs = []
    if req:
        age = max_relevant_age(req)
        if age is not None and age >= 70: recs.append({"priority": "critical", "title": "Проверка дееспособности", "text": "Возраст 70+: справки ПНД/НД обязательны."})
        elif age is not None and age >= 60: recs.append({"priority": "medium", "title": "Проверка дееспособности", "text": "60–69: ПНД/НД желательны."})
    for item in checklist:
        if item.get("status") != "risk": continue
        title = str(item.get("title",""))
        if "ЕГРН" in title: recs.append({"priority": "high", "title": "Проверить обременения ЕГРН", "text": "Получить документ-основание и порядок снятия."})
        elif "ФССП" in title: recs.append({"priority": "high", "title": "Закрыть ИП до сделки", "text": "Прописать порядок погашения в соглашении."})
        elif "Банкрот" in title: recs.append({"priority": "critical", "title": "Анализ банкротного дела", "text": "Проверить карточку дела и полномочия продавца."})
    recs.append({"priority": "high", "title": "Защитное авансовое соглашение", "text": "Закрепить условия возврата и ответственность."})
    return recs

def build_hidden_risks(req=None):
    return [
        {"category": "обязательно", "risk": "Супруг / согласие", "why": "Проверить режим собственности.", "law": "ст. 34, 35 СК РФ"},
        {"category": "обязательно", "risk": "Зарегистрированные лица", "why": "Кто прописан и выселение.", "law": "ЖК РФ"},
        {"category": "обязательно", "risk": "Правоустанавливающие документы", "why": "Основание приобретения.", "law": "ФЗ №218-ФЗ"},
        {"category": "критично", "risk": "Несовершеннолетние / опека", "why": "Разрешение органов опеки.", "law": "ст. 37 ГК РФ"},
        {"category": "критично", "risk": "Доверенность", "why": "Проверить срок и полномочия.", "law": "ст. 185-189 ГК РФ"},
    ]

def build_advance_decision(scoring):
    score = scoring.get("score", 0)
    if score >= 85: return {"decision": "Аванс не передавать", "level": "stop", "comment": "Сначала устранить ключевые риски."}
    if score >= 60: return {"decision": "Аванс только в защищённой схеме", "level": "strict_conditions", "comment": "Документы по каждому пункту."}
    if score >= 35: return {"decision": "Сначала документы, потом деньги", "level": "caution", "comment": "Закрыть вопросы до аванса."}
    return {"decision": "Можно переходить к документам", "level": "allowed", "comment": "Стандартная проверка."}

# -------------------- DeepSeek отчёт --------------------
DEEPSEEK_SYSTEM_PROMPT = (
    "Ты — юрист по недвижимости с 15-летним опытом. Подготовь экспертное заключение для покупателя квартиры.\n"
    "ПРАВИЛА: опирайся только на данные; не нумеруй разделы; не пиши пустые разделы; не раскрывай ИНН/паспорт/дату рождения; "
    "для судов пиши 'требует идентификации'; не называй завершённое банкротство активным.\n"
    "СТРУКТУРА (строго):\nКраткий вывод\nЧто подтверждено автоматическими источниками\nЧто не подтверждено и требует ручной проверки\n"
    "Ключевые риски\nЧто проверить до аванса\nЛогика сделки\nКак передавать аванс\nИтоговое заключение\nВажно\n"
    "В разделе 'Логика сделки' дай пошаговый план с таймингом, учитывай риски."
)

def build_deepseek_user_prompt(req, checklist, scoring, recs):
    safe = [{"title": i["title"], "status": i["status"], "summary": i["summary"], "details": (i.get("details") or [])[:4]} for i in checklist]
    age = calculate_age(normalize_dob(req.dob)[0])
    prop = normalize_property(req)
    return f"Продавец: {fio(req)}, возраст {age}\nОбъект: {prop['query']}\nРезультаты:\n{json.dumps(safe, ensure_ascii=False, indent=2)}\nСкоринг: {scoring.get('score')}/100 — {scoring.get('label')}\nВывод системы: {scoring.get('conclusion')}\nРекомендации:\n{json.dumps(recs, ensure_ascii=False, indent=2)}"

def is_ai_refusal(text):
    return any(w in (text or "").lower() for w in ["не могу", "извините", "ограничен"])

def redact_sensitive_from_ai_text(text):
    if not text: return ""
    text = re.sub(r"\b\d{12}\b", "ИНН скрыт", text)
    text = re.sub(r"\b\d{4}\s?\d{6}\b", "паспорт скрыт", text)
    return text.strip()

def normalize_legal_report_format(text):
    if not text: return ""
    s = str(text)
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def build_local_legal_report(req, checklist, scoring, recs):
    score = scoring.get("score", 0)
    risks = [x for x in checklist if x.get("status") == "risk"]
    oks = [x for x in checklist if x.get("status") == "ok"]
    manual = [x for x in checklist if x.get("status") == "manual_check"]
    lines = ["Краткий вывод", f"Оценка: {scoring.get('label')} ({score}/100). {scoring.get('conclusion')}", "",
             "Что подтверждено"] + [f"• {x['title']}: {x['summary']}" for x in oks] + [""] + \
             ["Что не подтверждено"] + [f"• {x['title']}: требуется ручная проверка" for x in manual] + [""] + \
             ["Ключевые риски"] + [f"• {x['title']}: {x['summary']}" for x in risks] + [""] + \
             ["Что проверить до аванса"] + [f"• {r['title']}: {r['text']}" for r in recs[:5]] + [""] + \
             ["Итоговое заключение"]
    if score >= 85: lines.append("Сначала документы, потом аванс в защищённой схеме.")
    elif score >= 60: lines.append("Сделка требует управляемого сценария с документальным подтверждением.")
    elif score >= 35: lines.append("Умеренный риск. Закрыть вопросы до аванса.")
    else: lines.append("Можно переходить к стандартной проверке документов.")
    lines += ["", "Важно", "Отчёт — аналитический ориентир. Не заменяет ручную юридическую проверку."]
    return normalize_legal_report_format("\n".join(lines))

async def maybe_deepseek_report(req, checklist, scoring, recs):
    fallback = build_local_legal_report(req, checklist, scoring, recs)
    if not (USE_DEEPSEEK_REPORT and DEEPSEEK_API_KEY):
        logger.info("DeepSeek отключён. Использую локальный отчёт.")
        return fallback
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={"model": "deepseek-v4-pro", "messages": [
                    {"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT},
                    {"role": "user", "content": build_deepseek_user_prompt(req, checklist, scoring, recs)}],
                    "temperature": 0.3, "max_tokens": 4096})
            if resp.status_code != 200:
                logger.error(f"DeepSeek вернул {resp.status_code}: {resp.text[:300]}")
                return fallback
            data = resp.json()
            if not data.get("choices"):
                logger.error(f"DeepSeek: нет choices в ответе. Ответ: {json.dumps(data, ensure_ascii=False)[:300]}")
                return fallback
            text = data["choices"][0]["message"]["content"].strip()
            text = redact_sensitive_from_ai_text(text)
            text = normalize_legal_report_format(text)
            if not text or is_ai_refusal(text):
                logger.warning("DeepSeek: отказ или пустой ответ")
                return fallback
            logger.info("DeepSeek отчёт успешно сгенерирован")
            return text
    except Exception as e:
        logger.exception(f"Ошибка DeepSeek: {e}")
        return fallback

# -------------------- PDF --------------------
class Palette:
    DARK_BLUE = "#0A1F3F"; WHITE = "#FFFFFF"; OFF_WHITE = "#F8F7F4"; MID_GRAY = "#6E7F8D"; DARK_TEXT = "#1A1A1A"
    CRITICAL = "#C0392B"; HIGH = "#E67E22"; MEDIUM = "#D4A373"; LOW = "#2EAD63"; MANUAL = "#7F8C8D"
    CRITICAL_BG = "#FDF0ED"; HIGH_BG = "#FEF7ED"; MEDIUM_BG = "#FEF9F3"; LOW_BG = "#EDF7F1"; MANUAL_BG = "#F5F3EF"
    @staticmethod
    def for_score(score):
        if score >= 85: return Palette.CRITICAL
        if score >= 60: return Palette.HIGH
        if score >= 35: return Palette.MEDIUM
        return Palette.LOW
    @staticmethod
    def for_severity(sev):
        return {"critical": Palette.CRITICAL, "high": Palette.HIGH, "medium": Palette.MEDIUM, "attention": Palette.LOW, "manual": Palette.MANUAL}.get(sev, Palette.MID_GRAY)
    @staticmethod
    def bg_for_severity(sev):
        return {"critical": Palette.CRITICAL_BG, "high": Palette.HIGH_BG, "medium": Palette.MEDIUM_BG, "attention": Palette.LOW_BG, "manual": Palette.MANUAL_BG}.get(sev, Palette.OFF_WHITE)

def register_pdf_font():
    if not REPORTLAB_AVAILABLE: return "Helvetica"
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]:
        if os.path.exists(path):
            try: pdfmetrics.registerFont(TTFont("AppFont", path)); return "AppFont"
            except: pass
    return "Helvetica"

class ScoreGauge(Flowable):
    def __init__(self, score, width=178*mm):
        Flowable.__init__(self); self.score = max(0, min(100, int(score or 0))); self.width = width; self.height = 14*mm
    def draw(self):
        c = self.canv; w = self.width; bar_h = 4.5*mm; x0, y0 = 0, 4*mm
        c.setFillColor(colors.HexColor(Palette.OFF_WHITE)); c.roundRect(x0, y0, w, bar_h, 2.2*mm, fill=1, stroke=0)
        for i in range(80):
            ratio = i/79; col = "#2EAD63" if ratio < 0.35 else ("#E7B742" if ratio < 0.65 else "#C0392B")
            c.setFillColor(colors.HexColor(col)); c.rect(x0 + w*i/80, y0, w/80+0.3, bar_h, fill=1, stroke=0)
        fn = "AppFont" if "AppFont" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
        c.setFillColor(colors.HexColor(Palette.MID_GRAY)); c.setFont(fn, 6.5)
        c.drawString(x0, y0-3*mm, "Низкий"); c.drawCentredString(x0+w/2, y0-3*mm, "Средний"); c.drawRightString(x0+w, y0-3*mm, "Высокий")
        mx = x0 + (self.score/100)*w
        c.setFillColor(colors.HexColor(Palette.DARK_BLUE))
        p = c.beginPath(); p.moveTo(mx, y0+bar_h+4*mm); p.lineTo(mx-3*mm, y0+bar_h); p.lineTo(mx+3*mm, y0+bar_h); p.close()
        c.drawPath(p, fill=1, stroke=0)
        bw = 16*mm; bx = min(max(mx-bw/2, x0), x0+w-bw); by = y0+bar_h+4.5*mm
        c.setFillColor(colors.HexColor(Palette.DARK_BLUE)); c.roundRect(bx, by, bw, 5.8*mm, 2*mm, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(Palette.WHITE)); c.setFont(fn, 7.5); c.drawCentredString(bx+bw/2, by+1.5*mm, f"{self.score}/100")

def p(text): return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")

def build_pdf_bytes(report):
    if not REPORTLAB_AVAILABLE: return ("PDF недоступен.\n\n" + json.dumps(report, ensure_ascii=False, indent=2)).encode("utf-8")
    font = register_pdf_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm, topMargin=13*mm, bottomMargin=14*mm)
    
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Z_Title", fontName=font, fontSize=21, leading=26, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_Subtitle", fontName=font, fontSize=9, leading=12, textColor=Palette.MID_GRAY))
    styles.add(ParagraphStyle("Z_H1", fontName=font, fontSize=14, leading=18, spaceBefore=8, spaceAfter=5, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_Body", fontName=font, fontSize=9.2, leading=13.5, textColor=Palette.DARK_TEXT, spaceAfter=3))
    styles.add(ParagraphStyle("Z_CardTitle", fontName=font, fontSize=10.5, leading=13.5, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_CardText", fontName=font, fontSize=8.8, leading=12.5, textColor=Palette.DARK_TEXT))
    styles.add(ParagraphStyle("Z_ScoreNum", fontName=font, fontSize=26, leading=30, alignment=TA_CENTER, textColor=Palette.DARK_BLUE))
    styles.add(ParagraphStyle("Z_ScoreLbl", fontName=font, fontSize=9, leading=12, alignment=TA_CENTER, textColor=Palette.WHITE))
    styles.add(ParagraphStyle("Z_TableHead", fontName=font, fontSize=8, leading=10.5, textColor=Palette.WHITE))
    styles.add(ParagraphStyle("Z_TableCell", fontName=font, fontSize=8.2, leading=11, textColor=Palette.DARK_TEXT))
    
    story = []
    scoring = report.get("risk_scoring") or {}
    breakdown = report.get("scoring_breakdown") or {}
    checklist = report.get("checklist") or report.get("classified_checklist") or []
    recs = report.get("recommendations") or []
    advance = report.get("advance_decision") or {}
    legal_text = report.get("legal_report") or ""
    score = scoring.get("score", 0)
    risk_color = Palette.for_score(score)
    
    def badge_label(lbl):
        return {"Опасно при самостоятельной сделке": "ОПАСНО", "Высокий риск при самостоятельной сделке": "ВЫСОКИЙ РИСК", "Условно рискованно": "УСЛОВНЫЙ РИСК", "Допустимо к рассмотрению": "ДОПУСТИМО"}.get(lbl, lbl.upper())
    
    header_left = [
        Paragraph("Комплексная проверка<br/>продавца и объекта недвижимости", styles["Z_Title"]),
        Spacer(1, 2*mm),
        Paragraph(f"Дата: {report.get('created_at','—')}  •  ID: {report.get('report_id','—')[:8]}", styles["Z_Subtitle"])
    ]
    
    score_badge = Table([
        [Paragraph(badge_label(str(scoring.get('label',''))), styles["Z_ScoreLbl"])],
        [Paragraph(str(score), styles["Z_ScoreNum"])],
        [Paragraph("из 100", styles["Z_Subtitle"])]
    ], colWidths=[42*mm])
    
    score_badge.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor(risk_color)),
        ("BACKGROUND", (0,1), (-1,-1), colors.HexColor(Palette.OFF_WHITE)),
        ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor("#E0DDD6")),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    
    story.append(Table([[header_left, score_badge]], colWidths=[130*mm, 46*mm]))
    story.append(Spacer(1, 5*mm))
    story.append(ScoreGauge(score, width=176*mm))
    story.append(Spacer(1, 5*mm))
    
    def add_card(title, body, bg=Palette.WHITE, border="#E0DDD6", title_color=Palette.DARK_BLUE):
        content = []
        if title:
            content.append(Paragraph(p(title), styles["Z_CardTitle"]))
            content.append(Spacer(1, 2*mm))
        if isinstance(body, list):
            for line in body:
                content.append(Paragraph(p(line), styles["Z_CardText"]))
        else:
            content.append(Paragraph(p(body), styles["Z_CardText"]))
        tbl = Table([[content]], colWidths=[174*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor(bg)),
            ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor(border)),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING", (0,0), (-1,-1), 7),
            ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 3.5*mm))
    
    add_card("Главный вывод", scoring.get("conclusion","—"), bg=Palette.OFF_WHITE, border="#D8D3C8")
    add_card("Решение по авансу", f"{advance.get('decision','')}. {advance.get('comment','')}", bg=Palette.HIGH_BG if score>=35 else Palette.LOW_BG)
    
    story.append(Paragraph("1. Расшифровка рейтинга", styles["Z_H1"]))
    factors = breakdown.get("factors") or scoring.get("factor_rows") or []
    if factors:
        fh = [Paragraph("Источник", styles["Z_TableHead"]), Paragraph("Баллы", styles["Z_TableHead"]), Paragraph("Причина", styles["Z_TableHead"])]
        fd = [fh]
        for f in factors[:10]:
            sev = f.get("severity","attention")
            fd.append([Paragraph(f.get("source",""), styles["Z_TableCell"]), Paragraph(f"+{f.get('points',0)}", styles["Z_TableCell"]), Paragraph(f.get("text", f.get("reason","")), styles["Z_TableCell"])])
        ft = Table(fd, colWidths=[60*mm,24*mm,90*mm], repeatRows=1)
        ft_style = [("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#E0DDD6")),("BACKGROUND",(0,0),(-1,0),colors.HexColor(Palette.DARK_BLUE)),
                    ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]
        for idx, f in enumerate(factors[:10], start=1):
            sev = f.get("severity","attention")
            ft_style.append(("BACKGROUND",(0,idx),(2,idx),colors.HexColor(Palette.bg_for_severity(sev))))
            ft_style.append(("TEXTCOLOR",(1,idx),(1,idx),colors.HexColor(Palette.for_severity(sev))))
        ft.setStyle(TableStyle(ft_style)); story.append(ft); story.append(Spacer(1,4*mm))
    
    story.append(Paragraph("2. Карта проверок", styles["Z_H1"]))
    ch = [Paragraph("Источник", styles["Z_TableHead"]), Paragraph("Статус", styles["Z_TableHead"]), Paragraph("Вывод", styles["Z_TableHead"])]
    cd = [ch]
    for item in checklist:
        st = item.get("status","")
        cd.append([
            Paragraph(item.get("title",""), styles["Z_TableCell"]),
            Paragraph({"ok":"ОК","risk":"РИСК","manual_check":"РУЧНАЯ"}.get(st,"?"), styles["Z_TableCell"]),
            Paragraph(item.get("summary",""), styles["Z_TableCell"])
        ])
    ct = Table(cd, colWidths=[50*mm,20*mm,104*mm], repeatRows=1)
    ct_style = [("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#E0DDD6")),("BACKGROUND",(0,0),(-1,0),colors.HexColor(Palette.DARK_BLUE)),
                ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]
    for idx, item in enumerate(checklist, start=1):
        st = item.get("status","")
        ct_style.append(("BACKGROUND",(0,idx),(2,idx),colors.HexColor({"ok":Palette.LOW_BG,"risk":Palette.HIGH_BG,"manual_check":Palette.MANUAL_BG}.get(st, Palette.OFF_WHITE))))
    ct.setStyle(TableStyle(ct_style)); story.append(ct); story.append(Spacer(1,4*mm))
    
    story.append(Paragraph("3. Экспертное заключение", styles["Z_H1"]))
    for block_type, text in pdf_report_blocks(legal_text):
        if not text.strip(): continue
        if block_type == "h": story.append(Paragraph(p(text), styles["Z_CardTitle"]))
        elif block_type == "bullet": story.append(Paragraph(f"• {p(text)}", styles["Z_CardText"]))
        else: story.append(Paragraph(p(text), styles["Z_Body"]))
    add_card("Важно", "Отчёт носит информационно-аналитический характер. Не заменяет ручную юридическую проверку.", bg=Palette.OFF_WHITE)
    
    doc.build(story)
    return buf.getvalue()

def pdf_report_blocks(text):
    headings = {"Краткий вывод","Что подтверждено автоматическими источниками","Что не подтверждено и требует ручной проверки","Ключевые риски","Что проверить до аванса","Логика сделки","Как передавать аванс","Итоговое заключение","Важно"}
    blocks = []; buf = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            if buf: blocks.append(("p", " ".join(buf))); buf = []
            continue
        if stripped in headings:
            if buf: blocks.append(("p", " ".join(buf))); buf = []
            blocks.append(("h", stripped))
        elif stripped.startswith("•"):
            if buf: blocks.append(("p", " ".join(buf))); buf = []
            blocks.append(("bullet", stripped[1:].strip()))
        else: buf.append(stripped)
    if buf: blocks.append(("p", " ".join(buf)))
    return blocks

# -------------------- Pipeline --------------------
async def run_one_person_checks(client, owner, base_req, label, representative=False):
    person_req = owner_to_check_request(owner, base_req)
    manual_inn = normalize_inn(person_req)
    minor = is_minor_owner(owner)
    meta = {"label": label, "role": owner.role, "share": owner.share, "is_minor": minor, "age": calculate_age(normalize_dob(owner.dob)[0]), "representative": representative}
    resp = {"label": label, "req": person_req, "meta": meta}
    if minor:
        if owner.has_passport and base_req.run_passport: resp["passport"] = await newdb_run(client, build_payloads(person_req).get("passport"), timeout_sec=120)
        resp["skipped_due_to_minor"] = True; return resp
    payloads = build_payloads(person_req)
    first = await asyncio.gather(newdb_run(client, payloads.get("passport"), timeout_sec=120), newdb_run(client, payloads.get("passport_fns"), timeout_sec=120), return_exceptions=True)
    resp["passport"] = first[0] if not isinstance(first[0], Exception) else {"state":"failed","errors_info":[{"error":str(first[0])}]}
    resp["passport_fns"] = first[1] if not isinstance(first[1], Exception) else {"state":"failed","errors_info":[{"error":str(first[1])}]}
    inn_resolution = resolve_final_inn(manual_inn, resp.get("passport_fns") or {})
    resp["inn_resolution"] = inn_resolution
    final_inn = inn_resolution.get("final_inn") or ""
    person_with_inn = with_final_inn(person_req, final_inn)
    payloads_inn = build_payloads(person_with_inn)
    tasks = [newdb_run(client, payloads_inn.get(k), timeout_sec=120) for k,_ in [("fssp",None),("bankruptcy",None),("arbitr",None),("pravosud",None),("nalog_debt",None),("egrul_ip",None)]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for (key,_), res in zip([("fssp",None),("bankruptcy",None),("arbitr",None),("pravosud",None),("nalog_debt",None),("egrul_ip",None)], results):
        resp[key] = {"state":"failed","errors_info":[{"error":str(res)}]} if isinstance(res, Exception) else res
    resp["final_inn_used"] = mask_inn(final_inn)
    return resp

async def run_checks(req):
    owners = owners_from_request(req); reps = unique_representatives(req, owners)
    payloads = {"participants": [], "egrn": build_payloads(req).get("egrn")}
    responses = {"participants": [], "egrn": None}
    async with httpx.AsyncClient() as client:
        tasks = []
        for idx, owner in enumerate(owners, 1): tasks.append(run_one_person_checks(client, owner, req, participant_label(idx, owner)))
        for idx, rep in enumerate(reps, 1): tasks.append(run_one_person_checks(client, rep, req, participant_label(idx, rep, representative=True), representative=True))
        egrn_task = newdb_run(client, payloads["egrn"], timeout_sec=180)
        all_res = await asyncio.gather(*tasks, egrn_task, return_exceptions=True)
    responses["participants"] = [r for r in all_res[:-1] if not isinstance(r, Exception)]
    responses["egrn"] = all_res[-1] if not isinstance(all_res[-1], Exception) else {"state":"failed","errors_info":[{"error":str(all_res[-1])}]}
    if len(responses["participants"]) == 1:
        p = responses["participants"][0]
        for k in ["passport","passport_fns","fssp","bankruptcy","arbitr","pravosud","nalog_debt","egrul_ip","inn_resolution"]:
            responses[k] = p.get(k)
    return payloads, responses

async def build_full_report(req, include_debug=False):
    payloads, responses = await run_checks(req)
    checklist = classify_all(responses, req)
    age = max_relevant_age(req)
    scoring = risk_scoring(checklist, age=age)
    scoring_breakdown = {"total_score": scoring["score"], "max_score": 100, "level": scoring["level"], "label": scoring["label"], "conclusion": scoring["conclusion"], "factors": []}
    for f in scoring.get("factor_rows", []):
        sev = f.get("severity","attention")
        scoring_breakdown["factors"].append({"source": f["source"], "points": f["points"], "severity": sev, "reason": f["text"], "icon": {"critical":"🔴","high":"🟠","medium":"🟡","attention":"🔵","manual":"⚪"}.get(sev,"⚪"), "impact": {"critical":"Критический стоп-фактор","high":"Высокий риск","medium":"Умеренный риск","attention":"Низкий риск","manual":"Ручная проверка"}.get(sev,"")})
    recs = build_recommendations(checklist, req)
    legal = await maybe_deepseek_report(req, checklist, scoring, recs)
    report_id = str(uuid.uuid4())
    result = {
        "success": True, "report_id": report_id, "pdf_available": True, "pdf_url": f"/download-pdf/{report_id}",
        "created_at": now_ru(),
        "executive_summary": {"label": scoring["label"], "level": scoring["level"], "score": scoring["score"], "max_score": 100, "conclusion": scoring["conclusion"]},
        "screen_report": {"headline": scoring["label"], "score": scoring["score"], "conclusion": scoring["conclusion"]},
        "checklist": strip_service_fields(checklist),
        "risk_scoring": scoring, "scoring_breakdown": scoring_breakdown,
        "recommendations": recs, "advance_decision": build_advance_decision(scoring),
        "hidden_risks": build_hidden_risks(req),
        "legal_report": legal, "normalized_input": normalized_input(req),
        "participants": public_participants_summary(responses.get("participants") or []),
        "warnings": [], "notes": ["v4.1 – улучшенный скоринг, логирование, DeepSeek с проверкой ошибок, ЕГРН с датами"]
    }
    if include_debug:
        result["payloads"] = payloads; result["responses"] = responses
    stored = dict(result); stored["normalized_input"] = normalized_input(req, expose_full_inn=False)
    REPORTS[report_id] = stored; _REPORT_TIMESTAMPS[report_id] = time.time()
    return result

# -------------------- Endpoints --------------------
@app.on_event("startup")
async def startup():
    async def cleanup_loop():
        while True:
            await asyncio.sleep(600); cleanup_expired_reports()
    asyncio.create_task(cleanup_loop())

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION, "newdb_token": bool(NEWDB_TOKEN), "deepseek_key": bool(DEEPSEEK_API_KEY), "reportlab": REPORTLAB_AVAILABLE}

@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest, request: Request):
    verify_debug_key(request)
    try: return await build_full_report(req, include_debug=True)
    except Exception as e:
        logger.exception(f"Ошибка debug-newdb: {e}")
        return {"success": False, "error": str(e)}

@app.post("/check-report")
async def check_report(req: CheckRequest, request: Request):
    verify_widget_key(request)
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip)
    cleanup_expired_reports()
    try:
        return await build_full_report(req, include_debug=False)
    except Exception as e:
        logger.exception(f"Ошибка формирования отчёта: {e}")
        return {"success": False, "message": "Ошибка формирования отчёта."}

@app.get("/download-pdf/{report_id}")
def download_pdf(report_id: str):
    report = REPORTS.get(report_id)
    if not report:
        return StreamingResponse(
            io.BytesIO(b"Report not found or expired (12h TTL). Please run the check again."),
            media_type="text/plain"
        )
    try:
        pdf = build_pdf_bytes(report)
        filename = f"real_estate_report_{report_id[:8]}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.exception(f"Ошибка генерации PDF: {e}")
        return StreamingResponse(
            io.BytesIO(f"PDF generation error: {str(e)}".encode()),
            media_type="text/plain"
        )
