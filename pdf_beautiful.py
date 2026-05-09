"""
pdf_beautiful.py — Красивая генерация PDF через WeasyPrint (HTML → PDF)
Подключение: заменить build_pdf_bytes в main.py на эту версию.

Установка: pip install weasyprint
"""

import html
import re
from typing import Any, Dict


# ── Палитра (идентична Palette в main.py) ──────────────────────────────────
DARK_BLUE   = "#0A1F3F"
WHITE       = "#FFFFFF"
OFF_WHITE   = "#F8F7F4"
MID_GRAY    = "#6E7F8D"
DARK_TEXT   = "#1A1A1A"
CRITICAL    = "#C0392B"
HIGH        = "#E67E22"
MEDIUM      = "#D4A373"
LOW         = "#27AE60"
MANUAL      = "#7F8C8D"
CRITICAL_BG = "#FDF0ED"
HIGH_BG     = "#FEF7ED"
MEDIUM_BG   = "#FEF9F3"
LOW_BG      = "#EDF7F1"
MANUAL_BG   = "#F5F3EF"

STATUS_LABEL = {"ok": "ОК", "risk": "РИСК", "manual_check": "ПРОВЕРИТЬ"}
STATUS_ICON  = {"ok": "✓", "risk": "!", "manual_check": "?"}
STATUS_COLOR = {
    "ok":           (LOW_BG,      LOW,      "#1a7a48"),
    "risk":         (CRITICAL_BG, CRITICAL, CRITICAL),
    "manual_check": (MANUAL_BG,   MANUAL,   MANUAL),
}

def _score_color(score: int) -> str:
    if score >= 85: return CRITICAL
    if score >= 60: return HIGH
    if score >= 35: return MEDIUM
    return LOW

def _score_label(score: int) -> str:
    if score >= 85: return "Опасно"
    if score >= 60: return "Высокий риск"
    if score >= 35: return "Условный риск"
    return "Допустимо"

def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)

def _nl(text: Any) -> str:
    """Escape + переносы строк → <br>"""
    return _esc(text).replace("\n", "<br>")

def _rub(val) -> str:
    try:
        n = int(float(val))
        return f"{n:,}".replace(",", "\u202f") + " ₽"
    except Exception:
        return str(val or "—")

def _clean(v: Any, maxlen: int = 0) -> str:
    s = str(v or "").strip()
    if maxlen and len(s) > maxlen:
        s = s[:maxlen - 1] + "…"
    return s


