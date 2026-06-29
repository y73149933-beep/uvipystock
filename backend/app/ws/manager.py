"""WebSocket connection manager.

Maintains two registries:
  * Per-symbol public connections (for orderbook/trades broadcasts)
  * Per-user private connections (for order/balance updates)

The matching worker and services publish events to Redis Pub/Sub; the
WS gateway subscribes to those channels and fans out to connected clients.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from app.redis_client import get_redis
from app.redis_client import pubsub

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages active WS connections and Redis Pub/Sub fan-out."""

    def __init__(self) -> None:
        # symbol → set of WebSocket connections
        self._public_connections: dict[str, set[WebSocket]] = {}
        # user_id → set of WebSocket connections
        self._private_connections: dict[int, set[WebSocket]] = {}
        # Background subscriber tasks
        self._subscriber_tasks: list[asyncio.Task] = []
        self._running = False
        self._lock = asyncio.Lock()

    # ─── Connection management ──────────────────────────────────────────────

    async def connect_public(self, websocket: WebSocket, symbol: str) -> None:
        """Accept a public WS connection and register it for `symbol`."""
        await websocket.accept()
        async with self._lock:
            if symbol not in self._public_connections:
                self._public_connections[symbol] = set()
            self._public_connections[symbol].add(websocket)
        logger.info("Public WS connected for %s (total: %d)",
                    symbol, len(self._public_connections.get(symbol, set())))

    async def disconnect_public(self, websocket: WebSocket, symbol: str) -> None:
        """Remove a public WS connection."""
        async with self._lock:
            if symbol in self._public_connections:
                self._public_connections[symbol].discard(websocket)
                if not self._public_connections[symbol]:
                    del self._public_connections[symbol]

    async def connect_private(self, websocket: WebSocket, user_id: int) -> None:
        """Register a private WS connection for `user_id`.

        NOTE: The WebSocket must already be accepted by the caller. This method
        only registers the connection — it does NOT call websocket.accept()
        (which would raise RuntimeError if called twice).
        """
        async with self._lock:
            if user_id not in self._private_connections:
                self._private_connections[user_id] = set()
            self._private_connections[user_id].add(websocket)
        logger.info("Private WS connected for user %d (total: %d)",
                    user_id, len(self._private_connections.get(user_id, set())))

    async def disconnect_private(self, websocket: WebSocket, user_id: int) -> None:
        """Remove a private WS connection."""
        async with self._lock:
            if user_id in self._private_connections:
                self._private_connections[user_id].discard(websocket)
                if not self._private_connections[user_id]:
                    del self._private_connections[user_id]

    # ─── Broadcasting ───────────────────────────────────────────────────────

    async def broadcast_public(self, symbol: str, message: dict) -> None:
        """Send a message to all public connections for `symbol`."""
        conns = self._public_connections.get(symbol, set()).copy()
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        # Clean up dead connections
        for ws in dead:
            await self.disconnect_public(ws, symbol)

    async def broadcast_private(self, user_id: int, message: dict) -> None:
        """Send a message to all private connections for `user_id`."""
        conns = self._private_connections.get(user_id, set()).copy()
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect_private(ws, user_id)

    # ─── Redis Pub/Sub fan-out ──────────────────────────────────────────────

    async def start_subscribers(self, symbols: list[str]) -> None:
        """Start Redis Pub/Sub listeners for the given symbols + private channels.

        For each symbol, subscribes to:
          * pub:orderbook:{symbol}  → broadcast to public connections
          * pub:trades:{symbol}     → broadcast to public connections

        Private channels (pub:orders:{uid}, pub:balances:{uid}) are subscribed
        on-demand when a user connects, since we don't know user IDs upfront.
        """
        if self._running:
            return
        self._running = True

        for symbol in symbols:
            task = asyncio.create_task(self._public_subscriber(symbol))
            self._subscriber_tasks.append(task)

        logger.info("WS subscribers started for symbols: %s", symbols)

    async def stop_subscribers(self) -> None:
        """Stop all background subscriber tasks."""
        self._running = False
        for task in self._subscriber_tasks:
            task.cancel()
        await asyncio.gather(*self._subscriber_tasks, return_exceptions=True)
        self._subscriber_tasks.clear()

    async def _public_subscriber(self, symbol: str) -> None:
        """Listen to pub:orderbook:{symbol} and pub:trades:{symbol}.

        Uses a dedicated Redis connection (via pubsub.subscribe()) to avoid
        blocking-command interference with the shared pool. Retries on
        connection errors with a 2s backoff.
        """
        channels = [
            pubsub.orderbook_channel(symbol),
            pubsub.trades_channel(symbol),
        ]

        while self._running:
            pubsub_obj = None
            try:
                pubsub_obj = await pubsub.subscribe(*channels)
                logger.info("WS public subscriber connected for %s", symbol)

                async for msg in pubsub.iter_messages(pubsub_obj):
                    if not self._running:
                        break
                    await self.broadcast_public(symbol, msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Public subscriber for %s error: %s — retrying in 2s", symbol, e)
                await asyncio.sleep(2.0)
            finally:
                if pubsub_obj is not None:
                    try:
                        await pubsub.unsubscribe(pubsub_obj)
                    except Exception:
                        pass

    async def subscribe_private_channel(self, user_id: int) -> asyncio.Task:
        """Start listening to a user's private channels.

        Returns the background task (caller should store it for cleanup).
        """
        task = asyncio.create_task(self._private_subscriber(user_id))
        return task

    async def _private_subscriber(self, user_id: int) -> None:
        """Listen to pub:orders:{uid}, pub:balances:{uid}, pub:bulk:{uid}.

        Uses a dedicated Redis connection (via pubsub.subscribe()) with retry
        logic on connection errors.
        """
        channels = [
            pubsub.orders_channel(user_id),
            pubsub.balances_channel(user_id),
            pubsub.bulk_channel(user_id),
        ]

        while self._running:
            pubsub_obj = None
            try:
                pubsub_obj = await pubsub.subscribe(*channels)
                logger.info("WS private subscriber connected for user %d", user_id)

                async for msg in pubsub.iter_messages(pubsub_obj):
                    await self.broadcast_private(user_id, msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Private subscriber for user %d error: %s — retrying in 2s", user_id, e)
                await asyncio.sleep(2.0)
            finally:
                if pubsub_obj is not None:
                    try:
                        await pubsub.unsubscribe(pubsub_obj)
                    except Exception:
                        pass

    # ─── Stats ──────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Return connection counts for monitoring."""
        return {
            "public_connections": {
                sym: len(conns) for sym, conns in self._public_connections.items()
            },
            "private_connections": {
                uid: len(conns) for uid, conns in self._private_connections.items()
            },
            "total_public": sum(len(c) for c in self._public_connections.values()),
            "total_private": sum(len(c) for c in self._private_connections.values()),
        }


# ─── Singleton ───────────────────────────────────────────────────────────────

ws_manager = WebSocketManager()


__all__ = ["WebSocketManager", "ws_manager"]
