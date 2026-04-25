from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio
import uuid

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
    region: int = 0  # 0 = все регионы

async def newdb_post(params: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }
    body = {
        "params": {**params, "country": "ru"},
        "requestId": str(uuid.uuid4()),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(NEWDB_URL, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


# --- ФССП ---
async def check_fssp(last: str, first: str, middle: str, dob: str, region: int):
    try:
        # В API newdb: firstname = фамилия, lastname = имя (их специфика, см. документацию)
        params = {
            "method": "fssp_person",
            "firstname": last,    # фамилия
            "lastname": first,    # имя
        }
        if middle:
            params["secondname"] = middle
        # Регион — только если явно выбран, иначе не передаём (0 ломает запрос)
        if region and region != 0:
            params["regioncode"] = region
        if dob:
            # Конвертируем DD.MM.YYYY → YYYY-MM-DD
            parts = dob.replace("-", ".").split(".")
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    params["dob"] = dob
                else:
                    params["dob"] = f"{parts[2]}-{parts[1]}-{parts[0]}"

        print(f"[FSSP] Отправляю запрос: {params}")
        data = await newdb_post(params)
        print(f"[FSSP] Ответ: state={data.get('state')}, raw_keys={list(data.get('results',{}).keys())}")
        raw = data.get("results", {}).get("fssp_person", {}).get("result", {})
        print(f"[FSSP] Статус результата: {raw.get('status')}, записей: {len(raw.get('data', []))}")
        items = raw.get("data", [])

        total = len(items)
        amount = 0.0
        details = []

        for item in items:
            subject_raw = item.get("SubjectAndDebtAmount", "")
            # Извлекаем сумму долга
            import re
            sums = re.findall(r"Сумма долга:\s*([\d\s.]+)\s*руб", subject_raw)
            if sums:
                try:
                    amount += float(sums[0].replace(" ", "").replace(",", "."))
                except:
                    pass

            details.append({
                "debtor":      item.get("Debtor", ""),
                "proceeding":  item.get("EnforcementProceeding", ""),
                "writ":        item.get("WritDetails", ""),
                "subject":     subject_raw,
                "department":  item.get("BailiffDepartment", ""),
                "officer":     item.get("BailiffOfficer", ""),
                "phone":       item.get("Phone", ""),
                "completed":   item.get("CompletionDateOrReason", ""),
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
        return {"found": False, "count": 0, "amount": "—", "details": [], "risk": "low",
                "source_url": "https://fssp.gov.ru/iss/ip", "error": str(e)}


# --- Банкротство (требует ИНН) ---
async def check_bankrupt(inn: str, last: str, first: str):
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
                    "name":       common.get("name_or_fio", ""),
                    "inn":        common.get("inn", inn),
                    "case":       bk.get("case_number", ""),
                    "case_url":   bk.get("case_url", ""),
                    "status":     bk.get("status", ""),
                    "address":    common.get("address", ""),
                    "details_url": common.get("details_url", ""),
                    "messages":   [m.get("type","") + " — " + m.get("message_info","") for m in bk.get("messages", [])[:5]],
                })
            # Если банкротств нет, но запись есть
            if not item.get("bankruptcy") and common.get("name_or_fio"):
                details.append({
                    "name":    common.get("name_or_fio", ""),
                    "inn":     common.get("inn", inn),
                    "status":  "Процедуры банкротства не найдены",
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
        return {"found": False, "count": 0, "details": [], "risk": "low",
                "source_url": "https://fedresurs.ru/", "error": str(e)}


# --- Суды (ГАС Правосудие) ---
async def check_courts(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        params = {
            "method": "pravo_search",
            "query": fio,
            "limit": 20,
        }
        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})
        items = raw.get("data", [])
        meta = raw.get("meta", {})

        details = []
        for item in items:
            parties = item.get("parties", [])
            roles = [f"{p.get('party_name','')} ({p.get('role_text','')})" for p in parties]
            details.append({
                "case_number":  item.get("case_number", item.get("delo_case_number", "")),
                "case_info":    item.get("case_info", ""),
                "category":     item.get("category_text", ""),
                "court":        item.get("court_url", ""),
                "judge":        item.get("judge_name", ""),
                "result":       item.get("result_text", ""),
                "date":         item.get("review_date", item.get("hearing_date", "")),
                "region":       item.get("region_name", ""),
                "parties":      roles,
                "case_url":     item.get("case_url", ""),
            })

        total = meta.get("count", len(items))
        return {
            "found": total > 0,
            "count": total,
            "details": details[:10],
            "risk": "medium" if total > 0 else "low",
            "source_url": "https://sudrf.ru/",
        }
    except Exception as e:
        return {"found": False, "count": 0, "details": [], "risk": "low",
                "source_url": "https://sudrf.ru/", "error": str(e)}


# --- Нотариат (парсинг — newdb не покрывает) ---
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
        return {"found": False, "note": "Недоступно", "risk": "low",
                "source_url": "https://notariat.ru/",
                "manual_url": f"https://notariat.ru/ru-ru/help/probate-cases/search/?search={last}+{first}",
                "error": str(e)}


@app.post("/check")
async def check(req: CheckRequest):
    fssp, bankrupt, courts, notary = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn, req.last, req.first),
        check_courts(req.last, req.first, req.middle),
        check_notary(req.last, req.first, req.middle),
        return_exceptions=False,
    )

    risks = [fssp.get("risk","low"), bankrupt.get("risk","low"), courts.get("risk","low"), notary.get("risk","low")]
    score = min(sum({"high":35,"medium":15,"low":0}[r] for r in risks), 100)

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": "high" if score > 50 else "medium" if score > 20 else "low",
        "fssp": fssp,
        "bankrupt": bankrupt,
        "courts": courts,
        "notary": notary,
    }

