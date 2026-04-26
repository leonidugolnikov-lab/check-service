
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx, asyncio, uuid, os, re, json, base64, html
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:
    SimpleDocTemplate = None

app = FastAPI(title="Real Estate Seller & Property Check API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
NEWDB_URL = os.getenv("NEWDB_URL", "https://api.newdb.net/v2").strip()

GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
GIGACHAT_VERIFY_SSL_CERTS = os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "false").lower() in {"1", "true", "yes"}

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT = httpx.Timeout(connect=20.0, read=360.0, write=60.0, pool=30.0)

MANUAL_LINKS = {
    "passport": "https://мвд.рф/сервисы-гувм",
    "fssp": "https://fssp.gov.ru/iss/ip",
    "bankruptcy": "https://bankrot.fedresurs.ru",
    "courts": "https://kad.arbitr.ru",
    "egrn": "https://rosreestr.gov.ru",
}

DISCLAIMER = (
    "Отчет носит информационно-аналитический характер, не является гарантией полной юридической "
    "безопасности сделки и не заменяет ручную юридическую проверку документов специалистом."
)

# --------------------------
# Basic helpers
# --------------------------

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        # common mojibake fix for cp1251/utf-8 issues
        if "Р" in text or "С" in text:
            fixed = text.encode("latin1").decode("utf-8")
            if fixed:
                text = fixed
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()

def only_digits(value: Any) -> str:
    return re.sub(r"\D+", "", clean_text(value))

def money(value: Any) -> str:
    try:
        v = float(str(value).replace(" ", "").replace(",", "."))
        s = f"{v:,.2f}".replace(",", " ").replace(".00", "")
        return f"{s} ₽"
    except Exception:
        return "0 ₽"

def parse_money(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value).replace("\xa0", " ")
    nums = re.findall(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:руб|₽)?", text, flags=re.I)
    values = []
    for n in nums:
        try:
            values.append(float(n.replace(" ", "").replace(",", ".")))
        except Exception:
            pass
    return max(values) if values else 0.0

def dob_to_iso(value: Any) -> str:
    v = clean_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", v)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return v

def dob_to_ru(value: Any) -> str:
    v = clean_text(value)
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v):
        return v
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return v

