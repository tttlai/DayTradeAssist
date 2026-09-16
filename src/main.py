"""
Entry point.

Runs as ONE persistently-running Railway service (--mode serve), NOT a
Railway Cron Schedule. This matters: Railway's Cron Schedule mode tears
down the container's filesystem between every trigger with no way to
attach persistent storage (Volumes are unavailable in that mode -- along
with replicas, serverless, and a configurable restart policy -- confirmed
empirically, not just undocumented). Since this app needs the 08:45
watchlist's output to still be there when the 09:35 confirmation run
checks for it an hour later, cron-per-invocation doesn't work here.

Instead, --mode serve runs an infinite loop that checks the clock every
LOOP_INTERVAL_SECONDS and decides which of three things to do, all within
the SAME continuously-running process -- so state just lives in a local
JSON file on that process's own disk for as long as it keeps running,
no Volume needed:

  1. WATCHLIST  (~08:45 IST) — pre-market technical screen -> Telegram.
     Uses NSE's free bhavcopy (src/nse_data.py) for daily OHLCV, NOT an
     Angel One login -- this phase doesn't need live data, so there's no
     reason to spend a login against Angel's tighter rate limits on it.
  2. CONFIRM    (~09:35 IST) — intraday trigger/volume check -> Telegram.
     Needs live prices, so this one does use Angel One (login required).
  3. SUMMARY    (~15:40 IST) — replays real intraday candles for whatever
     was confirmed in step 2 to report the hypothetical day's P&L. Also
     uses Angel One, for the same live-data reason as step 2.

Every loop iteration also checks for a pending /health command sent to
the bot on Telegram (see handle_commands()) -- since the loop runs
continuously, a reply arrives within LOOP_INTERVAL_SECONDS, any time of
day, any day of the week, with no cron schedule to widen.

The one residual risk versus a Volume-backed design: if the service gets
redeployed (a git push, a Railway settings change) in the middle of a
trading day, that day's in-progress state is lost when the old container
is torn down, same as today's watchlist->confirmation gap would be if it
happened right then. Avoid redeploying during market hours when possible.

For local testing / one-off runs, --mode forces a specific single action
and exits immediately instead of looping.
"""
import argparse
import dataclasses
import json
import time
from datetime import time as dtime

from . import config, health, notify, nse_data, report, screener, strategy, telegram_commands, timeutil, tradesim
from .angel_api import AngelAPI
from .screener import Candidate, build_watchlist

STATE_FILE = config.ROOT_DIR / "data" / "today_state.json"
TELEGRAM_STATE_FILE = config.ROOT_DIR / "data" / "telegram_state.json"

WATCHLIST_WINDOW = (dtime(8, 40), dtime(9, 5))
CONFIRM_WINDOW = (dtime(9, 20), dtime(9, 45))
SUMMARY_WINDOW = (dtime(15, 35), dtime(16, 0))

LOOP_INTERVAL_SECONDS = 30


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


def _load_last_update_id() -> int:
    """Telegram update IDs are a single ever-increasing sequence for the
    bot, not tied to any trading day -- stored separately from
    today_state.json so it isn't wiped by the daily reset."""
    if not TELEGRAM_STATE_FILE.exists():
        return 0
    return json.loads(TELEGRAM_STATE_FILE.read_text()).get("last_update_id", 0)


def _save_last_update_id(update_id: int):
    TELEGRAM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TELEGRAM_STATE_FILE.write_text(json.dumps({"last_update_id": update_id}))


def handle_commands():
    """Checks for anything you've sent the bot since the last run and
    replies. Currently understands /health (and /status as an alias)."""
    last_id = _load_last_update_id()
    messages, new_last_id = telegram_commands.get_new_messages(last_id)

    for text in messages:
        cmd = text.split("@")[0].strip().lower()
        if cmd in ("/health", "/status"):
            print("[health] /health command received, running check")
            notify.send_message(health.run_health_check())
        elif cmd == "/start":
            notify.send_message(
                "DayTradeAssist is connected. Send /health anytime to check "
                "Angel One + Railway status."
            )
        else:
            notify.send_message(f"Unrecognized command: {text}\n\nAvailable: /health")

    if new_last_id != last_id:
        _save_last_update_id(new_last_id)


def run_watchlist():
    print(
        f"[config] MAX_BUDGET=Rs{config.MAX_BUDGET:,.0f} "
        f"RISK_PER_TRADE_PCT={config.RISK_PER_TRADE_PCT*100:.1f}% "
        f"MAX_PICKS={config.MAX_PICKS}"
    )
    state = load_full_state()

    # No Angel One login for this phase -- daily OHLCV comes from NSE's
    # free bhavcopy (nse_data.py); AngelAPI is only used here for its
    # scrip-master token lookup, which doesn't require a session either.
    api = AngelAPI()
    symbols = screener.load_universe()
    history = nse_data.fetch_daily_history(symbols, days=30)
    candidates = build_watchlist(api.get_token, history)

    plans = [strategy.build_plan(c, len(candidates)) for c in candidates]
    notify.send_message(report.format_watchlist(plans))
    state["candidates"] = [dataclasses.asdict(c) for c in candidates]
    state["sent_watchlist"] = True
    save_full_state(state)


def run_confirm():
    state = load_full_state()
    candidates = [Candidate(**c) for c in state.get("candidates", [])]
    if not candidates:
        if state.get("sent_watchlist"):
            # The watchlist ran fine and genuinely found nothing worth
            # watching today -- a normal outcome, not a problem.
            notify.send_message(
                "*Confirmation Check*\n\nToday's watchlist was empty (no "
                "qualifying setups) -- nothing to confirm."
            )
        else:
            # The watchlist never ran at all -- worth investigating
            # (deploy/state-persistence/scheduling issue).
            notify.send_message(
                "*Confirmation Check*\n\n⚠️ No watchlist found for today, and "
                "it doesn't look like the pre-market run executed -- check "
                "Railway logs/deployment status."
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
    handle_commands()

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


def run_serve_loop():
    """The persistent-service entry point (see module docstring for why
    this replaced Railway's Cron Schedule). Runs forever, checking the
    clock every LOOP_INTERVAL_SECONDS; a crash in one iteration is logged
    and swallowed so the loop itself never dies from it."""
    print(f"[serve] Starting persistent loop, checking every {LOOP_INTERVAL_SECONDS}s")
    while True:
        try:
            run_auto()
        except Exception as e:
            print(f"[serve] run_auto() raised {e.__class__.__name__}: {e}")
        time.sleep(LOOP_INTERVAL_SECONDS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["watchlist", "confirm", "summary", "health", "auto", "serve"],
        default="auto",
    )
    args = parser.parse_args()

    if args.mode == "watchlist":
        run_watchlist()
    elif args.mode == "confirm":
        run_confirm()
    elif args.mode == "summary":
        run_summary()
    elif args.mode == "health":
        notify.send_message(health.run_health_check())
    elif args.mode == "serve":
        run_serve_loop()
    else:
        run_auto()


if __name__ == "__main__":
    main()
