from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import httpx
import asyncio
import uuid
import os
import re
from datetime import datetime
from html import escape


app = FastAPI(title="Person & Property Check API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


NEWDB_TOKEN = os.getenv("NEWDB_TOKEN")
NEWDB_URL = "https://api.newdb.net/v2"

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")


class PersonRequest(BaseModel):
    last: str
    first: str
    middle: str = ""
    dob: str = ""
    inn: str = ""
    region: int = 0
    passport_series: str = ""
    passport_number: str = ""


class PropertyRequest(BaseModel):
    address: str


class ManualReportRequest(BaseModel):
    person: Optional[Dict[str, Any]] = None
    property: Optional[Dict[str, Any]] = None
    expert_comment: str = ""


class FullCheckRequest(BaseModel):
    person: PersonRequest
    property: Optional[PropertyRequest] = None
    expert_comment: str = ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_fio(last: str, first: str, middle: str = "") -> str:
    return " ".join(
        [
            clean_text(last),
            clean_text(first),
            clean_text(middle),
        ]
    ).strip()


def validate_person_request(data: PersonRequest):
    if not data.last or not data.first:
        raise HTTPException(status_code=400, detail="Укажите фамилию и имя.")

    if data.dob:
        if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", data.dob):
            raise HTTPException(
                status_code=400,
                detail="Дата рождения должна быть в формате ДД.ММ.ГГГГ.",
            )

    if data.passport_series and len(normalize_digits(data.passport_series)) != 4:
        raise HTTPException(
            status_code=400,
            detail="Серия паспорта должна содержать 4 цифры.",
        )

    if data.passport_number and len(normalize_digits(data.passport_number)) != 6:
        raise HTTPException(
            status_code=400,
            detail="Номер паспорта должен содержать 6 цифр.",
        )


def validate_property_request(data: PropertyRequest):
    if not data.address or len(data.address.strip()) < 5:
        raise HTTPException(
            status_code=400,
            detail="Укажите адрес или кадастровый номер объекта.",
        )


def hide_technical_fields(data: Any) -> Any:
    if isinstance(data, dict):
        hidden = {}
        for key, value in data.items():
            if key.lower() in ["balance", "token", "apikey", "api_key"]:
                continue
            hidden[key] = hide_technical_fields(value)
        return hidden

    if isinstance(data, list):
        return [hide_technical_fields(item) for item in data]

    return data


async def newdb_post(params: dict, timeout_seconds: int = 45) -> dict:
    if not NEWDB_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Сервис проверки временно не настроен. Обратитесь к специалисту.",
        )

    payload = {
        "token": NEWDB_TOKEN,
        "requestId": str(uuid.uuid4()),
        "params": params,
    }

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            response = await client.post(NEWDB_URL, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Сервис проверки временно недоступен. Попробуйте позже или обратитесь к специалисту.",
            )

    return hide_technical_fields(data)


async def newdb_post_and_wait(params: dict, attempts: int = 12, delay: float = 2.5) -> dict:
    first = await newdb_post(params)

    state = str(first.get("state", "")).lower()
    qid = (
        first.get("newdb_qid")
        or first.get("qid")
        or first.get("params", {}).get("newdb_qid")
    )

    if state not in ["in progress", "progress", "processing"]:
        return hide_technical_fields(first)

    if not qid:
        return hide_technical_fields(first)

    for _ in range(attempts):
        await asyncio.sleep(delay)

        result = await newdb_post(
            {
                "method": "result",
                "newdb_qid": qid,
            }
        )

        result_state = str(result.get("state", "")).lower()

        if result_state not in ["in progress", "progress", "processing"]:
            return hide_technical_fields(result)

    return {
        "status": "manual_check_required",
        "message": "Проверка запущена, но результат не был получен автоматически. Требуется ручная проверка специалистом.",
        "raw": hide_technical_fields(first),
    }


def extract_text_from_result(data: Any) -> str:
    if data is None:
        return ""

    if isinstance(data, str):
        return data

    if isinstance(data, list):
        return "\n".join(extract_text_from_result(x) for x in data)

    if isinstance(data, dict):
        parts = []
        for key, value in data.items():
            if key.lower() in ["raw", "html", "xml"]:
                continue

            if isinstance(value, (dict, list)):
                nested = extract_text_from_result(value)
                if nested:
                    parts.append(f"{key}: {nested}")
            else:
                parts.append(f"{key}: {value}")

        return "\n".join(parts)

    return str(data)


