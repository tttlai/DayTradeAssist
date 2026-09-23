"""
Free, login-free source of daily NSE equity OHLCV data.

Used for the pre-market watchlist screen and the backtest -- neither
needs live/intraday data, so there's no reason to spend an Angel One
login (and its much stricter rate limits) on them. NSE publishes a daily
"bhavcopy" file with end-of-day OHLCV for every listed instrument,
reachable without any credentials.

Two things confirmed empirically before relying on this (see chat/commit
history, not repeated here): NSE's CDN silently hangs connections that
don't send a browser-like User-Agent (no clean 403/429 -- it just times
out), so one is always sent; and a missing date (weekend/holiday) comes
back as a clean 404, which is what lets fetch_daily_history() walk
backward transparently.

Only src/screener.py's build_watchlist() and src/backtest.py use this
module. src/angel_api.py (needing a live login) still handles the
confirmation/summary phases, which genuinely need live intraday data.

If NSE changes their bhavcopy URL format again (they have before), this
is the one place to update it. Confirmed in production (2026-09-23): NSE
can also return a hard 403 for every date attempted, seemingly IP-based
(the identical URL worked fine from a different network at the same
moment) -- likely NSE blocking Railway's shared egress IP range, nothing
wrong with the request itself. fetch_daily_history() raises
NSEUnavailable in that case rather than silently returning nothing;
main.py::run_watchlist() catches it and falls back to Angel One's own
daily-candle API for that day.
"""
import csv
import io
import time
import zipfile
from datetime import date, timedelta

import requests

from . import timeutil


class NSEUnavailable(Exception):
    """NSE responded, but with an error status other than 404 -- most
    likely blocked (IP-based) or their site/format changed, not a
    date-specific gap. Not worth retrying other dates for; the caller
    should fall back to a different data source instead."""

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 20
REQUEST_SLEEP = 0.3  # be polite to NSE's servers across a multi-day fetch


def fetch_bhavcopy_for_date(d: date) -> dict | None:
    """Returns {symbol: [date_str, open, high, low, close, volume]} for
    every regular-equity (SctySrs == 'EQ') NSE stock on that date, or
    None if there's no file for that date (weekend/holiday/too-recent).

    Raises NSEUnavailable for any non-404 error status (403, 500, etc) --
    a real server response rejecting the request, not a network blip, so
    retrying a different date is pointless; the caller should stop and
    fall back. A network-level failure (timeout, connection error)
    instead returns None like a 404, since those are more likely
    transient and date-independent -- worth trying another date for.
    """
    url = BHAVCOPY_URL.format(date_str=d.strftime("%Y%m%d"))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"[nse_data] request failed for {d}: {e}")
        return None

    if resp.status_code == 404:
        return None
    if not resp.ok:
        print(f"[nse_data] NSE returned HTTP {resp.status_code} for {d} -- likely blocked or format changed")
        raise NSEUnavailable(f"HTTP {resp.status_code} for {url}")

    rows = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                if row.get("Sgmt") != "CM" or row.get("SctySrs") != "EQ":
                    continue
                try:
                    rows[row["TckrSymb"]] = [
                        row["TradDt"],
                        float(row["OpnPric"]),
                        float(row["HghPric"]),
                        float(row["LwPric"]),
                        float(row["ClsPric"]),
                        float(row["TtlTradgVol"]),
                    ]
                except (KeyError, ValueError):
                    continue
    return rows


def fetch_daily_history(symbols: list[str], days: int) -> dict[str, list]:
    """Walks backward from today (IST) collecting one bhavcopy per trading
    day until `days` days of history are gathered, skipping weekends and
    holidays automatically (a missing file just means try the previous
    day). Returns {symbol: [candle, candle, ...]}, oldest first -- same
    shape as AngelAPI.get_daily_candles(), so screener.evaluate() doesn't
    care which source produced it.
    """
    symbol_set = set(symbols)
    history = {s: [] for s in symbols}
    d = timeutil.now_ist().date()
    collected_days = 0
    max_lookback = days * 3 + 15  # buffer for weekends + holidays

    for _ in range(max_lookback):
        if collected_days >= days:
            break
        day_rows = fetch_bhavcopy_for_date(d)
        d -= timedelta(days=1)
        if day_rows is None:
            continue
        collected_days += 1
        for symbol in symbol_set:
            if symbol in day_rows:
                history[symbol].append(day_rows[symbol])
        time.sleep(REQUEST_SLEEP)

    if collected_days < days:
        print(
            f"[nse_data] only found {collected_days}/{days} trading days of "
            "history -- NSE's site may be unreachable or its URL format may "
            "have changed again."
        )

    for symbol in history:
        history[symbol].sort(key=lambda c: c[0])

    return history
