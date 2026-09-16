"""Small, dependency-light technical indicator helpers over OHLCV rows.

Each candle row is [timestamp, open, high, low, close, volume] as returned
by Angel One's getCandleData.
"""
import numpy as np


def closes(candles):
    return np.array([c[4] for c in candles], dtype=float)


def highs(candles):
    return np.array([c[2] for c in candles], dtype=float)


def lows(candles):
    return np.array([c[3] for c in candles], dtype=float)


def volumes(candles):
    return np.array([c[5] for c in candles], dtype=float)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    h, l, c = highs(candles), lows(candles), closes(candles)
    prev_close = c[:-1]
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)),
    )
    return float(np.mean(tr[-period:]))


def rsi(candles, period=14):
    c = closes(candles)
    if len(c) < period + 1:
        return None
    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def avg_volume(candles, period=20):
    v = volumes(candles)
    if len(v) < period:
        return float(np.mean(v)) if len(v) else 0.0
    return float(np.mean(v[-period:]))


def prior_swing_high(candles, lookback=20):
    h = highs(candles)[:-1][-lookback:]  # exclude today, if today's candle is included
    return float(np.max(h)) if len(h) else None


def prior_swing_low(candles, lookback=20):
    l = lows(candles)[:-1][-lookback:]
    return float(np.min(l)) if len(l) else None


def had_circuit_lock(candles, lookback=10) -> bool:
    """A day where High == Low means the stock was pinned at its
    exchange circuit limit for the whole session -- no trading range at
    all that day. Detectable from plain OHLC with no extra data: once a
    stock hits its circuit band, no trade can occur away from that price
    for the rest of the session, so high and low collapse to the same
    value. Checks the last `lookback` days."""
    recent = candles[-lookback:]
    for c in recent:
        h, l = c[2], c[3]
        if h > 0 and abs(h - l) / h < 0.0005:
            return True
    return False
