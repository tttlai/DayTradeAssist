"""
Historical sanity-check backtest of the screener/strategy rules.

Approximation, read before trusting the numbers: this replays the
screener's decision (built from daily candles through the prior close)
against each day's own daily OHLC to decide the outcome. It does NOT use
real intraday candle sequencing, so when a day's range touches both
stop-loss and target we can't tell which happened first from daily bars
alone and conservatively count it as a stop-out (see src/tradesim.py).
That biases results pessimistic on that point -- but the headline P&L
is OPTIMISTIC in another: it fills every trade at the trigger price, even
when the prior close was already past it or the day opened past it (see
the "How the trades actually 'triggered'" section of the report, which
re-simulates those at the open). Trust that section over the headline. The live end-of-day
summary (`python -m src.main --mode summary`, or the automatic run each
afternoon) is far more accurate since it replays real 15-minute intraday
candles for trades you actually got alerted on.

Data comes from NSE's free bhavcopy (src/nse_data.py) -- same source as
the live pre-market watchlist -- so backtesting needs no Angel One
credentials at all and can't hit its rate limits.

Usage:
    python -m src.backtest --days 60
"""
import argparse
from collections import defaultdict

from . import config, eval_agent, nse_data, risk, screener, strategy, tradesim

LOOKBACK = 25  # matches indicators.* minimum candle requirements


def common_dates(history, threshold=0.9):
    """Dates present for at least `threshold` fraction of symbols, so one
    short-history stock doesn't shrink the whole backtest window."""
    counts = defaultdict(int)
    for candles in history.values():
        for c in candles:
            counts[c[0]] += 1
    n = len(history)
    if n == 0:
        return []
    return sorted(d for d, cnt in counts.items() if cnt >= n * threshold)


