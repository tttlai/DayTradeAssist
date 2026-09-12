"""
Rules-based technical screener. No ML, no news/sentiment — purely
price/volume structure on daily candles, evaluated pre-market.

For each stock in the universe we look for one of two clean setups:
  - BREAKOUT (long): price sitting within 1.5% of its 20-day high, with a
    rising 5-day volume trend versus its 20-day average (accumulation
    ahead of a possible breakout).
  - BREAKDOWN (short): the mirror image, price within 1.5% of its 20-day
    low with rising volume (distribution ahead of a possible breakdown).

Each candidate gets a numeric score so we can rank and keep only the
strongest MAX_PICKS setups. This is a starting heuristic, not a proven
edge — see README for how to swap in your own rules.

Daily candles come from src/nse_data.py (free, no login) rather than
Angel One -- this phase doesn't need live data, so there's no reason to
spend an Angel login (and its tighter rate limits) on it.
"""
import csv
from dataclasses import dataclass

from . import config, indicators, risk


@dataclass
class Candidate:
    symbol: str
    token: str
    direction: str  # "LONG" or "SHORT"
    last_close: float
    trigger_level: float
    atr: float
    rsi: float
    avg_volume_20: float
    score: float


def load_universe():
    with open(config.UNIVERSE_CSV, newline="") as f:
        return [row["symbol"] for row in csv.DictReader(f)]


NEAR_LEVEL_PCT = 0.015  # within 1.5% of the 20-day high/low
MIN_AVG_VOLUME = 200_000  # skip illiquid names — hard to fill/exit at size


def evaluate(symbol: str, candles) -> Candidate | None:
    if len(candles) < 25:
        return None

    avg_vol_20 = indicators.avg_volume(candles, 20)
    if avg_vol_20 < MIN_AVG_VOLUME:
        return None

    recent_vol_5 = indicators.avg_volume(candles, 5)
    volume_rising = recent_vol_5 > avg_vol_20 * 1.1

    last_close = indicators.closes(candles)[-1]
    swing_high = indicators.prior_swing_high(candles, 20)
    swing_low = indicators.prior_swing_low(candles, 20)
    atr_val = indicators.atr(candles, 14)
    rsi_val = indicators.rsi(candles, 14)

    if atr_val is None or rsi_val is None or swing_high is None or swing_low is None:
        return None

    near_high = swing_high > 0 and (swing_high - last_close) / swing_high <= NEAR_LEVEL_PCT
    near_low = swing_low > 0 and (last_close - swing_low) / swing_low <= NEAR_LEVEL_PCT

    if near_high and volume_rising and rsi_val >= 50:
        score = (recent_vol_5 / avg_vol_20) + (rsi_val / 100)
        return Candidate(
            symbol=symbol,
            token="",
            direction="LONG",
            last_close=last_close,
            trigger_level=swing_high,
            atr=atr_val,
            rsi=rsi_val,
            avg_volume_20=avg_vol_20,
            score=score,
        )

    if near_low and volume_rising and rsi_val <= 50:
        score = (recent_vol_5 / avg_vol_20) + ((100 - rsi_val) / 100)
        return Candidate(
            symbol=symbol,
            token="",
            direction="SHORT",
            last_close=last_close,
            trigger_level=swing_low,
            atr=atr_val,
            rsi=rsi_val,
            avg_volume_20=avg_vol_20,
            score=score,
        )

    return None


def build_watchlist(token_lookup, history, max_picks=None) -> list[Candidate]:
    """token_lookup: callable(symbol) -> Angel instrument token, needed
    later for the live confirmation/summary phases (a lookup against
    Angel's public scrip master, not a login). history: {symbol: candles}
    from nse_data.fetch_daily_history() -- no Angel login involved in
    getting the actual OHLCV data at all."""
    max_picks = max_picks or config.MAX_PICKS
    candidates = []
    for symbol in load_universe():
        candles = history.get(symbol)
        if not candles:
            continue
        cand = evaluate(symbol, candles)
        if cand:
            cand.token = token_lookup(symbol) or ""
            candidates.append(cand)

    candidates.sort(key=lambda c: c.score, reverse=True)

    # Drop anything your budget can't actually buy at least 1 share of,
    # *before* picking the top N -- otherwise a high-priced stock could
    # take a slot and show up with Qty 0. Uses the configured MAX_PICKS
    # (not the eventual, possibly-smaller, final count) as a conservative
    # divisor, so anything that passes this filter is guaranteed >=1 share
    # once real position sizing runs (which divides budget by a count
    # that's never larger than max_picks).
    price_ceiling = risk.min_affordable_price(max_picks)
    affordable = [c for c in candidates if c.last_close <= price_ceiling]
    dropped = len(candidates) - len(affordable)
    if dropped:
        print(
            f"[screener] Dropped {dropped} candidate(s) priced above your "
            f"per-pick budget (Rs {price_ceiling:,.0f} at {max_picks} picks): "
            + ", ".join(c.symbol for c in candidates if c.last_close > price_ceiling)
        )

    return affordable[:max_picks]
