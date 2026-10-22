"""
Observation-only review of what a confirmed trade's price did AFTER entry.

Nothing here changes any decision or sends any order. After the close, each
confirmed trade is replayed against the day's candles (the one-minute candles
the live stream recorded, or the 15-minute candles from REST as a fallback)
and summarised: how far price went for and against the position, and when
the events that might signal a failing setup first happened (back through the
trigger, VWAP against the position, volume fading, stop/target levels
touched). The records go to trade_paths.jsonl so exit rules can be replayed
and compared against "hold to 15:15" once there are enough trades.
"""
from . import config, timeutil

MARKET_MINUTES = 375
FAVORABLE_ATRS = (0.5, 1.0, 2.0)
ADVERSE_ATRS = (0.5, 1.0)
MIN_STREAM_CANDLES = 5  # fewer recorded candles than this -> use REST instead


def candles_from_store(records: list[dict], date: str, symbol: str) -> list[dict]:
    """The stream's recorded one-minute candles for one stock and day."""
    rows = [
        {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r.get("v") or 0.0}
        for r in records
        if r.get("date") == date and r.get("symbol") == symbol and r.get("t")
    ]
    return sorted(rows, key=lambda c: c["t"])


def candles_from_rest(intraday) -> list[dict]:
    """Angel's [timestamp, o, h, l, c, v] candles in the same shape."""
    rows = [
        {"t": timeutil.candle_time_str(c[0]), "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5] or 0.0}
        for c in intraday or []
    ]
    return sorted(rows, key=lambda c: c["t"])


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def analyze(trade: dict, candles: list[dict], source: str, resolution_minutes: int = 1) -> dict:
    """trade: symbol, direction, entered_at (HH:MM), entry, stop_loss, target,
    trigger_level, atr, avg_volume_20. Returns a JSON-able record; when there
    are no usable candles it still returns one, flagged source="none"."""
    direction = trade["direction"]
    entry = trade["entry"]
    atr = trade.get("atr") or abs(entry - trade["stop_loss"]) or 1.0
    sign = 1 if direction == "LONG" else -1
    record = {
        "symbol": trade["symbol"], "direction": direction, "entered_at": trade.get("entered_at"),
        "entry": entry, "trigger": trade.get("trigger_level"), "atr": round(atr, 2),
        "stop_loss": trade["stop_loss"], "target": trade["target"], "source": source,
        "resolution_minutes": resolution_minutes,
    }

    entered_at = trade.get("entered_at") or "00:00"
    after = [c for c in candles if c["t"] >= entered_at]
    if not candles or not after:
        return {**record, "source": "none", "n_candles": 0, "events": []}

    # Running VWAP and volume pace need the whole session so far, not just
    # the candles after entry.
    cum_pv = cum_v = 0.0
    session = {}
    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3.0
        cum_pv += typical * c["v"]
        cum_v += c["v"]
        elapsed = max(1, _minutes(c["t"]) + resolution_minutes - (9 * 60 + 15))
        expected = (trade.get("avg_volume_20") or 0) * min(1.0, elapsed / MARKET_MINUTES)
        session[c["t"]] = {
            "vwap": cum_pv / cum_v if cum_v > 0 else None,
            "pace": cum_v / expected if expected > 0 else None,
        }

    events, seen = [], set()
    counts = {"below_trigger": 0, "vwap_against": 0}

    def log(name, c, price):
        if name not in seen:
            seen.add(name)
            events.append({"event": name, "t": c["t"], "price": round(price, 2)})

    mfe = mae = 0.0
    mfe_t = mae_t = None
    trigger = trade.get("trigger_level")
    for c in after:
        favorable = ((c["h"] - entry) if direction == "LONG" else (entry - c["l"])) / atr
        adverse = ((entry - c["l"]) if direction == "LONG" else (c["h"] - entry)) / atr
        if favorable > mfe:
            mfe, mfe_t = favorable, c["t"]
        if adverse > mae:
            mae, mae_t = adverse, c["t"]
        for threshold in FAVORABLE_ATRS:
            if favorable >= threshold:
                log(f"favorable_{threshold:g}atr", c, c["c"])
        for threshold in ADVERSE_ATRS:
            if adverse >= threshold:
                log(f"adverse_{threshold:g}atr", c, c["c"])

        stop_hit = c["l"] <= trade["stop_loss"] if direction == "LONG" else c["h"] >= trade["stop_loss"]
        target_hit = c["h"] >= trade["target"] if direction == "LONG" else c["l"] <= trade["target"]
        if stop_hit:
            log("stop_level_touched", c, trade["stop_loss"])
        if target_hit:
            log("target_level_touched", c, trade["target"])

        if trigger is not None and (c["c"] < trigger if direction == "LONG" else c["c"] > trigger):
            counts["below_trigger"] += 1
            log("back_through_trigger", c, c["c"])
        info = session[c["t"]]
        if info["vwap"] is not None and (c["c"] < info["vwap"] if direction == "LONG" else c["c"] > info["vwap"]):
            counts["vwap_against"] += 1
            log("vwap_against", c, c["c"])
        if info["pace"] is not None and info["pace"] < 1.0:
            log("volume_fade", c, c["c"])

    exits = [c for c in after if c["t"] <= config.SQUARE_OFF_TIME] or after
    last = exits[-1]
    return {
        **record,
        "n_candles": len(after),
        "mfe_atr": round(mfe, 2), "mfe_t": mfe_t,
        "mae_atr": round(mae, 2), "mae_t": mae_t,
        "exit_t": last["t"], "exit_close": last["c"],
        "exit_atr": round(sign * (last["c"] - entry) / atr, 2),
        "minutes_back_through_trigger": counts["below_trigger"] * resolution_minutes,
        "minutes_vwap_against": counts["vwap_against"] * resolution_minutes,
        "events": events,
    }


def review_trade(trade: dict, stream_records: list[dict], date: str, rest_intraday) -> dict:
    """Pick the best candle source available and analyze. Never raises."""
    try:
        minute = candles_from_store(stream_records, date, trade["symbol"])
        after_entry = [c for c in minute if c["t"] >= (trade.get("entered_at") or "00:00")]
        if len(after_entry) >= MIN_STREAM_CANDLES:
            return analyze(trade, minute, "stream_1m", 1)
        coarse = candles_from_rest(rest_intraday)
        if coarse:
            return analyze(trade, coarse, "rest_15m", 15)
        return analyze(trade, [], "none")
    except Exception as e:
        print(f"[tradepath] {trade.get('symbol')}: analysis failed: {e.__class__.__name__}: {e}")
        return {"symbol": trade.get("symbol"), "source": "none", "n_candles": 0, "events": [], "error": str(e)[:120]}