# ── CSS ────────────────────────────────────────────────────────────────────
CSS = f"""
@page {{
  size: A4;
  margin: 14mm 15mm 16mm 15mm;
  @bottom-center {{
    content: "Угольников SPb · ugolnikovspb.ru · +7-952-375-20-20   Стр. " counter(page) " из " counter(pages);
    font-family: 'Helvetica Neue', Arial, sans-serif;
    font-size: 7pt;
    color: {MID_GRAY};
  }}
}}

* {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  font-family: 'Helvetica Neue', Arial, sans-serif;
  font-size: 9.5pt;
  color: {DARK_TEXT};
  line-height: 1.45;
  background: {WHITE};
}}

/* ── Шапка ────────────────────────────────────────── */
.page-header {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  background: {DARK_BLUE};
  color: {WHITE};
  padding: 9mm 10mm 8mm;
  margin: 0 0 6mm;
  border-radius: 2px;
}}
.header-left h1 {{
  font-size: 16pt;
  font-weight: 700;
  letter-spacing: -0.3px;
  line-height: 1.25;
  margin-bottom: 3px;
}}
.header-left .subtitle {{
  font-size: 8pt;
  opacity: 0.75;
  margin-top: 4px;
}}

/* Бейдж риска */
.risk-badge {{
  text-align: center;
  background: rgba(255,255,255,0.08);
  border: 1px solid rgba(255,255,255,0.2);
  border-radius: 4px;
  padding: 6px 12px 8px;
  min-width: 80px;
}}
.risk-badge .badge-label {{
  font-size: 7pt;
  font-weight: 700;
  letter-spacing: 0.8px;
  text-transform: uppercase;
  opacity: 0.85;
  display: block;
}}
.risk-badge .badge-score {{
  font-size: 28pt;
  font-weight: 900;
  line-height: 1;
  display: block;
  margin: 2px 0 0;
}}
.risk-badge .badge-max {{
  font-size: 7.5pt;
  opacity: 0.65;
}}

/* ── Разделитель секций ────────────────────────────── */
.section-title {{
  font-size: 11.5pt;
  font-weight: 700;
  color: {DARK_BLUE};
  border-bottom: 2px solid {DARK_BLUE};
  padding-bottom: 3px;
  margin: 7mm 0 4mm;
  letter-spacing: -0.2px;
}}

/* ── Сводная карточка проверки ─────────────────────── */
.check-meta-table {{
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 5mm;
  font-size: 9pt;
}}
.check-meta-table th {{
  background: {DARK_BLUE};
  color: {WHITE};
  text-align: left;
  font-weight: 600;
  padding: 5px 8px;
  font-size: 8.5pt;
  white-space: nowrap;
}}
.check-meta-table td {{
  padding: 5px 8px;
  vertical-align: top;
  border-bottom: 0.5px solid #E0DDD6;
}}
.check-meta-table tr:nth-child(even) td {{ background: {OFF_WHITE}; }}

/* ── Блок-карточка (вывод / задаток / рекомендации) ── */
.card {{
  border-radius: 3px;
  border: 0.5px solid #E0DDD6;
  padding: 6px 10px 7px;
  margin-bottom: 3mm;
}}
.card-title {{
  font-size: 9.5pt;
  font-weight: 700;
  color: {DARK_BLUE};
  margin-bottom: 3px;
}}
.card p {{
  font-size: 9pt;
  line-height: 1.5;
  margin: 0;
}}

/* ── Чеклист проверок ─────────────────────────────── */
.checklist-item {{
  display: grid;
  grid-template-columns: 52mm 18mm 1fr;
  border: 0.5px solid #E0DDD6;
  border-radius: 2px;
  margin-bottom: 1.5mm;
  font-size: 8.8pt;
  page-break-inside: avoid;
}}
.checklist-item > div {{
  padding: 5px 7px;
  vertical-align: top;
}}
.item-title {{
  font-weight: 600;
  color: {DARK_BLUE};
  font-size: 9pt;
  border-right: 0.5px solid #E0DDD6;
}}
.item-badge {{
  text-align: center;
  font-weight: 700;
  font-size: 8pt;
  letter-spacing: 0.3px;
  border-right: 0.5px solid #E0DDD6;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  padding-top: 6px;
  gap: 2px;
}}
.item-badge .icon {{
  font-size: 12pt;
  line-height: 1;
}}
.item-body {{ }}
.item-body .summary {{
  font-size: 9pt;
  margin-bottom: 2px;
}}
.item-body .detail {{
  font-size: 8pt;
  color: {MID_GRAY};
  line-height: 1.4;
  margin-top: 2px;
}}
.item-body .detail a {{
  color: #1A4F8F;
}}

/* ── Таблица ручной проверки ──────────────────────── */
.manual-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 8.8pt;
  margin-top: 2mm;
}}
.manual-table th {{
  background: {DARK_BLUE};
  color: {WHITE};
  text-align: left;
  padding: 5px 7px;
  font-size: 8.5pt;
  font-weight: 600;
}}
.manual-table td {{
  padding: 5px 7px;
  vertical-align: top;
  border-bottom: 0.5px solid #E0DDD6;
  line-height: 1.4;
}}
.manual-table tr:nth-child(even) td {{ background: {OFF_WHITE}; }}
.manual-table .law {{ color: {MID_GRAY}; font-size: 7.8pt; }}

/* ── Юридическое заключение ─────────────────────── */
.legal-h2 {{
  font-size: 9.5pt;
  font-weight: 700;
  color: {DARK_BLUE};
  margin: 4mm 0 1.5mm;
}}
.legal-body {{
  font-size: 9pt;
  line-height: 1.5;
  margin-bottom: 1.5mm;
}}
.legal-bullet {{
  font-size: 9pt;
  line-height: 1.5;
  padding-left: 8px;
  margin-bottom: 1px;
}}
.legal-bullet::before {{
  content: "•  ";
  color: {DARK_BLUE};
  font-weight: 700;
}}

/* ── Дисклеймер ──────────────────────────────────── */
.disclaimer {{
  background: {OFF_WHITE};
  border: 0.5px solid #E0DDD6;
  border-radius: 3px;
  padding: 6px 10px;
  font-size: 8pt;
  color: {MID_GRAY};
  margin-top: 7mm;
  line-height: 1.5;
}}
"""