def run_backtest(days=60, max_picks=None, stop_multiple=None, target_multiple=None, universe=None):
    max_picks = max_picks or config.MAX_PICKS
    stop_multiple = stop_multiple if stop_multiple is not None else strategy.STOP_R_MULTIPLE
    target_multiple = target_multiple if target_multiple is not None else strategy.TARGET_R_MULTIPLE
    if universe:
        config.UNIVERSE_MODE = universe
    symbols = screener.universe_symbols()

    print(f"Fetching {days + LOOKBACK + 10} days of NSE bhavcopy history "
          f"(universe={config.UNIVERSE_MODE}, "
          f"{len(symbols) if symbols is not None else 'all EQ'} symbols)...")
    history = nse_data.fetch_daily_history(symbols, days=days + LOOKBACK + 10)
    history = {s: c for s, c in history.items() if c}  # drop symbols with no data at all

    # Bhavcopy dates are market-wide, but a whole-market universe includes
    # recent listings that lack older days, so don't demand 90% coverage.
    dates = common_dates(history, threshold=0.5 if config.UNIVERSE_MODE == "liquid" else 0.9)
    if len(dates) < LOOKBACK + 2:
        print("Not enough overlapping trading history to backtest. Try a smaller --days.")
        return

    eval_dates = dates[LOOKBACK:][-days:]
    by_date_candles = {
        symbol: {c[0]: c for c in candles} for symbol, candles in history.items()
    }

    print(f"Replaying {len(eval_dates)} trading days "
          f"(stop={stop_multiple}x ATR, target={target_multiple}x ATR)...")
    trades = []
    for date in eval_dates:
        day_candidates = []
        for symbol, candles in history.items():
            prior = [c for c in candles if c[0] < date]
            if len(prior) < LOOKBACK:
                continue
            cand = screener.evaluate(symbol, prior)
            if cand:
                day_candidates.append(cand)

        day_candidates.sort(key=lambda c: c.score, reverse=True)
        price_ceiling = risk.min_affordable_price(max_picks)
        affordable = [c for c in day_candidates if c.last_close <= price_ceiling]
        picks = affordable[:max_picks]

        for cand in picks:
            plan = strategy.build_plan(
                cand, len(picks), stop_multiple=stop_multiple, target_multiple=target_multiple
            )
            if plan.quantity <= 0:
                continue

            today_candle = by_date_candles[cand.symbol].get(date)
            if not today_candle:
                continue

            _, o, h, l, _c, _v = today_candle
            triggered = (
                h >= plan.entry_trigger if cand.direction == "LONG" else l <= plan.entry_trigger
            )
            if not triggered:
                continue

            sim = tradesim.walk_candles([today_candle], cand.direction, plan.stop_loss, plan.target)
            trade_pnl = tradesim.pnl(cand.direction, plan.entry_trigger, sim.exit_price, plan.quantity)

            # The simulation above fills at the trigger price. That is only
            # achievable when price really travels up to the trigger during
            # the day ("normal"). Two other cases can't fill there:
            #   already -- the prior close was ALREADY beyond the trigger
            #              (the 20-day level excludes the latest bar), so
            #              "triggered" from the first minute;
            #   gap     -- prior close short of the trigger, but the day
            #              OPENED beyond it.
            # For both, the earliest real entry is the open. Re-simulate them
            # the way the live tool would: entry at the open, stop/target/qty
            # recomputed around it (see strategy.check_confirmation).
            already = (
                cand.last_close >= cand.trigger_level
                if cand.direction == "LONG"
                else cand.last_close <= cand.trigger_level
            )
            opened_through = o >= plan.entry_trigger if cand.direction == "LONG" else o <= plan.entry_trigger
            kind = "already" if already else ("gap" if opened_through else "normal")
            realistic_pnl = trade_pnl
            # How far past the trigger the earliest real entry (the open) is,
            # in ATRs -- what the live "too late" guard measures.
            chase_atr = 0.0
            if kind != "normal" and cand.atr:
                beyond = o - cand.trigger_level if cand.direction == "LONG" else cand.trigger_level - o
                chase_atr = max(beyond, 0.0) / cand.atr
            if kind != "normal":
                r_stop, r_target = strategy._compute_stop_target(
                    cand.direction, o, cand.atr, stop_multiple, target_multiple
                )
                r_qty = risk.position_size(o, r_stop, len(picks))
                r_sim = tradesim.walk_candles([today_candle], cand.direction, r_stop, r_target)
                realistic_pnl = tradesim.pnl(cand.direction, o, r_sim.exit_price, r_qty) if r_qty > 0 else 0.0

            trades.append(
                {
                    "date": date,
                    "symbol": cand.symbol,
                    "direction": cand.direction,
                    "outcome": sim.outcome,
                    "pnl": trade_pnl,
                    "kind": kind,
                    "chase_atr": chase_atr,
                    "realistic_pnl": realistic_pnl,
                    "approach_pct": eval_agent.approach_pct(
                        cand.direction, cand.last_close, cand.trigger_level, h, l
                    ),
                }
            )

    print_report(trades, len(eval_dates))


def print_report(trades, num_days):
    print()
    print("=" * 60)
    if not trades:
        print(f"No trades triggered across {num_days} trading days for this universe.")
        print("=" * 60)
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total_pnl = sum(t["pnl"] for t in trades)

    outcome_counts = defaultdict(int)
    for t in trades:
        outcome_counts[t["outcome"]] += 1

    print(f"Backtest window : {num_days} trading days")
    print(f"Trades triggered: {len(trades)}")
    print(f"Win rate        : {len(wins)}/{len(trades)} ({100*len(wins)/len(trades):.1f}%)")
    print(
        "Outcomes        : "
        + ", ".join(
            f"{outcome} {count} ({100*count/len(trades):.0f}%)"
            for outcome, count in sorted(outcome_counts.items())
        )
    )
    print(f"Total P&L       : Rs {total_pnl:,.0f}")
    print(f"Avg P&L / trade : Rs {total_pnl/len(trades):,.0f}")
    if wins:
        print(f"Avg win         : Rs {sum(t['pnl'] for t in wins)/len(wins):,.0f}")
    if losses:
        print(f"Avg loss        : Rs {sum(t['pnl'] for t in losses)/len(losses):,.0f}")

    by_date = defaultdict(float)
    for t in trades:
        by_date[t["date"]] += t["pnl"]
    equity, peak, max_drawdown = 0.0, 0.0, 0.0
    for date in sorted(by_date):
        equity += by_date[date]
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    print(f"Max drawdown    : Rs {max_drawdown:,.0f}")

    trading_days_with_trade = len(by_date)
    print(f"Avg P&L / active day (out of {trading_days_with_trade} days with a trade): "
          f"Rs {total_pnl/trading_days_with_trade:,.0f}")
    print_gap_report(trades)
    print()
    print("NOTE: daily-OHLC approximation, not true intraday sequencing -- see")
    print("module docstring in src/backtest.py before drawing conclusions.")
    print("=" * 60)


