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


def safe_error(e: Exception) -> str:
    text = str(e)
    if NEWDB_TOKEN:
        text = text.replace(NEWDB_TOKEN, "***")
    return text[:300]


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
        return float(value.replace(" ", "").replace(",", "."))
    except Exception:
        return 0.0


def is_service_unavailable(raw: dict) -> bool:
    status = raw.get("status")
    data = raw.get("data", [])

    if status and status != 200:
        return True

    if isinstance(data, list) and data:
        text = str(data[0]).lower()
        if "unavailable" in text or "error" in text or "service is unavailable" in text:
            return True

    return False


async def newdb_post(params: dict) -> dict:
    if not NEWDB_TOKEN:
        raise Exception("NEWDB_TOKEN не задан в Render Environment Variables")

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

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(NEWDB_URL, json=body, headers=headers)
        response.raise_for_status()

        data = response.json()
        attempts = 0

        while data.get("state") in ("queued", "processing", "in progress", "restart") and attempts < 25:
            await asyncio.sleep(3)
            attempts += 1

            poll_response = await client.post(NEWDB_URL, json=poll_body, headers=headers)
            poll_response.raise_for_status()

            data = poll_response.json()

        return data


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

        if is_service_unavailable(raw):
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
                "note": "ФССП через API временно недоступен. Отсутствие результата не означает отсутствие исполнительных производств.",
            }

        items = raw.get("data", []) or []
        if not isinstance(items, list):
            items = []

        amount = 0.0
        active = []
        closed = []

        for item in items:
            subject = item.get("SubjectAndDebtAmount", "") or ""
            completed = item.get("CompletionDateOrReason", "") or ""

            sums = re.findall(
                r"Сумма долга:\s*([\d\s.,]+)\s*руб",
                subject,
                flags=re.IGNORECASE,
            )

            item_amount = sum(normalize_money(s) for s in sums)

            rec = {
                "debtor": item.get("Debtor", ""),
                "proceeding": item.get("EnforcementProceeding", ""),
                "writ": item.get("WritDetails", ""),
                "subject": subject,
                "department": item.get("BailiffDepartment", ""),
                "officer": item.get("BailiffOfficer", ""),
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

        if is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "count": 0,
                "details": [],
                "risk": "unknown",
                "source": "Банкротство (ЕФРСБ)",
                "note": "ЕФРСБ через API временно недоступен. Отсутствие результата не означает отсутствие банкротства.",
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
                    "name": common.get("name_or_fio", ""),
                    "inn": common.get("inn", inn.strip()),
                    "case": bk.get("case_number", ""),
                    "case_url": bk.get("case_url", ""),
                    "status": bk.get("status", ""),
                    "address": common.get("address", ""),
                    "details_url": common.get("details_url", ""),
                    "messages": [
                        {
                            "type": m.get("type", ""),
                            "info": m.get("message_info", ""),
                            "url": m.get("url", ""),
                        }
                        for m in messages[:10]
                    ],
                    "publications": [
                        {
                            "title": p.get("title", ""),
                            "date": p.get("number_date", ""),
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

        if is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "count": 0,
                "as_defendant": 0,
                "as_plaintiff": 0,
                "details": [],
                "risk": "unknown",
                "source": "Судебные дела",
                "note": "Судебный реестр через API временно недоступен.",
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
                name = (p.get("party_name", "") or "").lower()
                role = p.get("role_text", "") or ""

                if last_l in name and first_l in name and (not middle_l or middle_l in name):
                    person_roles.append(role)

            details.append({
                "case_number": item.get("case_number", item.get("delo_case_number", "")),
                "category": item.get("category_text", ""),
                "result": item.get("result_text", ""),
                "date": item.get("review_date", item.get("hearing_date", "")),
                "region": item.get("region_name", ""),
                "person_roles": person_roles,
                "all_parties": [
                    f"{p.get('party_name', '')} ({p.get('role_text', '')})"
                    for p in parties
                ][:8],
                "case_url": item.get("case_url", ""),
            })

        total = meta.get("count", len(items))

        as_defendant = sum(
            1 for d in details
            if any("ответчик" in r.lower() for r in d.get("person_roles", []))
        )

        as_plaintiff = sum(
            1 for d in details
            if any("истец" in r.lower() for r in d.get("person_roles", []))
        )

        if as_defendant >= 3:
            risk = "high"
        elif total > 0:
            risk = "medium"
        else:
            risk = "low"

        return {
            "checked": True,
            "found": total > 0,
            "count": total,
            "as_defendant": as_defendant,
            "as_plaintiff": as_plaintiff,
            "details": details[:15],
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
            "note": f"Ошибка судов: {safe_error(e)}",
        }


async def check_passport(series, number, last, first, middle, dob):
    if not series or not number:
        return {
            "checked": False,
            "verdict": "Не проверялся",
            "risk": "unknown",
            "source": "Паспорт МВД",
            "note": "Введите серию и номер паспорта",
        }

    try:
        params = {
            "method": "passport_mvd",
            "series": series.replace(" ", ""),
            "seria": series.replace(" ", ""),
            "number": number.replace(" ", ""),
            "lastname": last.strip(),
            "firstname": first.strip(),
        }

        if middle:
            params["secondname"] = middle.strip()

        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)

        raw = data.get("results", {}).get("passport_mvd", {}).get("result", {})

        if is_service_unavailable(raw):
            return {
                "checked": False,
                "verdict": "Не проверялся",
                "risk": "unknown",
                "source": "Паспорт МВД",
                "note": "МВД через API временно недоступен. Требуется ручная проверка.",
            }

        result_data = raw.get("data", {})

        if isinstance(result_data, list):
            result_data = result_data[0] if result_data else {}

        if not isinstance(result_data, dict):
            result_data = {}

        valid = result_data.get("valid", result_data.get("is_valid"))
        status_raw = (
            result_data.get("status")
            or result_data.get("message")
            or result_data.get("description")
            or ""
        )

        status_l = str(status_raw).lower()

        if valid is True or valid == 1 or "действител" in status_l and "недейств" not in status_l:
            verdict = "Действителен"
            risk = "low"
        elif valid is False or valid == 0 or "недейств" in status_l:
            verdict = "Недействителен"
            risk = "high"
        elif status_raw:
            verdict = str(status_raw)
            risk = "medium"
        else:
            verdict = "Статус неизвестен"
            risk = "medium"

        return {
            "checked": True,
            "verdict": verdict,
            "status_raw": status_raw,
            "risk": risk,
            "source": "Паспорт МВД",
        }

    except Exception as e:
        return {
            "checked": False,
            "verdict": "Ошибка проверки",
            "risk": "unknown",
            "source": "Паспорт МВД",
            "note": f"Ошибка МВД: {safe_error(e)}",
        }


async def check_pledge_person(last, first, middle, dob):
    try:
        params = {
            "method": "pledge_person",
            "firstname": first.strip(),
            "lastname": last.strip(),
        }

        if middle:
            params["secondname"] = middle.strip()

        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)

        raw = data.get("results", {}).get("pledge_person", {}).get("result", {})

        if is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "fnp": [],
                "fedresurs": [],
                "risk": "unknown",
                "source": "Залоги (ФНП + Федресурс)",
                "note": "Реестр залогов через API временно недоступен.",
            }

        items = raw.get("data", []) or []
        if not isinstance(items, list):
            items = []

        fnp_all = []
        fed_all = []

        for item in items:
            for f in (item.get("fnp", []) or []):
                fnp_all.append({
                    "type": f.get("message_type", ""),
                    "pledgor": f.get("pledgor", ""),
                    "pledgee": f.get("pledgee", ""),
                    "date": f.get("message_number_and_date", ""),
                    "subject": f.get("pledge_subject_ids_raw", ""),
                    "url": f.get("fnp_url", ""),
                })

            for f in (item.get("fedresurs", []) or []):
                fed_all.append({
                    "type": f.get("message_type", ""),
                    "date": f.get("message_number_and_date", ""),
                    "lessee": f.get("lessee", ""),
                    "lessor": f.get("lessor", ""),
                    "subject": f.get("found_in_message", "")[:250],
                    "url": f.get("message_url", ""),
                })

        found = bool(fnp_all or fed_all)

        return {
            "checked": True,
            "found": found,
            "count": len(fnp_all) + len(fed_all),
            "fnp_count": len(fnp_all),
            "fed_count": len(fed_all),
            "fnp": fnp_all[:10],
            "fedresurs": fed_all[:10],
            "risk": "high" if found else "low",
            "source": "Залоги (ФНП + Федресурс)",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "fnp": [],
            "fedresurs": [],
            "risk": "unknown",
            "source": "Залоги (ФНП + Федресурс)",
            "note": f"Ошибка залогов: {safe_error(e)}",
        }


