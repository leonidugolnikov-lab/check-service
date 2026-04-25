from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio
import uuid
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

NEWDB_TOKEN = "0602fa15-d984-4f51-8215-b71edf7b2aeb"
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
    """Отправляет запрос и ждёт пока state станет complete."""
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
        print(f"  [{params.get('method')}] Первый ответ: state={data.get('state')}")

        attempts = 0
        while data.get("state") in ("queued", "processing", "in progress") and attempts < 15:
            await asyncio.sleep(3)
            attempts += 1
            r2 = await client.post(NEWDB_URL, json=poll_body, headers=headers)
            r2.raise_for_status()
            data = r2.json()
            print(f"  [{params.get('method')}] Попытка {attempts}: state={data.get('state')}")

        return data


def convert_dob(dob: str) -> str:
    """DD.MM.YYYY → YYYY-MM-DD"""
    parts = dob.replace("-", ".").split(".")
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return dob
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return dob


# --- ФССП ---
async def check_fssp(last: str, first: str, middle: str, dob: str, region: int):
    try:
        params = {
            "method": "fssp_person",
            "firstname": last,
            "lastname": first,
        }
        if middle:
            params["secondname"] = middle
        if region and region != 0:
            params["regioncode"] = region
        if dob:
            params["dob"] = convert_dob(dob)

        data = await newdb_post(params)
        raw = data.get("results", {}).get("fssp_person", {}).get("result", {})
        items = raw.get("data", [])
        print(f"  [FSSP] Финал: state={data.get('state')}, записей={len(items)}")

        total = len(items)
        amount = 0.0
        details = []

        for item in items:
            subject_raw = item.get("SubjectAndDebtAmount", "")
            sums = re.findall(r"Сумма долга:\s*([\d\s.]+)\s*руб", subject_raw)
            if sums:
                try:
                    amount += float(sums[0].replace(" ", "").replace(",", "."))
                except Exception:
                    pass
            details.append({
                "debtor":     item.get("Debtor", ""),
                "proceeding": item.get("EnforcementProceeding", ""),
                "writ":       item.get("WritDetails", ""),
                "subject":    subject_raw,
                "department": item.get("BailiffDepartment", ""),
                "officer":    item.get("BailiffOfficer", ""),
                "phone":      item.get("Phone", ""),
                "completed":  item.get("CompletionDateOrReason", ""),
            })

        return {
            "found": total > 0,
            "count": total,
            "amount": f"{amount:,.0f} ₽" if amount > 0 else "0 ₽",
            "details": details,
            "risk": "high" if total >= 3 else "medium" if total >= 1 else "low",
            "source_url": "https://fssp.gov.ru/iss/ip",
        }
    except Exception as e:
        print(f"  [FSSP] Ошибка: {e}")
        return {"found": False, "count": 0, "amount": "—", "details": [], "risk": "low",
                "source_url": "https://fssp.gov.ru/iss/ip", "error": str(e)}


# --- Банкротство ---
async def check_bankrupt(inn: str):
    if not inn:
        return {
            "found": False, "count": 0, "details": [], "risk": "low",
            "source_url": "https://fedresurs.ru/",
            "no_inn": True,
            "note": "Для проверки банкротства введите ИНН",
        }
    try:
        params = {"method": "bankrot_person", "innfiz": inn}
        data = await newdb_post(params)
        raw = data.get("results", {}).get("bankrot_person", {}).get("result", {})
        items = raw.get("data", [])

        details = []
        for item in items:
            common = item.get("commmon", {})
            for bk in item.get("bankruptcy", []):
                details.append({
                    "name":        common.get("name_or_fio", ""),
                    "inn":         common.get("inn", inn),
                    "case":        bk.get("case_number", ""),
                    "case_url":    bk.get("case_url", ""),
                    "status":      bk.get("status", ""),
                    "details_url": common.get("details_url", ""),
                    "messages":    [m.get("type", "") + " — " + m.get("message_info", "") for m in bk.get("messages", [])[:5]],
                })
            if not item.get("bankruptcy") and common.get("name_or_fio"):
                details.append({
                    "name":        common.get("name_or_fio", ""),
                    "inn":         common.get("inn", inn),
                    "status":      "Процедуры банкротства не найдены",
                    "details_url": common.get("details_url", ""),
                })

        found = any(d.get("case") for d in details)
        return {
            "found": found,
            "count": len([d for d in details if d.get("case")]),
            "details": details,
            "risk": "high" if found else "low",
            "source_url": "https://fedresurs.ru/",
        }
    except Exception as e:
        print(f"  [BANKRUPT] Ошибка: {e}")
        return {"found": False, "count": 0, "details": [], "risk": "low",
                "source_url": "https://fedresurs.ru/", "error": str(e)}


