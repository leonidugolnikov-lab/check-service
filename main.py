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

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "0602fa15-d984-4f51-8215-b71edf7b2aeb")
NEWDB_URL = "https://api.newdb.net/v2"


# ─── Модели ───────────────────────────────────────────────────────────────────

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
    address: str   # кадастровый номер или адрес


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def convert_dob(dob: str) -> str:
    if not dob:
        return ""
    parts = dob.replace("-", ".").split(".")
    if len(parts) == 3:
        return dob if len(parts[0]) == 4 else f"{parts[2]}-{parts[1]}-{parts[0]}"
    return dob


async def newdb_post(params: dict) -> dict:
    headers = {"Content-Type": "application/json", "X-API-KEY": NEWDB_TOKEN}
    request_id = str(uuid.uuid4())
    body = {"params": {**params, "country": "ru"}, "requestId": request_id}
    poll_body = {"requestId": request_id}

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(NEWDB_URL, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        print(f"  [{params.get('method')}] state={data.get('state')}")

        attempts = 0
        while data.get("state") in ("queued", "processing", "in progress") and attempts < 20:
            await asyncio.sleep(3)
            attempts += 1
            r2 = await client.post(NEWDB_URL, json=poll_body, headers=headers)
            r2.raise_for_status()
            data = r2.json()
            print(f"  [{params.get('method')}] попытка {attempts}: state={data.get('state')}")

        return data


def is_service_unavailable(raw: dict) -> bool:
    """Проверяет что реестр вернул ошибку 500 / unavailable"""
    status = raw.get("status")
    data = raw.get("data", [])
    if status == 500:
        return True
    if data and isinstance(data[0], dict):
        v = str(data[0]).lower()
        if "unavailable" in v or "error" in v:
            return True
    return False


# ─── Проверки продавца ────────────────────────────────────────────────────────

async def check_fssp(last, first, middle, dob, region):
    try:
        params = {"method": "fssp_person", "firstname": last.strip(), "lastname": first.strip()}
        if middle: params["secondname"] = middle.strip()
        if region and region != 0: params["regioncode"] = region
        if dob: params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("fssp_person", {}).get("result", {})
        items = raw.get("data", []) or []
        print(f"  [FSSP] записей={len(items)}")

        amount = 0.0
        active, closed = [], []
        for item in items:
            subject = item.get("SubjectAndDebtAmount", "") or ""
            completed = item.get("CompletionDateOrReason", "") or ""
            sums = re.findall(r"Сумма долга:\s*([\d\s.,]+)\s*руб", subject, re.IGNORECASE)
            item_amount = sum(
                float(s.replace(" ", "").replace(",", ".")) for s in sums
                if s.replace(" ", "").replace(",", ".").replace(".", "").isdigit() or True
            )
            try:
                item_amount = sum(float(s.replace(" ", "").replace(",", ".")) for s in sums)
            except Exception:
                item_amount = 0.0

            rec = {
                "proceeding": item.get("EnforcementProceeding", ""),
                "writ":       item.get("WritDetails", ""),
                "subject":    subject,
                "department": item.get("BailiffDepartment", ""),
                "officer":    item.get("BailiffOfficer", ""),
                "phone":      item.get("Phone", ""),
                "completed":  completed,
                "amount":     item_amount,
                "is_active":  not bool(completed),
            }
            if completed:
                closed.append(rec)
            else:
                active.append(rec)
                amount += item_amount

        total = len(items)
        risk = "high" if len(active) >= 3 or amount >= 300000 else "medium" if len(active) >= 1 else "low"
        return {
            "checked": True, "found": total > 0, "count": total,
            "active_count": len(active), "closed_count": len(closed),
            "amount": f"{amount:,.0f} ₽".replace(",", " "),
            "active": active[:20], "closed": closed[:20],
            "risk": risk, "source": "ФССП",
        }
    except Exception as e:
        print(f"  [FSSP] Ошибка: {e}")
        return {"checked": False, "found": False, "count": 0, "active_count": 0,
                "closed_count": 0, "amount": "—", "active": [], "closed": [],
                "risk": "unknown", "source": "ФССП", "error": str(e)[:300]}


async def check_bankrupt(inn):
    if not inn:
        return {"checked": False, "found": False, "count": 0, "details": [],
                "risk": "unknown", "source": "Банкротство (ЕФРСБ)",
                "note": "Введите ИНН для проверки банкротства"}
    try:
        params = {"method": "bankrot_person", "innfiz": inn.strip()}
        data = await newdb_post(params)
        raw = data.get("results", {}).get("bankrot_person", {}).get("result", {})

        if is_service_unavailable(raw):
            return {"checked": False, "found": False, "count": 0, "details": [],
                    "risk": "unknown", "source": "Банкротство (ЕФРСБ)",
                    "note": "ЕФРСБ временно недоступен — проверьте вручную",
                    "manual_url": f"https://fedresurs.ru/persons?q={inn}"}

        items = raw.get("data", []) or []
        print(f"  [BANKRUPT] записей={len(items)}")

        details = []
        for item in items:
            common = item.get("commmon", {}) or {}
            for bk in (item.get("bankruptcy", []) or []):
                msgs = bk.get("messages", []) or []
                pubs = item.get("publications", []) or []
                details.append({
                    "name":     common.get("name_or_fio", ""),
                    "inn":      common.get("inn", inn),
                    "case":     bk.get("case_number", ""),
                    "case_url": bk.get("case_url", ""),
                    "status":   bk.get("status", ""),
                    "address":  common.get("address", ""),
                    "details_url": common.get("details_url", ""),
                    "messages": [{"type": m.get("type",""), "info": m.get("message_info",""), "url": m.get("url","")} for m in msgs[:10]],
                    "publications": [{"title": p.get("title",""), "date": p.get("number_date",""), "url": p.get("url","")} for p in pubs[:5]],
                })

        found = any(d.get("case") for d in details)
        return {"checked": True, "found": found,
                "count": len([d for d in details if d.get("case")]),
                "details": details[:10], "risk": "high" if found else "low",
                "source": "Банкротство (ЕФРСБ)"}
    except Exception as e:
        print(f"  [BANKRUPT] Ошибка: {e}")
        return {"checked": False, "found": False, "count": 0, "details": [],
                "risk": "unknown", "source": "Банкротство (ЕФРСБ)", "error": str(e)[:300]}


async def check_courts(last, first, middle):
    try:
        fio = f"{last} {first} {middle}".strip()
        params = {"method": "pravo_search", "party_name": fio, "limit": 50}
        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})
        items = raw.get("data", []) or []
        meta = raw.get("meta", {}) or {}
        print(f"  [COURTS] найдено={meta.get('count', len(items))}")

        last_l, first_l, mid_l = last.lower(), first.lower(), middle.lower()
        details = []
        for item in items:
            parties = item.get("parties", []) or []
            person_roles = []
            for p in parties:
                name = (p.get("party_name","") or "").lower()
                if last_l in name and first_l in name and (not mid_l or mid_l in name):
                    person_roles.append(p.get("role_text",""))
            details.append({
                "case_number":  item.get("case_number",""),
                "category":     item.get("category_text",""),
                "result":       item.get("result_text",""),
                "date":         item.get("review_date", item.get("hearing_date","")),
                "region":       item.get("region_name",""),
                "person_roles": person_roles,
                "all_parties":  [f"{p.get('party_name','')} ({p.get('role_text','')})" for p in parties][:8],
                "case_url":     item.get("case_url",""),
            })

        total = meta.get("count", len(items))
        as_def = sum(1 for d in details if any("ответчик" in r.lower() for r in d.get("person_roles",[])))
        as_pl  = sum(1 for d in details if any("истец" in r.lower() for r in d.get("person_roles",[])))
        return {"checked": True, "found": total > 0, "count": total,
                "as_defendant": as_def, "as_plaintiff": as_pl,
                "details": details[:15],
                "risk": "high" if as_def >= 3 else "medium" if total > 0 else "low",
                "source": "Судебные дела"}
    except Exception as e:
        print(f"  [COURTS] Ошибка: {e}")
        return {"checked": False, "found": False, "count": 0, "as_defendant": 0,
                "as_plaintiff": 0, "details": [], "risk": "unknown",
                "source": "Судебные дела", "error": str(e)[:300]}


