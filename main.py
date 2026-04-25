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
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "ВСТАВЬ_СЮДА_НОВЫЙ_ТОКЕН")
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


async def newdb_post(params: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }

    request_id = str(uuid.uuid4())

    body = {
        "params": {**params, "country": "ru"},
        "requestId": request_id,
    }

    poll_body = {"requestId": request_id}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(NEWDB_URL, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()

        attempts = 0
        while data.get("state") in ("queued", "processing", "in progress") and attempts < 15:
            await asyncio.sleep(3)
            attempts += 1

            r2 = await client.post(NEWDB_URL, json=poll_body, headers=headers)
            r2.raise_for_status()
            data = r2.json()

        return data


def convert_dob(dob: str) -> str:
    if not dob:
        return ""

    parts = dob.replace("-", ".").split(".")

    if len(parts) == 3:
        if len(parts[0]) == 4:
            return dob
        return f"{parts[2]}-{parts[1]}-{parts[0]}"

    return dob


def normalize_money(value: str) -> float:
    try:
        return float(value.replace(" ", "").replace(",", "."))
    except Exception:
        return 0.0


async def check_fssp(last: str, first: str, middle: str, dob: str, region: int):
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
        items = raw.get("data", [])

        total = len(items)
        amount = 0.0
        details = []

        for item in items:
            subject_raw = item.get("SubjectAndDebtAmount", "")
            sums = re.findall(r"Сумма долга:\s*([\d\s.,]+)\s*руб", subject_raw)

            for s in sums:
                amount += normalize_money(s)

            details.append({
                "debtor": item.get("Debtor", ""),
                "proceeding": item.get("EnforcementProceeding", ""),
                "writ": item.get("WritDetails", ""),
                "subject": subject_raw,
                "department": item.get("BailiffDepartment", ""),
                "officer": item.get("BailiffOfficer", ""),
                "phone": item.get("Phone", ""),
                "completed": item.get("CompletionDateOrReason", ""),
            })

        if total == 0:
            risk = "low"
        elif total >= 3 or amount >= 300000:
            risk = "high"
        else:
            risk = "medium"

        return {
            "checked": True,
            "found": total > 0,
            "count": total,
            "amount": f"{amount:,.0f} ₽".replace(",", " "),
            "details": details,
            "risk": risk,
            "source": "ФССП",
        }

    except Exception:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "amount": "—",
            "details": [],
            "risk": "unknown",
            "source": "ФССП",
            "note": "Проверка временно недоступна",
        }


async def check_bankrupt(inn: str):
    if not inn:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "details": [],
            "risk": "unknown",
            "source": "Федресурс",
            "note": "Для проверки банкротства нужен ИНН",
        }

    try:
        params = {
            "method": "bankrot_person",
            "innfiz": inn.strip(),
        }

        data = await newdb_post(params)
        raw = data.get("results", {}).get("bankrot_person", {}).get("result", {})
        items = raw.get("data", [])

        details = []

        for item in items:
            common = item.get("common", item.get("commmon", {}))
            bankruptcies = item.get("bankruptcy", [])

            for bk in bankruptcies:
                details.append({
                    "name": common.get("name_or_fio", ""),
                    "inn": common.get("inn", inn),
                    "case": bk.get("case_number", ""),
                    "status": bk.get("status", ""),
                    "messages": [
                        f"{m.get('type', '')} — {m.get('message_info', '')}"
                        for m in bk.get("messages", [])[:5]
                    ],
                })

        found = any(d.get("case") for d in details)

        return {
            "checked": True,
            "found": found,
            "count": len(details),
            "details": details,
            "risk": "high" if found else "low",
            "source": "Федресурс",
        }

    except Exception:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "details": [],
            "risk": "unknown",
            "source": "Федресурс",
            "note": "Проверка временно недоступна",
        }


