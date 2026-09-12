"""
Entry point.

Runs as ONE Railway service on a single cron schedule that fires every few
minutes across the market session (see README for the exact cron
expression). Each invocation looks at the current IST time and today's
saved state to decide which of three things to do:

  1. WATCHLIST  (~08:45 IST) — pre-market technical screen -> Telegram
  2. CONFIRM    (~09:35 IST) — intraday trigger/volume check -> Telegram
  3. SUMMARY    (~15:40 IST) — replays real intraday candles for whatever
     was confirmed in step 2 to report the hypothetical day's P&L

This "one service, auto-detect" design avoids relying on multiple Railway
services staying in sync — everything reads/writes one state file on the
service's attached volume (see README: you must attach a Railway Volume
mounted at /app/data, otherwise state won't survive between runs).

For local testing, --mode forces a specific action regardless of time.
"""
import argparse
import dataclasses
import json
from datetime import time as dtime

from . import config, notify, report, strategy, timeutil, tradesim
from .angel_api import AngelAPI
from .screener import Candidate, build_watchlist

STATE_FILE = config.ROOT_DIR / "data" / "today_state.json"

WATCHLIST_WINDOW = (dtime(8, 40), dtime(9, 5))
CONFIRM_WINDOW = (dtime(9, 20), dtime(9, 45))
SUMMARY_WINDOW = (dtime(15, 35), dtime(16, 0))


def _today_key() -> str:
    return timeutil.now_ist().strftime("%Y-%m-%d")


def _default_state() -> dict:
    return {
        "date": _today_key(),
        "candidates": [],
        "confirmed_plans": [],
        "sent_watchlist": False,
        "sent_confirm": False,
        "sent_summary": False,
    }


def load_full_state() -> dict:
    if not STATE_FILE.exists():
        return _default_state()
    payload = json.loads(STATE_FILE.read_text())
    if payload.get("date") != _today_key():
        return _default_state()
    return payload


def save_full_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def run_watchlist():
    state = load_full_state()
    api = AngelAPI()
    api.login()
    try:
        candidates = build_watchlist(api)
        plans = [strategy.build_plan(c, len(candidates)) for c in candidates]
        notify.send_message(report.format_watchlist(plans))
        state["candidates"] = [dataclasses.asdict(c) for c in candidates]
        state["sent_watchlist"] = True
        save_full_state(state)
    finally:
        api.logout()


def run_confirm():
    state = load_full_state()
    candidates = [Candidate(**c) for c in state.get("candidates", [])]
    if not candidates:
        notify.send_message(
            "*Confirmation Check*\n\nNo watchlist found for today — did the "
            "pre-market run execute? Nothing to confirm."
        )
        state["sent_confirm"] = True
        save_full_state(state)
        return

    api = AngelAPI()
    api.login()
    try:
        plans = []
        for c in candidates:
            plan = strategy.build_plan(c, len(candidates))
            plan = strategy.check_confirmation(api, c, plan)
            if plan.status == "ENTER NOW":
                plan.entered_at = timeutil.now_ist().strftime("%H:%M")
            plans.append(plan)
        notify.send_message(report.format_confirmation(plans))
        state["confirmed_plans"] = [
            dataclasses.asdict(p) for p in plans if p.status == "ENTER NOW"
        ]
        state["sent_confirm"] = True
        save_full_state(state)
    finally:
        api.logout()


def run_summary():
    state = load_full_state()
    plans = [strategy.TradePlan(**p) for p in state.get("confirmed_plans", [])]

    if not plans:
        notify.send_message(report.format_daily_summary([]))
        state["sent_summary"] = True
        save_full_state(state)
        return

    api = AngelAPI()
    api.login()
    try:
        results = []
        for plan in plans:
            intraday = api.get_intraday_candles(plan.token, interval="FIFTEEN_MINUTE")
            relevant = [
                c
                for c in intraday
                if plan.entered_at <= timeutil.candle_time_str(c[0]) <= config.SQUARE_OFF_TIME
            ]
            sim = tradesim.walk_candles(relevant, plan.direction, plan.stop_loss, plan.target)
            trade_pnl = tradesim.pnl(plan.direction, plan.entry_trigger, sim.exit_price, plan.quantity)
            results.append((plan, sim, trade_pnl))
        notify.send_message(report.format_daily_summary(results))
        state["sent_summary"] = True
        save_full_state(state)
    finally:
        api.logout()


def run_auto():
    """Decide what to do based on current IST time + today's state."""
    t = timeutil.now_ist().time()
    state = load_full_state()

    if WATCHLIST_WINDOW[0] <= t <= WATCHLIST_WINDOW[1] and not state.get("sent_watchlist"):
        print(f"[auto] {t} IST in watchlist window, not yet sent -> running watchlist")
        run_watchlist()
    elif CONFIRM_WINDOW[0] <= t <= CONFIRM_WINDOW[1] and not state.get("sent_confirm"):
        print(f"[auto] {t} IST in confirm window, not yet sent -> running confirm")
        run_confirm()
    elif (
        SUMMARY_WINDOW[0] <= t <= SUMMARY_WINDOW[1]
        and state.get("sent_confirm")
        and not state.get("sent_summary")
    ):
        print(f"[auto] {t} IST in summary window, not yet sent -> running summary")
        run_summary()
    else:
        print(f"[auto] {t} IST - nothing to do (outside windows or already sent today)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["watchlist", "confirm", "summary", "auto"], default="auto"
    )
    args = parser.parse_args()

    if args.mode == "watchlist":
        run_watchlist()
    elif args.mode == "confirm":
        run_confirm()
    elif args.mode == "summary":
        run_summary()
    else:
        run_auto()


if __name__ == "__main__":
    main()