def text_blob(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        return str(obj).lower()

def strip_private(obj: Any) -> Any:
    # for client/report output: remove request ids, balance, raw service fields
    if isinstance(obj, dict):
        out = {}
        forbidden = {
            "requestId", "request_id", "newdb_qid", "qid", "balance",
            "token", "api_key", "authorization", "x-api-key",
            "taskId", "datecreated", "dateupdated", "_http_status",
            "params", "sent_params", "docs_url", "errors_info"
        }
        for k, v in obj.items():
            if k in forbidden:
                continue
            out[k] = strip_private(v)
        return out
    if isinstance(obj, list):
        return [strip_private(x) for x in obj]
    return obj

def get_any(data: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k in data and clean_text(data.get(k)):
            return clean_text(data.get(k))
    return ""

def normalize_input(raw: Dict[str, Any]) -> Dict[str, Any]:
    # Accept many frontend field names to stop losing INN/passport
    inn = only_digits(
        raw.get("inn")
        or raw.get("seller_inn")
        or raw.get("inn_fiz")
        or raw.get("innfiz")
        or raw.get("innfl")
        or raw.get("sellerInn")
        or raw.get("tax_id")
    )
    passport_series = only_digits(
        raw.get("passport_series")
        or raw.get("passportSeria")
        or raw.get("passport_seria")
        or raw.get("passportSeries")
        or raw.get("seria")
        or raw.get("series")
    )
    passport_number = only_digits(
        raw.get("passport_number")
        or raw.get("passportNumber")
        or raw.get("number")
        or raw.get("passport_num")
    )
    cadastral = clean_text(raw.get("cadastral_number") or raw.get("cadastre_number") or raw.get("cadnum") or raw.get("cad_number"))
    address = clean_text(raw.get("address") or raw.get("property_address") or raw.get("propertyAddress"))
    if not cadastral and re.fullmatch(r"\d{2}:\d{2}:\d{6,7}:\d+", address):
        cadastral = address
    if not address and cadastral:
        address = cadastral
    if address and not cadastral and re.fullmatch(r"\d{2}:\d{2}:\d{6,7}:\d+", address):
        cadastral = address
    return {
        "last": clean_text(raw.get("last") or raw.get("lastname") or raw.get("surname")),
        "first": clean_text(raw.get("first") or raw.get("firstname") or raw.get("name")),
        "middle": clean_text(raw.get("middle") or raw.get("middlename") or raw.get("secondname") or raw.get("patronymic")),
        "dob": dob_to_ru(raw.get("dob") or raw.get("birthdate") or raw.get("birthday") or raw.get("date_birth")),
        "dob_iso": dob_to_iso(raw.get("dob") or raw.get("birthdate") or raw.get("birthday") or raw.get("date_birth")),
        "inn": inn,
        "region": int(raw.get("region") or raw.get("regioncode") or 78),
        "passport_series": passport_series,
        "passport_number": passport_number,
        "property": {
            "query": cadastral or address,
            "cadastral_number": cadastral,
            "address": address,
            "type": "cadastral" if cadastral else "address"
        },
    }

def checklist_item(title: str, status: str, summary: str, details: Optional[List[str]] = None,
                   manual_key: str = "", data: Any = None) -> Dict[str, Any]:
    ui_status = "manual" if status == "manual_check" else status
    item = {
        "title": title,
        "source": title,
        "status": status,
        "ui_status": ui_status,
        "summary": summary,
        "details": [clean_text(x) for x in (details or []) if clean_text(x)],
        "manual_check_url": MANUAL_LINKS.get(manual_key, ""),
        "manual_url": MANUAL_LINKS.get(manual_key, ""),
    }
    if data is not None:
        item["data"] = strip_private(data)
    return item

def result_data(response: Dict[str, Any], method: str) -> Tuple[Optional[int], Any]:
    try:
        r = response.get("results", {}).get(method, {}).get("result", {})
        return r.get("status"), r.get("data")
    except Exception:
        return None, None

def response_error(response: Dict[str, Any], method: str) -> str:
    try:
        r = response.get("results", {}).get(method, {}).get("result", {})
        if r.get("error"):
            return clean_text(r.get("error"))
    except Exception:
        pass
    if response.get("error"):
        return clean_text(response.get("error"))
    return ""

# --------------------------
# NewDB integration
# --------------------------

async def newdb_post(params: Dict[str, Any], max_wait: int = 120, poll_interval: int = 5) -> Dict[str, Any]:
    if not NEWDB_TOKEN:
        return {"state": "not_configured", "error": "NEWDB_TOKEN не задан."}

    request_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
    }
    payload = {"params": params, "requestId": request_id}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            first = await client.post(NEWDB_URL, headers=headers, json=payload)
            try:
                data = first.json()
            except Exception:
                data = {"raw_text": first.text}
            data["_http_status"] = first.status_code
        except Exception as e:
            return {"state": "error", "error": f"Ошибка запроса newDB: {e}"}

        if first.status_code >= 400:
            data["state"] = data.get("state") or "error"
            return data

        # poll by repeating same requestId top-level
        elapsed = 0
        last = data
        while elapsed < max_wait:
            state = clean_text(last.get("state")).lower()
            if state in {"complete", "completed", "done", "success", "finished", "ready"}:
                return last
            if state in {"error", "failed", "fail", "rejected", "timeout"}:
                return last

            # Some sources return result with data before state complete
            if isinstance(last.get("results"), dict) and state not in {"restart", "in progress", "processing", "pending", "queued", "queue", ""}:
                return last

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                r = await client.post(NEWDB_URL, headers=headers, json=payload)
                try:
                    last = r.json()
                except Exception:
                    last = {"raw_text": r.text}
                last["_http_status"] = r.status_code
                if r.status_code >= 400:
                    last["state"] = last.get("state") or "error"
                    return last
            except Exception as e:
                return {"state": "error", "error": f"Ошибка polling newDB: {e}"}

        last["state"] = "timeout"
        last["error"] = f"Источник не вернул итоговый результат за {max_wait} секунд."
        return last

def build_payloads(inp: Dict[str, Any]) -> Dict[str, Any]:
    first, last, middle = inp["first"], inp["last"], inp["middle"]
    dob_iso = inp["dob_iso"]
    inn = inp["inn"]
    prop = inp["property"]

    payloads = {}

    if inp["passport_series"] and inp["passport_number"]:
        payloads["passport"] = {
            "method": "passport_mvd",
            "country": "ru",
            "seria": inp["passport_series"],
            "number": inp["passport_number"],
            "firstname": first,
            "lastname": last,
        }
        if middle:
            payloads["passport"]["secondname"] = middle
    else:
        payloads["passport"] = None

    if first and last and dob_iso:
        payloads["fssp"] = {
            "method": "fssp_person",
            "country": "ru",
            "firstname": first,
            "lastname": last,
            "secondname": middle,
            "dob": dob_iso,
            "regioncode": inp["region"],
        }
    else:
        payloads["fssp"] = None

    if len(inn) == 12:
        payloads["bankruptcy"] = {
            "method": "bankrot_person",
            "country": "ru",
            "innfiz": inn,
        }
        payloads["courts"] = {
            "method": "arbitr_person",
            "country": "ru",
            "innfiz": inn,
        }
    else:
        payloads["bankruptcy"] = None
        payloads["courts"] = None

    if prop.get("address") or prop.get("cadastral_number"):
        payloads["egrn"] = {
            "method": "rosreestr",
            "country": "ru",
            "address": prop.get("address") or prop.get("cadastral_number"),
        }
    else:
        payloads["egrn"] = None

    return payloads

async def run_checks(inp: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    payloads = build_payloads(inp)
    responses = {}

    async def call(name, payload, wait):
        if not payload:
            return name, {"state": "skipped", "error": "Недостаточно входных данных для автоматической проверки."}
        return name, await newdb_post(payload, max_wait=wait)

    tasks = [
        call("passport", payloads["passport"], 90),
        call("fssp", payloads["fssp"], 150),
        call("bankruptcy", payloads["bankruptcy"], 150),
        call("courts", payloads["courts"], 150),
        call("egrn", payloads["egrn"], 360),
    ]
    for name, resp in await asyncio.gather(*tasks):
        responses[name] = resp
    return payloads, responses

# --------------------------
# Classifiers
# --------------------------

def classify_passport(inp: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    title = "Паспорт МВД"
    if not inp["passport_series"] or not inp["passport_number"]:
        return checklist_item(title, "manual_check", "Паспорт не проверялся автоматически: не переданы серия и номер паспорта.",
                              ["Для автоматической проверки МВД укажите серию и номер паспорта."], "passport")

    status, data = result_data(response, "passport_mvd")
    if status != 200:
        err = response_error(response, "passport_mvd")
        return checklist_item(title, "manual_check", "Источник не вернул данные по паспорту. Требуется ручная проверка.",
                              [err or "Источник не вернул понятный результат проверки паспорта."], "passport")

    txt = text_blob(data)
    details = []
    if isinstance(data, list) and data:
        for item in data[:3]:
            if isinstance(item, dict):
                ds = clean_text(item.get("doc_status") or item.get("status") or item.get("message"))
                if ds:
                    details.append(ds)

    if any(x in txt for x in ["недейств", "разыскивается", "invalid"]):
        return checklist_item(title, "risk", "По паспорту выявлены признаки недействительности или иной проблемы.",
                              details or ["Проверьте паспорт вручную до аванса."], "passport", data)

    if any(x in txt for x in ["действителен", "действительный", "valid"]):
        return checklist_item(title, "ok", "Паспорт по полученным данным действителен.", details, "passport", data)

    if any(x in txt for x in ["данные не найдены", "не найден"]):
        # For passport this is not 'ok'; it means manual check
        return checklist_item(title, "manual_check", "МВД не подтвердило паспорт автоматически. Требуется ручная проверка.",
                              details or ["Ответ источника: данные не найдены."], "passport", data)

    return checklist_item(title, "manual_check", "Результат проверки паспорта неоднозначный. Требуется ручная проверка.",
                          details or ["Источник вернул ответ, который нельзя уверенно трактовать автоматически."], "passport", data)

def classify_fssp(inp: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    title = "ФССП"
    if not (inp["first"] and inp["last"] and inp["dob_iso"]):
        return checklist_item(title, "manual_check", "ФССП не проверялся автоматически: не хватает ФИО или даты рождения.",
                              ["Для ФССП нужны ФИО, дата рождения и регион."], "fssp")

    status, data = result_data(response, "fssp_person")
    if status != 200:
        err = response_error(response, "fssp_person")
        return checklist_item(title, "manual_check", "Источник ФССП не вернул данные. Требуется ручная проверка.",
                              [err or "ФССП/newDB не вернул корректный результат."], "fssp")

    records = data if isinstance(data, list) else []
    closed_words = ["оконч", "прекращ", "закрыт", "заверш", "ст. 46", "статья 46", "terminated", "closed"]
    active_words = ["возбужден", "актив", "задолженность", "остаток", "взыскание", "active", "исполнительное производство"]

    active, closed, unknown = [], [], []
    for rec in records:
        t = text_blob(rec)
        if any(w in t for w in closed_words):
            closed.append(rec)
        elif any(w in t for w in active_words):
            active.append(rec)
        else:
            # if an item exists and is not clearly closed, safer to treat as active/unknown
            unknown.append(rec)

    def rec_amount(rec):
        # try keys first, then text
        if isinstance(rec, dict):
            for key in ["sum", "amount", "debt", "debt_sum", "total", "balance", "debtSum", "ip_sum"]:
                if key in rec:
                    v = parse_money(rec.get(key))
                    if v:
                        return v
        return parse_money(rec)

    active_sum = sum(rec_amount(x) for x in active)
    closed_sum = sum(rec_amount(x) for x in closed)
    unknown_sum = sum(rec_amount(x) for x in unknown)

    stats = {
        "all_count": len(records),
        "active_count": len(active),
        "closed_count": len(closed),
        "unknown_count": len(unknown),
        "total_sum_all": active_sum + closed_sum + unknown_sum,
        "active_sum": active_sum,
        "closed_sum": closed_sum,
        "unknown_sum": unknown_sum,
        "actual_debt": active_sum + unknown_sum if unknown else active_sum,
        "active_items": strip_private(active),
        "closed_items": strip_private(closed),
        "unknown_items": strip_private(unknown),
    }

    details = [
        f"Всего найдено ИП: {stats['all_count']}",
        f"Активные ИП: {stats['active_count']}",
        f"Закрытые/оконченные ИП: {stats['closed_count']}",
        f"Неоднозначные записи: {stats['unknown_count']}",
        f"Общая сумма всех найденных ИП: {money(stats['total_sum_all'])}",
        f"Сумма по активным ИП: {money(stats['active_sum'])}",
        f"Сумма по закрытым ИП: {money(stats['closed_sum'])}",
        f"Актуальный долг по активным/неоднозначным ИП: {money(stats['actual_debt'])}",
    ]

    if active or unknown:
        status_name = "risk" if active or unknown_sum else "manual_check"
        summary = f"Найдены активные или неоднозначные исполнительные производства. Актуальная сумма для ручной оценки: {money(stats['actual_debt'])}."
        item = checklist_item(title, status_name, summary, details, "fssp", stats)
    elif closed:
        item = checklist_item(title, "ok", "Найдены только закрытые/оконченные ИП. Актуальный долг по активным производствам не подтвержден.",
                              details, "fssp", stats)
    else:
        item = checklist_item(title, "ok", "По полученным данным исполнительные производства не найдены.",
                              ["ФССП искался по ФИО, дате рождения и региону. Если дата рождения или регион указаны неверно, ИП могут не попасть в автоматический ответ."],
                              "fssp", stats)
    item["fssp_stats"] = stats
    return item

def meaningful_records(data: Any, negative_keys: Optional[List[str]] = None) -> List[Any]:
    if not isinstance(data, list):
        return []
    out = []
    negative_keys = negative_keys or []
    for rec in data:
        if rec in (None, False, "", [], {}):
            continue
        if isinstance(rec, dict):
            # Common "no result" shapes
            txt = text_blob(rec)
            if any(x in txt for x in ["не найден", "ничего не найдено", "нет сведений", "not found", "no data"]):
                continue
            # Ignore entries that contain only input echoes, urls and boolean flags
            useful = {}
            for k, v in rec.items():
                kl = str(k).lower()
                if kl in {"inn", "innfiz", "url", "link", "source", "search_url", "found", "success"}:
                    continue
                if v in (None, False, "", [], {}):
                    continue
                useful[k] = v
            if useful:
                out.append(rec)
        else:
            out.append(rec)
    return out

def classify_bankruptcy(inp: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    title = "Банкротство / Федресурс"
    if len(inp["inn"]) != 12:
        return checklist_item(title, "manual_check", "Банкротство не проверялось автоматически: не передан 12-значный ИНН физлица.",
                              ["Укажите ИНН физлица из 12 цифр."], "bankruptcy")

    status, data = result_data(response, "bankrot_person")
    if status != 200:
        err = response_error(response, "bankrot_person")
        return checklist_item(title, "manual_check", "Источник банкротств не вернул данные. Требуется ручная проверка.",
                              [err or "Федресурс/newDB не вернул корректный результат."], "bankruptcy")

    records = meaningful_records(data)
    txt = text_blob(records)
    if records and any(x in txt for x in ["банкрот", "арбитраж", "дело", "процедур", "финансов", "сообщение", "номер"]):
        return checklist_item(title, "risk", "Выявлены сведения, связанные с банкротством.",
                              summarize_records(records, 6), "bankruptcy", records)

    if records:
        return checklist_item(title, "manual_check", "Источник вернул неоднозначные сведения по банкротству. Требуется ручная оценка.",
                              summarize_records(records, 6), "bankruptcy", records)

    return checklist_item(title, "ok", "По полученным данным сведения о банкротстве физлица не выявлены.",
                          [f"Проверка выполнена по ИНН физлица: {inp['inn']}"], "bankruptcy", data)

def classify_courts(inp: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    title = "Суды / арбитраж"
    if len(inp["inn"]) != 12:
        return checklist_item(title, "manual_check", "Суды не проверялись автоматически: не передан 12-значный ИНН физлица.",
                              ["Укажите ИНН физлица из 12 цифр."], "courts")

    status, data = result_data(response, "arbitr_person")
    if status != 200:
        err = response_error(response, "arbitr_person")
        return checklist_item(title, "manual_check", "Источник судебных дел не вернул данные. Требуется ручная проверка.",
                              [err or "КАД/newDB не вернул корректный результат."], "courts")

    records = meaningful_records(data)
    txt = text_blob(records)

    # If API returns only input echo and search URL, do not mark as risk
    if not records:
        return checklist_item(title, "ok", "По полученным данным арбитражные дела не выявлены.",
                              [f"Проверка выполнена по ИНН физлица: {inp['inn']}"], "courts", data)

    if any(x in txt for x in ["номер дела", "case", "kad", "истец", "ответчик", "суд", "дело", "а56-", "а40-", "claim"]):
        return checklist_item(title, "risk", "Найдены судебные производства. Требуется анализ предмета спора.",
                              summarize_records(records, 8), "courts", records)

    return checklist_item(title, "manual_check", "Источник вернул неоднозначные судебные сведения. Требуется ручная проверка.",
                          summarize_records(records, 6), "courts", records)

def classify_egrn(inp: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    title = "ЕГРН / Росреестр"
    prop = inp["property"]
    if not prop.get("query"):
        return checklist_item(title, "manual_check", "Не передан адрес или кадастровый номер объекта.",
                              ["Укажите кадастровый номер или адрес."], "egrn")

    status, data = result_data(response, "rosreestr")
    if status != 200 or not isinstance(data, list) or not data:
        err = response_error(response, "rosreestr")
        return checklist_item(title, "manual_check", "Источник ЕГРН не вернул реальные данные объекта. Требуется ручная проверка.",
                              [err or f"Запрос: {prop.get('query')}"], "egrn")

    obj = data[0]
    address = obj.get("address") if isinstance(obj.get("address"), dict) else {}
    readable = clean_text(address.get("readableAddress") or address.get("address"))
    enc = obj.get("encumbrances") if isinstance(obj.get("encumbrances"), list) else []
    rights = obj.get("rights") if isinstance(obj.get("rights"), list) else []

    details = [
        f"Кадастровый номер: {clean_text(obj.get('cadNumber') or prop.get('cadastral_number'))}",
        f"Адрес: {readable}" if readable else "",
        f"Тип объекта: {clean_text(obj.get('objType_text'))}" if obj.get("objType_text") else "",
        f"Назначение: {clean_text(obj.get('purpose_text'))}" if obj.get("purpose_text") else "",
        f"Площадь: {clean_text(obj.get('area'))} кв.м" if obj.get("area") else "",
        f"Кадастровая стоимость: {money(obj.get('cadCost'))}" if obj.get("cadCost") else "",
        f"Записей о правах: {len(rights)}",
    ]
    details = [d for d in details if d]

    enc_details = []
    for e in enc:
        desc = clean_text(e.get("typeDesc")) or f"Тип ограничения: {clean_text(e.get('type'))}"
        num = clean_text(e.get("encumbranceNumber"))
        start = clean_text(e.get("startDate"))
        line = desc
        if num:
            line += f", № {num}"
        if start:
            line += f", дата начала: {start}"
        enc_details.append(line)

    if enc:
        details.extend(enc_details)
        item = checklist_item(title, "risk", "Данные по объекту получены. Выявлены ограничения / обременения по ЕГРН.",
                              details, "egrn", obj)
    else:
        item = checklist_item(title, "ok", "Данные по объекту получены. Явные ограничения/обременения в ЕГРН не выявлены.",
                              details, "egrn", obj)
    item["egrn_object"] = strip_private(obj)
    return item

def summarize_records(records: List[Any], limit: int = 6) -> List[str]:
    lines = []
    for rec in records[:limit]:
        if isinstance(rec, dict):
            parts = []
            preferred = [
                "caseNumber", "case_number", "number", "case", "status", "stage",
                "date", "publicationDate", "court", "category", "role", "sum",
                "messageType", "debtor", "procedure"
            ]
            for k in preferred:
                if k in rec and rec.get(k) not in (None, "", [], {}):
                    parts.append(f"{k}: {clean_text(rec.get(k))}")
            if not parts:
                # take first meaningful fields, but skip pure URLs/False/INN
                for k, v in rec.items():
                    if str(k).lower() in {"inn", "innfiz", "url", "link", "found", "success"}:
                        continue
                    if v in (None, False, "", [], {}):
                        continue
                    parts.append(f"{k}: {clean_text(v)[:120]}")
                    if len(parts) >= 4:
                        break
            if parts:
                lines.append("; ".join(parts))
        else:
            s = clean_text(rec)
            if s and s.lower() not in {"false", "true"} and not s.startswith("http"):
                lines.append(s[:200])
    return lines or ["Источник вернул запись, требуется ручная оценка содержания."]

def classify_all(inp: Dict[str, Any], responses: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        classify_passport(inp, responses.get("passport", {})),
        classify_fssp(inp, responses.get("fssp", {})),
        classify_bankruptcy(inp, responses.get("bankruptcy", {})),
        classify_courts(inp, responses.get("courts", {})),
        classify_egrn(inp, responses.get("egrn", {})),
    ]

# --------------------------
# Scoring & recommendations
# --------------------------

def build_scoring(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    score = 0
    factors = []
    for item in checklist:
        title = item["title"]
        status = item["status"]
        if title.startswith("ЕГРН") and status == "risk":
            score += 60
            factors.append("ЕГРН: выявлены ограничения/обременения (+60)")
        elif title == "ФССП" and status == "risk":
            stats = item.get("fssp_stats", {})
            actual = float(stats.get("actual_debt") or 0)
            add = 25 if actual < 100000 else 35
            score += add
            factors.append(f"ФССП: активные/неоднозначные ИП, сумма {money(actual)} (+{add})")
        elif title.startswith("Банкротство") and status == "risk":
            score += 45
            factors.append("Федресурс: признаки банкротства (+45)")
        elif title.startswith("Суды") and status == "risk":
            score += 25
            factors.append("Суды: найдены судебные производства (+25)")
        elif title.startswith("Паспорт") and status == "risk":
            score += 50
            factors.append("Паспорт: признаки проблемы (+50)")
        elif status == "manual_check":
            add = 6 if title in {"Паспорт МВД", "Банкротство / Федресурс", "Суды / арбитраж"} else 4
            score += add
            factors.append(f"{title}: требуется ручная проверка (+{add})")

    score = min(100, int(score))
    if score >= 70:
        level = "опасная"
        conclusion = "Сделку нельзя выводить на аванс без ручного юридического разбора и устранения выявленных факторов."
    elif score >= 35:
        level = "условно рискованная"
        conclusion = "Сделку можно рассматривать только после уточнения рисков и защитных условий в документах."
    else:
        level = "условно безопасная"
        conclusion = "По автоматическим источникам критические риски не подтверждены, но ручная проверка документов всё равно обязательна."

    return {"score": score, "max_score": 100, "level": level, "conclusion": conclusion, "factors": factors}

def build_recommendations(checklist: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    recs = []
    by_title = {x["title"]: x for x in checklist}

    egrn = by_title.get("ЕГРН / Росреестр", {})
    fssp = by_title.get("ФССП", {})
    bank = by_title.get("Банкротство / Федресурс", {})
    courts = by_title.get("Суды / арбитраж", {})
    passport = by_title.get("Паспорт МВД", {})

    if egrn.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Требовать снятие ограничения до основной сделки",
                     "text": "По ЕГРН выявлены ограничения/обременения. До аванса нужно получить основание ограничения и прописать обязанность продавца снять его в конкретный срок."})
        recs.append({"priority": "critical", "title": "Не передавать деньги напрямую продавцу",
                     "text": "При запрете регистрации использовать нотариальный депозит или аккредитив с раскрытием денег только после снятия ограничения и регистрации перехода права."})

    if fssp.get("status") == "risk":
        stats = fssp.get("fssp_stats", {})
        recs.append({"priority": "high", "title": "Закрыть активные ИП до сделки или через контролируемые расчеты",
                     "text": f"Актуальная сумма по активным/неоднозначным ИП: {money(stats.get('actual_debt'))}. В ПДКП прописать порядок погашения и последствия неснятия ограничений."})
    else:
        recs.append({"priority": "medium", "title": "Сверить ФССП вручную перед авансом",
                     "text": "Автоматический ответ ФССП зависит от точности ФИО, даты рождения и региона. Перед авансом нужна ручная сверка по сайту ФССП."})

    if bank.get("status") == "risk":
        recs.append({"priority": "critical", "title": "Не выходить на сделку без анализа банкротного риска",
                     "text": "Сведения о банкротстве требуют проверки статуса процедуры, периода подозрительности и риска оспаривания сделки."})
    elif bank.get("status") == "manual_check":
        recs.append({"priority": "medium", "title": "Проверить банкротство по ИНН вручную",
                     "text": "Если автоматический источник не подтвердил результат, Федресурс нужно проверить вручную до аванса."})

    if courts.get("status") == "risk":
        recs.append({"priority": "high", "title": "Разобрать судебные дела по предмету спора",
                     "text": "Нужно понять предмет спора, сумму требований и связь с недвижимостью, долгами или банкротством."})
    elif courts.get("status") == "manual_check":
        recs.append({"priority": "medium", "title": "Проверить судебные дела вручную",
                     "text": "При неполной автоматической проверке нужно сверить КАД/ГАС и оценить предмет найденных дел."})

    if passport.get("status") != "ok":
        recs.append({"priority": "medium", "title": "Проверить паспорт вручную",
                     "text": "До аванса проверить действительность паспорта МВД и сверить данные с правоустанавливающими документами."})

    recs.append({"priority": "high", "title": "Авансовое соглашение делать с защитными условиями",
                 "text": "Включить обязанность продавца раскрыть долги, запреты, банкротство, судебные споры; предусмотреть возврат аванса/задатка и ответственность при неподтверждении данных."})
    return recs

# --------------------------
# Legal report generation
# --------------------------

def build_local_legal_report(inp: Dict[str, Any], checklist: List[Dict[str, Any]], scoring: Dict[str, Any], recommendations: List[Dict[str, str]]) -> str:
    risks = [x for x in checklist if x["status"] == "risk"]
    manuals = [x for x in checklist if x["status"] == "manual_check"]
    oks = [x for x in checklist if x["status"] == "ok"]
    seller = " ".join([inp["last"], inp["first"], inp["middle"]]).strip()
    obj = inp["property"].get("query") or "не указан"

    lines = []
    lines.append("1. Краткий вывод")
    lines.append(f"Предварительная оценка сделки: {scoring['level'].upper()} ({scoring['score']}/100). {scoring['conclusion']}")
    lines.append("")
    lines.append("2. Что проверено")
    lines.append(f"Продавец: {seller or 'не указан'}. Дата рождения: {inp['dob'] or 'не указана'}. ИНН: {'передан' if inp['inn'] else 'не передан'}.")
    lines.append(f"Объект: {obj}.")
    lines.append(f"Проверок без явных рисков: {len(oks)}. Проверок с рисками: {len(risks)}. Требуют ручной проверки: {len(manuals)}.")
    lines.append("")
    lines.append("3. Риски по продавцу")
    seller_risks = [x for x in checklist if x["title"] != "ЕГРН / Росреестр" and x["status"] in {"risk", "manual_check"}]
    if seller_risks:
        for x in seller_risks:
            lines.append(f"- {x['title']}: {x['summary']}")
    else:
        lines.append("- По автоматическим источникам явные риски по продавцу не подтверждены.")
    lines.append("")
    lines.append("4. Риски по объекту")
    egrn = next((x for x in checklist if x["title"] == "ЕГРН / Росреестр"), None)
    if egrn:
        lines.append(f"- {egrn['summary']}")
        for d in egrn.get("details", [])[-3:]:
            if "огранич" in d.lower() or "запрещ" in d.lower():
                lines.append(f"  • {d}")
    else:
        lines.append("- По объекту данные не получены.")
    lines.append("")
    lines.append("5. Что говорит в пользу сделки")
    if oks:
        for x in oks:
            lines.append(f"- {x['title']}: {x['summary']}")
    else:
        lines.append("- На текущем этапе нет проверок, которые полностью снимают ключевые риски.")
    lines.append("")
    lines.append("6. Что обязательно проверить до аванса")
    for x in manuals:
        lines.append(f"- {x['title']}: требуется ручная проверка.")
    lines.append("- Основание и актуальность запрета/ограничений по ЕГРН.")
    lines.append("")
    lines.append("7. Что прописать в авансовом соглашении / ПДКП")
    lines.append("- Обязанность продавца снять запреты/ограничения в конкретный срок.")
    lines.append("- Возврат аванса/задатка, если ограничения не сняты или выявлены новые риски.")
    lines.append("- Порядок погашения долгов и подтверждения снятия исполнительных производств, если они есть.")
    lines.append("")
    lines.append("8. Безопасная схема расчетов")
    lines.append("- Нотариальный депозит или аккредитив. Деньги раскрываются только после снятия ограничений и регистрации перехода права.")
    lines.append("")
    lines.append("9. Итоговое заключение")
    lines.append("Сделку нельзя считать безопасной только на основании автоматической проверки. При выявленных ограничениях и рисках нужен ручной юридический разбор документов и оснований ограничений.")
    return "\n".join(lines)

async def maybe_gigachat_report(inp, checklist, scoring, recommendations) -> Tuple[str, List[str]]:
    warnings = []
    # Keep local report as stable fallback; GigaChat must never break product
    fallback = build_local_legal_report(inp, checklist, scoring, recommendations)
    credentials = GIGACHAT_CREDENTIALS
    if not credentials and GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET:
        credentials = base64.b64encode(f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode()).decode()

    if not credentials:
        warnings.append("GigaChat не настроен, использован встроенный юридический отчет.")
        return fallback, warnings

    try:
        # Use gigachat lib if installed
        from gigachat import GigaChat
        prompt = {
            "seller": {k: inp[k] for k in ["last", "first", "middle", "dob", "inn"]},
            "property": inp["property"],
            "checklist": checklist,
            "risk_scoring": scoring,
            "recommendations": recommendations,
        }
        system_prompt = (
            "Ты юрист-эксперт по недвижимости в Санкт-Петербурге. "
            "Сформируй подробный, понятный и коммерчески ценный юридический отчет для покупателя. "
            "Строго соблюдай структуру: 1. Краткий вывод 2. Что проверено 3. Риски по продавцу "
            "4. Риски по объекту 5. Что говорит в пользу сделки 6. Что обязательно проверить до аванса "
            "7. Что прописать в авансовом соглашении / ПДКП 8. Безопасная схема расчетов 9. Итоговое заключение. "
            "Не придумывай факты. Если данных нет — пиши 'по предоставленным данным не проверялось'. "
            "Если источник не ответил — пиши 'требуется ручная проверка'. Не обещай 100% безопасность."
        )
        with GigaChat(
            credentials=credentials,
            scope=GIGACHAT_SCOPE,
            model=GIGACHAT_MODEL,
            verify_ssl_certs=GIGACHAT_VERIFY_SSL_CERTS,
        ) as giga:
            resp = giga.chat(system_prompt + "\n\nДанные:\n" + json.dumps(prompt, ensure_ascii=False))
            text = clean_text(resp.choices[0].message.content) if resp and resp.choices else ""
            if text:
                return text, warnings
    except Exception as e:
        warnings.append(f"GigaChat недоступен, использован встроенный отчет: {clean_text(e)[:160]}")
    return fallback, warnings

# --------------------------
# Registry data and PDF
# --------------------------

def make_registry_data(checklist: List[Dict[str, Any]]) -> Dict[str, Any]:
    key_map = {
        "Паспорт МВД": "passport",
        "ФССП": "fssp",
        "Банкротство / Федресурс": "bankruptcy",
        "Суды / арбитраж": "courts",
        "ЕГРН / Росреестр": "egrn",
    }
    out = {}
    for item in checklist:
        key = key_map.get(item["title"], item["title"])
        out[key] = {
            "title": item["title"],
            "status": item["status"],
            "ui_status": item.get("ui_status"),
            "summary": item["summary"],
            "details": item.get("details", []),
            "data": item.get("data"),
        }
    return out

def get_status_label(status: str) -> str:
    return {"ok": "Проверено", "risk": "Риск", "manual_check": "Требуется ручная проверка", "manual": "Требуется ручная проверка"}.get(status, status)

def report_to_sections(text: str) -> List[Tuple[str, List[str]]]:
    text = (text or "").replace("###", "").replace("##", "")
    lines = [clean_text(x) for x in text.splitlines()]
    sections = []
    current_title = "Юридическое заключение"
    current = []
    for line in lines:
        if not line:
            continue
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if m:
            if current:
                sections.append((current_title, current))
            current_title = f"{m.group(1)}. {m.group(2)}"
            current = []
        else:
            current.append(line)
    if current:
        sections.append((current_title, current))
    return sections

def find_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return ""

def build_pdf(report: Dict[str, Any], path: Path) -> None:
    if SimpleDocTemplate is None:
        raise RuntimeError("reportlab не установлен")

    font_path = find_font()
    font_name = "Helvetica"
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("AppFont", font_path))
            font_name = "AppFont"
        except Exception:
            pass

    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=36, leftMargin=36, topMargin=38, bottomMargin=34)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleRu", fontName=font_name, fontSize=18, leading=22, spaceAfter=12, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="H2Ru", fontName=font_name, fontSize=12.5, leading=16, spaceBefore=12, spaceAfter=7, textColor=colors.HexColor("#0F3D56")))
    styles.add(ParagraphStyle(name="BodyRu", fontName=font_name, fontSize=9.5, leading=14, spaceAfter=5))
    styles.add(ParagraphStyle(name="SmallRu", fontName=font_name, fontSize=8, leading=11, textColor=colors.HexColor("#555555")))
    styles.add(ParagraphStyle(name="WarnRu", fontName=font_name, fontSize=10, leading=14, spaceAfter=5, textColor=colors.HexColor("#7f1d1d")))

    def P(txt, style="BodyRu"):
        return Paragraph(html.escape(clean_text(txt)), styles[style])

    story = []
    story.append(P("Юридический отчет по проверке продавца и объекта недвижимости", "TitleRu"))
    story.append(P(f"Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}", "SmallRu"))

    scoring = report["risk_scoring"]
    story.append(P("1. Риск-скоринг", "H2Ru"))
    score_table = Table([
        [P("Оценка риска", "BodyRu"), P(f"{scoring['score']}/100", "WarnRu")],
        [P("Категория", "BodyRu"), P(scoring["level"].upper(), "WarnRu")],
        [P("Вывод", "BodyRu"), P(scoring["conclusion"], "BodyRu")],
    ], colWidths=[120, 380])
    score_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7ED")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#FDBA74")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#FED7AA")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(score_table)

    inp = report["input"]
    seller = " ".join([inp["last"], inp["first"], inp["middle"]]).strip()
    story.append(P("2. Данные продавца и объекта", "H2Ru"))
    story.append(P(f"Продавец: {seller or 'не указан'}"))
    story.append(P(f"Дата рождения: {inp.get('dob') or 'не указана'}"))
    story.append(P(f"ИНН: {'передан' if inp.get('inn') else 'не передан'}"))
    story.append(P(f"Объект: {inp['property'].get('query') or 'не указан'}"))

    story.append(P("3. Чек-лист проверок", "H2Ru"))
    rows = [[P("Источник", "BodyRu"), P("Статус", "BodyRu"), P("Краткий результат", "BodyRu")]]
    for item in report["checklist"]:
        rows.append([P(item["title"]), P(get_status_label(item["status"])), P(item["summary"])])
    table = Table(rows, colWidths=[130, 100, 270], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F3D56")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    story.append(P("4. Важные найденные данные", "H2Ru"))
    for item in report["checklist"]:
        if item.get("details"):
            story.append(P(item["title"], "H2Ru"))
            for d in item["details"][:10]:
                if d.lower() in {"false", "true"} or d.startswith("http"):
                    continue
                story.append(P("• " + d))

    story.append(P("5. Рекомендации", "H2Ru"))
    for rec in report["recommendations"]:
        story.append(P(f"{rec['title']}: {rec['text']}"))

    story.append(P("6. Юридическое заключение", "H2Ru"))
    for title, paragraphs in report_to_sections(report.get("legal_report", "")):
        story.append(P(title, "H2Ru"))
        for para in paragraphs:
            story.append(P(para))

    story.append(P("Дисклеймер", "H2Ru"))
    story.append(P(DISCLAIMER, "SmallRu"))

    doc.build(story)

def save_report(report: Dict[str, Any]) -> Tuple[str, str, str]:
    report_id = str(uuid.uuid4())
    json_path = REPORT_DIR / f"{report_id}.json"
    pdf_path = REPORT_DIR / f"{report_id}.pdf"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    build_pdf(report, pdf_path)
    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    return report_id, str(pdf_path), pdf_b64

# --------------------------
# API endpoints
# --------------------------

@app.get("/health")
async def health():
    return {
        "success": True,
        "service": "real-estate-check",
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(GIGACHAT_CREDENTIALS or (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET)),
    }

@app.post("/debug-newdb")
async def debug_newdb(request: Request):
    raw = await request.json()
    inp = normalize_input(raw)
    payloads, responses = await run_checks(inp)
    checklist = classify_all(inp, responses)
    scoring = build_scoring(checklist)
    recommendations = build_recommendations(checklist)
    return {
        "success": True,
        "normalized_input": inp,
        "payloads": payloads,
        "responses": responses,
        "classified_checklist": checklist,
        "risk_scoring": scoring,
        "recommendations": recommendations,
        "notes": [
            "Залоги движимого имущества отключены и не участвуют в отчете.",
            "Банкротство и суды запускаются только при 12-значном ИНН физлица.",
            "Паспорт МВД запускается только при серии и номере паспорта.",
            "Клиентский /check-report очищает служебные поля newDB, debug показывает больше технической информации.",
        ],
    }

@app.post("/check-report")
async def check_report(request: Request):
    try:
        raw = await request.json()
        inp = normalize_input(raw)
        payloads, responses = await run_checks(inp)
        checklist = classify_all(inp, responses)
        scoring = build_scoring(checklist)
        recommendations = build_recommendations(checklist)
        legal_report, ai_warnings = await maybe_gigachat_report(inp, checklist, scoring, recommendations)

        report = {
            "success": True,
            "input": inp,
            "checklist": checklist,
            "registry_data": make_registry_data(checklist),
            "risk_scoring": scoring,
            "recommendations": recommendations,
            "legal_report": legal_report,
            "warnings": ai_warnings,
            "disclaimer": DISCLAIMER,
            "created_at": datetime.now().isoformat(),
        }

        try:
            report_id, pdf_path, pdf_b64 = save_report(report)
            report["report_id"] = report_id
            report["pdf_available"] = True
            report["pdf_url"] = f"/download-pdf/{report_id}"
            report["pdf_base64"] = pdf_b64  # compatibility with current widget
        except Exception as e:
            report["report_id"] = ""
            report["pdf_available"] = False
            report["pdf_url"] = ""
            report["pdf_base64"] = ""
            report["warnings"].append(f"PDF временно не сформирован: {clean_text(e)[:180]}")

        return JSONResponse(strip_private(report))
    except Exception as e:
        # client-safe failure, use /debug-newdb for technical diagnostics
        return JSONResponse({
            "success": False,
            "message": "Не удалось сформировать отчет. Проверьте данные и повторите запрос. Если ошибка повторяется — требуется ручная проверка.",
            "warnings": ["Техническая ошибка скрыта от пользователя. Для диагностики используйте /debug-newdb."],
        }, status_code=200)

@app.get("/download-pdf/{report_id}")
async def download_pdf(report_id: str):
    safe_id = re.sub(r"[^a-f0-9-]", "", report_id.lower())
    path = REPORT_DIR / f"{safe_id}.pdf"
    if not path.exists():
        return JSONResponse({"success": False, "message": "PDF не найден или срок хранения отчета истек."}, status_code=404)
    return FileResponse(str(path), media_type="application/pdf", filename=f"otchet_{datetime.now().strftime('%Y-%m-%d')}.pdf")
