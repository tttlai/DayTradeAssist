"""
Live tick streaming from Angel One's SmartWebSocketV2 (read-only market data).

Instead of repeatedly asking the REST API for candles, we hold one WebSocket
open and Angel pushes every price update for the subscribed watchlist stocks.
In "quote" mode each packet carries the last price, the exchange's own running
VWAP (average traded price), cumulative volume for the day and the day's
open/high/low -- everything the confirmation check needs, with no per-request
rate limit and no empty-candle failures.

This module also builds one-minute candles from the ticks and appends each
closed minute to candles_1m.jsonl (see store.py), which gives us real intraday
history to backtest against later -- the free NSE files only have daily bars.

Safety: the stream is an optimisation, never a dependency. It runs on a
background thread, reconnects with backoff, and reports is_healthy() so
src/main.py can fall back to REST polling whenever ticks stop or the
connection drops. Nothing here ever raises into the caller.

Angel sends prices in paise (1/100 rupee).
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from . import store, timeutil

EXCHANGE_NSE_CM = 1
MODE_QUOTE = 2
PAISE = 100.0
STALE_SECONDS = 90  # no tick at all for this long (market hours) => unhealthy
RECONNECT_BACKOFF_START = 5.0
RECONNECT_BACKOFF_MAX = 60.0
CANDLE_FILE = "candles_1m.jsonl"


@dataclass
class Snapshot:
    token: str
    ltp: float
    vwap: float | None
    volume: float  # cumulative shares traded today
    day_open: float | None
    day_high: float | None
    day_low: float | None
    prev_close: float | None
    received_at: datetime


def parse_tick(data: dict, now: datetime) -> Snapshot | None:
    """One SmartWebSocketV2 packet -> Snapshot, or None if it isn't a usable
    price update (control message, malformed, zero price)."""
    try:
        token = str(data["token"])
        ltp = data["last_traded_price"] / PAISE
    except (KeyError, TypeError):
        return None
    if not token or ltp <= 0:
        return None

    def price(key):
        value = data.get(key)
        return value / PAISE if isinstance(value, (int, float)) and value > 0 else None

    return Snapshot(
        token=token,
        ltp=ltp,
        vwap=price("average_traded_price"),
        volume=float(data.get("volume_trade_for_the_day") or 0),
        day_open=price("open_price_of_the_day"),
        day_high=price("high_price_of_the_day"),
        day_low=price("low_price_of_the_day"),
        prev_close=price("closed_price"),
        received_at=now,
    )


class StreamClient:
    def __init__(self, api_key, client_code, auth_token, feed_token, symbols: dict[str, str],
                 ws_factory=None, backoff_start=RECONNECT_BACKOFF_START):
        """symbols: {instrument token: trading symbol} to subscribe to."""
        self.api_key, self.client_code = api_key, client_code
        self.auth_token, self.feed_token = auth_token, feed_token
        self.symbols = dict(symbols)
        self._ws_factory = ws_factory or self._default_ws_factory
        self._backoff_start = backoff_start

        self._lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._bars: dict[str, dict] = {}
        self._triggers: dict[str, list] = {}  # token -> [direction, level, crossed]
        self._last_tick_at: datetime | None = None
        self._connected = False
        self._got_tick_this_connection = False

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self.wake = threading.Event()  # set when a watched stock newly crosses its trigger
        self.last_error: str | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="angel-stream", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close_connection()
            except Exception as e:
                print(f"[stream] close raised {e.__class__.__name__}: {e}")
        if self._thread:
            self._thread.join(timeout=5)
        self.flush_candles()
        self._connected = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _default_ws_factory(self):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        import logzero

        # max_retry_attempt=0: let our own supervisor loop handle reconnects
        # (the SDK's built-in retry recurses inside its own thread).
        ws = SmartWebSocketV2(self.auth_token, self.api_key, self.client_code, self.feed_token, max_retry_attempt=0)
        # The SDK logs every ping and message at INFO to stderr and a file.
        logzero.loglevel(logging.WARNING)
        logzero.logfile(None)
        ws.input_request_dict = {}  # it's a class-level dict shared across instances
        return ws

    def _run(self):
        backoff = self._backoff_start
        while not self._stop.is_set():
            self._got_tick_this_connection = False
            try:
                ws = self._ws_factory()
                self._ws = ws
                ws.on_open = self._on_open
                ws.on_data = self._on_data
                ws.on_close = self._on_close
                ws.on_error = self._on_error
                ws.connect()  # blocks until the connection closes
            except Exception as e:
                self.last_error = f"{e.__class__.__name__}: {str(e)[:120]}"
                print(f"[stream] connection failed: {self.last_error}")
            self._connected = False
            if self._stop.is_set():
                break
            backoff = self._backoff_start if self._got_tick_this_connection else min(backoff * 2, RECONNECT_BACKOFF_MAX)
            print(f"[stream] disconnected; reconnecting in {backoff:.0f}s")
            self._stop.wait(backoff)

    # ---- websocket callbacks (run on the stream thread) ------------------
    def _on_open(self, wsapp=None):
        try:
            self._ws.subscribe(
                "dtassist001", MODE_QUOTE,
                [{"exchangeType": EXCHANGE_NSE_CM, "tokens": list(self.symbols)}],
            )
            self._connected = True
            print(f"[stream] connected, subscribed to {len(self.symbols)} stocks")
        except Exception as e:
            self.last_error = f"subscribe failed: {e.__class__.__name__}: {str(e)[:120]}"
            print(f"[stream] {self.last_error}")

    def _on_close(self, wsapp=None, *args):
        self._connected = False

    def _on_error(self, *args):
        self.last_error = " ".join(str(a)[:120] for a in args) or "unknown error"
        print(f"[stream] error: {self.last_error}")

    def _on_data(self, wsapp, data):
        try:
            now = timeutil.now_ist()
            snap = parse_tick(data, now)
            if snap is None:
                return
            with self._lock:
                self._snapshots[snap.token] = snap
                self._last_tick_at = now
                self._update_bar(snap)
                self._check_trigger(snap)
            self._got_tick_this_connection = True
        except Exception as e:
            print(f"[stream] tick handling raised {e.__class__.__name__}: {e}")

    # ---- reading state (main thread) -------------------------------------
    def is_healthy(self, now: datetime | None = None) -> bool:
        now = now or timeutil.now_ist()
        with self._lock:
            last = self._last_tick_at
        return self._connected and last is not None and (now - last).total_seconds() <= STALE_SECONDS

    def snapshot(self, token: str, max_age: float = STALE_SECONDS, now: datetime | None = None) -> Snapshot | None:
        now = now or timeutil.now_ist()
        with self._lock:
            snap = self._snapshots.get(str(token))
        if snap is None or (now - snap.received_at).total_seconds() > max_age:
            return None
        return snap

    def set_triggers(self, triggers: dict[str, tuple[str, float]]):
        """{token: (direction, entry level)} for the stocks still waiting to
        trigger. The loop is woken the moment one newly crosses."""
        with self._lock:
            for token in list(self._triggers):
                if token not in triggers:
                    del self._triggers[token]
            for token, (direction, level) in triggers.items():
                if token in self._triggers:
                    self._triggers[token][0:2] = [direction, level]
                else:
                    self._triggers[token] = [direction, level, False]

    def _check_trigger(self, snap: Snapshot):
        trig = self._triggers.get(snap.token)
        if trig is None:
            return
        direction, level, was_crossed = trig
        crossed = snap.ltp >= level if direction == "LONG" else snap.ltp <= level
        trig[2] = crossed
        if crossed and not was_crossed:
            self.wake.set()

    # ---- one-minute candles ----------------------------------------------
    def _update_bar(self, snap: Snapshot):
        minute = snap.received_at.strftime("%H:%M")
        bar = self._bars.get(snap.token)
        carry_volume = None
        if bar is not None and bar["t"] != minute:
            self._emit(snap.token, bar)
            carry_volume = bar["last_vol"]
            bar = None
        if bar is None:
            self._bars[snap.token] = {
                "t": minute, "date": snap.received_at.strftime("%Y-%m-%d"),
                "o": snap.ltp, "h": snap.ltp, "l": snap.ltp, "c": snap.ltp,
                "base_vol": carry_volume if carry_volume is not None else snap.volume,
                "last_vol": snap.volume,
                "partial": carry_volume is None,  # joined mid-minute: its volume undercounts
            }
            return
        bar["h"] = max(bar["h"], snap.ltp)
        bar["l"] = min(bar["l"], snap.ltp)
        bar["c"] = snap.ltp
        bar["last_vol"] = snap.volume

    def _emit(self, token: str, bar: dict):
        store.append_record(
            CANDLE_FILE,
            {
                "date": bar["date"], "symbol": self.symbols.get(token, token), "t": bar["t"],
                "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
                "v": max(0.0, bar["last_vol"] - bar["base_vol"]), "partial": bar["partial"],
            },
        )

    def flush_candles(self):
        """Write out the in-progress minute for every stock (end of day/stop)."""
        with self._lock:
            for token, bar in self._bars.items():
                self._emit(token, bar)
            self._bars.clear()
