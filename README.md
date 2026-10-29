# Day Trade Assist

A personal, signal-only day-trading assistant for the Indian (NSE) market.
Every morning it screens a fixed universe of liquid stocks, sends you a
pre-market watchlist on Telegram with entry/stop-loss/target levels sized to
your budget, re-checks intraday and tells you which (if any) actually
triggered, then reports what your hypothetical P&L for the day would have
been if you'd followed every signal exactly. **You place every order
yourself in your Angel One app — this tool never places, modifies, or
cancels an order.**

## Important — read before using

- This is a rules-based technical screener, not financial advice, and it is
  **not SEBI-registered investment/research advice**. It's for your own
  personal use only — don't distribute picks from this to other people.
- **No day-trading system can guarantee a fixed daily profit.** Position
  sizing here is risk-based (a % of your budget per trade), not tuned to
  hit any target ₹ amount — treat any "make ₹X/day" goal as an aspiration,
  not something the tool enforces.
- The screener uses one heuristic (breakout/breakdown near a 20-day
  high/low with rising volume). It has not been backtested here — you
  should paper-trade its signals for a few weeks before risking real money,
  and adjust `src/screener.py` / `src/strategy.py` if you have a different
  edge you want to encode.
- Angel One SmartAPI credentials are used **read-only** (quotes + candles).
  Nothing in this codebase calls an order-placement endpoint.
- Only the intraday confirmation and end-of-day summary phases actually
  log into Angel One. The pre-market watchlist and the backtest both use
  NSE's free, login-free bhavcopy instead (see "Data sources" below) —
  neither needs live data, so there's no reason to spend an Angel login,
  and its stricter rate limits, on them.

## How it works

Three phases, one Railway service running as a persistent self-scheduling
loop (not a Railway Cron Schedule — see the Deploy section for why):

1. **Watchlist (~08:45 IST)** — pulls 30 days of daily candles (from NSE's
   free bhavcopy, no Angel login) for each stock in `data/universe.csv`
   (Nifty 50 by default), computes ATR(14),
   RSI(14), and 20-day swing high/low, and shortlists up to `MAX_PICKS`
   setups where price is within 1.5% of its 20-day high (bullish breakout
   watch) or low (bearish breakdown watch) with a rising 5-day volume
   trend. Stocks with ATR below `MIN_ATR_PCT` (0.8% of price) are dropped
   as too "sleepy" to move meaningfully intraday; among the rest, higher
   ATR% nudges the score up. Candidates priced above what `MAX_BUDGET` can
   actually buy at least 1 share of (split evenly across `MAX_PICKS`) are
   dropped *before* ranking, even if their setup scores highest — your
   budget shapes which stocks get proposed, not just how many shares of an
   already-chosen one. A stock that's hit a circuit limit (High == Low for
   a whole session) in the last 10 days is flagged with a ⚡ risk note, not
   scored up or down — it's a caution signal, not something to chase. If
   today looks like monthly F&O expiry (last Thursday of the month), a
   ⚠️ warning is added since breakout setups tend to whipsaw more on
   expiry days. Sends you the watchlist with trigger price, stop-loss
   (1 ATR), target (2 ATR), and share quantity sized to your budget/risk
   settings.