# ── Блок «что проверялось» ─────────────────────────────────────────────────
def _meta_table_html(report: Dict) -> str:
    checklist = report.get("checklist") or []
    participants = report.get("participants") or []

    rows = ""

    # Режим
    has_owners = any(
        not (item.get("data") or {}).get("manual_only")
        for item in checklist
    )
    mode = "Продавец + объект" if has_owners else "Только объект"
    rows += f'<tr><td width="27%">Режим</td><td>{_esc(mode)}</td></tr>'

    # Продавцы
    if participants:
        lines = []
        for idx, part in enumerate(participants, 1):
            label = _clean(part.get("label") or f"Продавец {idx}", 60)
            age   = f", возраст: {part.get('age')} лет" if part.get("age") else ""
            share = f", доля: {part.get('share')}" if part.get("share") else ""
            lines.append(f"{label}{age}{share}")
        rows += f'<tr><td>Продавец(ы)</td><td>{"<br>".join(_esc(l) for l in lines)}</td></tr>'

    # Объект (из первого чеклист-итема с данными ЕГРН/НСПД)
    for item in checklist:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        obj_bits = []
        if data.get("cadNumber"):  obj_bits.append(f"Кад. номер: {data['cadNumber']}")
        if data.get("objType_text"): obj_bits.append(f"Тип: {data['objType_text']}")
        if data.get("area"):       obj_bits.append(f"Площадь: {data['area']} кв.м")
        obj = data.get("object") if isinstance(data.get("object"), dict) else {}
        if obj.get("address"):    obj_bits.insert(0, f"Адрес: {_clean(obj['address'], 120)}")
        if obj.get("cad_cost"):   obj_bits.append(f"Кад. стоимость: {_rub(obj['cad_cost'])}")
        if obj_bits:
            rows += f'<tr><td>Объект</td><td>{"<br>".join(_esc(b) for b in obj_bits)}</td></tr>'
            break

    return f"""
<table class="check-meta-table">
  <tr><th width="27%">Параметр</th><th>Данные в отчёте</th></tr>
  {rows}
</table>"""