def _line(label, group, key):
    if not group:
        return f"  {label:<46}: none"
    total = sum(t[key] for t in group)
    wins = sum(1 for t in group if t[key] > 0)
    return (
        f"  {label:<46}: {len(group):>4} trades, win {100*wins/len(group):>4.1f}%, "
        f"total Rs {total:>8,.0f}, avg Rs {total/len(group):>5,.0f}"
    )


def print_gap_report(trades):
    """Do trades where the day opened already beyond the trigger make money
    when filled where they realistically could have been (the open)?"""
    normal = [t for t in trades if t["kind"] == "normal"]
    already = [t for t in trades if t["kind"] == "already"]
    gap = [t for t in trades if t["kind"] == "gap"]
    far = [t for t in trades if t["approach_pct"] is not None and t["approach_pct"] > 150]
    near = [t for t in trades if t["approach_pct"] is not None and t["approach_pct"] <= 150]
    print()
    print("How the trades actually 'triggered' (headline above fills ALL at trigger):")
    print(_line("normal: price travelled up to trigger intraday", normal, "pnl"))
    print(_line("already: prior close was already past trigger", already, "pnl"))
    print(_line("   ...same trades, realistic fill at open", already, "realistic_pnl"))
    print(_line("gap: prior close short, day opened past trigger", gap, "pnl"))
    print(_line("   ...same trades, realistic fill at open", gap, "realistic_pnl"))
    realistic_total = sum(t["realistic_pnl"] for t in trades)
    print(f"  Whole backtest with realistic fills             : total Rs {realistic_total:,.0f}")
    chased = [t for t in trades if t["kind"] != "normal"]
    print("Entry-distance guard (realistic fills; chase = how many ATRs past the trigger")
    print("the open already was):")
    for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 99)):
        grp = [t for t in chased if lo <= t["chase_atr"] < hi]
        print(_line(f"chase {lo:g}-{hi:g} ATR" if hi < 99 else f"chase >= {lo:g} ATR", grp, "realistic_pnl"))
    kept = [t for t in trades if t["kind"] == "normal" or t["chase_atr"] <= 0.5]
    print(_line("ALL trades, guard 0.5 ATR applied (realistic)", kept, "realistic_pnl"))
    print(_line("ALL trades, no guard (realistic)", trades, "realistic_pnl"))
    print("Split by day-extreme distance past the trigger (hindsight, not knowable at")
    print("check time; excludes 'already' trades where the distance is undefined):")
    print(_line("approach <= 150% of prior-close->trigger distance", near, "pnl"))
    print(_line("approach  > 150%", far, "pnl"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="Number of trading days to backtest")
    parser.add_argument(
        "--stop-multiple", type=float, default=None,
        help=f"Stop-loss distance as a multiple of ATR (default: strategy.STOP_R_MULTIPLE = {strategy.STOP_R_MULTIPLE})",
    )
    parser.add_argument(
        "--target-multiple", type=float, default=None,
        help=f"Target distance as a multiple of ATR (default: strategy.TARGET_R_MULTIPLE = {strategy.TARGET_R_MULTIPLE})",
    )
    parser.add_argument(
        "--universe", choices=["csv", "liquid"], default=None,
        help="Stock universe to screen (default: UNIVERSE_MODE from config/env)",
    )
    args = parser.parse_args()
    run_backtest(
        days=args.days, stop_multiple=args.stop_multiple, target_multiple=args.target_multiple,
        universe=args.universe,
    )


if __name__ == "__main__":
    main()
