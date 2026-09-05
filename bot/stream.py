"""
Real-time market and account data over websockets.

Runs in a background thread with its own asyncio loop and pushes typed events
onto a thread-safe queue. The trading logic stays synchronous, so the risk and
order code that has tests around it did not have to be rewritten to be async.

Streams consumed:
  <symbol>@markPrice@1s   -- one price per second, drives proximity alerts
  <symbol>@kline_<iv>     -- bar closes, drives strategy decisions
  <listenKey>             -- ORDER_TRADE_UPDATE / ACCOUNT_UPDATE, real fills
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("stream")

# websockets is imported lazily inside start(), so the event dataclasses below
# stay importable -- and `realtime: false` genuinely works -- on a host that
# does not have it installed. (QA F4)

WS_LIVE = "wss://fstream.binance.com"

# Matches the REST change: demo-fstream is the documented testnet stream host;
# stream.binancefuture.com is the legacy alias. Both serve today.
WS_TESTNET = "wss://demo-fstream.binance.com"
WS_TESTNET_LEGACY = "wss://stream.binancefuture.com"

KEEPALIVE_SECONDS = 1800          # listenKey expires after 60 min; refresh at 30
STALE_SECONDS = 45                # a connected-but-silent socket is a dead socket


@dataclass
class Tick:
    symbol: str
    mark_price: float
    event_time: int


@dataclass
class BarClosed:
    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderUpdate:
    symbol: str
    client_order_id: str
    side: str
    status: str            # NEW | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED
    order_type: str
    last_filled_qty: float
    cumulative_qty: float
    avg_price: float
    realized_pnl: float
    raw: dict[str, Any]

    @property
    def is_fill(self) -> bool:
        return self.status in ("FILLED", "PARTIALLY_FILLED")


@dataclass
class Disconnected:
    reason: str


@dataclass
class StreamStale:
    """Connected, handshake fine, but no data. Seen on fstream.binance.com from
    some networks -- the socket looks healthy and delivers nothing, which is
    worse than a clean drop because nothing errors."""
    seconds_silent: float


class MarketStream:
    """Start with .start(), read with .get(timeout=...), stop with .stop()."""

    def __init__(self, symbol: str, interval: str, api=None, testnet: bool = True):
        self.symbol = symbol.lower()
        self.interval = interval
        self.api = api
        import os
        self.base = (os.environ.get("BINANCE_TESTNET_WS", WS_TESTNET)
                     if testnet else WS_LIVE).rstrip("/")
        self.q: queue.Queue = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listen_key: str | None = None
        self._last_keepalive = 0.0
        self._last_message = 0.0
        self.connected = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        global websockets
        try:
            import websockets                      # noqa: PLW0603
        except ImportError as e:
            raise RuntimeError(
                "realtime: true needs the websockets package -- "
                "pip install websockets, or set realtime: false in config.yaml"
            ) from e

        if self.api is not None:
            try:
                self._listen_key = self.api.listen_key()
                self._last_keepalive = time.time()
                log.info("user-data stream opened")
            except Exception as e:
                log.warning("could not open user-data stream (%s); "
                            "market data only, fills will be polled", e)
        self._thread = threading.Thread(target=self._run, name="ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._listen_key and self.api is not None:
            try:
                self.api.close_listen_key()
            except Exception:
                pass

    @property
    def seconds_since_message(self) -> float:
        return time.time() - self._last_message if self._last_message else 0.0

    def get(self, timeout: float = 1.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    # --------------------------------------------------------------- thread
    def _url(self) -> str:
        streams = [f"{self.symbol}@markPrice@1s", f"{self.symbol}@kline_{self.interval}"]
        if self._listen_key:
            streams.append(self._listen_key)
        return f"{self.base}/stream?streams={'/'.join(streams)}"

    def _run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._url(), ping_interval=20,
                                              ping_timeout=20, close_timeout=5) as ws:
                    log.info("websocket connected: %s", self.symbol.upper())
                    self.connected.set()
                    backoff = 1
                    self._last_message = time.time()
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        except asyncio.TimeoutError:
                            silent = time.time() - self._last_message
                            if silent > STALE_SECONDS:
                                self._put(StreamStale(silent))
                                raise ConnectionError(
                                    f"stream silent for {silent:.0f}s -- reconnecting")
                            await ws.ping()
                            continue
                        self._last_message = time.time()
                        self._dispatch(json.loads(raw))
                        self._maybe_keepalive()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected.clear()
                self._put(Disconnected(str(e)))
                log.warning("websocket dropped (%s); reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.connected.clear()

    def _maybe_keepalive(self) -> None:
        if not self._listen_key or self.api is None:
            return
        if time.time() - self._last_keepalive < KEEPALIVE_SECONDS:
            return
        try:
            self.api.keepalive_listen_key()
            self._last_keepalive = time.time()
            log.debug("listenKey refreshed")
        except Exception as e:
            log.warning("listenKey keepalive failed: %s", e)

    def _put(self, item) -> None:
        try:
            self.q.put_nowait(item)
        except queue.Full:
            log.warning("event queue full; dropping oldest")
            try:
                self.q.get_nowait()
                self.q.put_nowait(item)
            except queue.Empty:
                pass

    def _dispatch(self, msg: dict) -> None:
        data = msg.get("data", msg)
        etype = data.get("e")

        if etype == "markPriceUpdate":
            self._put(Tick(data["s"], float(data["p"]), int(data["E"])))

        elif etype == "kline":
            k = data["k"]
            if k.get("x"):                     # only closed bars
                self._put(BarClosed(data["s"], k["i"], int(k["t"]), float(k["o"]),
                                    float(k["h"]), float(k["l"]), float(k["c"]),
                                    float(k["v"])))

        elif etype == "ORDER_TRADE_UPDATE":
            o = data["o"]
            self._put(OrderUpdate(
                symbol=o["s"], client_order_id=o.get("c", ""), side=o["S"],
                status=o["X"], order_type=o["ot"], last_filled_qty=float(o.get("l", 0)),
                cumulative_qty=float(o.get("z", 0)), avg_price=float(o.get("ap", 0)),
                realized_pnl=float(o.get("rp", 0)),
                raw=o))
