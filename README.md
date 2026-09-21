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
2. **Confirmation — roughly hourly, 09:20 through 14:00 IST** (see
   `CONFIRM_WINDOWS` in `src/main.py`) — re-checks each still-pending
   watchlist stock's live price and today's volume pace. A stock is
   promoted to "ENTER NOW" only if price has actually crossed its trigger
   *and* today's volume is running hot vs its normal pace; once a stock
   triggers it's not re-checked again that day. Checking isn't a single
   9:35 snapshot — a breakout that happens at 11:20 instead of 9:20 still
   gets caught. The **first** check (09:20) always sends a full status
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
   day's total, had you followed every signal exactly.

Every message repeats your mandatory square-off time (`SQUARE_OFF_TIME`,
default 15:15) since this tool only ever proposes intraday (MIS) trades.

## Data sources

Two different sources feed this tool, split by whether the phase actually
needs *live* data:

| Phase | Needs live data? | Source | Login required? |
|---|---|---|---|
| Watchlist (daily screen) | No — works off yesterday's close | NSE bhavcopy (`src/nse_data.py`) | No |
| Backtest | No — historical daily candles | NSE bhavcopy (`src/nse_data.py`) | No |
| Confirmation | Yes — live price/volume | Angel One SmartAPI | Yes |
| End-of-day summary | Yes — real intraday candles | Angel One SmartAPI | Yes |
| `/health` check | Checks both | Both | Attempts an Angel login |

NSE publishes a free "bhavcopy" file each trading day with end-of-day
OHLCV for every listed stock — no registration, no API key, no rate
limit worth worrying about for once-a-day use. This cuts your daily
Angel One logins roughly in half (down to just the confirmation and
summary runs) and means the backtest needs no Angel credentials at all.

Two things worth knowing about `src/nse_data.py`:
- **NSE's CDN silently hangs connections without a browser-like
  User-Agent header** (no clean error, just a timeout) — already handled,
  but if NSE ever changes their anti-bot behavior, that's the symptom.
- **NSE occasionally changes their bhavcopy URL format** (they have
  before). If the watchlist starts reporting "No qualifying setups" every
  day for no clear reason, check `BHAVCOPY_URL` in `src/nse_data.py`
  against NSE's current archives page.

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

### 3. Local run (optional, to test before deploying)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials
python -m src.main --mode watchlist   # force the pre-market report now
python -m src.main --mode confirm     # force the FIRST (09:20) confirmation check now
python -m src.main --mode confirm --window 1120   # force a specific later check instead
python -m src.main --mode summary     # force the end-of-day P&L summary now
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

### 4. Deploy to Railway

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
  report.py      Telegram message formatting
  notify.py      Telegram send
  main.py        Orchestration + auto time-window scheduling + commands
data/
  universe.csv         Stocks to scan (edit this to change coverage)
  today_state.json     Runtime state (watchlist + confirmed trades + sent
                        flags), resets daily, gitignored
  telegram_state.json  Last processed Telegram update ID, never resets,
                        gitignored
```
