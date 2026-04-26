from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, Any
import httpx
import uuid
import os
import io
import re
import json
import html
import asyncio
import time

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


app = FastAPI(title="Automatic Real Estate Check API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN")
NEWDB_URL = "https://api.newdb.net/v2"
NEWDB_DATA_URL = "https://api.newdb.net/v2/data"
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")

# Простое временное хранилище отчетов, чтобы PDF скачивался по уже показанному отчету,
# а не запускал проверки повторно.
REPORT_CACHE: dict[str, dict] = {}
REPORT_CACHE_TTL_SECONDS = 60 * 60


class CheckRequest(BaseModel):
    last: str
    first: str
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 0
    passport_series: str = ""
    passport_number: str = ""
    address: str = ""
    cadastre_number: str = ""


def only_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def dob_to_iso(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return value


def safe_json(value: Any, limit: int = 6000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = str(value)
    return text[:limit]


def strip_sensitive_newdb(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    cleaned = dict(data)
    for key in ["balance", "token", "api_key", "X-API-KEY"]:
        cleaned.pop(key, None)
    return cleaned


async def newdb_request(params: dict, timeout_seconds: int = 90) -> dict:
    if not NEWDB_TOKEN:
        return {
            "state": "not_configured",
            "error": "NEWDB_TOKEN не задан в переменных окружения Render",
            "params": params,
        }

    request_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }
    payload = {
        "params": params,
        "requestId": request_id,
    }

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            first.raise_for_status()
            first_data = strip_sensitive_newdb(first.json())
        except Exception as e:
            return {
                "state": "error",
                "error": f"Ошибка запроса NewDB: {str(e)}",
                "params": params,
                "requestId": request_id,
            }

        state = str(first_data.get("state", "")).lower()
        if state in ["complete", "completed", "done", "success"]:
            return first_data

        for _ in range(18):
            await asyncio.sleep(5)
            try:
                r = await client.get(
                    NEWDB_DATA_URL,
                    params={"requestId": request_id, "token": NEWDB_TOKEN},
                )
                r.raise_for_status()
                data = strip_sensitive_newdb(r.json())
                state = str(data.get("state", "")).lower()
                if state in ["complete", "completed", "done", "success"]:
                    return data
                if state in ["error", "failed", "fail"]:
                    return data
            except Exception as e:
                return {
                    "state": "error",
                    "error": f"Ошибка получения результата NewDB: {str(e)}",
                    "params": params,
                    "requestId": request_id,
                    "first_response": first_data,
                }

        return {
            "state": "timeout",
            "error": "NewDB не успел вернуть результат",
            "requestId": request_id,
            "params": params,
            "first_response": first_data,
        }


def deep_find_lists(value: Any) -> list:
    found = []
    if isinstance(value, list):
        found.append(value)
        for item in value:
            found.extend(deep_find_lists(item))
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(deep_find_lists(v))
    return found


def extract_result_items(newdb_response: dict, method: str) -> list:
    if not isinstance(newdb_response, dict):
        return []

    candidates = []

    paths = [
        ["results", method, "result", "data"],
        ["results", method, "data"],
        ["result", "data"],
        ["data"],
        ["items"],
    ]

    for path in paths:
        cur = newdb_response
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok:
            if isinstance(cur, list):
                candidates.extend(cur)
            elif isinstance(cur, dict):
                candidates.append(cur)
            elif cur not in [None, ""]:
                candidates.append({"value": cur})

    if candidates:
        return candidates

    # fallback: иногда API заворачивает полезные массивы глубже
    lists = deep_find_lists(newdb_response.get("results", newdb_response))
    for lst in lists:
        if lst and isinstance(lst, list):
            return lst

    return []


def text_of(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def has_real_data(items: list) -> bool:
    if not items:
        return False
    text = text_of(items)
    negative = [
        "данные не найдены",
        "не найдено",
        "ничего не найдено",
        "not found",
        "no data",
        "empty",
    ]
    if any(x in text for x in negative) and len(text) < 900:
        return False
    return True


def extract_money_sum(items: list) -> float:
    text = json.dumps(items, ensure_ascii=False)
    total = 0.0

    # Суммы вида 34 552.13 руб / 34552,13 ₽
    money = re.findall(r"(\d[\d\s]{1,12}(?:[,.]\d{1,2})?)\s*(?:руб|₽)", text, flags=re.I)
    for raw in money:
        try:
            total += float(raw.replace(" ", "").replace(",", "."))
        except Exception:
            pass

    # Частые ключи сумм
    for key in ["sum", "amount", "debt", "debt_sum", "ip_sum", "total"]:
        for m in re.finditer(rf'"{key}"\s*:\s*"?([0-9\s]+(?:[,.][0-9]{{1,2}})?)"?', text, flags=re.I):
            try:
                total += float(m.group(1).replace(" ", "").replace(",", "."))
            except Exception:
                pass

    return round(total, 2)


def fssp_status(items: list) -> dict:
    text = text_of(items)
    if not has_real_data(items):
        return {
            "found": False,
            "active_count": 0,
            "closed_count": 0,
            "debt_sum": 0,
            "summary": "по автоматической проверке ФССП сведения не выявлены",
        }

    closed_words = ["окончено", "прекращено", "завершено", "закрыто", "closed", "terminated"]
    active_words = ["возбуждено", "актив", "исполнительное производство", "взыскание", "задолженность", "active"]

    closed_count = sum(text.count(w) for w in closed_words)
    active_hits = sum(text.count(w) for w in active_words)

    # Если встречаются слова закрытия, но нет признаков активного долга — не считаем как активный долг.
    debt = extract_money_sum(items)
    active_count = 1 if active_hits > 0 and not (closed_count > 0 and debt == 0) else 0

    if closed_count > 0 and active_count == 0:
        summary = "найдены сведения ФССП, но есть признаки оконченного/закрытого исполнительного производства"
    elif active_count > 0:
        summary = f"найдены сведения ФССП, возможен активный долг: {debt} ₽"
    else:
        summary = "найдены сведения ФССП, требуется ручная интерпретация статуса"

    return {
        "found": True,
        "active_count": active_count,
        "closed_count": closed_count,
        "debt_sum": debt,
        "summary": summary,
    }


def passport_status(items: list) -> str:
    if not items:
        return "данные не найдены или требуется ручная проверка"
    text = json.dumps(items, ensure_ascii=False).lower()
    if "недействител" in text:
        return "паспорт недействителен"
    if "действител" in text:
        return "паспорт действителен"
    if "данные не найдены" in text or "не найдено" in text:
        return "данные не найдены, требуется ручная проверка"
    return "получен ответ, требуется ручная интерпретация"


def bankruptcy_status(items: list, response: dict | None = None) -> dict:
    text = text_of(items) + " " + text_of(response or {})
    if any(w in text for w in ["банкрот", "bankrupt", "ефрсб", "fedresurs", "сообщение о банкротстве"]):
        if has_real_data(items):
            return {"checked": True, "found": True, "summary": "найдены признаки сведений о банкротстве/Федресурсе"}
    if not response or response.get("state") in ["not_configured", "error", "timeout"]:
        return {"checked": False, "found": False, "summary": "актуальная проверка банкротства не выполнена, требуется ручная проверка ЕФРСБ"}
    if has_real_data(items):
        return {"checked": True, "found": False, "summary": "получены данные, признаков банкротства автоматически не выделено"}
    return {"checked": True, "found": False, "summary": "по автоматической проверке сведения о банкротстве не выявлены"}


def rosreestr_status(items: list, response: dict | None = None) -> dict:
    text = text_of(items) + " " + text_of(response or {})
    found = has_real_data(items)
    risk_words = ["арест", "запрет", "ипотека", "обремен", "огранич", "залог"]
    risks = [w for w in risk_words if w in text]

    if not response or response.get("state") in ["not_configured", "error", "timeout"]:
        return {
            "checked": False,
            "found": False,
            "risks": [],
            "summary": "ЕГРН/Росреестр не проверен или источник не вернул результат",
        }

    if not found:
        return {
            "checked": True,
            "found": False,
            "risks": [],
            "summary": "объект по автоматической проверке Росреестра не найден или требуется ручная проверка",
        }

    if risks:
        return {
            "checked": True,
            "found": True,
            "risks": risks,
            "summary": "объект найден, есть признаки ограничений/обременений: " + ", ".join(sorted(set(risks))),
        }

    return {
        "checked": True,
        "found": True,
        "risks": [],
        "summary": "объект найден по автоматической проверке Росреестра, явные признаки обременений в краткой сводке не выделены",
    }


async def run_all_newdb_checks(req: CheckRequest) -> dict:
    last = req.last.strip()
    first = req.first.strip()
    middle = req.middle.strip()
    dob_iso = dob_to_iso(req.dob)
    passport_series = only_digits(req.passport_series)
    passport_number = only_digits(req.passport_number)
    inn = only_digits(req.inn)
    cadastre = (req.cadastre_number or "").strip()
    address = (req.address or "").strip()

    tasks: list[tuple[str, str, dict]] = []

    if passport_series and passport_number:
        tasks.append(("passport", "passport_mvd", {
            "method": "passport_mvd",
            "series": passport_series,
            "seria": passport_series,
            "number": passport_number,
            "lastname": last,
            "firstname": first,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
        }))

    if last and first and dob_iso:
        fssp_params = {
            "method": "fssp_person",
            "lastname": last,
            "firstname": first,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
        }
        if req.region:
            fssp_params["region"] = req.region
            fssp_params["regioncode"] = req.region
        tasks.append(("fssp", "fssp_person", fssp_params))

    if last and first and dob_iso:
        tasks.append(("pledges_person", "zalogfiz", {
            "method": "zalogfiz",
            "lastname": last,
            "firstname": first,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
        }))

    if inn:
        tasks.append(("arbitration", "arbitr_ip", {
            "method": "arbitr_ip",
            "inn": inn,
            "country": "ru",
        }))

    # Банкротство. Если у NewDB в вашем тарифе другой method, его нужно заменить здесь.
    # Мы оставляем отдельный блок, чтобы в отчете не было ложного "банкротство не найдено",
    # когда фактически отдельный источник не проверялся.
    if inn:
        tasks.append(("bankruptcy", "bankruptcy", {
            "method": "bankruptcy",
            "inn": inn,
            "lastname": last,
            "firstname": first,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
        }))

    if passport_series and passport_number and last and first and dob_iso:
        tasks.append(("complex", "complex_by_passport", {
            "method": "complex_by_passport",
            "series": passport_series,
            "seria": passport_series,
            "number": passport_number,
            "lastname": last,
            "firstname": first,
            "secondname": middle,
            "dob": dob_iso,
            "country": "ru",
        }))

    if cadastre or address:
        params = {"method": "rosreestr", "country": "ru"}
        if cadastre:
            params["address"] = cadastre
            params["cadnum"] = cadastre
            params["cadastre_number"] = cadastre
            params["cadastral_number"] = cadastre
        else:
            params["address"] = address
        tasks.append(("rosreestr", "rosreestr", params))

    async def run_one(name: str, method: str, params: dict):
        response = await newdb_request(params)
        return name, method, params, response

    results = await asyncio.gather(
        *[run_one(name, method, params) for name, method, params in tasks],
        return_exceptions=True,
    )

    checks = {}
    for item in results:
        if isinstance(item, Exception):
            continue
        name, method, params, response = item
        items = extract_result_items(response, method)
        checks[name] = {
            "method": method,
            "params": {k: v for k, v in params.items() if k not in ["token", "api_key"]},
            "state": response.get("state"),
            "error": response.get("error", ""),
            "items": items,
            "items_count": len(items),
            "raw_preview": safe_json(items[:3], 3000),
        }

    return checks


def build_summary(req: CheckRequest, checks: dict) -> dict:
    passport_items = checks.get("passport", {}).get("items", [])
    fssp_items = checks.get("fssp", {}).get("items", [])
    bankruptcy_items = checks.get("bankruptcy", {}).get("items", [])
    complex_items = checks.get("complex", {}).get("items", [])
    rosreestr_items = checks.get("rosreestr", {}).get("items", [])
    pledge_items = checks.get("pledges_person", {}).get("items", [])
    arbitration_items = checks.get("arbitration", {}).get("items", [])

    fssp = fssp_status(fssp_items)

    # Банкротство смотрим и по отдельному источнику, и по complex, но честно показываем, проверялся ли отдельный источник.
    bankruptcy = bankruptcy_status(
        bankruptcy_items + complex_items,
        checks.get("bankruptcy") or checks.get("complex"),
    )
    ros = rosreestr_status(rosreestr_items, checks.get("rosreestr"))

    high = 0
    medium = 0
    unknown = 0
    notes = []

    pass_status = passport_status(passport_items)
    if "недействителен" in pass_status:
        high += 1
        notes.append("Паспорт имеет признак недействительности.")
    elif "ручная" in pass_status or "не найдены" in pass_status:
        medium += 1
        notes.append("По паспорту требуется ручная проверка.")

    if fssp["active_count"] > 0:
        if fssp["debt_sum"] >= 300000:
            high += 1
        else:
            medium += 1
        notes.append(fssp["summary"])
    elif fssp["closed_count"] > 0:
        notes.append("Есть закрытое/оконченное ИП: это не равно активному долгу, но причину закрытия лучше проверить.")

    if bankruptcy["found"]:
        high += 1
        notes.append(bankruptcy["summary"])
    elif not bankruptcy["checked"]:
        unknown += 1
        notes.append(bankruptcy["summary"])

    if has_real_data(pledge_items):
        medium += 1
        notes.append("Найдены сведения по залогам физлица, требуется ручная проверка предмета залога.")

    if has_real_data(arbitration_items):
        medium += 1
        notes.append("Найдены арбитражные/ИП-сведения, требуется ручная оценка связи с продавцом и недвижимостью.")

    if not ros["checked"]:
        unknown += 1
        notes.append(ros["summary"])
    elif not ros["found"]:
        medium += 1
        notes.append(ros["summary"])
    elif ros["risks"]:
        high += 1
        notes.append(ros["summary"])

    if high:
        level = "высокий риск"
    elif medium or unknown:
        level = "средний риск"
    else:
        level = "низкий риск"

    return {
        "seller": {
            "full_name": f"{req.last} {req.first} {req.middle}".strip(),
            "dob": req.dob,
            "inn": req.inn or "не указан",
            "region": req.region or "не указан",
            "passport": passport_status(passport_items),
        },
        "property": {
            "address": req.address or "не указан",
            "cadastre_number": req.cadastre_number or "не указан",
        },
        "checks_panel": {
            "passport": passport_status(passport_items),
            "fssp": fssp,
            "bankruptcy": bankruptcy,
            "pledges_person": {
                "found": has_real_data(pledge_items),
                "items_count": len(pledge_items),
                "summary": "сведения найдены" if has_real_data(pledge_items) else "по автоматической проверке сведения не выявлены",
            },
            "arbitration": {
                "found": has_real_data(arbitration_items),
                "items_count": len(arbitration_items),
                "summary": "сведения найдены" if has_real_data(arbitration_items) else "по автоматической проверке сведения не выявлены",
            },
            "rosreestr": ros,
        },
        "risk_score": {
            "level": level,
            "high_risks": high,
            "medium_risks": medium,
            "unknown": unknown,
            "notes": notes,
        },
    }


def checks_panel_text(summary: dict) -> str:
    panel = summary.get("checks_panel", {})
    risk = summary.get("risk_score", {})

    lines = [
        "РЕЗУЛЬТАТЫ АВТОМАТИЧЕСКИХ ПРОВЕРОК",
        "",
        f"Паспорт: {panel.get('passport', 'нет данных')}",
        f"ФССП: {panel.get('fssp', {}).get('summary', 'нет данных')}",
        f"Банкротство: {panel.get('bankruptcy', {}).get('summary', 'нет данных')}",
        f"Залоги физлица: {panel.get('pledges_person', {}).get('summary', 'нет данных')}",
        f"Суды / арбитраж / ИП: {panel.get('arbitration', {}).get('summary', 'нет данных')}",
        f"ЕГРН / Росреестр: {panel.get('rosreestr', {}).get('summary', 'нет данных')}",
        "",
        f"Предварительный уровень риска: {risk.get('level', 'требуется ручная проверка')}",
    ]
    notes = risk.get("notes", [])
    if notes:
        lines.append("Ключевые замечания:")
        for n in notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


async def get_gigachat_token() -> Optional[str]:
    if not GIGACHAT_AUTH_KEY:
        return None

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"scope": "GIGACHAT_API_PERS"}

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        response = await client.post(url, headers=headers, data=data)
        response.raise_for_status()
        return response.json().get("access_token")


def compact_checks_for_prompt(checks: dict) -> dict:
    compact = {}
    for name, item in checks.items():
        compact[name] = {
            "method": item.get("method"),
            "state": item.get("state"),
            "error": item.get("error"),
            "items_count": item.get("items_count"),
            "items_preview": item.get("items", [])[:5],
        }
    return compact


def build_prompt(summary: dict, checks: dict) -> str:
    return f"""
Ты — юрист-эксперт по недвижимости в Санкт-Петербурге.

Сформируй подробный юридический отчет для покупателя недвижимости по автоматическим проверкам.

Структура строго:
1. Краткий вывод
2. Что проверено
3. Риски по продавцу
4. Риски по объекту
5. Что говорит в пользу сделки
6. Что обязательно проверить до аванса
7. Что прописать в авансовом соглашении / ПДКП
8. Безопасная схема расчетов
9. Итоговое заключение

Критически важные правила:
- Не придумывай факты.
- Если источник не проверялся или вернул ошибку — прямо пиши: “по предоставленным данным не проверялось” или “требуется ручная проверка”.
- Если ФССП показывает закрытое/оконченное ИП, обязательно напиши, что ИП закрыто/окончено и не называй его активным долгом без подтверждения активного статуса.
- Если банкротство не проверено отдельным актуальным источником, не пиши “банкротства нет”. Пиши: “актуальную проверку ЕФРСБ нужно выполнить вручную”.
- Если Росреестр/ЕГРН не вернул данные, отдельным блоком напиши, что без актуальной выписки ЕГРН нельзя делать вывод по собственнику, обременениям и ограничениям.
- Не обещай 100% безопасность.
- Не показывай технические поля API, requestId, balance, taskId.

СВОДКА:
{json.dumps(summary, ensure_ascii=False, indent=2)}

ДАННЫЕ ПРОВЕРОК ДЛЯ АНАЛИЗА:
{json.dumps(compact_checks_for_prompt(checks), ensure_ascii=False, indent=2)}
"""


def fallback_report(summary: dict) -> str:
    panel = summary.get("checks_panel", {})
    risk = summary.get("risk_score", {})
    seller = summary.get("seller", {})
    prop = summary.get("property", {})

    return f"""
1. Краткий вывод

По автоматическим проверкам сформирована предварительная оценка. Уровень риска: {risk.get('level', 'требуется ручная проверка')}.

2. Что проверено

Продавец: {seller.get('full_name', 'по предоставленным данным не проверялось')}.
Дата рождения: {seller.get('dob', 'по предоставленным данным не проверялось')}.
Паспорт: {panel.get('passport', 'по предоставленным данным не проверялось')}.
Объект: {prop.get('address', 'по предоставленным данным не проверялось')}.
Кадастровый номер: {prop.get('cadastre_number', 'по предоставленным данным не проверялось')}.

3. Риски по продавцу

ФССП: {panel.get('fssp', {}).get('summary', 'по предоставленным данным не проверялось')}.
Банкротство: {panel.get('bankruptcy', {}).get('summary', 'по предоставленным данным не проверялось')}.
Залоги физлица: {panel.get('pledges_person', {}).get('summary', 'по предоставленным данным не проверялось')}.
Суды / арбитраж / ИП: {panel.get('arbitration', {}).get('summary', 'по предоставленным данным не проверялось')}.

4. Риски по объекту

ЕГРН / Росреестр: {panel.get('rosreestr', {}).get('summary', 'по предоставленным данным не проверялось')}.
Без актуальной выписки ЕГРН нельзя делать окончательный вывод по собственнику, обременениям и ограничениям.

5. Что говорит в пользу сделки

Высоких рисков: {risk.get('high_risks', 0)}.
Средних рисков: {risk.get('medium_risks', 0)}.
Непроверенных пунктов: {risk.get('unknown', 0)}.

6. Что обязательно проверить до аванса

Нужно получить актуальную выписку ЕГРН, проверить основание права, историю перехода права, семейное положение продавца, согласия, ФССП, ЕФРСБ, суды, залоги и паспорт.

7. Что прописать в авансовом соглашении / ПДКП

Прописать обязанность продавца подтвердить отсутствие скрытых обременений, арестов, запретов, банкротства и активных исполнительных производств. Если есть закрытые ИП, указать обязанность продавца предоставить документы о прекращении/окончании и отсутствии действующих ограничений.

8. Безопасная схема расчетов

При долгах, ограничениях или неполных данных безопаснее использовать аккредитив, депозит нотариуса или другую контролируемую схему расчетов.

9. Итоговое заключение

Автоматическая проверка помогает выявить предварительные риски, но не заменяет ручной юридический анализ документов и актуальной ЕГРН. 100% безопасность не гарантируется.
""".strip()


async def generate_ai_report(summary: dict, checks: dict) -> str:
    token = await get_gigachat_token()
    if not token:
        return fallback_report(summary)

    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "GigaChat",
        "messages": [{"role": "user", "content": build_prompt(summary, checks)}],
        "temperature": 0.15,
        "max_tokens": 3500,
    }

    async with httpx.AsyncClient(verify=False, timeout=120) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
    return result["choices"][0]["message"]["content"]


def register_pdf_font():
    possible_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
        "DejaVuSans.ttf",
    ]
    for path in possible_paths:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("DejaVuSans", path))
            return "DejaVuSans"
    return "Helvetica"


