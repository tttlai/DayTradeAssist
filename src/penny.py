"""
Opening-gap fade for cheap, liquid NSE stocks ("penny" stocks).

Backtest finding behind this (265 days, whole-market daily data): cheap
stocks tend to FADE an opening gap -- a stock that opens well above
yesterday's close tends to drop back, one that opens well below tends to
bounce -- while nothing known the evening before predicted direction. So
there is no pre-market pick: each morning the universe is armed, the live
stream reads every stock's opening gap right after 09:15, and the strongest
qualifying gaps become alerts:

    gap UP   >= PENNY_GAP_MIN  -> SHORT   (target below, stop above)
    gap DOWN <= -PENNY_GAP_MIN -> BUY     (target above, stop below)

with a percentage take-profit and stop (not an exit at the close).

This module is the pure logic (no network, no clock) shared by the live
alerts in src/main.py and the backtest in src/penny_backtest.py, so what is
validated is exactly what runs.
"""
import math
from dataclasses import dataclass

from . import config

TICK = 0.05  # NSE tick size for these stocks
LARGE_GAP = 0.05  # beyond this the move is probably news-driven: still allowed, flagged


@dataclass
class PennyStock:
    symbol: str
    token: str
    last_close: float
    avg_value_20: float  # 20-day average traded value, rupees


def in_universe(last_close: float, avg_value_20: float, yesterday_range: float, locked: bool) -> bool:
    """Yesterday-only filter: cheap enough, liquid enough, actually trading
    (not frozen at one price) and not locked at a circuit limit."""
    return (
        config.PENNY_PRICE_MIN <= last_close <= config.PENNY_PRICE_MAX
        and avg_value_20 >= config.PENNY_MIN_DAILY_VALUE
        and yesterday_range > 0
        and not locked
    )


def is_locked(ret_prev: float, close: float, high: float, low: float) -> bool:
    """Closed pinned at an upper/lower circuit yesterday (illiquid next day)."""
    return (ret_prev >= 0.049 and close >= high * 0.999) or (ret_prev <= -0.049 and close <= low * 1.001)


def build_universe(history: dict, token_of) -> list[PennyStock]:
    """history: {symbol: [[date, o, h, l, c, v], ...]} oldest first (the
    whole-market NSE history). token_of: {symbol: Angel token}; stocks Angel
    can't quote are dropped. Uses only data up to yesterday's close."""
    stocks = []
    for symbol, candles in history.items():
        if len(candles) < 22 or symbol not in token_of:
            continue
        closes = [c[4] for c in candles]
        values = [c[4] * c[5] for c in candles[-20:]]
        prev, last = candles[-2], candles[-1]
        ret_prev = last[4] / prev[4] - 1 if prev[4] else 0.0
        if in_universe(last[4], sum(values) / 20, last[2] - last[3], is_locked(ret_prev, last[4], last[2], last[3])):
            stocks.append(PennyStock(symbol, token_of[symbol], last[4], sum(values) / 20))
    return stocks


def gap_direction(gap: float) -> str | None:
    """SHORT after a gap up, BUY after a gap down, None if too small/too big."""
    if abs(gap) > config.PENNY_GAP_MAX or abs(gap) < config.PENNY_GAP_MIN:
        return None
    if gap > 0:
        return "SHORT" if config.PENNY_ALLOW_SHORTS else None
    return "BUY"


def rank_key(avg_value_20: float, gap: float) -> float:
    """Higher is better: prefer the most liquid names (cleaner fills), and
    halve the weight of very large gaps (more likely news-driven)."""
    return avg_value_20 * (0.5 if abs(gap) > LARGE_GAP else 1.0)


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def levels(direction: str, price: float) -> tuple[float, float]:
    """(target, stop) around the entry price, as percentages."""
    t, s = config.PENNY_TARGET_PCT, config.PENNY_STOP_PCT
    if direction == "BUY":
        return round_tick(price * (1 + t)), round_tick(price * (1 - s))
    return round_tick(price * (1 - t)), round_tick(price * (1 + s))


def quantity(price: float) -> int:
    """Shares for one alert: the penny budget split across the max alerts."""
    if price <= 0:
        return 0
    return max(0, math.floor((config.PENNY_BUDGET / max(1, config.PENNY_MAX_ALERTS)) / price))


