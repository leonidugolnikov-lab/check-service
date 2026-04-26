from fastapi import FastAPI
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


def strip_sensitive_newdb(data: Any) -> Any:
    if isinstance(data, dict):
        cleaned = {}
        for key, value in data.items():
            if key.lower() in {"balance", "token", "api_key", "x-api-key"}:
                continue
            cleaned[key] = strip_sensitive_newdb(value)
        return cleaned

    if isinstance(data, list):
        return [strip_sensitive_newdb(item) for item in data]

    return data


async def newdb_request(params: dict, timeout_seconds: int = 90) -> dict:
    if not NEWDB_TOKEN:
        return {
            "state": "not_configured",
            "error": "NEWDB_TOKEN не задан в переменных окружения Render",
            "params": params,
        }

    request_id = str(uuid.uuid4())

    payload = {
        "params": params,
        "requestId": request_id,
    }

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": NEWDB_TOKEN,
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
            }

        state = str(first_data.get("state", "")).lower()

        if state in ["complete", "completed", "done", "success"]:
            return first_data

        for _ in range(18):
            await asyncio.sleep(5)

            try:
                r = await client.get(
                    NEWDB_DATA_URL,
                    params={
                        "requestId": request_id,
                        "token": NEWDB_TOKEN,
                    },
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
                }

        return {
            "state": "timeout",
            "error": "NewDB не успел вернуть результат. Проверьте позже по requestId или увеличьте ожидание.",
            "requestId": request_id,
            "params": params,
            "first_response": first_data,
        }


def result_data(newdb_response: dict, method: str) -> list:
    if not isinstance(newdb_response, dict):
        return []

    possible_paths = [
        ["results", method, "result", "data"],
        ["results", method, "data"],
        ["result", "data"],
        ["data"],
    ]

    for path in possible_paths:
        current = newdb_response
        ok = True
        for part in path:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break

        if ok:
            if isinstance(current, list):
                return current
            if isinstance(current, dict):
                return [current]

    return []


def any_data_found(items: list) -> bool:
    if not items:
        return False

    text = json.dumps(items, ensure_ascii=False).lower()

    negative_markers = [
        "данные не найдены",
        "не найдено",
        "не найдена",
        "not found",
        "no data",
        "ничего не найдено",
        "отсутствуют",
    ]

    if any(marker in text for marker in negative_markers):
        return False

    return True


def extract_money_sum(items: list) -> float:
    text = json.dumps(items, ensure_ascii=False)
    numbers = re.findall(r"(\d+[\s\d]*[,.]?\d*)\s*(?:руб|₽)", text, flags=re.I)

    total = 0.0
    for n in numbers:
        try:
            total += float(n.replace(" ", "").replace(",", "."))
        except Exception:
            pass

    return round(total, 2)


def passport_summary(items: list) -> str:
    if not items:
        return "данные не найдены или требуется ручная проверка"

    text = json.dumps(items, ensure_ascii=False)
    low = text.lower()

    if "недействител" in low:
        return "паспорт недействителен"

    if "действител" in low:
        return "паспорт действителен"

    if "данные не найдены" in low or "не найдено" in low:
        return "данные не найдены, требуется ручная проверка"

    return "получен ответ, требуется ручная интерпретация"


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

    tasks = []

    if passport_series and passport_number:
        tasks.append((
            "passport",
            "passport_mvd",
            {
                "method": "passport_mvd",
                "seria": passport_series,
                "series": passport_series,
                "number": passport_number,
                "lastname": last,
                "firstname": first,
                "secondname": middle,
                "dob": dob_iso,
                "country": "ru",
            }
        ))

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
            fssp_params["regioncode"] = req.region
        tasks.append(("fssp", "fssp_person", fssp_params))

    if last and first and dob_iso:
        tasks.append((
            "pledges_person",
            "zalogfiz",
            {
                "method": "zalogfiz",
                "lastname": last,
                "firstname": first,
                "secondname": middle,
                "dob": dob_iso,
                "country": "ru",
            }
        ))

    if inn:
        tasks.append((
            "arbitration",
            "arbitr_ip",
            {
                "method": "arbitr_ip",
                "inn": inn,
                "country": "ru",
            }
        ))

    if passport_series and passport_number and last and first and dob_iso:
        tasks.append((
            "complex",
            "complex_by_passport",
            {
                "method": "complex_by_passport",
                "seria": passport_series,
                "series": passport_series,
                "number": passport_number,
                "lastname": last,
                "firstname": first,
                "secondname": middle,
                "dob": dob_iso,
                "country": "ru",
            }
        ))

    if cadastre or address:
        rosreestr_params = {
            "method": "rosreestr",
            "country": "ru",
        }

        if cadastre:
            rosreestr_params["address"] = cadastre
            rosreestr_params["cadnum"] = cadastre
            rosreestr_params["cadastral_number"] = cadastre
        else:
            rosreestr_params["address"] = address

        tasks.append(("rosreestr", "rosreestr", rosreestr_params))

    async def run_one(name: str, method: str, params: dict):
        response = await newdb_request(params)
        return name, method, response

    results = await asyncio.gather(
        *[run_one(name, method, params) for name, method, params in tasks],
        return_exceptions=True
    )

    checks = {}

    for item in results:
        if isinstance(item, Exception):
            continue

        name, method, response = item
        checks[name] = {
            "method": method,
            "response": response,
            "data": result_data(response, method),
        }

    return checks


