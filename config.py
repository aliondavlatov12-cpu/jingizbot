# language: Python, file: config.py
# Танҳо .env-ро мехонад. Ягон мантиқ нест.

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------- Telegram Bot ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN дар .env нест. .env.example-ро бинед.")

# ---------- Telegram API (барои userbot) ----------
_api_id = os.getenv("API_ID")
if not _api_id:
    raise RuntimeError("API_ID дар .env нест.")
API_ID = int(_api_id)

API_HASH = os.getenv("API_HASH")
if not API_HASH:
    raise RuntimeError("API_HASH дар .env нест.")

# ---------- Админҳои аввал ----------
SUPER_ADMINS = [
    int(x.strip())
    for x in os.getenv("SUPER_ADMINS", "").split(",")
    if x.strip().isdigit()
]

# ---------- Фосила (сония) ----------
DEFAULT_DELAY = int(os.getenv("DEFAULT_DELAY", "300"))
MIN_DELAY     = int(os.getenv("MIN_DELAY", "120"))
MAX_DELAY     = int(os.getenv("MAX_DELAY", "1200"))

# ---------- Нархи пешфарзи нест кардани имзо ----------
DEFAULT_UNSIGN_PRICE = int(os.getenv("DEFAULT_UNSIGN_PRICE", "100"))

# ---------- Роҳҳо ----------
DB_PATH = os.getenv("DB_PATH", "bot.db")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")

# Ҷузвдони sessions-ро фавран созед, то дертар хатогӣ нашавад
Path(SESSIONS_DIR).mkdir(exist_ok=True)


# ---------- Нархҳои пешфарзи функсияҳо (барои ҷадвали prices) ----------
# 0 = ройгон барои ҳама
DEFAULT_PRICES = {
    "connect_price":          0,     # пайваст кардани аккаунт
    "broadcast_price":        0,     # рассылка
    "unsign_price":           100,   # нест кардани имзо
    "timer_price":            0,     # таймер
    "groups_refresh_price":   0,     # навсозии гуруҳҳо
    "fetch_groups_price":     0,     # хондани рӯйхати гуруҳҳо
}