async def check_courts(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()

        params = {
            "method": "pravo_search",
            "party_name": fio,
            "limit": 50,
        }

        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})
        items = raw.get("data", [])
        meta = raw.get("meta", {})

        details = []

        last_l = last.lower().strip()
        first_l = first.lower().strip()
        middle_l = middle.lower().strip()

        for item in items:
            parties = item.get("parties", [])
            person_roles = []

            for p in parties:
                name = p.get("party_name", "").lower()
                role = p.get("role_text", "")

                fio_match = (
                    last_l in name
                    and first_l in name
                    and (not middle_l or middle_l in name)
                )

                if fio_match:
                    person_roles.append(role)

            all_parties = [
                f"{p.get('party_name', '')} ({p.get('role_text', '')})"
                for p in parties
            ]

            details.append({
                "case_number": item.get("case_number", item.get("delo_case_number", "")),
                "category": item.get("category_text", ""),
                "result": item.get("result_text", ""),
                "date": item.get("review_date", item.get("hearing_date", "")),
                "region": item.get("region_name", ""),
                "person_roles": person_roles,
                "all_parties": all_parties,
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
            "source": "Суды",
        }

    except Exception:
        return {
            "checked": False,
            "found": False,
            "count": 0,
            "as_defendant": 0,
            "as_plaintiff": 0,
            "details": [],
            "risk": "unknown",
            "source": "Суды",
            "note": "Проверка временно недоступна",
        }


async def check_passport(series: str, number: str):
    if not series or not number:
        return {
            "checked": False,
            "verdict": "Не проверялся",
            "risk": "unknown",
            "source": "МВД",
            "note": "Для проверки паспорта нужны серия и номер",
        }

    try:
        params = {
            "method": "passport_mvd",
            "series": series.replace(" ", ""),
            "number": number.replace(" ", ""),
        }

        data = await newdb_post(params)
        raw = data.get("results", {}).get("passport_mvd", {}).get("result", {})
        result_data = raw.get("data", {})

        valid = result_data.get("valid", result_data.get("is_valid"))
        note = result_data.get("message", result_data.get("description", ""))
        note_l = str(note).lower()

        if valid is True or valid == 1 or "действителен" in note_l:
            verdict = "Действителен"
            risk = "low"
        elif valid is False or valid == 0 or "недействителен" in note_l:
            verdict = "Недействителен"
            risk = "high"
        else:
            verdict = "Статус неизвестен"
            risk = "medium"

        return {
            "checked": True,
            "verdict": verdict,
            "note": note,
            "risk": risk,
            "source": "МВД",
        }

    except Exception:
        return {
            "checked": False,
            "verdict": "Не удалось проверить",
            "risk": "unknown",
            "source": "МВД",
            "note": "Проверка временно недоступна",
        }


async def check_notary(last: str, first: str, middle: str):
    fio = f"{last} {first} {middle}".strip()

    try:
        url = "https://notariat.ru/ru-ru/help/probate-cases/search/"

        headers = {
            "User-Agent": "Mozilla/5.0",
        }

        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
            r = await client.get(url, params={"search": fio})

            page_text = r.text.lower()

            found = (
                "наследственное дело" in page_text
                and "ничего не найдено" not in page_text
                and "не найдено" not in page_text
            )

            return {
                "checked": True,
                "found": found,
                "note": "Возможны наследственные дела — требуется ручная проверка" if found else "По автоматической проверке записей не найдено",
                "requires_manual_check": True,
                "risk": "medium" if found else "low",
                "source": "Нотариат",
            }

    except Exception:
        return {
            "checked": False,
            "found": False,
            "note": "Проверка нотариата временно недоступна",
            "requires_manual_check": True,
            "risk": "unknown",
            "source": "Нотариат",
        }


def calculate_score(checks: list):
    weights = {
        "high": 40,
        "medium": 20,
        "low": 0,
        "unknown": 10,
    }

    score = min(sum(weights.get(c.get("risk", "unknown"), 10) for c in checks), 100)

    critical = any(c.get("risk") == "high" for c in checks)
    unknown_count = sum(1 for c in checks if c.get("risk") == "unknown")

    if critical or score >= 60:
        level = "high"
    elif score >= 25 or unknown_count >= 2:
        level = "medium"
    else:
        level = "low"

    return score, level


@app.post("/check")
async def check(req: CheckRequest):
    fssp, bankrupt, courts, notary, passport = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn),
        check_courts(req.last, req.first, req.middle),
        check_notary(req.last, req.first, req.middle),
        check_passport(req.passport_series, req.passport_number),
    )

    checks = [fssp, bankrupt, courts, notary, passport]
    score, level = calculate_score(checks)

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": level,
        "summary": {
            "checked_count": sum(1 for c in checks if c.get("checked") is True),
            "unknown_count": sum(1 for c in checks if c.get("risk") == "unknown"),
            "high_risks": sum(1 for c in checks if c.get("risk") == "high"),
            "medium_risks": sum(1 for c in checks if c.get("risk") == "medium"),
        },
        "checks": {
            "fssp": fssp,
            "bankrupt": bankrupt,
            "courts": courts,
            "notary": notary,
            "passport": passport,
        },
    }


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Person Check API",
    }