async def check_terrorist(last, first, middle, dob):
    try:
        params = {
            "method": "terrorist",
            "lastname": last.strip(),
            "firstname": first.strip(),
        }

        if middle:
            params["secondname"] = middle.strip()

        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)

        raw = data.get("results", {}).get("terrorist", {}).get("result", {})

        if is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "suggestions": [],
                "risk": "unknown",
                "source": "Терроризм / ОМУ",
                "note": "Реестр через API временно недоступен.",
            }

        items = raw.get("data", []) or []
        if not isinstance(items, list):
            items = []

        suggestions = []

        for item in items:
            if isinstance(item, dict):
                suggestions.extend(item.get("suggestions", []) or [])

        found = bool(suggestions)

        return {
            "checked": True,
            "found": found,
            "count": len(suggestions),
            "suggestions": suggestions[:10],
            "risk": "high" if found else "low",
            "source": "Терроризм / ОМУ",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "suggestions": [],
            "risk": "unknown",
            "source": "Терроризм / ОМУ",
            "note": f"Ошибка проверки: {safe_error(e)}",
        }


async def check_rosreestr(address: str):
    try:
        params = {
            "method": "rosreestr",
            "address": address.strip(),
        }

        data = await newdb_post(params)

        raw = data.get("results", {}).get("rosreestr", {}).get("result", {})

        if is_service_unavailable(raw):
            return {
                "checked": False,
                "found": False,
                "objects": [],
                "risk": "unknown",
                "source": "Росреестр (ЕГРН)",
                "note": "Росреестр через API временно недоступен.",
            }

        items = raw.get("data", []) or []
        if not isinstance(items, list):
            items = []

        objects = []

        for obj in items:
            encumbrances = obj.get("encumbrances", []) or []
            rights = obj.get("rights", []) or []

            address_obj = obj.get("address", {}) or {}
            readable_address = address_obj.get("readableAddress", "") if isinstance(address_obj, dict) else ""

            objects.append({
                "cad_number": obj.get("cadNumber", ""),
                "address": readable_address or address.strip(),
                "area": obj.get("area", ""),
                "obj_type": obj.get("objType_text", ""),
                "purpose": obj.get("purpose_text", ""),
                "status": obj.get("status", ""),
                "reg_date": obj.get("regDate", ""),
                "cad_cost": obj.get("cadCost", ""),
                "floor": obj.get("levelFloor", ""),
                "rights": [
                    {
                        "type": r.get("rightTypeDesc", ""),
                        "date": r.get("rightRegDate", ""),
                        "number": r.get("rightNumber", ""),
                        "shared": r.get("sharedOwnershipType", False),
                    }
                    for r in rights
                ],
                "encumbrances": [
                    {
                        "type": e.get("typeDesc", ""),
                        "date": e.get("startDate", ""),
                        "number": e.get("encumbranceNumber", ""),
                    }
                    for e in encumbrances
                ],
                "has_encumbrances": len(encumbrances) > 0,
            })

        enc_total = sum(len(o.get("encumbrances", [])) for o in objects)

        return {
            "checked": True,
            "found": bool(objects),
            "objects": objects,
            "encumbrances_total": enc_total,
            "risk": "high" if enc_total > 0 else "low",
            "source": "Росреестр (ЕГРН)",
        }

    except Exception as e:
        return {
            "checked": False,
            "found": False,
            "objects": [],
            "risk": "unknown",
            "source": "Росреестр (ЕГРН)",
            "note": f"Ошибка Росреестра: {safe_error(e)}",
        }