def make_status(title: str, status: str, comment: str, raw: Any = None) -> dict:
    return {
        "title": title,
        "status": status,
        "comment": comment,
        "raw": hide_technical_fields(raw),
    }


def analyze_passport(result: dict) -> dict:
    text = extract_text_from_result(result).lower()

    if any(x in text for x in ["действителен", "действительный", "valid"]):
        return make_status(
            "Паспорт",
            "ok",
            "По автоматической проверке паспорт выглядит действительным.",
            result,
        )

    if any(x in text for x in ["недействител", "invalid", "утрачен", "разыскивается"]):
        return make_status(
            "Паспорт",
            "risk",
            "Есть признаки проблемы с паспортом. Требуется ручная проверка до продолжения сделки.",
            result,
        )

    if any(x in text for x in ["не найден", "данные не найдены", "not found"]):
        return make_status(
            "Паспорт",
            "manual",
            "Данные по паспорту не найдены автоматически. Это не доказывает риск, но требует ручной проверки.",
            result,
        )

    return make_status(
        "Паспорт",
        "manual",
        "Автоматическая проверка не дала однозначного результата. Требуется ручная проверка.",
        result,
    )


def analyze_bankruptcy(result: dict) -> dict:
    text = extract_text_from_result(result).lower()

    if any(x in text for x in ["банкрот", "дело о банкротстве", "несостоятельн"]):
        return make_status(
            "Банкротство",
            "risk",
            "Найдены признаки банкротства или связанные сведения. Сделку нельзя продолжать без юридической оценки.",
            result,
        )

    if any(x in text for x in ["не найден", "ничего не найдено", "данные отсутствуют", "not found"]):
        return make_status(
            "Банкротство",
            "ok",
            "Автоматически сведения о банкротстве не найдены.",
            result,
        )

    return make_status(
        "Банкротство",
        "manual",
        "Автоматическая проверка банкротства не дала однозначного результата. Нужна ручная сверка по ЕФРСБ.",
        result,
    )


def analyze_fssp(result: dict) -> dict:
    text = extract_text_from_result(result).lower()

    if any(x in text for x in ["исполнительное производство", "задолженность", "ип №", "долг"]):
        return make_status(
            "ФССП",
            "risk",
            "Найдены признаки исполнительных производств или задолженности. Нужно проверить суммы, предмет взыскания и ограничения.",
            result,
        )

    if any(x in text for x in ["не найден", "ничего не найдено", "данные отсутствуют", "not found"]):
        return make_status(
            "ФССП",
            "ok",
            "Исполнительные производства автоматически не найдены.",
            result,
        )

    return make_status(
        "ФССП",
        "manual",
        "Автоматическая проверка ФССП не дала однозначного результата. Нужна ручная проверка.",
        result,
    )


def analyze_property(result: dict) -> dict:
    text = extract_text_from_result(result).lower()

    if any(x in text for x in ["не найден", "объект не найден", "not found"]):
        return make_status(
            "Объект недвижимости",
            "manual",
            "Объект не найден автоматически по указанным данным. Нужно проверить кадастровый номер или адрес вручную.",
            result,
        )

    if any(x in text for x in ["запрет", "арест", "ипотека", "обременение", "ограничение"]):
        return make_status(
            "Объект недвижимости",
            "risk",
            "Найдены возможные ограничения, обременения или ипотека. Требуется анализ выписки ЕГРН.",
            result,
        )

    if result:
        return make_status(
            "Объект недвижимости",
            "ok",
            "Данные по объекту получены автоматически. Перед сделкой всё равно нужна сверка с актуальной выпиской ЕГРН.",
            result,
        )

    return make_status(
        "Объект недвижимости",
        "manual",
        "Данные по объекту не получены автоматически. Требуется ручная проверка.",
        result,
    )


def calculate_score(checks: List[dict]) -> dict:
    risk_count = sum(1 for x in checks if x.get("status") == "risk")
    manual_count = sum(1 for x in checks if x.get("status") == "manual")

    if risk_count >= 2:
        return {
            "level": "high",
            "label": "Высокий риск",
            "summary": "Есть несколько существенных факторов риска. Без ручной юридической проверки сделку продолжать нельзя.",
        }

    if risk_count == 1:
        return {
            "level": "medium_high",
            "label": "Повышенный риск",
            "summary": "Найден минимум один значимый риск. Нужно разбирать документы и сценарий расчетов.",
        }

    if manual_count >= 2:
        return {
            "level": "medium",
            "label": "Требуется ручная проверка",
            "summary": "Критичных рисков автоматически не выявлено, но часть проверок не дала однозначного результата.",
        }

    return {
        "level": "low",
        "label": "Низкий риск по автоматической проверке",
        "summary": "По автоматическим источникам критичных признаков риска не выявлено. Финальный вывод возможен только после проверки документов.",
    }


