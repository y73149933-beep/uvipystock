"""Public WebSocket handler: /ws/orderbook/{symbol}.

Each client gets its OWN Pub/Sub subscription for the symbol. This ensures
real-time updates (orderbook deltas + trade prints) work for ALL symbols,
including new pairs created via admin after backend startup.

Flow:
  1. Accept connection
  2. Send initial snapshot DIRECTLY to client
  3. Subscribe to pub:orderbook:{symbol} + pub:trades:{symbol}
  4. Forward Pub/Sub messages to client + handle pings
  5. On disconnect: close Pub/Sub
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.redis_client import get_redis
from app.redis_client import orderbook, pubsub
from app.ws.manager import ws_manager

logger = logging.getLogger(__name__)


async def handle_public_orderbook(websocket: WebSocket, symbol: str) -> None:
    """Handle a public orderbook WS connection with per-client Pub/Sub."""
    await ws_manager.connect_public(websocket, symbol)

    # Send initial snapshot DIRECTLY to this client
    try:
        redis = get_redis()
        snap = await orderbook.get_book_snapshot(redis, symbol, depth=20)
        snapshot_msg = {
            "event": "orderbook_snapshot",
            "symbol": symbol,
            "bids": [[p, q] for p, q in snap["bids"]],
            "asks": [[p, q] for p, q in snap["asks"]],
            "last_trade_price": None,
            "ts": int(asyncio.get_event_loop().time() * 1000),
        }
        await websocket.send_json(snapshot_msg)
        logger.info("Sent initial snapshot for %s: %d bids, %d asks",
                     symbol, len(snap["bids"]), len(snap["asks"]))
    except Exception as e:
        logger.warning("Failed to send initial snapshot for %s: %s", symbol, e)

    # Create per-client Pub/Sub subscription for this symbol
    # Uses the dedicated-connection subscribe() from pubsub module
    channels = [
        pubsub.orderbook_channel(symbol),
        pubsub.trades_channel(symbol),
    ]

    ps = None
    try:
        ps = await pubsub.subscribe(*channels)
        logger.info("Per-client Pub/Sub subscribed for %s", symbol)
    except Exception as e:
        logger.error("Failed to create Pub/Sub for %s: %s", symbol, e)

    # Run two concurrent tasks: Pub/Sub listener + client message listener
    async def listen_pubsub():
        """Forward Pub/Sub messages to the WebSocket client."""
        if ps is None:
            return
        try:
            async for msg in pubsub.iter_messages(ps):
                try:
                    await websocket.send_json(msg)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Pub/Sub listener error for %s: %s", symbol, e)

    async def listen_client():
        """Listen for client messages (pings)."""
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("action") == "ping":
                    await websocket.send_json({
                        "event": "pong",
                        "ts": int(asyncio.get_event_loop().time() * 1000),
                    })
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    try:
        await asyncio.gather(
            listen_pubsub(),
            listen_client(),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Public WS error for %s: %s", symbol, e)
    finally:
        if ps is not None:
            try:
                await pubsub.unsubscribe(ps)
            except Exception:
                pass
        await ws_manager.disconnect_public(websocket, symbol)
        logger.info("Public WS disconnected for %s", symbol)


__all__ = ["handle_public_orderbook"]