# --- Суды (улучшенный поиск) ---
async def check_courts(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()

        # Ищем дела где человек — участник (истец или ответчик)
        params = {
            "method": "pravo_search",
            "party_name": fio,   # фильтр именно по участнику, не по тексту
            "limit": 50,
        }
        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})
        items = raw.get("data", [])
        meta = raw.get("meta", {})
        print(f"  [COURTS] Финал: state={data.get('state')}, найдено={meta.get('count', len(items))}")

        details = []
        for item in items:
            parties = item.get("parties", [])

            # Определяем роль проверяемого человека в этом деле
            person_roles = []
            for p in parties:
                name = p.get("party_name", "").lower()
                if last.lower() in name and first.lower() in name:
                    person_roles.append(p.get("role_text", ""))

            all_parties = [f"{p.get('party_name', '')} ({p.get('role_text', '')})" for p in parties]

            details.append({
                "case_number":   item.get("case_number", item.get("delo_case_number", "")),
                "category":      item.get("category_text", ""),
                "judge":         item.get("judge_name", ""),
                "result":        item.get("result_text", ""),
                "date":          item.get("review_date", item.get("hearing_date", "")),
                "region":        item.get("region_name", ""),
                "person_roles":  person_roles,   # роль проверяемого
                "all_parties":   all_parties,
                "case_url":      item.get("case_url", ""),
            })

        total = meta.get("count", len(items))

        # Считаем роли для сводки
        as_defendant = sum(1 for d in details if any("ОТВЕТЧИК" in r for r in d.get("person_roles", [])))
        as_plaintiff  = sum(1 for d in details if any("ИСТЕЦ" in r for r in d.get("person_roles", [])))

        return {
            "found": total > 0,
            "count": total,
            "as_defendant": as_defendant,
            "as_plaintiff": as_plaintiff,
            "details": details[:15],
            "risk": "high" if as_defendant >= 3 else "medium" if total > 0 else "low",
            "source_url": "https://sudrf.ru/",
        }
    except Exception as e:
        print(f"  [COURTS] Ошибка: {e}")
        return {"found": False, "count": 0, "as_defendant": 0, "as_plaintiff": 0,
                "details": [], "risk": "low", "source_url": "https://sudrf.ru/", "error": str(e)}


# --- Проверка паспорта (МВД) ---
async def check_passport(series: str, number: str):
    if not series or not number:
        return {
            "checked": False,
            "note": "Для проверки паспорта введите серию и номер",
            "risk": "low",
            "source_url": "https://мвд.рф/",
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
        status = raw.get("status")
        print(f"  [PASSPORT] Финал: state={data.get('state')}, status={status}")

        # Интерпретируем результат
        valid = result_data.get("valid", result_data.get("is_valid"))
        note = result_data.get("message", result_data.get("description", ""))

        if valid is True or valid == 1 or "действителен" in str(note).lower():
            verdict = "Действителен"
            risk = "low"
        elif valid is False or valid == 0 or "недействителен" in str(note).lower():
            verdict = "Недействителен"
            risk = "high"
        else:
            verdict = "Статус неизвестен"
            risk = "low"

        return {
            "checked": True,
            "series": series,
            "number": number,
            "verdict": verdict,
            "note": note,
            "risk": risk,
            "raw": result_data,
            "source_url": "https://мвд.рф/",
        }
    except Exception as e:
        print(f"  [PASSPORT] Ошибка: {e}")
        return {
            "checked": False,
            "note": "Ошибка при проверке паспорта",
            "risk": "low",
            "source_url": "https://мвд.рф/",
            "error": str(e),
        }


# --- Нотариат ---
async def check_notary(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        url = "https://notariat.ru/ru-ru/help/probate-cases/search/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
            r = await client.get(url, params={"search": fio})
            found = "наследственное дело" in r.text.lower()
            return {
                "found": found,
                "note": "Найдены наследственные дела" if found else "Записей не найдено",
                "risk": "medium" if found else "low",
                "source_url": "https://notariat.ru/",
                "manual_url": f"https://notariat.ru/ru-ru/help/probate-cases/search/?search={fio}",
            }
    except Exception as e:
        return {
            "found": False, "note": "Недоступно", "risk": "low",
            "source_url": "https://notariat.ru/",
            "manual_url": f"https://notariat.ru/ru-ru/help/probate-cases/search/?search={last}+{first}",
            "error": str(e),
        }


@app.post("/check")
async def check(req: CheckRequest):
    fssp, bankrupt, courts, notary, passport = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn),
        check_courts(req.last, req.first, req.middle),
        check_notary(req.last, req.first, req.middle),
        check_passport(req.passport_series, req.passport_number),
    )

    risks = [
        fssp.get("risk", "low"),
        bankrupt.get("risk", "low"),
        courts.get("risk", "low"),
        notary.get("risk", "low"),
        passport.get("risk", "low"),
    ]
    score = min(sum({"high": 35, "medium": 15, "low": 0}[r] for r in risks), 100)

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": "high" if score > 50 else "medium" if score > 20 else "low",
        "fssp": fssp,
        "bankrupt": bankrupt,
        "courts": courts,
        "notary": notary,
        "passport": passport,
    }


@app.get("/")
def root():
    return {"status": "ok", "service": "Person Check API v7"}