def make_pdf(report_text: str) -> bytes:
    buffer = io.BytesIO()
    font_name = register_pdf_font()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=42)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], fontName=font_name, fontSize=18, leading=24, spaceAfter=18)
    body_style = ParagraphStyle("BodyCustom", parent=styles["BodyText"], fontName=font_name, fontSize=10.5, leading=16, spaceAfter=10)

    story = [Paragraph("Юридический отчет по проверке недвижимости", title_style), Spacer(1, 10)]
    for block in report_text.split("\n"):
        clean = block.strip()
        if not clean:
            story.append(Spacer(1, 8))
            continue
        story.append(Paragraph(html.escape(clean), body_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def cache_set(report_id: str, data: dict):
    REPORT_CACHE[report_id] = {"created_at": time.time(), **data}
    # чистим старые отчеты
    now = time.time()
    for key in list(REPORT_CACHE.keys()):
        if now - REPORT_CACHE[key].get("created_at", now) > REPORT_CACHE_TTL_SECONDS:
            REPORT_CACHE.pop(key, None)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Automatic Real Estate Check API",
        "endpoints": ["/check-report", "/report-pdf/{report_id}", "/debug-check"],
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(GIGACHAT_AUTH_KEY),
    }


@app.post("/debug-check")
async def debug_check(req: CheckRequest):
    checks = await run_all_newdb_checks(req)
    summary = build_summary(req, checks)
    return {"summary": summary, "checks": checks}


@app.post("/check-report")
async def check_report(req: CheckRequest):
    checks = await run_all_newdb_checks(req)
    summary = build_summary(req, checks)
    report = await generate_ai_report(summary, checks)
    panel_text = checks_panel_text(summary)

    report_id = str(uuid.uuid4())
    full_pdf_text = panel_text + "\n\n" + report
    cache_set(report_id, {"report": report, "panel_text": panel_text, "summary": summary, "pdf_text": full_pdf_text})

    return {
        "report_id": report_id,
        "checks_panel_text": panel_text,
        "report": report,
        "summary": summary,
    }


@app.get("/report-pdf/{report_id}")
async def report_pdf(report_id: str):
    item = REPORT_CACHE.get(report_id)
    if not item:
        raise HTTPException(status_code=404, detail="Отчет не найден или срок хранения истек. Сформируйте отчет заново.")
    pdf_bytes = make_pdf(item.get("pdf_text") or item.get("report") or "Отчет не найден")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=legal-real-estate-report.pdf"},
    )


# Старый POST endpoint оставлен для совместимости, но лучше пользоваться GET /report-pdf/{report_id}
@app.post("/check-report-pdf")
async def check_report_pdf(req: CheckRequest):
    result = await check_report(req)
    pdf_bytes = make_pdf(result["checks_panel_text"] + "\n\n" + result["report"])
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=legal-real-estate-report.pdf"},
    )
