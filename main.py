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

    try:
        if "Р" in text or "С" in text:
            return text.encode("latin1").decode("utf-8")
    except Exception:
        pass

    return text


def safe_error(e: Exception) -> str:
    text = str(e)

    if NEWDB_TOKEN:
        text = text.replace(NEWDB_TOKEN, "***")

    return fix_mojibake(text)[:300]


def clean_newdb_response(data: dict) -> dict:
    if isinstance(data, dict):
        data.pop("balance", None)
    return data


def convert_dob(dob: str) -> str:
    if not dob:
        return ""

    dob = dob.strip().replace("-", ".")
    parts = dob.split(".")

    if len(parts) == 3:
        if len(parts[0]) == 4:
            return dob.replace(".", "-")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"

    return dob


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


async def newdb_post(
    params: dict,
    *,
    max_attempts: int = 25,
    sleep_seconds: int = 3,
    client_timeout: int = 120,
) -> dict:
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
            "source": "Банкротство",
            "note": "Введите ИНН для проверки банкротства.",
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
                "source": "Банкротство",
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
            "source": "Банкротство",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "details": [],
            "risk": "unknown",
            "source": "Банкротство",
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

        if not isinstance(items, list):
            items = []

        details = []

        last_l = last.lower().strip()
        first_l = first.lower().strip()
        middle_l = middle.lower().strip()

        defendant_count = 0
        plaintiff_count = 0

        for item in items[:50]:
            parties = item.get("parties", []) or []
            person_roles = []

            for p in parties:
                name = fix_mojibake(p.get("party_name", "") or "").lower()
                role = fix_mojibake(p.get("role_text", "") or "")

                if last_l in name and first_l in name and (not middle_l or middle_l in name):
                    person_roles.append(role)

                    role_l = role.lower()

                    if "ответчик" in role_l or "должник" in role_l:
                        defendant_count += 1

                    if "истец" in role_l or "заявитель" in role_l:
                        plaintiff_count += 1

            details.append({
                "case_number": fix_mojibake(
                    item.get("case_number", item.get("delo_case_number", ""))
                ),
                "category": fix_mojibake(item.get("category", item.get("case_category", ""))),
                "result": fix_mojibake(item.get("result", item.get("decision", ""))),
                "region": fix_mojibake(item.get("region", "")),
                "date": fix_mojibake(item.get("date", item.get("case_date", ""))),
                "case_url": item.get("case_url", item.get("url", "")),
                "person_roles": person_roles,
            })

        found = len(items) > 0

        if defendant_count >= 3:
            risk = "high"
        elif defendant_count >= 1 or found:
            risk = "medium"
        else:
            risk = "low"

        return {
            "checked": True,
            "found": found,
            "count": len(items),
            "as_defendant": defendant_count,
            "as_plaintiff": plaintiff_count,
            "details": details[:20],
            "risk": risk,
            "source": "Судебные дела",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "as_defendant": 0,
            "as_plaintiff": 0,
            "details": [],
            "risk": "unknown",
            "source": "Судебные дела",
            "note": f"Ошибка судебного поиска: {safe_error(e)}",
        }


async def check_passport(series, number, last, first, middle):
    if not series or not number:
        return {
            "checked": False,
            "found": False,
            "risk": "unknown",
            "source": "Паспорт",
            "verdict": "не проверялся",
            "note": "Введите серию и номер паспорта для проверки.",
        }

    try:
        params = {
            "method": "passport_mvd",
            "series": series.strip(),
            "seria": series.strip(),
            "number": number.strip(),
            "lastname": last.strip(),
            "firstname": first.strip(),
        }

        if middle:
            params["secondname"] = middle.strip()

        data = await newdb_post(params)
        raw = data.get("results", {}).get("passport_mvd", {}).get("result", {})

        if data.get("manual_check_required") or is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "risk": "unknown",
                "source": "Паспорт",
                "verdict": "требуется ручная проверка",
                "note": "МВД не дало надежный автоматический ответ. Требуется ручная проверка.",
            }

        text = fix_mojibake(str(raw))
        text_l = text.lower()

        valid_markers = [
            "действител",
            "valid",
            "не значится",
            "не найден среди недействительных",
        ]

        invalid_markers = [
            "недействител",
            "утрачен",
            "похищен",
            "разыскивается",
            "заменен",
            "заменён",
        ]

        if any(m in text_l for m in invalid_markers):
            return {
                "checked": True,
                "found": True,
                "risk": "high",
                "source": "Паспорт",
                "verdict": "паспорт может быть недействительным",
                "raw_status": text[:500],
            }

        if any(m in text_l for m in valid_markers):
            return {
                "checked": True,
                "found": False,
                "risk": "low",
                "source": "Паспорт",
                "verdict": "паспорт действителен",
                "raw_status": text[:500],
            }

        return {
            "checked": True,
            "found": False,
            "risk": "unknown",
            "source": "Паспорт",
            "verdict": "статус не определён",
            "note": "Автоматический ответ получен, но статус паспорта не удалось надежно распознать.",
            "raw_status": text[:500],
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "risk": "unknown",
            "source": "Паспорт",
            "verdict": "ошибка проверки",
            "note": f"Ошибка МВД: {safe_error(e)}",
        }