2. **Confirmation — 09:20 through 14:15 IST** — a first status snapshot at
   09:20, then a **continuous monitor**: one Angel login held for the day,
   a price-only poll about every minute per still-pending stock, and the
   full volume/VWAP check (at most every 5 minutes per stock) the moment
   price crosses its trigger, so a breakout is caught within about a minute
   rather than at the next hourly window. The hourly `CONFIRM_WINDOWS` in
   `src/main.py` remain as a safety net: when the monitor is healthy they
   just mark themselves done, and if it isn't they run their own check. The
   volume pace uses real minutes since the 09:15 open (not 15 minutes per
   candle, which overstated it while the newest candle was still forming).
   An entry is also skipped as "too late" when price is already more than
   `MAX_CHASE_ATR` (default 0.5) ATRs past the trigger — a chase whose stop
   sits back at the broken level; set `MAX_CHASE_ATR=0` to turn it off.
   The screener looks at the ~50 large caps in `data/universe.csv` by
   default. `UNIVERSE_MODE=liquid` instead screens every NSE stock above a
   daily-value and price floor (`UNIVERSE_MIN_DAILY_VALUE`,
   `UNIVERSE_MIN_PRICE`), but it is opt-in because the backtest (realistic
   fills, 120 days) was clearly worse with it than with the CSV list.
   At most `MAX_PER_SECTOR` (default 2) watchlist picks may come from one
   sector, using the `sector` column of `data/universe.csv`, because a
   trending sector can otherwise fill the list with one correlated bet (the
   five IT names did exactly that for six days running). Backtest over 120
   days with realistic fills: a cap of 2 was neutral (-3,297 vs -3,288 with no
   cap) and a cap of 1 hurt (-5,905); only 4 of 110 two-plus-pick days
   historically had every pick in a single sector, so this guards the extreme
   case and costs nothing. `0` turns it off; sector labels are approximate.
   Each check's price/volume/VWAP/too-late results are recorded and shown
   to the daily eval. The original description of each check follows:
   re-checks each still-pending
   watchlist stock's live price and today's volume pace. A stock is
   promoted to "ENTER NOW" only if price has actually crossed its trigger,
   today's volume is running hot vs its normal pace, *and* (if
   `REQUIRE_VWAP_CONFIRMATION`, on by default) price is on the expected
   side of today's VWAP — above it for a LONG, below it for a SHORT. That
   VWAP check can't be backtested against this project's free NSE data
   (VWAP needs real intraday candles, which the daily bhavcopy doesn't
   have), so it's judged by watching live results rather than a
   historical backtest — set `REQUIRE_VWAP_CONFIRMATION=false` to turn it
   off if it doesn't end up helping. Once a stock triggers it's not
   re-checked again that day. Checking isn't a single
   9:35 snapshot — a breakout that happens at 11:20 instead of 9:20 still
   gets caught. When a stock does confirm, its stop-loss/target/quantity
   are fully recomputed around the real confirmed price, not left as
   whatever was computed from the morning's watchlist trigger level
   (confirmed necessary in production: a stock confirmed at 13:20 had
   already drifted 7% past its original trigger, leaving a stale target
   sitting on the wrong side of the real entry). The **first** check (09:20) always sends a full status
   message (same as before); **later** checks stay silent unless something
   *new* triggered, so you're not getting a repeat "still nothing" message
   every hour — a new trigger gets its own focused alert instead. Checks
   stop at 14:00-14:15 rather than continuing to market close: an entry
   confirmed any later has too little runway before the mandatory 15:15
   square-off to realistically reach a 2R target, and each check costs an
   Angel One login, so more/later checks isn't free. If the live price
   fetch itself fails for a stock, that's flagged separately with a ⚠️
   warning at the top of the message, distinct from a normal no-trigger,
   since it points to an Angel One API/token problem worth investigating
   rather than the market just not cooperating. One failure isolated to a
   single stock during a single window can't derail anything else: it's
   caught, logged, and that stock is just treated as a data failure for
   that check rather than aborting the whole window. And if a window ever
   fails so completely that it never gets marked done at all (say, Angel
   One's login itself was down for that entire 15-minute window), the
   next check after that window's end time auto-marks it skipped rather
   than blocking every later check and the summary for the rest of the
   day waiting for something that will never come.
3. **End-of-day summary (~15:40 IST)** — for whatever got confirmed in step
   2, replays the real 15-minute intraday candles from your alerted entry
   time through square-off to see which was actually touched first, the
   stop-loss or the target (or neither, in which case it uses the
   square-off-time price). Reports the hypothetical P&L per trade and the
   day's total, had you followed every signal exactly. Same resilience as
   the confirmation phase: one confirmed trade's API failure is surfaced
   with a ⚠️ note rather than silently costing the whole summary, and if
   the summary window closes without ever completing, you get a fallback
   "couldn't generate today's summary" notice instead of silence.
4. **Daily eval (~15:45-17:00 IST, after the summary)** — optional, needs
   `ANTHROPIC_API_KEY` (see "Daily eval agent" below). Fetches what
   *actually* happened today to every watchlist stock, not just the
   confirmed ones — including the ones the volume/VWAP filters rejected —
   and asks an LLM to assess whether the confirmation criteria look too
   strict, too loose, or about right, using the day's real price action as
   evidence, plus a short rolling history so it's not judging one day in
   isolation. This is the one phase in the whole project with a real
   recurring cost (a per-day API charge); everything else is free.

   If `GITHUB_TOKEN` is also set, the eval can go one step further: when
   the day's evidence clearly supports it (rare by design — most days
   shouldn't warrant a change), it opens a GitHub PR tweaking exactly one
   of a pre-approved whitelist of numeric constants (`VOLUME_CONFIRM_MULTIPLE`,
   `STOP_R_MULTIPLE`, `TARGET_R_MULTIPLE`), clamped to a safe range
   regardless of what it asks for. You approve or reject it with a plain
   **YES** or **NO** reply in Telegram — approving merges the PR (Railway
   redeploys with the new value), rejecting closes it unmerged. This is
   deliberately scoped to simple, bounded numbers: a bare reply is a
   strong enough review for "this one number changes within a safe
   range," but wouldn't be a safe way to approve an arbitrary code diff
   you never actually saw — anything more involved than a whitelisted
   constant just stays a prose suggestion for you to bring to the
   developer directly, same as before this existed. A bare "yes"/"no"
   only ever does anything when a suggestion is actually pending; sent
   any other time, they're just treated as an unrecognized command, so
   there's no risk of accidentally triggering one through casual chat
   with the bot.

### Live tick stream (WebSocket)

Instead of repeatedly polling Angel's REST API, the tool holds one
WebSocket open (`src/stream.py`, Angel's SmartWebSocketV2, read-only) from
the 09:15 open until 15:31 and subscribes to the day's watchlist. Each
packet carries the last price, the **exchange's own running VWAP**,
cumulative day volume and the day's open/high/low, so the confirmation check
needs no candle fetches at all: a breakout is seen within a couple of seconds
(the main loop wakes the instant a watched stock crosses its trigger) instead
of at the next poll, with no rate limit and no empty-candle failures.

- **The 09:20 snapshot reads the stream.** Because the stream is already up
  by then, the first confirmation check uses its snapshots and makes no REST
  calls, which avoids the burst of price and candle requests that hit Angel's
  rate limit at 09:20 (it failed for TCS two days running). A stock the stream
  has no data for yet falls back to REST on its own.
- **REST is the automatic fallback.** If the stream is down, stale (no ticks
  for 90s) or has no data for a stock yet, the monitor polls REST exactly as
  before, and switches back when the stream recovers.
- **Sanity guard.** The first streamed price per stock must be within 25% of
  its prior close, otherwise (wrong units/token) the stream is disabled for the
  day, you get a Telegram note, and REST takes over.
- **Visibility.** You get one Telegram note per day when the stream first
  delivers data, or if it hasn't within 3 minutes (with the reason).
- **One login.** The stream, the REST fallback and the fallback confirm
  windows share a single Angel login, which is released after the close.
- **Candle recording.** Each closed minute is appended to `candles_1m.jsonl`
  on the Volume (`date, symbol, t, o, h, l, c, v, partial`) for the watchlist
  stocks. That is real intraday history to backtest against later; the free
  NSE files only have daily bars. A candle is `partial` if the stream joined
  mid-minute (its volume undercounts).
- Send `/stream` in Telegram to check it: connection health, the latest price/VWAP/
  volume per stock, and how many minute candles have been recorded today.
- Turn it off with `STREAM_ENABLED=false`.

### Gap-fade alerts for cheap stocks

A separate, experimental feature for stocks priced Rs 10-100 that trade at
least Rs 3 crore a day (about 150 names). It does **not** pick stocks the
evening before: a backtest on 265 days of whole-market data found nothing
known beforehand that predicts direction, but it did find that these stocks
tend to **fade an opening gap** -- one that opens well above yesterday's close
drops back, one that opens well below bounces. So:

- **From 16:40, "Gap-Fade Sheet - results" note and graded CSV.** Once NSE
  publishes the day's file, every stock on that day's order sheet is graded and
  the CSV comes back with the result filled in (`gap_scan_results_<date>.csv`).
  A stock that opened with a 2-5% gap is graded as if traded at the open with
  the standard 3% target / 2% stop, replayed on real one-minute prices (daily
  bar if those can't be fetched; a stop touched first wins), with the P&L per
  share, in percent and in rupees for the quantity your budget gives. Stocks
  with no signal carry their open-to-close move only. The `alerted` column
  marks the ones that were really alerted, since the 3-alert cap means you saw
  fewer than the sheet shows. Saved to `penny_sheet_results.jsonl`.
- **From 18:00 the evening before, "Gap-Fade Scan - ready for <next trading
  day>" note.** NSE publishes the day's data at about 16:35 IST, so the
  universe is built from today's whole-market close (no Angel login) and saved
  for the next trading day (skipping weekends and holidays). It checks every
  5 minutes until the file is out, and gives up quietly at 22:00. If no
  universe exists by the morning, it is built at 08:40-09:30 as a fallback.
  Nothing needs placing that evening: the direction isn't known until the
  open. The note carries an **order sheet** for the 15 most liquid stocks
  ("SELL if open >= X | BUY if open <= Y", with the skip-beyond levels) and the
  **full list is attached as a CSV**, so the decision at 09:15 is a glance, not
  a calculation. Whichever way each stock opens picks its side.
- **09:00-09:15, pre-open probe and heads-up.** The stream is up from 09:00.
  NSE sets indicative opening prices at 09:08; if Angel's feed carries them, a
  **Pre-Open Heads-Up** at about 09:10 names the likely gap-fade setups five
  minutes before the open (the official alert then marks which were already
  flagged). Whether the feed really shows pre-open prices is unverified, so
  what it shows for the 60 most liquid names is recorded at 09:02, 09:06,
  09:10 and 09:14 to `penny_preopen.jsonl`; the heads-up simply stays silent if
  the feed shows nothing. `PENNY_PREOPEN=false` turns the heads-up off.
- **From 09:15:30, "Gap Alert".** A live stream reads every stock's open. A gap
  UP of 2-5% is a **SHORT** alert, a gap DOWN of 2-5% a **BUY** alert, at most
  3 alerts a day, the most liquid first. Each shows the open, the current
  price, a **3% target and 2% stop** (exit at those levels, not at the close;
  15:15 is the fallback) and a quantity from your budget split three ways. The
  alert is skipped if price has already moved over 1% from the open, and a gap
  over 5% is skipped altogether (news-driven: it lost money in the backtest).
- **After the close, "Gap Alerts - how they turned out".** Each alert is
  replayed against real one-minute prices to see whether the target or stop was
  hit, and kept in `penny_alerts.jsonl` / `penny_results.jsonl`. `/penny` shows
  today's status and the live track record.

Backtest of exactly this policy (`python -m src.penny_backtest`, buying/
shorting at the open, 0.2% round-trip cost, the stop assumed to hit first
whenever both levels were touched in a day): 728 trades over 243 days, 61%
winners, **+0.69% per trade** (+0.66% in the first half, +0.72% in the second),
roughly +Rs 115 on a Rs 16,700 position. It stays positive at 0.4% costs
(+0.49%) and across nearby gap/target/stop settings; shorts did better than
buys. Caveats: it is a backtest on daily bars (they cannot say whether the
target or stop came first, so the true figure lies between the +0.69%
pessimistic and a +1.08% optimistic case), the rules were chosen on the same
data, a market order at 09:16 can fill worse than the open, and shorting needs
the stock to be allowed for intraday shorts in your account. The live
track record is the real test. Turn it off with `PENNY_ENABLED=false`, or use
`PENNY_ALLOW_SHORTS=false` for buy alerts only.

### Trade review (what happened after entry)

Once a stock is confirmed the tool doesn't act on it again -- stops, targets
and the 15:15 exit stay as alerted. To learn whether that is leaving money on
the table, every confirmed trade is replayed after the close
(`src/tradepath.py`) against the day's recorded one-minute candles (or Angel's
15-minute candles if the stream recorded too few). You get a short **Trade
Review** message after the summary, and the same record is kept in
`trade_paths.jsonl` and shown to the daily eval.

Per trade it reports the best and worst price reached in ATRs (and when), the
exit versus entry in ATRs, and when each warning sign *first* appeared: price
back through the trigger, price on the wrong side of VWAP, volume pace fading
below its 20-day norm, and the stop/target levels being touched.

This is **observation only**: nothing changes any decision, and the message
says so. Its purpose is to build a record so exit rules (trailing stops,
breakeven, "exit when VWAP flips") can later be replayed against real
minute data and kept only if they beat holding to 15:15. With a handful of
trades it proves nothing either way.

### Market holidays and Angel rate limits

- **Holidays.** Weekends are skipped automatically. Weekday exchange holidays
  come from `data/market_holidays.txt` (and the `MARKET_HOLIDAYS` env var,
  comma-separated ISO dates): on those days nothing runs -- no watchlist,
  checks, summary or paid eval -- and you get a single "Market Closed" note.
  The file only holds dates confirmed against real data (currently 20 Oct
  2026, Dussehra); add upcoming ones from NSE's published holiday list. A
  confirmation message where every stock returns an empty candle list now
  hints that the market may be closed.
- **Rate limits.** Angel rejects bursts ("exceeding access rate"). Every Angel
  data call is now spaced at least 0.35s apart, and a rate-limited or failed
  call is retried twice with 1s / 2.5s backoff; a definite refusal (invalid
  token etc.) is not retried.

### Reliability

Two real production incidents (a bad confirm window silently blocking the
summary forever, then the same bug pattern in the summary phase itself)
led to a full audit of every place an Angel One/Telegram/NSE network call
sits upstream of the state update that marks a phase "done." The result:

- **Every per-stock/per-trade API call is isolated.** One bad stock during
  a confirmation check, or one confirmed trade whose outcome can't be
  computed during the summary, no longer aborts the whole phase — it's
  logged and surfaced as a data-failure note, and everything else that
  *did* work still gets reported normally.
- **`notify.send_message()` never raises.** Every phase does real
  bookkeeping (marking itself done) immediately after sending its
  message; a hardened, always-catches Telegram send protects all of them
  at once rather than needing every caller to guard against it
  individually.
- **`AngelAPI`'s data-fetching methods never raise** (`get_daily_candles`,
  `get_intraday_candles`) — same empty-result-on-any-failure contract as
  a normal "no data" response, so callers don't need to distinguish
  network failures from business-logic ones.
- **The scrip master falls back to a stale cache** rather than failing
  outright if NSE/Angel's servers are briefly unreachable — instrument
  tokens rarely change day to day, so slightly-stale data beats none.
- **Every phase (watchlist, each confirm window, summary) has a fallback
  path**: if it fails so completely that it never completes within its
  own time window, the next check sends an explicit "couldn't generate
  X due to a technical issue" notice and marks that phase resolved,
  rather than leaving it stuck (silently blocking everything downstream
  that depends on it) for the rest of the day.
- **The watchlist falls back to Angel One if NSE blocks the request
  entirely** (confirmed in production — NSE returned a hard 403 for every
  date attempted, seemingly IP-based, since the identical URL worked fine
  from a different network at the same moment). Costs one extra Angel
  login only on the days this happens, not every day.
- **A malformed Telegram message retries as plain text instead of being
  silently lost.** Also confirmed in production: `tradesim.py`'s
  `TIME_EXIT`/`NO_TRIGGER` outcome strings have a raw underscore, which
  Telegram's Markdown parser reads as opening italics — an odd count
  anywhere in the message breaks the *entire* send with a 400, and
  because that's a real HTTP response rather than a raised exception, the
  phase still marked itself "done" even though the message never arrived.
  Fixed at the source (outcomes are now rendered without underscores) and
  as a general safety net (`notify.send_message()` retries any 400 once
  without `parse_mode`, so a future formatting bug degrades to an
  unformatted message instead of vanishing).

Every message repeats your mandatory square-off time (`SQUARE_OFF_TIME`,
default 15:15) since this tool only ever proposes intraday (MIS) trades.

## Data sources

Two different sources feed this tool, split by whether the phase actually
needs *live* data:

| Phase | Needs live data? | Source | Login required? |
|---|---|---|---|
| Watchlist (daily screen) | No — works off yesterday's close | NSE bhavcopy, falls back to Angel One if NSE is unreachable (`src/nse_data.py`) | No, unless the NSE fallback kicks in |
| Backtest | No — historical daily candles | NSE bhavcopy (`src/nse_data.py`) | No |
| Confirmation | Yes — live price/volume | Angel One SmartAPI | Yes |
| End-of-day summary | Yes — real intraday candles | Angel One SmartAPI | Yes |
| Daily eval (optional) | Yes — today's full intraday candles for every watchlist stock | Angel One SmartAPI + Anthropic API | Yes, plus a paid LLM call |
| `/health` check | Checks both | Both | Attempts an Angel login |

NSE publishes a free "bhavcopy" file each trading day with end-of-day
OHLCV for every listed stock — no registration, no API key, no rate
limit worth worrying about for once-a-day use. This cuts your daily
Angel One logins roughly in half (down to just the confirmation and
summary runs) and means the backtest needs no Angel credentials at all.

Three things worth knowing about `src/nse_data.py`:
- **NSE's CDN silently hangs connections without a browser-like
  User-Agent header** (no clean error, just a timeout) — already handled,
  but if NSE ever changes their anti-bot behavior, that's the symptom.
- **NSE occasionally changes their bhavcopy URL format** (they have
  before). If the watchlist starts reporting "No qualifying setups" every
  day for no clear reason, check `BHAVCOPY_URL` in `src/nse_data.py`
  against NSE's current archives page.
- **NSE can also return a hard 403 for every date, seemingly IP-based**
  (confirmed in production — the identical URL worked fine from a
  different network at the same moment; most likely NSE blocking
  Railway's shared egress IP range, nothing to do with the request
  itself). `fetch_daily_history()` raises `NSEUnavailable` in that case
  rather than quietly returning nothing, and `run_watchlist()` catches it
  and falls back to Angel One's own daily-candle API for that day only —
  costing one extra login on the days this happens, not every day.

## Setup

### 1. Angel One SmartAPI (read-only)

1. Register at https://smartapi.angelbroking.com with your Angel One
   client code.
2. Create an app → note the **API key**.
3. Enable TOTP-based login for SmartAPI and save the **TOTP secret**
   (base32 string, used to generate 6-digit codes programmatically via
   `pyotp` — same mechanism as Google Authenticator).
4. You'll also need your **client code** and **PIN** (the numeric PIN you
   log into the Angel One app with).

