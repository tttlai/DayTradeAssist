# Day Trade Assist

A personal, signal-only day-trading assistant for the Indian (NSE) market.
Every morning it screens a fixed universe of liquid stocks, sends you a
pre-market watchlist on Telegram with entry/stop-loss/target levels sized to
your budget, then re-checks intraday and tells you which (if any) actually
triggered. **You place every order yourself in your Angel One app — this
tool never places, modifies, or cancels an order.**

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

## How it works

Two phases, one Railway service, one cron schedule:

1. **Watchlist (~08:45 IST)** — pulls 30 days of daily candles for each
   stock in `data/universe.csv` (Nifty 50 by default), computes ATR(14),
   RSI(14), and 20-day swing high/low, and shortlists up to `MAX_PICKS`
   setups where price is within 1.5% of its 20-day high (bullish breakout
   watch) or low (bearish breakdown watch) with a rising 5-day volume
   trend. Sends you the watchlist with trigger price, stop-loss (1 ATR),
   target (2 ATR), and share quantity sized to your budget/risk settings.
2. **Confirmation (~09:35 IST)**, after the opening range has formed —
   re-checks each watchlist stock's live price and today's volume pace. A
   stock is promoted to "ENTER NOW" only if price has actually crossed its
   trigger *and* today's volume is running hot vs its normal pace. Everything
   else is marked "no trigger — skip."

Every message repeats your mandatory square-off time (`SQUARE_OFF_TIME`,
default 15:15) since this tool only ever proposes intraday (MIS) trades.

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
python -m src.main --mode confirm     # force the confirmation check now
```

### 4. Deploy to Railway

1. Push this repo to GitHub, then in Railway: **New Project → Deploy from
   GitHub repo**.
2. Add all variables from `.env.example` under the service's **Variables**
   tab (this is where `MAX_BUDGET`, `RISK_PER_TRADE_PCT`, etc. live — edit
   them anytime without touching code to change your budget day to day).
3. **Attach a Volume** to the service, mounted at `/app/data`. This is
   required — it's how the watchlist picked at 08:45 is remembered when the
   09:35 confirmation run happens in a separate container invocation, and
   how the scrip-master cache avoids re-downloading every run.
4. Under **Settings → Cron Schedule**, set:
   ```
   */5 3-4 * * 1-5
   ```
   This runs the service every 5 minutes between 03:00–04:59 UTC
   (= 08:30–10:29 IST) on weekdays. `src/main.py --mode auto` (the default
   start command, already set in `railway.toml`) figures out on each run
   whether it's watchlist time, confirmation time, or nothing to do, and
   won't send the same report twice in a day.
5. Trigger a manual deploy/run once to confirm you get a Telegram message
   (or check the deploy logs).

## Customizing

- **Universe**: edit `data/universe.csv` — any NSE symbol Angel One lists
  as `<SYMBOL>-EQ`. Keep it liquid; the screener already filters out
  20-day average volume below 200k shares.
- **Budget / risk**: `MAX_BUDGET`, `RISK_PER_TRADE_PCT`, `MAX_PICKS` env
  vars — no redeploy needed on Railway, just edit and the next run picks
  it up.
- **Strategy**: the setup logic lives in `src/screener.py::evaluate`
  (which stocks qualify) and `src/strategy.py::build_plan` (entry/stop/
  target math). Swap in your own rules (VWAP, ORB, a specific indicator)
  there — `src/indicators.py` has ATR/RSI/volume helpers to build from.

## Project layout

```
src/
  angel_api.py   Read-only SmartAPI wrapper (login, quotes, candles)
  indicators.py  ATR / RSI / volume helpers over OHLCV candles
  screener.py    Universe scan -> ranked candidate setups
  strategy.py    Candidate -> entry/stop/target, + intraday confirmation
  risk.py        Position sizing from budget + risk-per-trade
  report.py      Telegram message formatting
  notify.py      Telegram send
  main.py        Orchestration + auto time-window scheduling
data/
  universe.csv       Stocks to scan (edit this to change coverage)
  today_state.json   Runtime state (watchlist + sent flags), gitignored
```