# ── Один элемент чеклиста ──────────────────────────────────────────────────
def _checklist_item_html(item: Dict) -> str:
    st = item.get("status", "manual_check")
    bg_col, badge_color, text_color = STATUS_COLOR.get(st, (MANUAL_BG, MANUAL, MANUAL))
    icon  = STATUS_ICON.get(st, "?")
    label = STATUS_LABEL.get(st, "?")

    data   = item.get("data") if isinstance(item.get("data"), dict) else {}
    details_html = ""

    # Детали / саб-данные
    if data.get("active_count") is not None:
        details_html += f'<div class="detail">Активных: {data.get("active_count", 0)}, '
        details_html += f'закрытых: {data.get("closed_count", 0)}, '
        details_html += f'долг: {_rub(data.get("actual_debt", 0))}</div>'
        for group, rows_d in [("Активные ФССП", data.get("active_items") or []),
                               ("Закрытые ФССП", data.get("closed_items") or [])]:
            for ip in rows_d[:5]:
                parts = []
                if ip.get("ip_number"): parts.append(f"ИП: {ip['ip_number']}")
                if ip.get("subject"):   parts.append(f"{_clean(ip['subject'], 80)}")
                if parts:
                    details_html += f'<div class="detail">{_esc(" · ".join(parts))}</div>'

    elif data.get("cadNumber") or data.get("area"):
        for bit in [
            data.get("cadNumber")  and f"Кад. номер: {data['cadNumber']}",
            data.get("objType_text") and f"Тип: {data['objType_text']}",
            data.get("area") and f"Площадь: {data['area']} кв.м",
        ]:
            if bit: details_html += f'<div class="detail">{_esc(bit)}</div>'
        if data.get("rights"):
            rights = data["rights"] if isinstance(data["rights"], list) else []
            details_html += f'<div class="detail">Зарегистрированных прав: {len(rights)}</div>'
            for idx, r in enumerate(rights[:6], 1):
                if isinstance(r, dict):
                    parts = [f"Право {idx}"]
                    if r.get("rightHolder") or r.get("owner"):
                        parts.append(_clean(r.get("rightHolder") or r.get("owner"), 55))
                    if r.get("registrationDate") or r.get("regDate"):
                        parts.append(_clean(r.get("registrationDate") or r.get("regDate")))
                    details_html += f'<div class="detail">{_esc(" · ".join(parts))}</div>'
        if data.get("encumbrances"):
            enc = data["encumbrances"] if isinstance(data["encumbrances"], list) else []
            details_html += f'<div class="detail">Обременений: {len(enc)}</div>'
            for idx, e in enumerate(enc[:4], 1):
                if isinstance(e, dict):
                    parts = [f"Обременение {idx}"]
                    if e.get("type") or e.get("encumbranceType"):
                        parts.append(_clean(e.get("type") or e.get("encumbranceType"), 40))
                    if e.get("holder") or e.get("encumbranceHolder"):
                        parts.append(_clean(e.get("holder") or e.get("encumbranceHolder"), 55))
                    details_html += f'<div class="detail">{_esc(" · ".join(parts))}</div>'

    elif data.get("geo_center"):
        obj = data.get("object") or {}
        if obj.get("address"): details_html += f'<div class="detail">{_esc(_clean(obj["address"], 100))}</div>'
        if obj.get("cad_cost"): details_html += f'<div class="detail">Кад. стоимость: {_rub(obj["cad_cost"])}</div>'
        if obj.get("year_built"): details_html += f'<div class="detail">Год постройки: {obj["year_built"]}</div>'
        gc = data.get("geo_center")
        if isinstance(gc, dict) and gc.get("lat"):
            lat, lon = gc["lat"], gc["lon"]
            url = f"https://yandex.ru/maps/?ll={lon},{lat}&z=17&pt={lon},{lat},pm2rdm"
            details_html += f'<div class="detail"><a href="{url}">Открыть на карте</a></div>'

    elif not details_html:
        for d in (item.get("details") or [])[:4]:
            details_html += f'<div class="detail">{_esc(d)}</div>'

    # Структурированные ссылки
    for ln in (data.get("links") or [])[:4]:
        if isinstance(ln, dict) and ln.get("url"):
            details_html += f'<div class="detail"><a href="{_esc(ln["url"])}">{_esc(ln.get("label", ln["url"]))}</a></div>'

    return f"""
<div class="checklist-item" style="background:{bg_col}">
  <div class="item-title">{_esc(item.get("title", ""))}</div>
  <div class="item-badge" style="color:{badge_color}">
    <span class="icon">{icon}</span>
    <span>{label}</span>
  </div>
  <div class="item-body">
    <div class="summary">{_nl(item.get("summary", ""))}</div>
    {details_html}
  </div>
</div>"""


# ── Юридическое заключение ─────────────────────────────────────────────────
def _legal_html(text: str) -> str:
    if not text:
        return ""
    headings = {
        "Краткий вывод", "Что подтверждено автоматическими источниками",
        "Что не подтверждено и требует ручной проверки", "Ключевые риски",
        "Ключевые угрозы для покупателя", "Что проверить до аванса",
        "Логика сделки", "Как передавать задаток / аванс",
        "Как передавать аванс", "Итоговое заключение", "Важно",
    }
    out = []
    para = []
    blocks_cnt = 0
    bullets_cnt = 0

    def flush():
        if para:
            out.append(f'<p class="legal-body">{_esc(" ".join(para))}</p>')
            para.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush(); continue
        if s in headings:
            flush()
            blocks_cnt += 1
            if blocks_cnt <= 8:
                out.append(f'<p class="legal-h2">{_esc(s)}</p>')
        elif s.startswith("•") or s.startswith("-"):
            flush()
            bullets_cnt += 1
            if bullets_cnt <= 25:
                out.append(f'<div class="legal-bullet">{_esc(s[1:].strip())}</div>')
        else:
            para.append(s)
    flush()
    return "\n".join(out)


