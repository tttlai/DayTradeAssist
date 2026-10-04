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

It's also asked for an OPTIONAL structured suggestion: a single tweak to
one of a pre-approved whitelist of numeric constants (see
github_pr.TUNABLE_PARAMS). If main.py finds one, it opens a GitHub PR
proposing exactly that change and asks you to approve it with a plain
Telegram reply. This stays scoped to simple, bounded numbers on purpose
-- a bare "yes" is a strong enough review for "this one number changes
within a safe range," but would not be a safe way to approve an
arbitrary code diff you never actually saw, which is why anything more
involved just stays a prose suggestion for you to bring to the
developer directly, same as before this feature existed.
"""
import json
import re

from . import config, timeutil
from .github_pr import TUNABLE_PARAMS

SUGGESTION_MARKER = "---SUGGESTION---"

EVAL_HISTORY_FILE = config.ROOT_DIR / "data" / "eval_history.json"
MAX_HISTORY_DAYS = 14
# Headroom for models that emit a thinking block before the answer (thinking
# counts toward max_tokens); the prompt itself asks for a short reply, which
# is what keeps the message under Telegram's 4096-char limit.
MAX_EVAL_TOKENS = 2500


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


def approach_pct(direction, last_close, trigger, day_high, day_low) -> float | None:
    """How far price travelled from yesterday's close toward the trigger, as
    a percentage (100 = reached it, >100 = went through). None if the
    trigger isn't actually on the far side of last_close."""
    if direction == "LONG":
        distance, travelled = trigger - last_close, day_high - last_close
    else:
        distance, travelled = last_close - trigger, last_close - day_low
    if distance <= 0:
        return None
    return round(max(travelled, 0.0) / distance * 100, 1)


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

        day_high = max(cd[2] for cd in intraday)
        day_low = min(cd[3] for cd in intraday)
        outcomes.append(
            {
                "symbol": c.symbol,
                "direction": c.direction,
                "trigger": c.trigger_level,
                "score": round(c.score, 2),
                "approach_pct": approach_pct(c.direction, c.last_close, c.trigger_level, day_high, day_low),
                "atr_pct": round(c.atr / c.last_close * 100, 2) if c.last_close else None,
                "crossed_trigger": crossed,
                "cross_time": cross_time,
                "day_high": day_high,
                "day_low": day_low,
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
            f"got {o.get('approach_pct', '?')}% of the way from prior close to trigger | "
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
        + (
            ", approach% " + ", ".join(
                f"{w['symbol']} {w['approach_pct']}" for w in h["watchlist"] if w.get("approach_pct") is not None
            )
            if h.get("watchlist") else ""
        )
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
already handled elsewhere), just the analysis.

After your analysis, on its own, add exactly this marker line:
{SUGGESTION_MARKER}
followed by ONE line of JSON. If, and only if, today's evidence clearly
supports changing one specific numeric constant, output:
{{"param": "<name>", "new_value": <number>, "reason": "<one short sentence>"}}
<name> MUST be exactly one of: {", ".join(TUNABLE_PARAMS.keys())}
<new_value> MUST be within its allowed range: {
        ", ".join(f"{k} in [{v['min']}, {v['max']}]" for k, v in TUNABLE_PARAMS.items())
    }
If you don't have a specific, evidence-backed change to propose today (this
should be the common case -- most days don't warrant a change), output
exactly: NONE
Do not propose a change based on one day's data alone unless it's a very
clear-cut case; prefer NONE when in doubt."""


def split_eval_response(raw: str) -> tuple[str, dict | None]:
    """Splits the model's response into (prose, suggestion-or-None).
    Validates the suggestion strictly before trusting it at all: must
    name a whitelisted param, must be a real number, clamped to that
    param's safe range regardless of what was asked for. Any parsing or
    validation failure is treated the same as an explicit NONE -- a
    malformed suggestion just means no PR gets opened, never a crash."""
    if SUGGESTION_MARKER not in raw:
        return raw.strip(), None

    prose, _, rest = raw.partition(SUGGESTION_MARKER)
    prose = prose.strip()
    rest = rest.strip()

    if rest.upper().startswith("NONE"):
        return prose, None

    match = re.search(r"\{.*\}", rest, re.DOTALL)
    if not match:
        print("[eval] suggestion marker present but no JSON found -- treating as no suggestion")
        return prose, None

    try:
        suggestion = json.loads(match.group(0))
        param = suggestion["param"]
        new_value = float(suggestion["new_value"])
        reason = str(suggestion.get("reason", "")).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"[eval] couldn't parse suggestion JSON ({e.__class__.__name__}), treating as no suggestion")
        return prose, None

    spec = TUNABLE_PARAMS.get(param)
    if not spec:
        print(f"[eval] suggested param '{param}' isn't whitelisted, ignoring")
        return prose, None

    clamped = max(spec["min"], min(spec["max"], new_value))
    return prose, {"param": param, "new_value": clamped, "reason": reason or "(no reason given)"}


last_error: str | None = None


def call_eval(prompt: str) -> str | None:
    """Returns the eval text, or None if unconfigured or the call failed
    (the reason is left in `last_error`). Never raises."""
    global last_error
    last_error = None
    if not config.ANTHROPIC_API_KEY:
        print("[eval] ANTHROPIC_API_KEY not configured, skipping eval")
        last_error = "ANTHROPIC_API_KEY is not set on the Railway service"
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.EVAL_MODEL,
            max_tokens=MAX_EVAL_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        if not text:
            last_error = f"empty response (stop_reason={response.stop_reason})"
            print(f"[eval] LLM returned no text: {last_error}")
            return None
        return text
    except Exception as e:
        print(f"[eval] LLM call failed: {e.__class__.__name__}: {e}")
        last_error = f"{e.__class__.__name__}: {str(e)[:200]}"
        return None
