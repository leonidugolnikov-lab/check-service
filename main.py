from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Any
from pathlib import Path
from datetime import datetime
import asyncio
import httpx
import json
import os
import re
import uuid

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
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip().rstrip("/")

GIGACHAT_TOKEN = os.getenv("GIGACHAT_TOKEN", "").strip()
GIGACHAT_URL = os.getenv(
    "GIGACHAT_URL",
    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
).strip()

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=20.0, read=240.0, write=40.0, pool=20.0)

DEFAULT_MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "pledges": "https://www.reestr-zalogov.ru/search",
    "courts": "https://kad.arbitr.ru",
    "egrn": "https://rosreestr.gov.ru",
}


class CheckRequest(BaseModel):
    last: str
    first: str
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


READY_STATES = {"complete", "completed", "done", "success", "finished", "ready", "ok"}
PENDING_STATES = {"in progress", "progress", "pending", "processing", "queued", "queue", "wait", "waiting"}
FAILED_STATES = {"error", "failed", "fail", "rejected", "canceled", "cancelled"}


def clean_text(value: Any) -> str:
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
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return value


def rub(value: Any) -> str:
    try:
        amount = float(str(value).replace(" ", "").replace(",", "."))
        return f"{amount:,.2f}".replace(",", " ").replace(".00", "") + " ₽"
    except Exception:
        return "0 ₽"


def flatten_strings(obj: Any, limit: int = 80) -> list[str]:
    out: list[str] = []

    def walk(x: Any):
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            s = clean_text(x)
            if s and len(s) <= 400:
                out.append(s)

    walk(obj)
    return out


def response_text(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False).lower()
    except Exception:
        return str(data).lower()