@app.get("/")
def root():
    return {"status": "ok", "service": "Person Check API v4 (newdb.net)"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(NEWDB_URL, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


# --- ФССП ---
async def check_fssp(last: str, first: str, middle: str, dob: str, region: int):
    try:
        # Внимание: в API newdb firstname = фамилия, lastname = имя (их специфика)
        params = {
            "method": "fssp_person",
            "firstname": last,    # фамилия
            "lastname": first,    # имя
            "regioncode": region if region else 0,
        }
        if middle:
            params["secondname"] = middle
        if dob:
            # Конвертируем DD.MM.YYYY → YYYY-MM-DD
            parts = dob.replace("-", ".").split(".")
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    params["dob"] = dob
                else:
                    params["dob"] = f"{parts[2]}-{parts[1]}-{parts[0]}"

        data = await newdb_post(params)
        raw = data.get("results", {}).get("fssp_person", {}).get("result", {})
        items = raw.get("data", [])

        total = len(items)
        amount = 0.0
        details = []

        for item in items:
            subject_raw = item.get("SubjectAndDebtAmount", "")
            # Извлекаем сумму долга
            import re
            sums = re.findall(r"Сумма долга:\s*([\d\s.]+)\s*руб", subject_raw)
            if sums:
                try:
                    amount += float(sums[0].replace(" ", "").replace(",", "."))
                except:
                    pass

            details.append({
                "debtor":      item.get("Debtor", ""),
                "proceeding":  item.get("EnforcementProceeding", ""),
                "writ":        item.get("WritDetails", ""),
                "subject":     subject_raw,
                "department":  item.get("BailiffDepartment", ""),
                "officer":     item.get("BailiffOfficer", ""),
                "phone":       item.get("Phone", ""),
                "completed":   item.get("CompletionDateOrReason", ""),
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
        return {"found": False, "count": 0, "amount": "—", "details": [], "risk": "low",
                "source_url": "https://fssp.gov.ru/iss/ip", "error": str(e)}


# --- Банкротство (требует ИНН) ---
async def check_bankrupt(inn: str, last: str, first: str):
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
                    "name":       common.get("name_or_fio", ""),
                    "inn":        common.get("inn", inn),
                    "case":       bk.get("case_number", ""),
                    "case_url":   bk.get("case_url", ""),
                    "status":     bk.get("status", ""),
                    "address":    common.get("address", ""),
                    "details_url": common.get("details_url", ""),
                    "messages":   [m.get("type","") + " — " + m.get("message_info","") for m in bk.get("messages", [])[:5]],
                })
            # Если банкротств нет, но запись есть
            if not item.get("bankruptcy") and common.get("name_or_fio"):
                details.append({
                    "name":    common.get("name_or_fio", ""),
                    "inn":     common.get("inn", inn),
                    "status":  "Процедуры банкротства не найдены",
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
        return {"found": False, "count": 0, "details": [], "risk": "low",
                "source_url": "https://fedresurs.ru/", "error": str(e)}


# --- Суды (ГАС Правосудие) ---
async def check_courts(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        params = {
            "method": "pravo_search",
            "query": fio,
            "limit": 20,
        }
        data = await newdb_post(params)
        raw = data.get("results", {}).get("pravo_search", {}).get("result", {})
        items = raw.get("data", [])
        meta = raw.get("meta", {})

        details = []
        for item in items:
            parties = item.get("parties", [])
            roles = [f"{p.get('party_name','')} ({p.get('role_text','')})" for p in parties]
            details.append({
                "case_number":  item.get("case_number", item.get("delo_case_number", "")),
                "case_info":    item.get("case_info", ""),
                "category":     item.get("category_text", ""),
                "court":        item.get("court_url", ""),
                "judge":        item.get("judge_name", ""),
                "result":       item.get("result_text", ""),
                "date":         item.get("review_date", item.get("hearing_date", "")),
                "region":       item.get("region_name", ""),
                "parties":      roles,
                "case_url":     item.get("case_url", ""),
            })

        total = meta.get("count", len(items))
        return {
            "found": total > 0,
            "count": total,
            "details": details[:10],
            "risk": "medium" if total > 0 else "low",
            "source_url": "https://sudrf.ru/",
        }
    except Exception as e:
        return {"found": False, "count": 0, "details": [], "risk": "low",
                "source_url": "https://sudrf.ru/", "error": str(e)}


# --- Нотариат (парсинг — newdb не покрывает) ---
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
        return {"found": False, "note": "Недоступно", "risk": "low",
                "source_url": "https://notariat.ru/",
                "manual_url": f"https://notariat.ru/ru-ru/help/probate-cases/search/?search={last}+{first}",
                "error": str(e)}


@app.post("/check")
async def check(req: CheckRequest):
    fssp, bankrupt, courts, notary = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob, req.region),
        check_bankrupt(req.inn, req.last, req.first),
        check_courts(req.last, req.first, req.middle),
        check_notary(req.last, req.first, req.middle),
        return_exceptions=False,
    )

    risks = [fssp.get("risk","low"), bankrupt.get("risk","low"), courts.get("risk","low"), notary.get("risk","low")]
    score = min(sum({"high":35,"medium":15,"low":0}[r] for r in risks), 100)

    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": "high" if score > 50 else "medium" if score > 20 else "low",
        "fssp": fssp,
        "bankrupt": bankrupt,
        "courts": courts,
        "notary": notary,
    }

@app.get("/")
def root():
    return {"status": "ok", "service": "Person Check API v4 (newdb.net)"}
