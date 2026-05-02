import os
import re
import json
import uuid
import logging
import asyncio
from typing import Optional, Tuple, List, Dict

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------- НАСТРОЙКИ ----------
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
PUBLIC_WIDGET_KEY = os.getenv("PUBLIC_WIDGET_KEY", "my_secret_key")
ALLOWED_ORIGINS = ["*"]  # для теста – разрешить всем

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- МОДЕЛИ ----------
class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    passport_series: str = ""
    passport_number: str = ""
    seria: str = ""
    number: str = ""
    seriapass: str = ""
    numberpass: str = ""
    region: int = 78
    address: str = ""
    cadastral_number: str = ""

# ---------- УТИЛИТЫ ----------
def digits(s: str) -> str:
    return re.sub(r"\D", "", str(s))

def normalize_dob(value: str) -> Optional[str]:
    raw = digits(value)
    if len(raw) == 8:
        return f"{raw[4:8]}-{raw[2:4]}-{raw[:2]}"
    parts = re.split(r"[.\-/]", value)
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
        else:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None

def get_passport(req: CheckRequest) -> Tuple[str, str]:
    series = digits(req.passport_series or req.seria or req.seriapass)
    number = digits(req.passport_number or req.number or req.numberpass)
    return series[:4], number[:6]

# ---------- NEWDB (POLLING) ----------
async def newdb_request(method: str, params: dict, timeout_sec: int = 180) -> dict:
    if not NEWDB_TOKEN:
        logger.error("NEWDB_TOKEN не задан")
        return {"state": "error", "error": "NEWDB_TOKEN missing"}

    url = "https://api.newdb.net/v2"
    headers = {"X-API-KEY": NEWDB_TOKEN, "Content-Type": "application/json"}
    request_id = str(uuid.uuid4())
    payload = {"params": params, "requestId": request_id}

    async with httpx.AsyncClient(timeout=40) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
        except Exception as e:
            logger.error(f"Ошибка при создании задачи: {e}")
            return {"state": "error", "error": str(e)}

        logger.info(f"[{method}] Создана задача {request_id}, state={data.get('state')}")
        if data.get("state") in ("complete", "done"):
            return data
        if data.get("state") in ("error", "failed"):
            return data

        start = asyncio.get_event_loop().time()
        interval = 3.0
        while (asyncio.get_event_loop().time() - start) < timeout_sec:
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, 20)
            try:
                poll_resp = await client.post(url, json={"requestId": request_id}, headers=headers)
                poll_data = poll_resp.json()
            except Exception as e:
                logger.error(f"Ошибка при опросе: {e}")
                continue
            state = poll_data.get("state")
            logger.info(f"[{method}] Опрос {request_id}, state={state}")
            if state in ("complete", "done"):
                return poll_data
            if state in ("error", "failed"):
                return poll_data

        logger.error(f"[{method}] Таймаут {timeout_sec}с")
        return {"state": "timeout", "error": "Timeout"}

# ---------- ПОСТРОЕНИЕ PAYLOAD ----------
def build_complex_payload(req: CheckRequest) -> Optional[dict]:
    series, number = get_passport(req)
    if not series or not number:
        logger.warning("Нет паспортных данных")
        return None
    dob_iso = normalize_dob(req.dob)
    if not dob_iso:
        logger.warning("Нет даты рождения")
        return None
    return {
        "method": "complex_by_passport",
        "country": "ru",
        "seria": series,
        "number": number,
        "seriapass": series,
        "numberpass": number,
        "lastname": req.last.strip(),
        "firstname": req.first.strip(),
        "secondname": req.middle.strip(),
        "dob": dob_iso,
        "regioncode": req.region,
    }

def build_pravo_payload(req: CheckRequest) -> Optional[dict]:
    fio = " ".join(filter(None, [req.last, req.first, req.middle])).strip()
    if not fio:
        return None
    return {
        "method": "pravo_search",
        "country": "ru",
        "query": fio,
        "lastname": req.last,
        "firstname": req.first,
        "secondname": req.middle,
        "limit": 50,
    }

