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
from datetime import time as dtime, timedelta

from . import config, eval_agent, github_pr, health, notify, nse_data, report, screener, store, stream, strategy, telegram_commands, timeutil, tradesim
from .angel_api import AngelAPI
from .screener import Candidate, build_watchlist

STATE_FILE = config.PERSIST_DIR / "today_state.json"
TELEGRAM_STATE_FILE = config.PERSIST_DIR / "telegram_state.json"
# Persists across days (not wiped by the daily reset) -- a suggestion from
# the eval agent waits here until you reply YES/NO, however long that takes.
PENDING_SUGGESTION_FILE = config.PERSIST_DIR / "pending_suggestion.json"

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
        "closed_notified": False,  # the one "market closed today" note has been sent
        "confirm_checks_done": [],  # window keys from CONFIRM_WINDOWS already run today
        "gate_log": [],  # per-window, per-stock confirmation gate results, reused by run_eval()
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
        elif cmd == "/stream":
            notify.send_message(stream_status_text())
        elif cmd == "/stats":
            notify.send_message(store.trade_stats(store.read_records("trades.jsonl")))
        elif cmd in ("/health", "/status"):
            print("[health] /health command received, running check")
            notify.send_message(health.run_health_check())
        elif cmd == "/start":
            notify.send_message(
                "DayTradeAssist is connected. Send /health anytime to check "
                "Angel One + Railway status."
            )
        else:
            notify.send_message(f"Unrecognized command: {text}\n\nAvailable: /health, /stats, /stream")

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
    symbols = screener.universe_symbols()
    try:
        history = nse_data.fetch_daily_history(symbols, days=30)
    except nse_data.NSEUnavailable as e:
        print(f"[watchlist] NSE bhavcopy unavailable ({e}) -- falling back to Angel One for daily candles")
        api.login()
        try:
            # One Angel call per symbol -- can't cover a whole-market
            # universe, so the fallback always uses the CSV list.
            history = _fetch_daily_history_via_angel(api, symbols or screener.load_universe(), days=30)
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

    if not is_first_check and _monitor_is_healthy(timeutil.now_ist()):
        print(f"[confirm:{window_key}] continuous monitor is healthy and already covering this, marking done")
        state["confirm_checks_done"] = state.get("confirm_checks_done", []) + [window_key]
        save_full_state(state)
        return

    # A later window only runs its own check when the monitor isn't healthy;
    # reuse the shared login if there is one (logging out would also kill the
    # live stream that depends on it).
    shared = None if is_first_check else _monitor["api"]
    api = shared or AngelAPI()
    if shared is None:
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

        state["gate_log"] = state.get("gate_log", []) + [
            {"window": window_key, "symbol": p.symbol, "status": p.status, **p.gates}
            for p in plans
            if p.gates
        ]
        state["confirmed_plans"] = state.get("confirmed_plans", []) + [
            dataclasses.asdict(p) for p in new_triggers
        ]
        state["confirm_checks_done"] = state.get("confirm_checks_done", []) + [window_key]
        save_full_state(state)
    finally:
        if shared is None:
            api.logout()


# --- Continuous trigger monitor -------------------------------------------
# The six CONFIRM_WINDOWS above are hourly snapshots, so a stock could cross
# its trigger and sit undetected for up to an hour (and be entered after the
# move was over). Between the first snapshot and the last window's end, this
# watches continuously and runs the full confirmation the moment price crosses.
#
# Preferred source is the live WebSocket stream (src/stream.py): free to read,
# no rate limit, exact VWAP/volume. If the stream is off, down or stale, the
# monitor transparently falls back to polling the REST API. The hourly windows
# stay as a last safety net: if the monitor is healthy they just mark
# themselves done; if it isn't they do their own check exactly as before.
MONITOR_POLL_SECONDS = 60  # REST fallback: price-only poll, one call per pending stock
MONITOR_FULL_CHECK_SECONDS = 300  # REST fallback: full check per stock once crossed
MONITOR_STREAM_POLL_SECONDS = 2  # reading the stream is free, so look often
MONITOR_STREAM_FULL_CHECK_SECONDS = 10  # full check is also free from a snapshot
MONITOR_RETRY_SECONDS = 120  # wait after a failed login / dead session
MONITOR_HEALTHY_SECONDS = 180  # window is skipped if the monitor polled this recently
MONITOR_END = CONFIRM_WINDOWS[-1][2]
STREAM_END = dtime(15, 31)  # keep streaming (recording candles) to the close
STREAM_LOOP_SECONDS = 5  # serve-loop wake interval while the stream is being watched
PLAUSIBLE_MOVE = 0.25  # NSE circuit limits keep a day's move well under this

