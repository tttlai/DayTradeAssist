"""
Entry point.

Runs as ONE Railway service on a single cron schedule that fires every few
minutes across the market-open window (see README for the exact cron
expression). Each invocation looks at the current IST time and today's
saved state to decide whether it's time to send the pre-market watchlist,
the intraday confirmation, or do nothing (already sent / outside window).

This "one service, auto-detect" design avoids relying on two separate
Railway services staying in sync — everything reads/writes one state file
on the service's attached volume (see README: you must attach a Railway
Volume mounted at /app/data, otherwise state won't survive between runs).

For local testing, --mode forces a specific action regardless of time.
"""
import argparse
import dataclasses
import json
from datetime import datetime, timedelta, timezone, time as dtime

from . import config, notify, report, strategy
from .angel_api import AngelAPI
from .screener import Candidate, build_watchlist

STATE_FILE = config.ROOT_DIR / "data" / "today_state.json"

IST_OFFSET = timedelta(hours=5, minutes=30)
WATCHLIST_WINDOW = (dtime(8, 40), dtime(9, 5))
CONFIRM_WINDOW = (dtime(9, 20), dtime(9, 45))


def now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET


def _today_key() -> str:
    return now_ist().strftime("%Y-%m-%d")


def _default_state() -> dict:
    return {"date": _today_key(), "candidates": [], "sent_watchlist": False, "sent_confirm": False}


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
            plans.append(plan)
        notify.send_message(report.format_confirmation(plans))
        state["sent_confirm"] = True
        save_full_state(state)
    finally:
        api.logout()


def run_auto():
    """Decide what to do based on current IST time + today's state."""
    t = now_ist().time()
    state = load_full_state()

    if WATCHLIST_WINDOW[0] <= t <= WATCHLIST_WINDOW[1] and not state.get("sent_watchlist"):
        print(f"[auto] {t} IST in watchlist window, not yet sent -> running watchlist")
        run_watchlist()
    elif CONFIRM_WINDOW[0] <= t <= CONFIRM_WINDOW[1] and not state.get("sent_confirm"):
        print(f"[auto] {t} IST in confirm window, not yet sent -> running confirm")
        run_confirm()
    else:
        print(f"[auto] {t} IST — nothing to do (outside windows or already sent today)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["watchlist", "confirm", "auto"], default="auto")
    args = parser.parse_args()

    if args.mode == "watchlist":
        run_watchlist()
    elif args.mode == "confirm":
        run_confirm()
    else:
        run_auto()


if __name__ == "__main__":
    main()
