from fastapi import FastAPI, HTTPException
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
NEWDB_URL = "https://api.newdb.net/v2"

GIGACHAT_TOKEN = os.getenv("GIGACHAT_TOKEN", "").strip()
GIGACHAT_URL = os.getenv("GIGACHAT_URL", "https://gigachat.devices.sberbank.ru/api/v1/chat/completions").strip()

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=20.0, read=180.0, write=40.0, pool=20.0)

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


def rub(value) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".00", "") + " ₽"
    except Exception:
        return "0 ₽"


def is_empty_data(data) -> bool:
    if data is None:
        return True
    if data in ("", [], {}, "null", "None"):
        return True
    if isinstance(data, dict):
        meaningful = {k: v for k, v in data.items() if k not in {"state", "requestId", "balance", "datecreated"}}
        return not meaningful
    return False


def flatten_strings(obj, limit=80):
    out = []

    def walk(x):
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
            if s and len(s) <= 300:
                out.append(s)

    walk(obj)
    return out


def normalize_property(address: str, cadastral_number: str) -> dict:
    cadastral_number = clean_text(cadastral_number)
    address = clean_text(address)

    cad_pattern = r"^\d{2}:\d{2}:\d{6,7}:\d+$"
    if cadastral_number and re.match(cad_pattern, cadastral_number):
        return {"type": "cadastral", "query": cadastral_number, "cadastral_number": cadastral_number, "address": ""}

    if address and re.match(cad_pattern, address):
        return {"type": "cadastral", "query": address, "cadastral_number": address, "address": ""}

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

    return {"type": "address", "query": address, "cadastral_number": "", "address": address}