_monitor = {
    "date": None, "api": None, "last_poll": None, "next_attempt": None,
    "last_ok": None, "last_full": {}, "last_sig": {},
    "stream": None, "stream_disabled": False, "verified": set(), "fast": False, "last_source": None,
    "stream_started_at": None, "stream_ok_notified": False, "stream_fail_notified": False,
}


def _get_session():
    """The one shared Angel login (REST fallback + stream tokens)."""
    api = _monitor["api"]
    if api is None:
        api = AngelAPI()
        api.login()  # raises -> callers back off
        _monitor["api"] = api
    return api


def _monitor_close(force=False):
    if _monitor["stream"] is not None and not force:
        return  # the stream still needs this login
    api = _monitor["api"]
    _monitor["api"] = None
    if api is not None:
        try:
            api.logout()
        except Exception as e:
            print(f"[monitor] logout raised {e.__class__.__name__}: {e}")


def _stop_stream():
    st = _monitor["stream"]
    _monitor["stream"] = None
    if st is not None:
        try:
            st.stop()
        except Exception as e:
            print(f"[stream] stop raised {e.__class__.__name__}: {e}")


def _monitor_is_healthy(now) -> bool:
    ok = _monitor["last_ok"]
    return ok is not None and (now - ok).total_seconds() <= MONITOR_HEALTHY_SECONDS


def _reset_day(now):
    today = now.strftime("%Y-%m-%d")
    if _monitor["date"] != today:
        _stop_stream()
        _monitor_close(force=True)
        _monitor.update(
            date=today, last_poll=None, next_attempt=None, last_ok=None, last_full={}, last_sig={},
            stream_disabled=False, verified=set(), fast=False, last_source=None,
            stream_started_at=None, stream_ok_notified=False, stream_fail_notified=False,
        )


def _manage_stream(now):
    """Start the stream once the first snapshot has run and stop it after the
    close. Failures here only mean we stay on REST polling."""
    state = load_full_state()
    candidates = [Candidate(**c) for c in state.get("candidates", [])]
    wanted = bool(
        config.STREAM_ENABLED
        and not _monitor["stream_disabled"]
        and candidates
        and CONFIRM_WINDOWS[0][0] in state.get("confirm_checks_done", [])
        and dtime(9, 15) <= now.time() <= STREAM_END
    )
    if wanted and _monitor["stream"] is None:
        if _monitor["next_attempt"] and now < _monitor["next_attempt"]:
            return  # a login/session just failed; don't hammer Angel
        tokens = {c.token: c.symbol for c in candidates if c.token}
        if not tokens:
            return
        api = _get_session()
        client = stream.StreamClient(
            config.ANGEL_API_KEY, config.ANGEL_CLIENT_CODE, api.auth_token, api.feed_token, tokens
        )
        client.start()
        _monitor["stream"] = client
        _monitor["stream_started_at"] = now
        print(f"[stream] started for {', '.join(tokens.values())}")
    elif not wanted and _monitor["stream"] is not None:
        _stop_stream()
        print("[stream] stopped")
        if now.time() > MONITOR_END:
            _monitor_close()


