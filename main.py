from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio
import uuid
import re
import os

app = FastAPI(title="Person Check API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "0602fa15-d984-4f51-8215-b71edf7b2aeb")
NEWDB_URL = "https://api.newdb.net/v2"


class CheckRequest(BaseModel):
    last: str
    first: str
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 0
    passport_series: str = ""
    passport_number: str = ""


def convert_dob(dob: str) -> str:
    if not dob:
        return ""
    parts = dob.replace("-", ".").split(".")
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return dob
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return dob


async def newdb_post(params: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }
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


# --- ФССП ---
async def check_fssp(last: str, first: str, middle: str, dob: str, region: int):
    try:
        params = {
            "method": "fssp_person",
            "firstname": last.strip(),   # в newdb firstname = фамилия
            "lastname": first.strip(),   # lastname = имя
        }
        if middle:
            params["secondname"] = middle.strip()
        if region and region != 0:
            params["regioncode"] = region
        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("fssp_person", {}).get("result", {})
        items = raw.get("data", []) or []
        print(f"  [FSSP] записей={len(items)}")

        amount = 0.0
        active = []
        closed = []

        for item in items:
            subject = item.get("SubjectAndDebtAmount", "") or ""
            completed = item.get("CompletionDateOrReason", "") or ""

            sums = re.findall(r"Сумма долга:\s*([\d\s.,]+)\s*руб", subject, re.IGNORECASE)
            item_amount = 0.0
            for s in sums:
                try:
                    item_amount += float(s.replace(" ", "").replace(",", "."))
                except Exception:
                    pass

            record = {
                "debtor":     item.get("Debtor", ""),
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
                closed.append(record)
            else:
                active.append(record)
                amount += item_amount

        total = len(items)
        active_count = len(active)
        risk = "high" if active_count >= 3 or amount >= 300000 else "medium" if active_count >= 1 else "low"

        return {
            "checked": True,
            "found": total > 0,
            "count": total,
            "active_count": active_count,
            "closed_count": len(closed),
            "amount": f"{amount:,.0f} ₽".replace(",", " "),
            "active": active[:20],
            "closed": closed[:20],
            "risk": risk,
            "source": "ФССП",
        }
    except Exception as e:
        print(f"  [FSSP] Ошибка: {e}")
        return {"checked": False, "found": False, "count": 0, "active_count": 0,
                "closed_count": 0, "amount": "—", "active": [], "closed": [],
                "risk": "unknown", "source": "ФССП", "error": str(e)[:300]}


# --- Банкротство ---
async def check_bankrupt(inn: str):
    if not inn:
        return {
            "checked": False, "found": False, "count": 0, "details": [],
            "risk": "unknown", "source": "Федресурс",
            "note": "Для проверки банкротства введите ИНН физлица",
        }
    try:
        params = {"method": "bankrot_person", "innfiz": inn.strip()}
        data = await newdb_post(params)
        raw = data.get("results", {}).get("bankrot_person", {}).get("result", {})
        items = raw.get("data", []) or []
        print(f"  [BANKRUPT] записей={len(items)}")

        details = []
        for item in items:
            common = item.get("commmon", {}) or {}  # три m — так в документации newdb
            bankruptcies = item.get("bankruptcy", []) or []
            publications = item.get("publications", []) or []

            for bk in bankruptcies:
                messages = bk.get("messages", []) or []
                details.append({
                    "name":     common.get("name_or_fio", ""),
                    "inn":      common.get("inn", inn),
                    "case":     bk.get("case_number", ""),
                    "case_url": bk.get("case_url", ""),
                    "status":   bk.get("status", ""),
                    "address":  common.get("address", ""),
                    "details_url": common.get("details_url", ""),
                    "messages": [
                        {"type": m.get("type", ""), "info": m.get("message_info", ""), "url": m.get("url", "")}
                        for m in messages[:10]
                    ],
                    "publications": [
                        {"title": p.get("title", ""), "date": p.get("number_date", ""), "url": p.get("url", "")}
                        for p in publications[:5]
                    ],
                })

        found = any(d.get("case") for d in details)
        return {
            "checked": True,
            "found": found,
            "count": len([d for d in details if d.get("case")]),
            "details": details[:10],
            "risk": "high" if found else "low",
            "source": "Федресурс",
        }
    except Exception as e:
        print(f"  [BANKRUPT] Ошибка: {e}")
        return {"checked": False, "found": False, "count": 0, "details": [],
                "risk": "unknown", "source": "Федресурс", "error": str(e)[:300]}


# --- Суды ---
async def check_courts(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        params = {"method": "pravo_search", "party_name": fio, "limit": 50}
        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})
        items = raw.get("data", []) or []
        meta = raw.get("meta", {}) or {}
        print(f"  [COURTS] найдено={meta.get('count', len(items))}")

        last_l = last.lower().strip()
        first_l = first.lower().strip()
        middle_l = middle.lower().strip()

        details = []
        for item in items:
            parties = item.get("parties", []) or []
            person_roles = []
            for p in parties:
                name = (p.get("party_name", "") or "").lower()
                if last_l in name and first_l in name and (not middle_l or middle_l in name):
                    person_roles.append(p.get("role_text", ""))

            details.append({
                "case_number":  item.get("case_number", ""),
                "category":     item.get("category_text", ""),
                "result":       item.get("result_text", ""),
                "date":         item.get("review_date", item.get("hearing_date", "")),
                "region":       item.get("region_name", ""),
                "person_roles": person_roles,
                "all_parties":  [f"{p.get('party_name','')} ({p.get('role_text','')})" for p in parties][:8],
                "case_url":     item.get("case_url", ""),
            })

        total = meta.get("count", len(items))
        as_defendant = sum(1 for d in details if any("ответчик" in r.lower() for r in d.get("person_roles", [])))
        as_plaintiff  = sum(1 for d in details if any("истец" in r.lower() for r in d.get("person_roles", [])))

        return {
            "checked": True,
            "found": total > 0,
            "count": total,
            "as_defendant": as_defendant,
            "as_plaintiff": as_plaintiff,
            "details": details[:15],
            "risk": "high" if as_defendant >= 3 else "medium" if total > 0 else "low",
            "source": "Суды",
        }
    except Exception as e:
        print(f"  [COURTS] Ошибка: {e}")
        return {"checked": False, "found": False, "count": 0, "as_defendant": 0,
                "as_plaintiff": 0, "details": [], "risk": "unknown",
                "source": "Суды", "error": str(e)[:300]}


# --- Паспорт МВД ---
async def check_passport(series: str, number: str, last: str, first: str, middle: str, dob: str):
    if not series or not number:
        return {
            "checked": False, "verdict": "Не проверялся",
            "risk": "unknown", "source": "МВД",
            "note": "Для проверки паспорта введите серию и номер",
        }
    try:
        # Поле серии — "seria" (не series), нужны ФИО и дата рождения
        params = {
            "method":     "passport_mvd",
            "seria":      series.replace(" ", ""),
            "number":     number.replace(" ", ""),
            "lastname":   last.strip(),    # у паспорта lastname = фамилия (обычная логика)
            "firstname":  first.strip(),
        }
        if middle:
            params["secondname"] = middle.strip()
        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("passport_mvd", {}).get("result", {})
        items = raw.get("data", []) or []
        result_data = items[0] if items else {}
        print(f"  [PASSPORT] data={result_data}")

        status_str = (result_data.get("status", "") or "").lower()

        if "действительный" in status_str or "действителен" in status_str or "valid" in status_str:
            verdict = "Действителен"
            risk = "low"
        elif "недействительный" in status_str or "недействителен" in status_str or "invalid" in status_str:
            verdict = "Недействителен"
            risk = "high"
        elif status_str:
            verdict = result_data.get("status", "Неизвестно")
            risk = "medium"
        else:
            verdict = "Нет данных"
            risk = "medium"

        return {
            "checked": True,
            "verdict": verdict,
            "status_raw": result_data.get("status", ""),
            "risk": risk,
            "source": "МВД",
        }
    except Exception as e:
        print(f"  [PASSPORT] Ошибка: {e}")
        return {"checked": False, "verdict": "Ошибка проверки", "risk": "unknown",
                "source": "МВД", "error": str(e)[:300]}


@app.post("/check")
async def check(req: CheckRequest):
    fssp, bankrupt, courts, passport = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn),
        check_courts(req.last, req.first, req.middle),
        check_passport(req.passport_series, req.passport_number,
                       req.last, req.first, req.middle, req.dob),
    )

    checks = [fssp, bankrupt, courts, passport]
    weights = {"high": 40, "medium": 20, "low": 0, "unknown": 10}
    score = min(sum(weights.get(c.get("risk", "unknown"), 10) for c in checks), 100)
    high = any(c.get("risk") == "high" for c in checks)
    unknown = sum(1 for c in checks if c.get("risk") == "unknown")
    level = "high" if high or score >= 60 else "medium" if score >= 25 or unknown >= 2 else "low"

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": level,
        "summary": {
            "checked_count": sum(1 for c in checks if c.get("checked")),
            "high_risks":    sum(1 for c in checks if c.get("risk") == "high"),
            "medium_risks":  sum(1 for c in checks if c.get("risk") == "medium"),
            "unknown_count": unknown,
        },
        "checks": {
            "fssp":     fssp,
            "bankrupt": bankrupt,
            "courts":   courts,
            "passport": passport,
        },
    }


@app.get("/")
def root():
    return {"status": "ok", "service": "Person Check API v8"}

@app.get("/health")
def health():
    return {"status": "ok", "token_set": bool(NEWDB_TOKEN)}
