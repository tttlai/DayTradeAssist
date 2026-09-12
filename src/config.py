import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


# Angel One SmartAPI (read-only: quotes & historical candles only, never order placement)
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
ANGEL_PIN = os.getenv("ANGEL_PIN", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Risk / budget — all overridable via env vars so budget can change day to day
# without touching code (Railway lets you edit env vars per run/redeploy).
MAX_BUDGET = _get_float("MAX_BUDGET", 50000.0)
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 0.01)
MAX_PICKS = _get_int("MAX_PICKS", 3)
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "15:15")

UNIVERSE_CSV = ROOT_DIR / "data" / "universe.csv"
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
SCRIP_MASTER_CACHE = ROOT_DIR / "data" / "scrip_master_cache.json"