STREAM_GRACE_SECONDS = 180  # how long a new stream gets to deliver data before we say so


def _report_stream_status(now):
    """One Telegram note per day when the stream first delivers data, or if it
    never does -- so a problem with the live feed is visible on day one."""
    st = _monitor["stream"]
    if st is None or _monitor["stream_started_at"] is None:
        return
    if st.is_healthy(now):
        if not _monitor["stream_ok_notified"]:
            _monitor["stream_ok_notified"] = True
            notify.send_message(f"*Live stream connected* — watching {len(st.symbols)} stocks tick by tick.")
    elif (
        not _monitor["stream_ok_notified"]
        and not _monitor["stream_fail_notified"]
        and (now - _monitor["stream_started_at"]).total_seconds() > STREAM_GRACE_SECONDS
    ):
        _monitor["stream_fail_notified"] = True
        reason = str(getattr(st, "last_error", None) or "no data received").replace("_", " ").replace("*", "").replace("`", "")
        notify.send_message(f"*Live stream not delivering data* — using REST polling instead. Reason: {reason}")


def stream_status_text() -> str:
    """Plain-text answer to /stream: is the live feed up, what is it seeing,
    and how many one-minute candles have been recorded today."""
    now = timeutil.now_ist()
    lines = ["Live stream status", ""]
    st = _monitor["stream"]
    if not config.STREAM_ENABLED:
        lines.append("Disabled (STREAM_ENABLED=false).")
    elif _monitor["stream_disabled"]:
        lines.append("Disabled for today (an implausible streamed price was seen); using REST polling.")
    elif st is None:
        lines.append("Not running right now (it runs from the 09:20 snapshot until 15:31 on trading days).")
    else:
        lines.append(f"Connection: {'healthy, ticks arriving' if st.is_healthy(now) else 'NOT healthy (no recent ticks)'}")
        if getattr(st, "last_error", None):
            lines.append(f"Last error: {str(st.last_error).replace('_', ' ')}")
        for token, symbol in st.symbols.items():
            snap = st.snapshot(token, max_age=10**9, now=now)
            if snap is None:
                lines.append(f"{symbol}: no tick yet")
                continue
            age = int((now - snap.received_at).total_seconds())
            vwap = f"{snap.vwap:.2f}" if snap.vwap else "n/a"
            lines.append(f"{symbol}: last {snap.ltp:.2f}, VWAP {vwap}, volume {snap.volume:,.0f}, {age}s ago")

    today = now.strftime("%Y-%m-%d")
    candles = [r for r in store.read_records(stream.CANDLE_FILE) if r.get("date") == today]
    lines.append("")
    if not candles:
        lines.append("Minute candles recorded today: none")
    else:
        lines.append(f"Minute candles recorded today: {len(candles)}")
        by_symbol = {}
        for r in candles:
            by_symbol.setdefault(r["symbol"], []).append(r)
        for symbol, rows in by_symbol.items():
            last = rows[-1]
            lines.append(f"{symbol}: {len(rows)} (last {last['t']}, close {last['c']})")
    return "\n".join(lines)


def _distrust_stream(c, snap):
    print(f"[stream] {c.symbol}: streamed price {snap.ltp} is implausible vs prior close {c.last_close} -- disabling stream for today")
    _monitor["stream_disabled"] = True
    _stop_stream()
    notify.send_message(
        f"*Live stream disabled*\n\nThe streamed price for {c.symbol} ({snap.ltp}) doesn't match its "
        f"prior close ({c.last_close}), so today falls back to REST polling."
    )


