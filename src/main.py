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
  2. CONFIRM    (~hourly, 09:20 through 14:00 IST, see CONFIRM_WINDOWS) —
     intraday trigger/volume re-check -> Telegram. A stock that hasn't
     triggered yet gets re-checked at each window rather than only once,
     so a breakout that happens at, say, 11:20 instead of 9:20 isn't
     missed. Once a stock triggers "ENTER NOW" it's not re-checked again
     that day. The FIRST check always sends a full status message (same
     as before); LATER checks stay silent unless something NEW triggered,
     to avoid repeating "still nothing" every hour. The last window
     (14:00) leaves over an hour before square-off so a fresh entry still
     has room to reach target -- checks don't run any later than that.
     Needs live prices, so this uses Angel One (login required each time).
  3. SUMMARY    (~15:40 IST) — replays real intraday candles for whatever
     was confirmed across all of today's confirmation checks to report
     the hypothetical day's P&L. Also uses Angel One, for the same
     live-data reason as step 2.

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

from . import config, eval_agent, github_pr, health, notify, nse_data, report, screener, strategy, telegram_commands, timeutil, tradesim
from .angel_api import AngelAPI
from .screener import Candidate, build_watchlist

STATE_FILE = config.ROOT_DIR / "data" / "today_state.json"
TELEGRAM_STATE_FILE = config.ROOT_DIR / "data" / "telegram_state.json"
# Persists across days (not wiped by the daily reset) -- a suggestion from
# the eval agent waits here until you reply YES/NO, however long that takes.
PENDING_SUGGESTION_FILE = config.ROOT_DIR / "data" / "pending_suggestion.json"

WATCHLIST_WINDOW = (dtime(8, 40), dtime(9, 5))
SUMMARY_WINDOW = (dtime(15, 35), dtime(16, 0))
# Runs after the summary, can take its time, but must finish by 17:00 IST
# (well clear of 5pm). See src/eval_agent.py -- the one phase in this
# project with a real recurring cost (an Anthropic API call/day).
EVAL_WINDOW = (dtime(15, 45), dtime(17, 0))

# Roughly hourly re-checks. Stops at 14:00-14:15 rather than continuing to
# market close -- a trade confirmed any later has too little runway before
# the mandatory 15:15 square-off to realistically reach a 2R target, and
# each check costs an Angel One login, so more/later checks isn't free.
CONFIRM_WINDOWS = [
    ("0920", dtime(9, 20), dtime(9, 35)),
    ("1020", dtime(10, 20), dtime(10, 35)),
    ("1120", dtime(11, 20), dtime(11, 35)),
    ("1220", dtime(12, 20), dtime(12, 35)),
    ("1320", dtime(13, 20), dtime(13, 35)),
    ("1400", dtime(14, 0), dtime(14, 15)),
]

LOOP_INTERVAL_SECONDS = 30


def _today_key() -> str:
    return timeutil.now_ist().strftime("%Y-%m-%d")