async def check_person(data: PersonRequest) -> dict:
    validate_person_request(data)

    fio = normalize_fio(data.last, data.first, data.middle)
    checks = []

    if data.passport_series and data.passport_number:
        passport_result = await newdb_post_and_wait(
            {
                "method": "passport_mvd",
                "series": normalize_digits(data.passport_series),
                "seria": normalize_digits(data.passport_series),
                "number": normalize_digits(data.passport_number),
                "lastname": data.last,
                "firstname": data.first,
                "middlename": data.middle,
                "birthdate": data.dob,
            }
        )
        checks.append(analyze_passport(passport_result))
    else:
        checks.append(
            make_status(
                "Паспорт",
                "manual",
                "Паспортные данные не указаны. Проверка паспорта не выполнялась.",
                None,
            )
        )

    fssp_result = await newdb_post_and_wait(
        {
            "method": "fssp",
            "lastname": data.last,
            "firstname": data.first,
            "middlename": data.middle,
            "birthdate": data.dob,
            "region": data.region,
        }
    )
    checks.append(analyze_fssp(fssp_result))

    bankruptcy_result = await newdb_post_and_wait(
        {
            "method": "bankruptcy",
            "lastname": data.last,
            "firstname": data.first,
            "middlename": data.middle,
            "birthdate": data.dob,
            "inn": data.inn,
        }
    )
    checks.append(analyze_bankruptcy(bankruptcy_result))

    if data.inn:
        egrip_result = await newdb_post_and_wait(
            {
                "method": "egrip",
                "inn": normalize_digits(data.inn),
            }
        )

        checks.append(
            make_status(
                "ФНС / ЕГРИП",
                "manual",
                "Проверка по ФНС/ЕГРИП выполнена. Данные требуют ручной интерпретации специалистом.",
                egrip_result,
            )
        )
    else:
        checks.append(
            make_status(
                "ФНС / ЕГРИП",
                "manual",
                "ИНН не указан. Проверка по ФНС/ЕГРИП не выполнялась.",
                None,
            )
        )

    score = calculate_score(checks)

    return {
        "checked_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "person": {
            "fio": fio,
            "dob": data.dob,
            "inn": data.inn,
            "region": data.region,
        },
        "checks": checks,
        "score": score,
    }


async def check_property(data: PropertyRequest) -> dict:
    validate_property_request(data)

    query = data.address.strip()

    result = await newdb_post_and_wait(
        {
            "method": "rosreestr",
            "address": query,
            "country": "ru",
        }
    )

    check = analyze_property(result)

    return {
        "checked_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "property": {
            "query": query,
        },
        "checks": [check],
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
        "scope": "GIGACHAT_API_PERS",
    }

    try:
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            response = await client.post(url, headers=headers, data=data)
            response.raise_for_status()
            return response.json().get("access_token")
    except Exception:
        return None