async def newdb_post(params: dict, max_wait: int = 60, poll_interval: int = 3) -> dict:
    """
    Универсальный запрос к NewDB.
    ВАЖНО: метод сделан терпимым к разным форматам ответа:
    - готовый ответ сразу;
    - requestId + state in progress;
    - task_id/qid/id для повторного polling.
    """
    if not NEWDB_TOKEN:
        return {"state": "manual", "error": "NEWDB_TOKEN не задан"}

    headers = {
        "Authorization": f"Bearer {NEWDB_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {"params": params}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            first.raise_for_status()
            data = first.json()
        except Exception as e:
            return {"state": "manual", "error": f"Ошибка запроса NewDB: {e}"}

        state = str(data.get("state", "")).lower()
        if state not in {"in progress", "progress", "pending", "processing"}:
            return data

        request_id = data.get("requestId") or data.get("request_id") or data.get("id") or data.get("qid") or data.get("newdb_qid")
        if not request_id:
            return data

        elapsed = 0
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                # В разных аккаунтах NewDB endpoint результата может отличаться.
                # Поэтому пробуем несколько безопасных вариантов.
                candidates = [
                    f"{NEWDB_URL}/{request_id}",
                    f"{NEWDB_URL}/result/{request_id}",
                    f"{NEWDB_URL}?requestId={request_id}",
                ]

                last_data = None
                for url in candidates:
                    try:
                        r = await client.get(url, headers=headers)
                        if r.status_code < 400:
                            last_data = r.json()
                            break
                    except Exception:
                        continue

                if not last_data:
                    continue

                state = str(last_data.get("state", "")).lower()
                if state not in {"in progress", "progress", "pending", "processing"}:
                    return last_data

            except Exception:
                continue

        data["state"] = "timeout"
        data["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд"
        return data


def extract_items(data) -> list:
    if not isinstance(data, dict):
        return []
    for key in ["items", "data", "result", "results", "records", "list", "rows", "tasks"]:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested
    return []


def classify_passport(data) -> dict:
    text = " ".join(flatten_strings(data)).lower()

    if data.get("state") in {"manual", "timeout"} or data.get("error"):
        return {
            "title": "Паспорт МВД",
            "status": "manual",
            "summary": "Не удалось автоматически получить результат. Требуется ручная проверка.",
            "details": [clean_text(data.get("error"))] if data.get("error") else [],
            "manual_check_url": DEFAULT_MANUAL_LINKS["passport"],
        }

    if any(x in text for x in ["действителен", "valid", "действительный"]):
        return {
            "title": "Паспорт МВД",
            "status": "ok",
            "summary": "Паспорт по полученным данным действителен.",
            "details": [],
            "manual_check_url": DEFAULT_MANUAL_LINKS["passport"],
        }

    if any(x in text for x in ["недейств", "разыскивается", "invalid"]):
        return {
            "title": "Паспорт МВД",
            "status": "risk",
            "summary": "Выявлены признаки проблемы с паспортом.",
            "details": flatten_strings(data, 8),
            "manual_check_url": DEFAULT_MANUAL_LINKS["passport"],
        }

    return {
        "title": "Паспорт МВД",
        "status": "manual",
        "summary": "Результат проверки паспорта неоднозначный. Требуется ручная проверка.",
        "details": flatten_strings(data, 5),
        "manual_check_url": DEFAULT_MANUAL_LINKS["passport"],
    }


def item_status_text(item) -> str:
    text = " ".join(flatten_strings(item, 20)).lower()
    return text


def extract_amount(item) -> float:
    text = " ".join(flatten_strings(item, 20))
    matches = re.findall(r"(\d+[.,]?\d*)\s*(?:руб|₽)", text, flags=re.I)
    amounts = []
    for m in matches:
        try:
            amounts.append(float(m.replace(",", ".")))
        except Exception:
            pass

    if amounts:
        return max(amounts)

    if isinstance(item, dict):
        for key in ["amount", "sum", "debt", "debt_sum", "total", "balance"]:
            if key in item:
                try:
                    return float(str(item[key]).replace(",", "."))
                except Exception:
                    pass
    return 0.0


def classify_fssp(data) -> dict:
    if data.get("state") in {"manual", "timeout"} or data.get("error"):
        return {
            "title": "ФССП",
            "status": "manual",
            "summary": "Не удалось автоматически получить результат. Требуется ручная проверка.",
            "details": [clean_text(data.get("error"))] if data.get("error") else [],
            "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
        }

    items = extract_items(data)
    text = " ".join(flatten_strings(data)).lower()

    if not items and any(x in text for x in ["не найден", "нет свед", "nothing found", "not found"]):
        return {
            "title": "ФССП",
            "status": "ok",
            "summary": "По полученным данным активные исполнительные производства не найдены.",
            "details": [],
            "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
        }

    active_items = []
    closed_items = []
    unknown_items = []

    closed_words = ["оконч", "прекращ", "закрыт", "заверш", "ст. 46", "статья 46"]
    active_words = ["возбужден", "актив", "исполнительное производство", "задолженность", "остаток"]

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
        f"Сумма по закрытым/оконченных ИП: {rub(closed_total)}",
    ]

    if active_items or active_total > 0:
        return {
            "title": "ФССП",
            "status": "risk",
            "summary": "Выявлены активные исполнительные производства или признаки актуальной задолженности.",
            "details": details,
            "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
        }

    if closed_items and not active_items:
        return {
            "title": "ФССП",
            "status": "ok",
            "summary": "Найдены закрытые/оконченные ИП. По полученным данным активный долг не подтвержден.",
            "details": details,
            "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
        }

    if unknown_items:
        return {
            "title": "ФССП",
            "status": "manual",
            "summary": "Найдены неоднозначные записи ФССП. Нужно проверить статус вручную.",
            "details": details,
            "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
        }

    if not items:
        return {
            "title": "ФССП",
            "status": "ok",
            "summary": "По полученным данным исполнительные производства не найдены.",
            "details": [],
            "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
        }

    return {
        "title": "ФССП",
        "status": "manual",
        "summary": "Результат ФССП неоднозначный. Требуется ручная проверка.",
        "details": flatten_strings(data, 6),
        "manual_check_url": DEFAULT_MANUAL_LINKS["fssp"],
    }


def classify_empty_or_risk(data, title: str, risk_summary: str, ok_summary: str, manual_url: str) -> dict:
    if data.get("state") in {"manual", "timeout"} or data.get("error"):
        return {
            "title": title,
            "status": "manual",
            "summary": "Не удалось автоматически получить результат. Требуется ручная проверка.",
            "details": [clean_text(data.get("error"))] if data.get("error") else [],
            "manual_check_url": manual_url,
        }

    items = extract_items(data)
    text = " ".join(flatten_strings(data)).lower()

    negative_markers = ["не найден", "нет свед", "ничего не найдено", "not found", "empty", "отсутств"]
    if not items and (is_empty_data(data) or any(x in text for x in negative_markers)):
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
        return {
            "title": title,
            "status": "manual",
            "summary": "Источник вернул неоднозначные сведения. Нужна ручная оценка релевантности записей.",
            "details": flatten_strings(data, 8),
            "manual_check_url": manual_url,
        }

    return {
        "title": title,
        "status": "ok",
        "summary": ok_summary,
        "details": [],
        "manual_check_url": manual_url,
    }


def classify_egrn(data, prop: dict) -> dict:
    if data.get("state") in {"manual", "timeout"} or data.get("error"):
        return {
            "title": "ЕГРН / Росреестр",
            "status": "manual",
            "summary": "Автоматически получить данные ЕГРН не удалось. Требуется ручная проверка.",
            "details": [
                f"Запрос: {prop.get('query')}",
                clean_text(data.get("error")) or "Росреестр мог не вернуть результат за время ожидания.",
            ],
            "manual_check_url": DEFAULT_MANUAL_LINKS["egrn"],
        }

    text_values = flatten_strings(data, 40)
    text = " ".join(text_values).lower()

    if is_empty_data(data) or any(x in text for x in ["не найден", "нет свед", "not found"]):
        return {
            "title": "ЕГРН / Росреестр",
            "status": "manual",
            "summary": "Объект не найден автоматически или источник не вернул понятный результат. Требуется ручная проверка.",
            "details": [f"Запрос: {prop.get('query')}"],
            "manual_check_url": DEFAULT_MANUAL_LINKS["egrn"],
        }

    risk_words = ["арест", "запрет", "ограничение", "ипотека", "залог", "обременение", "рента"]
    risks = [w for w in risk_words if w in text]

    details = []
    if prop.get("cadastral_number"):
        details.append(f"Кадастровый номер: {prop.get('cadastral_number')}")
    if prop.get("address"):
        details.append(f"Адрес: {prop.get('address')}")
    details.extend(text_values[:12])

    if risks:
        return {
            "title": "ЕГРН / Росреестр",
            "status": "risk",
            "summary": "В данных по объекту выявлены признаки ограничений, обременений или иных рисков.",
            "details": details,
            "manual_check_url": DEFAULT_MANUAL_LINKS["egrn"],
        }

    return {
        "title": "ЕГРН / Росреестр",
        "status": "ok",
        "summary": "Данные по объекту получены. Явные признаки ограничений или обременений в полученном результате не выявлены.",
        "details": details,
        "manual_check_url": DEFAULT_MANUAL_LINKS["egrn"],
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


def build_fallback_legal_report(req: CheckRequest, checklist: list) -> str:
    risks = [x for x in checklist if x["status"] == "risk"]
    manuals = [x for x in checklist if x["status"] == "manual"]
    oks = [x for x in checklist if x["status"] == "ok"]

    seller = " ".join([req.last, req.first, req.middle]).strip()
    obj = req.cadastral_number or req.address or "по предоставленным данным не указан"

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

    for item in manuals:
        lines.append(f"- {item['title']}: требуется ручная проверка. Ссылка: {item.get('manual_check_url','')}")

    if not manuals:
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


async def build_gigachat_report(req: CheckRequest, checklist: list, registry_results: dict) -> str:
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
- Ошибка API или timeout — это не риск, а ручная проверка.
- Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.

Данные продавца:
{json.dumps(req.model_dump(), ensure_ascii=False, indent=2)}

Чек-лист:
{json.dumps(checklist, ensure_ascii=False, indent=2)}

Данные реестров:
{json.dumps(registry_results, ensure_ascii=False, indent=2)}
""".strip()

    headers = {
        "Authorization": f"Bearer {GIGACHAT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

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


def generate_pdf(report_id: str, req: CheckRequest, checklist: list, registry_results: dict, legal_report: str) -> Path:
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


@app.get("/")
async def root():
    return {"ok": True, "service": "Real Estate Legal Check API"}


@app.post("/check-report")
async def check_report(req: CheckRequest):
    prop = normalize_property(req.address, req.cadastral_number)

    # Методы NewDB могут отличаться по тарифу/аккаунту.
    # Если конкретное имя метода у тебя в Swagger другое — замени только method.
    passport_params = {
        "method": "passport_mvd",
        "series": req.passport_series,
        "seria": req.passport_series,
        "number": req.passport_number,
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "birthdate": req.dob,
    }

    fssp_params = {
        "method": "fssp",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "birthdate": req.dob,
        "region": req.region,
    }

    bankruptcy_params = {
        "method": "bankruptcy",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "birthdate": req.dob,
        "inn": req.inn,
    }

    pledges_params = {
        "method": "pledges",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "birthdate": req.dob,
        "inn": req.inn,
    }

    courts_params = {
        "method": "courts",
        "lastname": req.last,
        "firstname": req.first,
        "middlename": req.middle,
        "birthdate": req.dob,
        "inn": req.inn,
    }

    egrn_params = {
        "method": "rosreestr",
        "address": prop["query"],
        "country": "ru",
    }

    passport_raw, fssp_raw, bankruptcy_raw, pledges_raw, courts_raw, egrn_raw = await asyncio.gather(
        newdb_post(passport_params, max_wait=45, poll_interval=3),
        newdb_post(fssp_params, max_wait=60, poll_interval=3),
        newdb_post(bankruptcy_params, max_wait=60, poll_interval=3),
        newdb_post(pledges_params, max_wait=60, poll_interval=3),
        newdb_post(courts_params, max_wait=60, poll_interval=3),
        newdb_post(egrn_params, max_wait=180, poll_interval=5),  # Росреестр ждем дольше
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
    legal_report = await build_gigachat_report(req, checklist, registry_results)

    report_id = str(uuid.uuid4())
    generate_pdf(report_id, req, checklist, registry_results, legal_report)

    return {
        "report_id": report_id,
        "registry_results": registry_results,
        "checklist": checklist,
        "legal_report": legal_report,
        "pdf_url": f"/download-pdf/{report_id}",
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