def _default_state() -> dict:
    return {
        "date": _today_key(),
        "candidates": [],
        "confirmed_plans": [],
        "sent_watchlist": False,
        "watchlist_failure_notified": False,
        "confirm_checks_done": [],  # window keys from CONFIRM_WINDOWS already run today
        "sent_summary": False,
        "summary_results": [],  # confirmed trades' computed outcomes, reused by run_eval()
        "sent_eval": False,
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


def load_pending_suggestion() -> dict | None:
    if not PENDING_SUGGESTION_FILE.exists():
        return None
    try:
        return json.loads(PENDING_SUGGESTION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_pending_suggestion(data: dict):
    PENDING_SUGGESTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_SUGGESTION_FILE.write_text(json.dumps(data))


def clear_pending_suggestion():
    if PENDING_SUGGESTION_FILE.exists():
        PENDING_SUGGESTION_FILE.unlink()


def handle_commands():
    """Checks for anything you've sent the bot since the last run and
    replies. Understands /health (and /status), plus YES/NO -- but only
    the latter when there's an actual pending eval-agent suggestion
    waiting; a bare "yes" typed any other time just falls through to the
    unrecognized-command reply, so this can't be accidentally triggered
    by casual chat with the bot."""
    last_id = _load_last_update_id()
    messages, new_last_id = telegram_commands.get_new_messages(last_id)

    for text in messages:
        cmd = text.split("@")[0].strip().lower()
        pending = load_pending_suggestion()

        if pending and cmd in ("yes", "y", "/approve", "approve"):
            print(f"[eval] approved: {pending['param']} -> {pending['new_value']} (PR #{pending['pr_number']})")
            if github_pr.merge_pr(pending["pr_number"]):
                notify.send_message(
                    f"✅ Merged: *{pending['param']}* -> {pending['new_value']}. "
                    "Railway will redeploy with the new value shortly."
                )
            else:
                notify.send_message(
                    f"⚠️ Couldn't merge the PR -- check it directly: {pending['pr_url']}"
                )
            clear_pending_suggestion()
        elif pending and cmd in ("no", "n", "/reject", "reject"):
            print(f"[eval] rejected: {pending['param']} -> {pending['new_value']} (PR #{pending['pr_number']})")
            github_pr.close_pr(pending["pr_number"])
            notify.send_message(f"Dismissed — *{pending['param']}* suggestion rejected, PR closed.")
            clear_pending_suggestion()
        elif cmd in ("/health", "/status"):
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


def _fetch_daily_history_via_angel(api: AngelAPI, symbols: list[str], days: int) -> dict:
    """Fallback used only when NSE's bhavcopy is unavailable (e.g. IP-
    blocked -- see nse_data.py). Costs one Angel One login and one API
    call per symbol, unlike the normal free NSE path; only invoked when
    NSE fails outright, not on every run."""
    history = {}
    for symbol in symbols:
        token = api.get_token(symbol)
        if not token:
            continue
        candles = api.get_daily_candles(token, days=days)
        if candles:
            history[symbol] = candles
    return history


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
    # Falls back to an Angel One login only if NSE fails outright.
    api = AngelAPI()
    symbols = screener.load_universe()
    try:
        history = nse_data.fetch_daily_history(symbols, days=30)
    except nse_data.NSEUnavailable as e:
        print(f"[watchlist] NSE bhavcopy unavailable ({e}) -- falling back to Angel One for daily candles")
        api.login()
        try:
            history = _fetch_daily_history_via_angel(api, symbols, days=30)
        finally:
            api.logout()

    candidates = build_watchlist(api.get_token, history)

    plans = [strategy.build_plan(c, len(candidates)) for c in candidates]
    notify.send_message(report.format_watchlist(plans))
    state["candidates"] = [dataclasses.asdict(c) for c in candidates]
    state["sent_watchlist"] = True
    save_full_state(state)


def run_confirm(window_key: str):
    is_first_check = window_key == CONFIRM_WINDOWS[0][0]
    state = load_full_state()
    candidates = [Candidate(**c) for c in state.get("candidates", [])]

    if not candidates:
        if is_first_check:
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
        # Later windows stay silent when there was never a watchlist --
        # the first check already said so, no need to repeat it hourly.
        state["confirm_checks_done"] = state.get("confirm_checks_done", []) + [window_key]
        save_full_state(state)
        return

    already_confirmed_symbols = {p["symbol"] for p in state.get("confirmed_plans", [])}
    pending = [c for c in candidates if c.symbol not in already_confirmed_symbols]

    if not pending:
        print(f"[confirm:{window_key}] everything already confirmed earlier today, nothing to re-check")
        state["confirm_checks_done"] = state.get("confirm_checks_done", []) + [window_key]
        save_full_state(state)
        return

    api = AngelAPI()
    api.login()
    try:
        plans = []
        new_triggers = []
        for c in pending:
            plan = strategy.build_plan(c, len(candidates))
            try:
                plan = strategy.check_confirmation(api, c, plan, len(candidates))
            except Exception as e:
                # One stock's API hiccup shouldn't abort the whole window --
                # if it did, confirm_checks_done below would never get
                # updated, which would permanently block every later window
                # (and the summary) for the rest of the day. Treat it like
                # any other live-data failure instead.
                print(f"[confirm:{window_key}] {c.symbol}: check_confirmation raised {e.__class__.__name__}: {e}")
                plan.status = "NO DATA"
            if plan.status == "ENTER NOW":
                plan.entered_at = timeutil.now_ist().strftime("%H:%M")
                new_triggers.append(plan)
            plans.append(plan)

        if is_first_check:
            notify.send_message(report.format_confirmation(plans))
        elif new_triggers:
            notify.send_message(
                report.format_new_triggers(new_triggers, timeutil.now_ist().strftime("%H:%M"))
            )
        else:
            print(f"[confirm:{window_key}] nothing new triggered, staying quiet")

        state["confirmed_plans"] = state.get("confirmed_plans", []) + [
            dataclasses.asdict(p) for p in new_triggers
        ]
        state["confirm_checks_done"] = state.get("confirm_checks_done", []) + [window_key]
        save_full_state(state)
    finally:
        api.logout()


def run_summary():
    state = load_full_state()
    plans = [strategy.TradePlan(**p) for p in state.get("confirmed_plans", [])]

    if not plans:
        notify.send_message(report.format_daily_summary([]))
        state["sent_summary"] = True
        state["summary_results"] = []
        save_full_state(state)
        return

    api = AngelAPI()
    api.login()
    try:
        results = []
        failed_symbols = []
        for plan in plans:
            try:
                intraday = api.get_intraday_candles(plan.token, interval="FIFTEEN_MINUTE")
                relevant = [
                    c
                    for c in intraday
                    if plan.entered_at <= timeutil.candle_time_str(c[0]) <= config.SQUARE_OFF_TIME
                ]
                sim = tradesim.walk_candles(relevant, plan.direction, plan.stop_loss, plan.target)
                trade_pnl = tradesim.pnl(plan.direction, plan.entry_trigger, sim.exit_price, plan.quantity)
                results.append((plan, sim, trade_pnl))
            except Exception as e:
                # One plan's API hiccup shouldn't cost the whole summary --
                # if it did, sent_summary below would never get set, same
                # class of bug as the confirm-window issue this mirrors.
                print(f"[summary] {plan.symbol}: failed to compute outcome: {e.__class__.__name__}: {e}")
                failed_symbols.append(plan.symbol)

        notify.send_message(report.format_daily_summary(results, failed_symbols))
        state["sent_summary"] = True
        # Persisted (not just the Telegram text) so run_eval() can reuse these
        # computed outcomes without re-fetching intraday data / re-running
        # tradesim for the same confirmed trades.
        state["summary_results"] = [
            {
                "symbol": plan.symbol,
                "direction": plan.direction,
                "entry": plan.entry_trigger,
                "exit": sim.exit_price,
                "outcome": sim.outcome,
                "pnl": trade_pnl,
            }
            for plan, sim, trade_pnl in results
        ]
        save_full_state(state)
    finally:
        api.logout()


def run_eval():
    """Daily self-eval (src/eval_agent.py): fetches what actually happened
    to every watchlist stock today (not just confirmed ones), combines it
    with the day's confirmed-trade outcomes and recent history, and asks
    an LLM for a short, data-grounded critique. Silently no-ops if
    ANTHROPIC_API_KEY isn't set -- this is opt-in, the one phase in this
    project with a real recurring cost."""
    state = load_full_state()
    candidates = [Candidate(**c) for c in state.get("candidates", [])]
    confirmed_results = state.get("summary_results", [])

    if not candidates:
        notify.send_message("*Daily Eval*\n\nNo watchlist today, nothing to evaluate.")
        state["sent_eval"] = True
        save_full_state(state)
        return

    api = AngelAPI()
    api.login()
    try:
        outcomes = eval_agent.fetch_watchlist_outcomes(api, candidates)
    finally:
        api.logout()

    history = eval_agent.load_eval_history()
    prompt = eval_agent.build_prompt(candidates, outcomes, confirmed_results, history)
    raw = eval_agent.call_eval(prompt)

    today = timeutil.now_ist().strftime("%d %b %Y")
    lines = [f"*Daily Eval — {today}*", ""]

    if raw is None:
        reason = (eval_agent.last_error or "unknown").replace("_", " ").replace("*", "").replace("`", "")
        lines.append(f"⚠️ Couldn't generate today's eval -- reason: {reason}")
    else:
        prose, suggestion = eval_agent.split_eval_response(raw)
        lines.append(prose)

        already_pending = load_pending_suggestion()
        if already_pending:
            lines.append(
                f"\n_A suggestion is still awaiting your reply: {already_pending['param']} -> "
                f"{already_pending['new_value']} — {already_pending['reason']}. "
                f"Reply YES or NO. PR: {already_pending['pr_url']}_"
            )
        elif suggestion:
            pr = github_pr.open_tuning_pr(suggestion["param"], suggestion["new_value"], suggestion["reason"])
            if pr:
                save_pending_suggestion(
                    {
                        "param": suggestion["param"],
                        "new_value": suggestion["new_value"],
                        "reason": suggestion["reason"],
                        "pr_number": pr["number"],
                        "pr_url": pr["url"],
                    }
                )
                lines.append(
                    f"\n*Suggested change:* `{suggestion['param']}` -> {suggestion['new_value']}\n"
                    f"{suggestion['reason']}\nPR: {pr['url']}\n\n"
                    "Reply YES to merge, or NO to dismiss."
                )
            else:
                lines.append(
                    f"\n_Suggested `{suggestion['param']}` -> {suggestion['new_value']}, but couldn't "
                    "open a PR (GITHUB_TOKEN not configured, or the request failed) -- check Railway logs._"
                )

    notify.send_message("\n".join(lines))

    outcome_counts = {}
    for r in confirmed_results:
        outcome_counts[r["outcome"]] = outcome_counts.get(r["outcome"], 0) + 1
    eval_agent.append_eval_history(
        {
            "date": today,
            "watchlist_count": len(candidates),
            "confirmed_count": len(confirmed_results),
            "outcomes": outcome_counts,
            "total_pnl": sum(r["pnl"] for r in confirmed_results),
            "watchlist": [
                {
                    "symbol": o["symbol"],
                    "direction": o["direction"],
                    "approach_pct": o.get("approach_pct"),
                    "crossed_trigger": o.get("crossed_trigger"),
                }
                for o in outcomes
                if "data" not in o
            ],
        }
    )

    state["sent_eval"] = True
    save_full_state(state)


def run_auto():
    """Decide what to do based on current IST time + today's state."""
    handle_commands()

    t = timeutil.now_ist().time()
    state = load_full_state()

    if WATCHLIST_WINDOW[0] <= t <= WATCHLIST_WINDOW[1] and not state.get("sent_watchlist"):
        print(f"[auto] {t} IST in watchlist window, not yet sent -> running watchlist")
        run_watchlist()
        return

    if (
        t > WATCHLIST_WINDOW[1]
        and not state.get("sent_watchlist")
        and not state.get("watchlist_failure_notified")
    ):
        # The watchlist window closed without ever completing (every retry
        # inside it raised). Leaves sent_watchlist False -- run_confirm()'s
        # existing "doesn't look like the pre-market run executed" message
        # already handles that accurately -- this just gets you an earlier
        # heads-up at 09:05 instead of waiting for confirm's first check.
        print(f"[auto] {t} IST - watchlist window expired without completing, sending fallback notice")
        notify.send_message(
            "*Day Trading Watchlist*\n\n⚠️ Couldn't generate today's watchlist due to a "
            "technical issue -- check Railway logs around 08:40-09:05 IST."
        )
        state["watchlist_failure_notified"] = True
        save_full_state(state)

    checks_done = state.get("confirm_checks_done", [])
    for key, start, end in CONFIRM_WINDOWS:
        if key in checks_done:
            continue
        if t < start:
            break  # windows are chronological -- haven't reached this one yet
        if t <= end:
            print(f"[auto] {t} IST in confirm window {key}, not yet run -> running confirm")
            run_confirm(key)
            return
        # t > end: this window's time passed without ever completing (e.g.
        # every attempt inside it raised before reaching the normal
        # confirm_checks_done update -- api.login() itself failing, say).
        # Mark it skipped rather than leaving it missing forever, which
        # would otherwise permanently block every later window and the
        # summary for the rest of the day.
        print(f"[auto] {t} IST - confirm window {key} expired without completing, marking skipped")
        checks_done = checks_done + [key]
        state["confirm_checks_done"] = checks_done
        save_full_state(state)

    all_confirms_done = all(key in checks_done for key, _, _ in CONFIRM_WINDOWS)
    if SUMMARY_WINDOW[0] <= t <= SUMMARY_WINDOW[1] and all_confirms_done and not state.get("sent_summary"):
        print(f"[auto] {t} IST in summary window, not yet sent -> running summary")
        run_summary()
        return

    if t > SUMMARY_WINDOW[1] and all_confirms_done and not state.get("sent_summary"):
        # The summary window closed without ever completing (every retry
        # inside it raised before reaching the normal sent_summary update).
        # Unlike confirm windows there's only one summary window, so there's
        # no later window to fall through to -- send a fallback notice
        # instead of leaving it silently unresolved for the rest of the day.
        print(f"[auto] {t} IST - summary window expired without completing, sending fallback notice")
        notify.send_message(
            "*End of Day Summary*\n\n⚠️ Couldn't generate today's summary due to a "
            "technical issue -- check Railway logs around 15:35-16:00 IST."
        )
        state["sent_summary"] = True
        save_full_state(state)
        return

    if EVAL_WINDOW[0] <= t <= EVAL_WINDOW[1] and state.get("sent_summary") and not state.get("sent_eval"):
        print(f"[auto] {t} IST in eval window, not yet sent -> running eval")
        run_eval()
        return

    if t > EVAL_WINDOW[1] and state.get("sent_summary") and not state.get("sent_eval"):
        # Same fallback pattern as watchlist/confirm/summary -- if the eval
        # window closes without ever completing, say so rather than just
        # never sending anything and leaving sent_eval permanently False.
        print(f"[auto] {t} IST - eval window expired without completing, sending fallback notice")
        notify.send_message(
            "*Daily Eval*\n\n⚠️ Couldn't generate today's eval due to a technical "
            "issue -- check Railway logs around 15:45-17:00 IST."
        )
        state["sent_eval"] = True
        save_full_state(state)
        return

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
        choices=["watchlist", "confirm", "summary", "eval", "health", "auto", "serve"],
        default="auto",
    )
    parser.add_argument(
        "--window",
        choices=[key for key, _, _ in CONFIRM_WINDOWS],
        default=CONFIRM_WINDOWS[0][0],
        help="Which confirm window to force with --mode confirm (default: the first/09:20 one)",
    )
    args = parser.parse_args()

    if args.mode == "watchlist":
        run_watchlist()
    elif args.mode == "confirm":
        run_confirm(args.window)
    elif args.mode == "summary":
        run_summary()
    elif args.mode == "eval":
        run_eval()
    elif args.mode == "health":
        notify.send_message(health.run_health_check())
    elif args.mode == "serve":
        run_serve_loop()
    else:
        run_auto()


if __name__ == "__main__":
    main()