def evaluate_open(stock: PennyStock, day_open: float, ltp: float, prev_close: float | None = None) -> dict | None:
    """Decide whether this stock's opening gap is an alert. prev_close is the
    exchange's own base price when known (it is adjusted for splits/bonuses,
    which our unadjusted history is not); falls back to yesterday's close.
    Returns the alert dict or None."""
    base = prev_close if prev_close and prev_close > 0 else stock.last_close
    if base <= 0 or day_open <= 0 or ltp <= 0:
        return None
    # A streamed price far from our own record of yesterday's close means a
    # unit/token mistake or a corporate action: don't trade on it.
    if abs(ltp / stock.last_close - 1) > 0.25:
        return None
    gap = day_open / base - 1
    direction = gap_direction(gap)
    if direction is None:
        return None
    # Already running away from the open by the time we look: the entry the
    # backtest assumed (the open) is gone.
    if abs(ltp / day_open - 1) > config.PENNY_MAX_DRIFT:
        return None
    qty = quantity(ltp)
    if qty <= 0:
        return None
    target, stop = levels(direction, ltp)
    return {
        "symbol": stock.symbol, "token": stock.token, "direction": direction,
        "prev_close": round(base, 2), "open": round(day_open, 2), "ltp": round(ltp, 2),
        "gap_pct": round(gap * 100, 2), "target": target, "stop": stop, "quantity": qty,
        "large_gap": abs(gap) > LARGE_GAP, "avg_value_20": stock.avg_value_20,
        "rank": rank_key(stock.avg_value_20, gap),
    }


def pick_alerts(candidates: list[dict], already: set[str], limit: int | None = None) -> list[dict]:
    """Best-ranked qualifying alerts not already sent, capped so a day never
    exceeds PENNY_MAX_ALERTS in total (already-sent ones count)."""
    limit = config.PENNY_MAX_ALERTS if limit is None else limit
    room = max(0, limit - len(already))
    fresh = [c for c in candidates if c["symbol"] not in already]
    return sorted(fresh, key=lambda c: c["rank"], reverse=True)[:room]


def stats_line(results: list[dict]) -> str:
    """One line on the live track record, or "" if there is none yet."""
    done = [r for r in results if r.get("pct") is not None]
    if not done:
        return ""
    wins = sum(1 for r in done if r["pct"] > 0)
    avg = sum(r["pct"] for r in done) / len(done)
    total = sum(r.get("pnl") or 0 for r in done)
    return (
        f"Live record so far: {len(done)} alerts, {100 * wins / len(done):.0f}% winners, "
        f"average {avg:+.2f}% per trade (Rs {total:+,.0f} in total, before costs)."
    )


def stats_text(results: list[dict]) -> str:
    """Plain-text summary of penny_results.jsonl for /penny."""
    if not results:
        return "No gap alerts have been reviewed yet."
    lines = [stats_line(results) or "No alert has a computed result yet.", "", "Last 8:"]
    for r in results[-8:]:
        pct = "n/a" if r.get("pct") is None else f"{r['pct']:+.2f}%"
        lines.append(
            f"{r.get('date')} {r.get('symbol')} {r.get('direction')} gap {r.get('gap_pct'):+.1f}% -> "
            f"{str(r.get('outcome')).replace('_', ' ')} {pct}"
        )
    return "\n".join(lines)


def order_rows(universe: list[PennyStock]) -> list[dict]:
    """The evening order sheet: for each stock, the opening-price levels that
    would make it a SELL (short) or BUY, most liquid first. Direction is
    unknowable the evening before, so each stock carries BOTH conditions."""
    rows = []
    for s in sorted(universe, key=lambda x: x.avg_value_20, reverse=True):
        rows.append({
            "symbol": s.symbol,
            "prev_close": round(s.last_close, 2),
            "value_cr": round(s.avg_value_20 / 1e7, 1),
            "sell_if_open_at_or_above": round_tick(s.last_close * (1 + config.PENNY_GAP_MIN)) if config.PENNY_ALLOW_SHORTS else None,
            "buy_if_open_at_or_below": round_tick(s.last_close * (1 - config.PENNY_GAP_MIN)),
            "skip_if_open_above": round_tick(s.last_close * (1 + config.PENNY_GAP_MAX)),
            "skip_if_open_below": round_tick(s.last_close * (1 - config.PENNY_GAP_MAX)),
        })
    return rows


def sheet_csv(universe: list[PennyStock]) -> str:
    """The full order sheet as CSV text (for the Telegram attachment)."""
    rows = order_rows(universe)
    cols = ["symbol", "prev_close", "value_cr", "sell_if_open_at_or_above", "buy_if_open_at_or_below",
            "skip_if_open_above", "skip_if_open_below"]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join("" if r[c] is None else str(r[c]) for c in cols))
    return "\n".join(lines) + "\n"
