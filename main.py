from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio
import uuid
import re
import os


app = FastAPI(title="Person & Property Check API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN")
NEWDB_URL = "https://api.newdb.net/v2"


class PersonRequest(BaseModel):
    last: str
    first: str
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 0
    passport_series: str = ""
    passport_number: str = ""


class PropertyRequest(BaseModel):
    address: str


def fix_mojibake(text: str) -> str:
    if not isinstance(text, str):
        return ""

    if "Р" not in text and "С" not in text:
        return text

    try:
        return text.encode("latin1").decode("utf-8")
    except Exception:
        return text


def safe_error(e: Exception) -> str:
    text = str(e)

    if NEWDB_TOKEN:
        text = text.replace(NEWDB_TOKEN, "***")

    return fix_mojibake(text)[:300]


def clean_newdb_response(data: dict) -> dict:
    """
    Полностью убираем баланс из ответа.
    Пользователь коммерческого виджета не должен видеть баланс API.
    """
    if isinstance(data, dict):
        data.pop("balance", None)

    return data


def convert_dob(dob: str) -> str:
    if not dob:
        return ""

    parts = dob.strip().replace("-", ".").split(".")

    if len(parts) == 3:
        if len(parts[0]) == 4:
            return dob.strip()
        return f"{parts[2]}-{parts[1]}-{parts[0]}"

    return dob.strip()


def normalize_money(value: str) -> float:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return 0.0


def is_pending_state(data: dict) -> bool:
    state = str(data.get("state", "")).lower().strip()
    return state in ("queued", "processing", "in progress", "restart", "pending")


def is_final_bad_state(data: dict) -> bool:
    state = str(data.get("state", "")).lower().strip()
    return state in ("timeout", "error", "failed", "cancelled", "canceled")


def is_service_unavailable(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return True

    status = raw.get("status")
    data = raw.get("data", [])

    if status and status != 200:
        return True

    text = str(raw).lower()

    bad_markers = [
        "unavailable",
        "service is unavailable",
        "error",
        "timeout",
        "temporarily",
        "недоступ",
        "ошибка",
        "таймаут",
        "превышено время",
    ]

    return any(marker in text for marker in bad_markers)


def looks_like_real_empty_result(raw: dict) -> bool:
    """
    True только когда источник реально ответил корректно,
    но данных нет.

    Это важно для Росреестра:
    пустота после timeout / in progress не должна становиться
    ошибочным выводом «объект не найден».
    """
    if not isinstance(raw, dict):
        return False

    status = raw.get("status")
    data = raw.get("data", None)

    if status == 200 and isinstance(data, list) and len(data) == 0:
        return True

    return False


async def newdb_post(
    params: dict,
    *,
    max_attempts: int = 25,
    sleep_seconds: int = 3,
    client_timeout: int = 120,
) -> dict:
    """
    Универсальный запрос к NewDB.

    Ключ НЕ хранится в коде.
    Он берется только из переменной окружения NEWDB_TOKEN.
    """
    if not NEWDB_TOKEN:
        raise Exception("NEWDB_TOKEN не задан в Environment Variables")

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }

    request_id = str(uuid.uuid4())

    body = {
        "params": {
            **params,
            "country": "ru",
        },
        "requestId": request_id,
    }

    poll_body = {
        "requestId": request_id,
    }

    async with httpx.AsyncClient(timeout=client_timeout) as client:
        response = await client.post(NEWDB_URL, json=body, headers=headers)
        response.raise_for_status()

        data = clean_newdb_response(response.json())
        attempts = 0

        while is_pending_state(data) and attempts < max_attempts:
            await asyncio.sleep(sleep_seconds)
            attempts += 1

            poll_response = await client.post(NEWDB_URL, json=poll_body, headers=headers)
            poll_response.raise_for_status()

            data = clean_newdb_response(poll_response.json())

        if is_pending_state(data):
            data["manual_check_required"] = True
            data["manual_reason"] = "Источник не успел ответить за отведённое время"

        if is_final_bad_state(data):
            data["manual_check_required"] = True
            data["manual_reason"] = f"Источник вернул состояние: {data.get('state')}"

        return clean_newdb_response(data)