def _stream_snapshot(c, now):
    """A trustworthy fresh snapshot for this stock, or None (use REST)."""
    st = _monitor["stream"]
    if st is None or _monitor["stream_disabled"] or not st.is_healthy(now):
        return None
    snap = st.snapshot(c.token, now=now)
    if snap is None:
        return None
    if c.symbol not in _monitor["verified"]:
        # Guards against a unit mistake (paise vs rupees) or a wrong token
        # silently feeding bad prices into entry decisions.
        if c.last_close > 0 and abs(snap.ltp - c.last_close) / c.last_close > PLAUSIBLE_MOVE:
            _distrust_stream(c, snap)
            return None
        _monitor["verified"].add(c.symbol)
    return snap


def run_monitor():
    """One monitor tick; called every serve-loop iteration. Never raises."""
    now = timeutil.now_ist()
    try:
        _reset_day(now)
        _manage_stream(now)
        _report_stream_status(now)
    except Exception as e:
        print(f"[stream] management raised {e.__class__.__name__}: {e}")
        _monitor["next_attempt"] = now + timedelta(seconds=MONITOR_RETRY_SECONDS)
    try:
        _run_monitor(now)
    except Exception as e:
        print(f"[monitor] tick raised {e.__class__.__name__}: {e}")
        _monitor["next_attempt"] = timeutil.now_ist() + timedelta(seconds=MONITOR_RETRY_SECONDS)
        _monitor_close()


