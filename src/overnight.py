"""
Overnight shock watch: which Indian stocks have historically gapped down the
most on mornings after a big US sell-off.

For each NSE stock, over the past ~265 trading days, take the mornings whose
preceding US session (the latest one dated before that NSE day) closed down
at least US_DROP in the S&P 500, and average the stock's opening gap (and what
it did from the open to the close). Subtract the stock's own normal gap on
calm mornings so a stock that always gaps up a little isn't flattered. This
is an average of what actually happened, not a fitted slope: a regression on
so few big days gave implausible magnitudes, so no point forecast is made.

Pure functions (no network) except fetch_us_moves; the cached stats live in
the persistent data dir because building them reads a year of whole-market
NSE files (a few minutes).
"""
import datetime as dt
import json
from urllib.parse import quote

import requests

from . import config, nse_data, penny

US_DROP = -0.01  # an S&P day this bad (or worse) counts as an event
MIN_EVENTS = 25
CALM = -0.003  # the S&P was no worse than this on "calm" mornings
STATS_MAX_AGE_DAYS = 7
LIQUID_VALUE = 1e8  # Rs 10 crore a day
LIQUID_MIN_PRICE = 20
COMMODITY_WORDS = ("SILV", "GOLD")  # silver/gold ETFs: their gap is the metal's move

MOVES = {  # label -> Yahoo symbol
    "S&P 500": "^GSPC", "Nasdaq": "^IXIC", "Chips (SOX)": "^SOX", "VIX": "^VIX",
    "Silver": "SI=F", "Gold": "GC=F", "Copper": "HG=F",
}


def _closes(symbol: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=2y&interval=1d"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    out = {}
    for ts, close in zip(result["timestamp"], result["indicators"]["quote"][0]["close"]):
        if close:
            out[dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()] = close
    return out


def _returns(closes: dict) -> dict:
    days = sorted(closes)
    return {d: closes[days[i]] / closes[days[i - 1]] - 1 for i, d in enumerate(days) if i and closes[days[i - 1]]}


def fetch_us_moves() -> dict | None:
    """{"as_of": date, "moves": {label: fraction}, "spx": {date: fraction}} for
    the latest US session, or None if the S&P itself can't be fetched. A
    missing secondary series just leaves its label out. Never raises."""
    try:
        spx = _returns(_closes(MOVES["S&P 500"]))
    except Exception as e:
        print(f"[overnight] S&P fetch failed: {e.__class__.__name__}: {e}")
        return None
    if not spx:
        return None
    as_of = max(spx)
    moves = {"S&P 500": spx[as_of]}
    for label, symbol in MOVES.items():
        if label == "S&P 500":
            continue
        try:
            series = _returns(_closes(symbol))
            if as_of in series:
                moves[label] = series[as_of]
        except Exception as e:
            print(f"[overnight] {label} fetch failed: {e.__class__.__name__}: {e}")
    return {"as_of": as_of, "moves": moves, "spx": spx}


def us_before(spx: dict, d: dt.date) -> float | None:
    """The S&P return of the latest US session dated before NSE day d (the
    one that closed, in Indian time, before d's open)."""
    prior = [u for u in spx if u < d]
    return spx[max(prior)] if prior else None


def compute_stats(history: dict, spx: dict) -> dict:
    """{symbol: {gap, oc, excess, n, price, value}} from whole-market history
    ({symbol: [[date, o, h, l, c, v], ...]}) and the S&P daily returns.
    gap/oc/excess are fractions; stocks with fewer than MIN_EVENTS big-drop
    mornings are left out."""
    stats = {}
    for symbol, candles in history.items():
        if len(candles) < 120:
            continue
        event_gap, event_oc, calm_gap = [], [], []
        for i in range(1, len(candles)):
            prev_close, day_open, day_close = candles[i - 1][4], candles[i][1], candles[i][4]
            if prev_close <= 0 or day_open <= 0:
                continue
            us = us_before(spx, dt.date.fromisoformat(candles[i][0]))
            gap = day_open / prev_close - 1
            if us is None or abs(gap) > 0.25:  # a split/bonus/bad print, not a real gap
                continue
            if us <= US_DROP:
                event_gap.append(gap)
                event_oc.append(day_close / day_open - 1)
            elif us > CALM:
                calm_gap.append(gap)
        if len(event_gap) < MIN_EVENTS or not calm_gap:
            continue
        avg = sum(event_gap) / len(event_gap)
        stats[symbol] = {
            "gap": avg, "oc": sum(event_oc) / len(event_oc), "excess": avg - sum(calm_gap) / len(calm_gap),
            "n": len(event_gap), "price": candles[-1][4],
            "value": sum(c[4] * c[5] for c in candles[-20:]) / 20,
        }
    return stats


def is_commodity_etf(symbol: str) -> bool:
    return any(w in symbol for w in COMMODITY_WORDS)


def rank(stats: dict, k_liquid: int = 8, k_penny: int = 6, k_commodity: int = 5) -> dict:
    """Most-affected stocks, by excess gap (most negative first), in the
    groups the user trades: liquid names, penny-range stocks, silver/gold ETFs."""
    groups = {"liquid": [], "penny": [], "commodity": []}
    for symbol, s in stats.items():
        if symbol == penny.BENCHMARK or s["gap"] >= 0 or s["excess"] >= 0:
            continue
        row = {"symbol": symbol, **s}
        if is_commodity_etf(symbol):
            if s["value"] >= config.PENNY_MIN_DAILY_VALUE:
                groups["commodity"].append(row)
        elif config.PENNY_PRICE_MIN <= s["price"] <= config.PENNY_PRICE_MAX and s["value"] >= config.PENNY_MIN_DAILY_VALUE:
            groups["penny"].append(row)
        elif s["price"] >= LIQUID_MIN_PRICE and s["value"] >= LIQUID_VALUE:
            groups["liquid"].append(row)
    limits = {"liquid": k_liquid, "penny": k_penny, "commodity": k_commodity}
    return {g: sorted(rows, key=lambda r: r["excess"])[: limits[g]] for g, rows in groups.items()}


def is_shock(moves: dict | None, threshold: float | None = None) -> bool:
    threshold = config.OVERNIGHT_SHOCK_SPX if threshold is None else threshold
    return bool(moves) and moves["moves"].get("S&P 500", 0) <= -abs(threshold)


def stats_file():
    return config.PERSIST_DIR / "overnight_stats.json"


def save_stats(stats: dict, now: dt.datetime) -> None:
    stats_file().parent.mkdir(parents=True, exist_ok=True)
    keep = {s: v for s, v in stats.items() if v["value"] >= config.PENNY_MIN_DAILY_VALUE and v["price"] >= config.PENNY_PRICE_MIN}
    stats_file().write_text(json.dumps({"built": now.isoformat(), "stats": keep}))


def load_stats(now: dt.datetime, max_age_days: int = STATS_MAX_AGE_DAYS) -> dict | None:
    """The cached stats if they are fresh enough, else None."""
    try:
        data = json.loads(stats_file().read_text())
        built = dt.datetime.fromisoformat(data["built"])
    except (OSError, ValueError, KeyError):
        return None
    return data["stats"] if (now - built).days <= max_age_days else None


def build_and_save(spx: dict, now: dt.datetime) -> dict:
    """Read a year of whole-market NSE files (minutes), compute and cache the
    stats. Raises nse_data.NSEUnavailable if NSE can't be read."""
    stats = compute_stats(nse_data.fetch_daily_history(None, days=265), spx)
    save_stats(stats, now)
    return stats