# ── Главная функция ────────────────────────────────────────────────────────
def build_pdf_bytes(report: Dict) -> bytes:
    """
    Drop-in замена для build_pdf_bytes из main.py.
    Требует: pip install weasyprint
    """
    try:
        from weasyprint import HTML, CSS as WeasyCSS
    except ImportError:
        raise RuntimeError(
            "WeasyPrint не установлен. Выполните: pip install weasyprint"
        )

    scoring   = report.get("risk_scoring") or {}
    checklist = report.get("checklist")    or []
    recs      = report.get("recommendations") or []
    hidden    = report.get("hidden_risks") or []
    advance   = report.get("advance_decision") or {}
    legal_txt = report.get("legal_report") or ""

    score = int(scoring.get("score", 0))
    sc    = _score_color(score)

    # ── Шапка ──────────────────────────────────────────────────────────────
    header_html = f"""
<div class="page-header">
  <div class="header-left">
    <h1>Комплексная проверка<br>продавца и объекта недвижимости</h1>
    <div class="subtitle">Дата формирования: {_esc(report.get("created_at", "—"))}</div>
  </div>
  <div class="risk-badge" style="border-color:{sc}88">
    <span class="badge-label" style="color:{sc}">{_esc(_score_label(score))}</span>
    <span class="badge-score" style="color:{sc}">{score}</span>
    <span class="badge-max">из 100</span>
  </div>
</div>"""

    # ── Мета-таблица ────────────────────────────────────────────────────────
    meta_html = _meta_table_html(report)

    # ── Главный вывод + решение по задатку ─────────────────────────────────
    adv_bg = HIGH_BG if score >= 60 else (MEDIUM_BG if score >= 35 else LOW_BG)
    summary_html = f"""
<div class="section-title">Главный вывод</div>
<div class="card" style="background:{OFF_WHITE}">
  <div class="card-title">Заключение</div>
  <p>{_nl(scoring.get("conclusion", "—"))}</p>
</div>
<div class="card" style="background:{adv_bg}">
  <div class="card-title">Решение по задатку / авансу</div>
  <p>{_esc(advance.get("decision", "—"))}.</p>
  <p style="margin-top:3px;color:{MID_GRAY};font-size:8.5pt">{_esc(advance.get("comment", ""))}</p>
</div>"""

    # ── Чеклист ─────────────────────────────────────────────────────────────
    checks_html = '<div class="section-title">Результаты проверок</div>\n'
    for item in checklist:
        checks_html += _checklist_item_html(item)

    # ── Юридическое заключение ──────────────────────────────────────────────
    legal_html = ""
    if legal_txt:
        legal_html = f'<div class="section-title">Юридическое заключение</div>\n{_legal_html(legal_txt)}'

    # ── Рекомендации ────────────────────────────────────────────────────────
    recs_html = ""
    if recs:
        recs_html = '<div class="section-title">Рекомендации</div>\n'
        for rec in recs:
            pri = rec.get("priority", "")
            bg  = CRITICAL_BG if pri == "critical" else (HIGH_BG if pri == "high" else OFF_WHITE)
            recs_html += f"""
<div class="card" style="background:{bg}">
  <div class="card-title">{_esc(rec.get("title", ""))}</div>
  <p>{_nl(rec.get("text", ""))}</p>
</div>"""

    # ── Ручная проверка ─────────────────────────────────────────────────────
    manual_html = ""
    if hidden:
        manual_html = '<div class="section-title">Дополнительно рекомендуется проверить</div>\n<table class="manual-table">'
        manual_html += '<tr><th width="32%">Что проверить</th><th width="47%">Зачем</th><th>Норма</th></tr>'
        for r in hidden:
            manual_html += f"""<tr>
  <td>{_esc(r.get("risk", ""))}</td>
  <td>{_esc(r.get("why", ""))}</td>
  <td class="law">{_esc(r.get("law", ""))}</td>
</tr>"""
        manual_html += "</table>"

    # ── Дисклеймер ──────────────────────────────────────────────────────────
    disclaimer_html = """
<div class="disclaimer">
  Настоящее заключение носит информационно-аналитический характер.
  Подготовлено на основании данных из открытых государственных реестров.
  Не заменяет юридическую проверку правоустанавливающих документов.
  Рекомендуется привлечь квалифицированного специалиста по недвижимости или нотариуса.
</div>"""

    # ── Сборка HTML ──────────────────────────────────────────────────────────
    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <style>{CSS}</style>
</head>
<body>
  {header_html}
  {meta_html}
  {summary_html}
  {checks_html}
  {legal_html}
  {recs_html}
  {manual_html}
  {disclaimer_html}
</body>
</html>"""

    return HTML(string=full_html).write_pdf()
