"""Public WebSocket handler: /ws/orderbook/{symbol}.

On connect:
  1. Accept the connection.
  2. Register with ws_manager for `symbol`.
  3. Send an initial L2 snapshot from Redis.
  4. Listen for client messages (subscribe/unsubscribe/depth changes).
  5. The ws_manager's background subscriber pushes updates from Redis Pub/Sub.

No authentication required for public channels.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.redis_client import get_redis
from app.redis_client import orderbook, pubsub
from app.ws.manager import ws_manager

logger = logging.getLogger(__name__)


async def handle_public_orderbook(websocket: WebSocket, symbol: str) -> None:
    """Handle a public orderbook WS connection."""
    await ws_manager.connect_public(websocket, symbol)

    # Send initial snapshot
    try:
        redis = get_redis()
        snap = await orderbook.get_book_snapshot(redis, symbol, depth=20)
        last_price = await _get_last_trade_price(redis, symbol)
        await pubsub.publish_orderbook_snapshot(
            redis, symbol,
            bids=[[p, q] for p, q in snap["bids"]],
            asks=[[p, q] for p, q in snap["asks"]],
            last_trade_price=last_price,
        )
        # Note: the snapshot is published to Redis, which the ws_manager
        # subscriber picks up and broadcasts. This is slightly indirect but
        # ensures a single code path for snapshots + updates.
    except Exception as e:
        logger.warning("Failed to send initial snapshot for %s: %s", symbol, e)

    # Listen for client messages (depth changes, pings)
    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")
            if action == "ping":
                await websocket.send_json({"event": "pong", "ts": int(asyncio.get_event_loop().time())})
            # Other actions (subscribe, unsubscribe) are no-ops for now since
            # the connection is already tied to a single symbol.
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Public WS error for %s: %s", symbol, e)
    finally:
        await ws_manager.disconnect_public(websocket, symbol)


async def _get_last_trade_price(redis, symbol: str) -> float | None:
    """Get the last trade price from Redis (or None if no trades yet).

    This is a best-effort lookup — in production we'd cache the last price
    in a Redis string key updated on each trade.
    """
    # TODO: maintain last_price:{symbol} key updated by TradeService
    return None


__all__ = ["handle_public_orderbook"]