async def generate_ai_conclusion(report_data: dict) -> str:
    token = await get_gigachat_token()

    fallback = build_fallback_conclusion(report_data)

    if not token:
        return fallback

    prompt = f"""
Ты юрист-эксперт по недвижимости в Санкт-Петербурге.

На основе переданных данных сформируй подробный юридический отчет для покупателя недвижимости.

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
- Не обещай 100% безопасность.
- Пиши уверенно, как юрист по недвижимости, но понятным клиенту языком.
- Не используй слишком общие фразы.
- Объясняй риски практично: что может пойти не так для покупателя.
- Отчет должен быть подробным, но без воды.
- Если автоматическая проверка дала неоднозначный результат — укажи, что нужна ручная проверка.
- Если найден риск — объясни, как его закрыть до сделки.
- Отдельно подчеркни, что отчет не заменяет актуальные документы: ЕГРН, паспорт, справки, подтверждение расчетов и проверку оснований права.
- Не упоминай технические детали API.
- Не пиши, что ты искусственный интеллект.

Данные для анализа:
{report_data}
"""

    payload = {
        "model": "GigaChat",
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.2,
        "max_tokens": 3500,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            response = await client.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception:
        return fallback


def build_fallback_conclusion(report_data: dict) -> str:
    person_data = report_data.get("person")
    property_data = report_data.get("property")
    expert_comment = report_data.get("expert_comment", "")

    person_checks = []
    property_checks = []

    if isinstance(person_data, dict):
        person_checks = person_data.get("checks", [])

    if isinstance(property_data, dict):
        property_checks = property_data.get("checks", [])

    all_checks = person_checks + property_checks

    if all_checks:
        score = calculate_score(all_checks)
    else:
        score = {
            "label": "Недостаточно данных",
            "summary": "По предоставленным данным невозможно сделать полноценный автоматический вывод.",
        }

    def section_checks(checks):
        if not checks:
            return "по предоставленным данным не проверялось"

        rows = []
        for item in checks:
            rows.append(
                f"- {item.get('title', 'Проверка')}: {item.get('comment', 'результат требует ручной оценки')}"
            )
        return "\n".join(rows)

    risk_seller = [
        x for x in person_checks
        if x.get("status") in ["risk", "manual"]
    ]

    risk_property = [
        x for x in property_checks
        if x.get("status") in ["risk", "manual"]
    ]

    positive = [
        x for x in all_checks
        if x.get("status") == "ok"
    ]

    return f"""
1. Краткий вывод

{score.get("label", "Недостаточно данных")}. {score.get("summary", "")}

Этот вывод не означает 100% безопасность сделки. Он показывает предварительную оценку по тем данным, которые были переданы в проверку.

2. Что проверено

По продавцу:
{section_checks(person_checks)}

По объекту:
{section_checks(property_checks)}

3. Риски по продавцу

{section_checks(risk_seller)}

Если по части проверок указан ручной контроль, это означает, что до внесения аванса нужно дополнительно сверить документы и открытые источники.

4. Риски по объекту

{section_checks(risk_property)}

Отдельно нужно получить актуальную выписку ЕГРН и проверить собственника, обременения, ограничения, аресты, ипотеку, основание права и историю переходов.

5. Что говорит в пользу сделки

{section_checks(positive)}

Если положительных автоматических результатов мало или их нет, это не делает сделку плохой, но означает, что решение нельзя принимать без ручной юридической проверки.

6. Что обязательно проверить до аванса

- Актуальную выписку ЕГРН.
- Паспорт продавца и его действительность.
- Основание права собственности.
- Наличие брака, согласия супруга или брачного договора.
- Исполнительные производства ФССП.
- Банкротство продавца и признаки финансовых проблем.
- Зарегистрированных лиц.
- Наличие несовершеннолетних, отказников, наследственных рисков.
- Ограничения, запреты, аресты, ипотеку и иные обременения.
- Полномочия представителя, если продавец действует по доверенности.

7. Что прописать в авансовом соглашении / ПДКП

- Полные данные сторон.
- Точный объект: адрес, кадастровый номер, площадь.
- Цену квартиры и порядок расчетов.
- Условие возврата аванса, если выявятся юридические риски.
- Обязанность продавца предоставить документы до основной сделки.
- Сроки снятия ограничений, погашения долгов или ипотеки, если они есть.
- Ответственность продавца за недостоверные сведения.
- Условие, что деньги передаются только при юридически чистом сценарии сделки.
- Перечень документов, без которых покупатель вправе отказаться от сделки.

8. Безопасная схема расчетов

Рекомендуется использовать безопасную форму расчетов: аккредитив, банковскую ячейку, нотариальный депозит или сервис безопасных расчетов.

Если есть долги, ипотека, запреты или ограничения, деньги лучше делить по назначению:
- часть — на погашение обязательств;
- часть — продавцу только после перехода права;
- часть — при необходимости после снятия ограничений.

Наличную передачу денег без понятной документальной фиксации использовать нежелательно.

9. Итоговое заключение

Сделку можно рассматривать только после ручной проверки документов и подтверждения всех данных актуальными источниками.

По предоставленным данным автоматическая проверка является предварительной. Она помогает увидеть очевидные риски, но не заменяет юридическую экспертизу перед авансом, ПДКП и основной сделкой.

Комментарий специалиста:
{expert_comment if expert_comment else "по предоставленным данным не проверялось"}
""".strip()


def report_text_to_html(report_text: str) -> str:
    safe = escape(report_text)
    safe = safe.replace("\n", "<br>")
    return safe


def create_pdf_report(report_text: str) -> bytes:
    try:
        from weasyprint import HTML

        html = f"""
        <!doctype html>
        <html lang="ru">
        <head>
            <meta charset="utf-8">
            <style>
                body {{
                    font-family: DejaVu Sans, Arial, sans-serif;
                    color: #111827;
                    background: #ffffff;
                    padding: 34px;
                    line-height: 1.55;
                    font-size: 13px;
                }}
                .header {{
                    border-bottom: 3px solid #0F3D56;
                    padding-bottom: 18px;
                    margin-bottom: 24px;
                }}
                .brand {{
                    font-size: 22px;
                    font-weight: 800;
                    color: #0F3D56;
                    margin-bottom: 4px;
                }}
                .subtitle {{
                    font-size: 13px;
                    color: #3C4853;
                }}
                .date {{
                    margin-top: 8px;
                    font-size: 12px;
                    color: #6E7F8D;
                }}
                .report {{
                    white-space: normal;
                }}
                .footer {{
                    margin-top: 28px;
                    padding-top: 14px;
                    border-top: 1px solid #e5e7eb;
                    font-size: 11px;
                    color: #6E7F8D;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="brand">Юридический отчет по проверке недвижимости</div>
                <div class="subtitle">Леонид Угольников · Риелтор в Санкт-Петербурге + юридический подход</div>
                <div class="date">Дата формирования: {datetime.now().strftime("%d.%m.%Y %H:%M")}</div>
            </div>

            <div class="report">
                {report_text_to_html(report_text)}
            </div>

            <div class="footer">
                Отчет носит предварительный информационно-аналитический характер и не заменяет изучение оригиналов документов,
                актуальной выписки ЕГРН, проверку личности продавца и юридическое сопровождение сделки.
            </div>
        </body>
        </html>
        """

        return HTML(string=html).write_pdf()

    except Exception:
        plain = (
            "Юридический отчет по проверке недвижимости\n\n"
            + report_text
            + "\n\nPDF-библиотека недоступна на сервере. Проверьте установку weasyprint."
        )
        return plain.encode("utf-8")


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Person & Property Check API работает.",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "newdb_configured": bool(NEWDB_TOKEN),
        "gigachat_configured": bool(GIGACHAT_AUTH_KEY),
    }