async def check_passport(series, number, last, first, middle, dob):
    if not series or not number:
        return {"checked": False, "verdict": "Не проверялся", "risk": "unknown",
                "source": "Паспорт МВД", "note": "Введите серию и номер паспорта"}
    try:
        params = {
            "method": "passport_mvd",
            "seria": series.replace(" ",""),
            "number": number.replace(" ",""),
            "lastname":  last.strip(),
            "firstname": first.strip(),
        }
        if middle: params["secondname"] = middle.strip()
        if dob: params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("passport_mvd", {}).get("result", {})
        items = raw.get("data", []) or []
        result_data = items[0] if items else {}
        status_str = (result_data.get("status","") or "").lower()
        print(f"  [PASSPORT] status={status_str}")

        if "действительный" in status_str or "действителен" in status_str:
            verdict, risk = "Действителен", "low"
        elif "недействительный" in status_str or "недействителен" in status_str:
            verdict, risk = "Недействителен", "high"
        elif status_str:
            verdict, risk = result_data.get("status","Неизвестно"), "medium"
        else:
            verdict, risk = "Нет данных от МВД", "medium"

        return {"checked": True, "verdict": verdict,
                "status_raw": result_data.get("status",""),
                "risk": risk, "source": "Паспорт МВД"}
    except Exception as e:
        print(f"  [PASSPORT] Ошибка: {e}")
        return {"checked": False, "verdict": "Ошибка проверки", "risk": "unknown",
                "source": "Паспорт МВД", "error": str(e)[:300]}