async def check_fssp(last, first, middle, dob, region):
    try:
        params = {
            "method": "fssp_person",
            "firstname": first.strip(),
            "lastname": last.strip(),
        }

        if middle:
            params["secondname"] = middle.strip()

        if region and region != 0:
            params["regioncode"] = region

        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("fssp_person", {}).get("result", {})

        if data.get("manual_check_required") or is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "count": 0,
                "active_count": 0,
                "closed_count": 0,
                "amount": "—",
                "active": [],
                "closed": [],
                "risk": "unknown",
                "source": "ФССП",
                "note": "ФССП не дал надежный автоматический ответ. Требуется ручная проверка.",
            }

        items = raw.get("data", []) or []

        if not isinstance(items, list):
            items = []

        amount = 0.0
        active = []
        closed = []

        for item in items:
            subject = fix_mojibake(item.get("SubjectAndDebtAmount", "") or "")
            completed = fix_mojibake(item.get("CompletionDateOrReason", "") or "")

            sums = re.findall(
                r"Сумма долга:\s*([\d\s.,]+)\s*руб",
                subject,
                flags=re.IGNORECASE,
            )

            item_amount = sum(normalize_money(s) for s in sums)

            rec = {
                "debtor": fix_mojibake(item.get("Debtor", "")),
                "proceeding": fix_mojibake(item.get("EnforcementProceeding", "")),
                "writ": fix_mojibake(item.get("WritDetails", "")),
                "subject": subject,
                "department": fix_mojibake(item.get("BailiffDepartment", "")),
                "officer": fix_mojibake(item.get("BailiffOfficer", "")),
                "phone": item.get("Phone", ""),
                "completed": completed,
                "amount": item_amount,
                "is_active": not bool(completed),
            }

            if completed:
                closed.append(rec)
            else:
                active.append(rec)
                amount += item_amount

        total = len(items)

        if len(active) >= 3 or amount >= 300000:
            risk = "high"
        elif len(active) >= 1:
            risk = "medium"
        else:
            risk = "low"

        return {
            "checked": True,
            "found": total > 0,
            "count": total,
            "active_count": len(active),
            "closed_count": len(closed),
            "amount": f"{amount:,.0f} ₽".replace(",", " "),
            "active": active[:20],
            "closed": closed[:20],
            "risk": risk,
            "source": "ФССП",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "active_count": 0,
            "closed_count": 0,
            "amount": "—",
            "active": [],
            "closed": [],
            "risk": "unknown",
            "source": "ФССП",
            "note": f"Ошибка ФССП: {safe_error(e)}",
        }


async def check_bankrupt(inn):
    if not inn:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "details": [],
            "risk": "unknown",
            "source": "Банкротство (ЕФРСБ)",
            "note": "Введите ИНН для проверки банкротства",
        }

    try:
        params = {
            "method": "bankrot_person",
            "innfiz": inn.strip(),
        }

        data = await newdb_post(params)
        raw = data.get("results", {}).get("bankrot_person", {}).get("result", {})

        if data.get("manual_check_required") or is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "count": 0,
                "details": [],
                "risk": "unknown",
                "source": "Банкротство (ЕФРСБ)",
                "note": "ЕФРСБ не дал надежный автоматический ответ. Требуется ручная проверка.",
                "manual_url": f"https://fedresurs.ru/persons?q={inn.strip()}",
            }

        items = raw.get("data", []) or []

        if not isinstance(items, list):
            items = []

        details = []

        for item in items:
            common = item.get("common", item.get("commmon", {})) or {}
            bankruptcies = item.get("bankruptcy", []) or []
            publications = item.get("publications", []) or []

            if isinstance(bankruptcies, dict):
                bankruptcies = [bankruptcies]

            for bk in bankruptcies:
                messages = bk.get("messages", []) or []

                details.append({
                    "name": fix_mojibake(common.get("name_or_fio", "")),
                    "inn": common.get("inn", inn.strip()),
                    "case": fix_mojibake(bk.get("case_number", "")),
                    "case_url": bk.get("case_url", ""),
                    "status": fix_mojibake(bk.get("status", "")),
                    "address": fix_mojibake(common.get("address", "")),
                    "details_url": common.get("details_url", ""),
                    "messages": [
                        {
                            "type": fix_mojibake(m.get("type", "")),
                            "info": fix_mojibake(m.get("message_info", "")),
                            "url": m.get("url", ""),
                        }
                        for m in messages[:10]
                    ],
                    "publications": [
                        {
                            "title": fix_mojibake(p.get("title", "")),
                            "date": fix_mojibake(p.get("number_date", "")),
                            "url": p.get("url", ""),
                        }
                        for p in publications[:5]
                    ],
                })

        found = any(
            d.get("case") or d.get("status") or d.get("messages") or d.get("publications")
            for d in details
        )

        return {
            "checked": True,
            "found": found,
            "count": len(details),
            "details": details[:10],
            "risk": "high" if found else "low",
            "source": "Банкротство (ЕФРСБ)",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "details": [],
            "risk": "unknown",
            "source": "Банкротство (ЕФРСБ)",
            "note": f"Ошибка ЕФРСБ: {safe_error(e)}",
        }


async def check_courts(last, first, middle):
    try:
        fio = f"{last} {first} {middle}".strip()

        params = {
            "method": "pravo_search",
            "party_name": fio,
            "limit": 50,
        }

        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})

        if data.get("manual_check_required") or is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "count": 0,
                "as_defendant": 0,
                "as_plaintiff": 0,
                "details": [],
                "risk": "unknown",
                "source": "Судебные дела",
                "note": "Судебный реестр не дал надежный автоматический ответ. Требуется ручная проверка.",
            }

        items = raw.get("data", []) or []
        meta = raw.get("meta", {}) or {}

        if not isinstance(items, list):
            items = []

        details = []

        last_l = last.lower().strip()
        first_l = first.lower().strip()
        middle_l = middle.lower().strip()

        for item in items:
            parties = item.get("parties", []) or []
            person_roles = []

            for p in parties:
                name = fix_mojibake(p.get("party_name", "") or "").lower()
                role = fix_mojibake(p.get("role_text", "") or "")

                if last_l in name and first_l in name and (not middle_l or middle_l in name):
                    person_roles.append(role)

            details.append({
                "case_number": fix_mojibake(item.get("case_number", item.get("delo_case_number", ""))),
                "category": fix_mojibake(item.get("