def build_risk_score(checks: dict) -> dict:
    high = 0
    medium = 0
    unknown = 0
    notes = []

    passport_items = checks.get("passport", {}).get("data", [])
    passport_text = passport_summary(passport_items)

    if "недействителен" in passport_text:
        high += 1
        notes.append("Паспорт имеет признак недействительности.")
    elif "ручная" in passport_text or "не найдены" in passport_text:
        medium += 1
        notes.append("По паспорту требуется ручная проверка.")

    fssp_items = checks.get("fssp", {}).get("data", [])
    fssp_found = any_data_found(fssp_items)
    fssp_debt = extract_money_sum(fssp_items)

    if fssp_found:
        if fssp_debt >= 300000:
            high += 1
            notes.append(f"Найдены исполнительные производства ФССП, ориентировочная сумма: {fssp_debt} ₽.")
        else:
            medium += 1
            notes.append(f"Найдены сведения ФССП, ориентировочная сумма: {fssp_debt} ₽.")
    elif "fssp" not in checks:
        unknown += 1
        notes.append("ФССП не проверялось.")

    pledge_items = checks.get("pledges_person", {}).get("data", [])
    if any_data_found(pledge_items):
        medium += 1
        notes.append("Найдены сведения по залогам/обременениям физлица.")

    arbitration_items = checks.get("arbitration", {}).get("data", [])
    if any_data_found(arbitration_items):
        medium += 1
        notes.append("Найдены арбитражные дела/сведения по ИП.")

    complex_items = checks.get("complex", {}).get("data", [])
    complex_text = json.dumps(complex_items, ensure_ascii=False).lower()
    if "банкрот" in complex_text or "fedresurs" in complex_text or "ефрсб" in complex_text:
        high += 1
        notes.append("В комплексной проверке есть признаки банкротства/Федресурса. Требуется ручная проверка ЕФРСБ.")

    rosreestr_items = checks.get("rosreestr", {}).get("data", [])
    rosreestr_text = json.dumps(rosreestr_items, ensure_ascii=False).lower()

    if "rosreestr" not in checks:
        unknown += 1
        notes.append("Росреестр не проверялся.")
    elif not any_data_found(rosreestr_items):
        medium += 1
        notes.append("Объект Росреестра не найден или требуется ручная проверка.")
    else:
        risk_words = ["арест", "запрет", "ипотека", "обремен", "огранич", "залог"]
        if any(word in rosreestr_text for word in risk_words):
            high += 1
            notes.append("В данных Росреестра есть признаки обременений/ограничений.")

    if high > 0:
        level = "высокий риск"
    elif medium > 0 or unknown > 0:
        level = "средний риск"
    else:
        level = "низкий риск"

    return {
        "level": level,
        "high_risks": high,
        "medium_risks": medium,
        "unknown": unknown,
        "notes": notes,
    }