def _run_monitor(now):
    state = load_full_state()
    first_key = CONFIRM_WINDOWS[0][0]
    candidates = [Candidate(**c) for c in state.get("candidates", [])]
    confirmed = {p["symbol"] for p in state.get("confirmed_plans", [])}
    pending = [c for c in candidates if c.symbol not in confirmed]

    if (
        not pending
        or first_key not in state.get("confirm_checks_done", [])
        or now.time() > MONITOR_END
    ):
        _monitor["fast"] = False
        _monitor_close()  # no-op while the stream is still recording
        return

    st = _monitor["stream"]
    use_stream = st is not None and not _monitor["stream_disabled"] and st.is_healthy(now)
    poll_gap = MONITOR_STREAM_POLL_SECONDS if use_stream else MONITOR_POLL_SECONDS
    full_gap = MONITOR_STREAM_FULL_CHECK_SECONDS if use_stream else MONITOR_FULL_CHECK_SECONDS
    _monitor["fast"] = st is not None
    if use_stream:
        st.set_triggers(
            {c.token: (c.direction, strategy.build_plan(c, len(candidates)).entry_trigger) for c in pending if c.token}
        )

    source = "stream" if use_stream else "rest"
    # Changing source (e.g. the stream just dropped) polls immediately rather
    # than waiting out the other source's interval.
    if (
        _monitor["last_poll"]
        and _monitor["last_source"] == source
        and (now - _monitor["last_poll"]).total_seconds() < poll_gap
    ):
        return
    if _monitor["next_attempt"] and now < _monitor["next_attempt"]:
        return
    _monitor["last_poll"] = now
    _monitor["last_source"] = source

    api = _monitor["api"]  # only needed (and created) if a REST fallback is used

    hhmm = now.strftime("%H:%M")
    new_triggers, gate_entries, prices_seen = [], [], 0
    for c in pending:
        try:
            plan = strategy.build_plan(c, len(candidates))
            snap = _stream_snapshot(c, now) if use_stream else None
            if snap is not None:
                ltp = snap.ltp
            else:
                if api is None:
                    api = _get_session()  # raises -> run_monitor() backs off
                ltp = api.get_ltp(c.symbol, c.token)
            if ltp is None:
                continue
            prices_seen += 1
            crossed = ltp >= plan.entry_trigger if c.direction == "LONG" else ltp <= plan.entry_trigger
            if not crossed:
                continue
            last_full = _monitor["last_full"].get(c.symbol)
            if last_full and (now - last_full).total_seconds() < full_gap:
                continue
            _monitor["last_full"][c.symbol] = now
            plan = strategy.check_confirmation(api, c, plan, len(candidates), snap=snap)
        except Exception as e:
            print(f"[monitor] {c.symbol}: raised {e.__class__.__name__}: {e}")
            continue

        if plan.gates:
            sig = (
                plan.gates["price_broke"], plan.gates["volume_ok"], plan.gates["vwap_ok"],
                plan.gates["too_late"], plan.gates["no_candles"],
            )
            if plan.status == "NO DATA":
                _monitor["last_full"].pop(c.symbol, None)  # retry next poll, don't wait
            if plan.status == "ENTER NOW" or sig != _monitor["last_sig"].get(c.symbol):
                gate_entries.append({"window": hhmm, "symbol": c.symbol, "status": plan.status, **plan.gates})
            _monitor["last_sig"][c.symbol] = sig
        if plan.status == "ENTER NOW":
            plan.entered_at = hhmm
            new_triggers.append(plan)

    if prices_seen == 0:
        # Every price lookup failed: the session is probably dead.
        print(f"[monitor] no prices returned for any pending stock ({getattr(api, 'last_error', None)}) -- dropping session, will retry")
        _monitor["next_attempt"] = now + timedelta(seconds=MONITOR_RETRY_SECONDS)
        _stop_stream()  # its login is probably dead too; restarts with a fresh one
        _monitor_close(force=True)
        return
    _monitor["last_ok"] = now

    if gate_entries or new_triggers:
        state = load_full_state()
        state["gate_log"] = state.get("gate_log", []) + gate_entries
        state["confirmed_plans"] = state.get("confirmed_plans", []) + [dataclasses.asdict(p) for p in new_triggers]
        save_full_state(state)
    if new_triggers:
        print(f"[monitor] {hhmm}: new trigger(s): {', '.join(p.symbol for p in new_triggers)}")
        notify.send_message(report.format_new_triggers(new_triggers, hhmm))


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
                if not intraday:
                    time.sleep(2)  # one retry: an empty reply is usually transient
                    intraday = api.get_intraday_candles(plan.token, interval="FIFTEEN_MINUTE")
                relevant = [
                    c
                    for c in intraday
                    if plan.entered_at <= timeutil.candle_time_str(c[0]) <= config.SQUARE_OFF_TIME
                ]
                if not relevant:
                    # Without candles after entry there is no outcome to compute.
                    # walk_candles() would return exit 0.0, which pnl() turns into
                    # a made-up profit/loss (a short "exiting at 0" = entry x qty).
                    print(f"[summary] {plan.symbol}: no candles after entry {plan.entered_at} -- can't compute outcome")
                    failed_symbols.append(plan.symbol)
                    continue
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
        trade_date = state.get("date") or _today_key()
        for plan, sim, trade_pnl in results:
            store.append_record(
                "trades.jsonl",
                {
                    "date": trade_date,
                    "symbol": plan.symbol,
                    "direction": plan.direction,
                    "entered_at": plan.entered_at,
                    "entry": plan.entry_trigger,
                    "stop_loss": plan.stop_loss,
                    "target": plan.target,
                    "quantity": plan.quantity,
                    "exit": sim.exit_price,
                    "outcome": sim.outcome,
                    "pnl": trade_pnl,
                    "circuit_history": plan.circuit_history,
                    "gates_at_entry": plan.gates,
                },
                key=("date", "symbol"),
            )
        for plan in plans:
            if plan.symbol in failed_symbols:
                store.append_record(
                    "trades.jsonl",
                    {
                        "date": trade_date, "symbol": plan.symbol, "direction": plan.direction,
                        "entered_at": plan.entered_at, "entry": plan.entry_trigger,
                        "stop_loss": plan.stop_loss, "target": plan.target, "quantity": plan.quantity,
                        "exit": None, "outcome": "UNKNOWN", "pnl": None,
                        "circuit_history": plan.circuit_history, "gates_at_entry": plan.gates,
                    },
                    key=("date", "symbol"),
                )
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
    confirmed_results = eval_agent.attach_entry_details(
        state.get("summary_results", []), state.get("confirmed_plans", [])
    )

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
    gate_log = state.get("gate_log", [])
    prompt = eval_agent.build_prompt(candidates, outcomes, confirmed_results, history, gate_log)
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
            "trades": [
                {
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "outcome": r["outcome"],
                    "pnl": r["pnl"],
                    **{k: v for k, v in r["entry_info"].items() if k != "vwap_ok"},
                }
                for r in confirmed_results
            ],
            "watchlist": [
                {
                    "symbol": o["symbol"],
                    "direction": o["direction"],
                    "approach_pct": o.get("approach_pct"),
                    "crossed_trigger": o.get("crossed_trigger"),
                    "gapped_through": o.get("gapped_through"),
                    "blocked_by": eval_agent.gate_block_counts(gate_log, o["symbol"]),
                }
                for o in outcomes
                if "data" not in o
            ],
        }
    )

    store.append_record(
        "days.jsonl",
        {
            "date": state.get("date") or _today_key(),
            "settings": {
                "budget": config.MAX_BUDGET, "max_picks": config.MAX_PICKS,
                "max_chase_atr": config.MAX_CHASE_ATR, "universe": config.UNIVERSE_MODE,
                "volume_multiple": strategy.VOLUME_CONFIRM_MULTIPLE,
                "stop_r": strategy.STOP_R_MULTIPLE, "target_r": strategy.TARGET_R_MULTIPLE,
                "require_vwap": config.REQUIRE_VWAP_CONFIRMATION,
            },
            "watchlist": [dataclasses.asdict(c) for c in candidates],
            "outcomes": outcomes,
            "gate_log": gate_log,
            "confirmed": [r["symbol"] for r in confirmed_results],
            "total_pnl": sum(r["pnl"] for r in confirmed_results),
        },
        key=("date",),
    )

    state["sent_eval"] = True
    save_full_state(state)