### 2. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the
   **bot token**.
2. Message your new bot once (anything), then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read your numeric
   **chat id** from the JSON — or just message
   [@userinfobot](https://t.me/userinfobot) to get your own id.

### 3. Daily eval agent (optional)

Skip this entirely to leave the eval phase off — without
`ANTHROPIC_API_KEY` set, it just doesn't run (no error).

1. Get a key at [console.anthropic.com](https://console.anthropic.com) —
   note this is a **separate product** from claude.ai or Claude Code,
   billed per API call. Set it as `ANTHROPIC_API_KEY`.
2. This is the one real recurring cost in this project — one LLM call a
   day, with maybe a few thousand tokens of context, likely a fraction of
   a cent to a few cents depending on the model. `EVAL_MODEL` defaults to
   `claude-sonnet-5`; switch to a cheaper/faster model if you want to
   trim that further. If your Anthropic account supports workspaces, a
   dedicated one for just this key gives you clean, isolated cost
   visibility for this feature specifically.

#### Auto-tuning via GitHub PR (optional, needs the eval agent above)

1. Create a **fine-grained** personal access token at
   [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens) —
   scope it to **only this one repository**, with **Contents** and
   **Pull requests** permissions set to Read and write, nothing else. Set
   it as `GITHUB_TOKEN`, and set `GITHUB_REPO` to `owner/repo` (e.g.
   `tttlai/DayTradeAssist`).
2. Without this set, the eval still runs and gives its prose analysis —
   it just never proposes a parameter change or opens a PR.
3. When it does propose one, you'll get a Telegram message naming the
   exact constant, its suggested new value, and a PR link. Reply **YES**
   to merge it (Railway redeploys with the new value) or **NO** to close
   it without merging. Only one suggestion is ever pending at a time.

### 4. Local run (optional, to test before deploying)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials
python -m src.main --mode watchlist   # force the pre-market report now
python -m src.main --mode confirm     # force the FIRST (09:20) confirmation check now
python -m src.main --mode confirm --window 1120   # force a specific later check instead
python -m src.main --mode summary     # force the end-of-day P&L summary now
python -m src.main --mode eval        # force the daily eval now (needs ANTHROPIC_API_KEY)
python -m src.main --mode health      # run the health check immediately
```

### Backtesting the screener first (recommended before risking real money)

```bash
python -m src.backtest --days 60
```

Replays the screener's exact rules against the last 60 trading days of
daily candles for your universe and prints win rate, total/average P&L,
and max drawdown. **Read the caveat in `src/backtest.py`'s docstring**:
it uses daily OHLC, not real intraday sequencing, so when a day's range
touches both the stop-loss and the target it can't tell which happened
first and conservatively assumes the stop — results are a rough,
pessimistically-biased lower bound, not a faithful replay. It exists to
catch an obviously broken or negative-expectancy setup before you trade
it live, not to prove an edge. The live end-of-day summary (above) is
the accurate version, since it uses real 15-minute candles for trades
you actually got alerted on.

`--stop-multiple` / `--target-multiple` override the ATR multiples
(defaults: `strategy.STOP_R_MULTIPLE` = 1.0, `strategy.TARGET_R_MULTIPLE`
= 2.0) to compare configurations, e.g. `python -m src.backtest --days 60
--stop-multiple 0.5 --target-multiple 1.0`. **What we found testing
this**: 88-89% of trades hit neither the stop nor the target at the
default multiples — they drift to square-off (`TIME_EXIT`) — consistent
across both a 60-day and a 120-day window, so that's the strategy's
normal behavior, not a red flag. Tightening the multiples to force more
trades to resolve made total P&L and max drawdown *worse* in every
variant tried, not better — a tighter stop mainly bought more
stopped-out trades on ordinary noise, not more clean wins. Left the
defaults as-is based on this; see the outcome breakdown the backtest
prints (`Outcomes: STOPPED n%, TARGET n%, TIME_EXIT n%`) before changing
these yourself.

### 5. Deploy to Railway

**Do not use Railway's Cron Schedule setting for this service.** Cron
Schedule mode tears the container's filesystem down between every
trigger, and — confirmed by actually checking the Settings page, not
just documentation — offers no way to attach a Volume in that mode
either (Volumes, replicas, serverless, and a configurable restart policy
are all unavailable for cron-scheduled services). Since this app needs
the 08:45 watchlist's output to still exist when the 09:35 confirmation
run checks for it an hour later, that's fatal to how it works: the
confirmation run would always find an empty state and report "no
watchlist found," even on a day the watchlist ran perfectly fine.

Instead, this deploys as a normal **persistent service** that schedules
itself internally:

1. Push this repo to GitHub, then in Railway: **New Project → Deploy from
   GitHub repo**.
2. Add all variables from `.env.example` under the service's **Variables**
   tab (this is where `MAX_BUDGET`, `RISK_PER_TRADE_PCT`, etc. live — edit
   them anytime without touching code to change your budget day to day).
3. Leave **Settings → Cron Schedule** empty/unset. `railway.toml` already
   sets the start command to `python -m src.main --mode serve`, which
   runs its own internal loop (see `src/main.py`'s module docstring) —
   checking the clock every 30 seconds, all day, every day, and deciding
   which of the watchlist/confirmation/summary phases (if any) to run.
   Because it's one continuously-running process, state just lives in a
   local file on that process's own disk for as long as it keeps running
   — no Volume needed.
4. Trigger a manual deploy once to confirm you get a Telegram message
   (or check the deploy logs) — you should see `[serve] Starting
   persistent loop...` in the logs shortly after deploy.

**One tradeoff worth knowing**: if you redeploy (a git push, a settings
change) in the middle of a trading day, that day's in-progress state is
lost when the old container is torn down — same failure mode as the
Volume-less cron design had on *every* trigger, just now only on the
rare occasion you actually redeploy mid-day. Avoid pushing changes
during market hours when you can.

### Health check via Telegram

Send **`/health`** (or `/status`) to your bot anytime and it'll reply with
two independent checks:

```
*Health Check*

✅ Angel One reachable — HTTP 200
✅ Angel One login — login OK
✅ Railway service is up — this reply is proof the current run executed
```

The first line hits Angel's public endpoint with no credentials, so it
tells you whether *their* servers are up at all. The second actually logs
in with your API key/PIN/TOTP, so a ❌ there while the first line is ✅
means Angel is fine but your credentials or IP whitelist need attention.
Getting any reply at all is itself proof the Railway service is alive —
that's not a separate network call, just what "you got this message"
already means.

Since the service runs as a persistent loop (see above) rather than
Railway's Cron Schedule, `/health` gets checked every ~30 seconds, any
time of day, any day of the week — no schedule to widen, no waiting for
a cron tick. A reply should land within about 30 seconds of sending it.

## Customizing

- **Universe**: edit `data/universe.csv` — any NSE symbol Angel One lists
  as `<SYMBOL>-EQ`. Keep it liquid; the screener already filters out
  20-day average volume below 200k shares.
- **Budget / risk**: `MAX_BUDGET`, `RISK_PER_TRADE_PCT`, `MAX_PICKS` env
  vars. Saving a variable change on Railway triggers a redeploy, which
  restarts the persistent service — and since state now lives only in
  that running process (no Volume, see Deploy section), **a mid-day
  restart loses that day's in-progress watchlist/confirmation state**,
  the same way an actual crash would. This makes timing your changes
  more important than it used to be: **change budget/risk settings
  before ~08:30 IST**, ahead of the pre-market watchlist run, and don't
  touch them again until after the ~15:40 summary has gone out. Every
  watchlist run also logs the active `MAX_BUDGET`/`RISK_PER_TRADE_PCT`/
  `MAX_PICKS` values, so you can check Railway's logs to confirm exactly
  what was in effect for a given day's run.
- **Strategy**: the setup logic lives in `src/screener.py::evaluate`
  (which stocks qualify) and `src/strategy.py::build_plan` (entry/stop/
  target math). Swap in your own rules (VWAP, ORB, a specific indicator)
  there — `src/indicators.py` has ATR/RSI/volume helpers to build from.
  The volatility floor (`MIN_ATR_PCT`), how much ATR% weighs into the
  score (`ATR_SCORE_WEIGHT`), and the circuit-lock lookback window are
  constants at the top of `src/screener.py` if you want to tune them.

## Project layout

```
src/
  angel_api.py   Read-only SmartAPI wrapper (login, quotes, candles)
  nse_data.py    Free, login-free NSE bhavcopy fetch (daily OHLCV)
  timeutil.py    IST-aware clock helpers (containers run in UTC)
  indicators.py  ATR / RSI / volume helpers over OHLCV candles
  screener.py    Universe scan -> ranked candidate setups
  strategy.py    Candidate -> entry/stop/target, + intraday confirmation
  risk.py        Position sizing from budget + risk-per-trade
  tradesim.py    Walks OHLCV candles to see if a stop/target was hit
  backtest.py    Historical replay of the screener over daily candles
  health.py      /health check: Angel One reachability + login
  telegram_commands.py  Polls Telegram for messages sent to the bot
  eval_agent.py  Optional daily LLM self-eval (needs ANTHROPIC_API_KEY)
  github_pr.py   Opens/merges/closes the eval agent's whitelisted-constant
                 tuning PRs (needs GITHUB_TOKEN)
  report.py      Telegram message formatting
  notify.py      Telegram send
  main.py        Orchestration + auto time-window scheduling + commands
data/
  universe.csv         Stocks to scan (edit this to change coverage)
  today_state.json     Runtime state (watchlist + confirmed trades + sent
                        flags), resets daily, gitignored
  telegram_state.json  Last processed Telegram update ID, never resets,
                        gitignored
  eval_history.json    Rolling ~14-day eval stats log, never resets,
                        gitignored
  pending_suggestion.json  The one eval-agent tuning suggestion awaiting
                        your YES/NO, if any; never resets, gitignored
```

### Persistent storage (Railway Volume)

Without a Volume, everything under `data/` is wiped on every redeploy or
restart -- including the eval's rolling history. Attach a Railway Volume to
the service (mount path `/app/data/store`, **not** `/app/data`, which would
hide `universe.csv`). Railway then sets `RAILWAY_VOLUME_MOUNT_PATH`, and the
daily state, Telegram cursor, eval history, pending suggestion and two
permanent logs are all kept there (override with `PERSIST_DIR`; locally it
falls back to `data/`). Send `/health` to confirm: it reports whether storage
is persistent and writable.

The permanent logs are append-only, one JSON object per line, never pruned:
- `trades.jsonl` -- one record per confirmed trade: entry time/price, stop,
  target, quantity, exit price, outcome, P&L, and the confirmation readings at
  entry (`UNKNOWN` outcome when the data failed).
- `trade_paths.jsonl` -- one record per confirmed trade describing what price
  did AFTER entry (see "Trade review" below).
- `candles_1m.jsonl` -- the stream's closed one-minute candles for the day's
  watchlist stocks (`date, symbol, t, o, h, l, c, v, partial`).
- `days.jsonl` -- one record per day: the settings in force, the watchlist,
  each stock's day open/high/low/close and whether it crossed, and the full
  check-by-check gate log.

Send `/stats` in Telegram for a summary of `trades.jsonl` (trade count, win
rate, total P&L, outcome mix, last 8 trades).