async def check_pledge_person(last, first, middle, dob):
    """Залоги физлица: ФНП + Федресурс"""
    try:
        # pledge_person: firstname=имя, lastname=фамилия (стандартная логика)
        params = {
            "method": "pledge_person",
            "firstname": first.strip(),
            "lastname":  last.strip(),
        }
        if middle: params["secondname"] = middle.strip()
        if dob: params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("pledge_person", {}).get("result", {})

        if is_service_unavailable(raw):
            return {"checked": False, "found": False, "fnp": [], "fedresurs": [],
                    "risk": "unknown", "source": "Залоги (ФНП + Федресурс)",
                    "note": "Сервис временно недоступен"}

        items = raw.get("data", []) or []
        print(f"  [PLEDGE] записей={len(items)}")

        fnp_all, fed_all = [], []
        for item in items:
            for f in (item.get("fnp", []) or []):
                fnp_all.append({
                    "type":     f.get("message_type",""),
                    "pledgor":  f.get("pledgor",""),
                    "pledgee":  f.get("pledgee",""),
                    "date":     f.get("message_number_and_date",""),
                    "subject":  f.get("pledge_subject_ids_raw",""),
                    "url":      f.get("fnp_url",""),
                })
            for f in (item.get("fedresurs", []) or []):
                fed_all.append({
                    "type":    f.get("message_type",""),
                    "date":    f.get("message_number_and_date",""),
                    "lessee":  f.get("lessee",""),
                    "lessor":  f.get("lessor",""),
                    "snippet": f.get("found_in_message","")[:200],
                    "url":     f.get("message_url",""),
                })

        found = bool(fnp_all or fed_all)
        return {
            "checked": True, "found": found,
            "fnp_count": len(fnp_all), "fed_count": len(fed_all),
            "fnp": fnp_all[:10], "fedresurs": fed_all[:10],
            "risk": "high" if found else "low",
            "source": "Залоги (ФНП + Федресурс)",
        }
    except Exception as e:
        print(f"  [PLEDGE] Ошибка: {e}")
        return {"checked": False, "found": False, "fnp": [], "fedresurs": [],
                "risk": "unknown", "source": "Залоги (ФНП + Федресурс)", "error": str(e)[:300]}


async def check_terrorist(last, first, middle, dob):
    """Проверка по перечням террористов и экстремистов"""
    try:
        params = {
            "method":     "terrorist",
            "lastname":   last.strip(),
            "firstname":  first.strip(),
        }
        if middle: params["secondname"] = middle.strip()
        if dob: params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("terrorist", {}).get("result", {})
        items = raw.get("data", []) or []
        print(f"  [TERRORIST] записей={len(items)}")

        suggestions = []
        for item in items:
            suggestions.extend(item.get("suggestions", []))

        found = bool(suggestions)
        return {
            "checked": True, "found": found,
            "suggestions": suggestions[:10],
            "risk": "high" if found else "low",
            "source": "Терроризм / ОМУ",
        }
    except Exception as e:
        print(f"  [TERRORIST] Ошибка: {e}")
        return {"checked": False, "found": False, "suggestions": [],
                "risk": "unknown", "source": "Терроризм / ОМУ", "error": str(e)[:300]}


# ─── Проверки недвижимости ────────────────────────────────────────────────────

