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


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    return val.strip().lower() in ("1", "true", "yes") if val else default


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

# Requires price to be on the "right side" of the day's VWAP (above for a
# LONG, below for a SHORT) before confirming a trigger -- a standard,
# well-evidenced intraday order-flow filter. Can't be backtested against
# our free NSE data (VWAP needs real intraday candles, which the daily
# bhavcopy doesn't have), so this is validated by watching live results,
# not a historical backtest. Set REQUIRE_VWAP_CONFIRMATION=false to
# disable if it turns out not to help.
REQUIRE_VWAP_CONFIRMATION = _get_bool("REQUIRE_VWAP_CONFIRMATION", True)

# Don't enter once price is already more than this many ATRs past the
# trigger -- a chase, with a stop sitting right back at the broken level.
# Backtest (realistic fills): entries >0.5 ATR past the trigger lost money
# and excluding them took 120 days from Rs -3.8k to Rs -0.9k. 0 disables.
MAX_CHASE_ATR = _get_float("MAX_CHASE_ATR", 0.5)

# Daily LLM-powered eval agent (src/eval_agent.py) -- optional. Without an
# API key it just skips itself (no error), same as Telegram being
# unconfigured. This is the one real recurring cost in this project: a
# small per-day charge to your Anthropic account for one API call, unlike
# everything else here which is free.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EVAL_MODEL = os.getenv("EVAL_MODEL", "claude-sonnet-5")

# Lets the eval agent open a GitHub PR proposing a tweak to one of a
# pre-approved whitelist of numeric constants (src/github_pr.py), which
# you approve or reject by replying to the Telegram message that names
# it. Optional -- without GITHUB_TOKEN set, this step is skipped and the
# eval just gives its prose analysis as before. Use a fine-grained PAT
# scoped to ONLY this one repo, with Contents + Pull requests read/write
# and nothing else.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # "owner/repo"
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "master")

UNIVERSE_CSV = ROOT_DIR / "data" / "universe.csv"
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
SCRIP_MASTER_CACHE = ROOT_DIR / "data" / "scrip_master_cache.json"
