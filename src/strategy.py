"""
Turns a screened Candidate into concrete, actionable levels.

Two stages, matching the two scheduled runs described in the README:

  WATCHLIST (run pre-market, ~08:45 IST)
    Publishes a trigger price to watch for, plus the stop-loss/target/
    quantity you'd use *if* it triggers. Nothing has happened yet — this
    is "here's what to watch for at the open."

  CONFIRMATION (run intraday, ~09:35 IST, after the opening range forms)
    Re-checks each watchlist name against live price + volume. Only
    names that actually broke their trigger with above-average volume
    are promoted to "enter now"; the rest are marked "no trigger — skip."
"""
from dataclasses import dataclass

from . import config, indicators, risk
from .screener import Candidate

TARGET_R_MULTIPLE = 2.0  # target = 2x the risk (stop distance)
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


def build_plan(cand: Candidate, num_picks: int, status="WATCH") -> TradePlan:
    if cand.direction == "LONG":
        entry = round(cand.trigger_level * 1.001, 2)
        stop_loss = round(entry - cand.atr, 2)
        target = round(entry + TARGET_R_MULTIPLE * cand.atr, 2)
    else:
        entry = round(cand.trigger_level * 0.999, 2)
        stop_loss = round(entry + cand.atr, 2)
        target = round(entry - TARGET_R_MULTIPLE * cand.atr, 2)

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
    )


VOLUME_CONFIRM_MULTIPLE = 1.2  # today's pace must beat this x the 20-day average pace


def check_confirmation(api, cand: Candidate, plan: TradePlan) -> TradePlan:
    """Intraday re-check: did price actually cross the trigger with volume?"""
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

    if price_broke_trigger and volume_confirms:
        plan.status = "ENTER NOW"
        plan.entry_trigger = round(ltp, 2)
    else:
        plan.status = "NO TRIGGER - SKIP"

    return plan