def build_structured_summary(req: CheckRequest, checks: dict, risk_score: dict) -> dict:
    passport_items = checks.get("passport", {}).get("data", [])
    fssp_items = checks.get("fssp", {}).get("data", [])
    pledge_items = checks.get("pledges_person", {}).get("data", [])
    arbitration_items = checks.get("arbitration", {}).get("data", [])
    complex_items = checks.get("complex", {}).get("data", [])
    rosreestr_items = checks.get("rosreestr", {}).get("data", [])

    return {
        "seller": {
            "full_name": f"{req.last} {req.first} {req.middle}".strip(),
            "dob": req.dob,
            "inn": req.inn or "по предоставленным данным не указано",
            "region": req.region or "по предоставленным данным не указано",
            "passport": {
                "series": req.passport_series,
                "number": req.passport_number,
                "summary": passport_summary(passport_items),
            },
        },
        "property": {
            "address": req.address or "по предоставленным данным не указано",
            "cadastre_number": req.cadastre_number or "по предоставленным данным не указано",
            "rosreestr_found": any_data_found(rosreestr_items),
        },
        "checks_summary": {
            "passport": passport_summary(passport_items),
            "fssp": {
                "found": any_data_found(fssp_items),
                "items_count": len(fssp_items),
                "estimated_debt": extract_money_sum(fssp_items),
            },
            "pledges_person": {
                "found": any_data_found(pledge_items),
                "items_count": len(pledge_items),
            },
            "arbitration": {
                "found": any_data_found(arbitration_items),
                "items_count": len(arbitration_items),
            },
            "complex_check": {
                "received": bool(complex_items),
                "items_count": len(complex_items),
            },
            "rosreestr": {
                "found": any_data_found(rosreestr_items),
                "items_count": len(rosreestr_items),
            },
        },
        "risk_score": risk_score,
    }


async def get_gigachat_token() -> Optional[str]:
    if not GIGACHAT_AUTH_KEY:
        return None

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

    headers = {
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "scope": "GIGACHAT_API_PERS"
    }

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        response = await client.post(url, headers=headers, data=data)
        response.raise_for_status()
        result = response.json()
        return result.get("access_token")


def compact_raw_for_prompt(checks: dict) -> dict:
    compact = {}

    for name, item in checks.items():
        response = item.get("response", {})
        method = item.get("method")
        data = item.get("data", [])

        compact[name] = {
            "method": method,
            "state": response.get("state"),
            "error": response.get("error", ""),
            "data": data[:5],
        }

    return compact


def build_prompt(summary: dict, raw_checks: dict) -> str:
    return f"""
Ты — юрист-эксперт по недвижимости в Санкт-Петербурге.

На основе автоматических проверок сформируй подробный юридический отчет для покупателя недвижимости.

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
- Если данных нет — прямо пиши: “по предоставленным данным не проверялось”.
- Если источник вернул пустой результат — пиши осторожно: “по автоматической проверке сведения не выявлены”.
- Не обещай 100% безопасность.
- Не называй сделку безопасной окончательно без актуальной выписки ЕГРН и анализа документов.
- Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.
- Учитывай, что отчет нужен покупателю перед внесением аванса.
- Если есть ФССП, банкротство, суды, залоги, обременения, аресты, запреты, ипотека или неясность по объекту — выдели это как риск.
- Не показывай технические поля API, requestId, balance, taskId.

КРАТКАЯ СВОДКА:
{json.dumps(summary, ensure_ascii=False, indent=2)}

СЫРЫЕ ДАННЫЕ АВТОМАТИЧЕСКИХ ПРОВЕРОК:
{json.dumps(raw_checks, ensure_ascii=False, indent=2)}
"""


