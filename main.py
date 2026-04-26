from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import asyncio
import uuid
import os
import re
import json
from pathlib import Path
from datetime import datetime

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None


app = FastAPI(title="Real Estate Legal Check API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip()

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=25.0, read=240.0, write=60.0, pool=30.0)

IN_PROGRESS_STATES = {
    "queued",
    "queue",
    "in progress",
    "progress",
    "pending",
    "processing",
    "wait",
    "waiting",
}

BAD_STATES = {
    "failed",
    "fail",
    "error",
    "rejected",
    "denied",
    "timeout",
    "manual",
    "not_configured",
}

GOOD_STATES = {
    "complete",
    "completed",
    "done",
    "success",
    "finished",
    "ready",
    "ok",
}

DEFAULT_MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "pledges": "https://www.reestr-zalogov.ru/search",
    "courts": "https://kad.arbitr.ru",
    "egrn": "https://rosreestr.gov.ru",
}


class CheckRequest(BaseModel):
    last: str = ""
    first: str = ""
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 0
    passport_series: str = ""
    passport_number: str = ""
    property_type: str = ""
    cadastral_number: str = ""
    cadastre_number: str = ""
    address: str = ""


def clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed:
                text = fixed
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


def only_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_dob(value: str) -> str:
    value = clean_text(value)
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        y, m, d = value.split("-")
        return f"{d}.{m}.{y}"
    return value


def dob_to_iso(value: str) -> str:
    value = clean_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return value


def rub(value) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".00", "") + " ₽"
    except Exception:
        return "0 ₽"


