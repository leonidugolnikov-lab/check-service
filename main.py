from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import httpx
import uuid
import os
import io
import textwrap

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


app = FastAPI(title="Real Estate Legal Report API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")


class SellerData(BaseModel):
    full_name: str = ""
    dob: str = ""
    passport: str = ""
    inn: str = ""
    region: str = ""


class PropertyData(BaseModel):
    address: str = ""
    cadastre_number: str = ""
    egrn_status: str = "по предоставленным данным не проверялось"
    owner_match: str = "по предоставленным данным не проверялось"
    encumbrances: str = "по предоставленным данным не проверялось"


class FsspData(BaseModel):
    status: str = "по предоставленным данным не проверялось"
    active_count: int = 0
    closed_count: int = 0
    active_debt: float = 0


class ChecksData(BaseModel):
    fssp: FsspData = FsspData()
    bankruptcy: str = "по предоставленным данным не проверялось"
    courts: str = "по предоставленным данным не проверялось"


class RiskScoreData(BaseModel):
    level: str = "требуется ручная проверка"
    high_risks: int = 0
    medium_risks: int = 0
    unknown: int = 0


class ManualReportRequest(BaseModel):
    seller: SellerData
    property: PropertyData
    checks: ChecksData
    risk_score: RiskScoreData


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


def build_prompt(data: ManualReportRequest) -> str:
    return f"""
Ты — юрист-эксперт по недвижимости в Санкт-Петербурге.

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
- Учитывай, что отчет нужен покупателю перед внесением аванса.
- Отдельно оцени риски по продавцу и по объекту.
- Если есть долги, исполнительные производства, обременения или несовпадение собственника — обязательно выдели это как риск.
- Если данных по ЕГРН недостаточно — прямо укажи, что без актуальной выписки ЕГРН нельзя делать окончательный вывод.

ДАННЫЕ ПРОДАВЦА:
ФИО: {data.seller.full_name or "по предоставленным данным не проверялось"}
Дата рождения: {data.seller.dob or "по предоставленным данным не проверялось"}
Паспорт: {data.seller.passport or "по предоставленным данным не проверялось"}
ИНН: {data.seller.inn or "по предоставленным данным не проверялось"}
Регион: {data.seller.region or "по предоставленным данным не проверялось"}

ДАННЫЕ ОБЪЕКТА:
Адрес: {data.property.address or "по предоставленным данным не проверялось"}
Кадастровый номер: {data.property.cadastre_number or "по предоставленным данным не проверялось"}
Статус ЕГРН: {data.property.egrn_status or "по предоставленным данным не проверялось"}
Совпадение собственника: {data.property.owner_match or "по предоставленным данным не проверялось"}
Обременения: {data.property.encumbrances or "по предоставленным данным не проверялось"}

ПРОВЕРКИ:
ФССП:
- статус: {data.checks.fssp.status}
- активных производств: {data.checks.fssp.active_count}
- закрытых производств: {data.checks.fssp.closed_count}
- активный долг: {data.checks.fssp.active_debt} ₽

Банкротство: {data.checks.bankruptcy}
Суды: {data.checks.courts}

РИСК-СКОРИНГ:
Уровень риска: {data.risk_score.level}
Высоких рисков: {data.risk_score.high_risks}
Средних рисков: {data.risk_score.medium_risks}
Неизвестных / непроверенных пунктов: {data.risk_score.unknown}
"""


async def generate_ai_report(data: ManualReportRequest) -> str:
    token = await get_gigachat_token()

    if not token:
        return generate_fallback_report(data)

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
                "content": build_prompt(data)
            }
        ],
        "temperature": 0.2,
        "max_tokens": 3000
    }

    async with httpx.AsyncClient(verify=False, timeout=90) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()

    return result["choices"][0]["message"]["content"]


def generate_fallback_report(data: ManualReportRequest) -> str:
    return f"""
1. Краткий вывод

По предоставленным данным сделка требует предварительной юридической проверки перед внесением аванса. Уровень риска: {data.risk_score.level}.

2. Что проверено

Продавец: {data.seller.full_name or "по предоставленным данным не проверялось"}.
Паспорт: {data.seller.passport or "по предоставленным данным не проверялось"}.
Объект: {data.property.address or "по предоставленным данным не проверялось"}.
Кадастровый номер: {data.property.cadastre_number or "по предоставленным данным не проверялось"}.
ЕГРН: {data.property.egrn_status or "по предоставленным данным не проверялось"}.

3. Риски по продавцу

ФССП: {data.checks.fssp.status}.
Активных производств: {data.checks.fssp.active_count}.
Активный долг: {data.checks.fssp.active_debt} ₽.
Банкротство: {data.checks.bankruptcy}.
Суды: {data.checks.courts}.

4. Риски по объекту

Совпадение собственника: {data.property.owner_match}.
Обременения: {data.property.encumbrances}.
Без актуальной выписки ЕГРН окончательный вывод по объекту делать нельзя.

5. Что говорит в пользу сделки

По предоставленным данным высоких рисков указано: {data.risk_score.high_risks}.
Средних рисков указано: {data.risk_score.medium_risks}.

6. Что обязательно проверить до аванса

Необходимо получить актуальную выписку ЕГРН, проверить собственника, основание права, наличие обременений, запретов, арестов и судебных споров. Также желательно повторно проверить ФССП, ЕФРСБ, суды и действительность паспорта.

7. Что прописать в авансовом соглашении / ПДКП

Нужно прописать обязанность продавца подтвердить право собственности, отсутствие скрытых обременений, отсутствие задолженностей и предоставить документы до сделки. Если выявлены ограничения или долги, порядок их погашения должен быть зафиксирован письменно.

8. Безопасная схема расчетов

При наличии долгов, ограничений или неполных данных безопаснее использовать аккредитив, банковскую ячейку, депозит нотариуса или иную контролируемую схему расчетов. Деньги продавцу лучше передавать только после перехода права и выполнения условий.

9. Итоговое заключение

Сделку нельзя считать полностью безопасной только на основании предоставленных данных. Перед внесением аванса требуется ручная юридическая проверка документов, продавца и объекта. 100% безопасность не гарантируется.
""".strip()


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

    story = []
    story.append(Paragraph("Юридический отчет по проверке недвижимости", title_style))
    story.append(Spacer(1, 10))

    for block in report_text.split("\n"):
        clean = block.strip()
        if not clean:
            story.append(Spacer(1, 8))
            continue

        safe_text = (
            clean.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        story.append(Paragraph(safe_text, body_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Real Estate Legal Report API",
        "endpoints": [
            "/manual-report",
            "/manual-report-pdf",
            "/generate-ai-report",
            "/generate-ai-report-pdf"
        ]
    }


@app.post("/manual-report")
async def manual_report(data: ManualReportRequest):
    report = await generate_ai_report(data)
    return {
        "report": report
    }


@app.post("/manual-report-pdf")
async def manual_report_pdf(data: ManualReportRequest):
    report = await generate_ai_report(data)
    pdf_bytes = make_pdf(report)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=legal-real-estate-report.pdf"
        }
    )


@app.post("/generate-ai-report")
async def generate_ai_report_alias(data: ManualReportRequest):
    report = await generate_ai_report(data)
    return {
        "report": report
    }


@app.post("/generate-ai-report-pdf")
async def generate_ai_report_pdf_alias(data: ManualReportRequest):
    report = await generate_ai_report(data)
    pdf_bytes = make_pdf(report)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=legal-real-estate-report.pdf"
        }
    )