def get_state(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return clean_text(data.get("state") or data.get("status") or "").lower()


def has_api_error(data: Any) -> bool:
    text = response_text(data)
    state = get_state(data)
    markers = [
        "ошибка",
        "error",
        "failed",
        "fail",
        "не заполнено значение обязательного параметра",
        "обязательного параметра",
        "required parameter",
        "bad request",
        "400",
        "401",
        "403",
        "проверьте баланс",
        "токен",
        "token",
        "x-api-key",
        "unauthorized",
        "forbidden",
        "access@newdb.net",
    ]
    return state in FAILED_STATES or any(m in text for m in markers)


def api_error_message(data: Any, default: str = "Источник вернул ошибку. Требуется ручная проверка.") -> str:
    strings = flatten_strings(data, 30)
    priority = [
        "Не заполнено значение обязательного параметра",
        "Проверьте баланс",
        "ошибка",
        "error",
        "failed",
        "400",
        "401",
        "403",
    ]
    for s in strings:
        low = s.lower()
        if any(p.lower() in low for p in priority):
            return s
    return default


def extract_request_id(data: dict) -> str:
    return clean_text(
        data.get("requestId")
        or data.get("request_id")
        or data.get("id")
        or data.get("qid")
        or data.get("newdb_qid")
        or ""
    )


def extract_items(data: Any) -> list:
    if not isinstance(data, dict):
        if isinstance(data, list):
            return data
        return []

    # Если ответ явно технический/ошибочный/ожидающий — не считаем параметры запроса полезными данными.
    if has_api_error(data) or get_state(data) in PENDING_STATES:
        return []

    keys = ["items", "data", "result", "results", "records", "list", "rows"]
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested

    # Если ответ готовый, но без привычных ключей, берем только нетехнические поля.
    technical = {"state", "status", "requestId", "request_id", "balance", "datecreated", "tasks", "params", "method"}
    meaningful = {k: v for k, v in data.items() if k not in technical and v not in (None, "", [], {})}
    if meaningful:
        return [meaningful]
    return []


def normalize_property(req: CheckRequest) -> dict:
    cad = clean_text(req.cadastral_number or req.cadastre_number)
    address = clean_text(req.address)
    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"

    if cad and re.match(cad_pattern, cad):
        # NewDB rosreestr по их ошибке требует именно поле address, даже если туда передается кадастровый номер.
        return {"type": "cadastral", "query": cad, "address_for_newdb": cad, "cadastral_number": cad, "address": ""}

    if address and re.match(cad_pattern, address):
        return {"type": "cadastral", "query": address, "address_for_newdb": address, "cadastral_number": address, "address": ""}

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

    return {"type": "address", "query": address, "address_for_newdb": address, "cadastral_number": "", "address": address}


async def newdb_post(params: dict, max_wait: int = 60, poll_interval: int = 4) -> dict:
    if not NEWDB_TOKEN:
        return {"state": "manual", "error": "NEWDB_TOKEN не задан в Environment на Render"}

    # NewDB в разных примерах встречается и с Bearer, и с X-API-KEY. Передаем оба, это безопасно.
    headers = {
        "Authorization": f"Bearer {NEWDB_TOKEN}",
        "X-API-KEY": NEWDB_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"params": params, "requestId": str(uuid.uuid4())}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            first.raise_for_status()
            data = first.json()
        except Exception as e:
            return {"state": "manual", "error": f"Ошибка запроса NewDB: {e}", "method": params.get("method")}

        if has_api_error(data):
            data["state"] = "failed"
            return data

        state = get_state(data)
        if state and state not in PENDING_STATES:
            return data

        request_id = extract_request_id(data)
        if not request_id:
            # Иногда API возвращает ответ без state/requestId — отдаем как есть, классификатор сам решит.
            return data

        elapsed = 0
        last_data = data
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            candidates = [
                f"{NEWDB_URL}/{request_id}",
                f"{NEWDB_URL}/result/{request_id}",
                f"{NEWDB_URL}?requestId={request_id}",
            ]

            for url in candidates:
                try:
                    r = await client.get(url, headers=headers)
                    if r.status_code >= 400:
                        continue
                    candidate_data = r.json()
                    last_data = candidate_data

                    if has_api_error(candidate_data):
                        candidate_data["state"] = "failed"
                        return candidate_data

                    st = get_state(candidate_data)
                    if st and st not in PENDING_STATES:
                        return candidate_data

                    # Если появились полезные данные — можно возвращать.
                    if extract_items(candidate_data):
                        return candidate_data
                except Exception:
                    continue

        return {
            "state": "timeout",
            "error": f"Источник не вернул итоговый результат за {max_wait} секунд",
            "first_response": data,
            "last_response": last_data,
            "method": params.get("method"),
        }


def manual_result(title: str, summary: str, url: str, details: list[str] | None = None) -> dict:
    return {
        "title": title,
        "status": "manual",
        "summary": summary,
        "details": details or [],
        "manual_check_url": url,
    }


def ok_result(title: str, summary: str, url: str, details: list[str] | None = None) -> dict:
    return {"title": title, "status": "ok", "summary": summary, "details": details or [], "manual_check_url": url}


def risk_result(title: str, summary: str, url: str, details: list[str] | None = None) -> dict:
    return {"title": title, "status": "risk", "summary": summary, "details": details or [], "manual_check_url": url}


def classify_common_failure(data: dict, title: str, url: str) -> dict | None:
    state = get_state(data)
    if state == "manual" or state == "timeout":
        return manual_result(title, "Не удалось автоматически получить итоговый результат. Требуется ручная проверка.", url, [clean_text(data.get("error"))])
    if state in PENDING_STATES:
        return manual_result(title, "Источник еще не вернул итоговый результат. Требуется повторить проверку или проверить вручную.", url, [f"Статус источника: {state}"])
    if has_api_error(data):
        return manual_result(title, "Источник проверки вернул ошибку. Требуется ручная проверка.", url, [api_error_message(data)])
    return None


def classify_passport(data: dict) -> dict:
    title = "Паспорт МВД"
    url = DEFAULT_MANUAL_LINKS["passport"]
    common = classify_common_failure(data, title, url)
    if common:
        return common

    text = response_text(data)
    if any(x in text for x in ["недейств", "разыскивается", "invalid"]):
        return risk_result(title, "Выявлены признаки проблемы с паспортом.", url, flatten_strings(data, 8))
    if any(x in text for x in ["действителен", "действительный", "valid"]):
        return ok_result(title, "Паспорт по полученным данным действителен.", url)
    if any(x in text for x in ["не найден", "нет свед", "not found"]):
        return manual_result(title, "Данные по паспорту не найдены. Требуется ручная проверка.", url, ["Источник не подтвердил паспорт автоматически."])
    return manual_result(title, "Результат проверки паспорта неоднозначный. Требуется ручная проверка.", url, ["Источник вернул ответ, который нельзя уверенно трактовать автоматически."])


def item_status_text(item: Any) -> str:
    return " ".join(flatten_strings(item, 30)).lower()


def extract_amount(item: Any) -> float:
    text = " ".join(flatten_strings(item, 40))
    amounts: list[float] = []
    for m in re.findall(r"(\d[\d\s]{0,12}(?:[,.]\d{1,2})?)\s*(?:руб|₽)", text, flags=re.I):
        try:
            amounts.append(float(m.replace(" ", "").replace(",", ".")))
        except Exception:
            pass
    if isinstance(item, dict):
        for key in ["amount", "sum", "debt", "debt_sum", "total", "balance", "rest"]:
            if key in item:
                try:
                    amounts.append(float(str(item[key]).replace(" ", "").replace(",", ".")))
                except Exception:
                    pass
    return max(amounts) if amounts else 0.0


def classify_fssp(data: dict) -> dict:
    title = "ФССП"
    url = DEFAULT_MANUAL_LINKS["fssp"]
    common = classify_common_failure(data, title, url)
    if common:
        return common

    items = extract_items(data)
    text = response_text(data)

    if not items and any(x in text for x in ["не найден", "нет свед", "nothing found", "not found", "отсутств"]):
        return ok_result(title, "По полученным данным активные исполнительные производства не найдены.", url)

    if not items:
        return manual_result(title, "Источник не вернул понятные записи ФССП. Требуется ручная проверка.", url)

    active_items = []
    closed_items = []
    unknown_items = []
    closed_words = ["оконч", "прекращ", "закрыт", "заверш", "ст. 46", "статья 46", "closed", "terminated"]
    active_words = ["возбужден", "актив", "исполнительное производство", "задолженность", "остаток", "взыскание", "active"]

    for item in items:
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
        return risk_result(title, "Выявлены активные исполнительные производства или признаки актуальной задолженности.", url, details)
    if closed_items and not active_items:
        return ok_result(title, "Найдены закрытые/оконченные ИП. По полученным данным активный долг не подтвержден.", url, details)
    if unknown_items:
        return manual_result(title, "Найдены неоднозначные записи ФССП. Нужно проверить статус вручную.", url, details)
    return ok_result(title, "По полученным данным исполнительные производства не найдены.", url)


def classify_empty_or_risk(data: dict, title: str, risk_summary: str, ok_summary: str, manual_url: str) -> dict:
    common = classify_common_failure(data, title, manual_url)
    if common:
        return common

    items = extract_items(data)
    text = response_text(data)
    negative_markers = ["не найден", "нет свед", "ничего не найдено", "not found", "empty", "отсутств", "no data"]

    if not items and any(x in text for x in negative_markers):
        return ok_result(title, ok_summary, manual_url)
    if not items:
        return ok_result(title, ok_summary, manual_url)

    return risk_result(title, risk_summary, manual_url, flatten_strings(items, 10))


def classify_egrn(data: dict, prop: dict) -> dict:
    title = "ЕГРН / Росреестр"
    url = DEFAULT_MANUAL_LINKS["egrn"]

    if not prop.get("address_for_newdb"):
        return manual_result(title, "Для проверки ЕГРН не передан адрес или кадастровый номер.", url, ["Заполните кадастровый номер или адрес объекта."])

    common = classify_common_failure(data, title, url)
    if common:
        # Уточняем текст именно для ЕГРН, не размазывая эту ошибку на другие источники.
        common["details"] = [f"Запрос: {prop.get('query')}"] + (common.get("details") or [])
        return common

    state = get_state(data)
    if state in PENDING_STATES:
        return manual_result(title, "Росреестр еще не вернул итоговый результат. Повторите проверку позже или проверьте вручную.", url, [f"Запрос: {prop.get('query')}", f"Статус: {state}"])

    items = extract_items(data)
    text_values = flatten_strings(items or data, 40)
    text = " ".join(text_values).lower()

    if not items and any(x in response_text(data) for x in ["не найден", "нет свед", "not found", "объект не найден"]):
        return manual_result(title, "Объект не найден автоматически или источник не вернул понятный результат. Требуется ручная проверка.", url, [f"Запрос: {prop.get('query')}"])
    if not items:
        return manual_result(title, "Росреестр не вернул данные объекта в понятном виде. Требуется ручная проверка.", url, [f"Запрос: {prop.get('query')}"])

    risk_words = ["арест", "запрет", "ограничение", "ипотека", "залог", "обременение", "рента"]
    risks = [w for w in risk_words if w in text]

    details = []
    if prop.get("cadastral_number"):
        details.append(f"Кадастровый номер: {prop.get('cadastral_number')}")
    if prop.get("address"):
        details.append(f"Адрес: {prop.get('address')}")
    details.extend(text_values[:12])

    if risks:
        return risk_result(title, "В данных по объекту выявлены признаки ограничений, обременений или иных рисков.", url, details)
    return ok_result(title, "Данные по объекту получены. Явные признаки ограничений или обременений в полученном результате не выявлены.", url, details)


def make_registry_results(checklist: list[dict]) -> dict:
    mapping = {
        "Паспорт МВД": "passport",
        "ФССП": "fssp",
        "Банкротство / Федресурс": "bankruptcy",
        "Залоги движимого имущества": "pledges",
        "Суды / арбитраж": "courts",
        "ЕГРН / Росреестр": "egrn",
    }
    return {
        mapping.get(item["title"], item["title"]): {
            "title": item["title"],
            "status": item["status"],
            "summary": item["summary"],
            "details": item.get("details", []),
            "manual_check_url": item.get("manual_check_url", ""),
        }
        for item in checklist
    }


def build_fallback_legal_report(req: CheckRequest, checklist: list[dict]) -> str:
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

    seller_titles = ["Паспорт МВД", "ФССП", "Банкротство / Федресурс", "Залоги движимого имущества", "Суды / арбитраж"]
    for item in [x for x in checklist if x["title"] in seller_titles]:
        lines.append(f"- {item['title']}: {item['summary']}")

    lines.extend(["", "4. Риски по объекту"])
    egrn = next((x for x in checklist if x["title"] == "ЕГРН / Росреестр"), None)
    lines.append(f"- {egrn['title']}: {egrn['summary']}" if egrn else "- ЕГРН / Росреестр: по предоставленным данным не проверялось.")

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
        lines.append("- Перед авансом нужно сверить оригиналы документов, актуальную ЕГРН, основание права, семейное положение продавца и отсутствие ограничений.")

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


async def build_gigachat_report(req: CheckRequest, checklist: list[dict], registry_results: dict) -> str:
    if not GIGACHAT_TOKEN:
        return build_fallback_legal_report(req, checklist)

    prompt = f"""
Ты юрист-эксперт по недвижимости в Санкт-Петербурге.
На основе переданных нормализованных данных сформируй подробный юридический отчет для покупателя недвижимости.

Строго соблюдай структуру:
1. Краткий вывод
2. Что проверено
3. Риски по продавцу
4. Риски по объекту
5. Что говорит в пользу сделки
6. Что обязательно проверить до аванса
7. Что прописать в авансовом соглашении / ПДКП
8. Безопасная схема расчетов
9. Итоговое заключение

Правила:
- Не придумывай факты.
- Если данных нет — прямо пиши: "по предоставленным данным не проверялось".
- Не обещай 100% безопасность.
- Не называй закрытые ИП активным долгом.
- Ошибка API, queued, pending или timeout — это не риск и не положительная проверка, а ручная проверка.
- Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.

Данные продавца:
{json.dumps(req.model_dump(), ensure_ascii=False, indent=2)}

Чек-лист:
{json.dumps(checklist, ensure_ascii=False, indent=2)}

Данные реестров:
{json.dumps(registry_results, ensure_ascii=False, indent=2)}
""".strip()

    headers = {"Authorization": f"Bearer {GIGACHAT_TOKEN}", "Content-Type": "application/json", "Accept": "application/json"}
    body = {
        "model": "GigaChat",
        "messages": [
            {"role": "system", "content": "Ты юридический эксперт по сделкам с недвижимостью."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False) as client:
            r = await client.post(GIGACHAT_URL, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return build_fallback_legal_report(req, checklist)


def generate_pdf(report_id: str, checklist: list[dict], registry_results: dict, legal_report: str) -> Path:
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

    def esc(s: Any) -> str:
        return clean_text(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = [
        Paragraph("Юридический отчет по проверке недвижимости", title_style),
        Paragraph(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", p_style),
        Spacer(1, 8),
        Paragraph("1. Данные, полученные из реестров", h_style),
    ]

    for item in registry_results.values():
        status_label = {"ok": "Проверено — риск не выявлен", "manual": "Требуется ручная проверка", "risk": "Выявлены риски"}.get(item.get("status"), "Статус не определен")
        story.append(Paragraph(f"<b>{esc(item.get('title'))}</b>", p_style))
        story.append(Paragraph(f"{esc(status_label)}. {esc(item.get('summary'))}", p_style))
        for d in item.get("details", [])[:10]:
            if d:
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


@app.get("/")
async def root():
    return {"ok": True, "service": "Real Estate Legal Check API", "newdb_configured": bool(NEWDB_TOKEN), "gigachat_configured": bool(GIGACHAT_TOKEN)}


@app.post("/check-report")
async def check_report(req: CheckRequest, request: Request):
    prop = normalize_property(req)
    dob = normalize_dob(req.dob)
    passport_series = only_digits(req.passport_series)
    passport_number = only_digits(req.passport_number)
    inn = only_digits(req.inn)

    passport_params = {
        "method": "passport_mvd",
        "series": passport_series,
        "seria": passport_series,
        "number": passport_number,
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob,
        "dob": dob,
        "country": "ru",
    }
    fssp_params = {
        "method": "fssp",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob,
        "dob": dob,
        "region": req.region,
        "country": "ru",
    }
    bankruptcy_params = {
        "method": "bankruptcy",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob,
        "dob": dob,
        "inn": inn,
        "country": "ru",
    }
    pledges_params = {
        "method": "pledges",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob,
        "dob": dob,
        "inn": inn,
        "country": "ru",
    }
    courts_params = {
        "method": "courts",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "secondname": req.middle,
        "birthdate": dob,
        "dob": dob,
        "inn": inn,
        "country": "ru",
    }
    egrn_params = {"method": "rosreestr", "address": prop.get("address_for_newdb", ""), "country": "ru"}

    passport_raw, fssp_raw, bankruptcy_raw, pledges_raw, courts_raw, egrn_raw = await asyncio.gather(
        newdb_post(passport_params, max_wait=45, poll_interval=3),
        newdb_post(fssp_params, max_wait=70, poll_interval=4),
        newdb_post(bankruptcy_params, max_wait=70, poll_interval=4),
        newdb_post(pledges_params, max_wait=70, poll_interval=4),
        newdb_post(courts_params, max_wait=70, poll_interval=4),
        newdb_post(egrn_params, max_wait=240, poll_interval=5),
    )

    checklist = [
        classify_passport(passport_raw),
        classify_fssp(fssp_raw),
        classify_empty_or_risk(bankruptcy_raw, "Банкротство / Федресурс", "Выявлены сведения, похожие на банкротство или публикации Федресурса.", "По полученным данным сведения о банкротстве не найдены.", DEFAULT_MANUAL_LINKS["bankruptcy"]),
        classify_empty_or_risk(pledges_raw, "Залоги движимого имущества", "Выявлены сведения о залогах. Требуется оценить предмет залога и связь с продавцом.", "По полученным данным сведения о залогах не найдены.", DEFAULT_MANUAL_LINKS["pledges"]),
        classify_empty_or_risk(courts_raw, "Суды / арбитраж", "Выявлены судебные/арбитражные сведения. Требуется оценить связь с продавцом и сделкой.", "По полученным данным релевантные судебные сведения не найдены.", DEFAULT_MANUAL_LINKS["courts"]),
        classify_egrn(egrn_raw, prop),
    ]

    registry_results = make_registry_results(checklist)
    legal_report = await build_gigachat_report(req, checklist, registry_results)
    report_id = str(uuid.uuid4())
    generate_pdf(report_id, checklist, registry_results, legal_report)

    base_url = str(request.base_url).rstrip("/")
    return {
        "report_id": report_id,
        "registry_results": registry_results,
        "checklist": checklist,
        "legal_report": legal_report,
        "pdf_url": f"{base_url}/download-pdf/{report_id}",
    }


@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    if not re.match(r"^[a-f0-9-]{36}$", report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    path = REPORT_DIR / f"{report_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(path=str(path), media_type="application/pdf", filename=f"legal-real-estate-report-{report_id}.pdf")