async def check_rosreestr(address: str):
    """ЕГРН: права и обременения по кадастровому номеру или адресу"""
    try:
        params = {"method": "rosreestr", "address": address.strip()}
        data = await newdb_post(params)
        raw = data.get("results", {}).get("rosreestr", {}).get("result", {})

        if is_service_unavailable(raw):
            return {"checked": False, "found": False, "objects": [],
                    "risk": "unknown", "source": "Росреестр (ЕГРН)",
                    "note": "Росреестр временно недоступен"}

        items = raw.get("data", []) or []
        print(f"  [ROSREESTR] объектов={len(items)}")

        objects = []
        for obj in items:
            encumbrances = obj.get("encumbrances", []) or []
            rights = obj.get("rights", []) or []
            objects.append({
                "cad_number":   obj.get("cadNumber",""),
                "address":      (obj.get("address",{}) or {}).get("readableAddress","") or address,
                "area":         obj.get("area",""),
                "obj_type":     obj.get("objType_text",""),
                "purpose":      obj.get("purpose_text",""),
                "status":       obj.get("status",""),
                "reg_date":     obj.get("regDate",""),
                "cad_cost":     obj.get("cadCost",""),
                "floor":        obj.get("levelFloor",""),
                "rights": [{"type": r.get("rightTypeDesc",""), "date": r.get("rightRegDate",""),
                            "number": r.get("rightNumber",""), "shared": r.get("sharedOwnershipType",False)} for r in rights],
                "encumbrances": [{"type": e.get("typeDesc",""), "date": e.get("startDate",""),
                                  "number": e.get("encumbranceNumber","")} for e in encumbrances],
                "has_encumbrances": len(encumbrances) > 0,
            })

        enc_total = sum(len(o["encumbrances"]) for o in objects)
        risk = "high" if enc_total > 0 else "low"
        return {"checked": True, "found": bool(objects), "objects": objects,
                "encumbrances_total": enc_total, "risk": risk, "source": "Росреестр (ЕГРН)"}
    except Exception as e:
        print(f"  [ROSREESTR] Ошибка: {e}")
        return {"checked": False, "found": False, "objects": [],
                "risk": "unknown", "source": "Росреестр (ЕГРН)", "error": str(e)[:300]}


# ─── Эндпоинты ────────────────────────────────────────────────────────────────

@app.post("/check/person")
async def check_person(req: PersonRequest):
    results = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn),
        check_courts(req.last, req.first, req.middle),
        check_passport(req.passport_series, req.passport_number, req.last, req.first, req.middle, req.dob),
        check_pledge_person(req.last, req.first, req.middle, req.dob),
        check_terrorist(req.last, req.first, req.middle, req.dob),
    )
    fssp, bankrupt, courts, passport, pledge, terrorist = results
    checks = [fssp, bankrupt, courts, passport, pledge, terrorist]

    weights = {"high": 40, "medium": 20, "low": 0, "unknown": 10}
    score = min(sum(weights.get(c.get("risk","unknown"), 10) for c in checks), 100)
    high = any(c.get("risk") == "high" for c in checks)
    unknown = sum(1 for c in checks if c.get("risk") == "unknown")
    level = "high" if high or score >= 60 else "medium" if score >= 25 or unknown >= 2 else "low"

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score, "level": level,
        "summary": {
            "checked_count": sum(1 for c in checks if c.get("checked")),
            "high_risks":    sum(1 for c in checks if c.get("risk") == "high"),
            "medium_risks":  sum(1 for c in checks if c.get("risk") == "medium"),
            "unknown_count": unknown,
        },
        "checks": {
            "fssp": fssp, "bankrupt": bankrupt, "courts": courts,
            "passport": passport, "pledge": pledge, "terrorist": terrorist,
        },
    }


@app.post("/check/property")
async def check_property(req: PropertyRequest):
    rosreestr = await check_rosreestr(req.address)
    risk = rosreestr.get("risk","unknown")
    return {
        "address": req.address,
        "score": 70 if risk == "high" else 0,
        "level": risk,
        "checks": {"rosreestr": rosreestr},
    }


# Обратная совместимость со старым /check
@app.post("/check")
async def check_legacy(req: PersonRequest):
    return await check_person(req)


@app.get("/")
def root():
    return {"status": "ok", "service": "Person & Property Check API v9"}

@app.get("/health")
def health():
    return {"status": "ok", "token_set": bool(NEWDB_TOKEN)}

@app.get("/debug/bankrupt/{inn}")
async def debug_bankrupt(inn: str):
    params = {"method": "bankrot_person", "innfiz": inn.strip()}
    return await newdb_post(params)

@app.get("/debug/rosreestr")
async def debug_rosreestr(address: str):
    params = {"method": "rosreestr", "address": address}
    return await newdb_post(params)