# ---------- ПАРСИНГ ОТВЕТОВ ----------
def parse_complex_result(resp: dict) -> List[dict]:
    if resp.get("state") != "complete":
        return [{"title": "Комплексная проверка", "status": "error", "summary": "Не удалось получить данные"}]

    results = resp.get("results", {})
    checklist = []

    # Паспорт МВД
    mvd = results.get("passport_mvd", {})
    mvd_data = mvd.get("result", {}).get("data")
    if mvd_data:
        text = json.dumps(mvd_data, ensure_ascii=False).lower()
        if "недействителен" in text:
            checklist.append({"title": "Паспорт МВД", "status": "risk", "summary": "Паспорт может быть недействительным"})
        else:
            checklist.append({"title": "Паспорт МВД", "status": "ok", "summary": "Паспорт действителен"})
    else:
        checklist.append({"title": "Паспорт МВД", "status": "manual", "summary": "Нет данных, проверьте вручную"})

    # ФССП
    fssp = results.get("fssp_person", {})
    fssp_data = fssp.get("result", {}).get("data")
    if isinstance(fssp_data, list):
        active = [x for x in fssp_data if not x.get("CompletionDateOrReason")]
        if active:
            checklist.append({"title": "ФССП", "status": "risk", "summary": f"Активных производств: {len(active)}"})
        else:
            checklist.append({"title": "ФССП", "status": "ok", "summary": "Исполнительных производств нет"})
    else:
        checklist.append({"title": "ФССП", "status": "manual", "summary": "Нет данных от ФССП"})

    # Залоги
    pledge = results.get("pledge_person", {})
    pledge_data = pledge.get("result", {}).get("data")
    if pledge_data:
        cnt = len(pledge_data) if isinstance(pledge_data, list) else 1
        checklist.append({"title": "Залоги (ФНП)", "status": "risk", "summary": f"Найдено {cnt} записей о залогах"})
    else:
        checklist.append({"title": "Залоги (ФНП)", "status": "ok", "summary": "Залоги не найдены"})

    # ЕГРИП
    egrul = results.get("egrul_ip", {})
    egrul_data = egrul.get("result", {}).get("data")
    if egrul_data:
        text = json.dumps(egrul_data, ensure_ascii=False).lower()
        if "действующ" in text:
            checklist.append({"title": "ЕГРИП", "status": "risk", "summary": "Продавец зарегистрирован как ИП"})
        else:
            checklist.append({"title": "ЕГРИП", "status": "ok", "summary": "ИП не активен"})
    else:
        checklist.append({"title": "ЕГРИП", "status": "manual", "summary": "Нет данных"})

    return checklist

def parse_pravo_result(resp: dict) -> dict:
    if resp.get("state") != "complete":
        return {"title": "Суды (ГАС)", "status": "error", "summary": "Не удалось получить данные"}
    data = resp.get("results", {}).get("pravo_search", {}).get("result", {}).get("data")
    if not isinstance(data, list) or len(data) == 0:
        return {"title": "Суды (ГАС)", "status": "ok", "summary": "Судебные дела по ФИО не найдены"}
    return {"title": "Суды (ГАС)", "status": "manual", "summary": f"Найдено {len(data)} дел. Требуется ручная сверка"}

def compute_score(checklist: List[dict]) -> dict:
    score = 0
    for item in checklist:
        if item.get("status") == "risk":
            title = item["title"]
            if "ФССП" in title:
                score += 18
            elif "Залоги" in title:
                score += 12
            elif "Паспорт" in title and "недействителен" in item["summary"]:
                score += 40
            else:
                score += 8
    score = min(100, score)
    if score >= 85:
        label = "Опасно при самостоятельной сделке"
    elif score >= 60:
        label = "Высокий риск"
    elif score >= 35:
        label = "Условно рискованно"
    else:
        label = "Допустимо к рассмотрению"
    return {"score": score, "label": label, "max_score": 100}

# ---------- ОСНОВНОЙ ЭНДПОИНТ ----------
@app.post("/check-report")
async def check_report(request: CheckRequest, http_request: Request):
    widget_key = http_request.headers.get("X-Widget-Key")
    if widget_key != PUBLIC_WIDGET_KEY:
        raise HTTPException(401, "Invalid X-Widget-Key")

    logger.info(f"Запрос от {request.last} {request.first}, дата {request.dob}")

    if not request.last or not request.first or not request.dob:
        return {"success": False, "error": "Заполните фамилию, имя и дату рождения"}
    series, number = get_passport(request)
    if not series or len(series) != 4 or not number or len(number) != 6:
        return {"success": False, "error": "Введите серию (4 цифры) и номер (6 цифр) паспорта"}

    complex_payload = build_complex_payload(request)
    pravo_payload = build_pravo_payload(request)

    complex_resp, pravo_resp = await asyncio.gather(
        newdb_request("complex_by_passport", complex_payload, 240) if complex_payload else asyncio.sleep(0, {"state": "skipped"}),
        newdb_request("pravo_search", pravo_payload, 120) if pravo_payload else asyncio.sleep(0, {"state": "skipped"}),
    )

    checklist = []
    if complex_resp and complex_resp.get("state") != "skipped":
        checklist.extend(parse_complex_result(complex_resp))
    else:
        checklist.append({"title": "Комплексная проверка", "status": "error", "summary": "Не удалось запустить"})

    if pravo_resp and pravo_resp.get("state") != "skipped":
        checklist.append(parse_pravo_result(pravo_resp))
    else:
        checklist.append({"title": "Суды", "status": "manual", "summary": "Проверка не выполнялась"})

    scoring = compute_score(checklist)
    return {
        "success": True,
        "report_id": str(uuid.uuid4()),
        "checklist": checklist,
        "risk_scoring": scoring,
        "recommendations": [
            {"title": "Проверьте оригинал паспорта", "text": "Сравните с данными из МВД"},
            {"title": "Запросите выписку ЕГРН", "text": "Актуальную на день сделки"},
            {"title": "Аванс только по письменному соглашению", "text": "С условиями возврата"}
        ]
    }

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
