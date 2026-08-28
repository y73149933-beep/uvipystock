"""Reconnecting async WebSocket client for the exchange PRIVATE channel.

Auth flow (Option B from `backend/app/ws/private.py`)
------------------------------------------------------
1. Connect to ``/api/v1/ws/private``.
2. Server sends ``{"event":"auth_required"}``.
3. Client sends within 5 seconds::

       {"action":"auth", "api_key": "...", "timestamp": <unix_seconds>,
        "signature": "<compute_ws_signature(secret, api_key, timestamp)>"}

4. Server replies with ``{"event":"auth_ok","user_id":...}`` on success.
5. Thereafter, the server forwards private pubsub events verbatim:
   - ``event=order``   — order lifecycle updates (placed/filled/canceled/...)
   - ``event=balance`` — balance updates (total / locked / available)
   - ``event=bulk_result``  — bulk operation results
   - ``event=sl_tp_activated`` — SL/TP children activated
   - ``event=pong``    — reply to client ping

We reuse the same reconnect/backoff machinery as `ExchangeWSClient`
but the endpoint is fixed and the dispatch table is different.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from .hmac_rest_client import ExchangeAuth, compute_ws_signature

logger = logging.getLogger(__name__)


DEFAULT_PING_INTERVAL = 30.0
DEFAULT_RECONNECT_BASE = 1.0
DEFAULT_RECONNECT_MAX = 30.0
DEFAULT_RECONNECT_JITTER = 0.5
AUTH_TIMEOUT = 6.0  # server allows 5s; give ourselves a tiny buffer


class PrivateChannelClient:
    """Reconnecting subscriber for the private WS channel.

    Subclasses override the typed callbacks (``on_auth_ok``,
    ``on_order_update``, ``on_balance_update``, …) to react to events.

    Usage::

        class MyBot(PrivateChannelClient):
            async def on_balance_update(self, *, asset, total, locked, available, ...):
                ...

        bot = MyBot(url="ws://localhost:8000", auth=my_auth)
        await bot.run()
    """

    def __init__(
        self,
        *,
        url: str,
        auth: ExchangeAuth,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        reconnect_base: float = DEFAULT_RECONNECT_BASE,
        reconnect_max: float = DEFAULT_RECONNECT_MAX,
    ) -> None:
        self.base_url = url.rstrip("/")
        self.endpoint = f"{self.base_url}/api/v1/ws/private"
        self.auth = auth
        self.ping_interval = ping_interval
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max

        self._stop = asyncio.Event()
        self._ws: Any = None
        self._connect_count = 0

    # ─── Public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Block in a reconnect loop until `stop()` is called."""
        backoff = self.reconnect_base
        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                backoff = self.reconnect_base
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Private WS connection failed: %s", e)
                await self.on_disconnect(reason=str(e))

            if self._stop.is_set():
                break

            sleep_for = min(backoff, self.reconnect_max)
            import random
            sleep_for += random.uniform(-sleep_for * DEFAULT_RECONNECT_JITTER,
                                         sleep_for * DEFAULT_RECONNECT_JITTER)
            sleep_for = max(0.1, sleep_for)
            logger.info("Private WS reconnecting in %.1fs ...", sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.reconnect_max)

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                asyncio.create_task(self._ws.close())
            except Exception:
                pass

    # ─── Internal: connection lifecycle ─────────────────────────────────

    async def _connect_and_serve(self) -> None:
        logger.info("Connecting to private WS %s ...", self.endpoint)
        async with websockets.connect(
            self.endpoint,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            max_size=2**22,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            self._connect_count += 1
            await self.on_connect(connect_count=self._connect_count)

            # Step 1: wait for auth_required (or assume header-auth worked)
            # Step 2: send auth message
            await self._do_handshake(ws)

            # Spawn ping task
            tasks: list[asyncio.Task] = []
            if self.ping_interval > 0:
                tasks.append(asyncio.create_task(self._ping_loop(ws)))

            try:
                await self._receive_loop(ws)
            finally:
                for t in tasks:
                    t.cancel()
                self._ws = None

    async def _do_handshake(self, ws: Any) -> None:
        """Send the auth message and wait for ``auth_ok``."""
        # Wait briefly for the auth_required frame (don't fail hard if
        # the server skips it — some deployments use header auth).
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=2.0)
            try:
                msg = json.loads(first) if isinstance(first, (str, bytes)) else first
                await self._dispatch(msg)
                if isinstance(msg, dict) and msg.get("event") == "auth_ok":
                    return  # header auth succeeded already
            except json.JSONDecodeError:
                pass
        except asyncio.TimeoutError:
            pass  # server didn't send auth_required — proceed anyway

        # Send our auth message
        timestamp = int(time.time())
        signature = compute_ws_signature(
            secret=self.auth.api_secret,
            api_key=self.auth.api_key,
            timestamp=timestamp,
        )
        auth_msg = {
            "action": "auth",
            "api_key": self.auth.api_key,
            "timestamp": timestamp,
            "signature": signature,
        }
        await ws.send(json.dumps(auth_msg))
        logger.debug("Sent auth message for api_key=%s...", self.auth.api_key[:8])

        # Wait for auth_ok / auth_failed / auth_timeout
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
            except asyncio.TimeoutError:
                raise ConnectionError("Auth handshake timed out waiting for auth_ok")

            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except json.JSONDecodeError:
                continue

            event = msg.get("event") if isinstance(msg, dict) else None
            if event == "auth_ok":
                await self.on_auth_ok(user_id=msg.get("user_id"))
                return
            if event in ("auth_failed", "auth_timeout"):
                raise ConnectionError(
                    f"Private WS auth failed: {msg.get('message', event)}"
                )
            # Some other event arrived during handshake — dispatch and keep waiting
            await self._dispatch(msg)

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Non-JSON private WS frame: %r", raw)
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
        await self.on_raw(msg)
        event = msg.get("event")

        if event == "auth_ok":
            await self.on_auth_ok(user_id=msg.get("user_id"))
        elif event == "auth_required":
            # Informational — we already send auth unconditionally
            await self.on_auth_required()
        elif event == "auth_failed":
            await self.on_auth_failed(message=msg.get("message", ""))
        elif event == "auth_timeout":
            await self.on_auth_failed(message="auth_timeout")
        elif event == "order":
            await self.on_order_update(
                order_id=msg.get("order_id"),
                symbol=msg.get("symbol"),
                side=msg.get("side"),
                type=msg.get("type"),
                status=msg.get("status"),
                status_event=msg.get("status_event"),
                price=msg.get("price"),
                quantity=msg.get("quantity"),
                filled_quantity=msg.get("filled_quantity"),
                remaining_quantity=msg.get("remaining_quantity"),
                avg_fill_price=msg.get("avg_fill_price"),
                last_trade_qty=msg.get("last_trade_qty"),
                last_trade_price=msg.get("last_trade_price"),
                client_order_id=msg.get("client_order_id"),
                bulk_id=msg.get("bulk_id"),
            )
        elif event == "balance":
            await self.on_balance_update(
                asset=msg.get("asset"),
                total=msg.get("total"),
                locked=msg.get("locked"),
                available=msg.get("available"),
                change=msg.get("change"),
                reason=msg.get("reason"),
                order_id=msg.get("order_id"),
            )
        elif event == "bulk_result":
            await self.on_bulk_result(
                bulk_id=msg.get("bulk_id"),
                action=msg.get("action"),
                total=msg.get("total"),
                succeeded=msg.get("succeeded"),
                failed=msg.get("failed", []),
            )
        elif event == "sl_tp_activated":
            await self.on_sl_tp_activated(
                parent_order_id=msg.get("parent_order_id"),
                sl_order_id=msg.get("sl_order_id"),
                tp_order_id=msg.get("tp_order_id"),
            )
        elif event == "pong":
            await self.on_pong(ts=msg.get("ts"))
        else:
            logger.debug("Unknown private WS event %r: %s", event, msg)

    # ─── Callbacks (override in subclasses) ──────────────────────────────

    async def on_connect(self, *, connect_count: int) -> None:
        """Called after the WS handshake (before auth)."""

    async def on_disconnect(self, *, reason: str) -> None:
        """Called when the WS drops, before backoff kicks in."""

    async def on_raw(self, msg: dict) -> None:
        """Called for every inbound message, before typed dispatch."""

    async def on_auth_required(self) -> None:
        """Server asked for auth (informational)."""

    async def on_auth_ok(self, *, user_id: int | None) -> None:
        """Auth succeeded — private events will start arriving."""

    async def on_auth_failed(self, *, message: str) -> None:
        """Auth failed — the connection will be closed by the server."""

    async def on_order_update(self, **kwargs) -> None:
        """Order lifecycle event (placed/partially_filled/filled/canceled/...)."""

    async def on_balance_update(
        self, *, asset: str, total: float, locked: float, available: float,
        change: float | None = None, reason: str | None = None,
        order_id: int | None = None,
    ) -> None:
        """Balance update — when an asset's total/locked/available changes."""

    async def on_bulk_result(
        self, *, bulk_id: str, action: str, total: int,
        succeeded: int, failed: list[dict],
    ) -> None:
        """Result of a bulk place/cancel operation."""

    async def on_sl_tp_activated(
        self, *, parent_order_id: int | None,
        sl_order_id: int | None, tp_order_id: int | None,
    ) -> None:
        """SL/TP children activated after parent fill."""

    async def on_pong(self, *, ts: int | None) -> None:
        """Server ponged our ping."""


__all__ = ["PrivateChannelClient"]
