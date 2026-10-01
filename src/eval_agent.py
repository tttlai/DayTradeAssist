"""
Daily LLM-powered self-evaluation, run once a day after the end-of-day
summary (see main.py's EVAL_WINDOW, ~15:45-17:00 IST).

Unlike every other phase in this project, this one costs real money --
a small per-day charge to your Anthropic account for one API call.
Everything else here (NSE bhavcopy, Angel One, Telegram) is free.
Without ANTHROPIC_API_KEY set, this phase just skips itself quietly,
same as Telegram being unconfigured elsewhere in the codebase.

What it looks at, and why each piece matters:
  - Today's full watchlist (every candidate's setup details), not just
    the ones that got confirmed -- a screener that only ever looks at
    its own picks can't tell if it's too strict or too loose.
  - What ACTUALLY happened to every watchlist stock by end of day,
    fetched fresh via Angel's intraday candles -- including stocks that
    never triggered. This is the one piece of data nothing else in this
    project computes: did a stock we passed on actually make the move we
    were looking for, just without the volume/VWAP confirmation we
    require? That's the concrete signal for "is the filter too strict."
  - The day's actual confirmed-trade outcomes (from the summary phase).
  - A short rolling history of recent days (data/eval_history.json, kept
    separately from the daily-reset state since it needs to persist
    across days) so the model isn't evaluating today in a vacuum.

The model is asked for a short, concrete, data-grounded critique --
not generic trading advice -- capped in length to fit a Telegram message.
"""
import json

from . import config, timeutil

EVAL_HISTORY_FILE = config.ROOT_DIR / "data" / "eval_history.json"
MAX_HISTORY_DAYS = 14
MAX_EVAL_TOKENS = 700  # keeps the response comfortably under Telegram's 4096-char limit


def load_eval_history() -> list[dict]:
    if not EVAL_HISTORY_FILE.exists():
        return []
    try:
        return json.loads(EVAL_HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def append_eval_history(entry: dict):
    history = load_eval_history()
    history.append(entry)
    history = history[-MAX_HISTORY_DAYS:]
    EVAL_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVAL_HISTORY_FILE.write_text(json.dumps(history))


def fetch_watchlist_outcomes(api, candidates) -> list[dict]:
    """For every watchlist candidate -- confirmed or not -- fetch today's
    full intraday candles and report what actually happened: did price
    ever cross the trigger level at all today, and what was the day's
    actual range? Never raises; a per-symbol failure is recorded as
    "data": "unavailable" rather than aborting the rest."""
    outcomes = []
    for c in candidates:
        try:
            intraday = api.get_intraday_candles(c.token, interval="FIFTEEN_MINUTE")
        except Exception as e:
            print(f"[eval] {c.symbol}: intraday fetch raised {e.__class__.__name__}: {e}")
            intraday = []

        if not intraday:
            outcomes.append(
                {"symbol": c.symbol, "direction": c.direction, "trigger": c.trigger_level, "data": "unavailable"}
            )
            continue

        crossed = False
        cross_time = None
        for candle in intraday:
            ts, _o, h, l, _c, _v = candle
            if c.direction == "LONG" and h >= c.trigger_level:
                crossed, cross_time = True, timeutil.candle_time_str(ts)
                break
            if c.direction == "SHORT" and l <= c.trigger_level:
                crossed, cross_time = True, timeutil.candle_time_str(ts)
                break

        outcomes.append(
            {
                "symbol": c.symbol,
                "direction": c.direction,
                "trigger": c.trigger_level,
                "score": round(c.score, 2),
                "atr_pct": round(c.atr / c.last_close * 100, 2) if c.last_close else None,
                "crossed_trigger": crossed,
                "cross_time": cross_time,
                "day_high": max(cd[2] for cd in intraday),
                "day_low": min(cd[3] for cd in intraday),
                "day_close": intraday[-1][4],
            }
        )
    return outcomes


def build_prompt(candidates, outcomes, confirmed_results, history) -> str:
    today = timeutil.now_ist().strftime("%d %b %Y")

    watchlist_lines = []
    outcomes_by_symbol = {o["symbol"]: o for o in outcomes}
    confirmed_symbols = {r["symbol"] for r in confirmed_results}
    for c in candidates:
        o = outcomes_by_symbol.get(c.symbol, {})
        status = "CONFIRMED" if c.symbol in confirmed_symbols else (
            "crossed trigger but NOT confirmed (filtered out)" if o.get("crossed_trigger")
            else "never crossed trigger"
        )
        watchlist_lines.append(
            f"- {c.symbol} {c.direction} | trigger {c.trigger_level} | score {c.score:.2f} | "
            f"ATR% {round(c.atr / c.last_close * 100, 2) if c.last_close else '?'} | "
            f"day range {o.get('day_low', '?')}-{o.get('day_high', '?')}, close {o.get('day_close', '?')} | "
            f"{status}"
        )

    confirmed_lines = [
        f"- {r['symbol']} {r['direction']}: entry {r['entry']} -> exit {r['exit']} "
        f"({r['outcome']}), P&L Rs {r['pnl']:.0f}"
        for r in confirmed_results
    ] or ["(nothing was confirmed today)"]

    history_lines = [
        f"- {h['date']}: {h['watchlist_count']} watchlist, {h['confirmed_count']} confirmed, "
        f"outcomes {h['outcomes']}, P&L Rs {h['total_pnl']:.0f}"
        for h in history
    ] or ["(no prior history yet)"]

    return f"""You are reviewing one trading day's output from an automated NSE day-trading
signal tool (technical breakout/breakdown screener + volume + VWAP confirmation).
This is for the tool's own developer/user to improve the system -- not investment
advice, and not addressed to an end investor.

Today's date: {today}

TODAY'S WATCHLIST ({len(candidates)} stocks) -- shortlisted pre-market from a 20-day
breakout/breakdown screen with rising volume and a minimum ATR% filter:
{chr(10).join(watchlist_lines)}

CONFIRMED TRADES TODAY (price crossed the trigger AND volume AND, if enabled, VWAP
confirmed -- the only subset actually "traded"):
{chr(10).join(confirmed_lines)}

RECENT HISTORY (last {len(history)} days, oldest first):
{chr(10).join(history_lines)}

Please answer concretely, grounded in the numbers above, not generic trading
platitudes:
1. Looking at stocks that crossed their trigger but were filtered out (not
   confirmed) -- does the volume/VWAP confirmation look too strict today, too
   loose, or about right? Say why, using the actual day-range data.
2. Anything in today's confirmed trades' outcomes worth flagging, especially in
   light of the recent history trend?
3. One to three specific, concrete suggestions for this tool's screener/strategy
   logic (not general trading wisdom) -- reference actual parameters if you have
   a specific one in mind (e.g. the volume multiplier, ATR thresholds, universe
   composition).

Keep your entire reply under 280 words -- it's going straight into a Telegram
message. No preamble, no disclaimers about not being financial advice (that's
already handled elsewhere), just the analysis."""


def call_eval(prompt: str) -> str | None:
    """Returns the eval text, or None if unconfigured or the call failed.
    Never raises."""
    if not config.ANTHROPIC_API_KEY:
        print("[eval] ANTHROPIC_API_KEY not configured, skipping eval")
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.EVAL_MODEL,
            max_tokens=MAX_EVAL_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"[eval] LLM call failed: {e.__class__.__name__}: {e}")
        return None