@app.post("/check/person")
async def check_person(req: PersonRequest):
    fssp, bankrupt, courts, passport, pledges, terrorist = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn),
        check_courts(req.last, req.first, req.middle),
        check_passport(req.passport_series, req.passport_number, req.last, req.first, req.middle, req.dob),
        check_pledge_person(req.last, req.first, req.middle, req.dob),
        check_terrorist(req.last, req.first, req.middle, req.dob),
    )

    checks = [fssp, bankrupt, courts, passport, pledges, terrorist]

    weights = {
        "high": 40,
        "medium": 20,
        "low": 0,
        "unknown": 10,
    }

    score = min(sum(weights.get(c.get("risk", "unknown"), 10) for c in checks), 100)

    high_risks = sum(1 for c in checks if c.get("risk") == "high")
    medium_risks = sum(1 for c in checks if c.get("risk") == "medium")
    unknown_count = sum(1 for c in checks if c.get("risk") == "unknown")

    if high_risks > 0 or score >= 60:
        level = "high"
    elif score >= 25 or unknown_count >= 2:
        level = "medium"
    else:
        level = "low"

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": level,
        "summary": {
            "checked": sum(1 for c in checks if c.get("checked") is True),
            "checked_count": sum(1 for c in checks if c.get("checked") is True),
            "high": high_risks,
            "high_risks": high_risks,
            "medium": medium_risks,
            "medium_risks": medium_risks,
            "unknown": unknown_count,
            "unknown_count": unknown_count,
        },
        "checks": {
            "fssp": fssp,
            "bankrupt": bankrupt,
            "courts": courts,
            "passport": passport,
            "pledges": pledges,
            "terrorist": terrorist,
        },
    }


