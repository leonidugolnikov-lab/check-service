"""
Real Estate Seller & Property Check API — v5.0
Copyright (c) 2026 Ugolnikov SPb. All rights reserved.

v5.0 — Архитектурный рефакторинг:
- Основной метод проверки продавца: complex_by_passport (1 запрос вместо 8)
  Передаём оба варианта полей паспорта: seria/number + seriapass/numberpass
- Суды: pravo_search → фильтрация по роли + категории → scoring → pravo_cases_details
  только для значимых дел (score >= 50)
- Объект: rosreestr + nspd_cadastr (геоданные и характеристики)
- Умный polling: адаптивные интервалы, разные таймауты по методу
- Итого запросов: 3 на продавца + 2 на объект + N карточек судебных дел
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# -------------------- Логирование --------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# -------------------- PDF --------------------
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable
    )
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False
    logger.warning("ReportLab не доступен. PDF-генерация отключена.")

# -------------------- Настройки --------------------
APP_VERSION = "5.0.0"
NEWDB_URL = "https://api.newdb.net/v2"
NEWDB_TOKEN = os.getenv("NEWDB_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
USE_DEEPSEEK_REPORT = os.getenv("USE_DEEPSEEK_REPORT", "0").strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
SHOW_RAW_REGISTRY_DATA = os.getenv("SHOW_RAW_REGISTRY_DATA", "0").strip().lower() in {"1", "true", "yes", "on"}
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
REPORT_FROM_EMAIL = os.getenv("REPORT_FROM_EMAIL", "Ugolnikov SPb <reports@ugolnikovspb.ru>").strip()
REPORT_REPLY_TO_EMAIL = os.getenv("REPORT_REPLY_TO_EMAIL", "").strip()

# Безопасность
ALLOWED_ORIGINS = ["https://ugolnikovspb.ru", "https://www.ugolnikovspb.ru"]
PUBLIC_WIDGET_API_KEY = os.getenv("PUBLIC_WIDGET_API_KEY", "")
ENABLE_DEBUG_NEWDB = os.getenv("ENABLE_DEBUG_NEWDB", "0").lower() in {"1", "true", "yes", "on"}
DEBUG_API_KEY = os.getenv("DEBUG_API_KEY", "")
REPORT_TTL_SECONDS = int(os.getenv("REPORT_TTL_SECONDS", "43200"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW = 3600
MAX_OWNERS = int(os.getenv("MAX_OWNERS", "50"))

# Cloudflare Turnstile (бесплатная капча) — опциональная защита от ботов.
# Регистрация: https://dash.cloudflare.com/?to=/:account/turnstile
# Если TURNSTILE_SECRET не задан — проверка отключена.
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "").strip()
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Кеш ответов NewDB. Снижает расход токенов на повторных проверках.
NEWDB_CACHE_ENABLED = os.getenv("NEWDB_CACHE_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
NEWDB_CACHE_TTL = int(os.getenv("NEWDB_CACHE_TTL", "86400"))  # 24 часа
NEWDB_CACHE_MAX = int(os.getenv("NEWDB_CACHE_MAX", "500"))

# Таймауты по методу (секунды)
# Таймауты увеличены — NewDB обрабатывает complex_by_passport и pravo_search до 3-5 минут
# По логам: pravo_search тайм-аутил на 120с (ещё шёл), complex_by_passport на ~180с (ещё шёл)
METHOD_TIMEOUTS = {
    "complex_by_passport": 360,   # до 6 минут — включает 5-10 субпроверок
    "pravo_search": 300,          # до 5 минут — поиск по всем судам
    "pravo_cases_details": 120,
    "rosreestr": 300,
    "nspd_cadastr": 90,
}
