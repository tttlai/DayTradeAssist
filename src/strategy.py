"""
Turns a screened Candidate into concrete, actionable levels.

Two stages, matching the two scheduled runs described in the README:

  WATCHLIST (run pre-market, ~08:45 IST)
    Publishes a trigger price to watch for, plus the stop-loss/target/
    quantity you'd use *if* it triggers. Nothing has happened yet — this
    is "here's what to watch for at the open."

  CONFIRMATION (run intraday, ~09:35 IST, after the opening range forms)
    Re-checks each watchlist name against live price + volume, and (if
    REQUIRE_VWAP_CONFIRMATION) whether price is on the expected side of
    today's VWAP. Only names that pass all three are promoted to "enter
    now"; the rest are marked "no trigger — skip."
"""
from dataclasses import dataclass

from . import config, indicators, risk
from .screener import Candidate

STOP_R_MULTIPLE = 1.0  # stop-loss = 1x ATR from entry
TARGET_R_MULTIPLE = 2.0  # target = 2x ATR from entry (2x the stop distance)
MARKET_MINUTES = 375  # 9:15 to 15:30


@dataclass
class TradePlan:
    symbol: str
    direction: str
    entry_trigger: float
    stop_loss: float
    target: float
    quantity: int
    square_off_time: str
    status: str  # "WATCH" or "ENTER NOW" or "NO TRIGGER"
    token: str = ""
    entered_at: str = ""  # "HH:MM" IST, set once status becomes "ENTER NOW"
    circuit_history: bool = False  # hit a circuit limit in the last 10 days


def _compute_stop_target(
    direction: str, entry: float, atr: float,
    stop_multiple: float = STOP_R_MULTIPLE,
    target_multiple: float = TARGET_R_MULTIPLE,
) -> tuple[float, float]:
    """Shared by build_plan() (around the watchlist-time trigger level)
    and check_confirmation() (around the real confirmed entry price) so
    the two can never drift out of sync with each other."""
    if direction == "LONG":
        stop_loss = round(entry - stop_multiple * atr, 2)
        target = round(entry + target_multiple * atr, 2)
    else:
        stop_loss = round(entry + stop_multiple * atr, 2)
        target = round(entry - target_multiple * atr, 2)
    return stop_loss, target


def build_plan(
    cand: Candidate,
    num_picks: int,
    status="WATCH",
    stop_multiple: float = STOP_R_MULTIPLE,
    target_multiple: float = TARGET_R_MULTIPLE,
) -> TradePlan:
    """stop_multiple/target_multiple default to the live constants above --
    only src/backtest.py overrides them, to compare configurations without
    touching what's actually running."""
    if cand.direction == "LONG":
        entry = round(cand.trigger_level * 1.001, 2)
    else:
        entry = round(cand.trigger_level * 0.999, 2)
    stop_loss, target = _compute_stop_target(
        cand.direction, entry, cand.atr, stop_multiple, target_multiple
    )

    qty = risk.position_size(entry, stop_loss, num_picks)

    return TradePlan(
        symbol=cand.symbol,
        direction=cand.direction,
        entry_trigger=entry,
        stop_loss=stop_loss,
        target=target,
        quantity=qty,
        square_off_time=config.SQUARE_OFF_TIME,
        status=status,
        token=cand.token,
        circuit_history=cand.circuit_history,
    )


VOLUME_CONFIRM_MULTIPLE = 1.2  # today's pace must beat this x the 20-day average pace


def check_confirmation(api, cand: Candidate, plan: TradePlan, num_picks: int) -> TradePlan:
    """Intraday re-check: did price actually cross the trigger with volume
    (and, if enabled, on the right side of VWAP)?

    num_picks is needed here, not just at build_plan() time, because a
    confirmed trade gets its stop-loss/target/quantity fully recomputed
    around the REAL confirmed price below -- not just the entry label
    updated while leaving stale levels computed from the watchlist-time
    trigger. Confirmation can now happen as late as 14:00 (vs. the
    original single 9:20-9:45 window), so price can have drifted far
    enough from this morning's trigger that the old stop/target no
    longer bracket the real entry sensibly -- confirmed in production:
    a SHORT whose price had already fallen past where the target was
    computed, relative to the stale trigger, left a target sitting
    *above* the real entry instead of below it."""
    ltp = api.get_ltp(cand.symbol, cand.token)
    if ltp is None:
        print(f"[confirm] {cand.symbol}: get_ltp returned None -- Angel One API/token issue?")
        plan.status = "NO DATA"
        return plan

    intraday = api.get_intraday_candles(cand.token, interval="FIFTEEN_MINUTE")
    minutes_elapsed = max(1, len(intraday) * 15)
    today_volume_so_far = sum(c[5] for c in intraday) if intraday else 0

    expected_fraction_of_day = min(1.0, minutes_elapsed / MARKET_MINUTES)
    expected_volume_by_now = cand.avg_volume_20 * expected_fraction_of_day
    volume_confirms = (
        expected_volume_by_now > 0
        and today_volume_so_far >= expected_volume_by_now * VOLUME_CONFIRM_MULTIPLE
    )

    price_broke_trigger = (
        ltp >= plan.entry_trigger if cand.direction == "LONG" else ltp <= plan.entry_trigger
    )

    # Order-flow filter: only take a LONG above today's VWAP, a SHORT
    # below it -- can't be backtested against our free NSE data (VWAP
    # needs real intraday candles the daily bhavcopy doesn't have), so
    # this is judged by watching live results, not a historical backtest.
    # REQUIRE_VWAP_CONFIRMATION=false disables it if that doesn't pan out.
    vwap_confirms = True
    if config.REQUIRE_VWAP_CONFIRMATION:
        today_vwap = indicators.vwap(intraday)
        if today_vwap is None:
            vwap_confirms = False
        else:
            vwap_confirms = ltp > today_vwap if cand.direction == "LONG" else ltp < today_vwap

    if price_broke_trigger and volume_confirms and vwap_confirms:
        plan.status = "ENTER NOW"
        plan.entry_trigger = round(ltp, 2)
        plan.stop_loss, plan.target = _compute_stop_target(cand.direction, plan.entry_trigger, cand.atr)
        plan.quantity = risk.position_size(plan.entry_trigger, plan.stop_loss, num_picks)
    else:
        plan.status = "NO TRIGGER - SKIP"

    return plan