def fallback_report(summary: dict) -> str:
    risk = summary.get("risk_score", {})
    seller = summary.get("seller", {})
    prop = summary.get("property", {})
    checks = summary.get("checks_summary", {})

    return f"""
1. Краткий вывод

По автоматическим проверкам сформирована предварительная оценка рисков. Уровень риска: {risk.get("level", "требуется ручная проверка")}.
Отчет не является гарантией безопасности сделки и требует проверки документов перед внесением аванса.

2. Что проверено

Продавец: {seller.get("full_name", "по предоставленным данным не проверялось")}.
Дата рождения: {seller.get("dob", "по предоставленным данным не проверялось")}.
Паспорт: {seller.get("passport", {}).get("summary", "по предоставленным данным не проверялось")}.
Объект: {prop.get("address", "по предоставленным данным не проверялось")}.
Кадастровый номер: {prop.get("cadastre_number", "по предоставленным данным не проверялось")}.

3. Риски по продавцу

ФССП: найдено записей — {checks.get("fssp", {}).get("items_count", 0)}, ориентировочная сумма — {checks.get("fssp", {}).get("estimated_debt", 0)} ₽.
Залоги физлица: найдено записей — {checks.get("pledges_person", {}).get("items_count", 0)}.
Арбитраж/ИП: найдено записей — {checks.get("arbitration", {}).get("items_count", 0)}.
Комплексная проверка: получена — {checks.get("complex_check", {}).get("received", False)}.

4. Риски по объекту

Росреестр: объект найден автоматической проверкой — {checks.get("rosreestr", {}).get("found", False)}.
Без актуальной выписки ЕГРН нельзя делать окончательный вывод по собственнику, обременениям и ограничениям.

5. Что говорит в пользу сделки

Высоких рисков: {risk.get("high_risks", 0)}.
Средних рисков: {risk.get("medium_risks", 0)}.
Непроверенных/неясных пунктов: {risk.get("unknown", 0)}.

6. Что обязательно проверить до аванса

Получить актуальную выписку ЕГРН, проверить основание права, историю переходов, семейное положение продавца, согласия, отсутствие банкротства, исполнительных производств, арестов, запретов и судебных споров.

7. Что прописать в авансовом соглашении / ПДКП

Прописать обязанность продавца подтвердить право собственности, отсутствие скрытых обременений, долгов, запретов и судебных споров. Если выявлены долги или ограничения — указать порядок их снятия, сроки и последствия нарушения.

8. Безопасная схема расчетов

При наличии долгов, ограничений или неполных данных безопаснее использовать аккредитив, депозит нотариуса или иную контролируемую схему расчетов. Деньги продавцу передавать только после выполнения условий.

9. Итоговое заключение

Автоматическая проверка помогает выявить предварительные риски, но не заменяет ручной юридический анализ документов. Перед авансом требуется финальная проверка ЕГРН, продавца, оснований права и условий сделки.
""".strip()


async def generate_ai_report(summary: dict, checks: dict) -> str:
    token = await get_gigachat_token()
    raw_checks = compact_raw_for_prompt(checks)

    if not token:
        return fallback_report(summary)

    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "GigaChat",
        "messages": [
            {
                "role": "user",
                "content": build_prompt(summary, raw_checks)
            }
        ],
        "temperature": 0.2,
        "max_tokens": 3500
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

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=42,
        leftMargin=42,
        topMargin=42,
        bottomMargin=42
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=24,
        spaceAfter=18
    )

    body_style = ParagraphStyle(
        "BodyCustom",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        spaceAfter=10
    )

    story = [
        Paragraph("Юридический отчет по проверке недвижимости", title_style),
        Spacer(1, 10)
    ]

    for block in report_text.split("\n"):
        clean = block.strip()
        if not clean:
            story.append(Spacer(1, 8))
            continue

        story.append(Paragraph(html.escape(clean), body_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Automatic Real Estate Check API",
        "endpoints": [
            "/newdb-check",
            "/check-report",
            "/check-report-pdf"
        ],
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(GIGACHAT_AUTH_KEY),
    }


@app.post("/newdb-check")
async def newdb_check(req: CheckRequest):
    checks = await run_all_newdb_checks(req)
    risk_score = build_risk_score(checks)
    summary = build_structured_summary(req, checks, risk_score)

    return {
        "summary": summary,
        "checks": checks,
    }


@app.post("/check-report")
async def check_report(req: CheckRequest):
    checks = await run_all_newdb_checks(req)
    risk_score = build_risk_score(checks)
    summary = build_structured_summary(req, checks, risk_score)
    report = await generate_ai_report(summary, checks)

    return {
        "report": report,
        "summary": summary,
    }


@app.post("/check-report-pdf")
async def check_report_pdf(req: CheckRequest):
    checks = await run_all_newdb_checks(req)
    risk_score = build_risk_score(checks)
    summary = build_structured_summary(req, checks, risk_score)
    report = await generate_ai_report(summary, checks)
    pdf_bytes = make_pdf(report)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=legal-real-estate-report.pdf"
        }
    )


@app.post("/generate-ai-report")
async def generate_ai_report_alias(req: CheckRequest):
    return await check_report(req)


@app.post("/generate-ai-report-pdf")
async def generate_ai_report_pdf_alias(req: CheckRequest):
    return await check_report_pdf(req)
