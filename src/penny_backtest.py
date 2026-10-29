"""
Backtest of the opening-gap fade (src/penny.py) on whole-market NSE daily
data, using the same universe/gap/ranking functions as the live alerts.

Each day: build the universe from data up to yesterday, find stocks whose
OPEN gapped past the threshold, take the PENNY_MAX_ALERTS best-ranked, enter
at the open, exit at the percentage target/stop or at the close, net of costs.

Daily bars can't say whether the target or the stop came first, so every
result has a PESSIMISTIC bound (stop first when both were touched) and an
OPTIMISTIC bound (target first); real results sit between. The first/second
half split guards against a result that only works in one stretch of time.

Usage:  python -m src.penny_backtest --days 265
"""
import argparse
from collections import defaultdict

import numpy as np

from . import config, nse_data, penny

COST = 0.0020  # round-trip brokerage/charges/slippage assumption


def load_rows(days: int) -> list[dict]:
    """One row per (stock, day) in the cheap-liquid universe, with what is
    known at the open (gap) and what happened after (best/worst move)."""
    history = nse_data.fetch_daily_history(None, days=days)
    rows = []
    for symbol, candles in history.items():
        n = len(candles)
        if n < 30:
            continue
        o = np.array([c[1] for c in candles]); h = np.array([c[2] for c in candles])
        l = np.array([c[3] for c in candles]); c_ = np.array([c[4] for c in candles])
        v = np.array([c[5] for c in candles]); d = [c[0] for c in candles]
        value = c_ * v
        cum = np.concatenate([[0], np.cumsum(value)])
        for i in range(22, n):
            pc = c_[i - 1]
            value20 = (cum[i] - cum[i - 20]) / 20
            ret_prev = pc / c_[i - 2] - 1
            locked = penny.is_locked(ret_prev, pc, h[i - 1], l[i - 1])
            if not penny.in_universe(pc, value20, h[i - 1] - l[i - 1], locked) or o[i] <= 0:
                continue
            rows.append({
                "date": d[i], "symbol": symbol, "value20": value20, "gap": o[i] / pc - 1,
                "up": h[i] / o[i] - 1, "dn": 1 - l[i] / o[i], "cl": c_[i] / o[i] - 1,
            })
    return rows


def trade_return(row: dict, direction: str, mode: str) -> float:
    t, s = config.PENNY_TARGET_PCT, config.PENNY_STOP_PCT
    if direction == "BUY":
        tgt, stp, close = row["up"] >= t, row["dn"] >= s, row["cl"]
    else:
        tgt, stp, close = row["dn"] >= t, row["up"] >= s, -row["cl"]
    if mode == "pess":
        out = -s if stp else (t if tgt else close)
    else:
        out = t if tgt else (-s if stp else close)
    return out - COST


def simulate(rows: list[dict]) -> list[dict]:
    """The live policy: per day, the PENNY_MAX_ALERTS best-ranked qualifying gaps."""
    by_day = defaultdict(list)
    for r in rows:
        direction = penny.gap_direction(r["gap"])
        if direction:
            by_day[r["date"]].append((penny.rank_key(r["value20"], r["gap"]), direction, r))
    trades = []
    for day in sorted(by_day):
        best = sorted(by_day[day], key=lambda x: x[0], reverse=True)[: config.PENNY_MAX_ALERTS]
        for _, direction, r in best:
            trades.append({
                "date": day, "symbol": r["symbol"], "direction": direction, "gap": r["gap"],
                "pess": trade_return(r, direction, "pess"), "opt": trade_return(r, direction, "opt"),
                "hit_target": (r["up"] if direction == "BUY" else r["dn"]) >= config.PENNY_TARGET_PCT,
            })
    return trades


def summarize(label: str, trades: list[dict]):
    if not trades:
        print(f"{label:<44} no trades"); return
    dates = sorted({t["date"] for t in trades}); mid = dates[len(dates) // 2]
    f = lambda sel, key: np.mean([t[key] for t in sel]) * 100 if sel else float("nan")
    h1 = [t for t in trades if t["date"] < mid]; h2 = [t for t in trades if t["date"] >= mid]
    pess = np.array([t["pess"] for t in trades])
    # equity curve on pessimistic returns with a flat Rs 1 unit per trade, to show drawdown shape
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pess"]
    eq = peak = dd = 0.0
    for day in sorted(daily):
        eq += daily[day]; peak = max(peak, eq); dd = min(dd, eq - peak)
    print(f"{label:<44} n={len(trades):>5} ({len(trades)/len(dates):.1f}/day) win={100*np.mean(pess>0):>4.0f}% "
          f"pess={f(trades,'pess'):+.2f}% opt={f(trades,'opt'):+.2f}% | h1 pess={f(h1,'pess'):+.2f}% h2 pess={f(h2,'pess'):+.2f}% "
          f"| worst drawdown {dd*100:+.0f}% of one-position")


def main():
    global COST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=265)
    args = parser.parse_args()
    print(f"Loading ~{args.days} days of whole-market history (a few minutes)...")
    rows = load_rows(args.days)
    print(f"{len(rows)} stock-days in the universe\n")
    print(f"Net of {COST*100:.1f}% round-trip cost. Results are average return per trade; pess/opt = stop-first/target-first.\n")
    base = dict(g=config.PENNY_GAP_MIN, t=config.PENNY_TARGET_PCT, s=config.PENNY_STOP_PCT, m=config.PENNY_MAX_ALERTS)
    for gap_min in (0.015, 0.02, 0.03):
        for t, s in ((0.03, 0.02), (0.02, 0.015), (0.04, 0.02), (0.03, 0.03)):
            config.PENNY_GAP_MIN, config.PENNY_TARGET_PCT, config.PENNY_STOP_PCT = gap_min, t, s
            summarize(f"gap>={gap_min*100:g}% target {t*100:g}% stop {s*100:g}%", simulate(rows))
    config.PENNY_GAP_MIN, config.PENNY_TARGET_PCT, config.PENNY_STOP_PCT = base["g"], base["t"], base["s"]
    print()
    for m in (1, 2, 3, 5, 99):
        config.PENNY_MAX_ALERTS = m
        summarize(f"defaults, max {m if m < 99 else 'all'} alerts/day", simulate(rows))
    config.PENNY_MAX_ALERTS = base["m"]
    print()
    trades = simulate(rows)
    for side in ("SHORT", "BUY"):
        summarize(f"defaults, {side} only", [t for t in trades if t["direction"] == side])
    config.PENNY_ALLOW_SHORTS = False
    summarize("defaults, shorts disabled (buy-only)", simulate(rows))
    config.PENNY_ALLOW_SHORTS = True
    print()
    for cost in (0.0020, 0.0030, 0.0040, 0.0060):
        COST = cost
        summarize(f"defaults at {cost*100:.1f}% round-trip cost", simulate(rows))
    COST = 0.0020
    big = [t for t in trades if abs(t["gap"]) > penny.LARGE_GAP]
    summarize("defaults, only large gaps (>5%)", big)
    summarize("defaults, only moderate gaps (2-5%)", [t for t in trades if abs(t["gap"]) <= penny.LARGE_GAP])
    print(f"\nTarget hit rate for the default policy: {100*np.mean([t['hit_target'] for t in trades]):.0f}%")


if __name__ == "__main__":
    main()
