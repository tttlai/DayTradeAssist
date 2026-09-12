"""
Shared trade-outcome simulation: given a chronological sequence of OHLCV
candles and a planned stop-loss/target, work out how the trade would have
played out.

Used by both:
  - src/backtest.py, where each "candle" is a whole historical day's OHLC
    (since only daily history is fetched for the backtest window).
  - main.py's end-of-day summary, where candles are the real intraday
    (15-min) bars for a trade that was actually confirmed today.

When a single candle's range touches both target and stop-loss, OHLC
alone can't tell us which happened first, so we conservatively call it a
stop-out. This biases results slightly pessimistic, never optimistic.
"""
from dataclasses import dataclass


@dataclass
class SimResult:
    exit_price: float
    outcome: str  # "TARGET", "STOPPED", "TIME_EXIT", "NO_TRIGGER"


def walk_candles(candles, direction: str, stop_loss: float, target: float) -> SimResult:
    last_close = None
    for candle in candles:
        _, _o, h, l, c, _v = candle
        last_close = c

        if direction == "LONG":
            hit_target = h >= target
            hit_stop = l <= stop_loss
        else:
            hit_target = l <= target
            hit_stop = h >= stop_loss

        if hit_stop:
            return SimResult(exit_price=stop_loss, outcome="STOPPED")
        if hit_target:
            return SimResult(exit_price=target, outcome="TARGET")

    if last_close is None:
        return SimResult(exit_price=0.0, outcome="NO_TRIGGER")
    return SimResult(exit_price=last_close, outcome="TIME_EXIT")


def pnl(direction: str, entry: float, exit_price: float, quantity: int) -> float:
    if direction == "LONG":
        return (exit_price - entry) * quantity
    return (entry - exit_price) * quantity