def calculate_person_score(checks: dict) -> tuple[int, str]:
    score = 0

    for check in checks.values():
        risk = check.get("risk", "unknown")

        if risk == "high":
            score += 40
        elif risk == "medium":
            score += 20
        elif risk == "unknown":
            score += 10

    score = min(score, 100)

    if score >= 60:
        level = "high"
    elif score >= 25:
        level = "medium"
    else:
        level = "low"

    return score, level


def build_summary(checks: dict) -> dict:
    values = list(checks.values())

    return {
        "checked_count": sum(1 for x in values if x.get("checked") is True),
        "high_risks": sum(1 for x in values if x.get("risk") == "high"),
        "medium_risks": sum(1 for x in values if x.get("risk") == "medium"),
        "unknown_count": sum(
            1 for x in values
            if x.get("checked") is not True or x.get("risk") == "unknown"
        ),
    }


async def check_property_rosreestr(address: str):
    try:
        query = address.strip()

        params = {
            "method": "rosreestr",
            "address": query,
        }

        data = await newdb_post(
            params,
            max_attempts=35,
            sleep_seconds=3,
            client_timeout=150,
        )

        raw = data.get("results", {}).get("rosreestr", {}).get("result", {})

        if data.get("manual_check_required") or is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "risk": "unknown",
                "level": "unknown",
                "note": "Росреестр не дал надежный автоматический ответ. Требуется ручная проверка или официальный запрос выписки ЕГРН.",
            }

        items = raw.get("data", []) or []

        if not isinstance(items, list):
            items = []

        if not items:
            return {
                "checked": True,
                "found": False,
                "risk": "unknown",
                "level": "unknown",
                "note": "Объект не найден по указанному адресу или кадастровому номеру. Проверьте ввод или выполните ручную проверку.",
            }

        obj = items[0] or {}

        rights = obj.get("rights", []) or obj.get("right", []) or []
        encumbrances = (
            obj.get("encumbrances", [])
            or obj.get("restrictions", [])
            or obj.get("encumbrance", [])
            or []
        )

        if isinstance(rights, dict):
            rights = [rights]

        if isinstance(encumbrances, dict):
            encumbrances = [encumbrances]

        clean_rights = []

        for r in rights:
            clean_rights.append({
                "type": fix_mojibake(
                    r.get("type", r.get("right_type", r.get("name", "")))
                ),
                "number": fix_mojibake(
                    r.get("number", r.get("reg_number", r.get("registration_number", "")))
                ),
                "date": fix_mojibake(
                    r.get("date", r.get("reg_date", r.get("registration_date", "")))
                ),
            })

        clean_enc = []

        for e in encumbrances:
            clean_enc.append({
                "type": fix_mojibake(
                    e.get("type", e.get("restriction_type", e.get("name", "")))
                ),
                "number": fix_mojibake(
                    e.get("number", e.get("reg_number", e.get("registration_number", "")))
                ),
                "date": fix_mojibake(
                    e.get("date", e.get("reg_date", e.get("registration_date", "")))
                ),
            })

        enc_count = len(clean_enc)

        level = "high" if enc_count > 0 else "low"
        risk = "high" if enc_count > 0 else "low"

        return {
            "checked": True,
            "found": True,
            "risk": risk,
            "level": level,
            "enc_count": enc_count,
            "cad_number": fix_mojibake(
                obj.get("cad_number", obj.get("cadnum", obj.get("cadastral_number", query)))
            ),
            "address": fix_mojibake(
                obj.get("address", obj.get("full_address", query))
            ),
            "obj_type": fix_mojibake(
                obj.get("obj_type", obj.get("object_type", obj.get("type", "—")))
            ),
            "purpose": fix_mojibake(
                obj.get("purpose", obj.get("assignation", obj.get("usage", "—")))
            ),
            "area": fix_mojibake(
                str(obj.get("area", obj.get("square", "—")))
            ),
            "floor": fix_mojibake(
                str(obj.get("floor", obj.get("level", "—")))
            ),
            "reg_date": fix_mojibake(
                obj.get("reg_date", obj.get("registration_date", "—"))
            ),
            "cad_cost": fix_mojibake(
                str(obj.get("cad_cost", obj.get("cad_price", obj.get("cadastral_value", "—"))))
            ),
            "rights": clean_rights[:20],
            "encumbrances": clean_enc[:20],
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "risk": "unknown",
            "level": "unknown",
            "note": f"Ошибка Росреестра: {safe_error(e)}",
        }


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Person & Property Check API",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "newdb_token": bool(NEWDB_TOKEN),
    }


@app.post("/check/person")
async def check_person(req: PersonRequest):
    fssp, bankrupt, courts, passport = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn),
        check_courts(req.last, req.first, req.middle),
        check_passport(
            req.passport_series,
            req.passport_number,
            req.last,
            req.first,
            req.middle,
        ),
    )

    checks = {
        "fssp": fssp,
        "bankrupt": bankrupt,
        "courts": courts,
        "passport": passport,
    }

    score, level = calculate_person_score(checks)
    summary = build_summary(checks)

    return {
        "checked": True,
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": level,
        "summary": summary,
        "checks": checks,
    }


@app.post("/check/property")
async def check_property(req: PropertyRequest):
    return await check_property_rosreestr(req.address)