def run_auto():
    """Decide what to do based on current IST time + today's state."""
    handle_commands()

    now = timeutil.now_ist()
    if not timeutil.is_trading_day(now.date()):
        # Weekend or listed exchange holiday: no watchlist, checks, summary or
        # (paid) eval. Weekends stay silent; a weekday holiday gets one note so
        # you can tell the tool knows rather than being broken.
        state = load_full_state()
        if (
            not timeutil.is_weekend(now.date())
            and not state.get("closed_notified")
            and now.time() >= WATCHLIST_WINDOW[0]
        ):
            notify.send_message(
                "*Market Closed*\n\nToday is an exchange holiday -- no watchlist, "
                "confirmation checks or eval will run."
            )
            state["closed_notified"] = True
            save_full_state(state)
        print(f"[auto] {now.date()} is not a trading day -- nothing to do")
        return

    run_monitor()

    t = now.time()
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


def _wait_for_next_tick():
    """Sleep until the next loop iteration -- or, while the live stream is
    running, wake early when a watched stock newly crosses its trigger (and
    loop more often while watching)."""
    st = _monitor["stream"]
    if st is None or not st.running:
        time.sleep(LOOP_INTERVAL_SECONDS)
        return
    st.wake.wait(STREAM_LOOP_SECONDS if _monitor["fast"] else LOOP_INTERVAL_SECONDS)
    st.wake.clear()
    time.sleep(1)  # never spin


def run_serve_loop():
    """The persistent-service entry point (see module docstring for why
    this replaced Railway's Cron Schedule). Runs forever, checking the
    clock every LOOP_INTERVAL_SECONDS; a crash in one iteration is logged
    and swallowed so the loop itself never dies from it."""
    print(f"[serve] Starting persistent loop, checking every {LOOP_INTERVAL_SECONDS}s")
    print(f"[serve] Persistent data dir: {config.PERSIST_DIR}")
    while True:
        try:
            run_auto()
        except Exception as e:
            print(f"[serve] run_auto() raised {e.__class__.__name__}: {e}")
        _wait_for_next_tick()


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