def safe_json(value, limit: int = 6000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = str(value)
    return text[:limit]


def text_blob(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()


def state_of(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    return clean_text(data.get("state") or data.get("status") or "").lower()


def is_bad_response(data: dict) -> bool:
    if not isinstance(data, dict):
        return True

    st = state_of(data)
    txt = text_blob(data)

    if data.get("http_status") and int(data.get("http_status")) >= 400:
        return True

    if st in BAD_STATES:
        return True

    bad_markers = [
        "не заполнено значение обязательного параметра",
        "не передан",
        "missing required",
        "required parameter",
        "проверьте баланс",
        "токен доступа",
        "access@newdb.net",
        "x-api-key",
        '"code":400',
        '"status":400',
        "bad request",
        "unauthorized",
        "forbidden",
        "доступ запрещ",
        "not enough balance",
        "insufficient balance",
    ]

    return any(marker in txt for marker in bad_markers)


def is_in_progress_response(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    st = state_of(data)
    return st in IN_PROGRESS_STATES


def strip_sensitive(data):
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            kl = str(k).lower()
            if kl in {"balance", "token", "api_key", "x-api-key", "authorization"}:
                continue
            cleaned[k] = strip_sensitive(v)
        return cleaned
    if isinstance(data, list):
        return [strip_sensitive(x) for x in data]
    return data


def flatten_strings(obj, limit=80):
    out = []

    skip_values = {
        "ru",
        "queued",
        "pending",
        "processing",
        "in progress",
        "progress",
        "complete",
        "completed",
        "done",
        "success",
        "ok",
    }

    def walk(x):
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if key in {"requestid", "request_id", "newdb_qid", "qid", "balance", "token", "method", "country", "datecreated", "state", "status"}:
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            s = clean_text(x)
            if not s:
                return
            if s.lower() in skip_values:
                return
            if re.fullmatch(r"[a-f0-9-]{20,}", s.lower()):
                return
            if s.startswith("https://newdb.net/docs"):
                return
            if len(s) <= 300:
                out.append(s)

    walk(obj)
    return out


def extract_error_details(data: dict) -> list:
    if not isinstance(data, dict):
        return ["Источник проверки вернул непонятный ответ."]

    details = []

    for key in ["error", "message", "detail", "description"]:
        val = clean_text(data.get(key))
        if val:
            details.append(val)

    txt = text_blob(data)
    if "не заполнено значение обязательного параметра" in txt:
        details.append("Источник проверки не принял запрос: не заполнен обязательный параметр.")
    if "address" in txt and ("обязательного параметра" in txt or "required" in txt):
        details.append("NewDB требует параметр address для этого метода.")
    if data.get("http_status"):
        details.append(f"HTTP статус: {data.get('http_status')}")
    if "проверьте баланс" in txt or "access@newdb.net" in txt:
        details.append("NewDB просит проверить баланс, тариф или токен доступа.")
    if "x-api-key" in txt:
        details.append("NewDB ожидает токен в HTTP-заголовке X-API-KEY.")

    clean = []
    seen = set()
    for d in details:
        d = clean_text(d)
        if d and d not in seen:
            seen.add(d)
            clean.append(d)

    return clean or ["Источник проверки вернул ошибку."]


def extract_items(data) -> list:
    if not isinstance(data, dict):
        return []

    if is_bad_response(data) or is_in_progress_response(data):
        return []

    keys = ["items", "data", "result", "results", "records", "list", "rows", "objects", "response"]

    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested
            # Иногда полезный результат приходит словарем, а не массивом.
            if not is_bad_response(value) and not is_in_progress_response(value):
                meaningful = {k: v for k, v in value.items() if k not in {"state", "status", "requestId", "request_id", "balance", "datecreated"}}
                if meaningful:
                    return [meaningful]

    return []


def has_meaningful_data(data) -> bool:
    if not isinstance(data, dict):
        return False
    if is_bad_response(data) or is_in_progress_response(data):
        return False

    items = extract_items(data)
    if items:
        return True

    txt = text_blob(data)
    empty_markers = [
        "не найден",
        "не найдено",
        "нет свед",
        "nothing found",
        "not found",
        "no data",
        "empty",
        "отсутств",
    ]
    if any(m in txt for m in empty_markers):
        return False

    useful = {k: v for k, v in data.items() if k not in {"state", "status", "requestId", "request_id", "balance", "datecreated", "params"}}
    useful_text = flatten_strings(useful, 20)
    return bool(useful_text)


def normalize_property(req: CheckRequest) -> dict:
    cadastral = clean_text(req.cadastral_number or req.cadastre_number)
    address = clean_text(req.address)

    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"

    if cadastral and re.match(cad_pattern, cadastral):
        return {
            "type": "cadastral",
            "query": cadastral,
            "cadastral_number": cadastral,
            "address": cadastral,  # NewDB rosreestr часто требует именно address, даже если это кадастровый номер.
        }

    if address and re.match(cad_pattern, address):
        return {
            "type": "cadastral",
            "query": address,
            "cadastral_number": address,
            "address": address,
        }

    address = re.sub(r"\s+", " ", address)
    address = re.sub(r"(?i)\bспб\b|санкт[- ]петербург", "Санкт-Петербург", address)
    address = re.sub(r"(?i)\bгород\s+", "г. ", address)
    address = re.sub(r"(?i)\bулица\s+", "ул. ", address)
    address = re.sub(r"(?i)\bпроспект\s+", "пр. ", address)
    address = re.sub(r"(?i)\bдом\s+", "д. ", address)
    address = re.sub(r"(?i)\bкорпус\s+", "к. ", address)
    address = re.sub(r"(?i)\bквартира\s+", "кв. ", address)

    if address and not re.search(r"(?i)санкт-петербург|ленинградская область", address):
        address = "Санкт-Петербург, " + address

    return {
        "type": "address",
        "query": address,
        "cadastral_number": "",
        "address": address,
    }


async def newdb_post(params: dict, max_wait: int = 90, poll_interval: int = 5) -> dict:
    if not NEWDB_TOKEN:
        return {
            "state": "not_configured",
            "error": "NEWDB_TOKEN не задан в Environment на Render.",
            "params": strip_sensitive(params),
        }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
        # Оставляем Bearer как дополнительный вариант совместимости.
        "Authorization": f"Bearer {NEWDB_TOKEN}",
    }

    payload = {"params": params}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            try:
                data = first.json()
            except Exception:
                data = {"raw_text": first.text}

            data = strip_sensitive(data)
            data["http_status"] = first.status_code
            data["sent_params"] = strip_sensitive(params)

            if first.status_code >= 400:
                data["state"] = data.get("state") or "error"
                return data

        except Exception as e:
            return {
                "state": "error",
                "error": f"Ошибка запроса NewDB: {e}",
                "sent_params": strip_sensitive(params),
            }

        if is_bad_response(data):
            return data

        # Если ответ сразу готовый — возвращаем.
        st = state_of(data)
        if st and st not in IN_PROGRESS_STATES:
            return data

        # Если state отсутствует, но есть содержательные данные — возвращаем.
        if not st and has_meaningful_data(data):
            return data

        request_id = (
            data.get("requestId")
            or data.get("request_id")
            or data.get("id")
            or data.get("qid")
            or data.get("newdb_qid")
        )

        # Часто qid лежит внутри params.
        if not request_id and isinstance(data.get("params"), dict):
            request_id = data["params"].get("newdb_qid") or data["params"].get("requestId")

        if not request_id:
            data["state"] = "manual"
            data["error"] = "NewDB поставил задачу в очередь, но не вернул идентификатор для получения результата."
            return data

        elapsed = 0
        last_data = data

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            candidates = [
                ("GET", f"{NEWDB_URL}/{request_id}", None),
                ("GET", f"{NEWDB_URL}/result/{request_id}", None),
                ("GET", f"{NEWDB_URL}", {"requestId": request_id}),
                ("GET", f"{NEWDB_URL}", {"newdb_qid": request_id}),
                ("POST", NEWDB_URL, {"params": {"newdb_qid": request_id}}),
                ("POST", NEWDB_URL, {"params": {"requestId": request_id}}),
            ]

            for method, url, extra in candidates:
                try:
                    if method == "GET":
                        r = await client.get(url, headers=headers, params=extra)
                    else:
                        r = await client.post(url, headers=headers, json=extra)

                    if r.status_code >= 500:
                        continue

                    try:
                        polled = r.json()
                    except Exception:
                        polled = {"raw_text": r.text}

                    polled = strip_sensitive(polled)
                    polled["http_status"] = r.status_code
                    polled["sent_params"] = strip_sensitive(params)
                    last_data = polled

                    if r.status_code >= 400:
                        continue

                    if is_bad_response(polled):
                        return polled

                    pst = state_of(polled)
                    if pst in IN_PROGRESS_STATES:
                        continue

                    if pst in GOOD_STATES or has_meaningful_data(polled):
                        return polled

                except Exception:
                    continue

        last_data["state"] = "timeout"
        last_data["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд. Это не означает, что проверка чистая."
        return last_data


def base_manual(title: str, summary: str, details: list, url: str) -> dict:
    return {
        "title": title,
        "status": "manual",
        "summary": summary,
        "details": details,
        "manual_check_url": url,
    }


def classify_passport(data) -> dict:
    title = "Паспорт МВД"
    url = DEFAULT_MANUAL_LINKS["passport"]

    if is_in_progress_response(data):
        return base_manual(title, "Источник еще не завершил проверку паспорта. Требуется повторить запрос или проверить вручную.", extract_error_details(data), url)

    if is_bad_response(data):
        return base_manual(title, "Источник проверки паспорта вернул ошибку. Требуется ручная проверка.", extract_error_details(data), url)

    text = text_blob(data)

    if any(x in text for x in ["недейств", "разыскивается", "invalid"]):
        return {
            "title": title,
            "status": "risk",
            "summary": "Выявлены признаки проблемы с паспортом.",
            "details": flatten_strings(data, 8),
            "manual_check_url": url,
        }

    if any(x in text for x in ["действителен", "действительный", "valid"]):
        return {
            "title": title,
            "status": "ok",
            "summary": "Паспорт по полученным данным действителен.",
            "details": [],
            "manual_check_url": url,
        }

    return base_manual(title, "Результат проверки паспорта неоднозначный. Требуется ручная проверка.", ["Источник вернул ответ, который нельзя уверенно трактовать автоматически."], url)


def item_status_text(item) -> str:
    return text_blob(item)


def extract_amount(item) -> float:
    text = json.dumps(item, ensure_ascii=False)
    matches = re.findall(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:руб|₽)", text, flags=re.I)
    amounts = []
    for m in matches:
        try:
            amounts.append(float(m.replace(" ", "").replace(",", ".")))
        except Exception:
            pass

    if amounts:
        return max(amounts)

    if isinstance(item, dict):
        for key in ["amount", "sum", "debt", "debt_sum", "total", "balance", "debtSum"]:
            if key in item:
                try:
                    return float(str(item[key]).replace(" ", "").replace(",", "."))
                except Exception:
                    pass
    return 0.0


def classify_fssp(data) -> dict:
    title = "ФССП"
    url = DEFAULT_MANUAL_LINKS["fssp"]

    if is_in_progress_response(data):
        return base_manual(title, "Источник еще не завершил проверку ФССП. Требуется повторить запрос или проверить вручную.", extract_error_details(data), url)

    if is_bad_response(data):
        return base_manual(title, "Источник проверки ФССП вернул ошибку. Требуется ручная проверка.", extract_error_details(data), url)

    items = extract_items(data)
    text = text_blob(data)

    if not items and any(x in text for x in ["не найден", "нет свед", "nothing found", "not found", "отсутств"]):
        return {
            "title": title,
            "status": "ok",
            "summary": "По полученным данным активные исполнительные производства не найдены.",
            "details": [],
            "manual_check_url": url,
        }

    if not items and not has_meaningful_data(data):
        return {
            "title": title,
            "status": "ok",
            "summary": "По полученным данным исполнительные производства не найдены.",
            "details": [],
            "manual_check_url": url,
        }

    active_items = []
    closed_items = []
    unknown_items = []

    closed_words = ["оконч", "прекращ", "закрыт", "заверш", "ст. 46", "статья 46", "terminated", "closed"]
    active_words = ["возбужден", "актив", "исполнительное производство", "задолженность", "остаток", "взыскание", "active"]

    for item in items or [data]:
        t = item_status_text(item)
        if any(w in t for w in closed_words):
            closed_items.append(item)
        elif any(w in t for w in active_words):
            active_items.append(item)
        else:
            unknown_items.append(item)

    active_total = sum(extract_amount(x) for x in active_items)
    closed_total = sum(extract_amount(x) for x in closed_items)

    details = [
        f"Активные ИП: {len(active_items)}",
        f"Оконченные/закрытые ИП: {len(closed_items)}",
        f"Неоднозначные записи: {len(unknown_items)}",
        f"Сумма по активным ИП: {rub(active_total)}",
        f"Сумма по закрытым/оконченным ИП: {rub(closed_total)}",
    ]

    if active_items or active_total > 0:
        return {
            "title": title,
            "status": "risk",
            "summary": "Выявлены активные исполнительные производства или признаки актуальной задолженности.",
            "details": details,
            "manual_check_url": url,
        }

    if closed_items and not active_items:
        return {
            "title": title,
            "status": "ok",
            "summary": "Найдены закрытые/оконченные ИП. По полученным данным активный долг не подтвержден.",
            "details": details,
            "manual_check_url": url,
        }

    if unknown_items:
        return base_manual(title, "Найдены неоднозначные записи ФССП. Нужно проверить статус вручную.", details, url)

    return {
        "title": title,
        "status": "ok",
        "summary": "По полученным данным исполнительные производства не найдены.",
        "details": [],
        "manual_check_url": url,
    }


def classify_empty_or_risk(data, title: str, risk_summary: str, ok_summary: str, manual_url: str) -> dict:
    if is_in_progress_response(data):
        return base_manual(title, f"Источник еще не завершил проверку: {title}. Требуется повторить запрос или проверить вручную.", extract_error_details(data), manual_url)

    if is_bad_response(data):
        return base_manual(title, f"Источник проверки «{title}» вернул ошибку. Требуется ручная проверка.", extract_error_details(data), manual_url)

    items = extract_items(data)
    text = text_blob(data)

    negative_markers = ["не найден", "нет свед", "ничего не найдено", "not found", "empty", "отсутств", "no data"]

    if not items and (not has_meaningful_data(data) or any(x in text for x in negative_markers)):
        return {
            "title": title,
            "status": "ok",
            "summary": ok_summary,
            "details": [],
            "manual_check_url": manual_url,
        }

    if items:
        return {
            "title": title,
            "status": "risk",
            "summary": risk_summary,
            "details": flatten_strings(items, 10),
            "manual_check_url": manual_url,
        }

    suspicious = [x for x in ["банкрот", "залог", "арбитраж", "дело", "иск", "огранич", "обремен"] if x in text]
    if suspicious:
        return base_manual(title, "Источник вернул неоднозначные сведения. Нужна ручная оценка релевантности записей.", flatten_strings(data, 8), manual_url)

    return {
        "title": title,
        "status": "ok",
        "summary": ok_summary,
        "details": [],
        "manual_check_url": manual_url,
    }


def classify_egrn(data, prop: dict) -> dict:
    title = "ЕГРН / Росреестр"
    url = DEFAULT_MANUAL_LINKS["egrn"]

    if not prop.get("query"):
        return base_manual(title, "Не передан адрес или кадастровый номер объекта. Проверка ЕГРН не выполнена.", ["Укажите кадастровый номер или адрес объекта."], url)

    if is_in_progress_response(data):
        return base_manual(title, "Росреестр еще не завершил обработку запроса. Это не является положительным результатом.", extract_error_details(data), url)

    if is_bad_response(data):
        return base_manual(title, "Источник проверки ЕГРН / Росреестра вернул ошибку. Требуется ручная проверка.", extract_error_details(data), url)

    text_values = flatten_strings(data, 40)
    text = text_blob(data)

    if not has_meaningful_data(data) or any(x in text for x in ["не найден", "нет свед", "not found", "объект не найден"]):
        return base_manual(title, "Объект не найден автоматически или источник не вернул понятный результат. Требуется ручная проверка.", [f"Запрос: {prop.get('query')}"], url)

    risk_words = ["арест", "запрет", "ограничение", "ипотека", "залог", "обременение", "рента"]
    risks = [w for w in risk_words if w in text]

    details = []
    if prop.get("cadastral_number"):
        details.append(f"Кадастровый номер: {prop.get('cadastral_number')}")
    elif prop.get("address"):
        details.append(f"Адрес/запрос: {prop.get('address')}")

    useful_details = [x for x in text_values[:12] if x not in details]
    details.extend(useful_details)

    if risks:
        return {
            "title": title,
            "status": "risk",
            "summary": "В данных по объекту выявлены признаки ограничений, обременений или иных рисков.",
            "details": details,
            "manual_check_url": url,
        }

    return {
        "title": title,
        "status": "ok",
        "summary": "Данные по объекту получены. Явные признаки ограничений или обременений в полученном результате не выявлены.",
        "details": details,
        "manual_check_url": url,
    }


def make_registry_results(checklist: list) -> dict:
    mapping = {
        "Паспорт МВД": "passport",
        "ФССП": "fssp",
        "Банкротство / Федресурс": "bankruptcy",
        "Залоги движимого имущества": "pledges",
        "Суды / арбитраж": "courts",
        "ЕГРН / Росреестр": "egrn",
    }

    result = {}
    for item in checklist:
        key = mapping.get(item["title"], item["title"])
        result[key] = {
            "title": item["title"],
            "status": item["status"],
            "summary": item["summary"],
            "details": item.get("details", []),
            "manual_check_url": item.get("manual_check_url", ""),
        }
    return result


def build_legal_report(req: CheckRequest, checklist: list) -> str:
    risks = [x for x in checklist if x["status"] == "risk"]
    manuals = [x for x in checklist if x["status"] == "manual"]
    oks = [x for x in checklist if x["status"] == "ok"]

    seller = " ".join([req.last, req.first, req.middle]).strip()
    obj = req.cadastral_number or req.cadastre_number or req.address or "по предоставленным данным не указан"

    risk_level = "низкий предварительный риск"
    if risks:
        risk_level = "повышенный риск"
    if len(risks) >= 2:
        risk_level = "высокий риск"
    if manuals and not risks:
        risk_level = "неполные данные, требуется ручная проверка"

    lines = [
        "1. Краткий вывод",
        f"По автоматическим проверкам сформирована предварительная оценка: {risk_level}.",
        "Этот вывод не является гарантией безопасности сделки и требует сверки с оригиналами документов.",
        "",
        "2. Что проверено",
        f"Продавец: {seller or 'по предоставленным данным не указан'}.",
        f"Дата рождения: {req.dob or 'по предоставленным данным не указана'}.",
        f"Объект: {obj}.",
        "",
        "3. Риски по продавцу",
    ]

    seller_items = [x for x in checklist if x["title"] in ["Паспорт МВД", "ФССП", "Банкротство / Федресурс", "Залоги движимого имущества", "Суды / арбитраж"]]
    for item in seller_items:
        lines.append(f"- {item['title']}: {item['summary']}")

    lines.extend(["", "4. Риски по объекту"])
    egrn = next((x for x in checklist if x["title"] == "ЕГРН / Росреестр"), None)
    if egrn:
        lines.append(f"- {egrn['title']}: {egrn['summary']}")
    else:
        lines.append("- ЕГРН / Росреестр: по предоставленным данным не проверялось.")

    lines.extend([
        "",
        "5. Что говорит в пользу сделки",
        f"Проверок без выявленных рисков: {len(oks)}.",
        "Положительным фактором можно считать только те пункты, где получен понятный результат и не выявлены актуальные риски.",
        "",
        "6. Что обязательно проверить до аванса",
    ])

    if manuals:
        for item in manuals:
            lines.append(f"- {item['title']}: требуется ручная проверка. Ссылка: {item.get('manual_check_url','')}")
    else:
        lines.append("- Перед авансом всё равно нужно сверить оригиналы документов, актуальную ЕГРН, основание права, семейное положение продавца и отсутствие ограничений.")

    lines.extend([
        "",
        "7. Что прописать в авансовом соглашении / ПДКП",
        "Прописать обязанность продавца подтвердить отсутствие скрытых обременений, арестов, запретов, банкротства и активных исполнительных производств.",
        "Если есть закрытые ИП, отдельно указать обязанность продавца предоставить документы об окончании/прекращении и отсутствии действующих ограничений.",
        "",
        "8. Безопасная схема расчетов",
        "При долгах, ограничениях или неполных данных безопаснее использовать аккредитив, депозит нотариуса или иную контролируемую схему расчетов с условиями раскрытия после перехода права и снятия ограничений.",
        "",
        "9. Итоговое заключение",
        "Автоматическая проверка помогает выявить предварительные риски, но не заменяет ручной юридический анализ документов и актуальной выписки ЕГРН. 100% безопасность сделки не гарантируется.",
    ])

    return "\n".join(lines)


def generate_pdf(report_id: str, registry_results: dict, legal_report: str) -> Path:
    pdf_path = REPORT_DIR / f"{report_id}.pdf"

    if SimpleDocTemplate is None:
        pdf_path.write_text("PDF module reportlab is not installed. Add reportlab to requirements.txt", encoding="utf-8")
        return pdf_path

    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=42)

    font_name = "Helvetica"
    try:
        dejavu = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        if Path(dejavu).exists():
            pdfmetrics.registerFont(TTFont("DejaVuSans", dejavu))
            font_name = "DejaVuSans"
    except Exception:
        pass

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleRU", parent=styles["Title"], fontName=font_name, fontSize=18, leading=24, alignment=TA_LEFT, spaceAfter=16)
    h_style = ParagraphStyle("HeadRU", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=18, spaceBefore=12, spaceAfter=8)
    p_style = ParagraphStyle("BodyRU", parent=styles["BodyText"], fontName=font_name, fontSize=10, leading=15, spaceAfter=6)

    def esc(s):
        return clean_text(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = []
    story.append(Paragraph("Юридический отчет по проверке недвижимости", title_style))
    story.append(Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", p_style))
    story.append(Spacer(1, 8))

    story.append(Paragraph("1. Данные, полученные из реестров", h_style))
    for item in registry_results.values():
        status_label = {"ok": "Проверено — риск не выявлен", "manual": "Требуется ручная проверка", "risk": "Выявлены риски"}.get(item.get("status"), "Статус не определен")
        story.append(Paragraph(f"<b>{esc(item.get('title'))}</b>", p_style))
        story.append(Paragraph(f"{esc(status_label)}. {esc(item.get('summary'))}", p_style))
        for d in item.get("details", [])[:10]:
            story.append(Paragraph(f"• {esc(d)}", p_style))
        if item.get("status") == "manual" and item.get("manual_check_url"):
            story.append(Paragraph(f"Ручная проверка: {esc(item.get('manual_check_url'))}", p_style))
        story.append(Spacer(1, 4))

    story.append(Paragraph("2. Юридический анализ", h_style))
    for paragraph in legal_report.split("\n"):
        if paragraph.strip():
            story.append(Paragraph(esc(paragraph), p_style))
        else:
            story.append(Spacer(1, 4))

    doc.build(story)
    return pdf_path


def absolute_pdf_url(request: Request, report_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/download-pdf/{report_id}"


@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "Real Estate Legal Check API",
        "newdb_configured": bool(NEWDB_TOKEN),
        "newdb_url": NEWDB_URL,
        "endpoints": ["/check-report", "/download-pdf/{report_id}", "/debug-newdb"],
    }


@app.post("/debug-newdb")
async def debug_newdb(req: CheckRequest):
    """
    Техническая диагностика.
    Этот endpoint нужен, чтобы увидеть, какие именно ответы NewDB возвращает по каждому методу.
    Клиентам его не показывать.
    """
    prop = normalize_property(req)
    dob_ru = normalize_dob(req.dob)
    dob_iso = dob_to_iso(req.dob)
    passport_series = only_digits(req.passport_series)
    passport_number = only_digits(req.passport_number)

    methods = {
        "passport": {
            "method": "passport_mvd",
            "series": passport_series,
            "seria": passport_series,
            "number": passport_number,
            "lastname": req.last,
            "firstname": req.first,
            "middlename": req.middle,
            "secondname": req.middle,
            "birthdate": dob_ru,
            "dob": dob_iso,
            "country": "ru",
        },
        "fssp": {
            "method": "fssp",
            "lastname": req.last,
            "firstname": req.first,
            "middlename": req.middle,
            "secondname": req.middle,
            "birthdate": dob_ru,
            "dob": dob_iso,
            "region": req.region,
            "country": "ru",
        },
        "bankruptcy": {
            "method": "bankruptcy",
            "lastname": req.last,
            "firstname": req.first,
            "middlename": req.middle,
            "secondname": req.middle,
            "birthdate": dob_ru,
            "dob": dob_iso,
            "inn": req.inn,
            "country": "ru",
        },
        "pledges": {
            "method": "pledges",
            "lastname": req.last,
            "firstname": req.first,
            "middlename": req.middle,
            "secondname": req.middle,
            "birthdate": dob_ru,
            "dob": dob_iso,
            "inn": req.inn,
            "country": "ru",
        },
        "courts": {
            "method": "courts",
            "lastname": req.last,
            "firstname": req.first,
            "middlename": req.middle,
            "secondname": req.middle,
            "birthdate": dob_ru,
            "dob": dob_iso,
            "inn": req.inn,
            "country": "ru",
        },
        "egrn": {
            "method": "rosreestr",
            "address": prop.get("address") or prop.get("query"),
            "cadnum": prop.get("cadastral_number"),
            "cadastral_number": prop.get("cadastral_number"),
            "country": "ru",
        },
    }

    async def one(name, params):
        res = await newdb_post(params, max_wait=20, poll_interval=5)
        return name, res

    results = await asyncio.gather(*[one(name, params) for name, params in methods.items()])
    return {
        "sent_property": prop,
        "results": {name: result for name, result in results},
    }


@app.post("/check-report")
async def check_report(req: CheckRequest, request: Request):
    prop = normalize_property(req)
    dob_ru = normalize_dob(req.dob)
    dob_iso = dob_to_iso(req.dob)
    passport_series = only_digits(req.passport_series)
    passport_number = only_digits(req.passport_number)

    passport_params = {
        "method": "passport_mvd",
        "series": passport_series,
        "seria": passport_series,
        "number": passport_number,
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob_ru,
        "dob": dob_iso,
        "country": "ru",
    }

    fssp_params = {
        "method": "fssp",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob_ru,
        "dob": dob_iso,
        "region": req.region,
        "country": "ru",
    }

    bankruptcy_params = {
        "method": "bankruptcy",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob_ru,
        "dob": dob_iso,
        "inn": req.inn,
        "country": "ru",
    }

    pledges_params = {
        "method": "pledges",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob_ru,
        "dob": dob_iso,
        "inn": req.inn,
        "country": "ru",
    }

    courts_params = {
        "method": "courts",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob_ru,
        "dob": dob_iso,
        "inn": req.inn,
        "country": "ru",
    }

    egrn_params = {
        "method": "rosreestr",
        "address": prop.get("address") or prop.get("query"),  # обязательно address
        "cadnum": prop.get("cadastral_number"),
        "cadastral_number": prop.get("cadastral_number"),
        "country": "ru",
    }

    passport_raw, fssp_raw, bankruptcy_raw, pledges_raw, courts_raw, egrn_raw = await asyncio.gather(
        newdb_post(passport_params, max_wait=70, poll_interval=5),
        newdb_post(fssp_params, max_wait=90, poll_interval=5),
        newdb_post(bankruptcy_params, max_wait=90, poll_interval=5),
        newdb_post(pledges_params, max_wait=90, poll_interval=5),
        newdb_post(courts_params, max_wait=90, poll_interval=5),
        newdb_post(egrn_params, max_wait=240, poll_interval=8),
    )

    checklist = [
        classify_passport(passport_raw),
        classify_fssp(fssp_raw),
        classify_empty_or_risk(
            bankruptcy_raw,
            "Банкротство / Федресурс",
            "Выявлены сведения, похожие на банкротство или публикации Федресурса.",
            "По полученным данным сведения о банкротстве не найдены.",
            DEFAULT_MANUAL_LINKS["bankruptcy"],
        ),
        classify_empty_or_risk(
            pledges_raw,
            "Залоги движимого имущества",
            "Выявлены сведения о залогах. Требуется оценить предмет залога и связь с продавцом.",
            "По полученным данным сведения о залогах не найдены.",
            DEFAULT_MANUAL_LINKS["pledges"],
        ),
        classify_empty_or_risk(
            courts_raw,
            "Суды / арбитраж",
            "Выявлены судебные/арбитражные сведения. Требуется оценить связь с продавцом и сделкой.",
            "По полученным данным релевантные судебные сведения не найдены.",
            DEFAULT_MANUAL_LINKS["courts"],
        ),
        classify_egrn(egrn_raw, prop),
    ]

    registry_results = make_registry_results(checklist)
    legal_report = build_legal_report(req, checklist)

    report_id = str(uuid.uuid4())
    generate_pdf(report_id, registry_results, legal_report)

    return {
        "report_id": report_id,
        "registry_results": registry_results,
        "checklist": checklist,
        "legal_report": legal_report,
        "pdf_url": absolute_pdf_url(request, report_id),
    }


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    if not re.match(r"^[a-f0-9-]{36}$", report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")

    path = REPORT_DIR / f"{report_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF not found")

    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=f"legal-real-estate-report-{report_id}.pdf",
    )
