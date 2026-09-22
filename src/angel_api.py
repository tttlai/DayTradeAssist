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


class AngelAPI:
    def __init__(self):
        self._client = None
        self._scrip_master = None

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

    def get_ltp(self, symbol: str, token: str) -> float | None:
        try:
            data = self._client.ltpData("NSE", f"{symbol}-EQ", token)
            if data.get("status"):
                return float(data["data"]["ltp"])
        except Exception:
            return None
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
        try:
            result = self._client.getCandleData(params)
        except Exception as e:
            print(f"[angel_api] get_daily_candles({token}) raised {e.__class__.__name__}: {e}")
            return []
        if not result.get("status"):
            return []
        return result.get("data", [])[-days:]

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
        try:
            result = self._client.getCandleData(params)
        except Exception as e:
            print(f"[angel_api] get_intraday_candles({token}) raised {e.__class__.__name__}: {e}")
            return []
        if not result.get("status"):
            return []
        return result.get("data", [])
