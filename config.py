import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _ids_env(name: str) -> set[int]:
    result = set()
    for item in os.getenv(name, "").split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                raise ValueError(f"{name} must contain comma-separated numeric Telegram IDs")
    return result

@dataclass(frozen=True)
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    api_id: int = _int_env("API_ID", 0)
    api_hash: str = os.getenv("API_HASH", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    super_admins: set[int] = None
    default_delay: int = _int_env("DEFAULT_DELAY", 300)
    min_delay: int = _int_env("MIN_DELAY", 120)
    max_delay: int = _int_env("MAX_DELAY", 1200)
    port: int = _int_env("PORT", 10000)
    signature: str = "\n\nотправлено через @jingizbot"

config = Config(super_admins=_ids_env("SUPER_ADMINS"))
if not config.bot_token:
    raise RuntimeError("BOT_TOKEN is missing")
if not config.database_url:
    raise RuntimeError("DATABASE_URL is missing")
if not config.api_id or not config.api_hash:
    raise RuntimeError("API_ID and API_HASH are required for Telethon")
if config.min_delay < 1 or config.max_delay < config.min_delay:
    raise RuntimeError("Invalid MIN_DELAY/MAX_DELAY configuration")
