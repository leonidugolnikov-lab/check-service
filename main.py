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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

async def check_fssp(last: str, first: str, middle: str, dob: str):
    try:
        url = "https://api-ip.fssp.gov.ru/api/v1.0/debt/search"
        params = {"fio[0][n]": last, "fio[0][f]": first, "region": "0", "token": "none"}
        if middle:
            params["fio[0][o]"] = middle
        if dob:
            params["fio[0][b]"] = dob

        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200 or not r.text.strip():
                raise ValueError(f"Пустой ответ (статус {r.status_code})")
            data = r.json()
            items = data.get("result", {}).get("items", [])
            total = sum(len(i.get("exe_production", [])) for i in items)
            amount = 0.0
            details = []
            for item in items:
                for debt in item.get("exe_production", []):
                    try:
                        amount += float(str(debt.get("sum","0")).replace(",",".").replace(" ","").replace("\xa0",""))
                    except:
                        pass
                    details.append({"number": debt.get("ip_id",""), "subject": debt.get("subject",""), "sum": debt.get("sum","")})
            return {
                "found": total > 0, "count": total,
                "amount": f"{amount:,.0f} ₽" if amount > 0 else "0 ₽",
                "details": details[:5], "risk": "high" if total >= 3 else "medium" if total >= 1 else "low",
                "note": f"Найдено производств: {total}" if total > 0 else "Производств не найдено",
                "source_url": "https://fssp.gov.ru/iss/ip",
            }
    except Exception as e:
        return {
            "found": False, "count": 0, "amount": "—", "details": [], "risk": "low",
            "note": "Автопроверка временно недоступна — проверьте вручную на сайте ФССП",
            "manual_url": f"https://fssp.gov.ru/iss/ip",
            "error": str(e),
        }

async def check_bankrupt(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        url = "https://bankrot.fedresurs.ru/api/bankrupts"
        params = {"searchString": fio, "pageSize": 10, "page": 1, "facets": "PERSON"}
        headers = {**HEADERS, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200 or not r.text.strip():
                raise ValueError(f"Пустой ответ (статус {r.status_code})")
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", data.get("items", []))
            total = len(items)
            details = []
            for item in items[:5]:
                details.append({
                    "name": item.get("name", item.get("fullName","")),
                    "status": item.get("status", item.get("currentProcedure","")),
                    "date": str(item.get("publishedDate", item.get("startDate","")))[:10],
                    "region": item.get("region", item.get("regionName","")),
                })
            return {
                "found": total > 0, "count": total, "details": details,
                "risk": "high" if total > 0 else "low",
                "note": f"Найдено дел о банкротстве: {total}" if total > 0 else "Дел о банкротстве не найдено",
                "source_url": "https://fedresurs.ru/",
            }
    except Exception as e:
        return {
            "found": False, "count": 0, "details": [], "risk": "low",
            "note": "Автопроверка временно недоступна — проверьте вручную на сайте ЕФРСБ",
            "manual_url": f"https://fedresurs.ru/search/person?q={last}+{first}",
            "error": str(e),
        }

async def check_courts(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        url = "https://sudrf.ru/index.php"
        params = {"id": "300", "act": "go_search", "searchtype": "all", "name": fio, "f": "1"}
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            text = r.text
            found = "результат" in text.lower() or "дело" in text.lower()
            count = text.lower().count("дело №") + text.lower().count("производство")
            return {
                "found": found, "count": count,
                "note": "Найдены совпадения в ГАС Правосудие" if found else "Совпадений не найдено",
                "risk": "medium" if found else "low",
                "source_url": "https://sudrf.ru/",
                "manual_url": f"https://sudrf.ru/index.php?id=300&act=go_search&searchtype=all&name={last}+{first}",
            }
    except Exception as e:
        return {
            "found": False, "count": 0,
            "note": "Автопроверка недоступна — проверьте вручную",
            "risk": "low",
            "manual_url": f"https://sudrf.ru/index.php?id=300&act=go_search&searchtype=all&name={last}+{first}",
            "error": str(e),
        }

async def check_notary(last: str, first: str, middle: str):
    try:
        fio = f"{last} {first} {middle}".strip()
        url = "https://notariat.ru/ru-ru/help/probate-cases/search/"
        params = {"search": fio}
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            text = r.text
            found = "наследственное дело" in text.lower()
            return {
                "found": found,
                "note": "Найдены наследственные дела" if found else "Записей не найдено",
                "risk": "medium" if found else "low",
                "source_url": "https://notariat.ru/",
                "manual_url": f"https://notariat.ru/ru-ru/help/probate-cases/search/?search={last}+{first}",
            }
    except Exception as e:
        return {
            "found": False,
            "note": "Автопроверка недоступна — проверьте вручную",
            "risk": "low",
            "manual_url": f"https://notariat.ru/ru-ru/help/probate-cases/search/?search={last}+{first}",
            "error": str(e),
        }

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
        r if isinstance(r, dict) else {"found": False, "risk": "low", "note": "Ошибка запроса", "error": str(r)}
        for r in results
    ]
    risks = [fssp.get("risk","low"), bankrupt.get("risk","low"), courts.get("risk","low"), notary.get("risk","low")]
    score = min(sum({"high":35,"medium":15,"low":0}[r] for r in risks), 100)
    return {
        "name": f"{req.last} {req.first} {req.middle}".strip(),
        "score": score,
        "level": "high" if score > 50 else "medium" if score > 20 else "low",
        "fssp": fssp, "bankrupt": bankrupt, "courts": courts, "notary": notary,
    }

@app.get("/")
def root():
    return {"status": "ok", "service": "Person Check API v2"}
