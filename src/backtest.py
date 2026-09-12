"""
Historical sanity-check backtest of the screener/strategy rules.

Approximation, read before trusting the numbers: this replays the
screener's decision (built from daily candles through the prior close)
against each day's own daily OHLC to decide the outcome. It does NOT use
real intraday candle sequencing, so when a day's range touches both
stop-loss and target we can't tell which happened first from daily bars
alone and conservatively count it as a stop-out (see src/tradesim.py).
That biases results pessimistic, never optimistic — treat this as a
rough lower bound, not a faithful P&L replay. The live end-of-day
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

from . import config, nse_data, risk, screener, strategy, tradesim

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


def run_backtest(days=60, max_picks=None):
    max_picks = max_picks or config.MAX_PICKS
    symbols = screener.load_universe()

    print(f"Fetching {days + LOOKBACK + 10} days of NSE bhavcopy history for {len(symbols)} symbols...")
    history = nse_data.fetch_daily_history(symbols, days=days + LOOKBACK + 10)
    history = {s: c for s, c in history.items() if c}  # drop symbols with no data at all

    dates = common_dates(history)
    if len(dates) < LOOKBACK + 2:
        print("Not enough overlapping trading history to backtest. Try a smaller --days.")
        return

    eval_dates = dates[LOOKBACK:][-days:]
    by_date_candles = {
        symbol: {c[0]: c for c in candles} for symbol, candles in history.items()
    }

    print(f"Replaying {len(eval_dates)} trading days...")
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
            plan = strategy.build_plan(cand, len(picks))
            if plan.quantity <= 0:
                continue

            today_candle = by_date_candles[cand.symbol].get(date)
            if not today_candle:
                continue

            _, _o, h, l, _c, _v = today_candle
            triggered = (
                h >= plan.entry_trigger if cand.direction == "LONG" else l <= plan.entry_trigger
            )
            if not triggered:
                continue

            sim = tradesim.walk_candles([today_candle], cand.direction, plan.stop_loss, plan.target)
            trade_pnl = tradesim.pnl(cand.direction, plan.entry_trigger, sim.exit_price, plan.quantity)
            trades.append(
                {
                    "date": date,
                    "symbol": cand.symbol,
                    "direction": cand.direction,
                    "outcome": sim.outcome,
                    "pnl": trade_pnl,
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

    print(f"Backtest window : {num_days} trading days")
    print(f"Trades triggered: {len(trades)}")
    print(f"Win rate        : {len(wins)}/{len(trades)} ({100*len(wins)/len(trades):.1f}%)")
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
    print()
    print("NOTE: daily-OHLC approximation, not true intraday sequencing -- see")
    print("module docstring in src/backtest.py before drawing conclusions.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="Number of trading days to backtest")
    args = parser.parse_args()
    run_backtest(days=args.days)


if __name__ == "__main__":
    main()