@app.post("/check/property")
async def check_property(req: PropertyRequest):
    rosreestr = await check_rosreestr(req.address)

    objects = rosreestr.get("objects", [])

    if not rosreestr.get("checked"):
        return {
            "checked": False,
            "found": False,
            "risk": "unknown",
            "level": "unknown",
            "score": 10,
            "source": rosreestr.get("source", "Росреестр"),
            "note": rosreestr.get("note", "Росреестр временно недоступен"),
        }

    if not objects:
        return {
            "checked": True,
            "found": False,
            "risk": "low",
            "level": "low",
            "score": 0,
            "source": "Росреестр (ЕГРН)",
            "note": "Объект не найден по указанному адресу или кадастровому номеру",
        }

    obj = objects[0]
    enc_count = len(obj.get("encumbrances", []))

    return {
        "checked": True,
        "found": True,
        "risk": "high" if enc_count > 0 else "low",
        "level": "high" if enc_count > 0 else "low",
        "score": 70 if enc_count > 0 else 0,
        "source": "Росреестр (ЕГРН)",
        "cad_number": obj.get("cad_number", ""),
        "address": obj.get("address", req.address),
        "area": obj.get("area", ""),
        "obj_type": obj.get("obj_type", ""),
        "purpose": obj.get("purpose", ""),
        "status": obj.get("status", ""),
        "reg_date": obj.get("reg_date", ""),
        "cad_cost": obj.get("cad_cost", ""),
        "floor": obj.get("floor", ""),
        "rights": obj.get("rights", []),
        "encumbrances": obj.get("encumbrances", []),
        "enc_count": enc_count,
        "objects": objects,
    }


@app.post("/check")
async def check_legacy(req: PersonRequest):
    return await check_person(req)


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Person & Property Check API",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "token_set": bool(NEWDB_TOKEN),
        "newdb_token_set": bool(NEWDB_TOKEN),
    }


@app.get("/debug/bankrupt/{inn}")
async def debug_bankrupt(inn: str):
    params = {
        "method": "bankrot_person",
        "innfiz": inn.strip(),
    }
    data = await newdb_post(params)

    if "balance" in data:
        data["balance"] = "***"

    return data
