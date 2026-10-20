"""
Thin read-only wrapper around Angel One's SmartAPI.

Only ever calls quote / historical-candle / instrument-master endpoints.
This module intentionally has no method that places, modifies, or cancels
an order — the assistant is signal-only by design.
"""
import json
import time
from datetime import timedelta

import pyotp
import requests
from SmartApi import SmartConnect

from . import config, timeutil

# Angel rejects bursts ("Access denied because of exceeding access rate"), and
# the SDK surfaces that as a JSON-parse exception. Space calls out, and retry a
# rate-limited or failed call a couple of times with backoff before giving up.
MIN_CALL_GAP_SECONDS = 0.35
RETRY_DELAYS = (1.0, 2.5)
_RATE_LIMIT_MARKERS = ("access rate", "exceeding", "too many")


def _is_rate_limited(text: str) -> bool:
    text = (text or "").lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


class AngelAPI:
    def __init__(self):
        self._client = None
        self._scrip_master = None
        self.last_error: str | None = None  # why the most recent data call failed
        self._last_call_at = 0.0

    def login(self):
        totp = pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()
        self._client = SmartConnect(api_key=config.ANGEL_API_KEY)
        session = self._client.generateSession(
            config.ANGEL_CLIENT_CODE, config.ANGEL_PIN, totp
        )
        if not session.get("status"):
            raise RuntimeError(f"Angel One login failed: {session}")
        return session

    def logout(self):
        if self._client:
            try:
                self._client.terminateSession(config.ANGEL_CLIENT_CODE)
            except Exception:
                pass

    def _load_scrip_master(self):
        if self._scrip_master is not None:
            return self._scrip_master

        if config.SCRIP_MASTER_CACHE.exists():
            age = time.time() - config.SCRIP_MASTER_CACHE.stat().st_mtime
            if age < 24 * 3600:
                self._scrip_master = json.loads(
                    config.SCRIP_MASTER_CACHE.read_text()
                )
                return self._scrip_master

        try:
            resp = requests.get(config.SCRIP_MASTER_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            config.SCRIP_MASTER_CACHE.write_text(json.dumps(data))
            self._scrip_master = data
            return data
        except requests.RequestException as e:
            # Instrument tokens rarely change day to day -- a stale cache
            # is far more useful than crashing the whole watchlist run
            # over a fetch that failed. Fall back to it if one exists at
            # all, however old; only give up empty if there's truly
            # nothing on disk yet.
            print(f"[angel_api] scrip master fetch failed ({e.__class__.__name__}): {e}")
            if config.SCRIP_MASTER_CACHE.exists():
                print("[angel_api] falling back to stale cached scrip master")
                self._scrip_master = json.loads(config.SCRIP_MASTER_CACHE.read_text())
                return self._scrip_master
            self._scrip_master = []
            return self._scrip_master

    def get_token(self, symbol: str) -> str | None:
        """Look up the NSE equity instrument token for a trading symbol."""
        master = self._load_scrip_master()
        target = f"{symbol}-EQ"
        for row in master:
            if row.get("exch_seg") == "NSE" and row.get("symbol") == target:
                return row.get("token")
        return None

    def _pace(self):
        wait = MIN_CALL_GAP_SECONDS - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _call(self, label: str, fn):
        """Runs one Angel data request with pacing and retries. Returns the
        reply dict on success (status true), else None with self.last_error
        set. Exceptions and rate-limit replies are retried with backoff; a
        definite refusal (invalid token, etc.) is not."""
        self.last_error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            self._pace()
            try:
                result = fn()
            except Exception as e:
                self.last_error = f"{e.__class__.__name__}: {str(e)[:120]}"
            else:
                if isinstance(result, dict) and result.get("status"):
                    self.last_error = None  # a retry that worked leaves no stale error behind
                    return result
                if isinstance(result, dict):
                    self.last_error = f"{result.get('message')} ({result.get('errorcode')})"
                else:
                    self.last_error = f"unexpected reply: {str(result)[:80]}"
                if not _is_rate_limited(self.last_error):
                    break
            if attempt < len(RETRY_DELAYS):
                print(f"[angel_api] {label} failed ({self.last_error}); retrying in {RETRY_DELAYS[attempt]}s")
                time.sleep(RETRY_DELAYS[attempt])
        print(f"[angel_api] {label} failed: {self.last_error}")
        return None

    def get_ltp(self, symbol: str, token: str) -> float | None:
        result = self._call(f"get_ltp({symbol})", lambda: self._client.ltpData("NSE", f"{symbol}-EQ", token))
        if result is None:
            return None
        try:
            return float(result["data"]["ltp"])
        except Exception as e:
            self.last_error = f"{e.__class__.__name__}: {str(e)[:120]}"
            print(f"[angel_api] get_ltp({symbol}) malformed reply: {self.last_error}")
            return None

    def get_daily_candles(self, token: str, days: int = 40):
        """Returns list of [timestamp, open, high, low, close, volume].
        Never raises -- an empty list on any failure, same contract as a
        "status": false response, so callers don't need to know which
        kind of failure occurred."""
        to_date = timeutil.now_ist()
        from_date = to_date - timedelta(days=days * 2)  # buffer for weekends/holidays
        params = {
            "exchange": "NSE",
            "symboltoken": token,
            "interval": "ONE_DAY",
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        result = self._call(f"get_daily_candles({token})", lambda: self._client.getCandleData(params))
        if result is None:
            return []
        return (result.get("data") or [])[-days:]

    def get_intraday_candles(self, token: str, interval="FIFTEEN_MINUTE"):
        """Today's (IST) intraday candles, used for confirmation + summary.
        Never raises -- same empty-list-on-any-failure contract as
        get_daily_candles()."""
        today = timeutil.now_ist()
        from_date = today.replace(hour=9, minute=15, second=0, microsecond=0)
        params = {
            "exchange": "NSE",
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": today.strftime("%Y-%m-%d %H:%M"),
        }
        result = self._call(f"get_intraday_candles({token})", lambda: self._client.getCandleData(params))
        if result is None:
            return []
        data = result.get("data") or []
        if not data:
            self.last_error = "Angel returned an empty candle list"
        return data
