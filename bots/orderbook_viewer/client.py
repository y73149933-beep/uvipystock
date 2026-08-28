"""Reconnecting async WebSocket client for the exchange public channel.

Design goals
------------
* **Foundation for the market maker.** The MM bot will subclass this and
  add HMAC REST calls + quoting logic. Everything connection-related
  (reconnect, ping/pong, message dispatch) lives here so the MM only
  deals with strategy.
* **No auth needed for public channel.** The private channel will need
  an HMAC handshake — that will be a separate subclass or a mixin.
* **Backoff reconnect.** Network blips should not kill the bot.
* **Pluggable dispatch.** Subclasses override `on_snapshot` / `on_update`
  / `on_trade` / `on_raw` / `on_disconnect` / `on_connect`. Default
  implementations are no-ops, so subclasses only override what they need.

The exchange's WS contract (see ``backend/app/ws/public.py`` and
``backend/app/redis_client/pubsub.py``):

* URL: ``/api/v1/ws/orderbook/{symbol}``  (symbol may contain '/')
* On connect the server sends an ``orderbook_snapshot`` immediately.
* Then pubsub messages are forwarded verbatim:
    - ``orderbook_snapshot``  — full L2 book
    - ``orderbook_update``    — ``{"changes": [{"side","price","qty"}, ...]}``
    - ``trade``               — trade print (price, qty, side, ts)
    - ``stops_changed``       — signal to refetch stop ladder (ignored here)
    - ``pong``                — reply to a client ``{"action":"ping"}``
* Client may send ``{"action":"ping"}`` periodically to keep the
  connection alive through proxies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


# ─── Defaults ───────────────────────────────────────────────────────────────

DEFAULT_PING_INTERVAL = 30.0    # seconds between client-side pings
DEFAULT_RECONNECT_BASE = 1.0    # initial backoff
DEFAULT_RECONNECT_MAX = 30.0    # backoff cap
DEFAULT_RECONNECT_JITTER = 0.5  # +/- random jitter fraction


class ExchangeWSClient:
    """Reconnecting subscriber for the public orderbook WS channel.

    Lifecycle::

        client = ExchangeWSClient(url=..., symbol="BTC/USDT")
        await client.run()   # blocks until Ctrl+C / cancel

    Subclasses override the ``on_*`` callbacks. All callbacks are async
    or sync; if sync, they're called directly (no await).
    """

    def __init__(
        self,
        *,
        url: str,
        symbol: str,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        reconnect_base: float = DEFAULT_RECONNECT_BASE,
        reconnect_max: float = DEFAULT_RECONNECT_MAX,
    ) -> None:
        """
        :param url: Base WS URL, e.g. ``ws://localhost:8000``.
                    The full endpoint is built as ``{url}/api/v1/ws/orderbook/{symbol}``.
        :param symbol: Trading pair symbol, e.g. ``BTC/USDT``.
        :param ping_interval: Seconds between client-side pings. 0 disables.
        :param reconnect_base: Initial reconnect backoff (seconds).
        :param reconnect_max: Maximum backoff cap.
        """
        self.base_url = url.rstrip("/")
        self.symbol = symbol
        self.endpoint = f"{self.base_url}/api/v1/ws/orderbook/{symbol}"
        self.ping_interval = ping_interval
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max

        self._stop = asyncio.Event()
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._connect_count = 0

    # ─── Public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Block in a reconnect loop until `stop()` is called."""
        backoff = self.reconnect_base
        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                # Clean disconnect — reset backoff
                backoff = self.reconnect_base
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("WS connection to %s failed: %s", self.endpoint, e)
                await self.on_disconnect(reason=str(e))

            if self._stop.is_set():
                break

            # Exponential backoff with jitter
            sleep_for = min(backoff, self.reconnect_max)
            sleep_for += random.uniform(-sleep_for * DEFAULT_RECONNECT_JITTER,
                                         sleep_for * DEFAULT_RECONNECT_JITTER)
            sleep_for = max(0.1, sleep_for)
            logger.info("Reconnecting in %.1fs ...", sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.reconnect_max)

    def stop(self) -> None:
        """Signal the run loop to exit after the next iteration."""
        self._stop.set()
        if self._ws is not None:
            try:
                asyncio.create_task(self._ws.close())
            except Exception:
                pass

    # ─── Internal: connection lifecycle ─────────────────────────────────

    async def _connect_and_serve(self) -> None:
        logger.info("Connecting to %s ...", self.endpoint)
        async with websockets.connect(
            self.endpoint,
            ping_interval=None,        # we send our own application-level pings
            ping_timeout=None,
            close_timeout=5,
            max_size=2**22,            # 4 MiB — snapshots can be sizeable
            open_timeout=10,
        ) as ws:
            self._ws = ws
            self._connect_count += 1
            await self.on_connect(connect_count=self._connect_count)

            # Spawn ping task alongside the receive loop
            tasks: list[asyncio.Task] = []
            if self.ping_interval > 0:
                tasks.append(asyncio.create_task(self._ping_loop(ws)))

            try:
                await self._receive_loop(ws)
            finally:
                for t in tasks:
                    t.cancel()
                self._ws = None

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Non-JSON WS frame: %r", raw)
                continue

            await self._dispatch(msg)

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self.ping_interval)
                if self._stop.is_set() or ws.closed:
                    return
                try:
                    await ws.send(json.dumps({"action": "ping"}))
                except ConnectionClosed:
                    return
        except asyncio.CancelledError:
            return

    # ─── Internal: message dispatch ─────────────────────────────────────

    async def _dispatch(self, msg: dict) -> None:
        """Route an inbound message to the appropriate callback."""
        event = msg.get("event")
        # Always fire on_raw first (for debug / subclasses that want
        # to see everything)
        await self.on_raw(msg)

        if event == "orderbook_snapshot":
            await self.on_snapshot(
                symbol=msg.get("symbol", self.symbol),
                bids=msg.get("bids", []),
                asks=msg.get("asks", []),
                last_trade_price=msg.get("last_trade_price"),
                ts=msg.get("ts"),
            )
        elif event == "orderbook_update":
            await self.on_update(
                symbol=msg.get("symbol", self.symbol),
                changes=msg.get("changes", []),
                ts=msg.get("ts"),
            )
        elif event == "trade":
            await self.on_trade(
                symbol=msg.get("symbol", self.symbol),
                trade_id=msg.get("trade_id"),
                price=msg.get("price"),
                quantity=msg.get("quantity"),
                side=msg.get("side"),
                ts=msg.get("ts"),
            )
        elif event == "stops_changed":
            await self.on_stops_changed(symbol=msg.get("symbol", self.symbol))
        elif event == "pong":
            await self.on_pong(ts=msg.get("ts"))
        else:
            logger.debug("Unknown WS event %r: %s", event, msg)

    # ─── Callbacks (override in subclasses) ──────────────────────────────

    async def on_connect(self, *, connect_count: int) -> None:
        """Called after a successful WS handshake."""

    async def on_disconnect(self, *, reason: str) -> None:
        """Called when the WS drops, before backoff kicks in."""

    async def on_raw(self, msg: dict) -> None:
        """Called for every inbound message, before typed dispatch."""

    async def on_snapshot(
        self, *, symbol: str, bids: list, asks: list,
        last_trade_price: float | None, ts: int | None,
    ) -> None:
        """Full L2 book replacement."""

    async def on_update(
        self, *, symbol: str, changes: list[dict], ts: int | None,
    ) -> None:
        """Incremental level changes (qty=0 means remove level)."""

    async def on_trade(
        self, *, symbol: str, trade_id: int | None, price: float,
        quantity: float, side: str, ts: int | None,
    ) -> None:
        """Public trade print."""

    async def on_stops_changed(self, *, symbol: str) -> None:
        """Stop-ladder changed; MM should refetch via REST (ignored by viewer)."""

    async def on_pong(self, *, ts: int | None) -> None:
        """Server ponged our ping."""


__all__ = ["ExchangeWSClient"]