@app.post("/check-person")
async def check_person_endpoint(data: PersonRequest):
    result = await check_person(data)

    return {
        "status": "ok",
        "message": "Проверка продавца выполнена.",
        "result": result,
    }


@app.post("/check-property")
async def check_property_endpoint(data: PropertyRequest):
    result = await check_property(data)

    return {
        "status": "ok",
        "message": "Проверка объекта выполнена.",
        "result": result,
    }


@app.post("/full-check")
async def full_check(data: FullCheckRequest):
    person_result = await check_person(data.person)

    property_result = None
    if data.property:
        property_result = await check_property(data.property)

    report_data = {
        "person": person_result,
        "property": property_result,
        "expert_comment": data.expert_comment,
    }

    legal_report = await generate_ai_conclusion(report_data)

    return {
        "status": "ok",
        "message": "Проверка выполнена. Юридический отчет сформирован.",
        "report": legal_report,
        "data": report_data,
    }


@app.post("/full-check/pdf")
async def full_check_pdf(data: FullCheckRequest):
    person_result = await check_person(data.person)

    property_result = None
    if data.property:
        property_result = await check_property(data.property)

    report_data = {
        "person": person_result,
        "property": property_result,
        "expert_comment": data.expert_comment,
    }

    legal_report = await generate_ai_conclusion(report_data)
    pdf_bytes = create_pdf_report(legal_report)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=legal_report.pdf"
        },
    )


@app.post("/manual-report")
async def manual_report(data: ManualReportRequest):
    report_data = {
        "person": data.person,
        "property": data.property,
        "expert_comment": data.expert_comment,
    }

    legal_report = await generate_ai_conclusion(report_data)

    return {
        "status": "ok",
        "message": "Юридический отчет сформирован по переданным данным.",
        "report": legal_report,
        "data": report_data,
    }


@app.post("/manual-report/pdf")
async def manual_report_pdf(data: ManualReportRequest):
    report_data = {
        "person": data.person,
        "property": data.property,
        "expert_comment": data.expert_comment,
    }

    legal_report = await generate_ai_conclusion(report_data)
    pdf_bytes = create_pdf_report(legal_report)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=legal_report.pdf"
        },
    )
