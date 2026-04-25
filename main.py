from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class CheckRequest(BaseModel):
    last: str
    first: str
    middle: str = ""
    dob: str = ""

# --- ФССП ---
async def check_fssp(last: str, first: str, middle: str, dob: str):
    url = "https://api-ip.fssp.gov.ru/api/v1.0/debt/search"
    params = {
        "fio[0][n]": last,
        "fio[0][f]": first,
        "fio[0][o]": middle,
        "iss": "1",
        "region": "0",
        "token": "none",
    }
    if dob:
        params["fio[0][b]"] = dob

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params)
            data = r.json()
            items = data.get("result", {}).get("items", [])
            total = len(items)
            amount = 0.0
            details = []
            for item in items[:10]:
                for debt in item.get("exe_production", []):
                    try:
                        amount += float(str(debt.get("sum", "0")).replace(",", ".").replace(" ", ""))
                    except:
                        pass
                    details.append({
                        "number": debt.get("ip_id", ""),
                        "subject": debt.get("subject", ""),
                        "sum": debt.get("sum", ""),
                        "department": debt.get("kladr_name", ""),
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
        return {"found": False, "count": 0, "amount": "—", "details": [], "risk": "low", "error": str(e)}


# --- ЕФРСБ (банкротства) ---
async def check_bankrupt(last: str, first: str, middle: str):
    url = "https://bankrot.fedresurs.ru/api/bankrupts"
    params = {
        "searchString": f"{last} {first} {middle}".strip(),
        "pageSize": 10,
        "page": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params)
            data = r.json()
            items = data.get("data", [])
            total = len(items)
            details = []
            for item in items[:5]:
                details.append({
                    "name": item.get("name", ""),
                    "status": item.get("status", ""),
                    "date": item.get("publishedDate", "")[:10] if item.get("publishedDate") else "",
                    "region": item.get("region", ""),
                })
            return {
                "found": total > 0,
                "count": total,
                "details": details,
                "risk": "high" if total > 0 else "low",
                "source_url": "https://fedresurs.ru/",
            }
    except Exception as e:
        return {"found": False, "count": 0, "details": [], "risk": "low", "error": str(e)}


# --- Судебные производства (ГАС Правосудие) ---
async def check_courts(last: str, first: str, middle: str):
    url = "https://sudrf.ru/index.php"
    params = {
        "id": "300",
        "act": "go_search",
        "searchtype": "all",
        "name": f"{last} {first} {middle}".strip(),
        "f": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=headers)
            text = r.text
            count = text.count("class=\"results\"") + text.count("sud-name")
            found = count > 0 or "результат" in text.lower()
            return {
                "found": found,
                "count": count if found else 0,
                "note": "Найдены совпадения в ГАС Правосудие" if found else "Совпадений не найдено",
                "risk": "medium" if found else "low",
                "source_url": "https://sudrf.ru/",
            }
    except Exception as e:
        return {"found": False, "count": 0, "note": "Ошибка запроса", "risk": "low", "error": str(e)}


# --- Нотариальная палата ---
async def check_notary(last: str, first: str, middle: str):
    url = "https://notariat.ru/ru-ru/help/probate-cases/search/"
    params = {
        "search": f"{last} {first} {middle}".strip(),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=headers)
            text = r.text
            found = "наследственное дело" in text.lower() or "нотариус" in text.lower() and "результат" in text.lower()
            return {
                "found": found,
                "note": "Найдены наследственные дела" if found else "Записей не найдено",
                "risk": "medium" if found else "low",
                "source_url": "https://notariat.ru/",
            }
    except Exception as e:
        return {"found": False, "note": "Ошибка запроса", "risk": "low", "error": str(e)}


@app.post("/check")
async def check(req: CheckRequest):
    results = await asyncio.gather(
        check_fssp(req.last, req.first, req.middle, req.dob),
        check_bankrupt(req.last, req.first, req.middle),
        check_courts(req.last, req.first, req.middle),
        check_notary(req.last, req.first, req.middle),
        return_exceptions=True,
    )

    fssp, bankrupt, courts, notary = [
        r if isinstance(r, dict) else {"found": False, "risk": "low", "error": str(r)}
        for r in results
    ]

    risks = [fssp.get("risk","low"), bankrupt.get("risk","low"), courts.get("risk","low"), notary.get("risk","low")]
    score = sum({"high": 35, "medium": 15, "low": 0}[r] for r in risks)
    score = min(score, 100)

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
    return {"status": "ok", "service": "Person Check API"}
