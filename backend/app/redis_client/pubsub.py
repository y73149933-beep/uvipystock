"""Redis Pub/Sub: event broadcast for WebSocket fan-out and inter-service signaling.

Channel layout
--------------
* ``pub:orderbook:{symbol}``   — public: L2 book snapshots + deltas
* ``pub:trades:{symbol}``      — public: trade prints (also consumed by stop monitor)
* ``pub:orders:{user_id}``     — private: per-user order status updates
* ``pub:balances:{user_id}``   — private: per-user balance updates
* ``pub:bulk:{user_id}``       — private: bulk operation results

Publishers
----------
The matching worker and the order service are the primary publishers.
Each publish serializes the payload as compact JSON.

Subscribers
-----------
* The WebSocket gateway subscribes to ``pub:orderbook:{symbol}`` and
  ``pub:trades:{symbol}`` for the public channels, and to
  ``pub:orders:{user_id}`` / ``pub:balances:{user_id}`` for each
  connected user's private channel.
* The stop monitor subscribes to ``pub:trades:{symbol}`` to evaluate
  stop triggers on each new print.

Reliability
-----------
Redis Pub/Sub is fire-and-forget — subscribers that are not connected
when a message is published will NOT receive it. For stateful consumers
(like the WS gateway), we mitigate this by:
  1. Sending an initial snapshot when a client subscribes.
  2. Using the Redis Streams fallback (TODO) for critical events that
     must not be lost (e.g., balance updates).
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.client import PubSub


# ─── Channel name builders ───────────────────────────────────────────────────

ORDERBOOK_CHANNEL = "pub:orderbook:{symbol}"
TRADES_CHANNEL = "pub:trades:{symbol}"
ORDERS_CHANNEL = "pub:orders:{user_id}"
BALANCES_CHANNEL = "pub:balances:{user_id}"
BULK_CHANNEL = "pub:bulk:{user_id}"


def orderbook_channel(symbol: str) -> str:
    return ORDERBOOK_CHANNEL.format(symbol=symbol)


def trades_channel(symbol: str) -> str:
    return TRADES_CHANNEL.format(symbol=symbol)


def orders_channel(user_id: int) -> str:
    return ORDERS_CHANNEL.format(user_id=user_id)


def balances_channel(user_id: int) -> str:
    return BALANCES_CHANNEL.format(user_id=user_id)


def bulk_channel(user_id: int) -> str:
    return BULK_CHANNEL.format(user_id=user_id)


# ─── Payload builders ────────────────────────────────────────────────────────

def _envelope(event_type: str, data: dict[str, Any]) -> str:
    """Wrap an event in a standard envelope with timestamp.

    The envelope uses ``event`` (not ``type``) for the event-type discriminator
    to avoid collision with domain fields named ``type`` (e.g. OrderType).
    """
    payload = {
        "event": event_type,
        "ts": int(time.time() * 1000),
        **data,
    }
    return json.dumps(payload, separators=(",", ":"), default=str)


# ─── Publishers ──────────────────────────────────────────────────────────────

async def publish_orderbook_snapshot(
    redis: Redis,
    symbol: str,
    bids: list[list[float]],
    asks: list[list[float]],
    last_trade_price: float | None = None,
) -> int:
    """Publish a full L2 snapshot for `symbol`.

    `bids` and `asks` are lists of `[price, volume]` pairs. Receivers
    (WS gateway) replace their entire book state with this snapshot.
    """
    channel = orderbook_channel(symbol)
    payload = _envelope("orderbook_snapshot", {
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "last_trade_price": last_trade_price,
    })
    return await redis.publish(channel, payload)


async def publish_orderbook_update(
    redis: Redis,
    symbol: str,
    changes: list[dict[str, Any]],
) -> int:
    """Publish an incremental L2 update.

    `changes` is a list of ``{"side": "bid"|"ask", "price": float, "qty": float}``
    dicts. A qty of 0 means the level should be removed.
    """
    channel = orderbook_channel(symbol)
    payload = _envelope("orderbook_update", {
        "symbol": symbol,
        "changes": changes,
    })
    return await redis.publish(channel, payload)


async def publish_trade(
    redis: Redis,
    symbol: str,
    trade_id: int,
    price: float,
    quantity: float,
    side: str,
    taker_order_id: int | None = None,
    maker_order_id: int | None = None,
) -> int:
    """Publish a trade print to the public trades channel.

    `side` is the taker side ("buy" or "sell").
    """
    channel = trades_channel(symbol)
    data: dict[str, Any] = {
        "symbol": symbol,
        "trade_id": trade_id,
        "price": price,
        "quantity": quantity,
        "side": side,
    }
    if taker_order_id is not None:
        data["taker_order_id"] = taker_order_id
    if maker_order_id is not None:
        data["maker_order_id"] = maker_order_id
    payload = _envelope("trade", data)
    return await redis.publish(channel, payload)


async def publish_order_update(
    redis: Redis,
    user_id: int,
    event: str,             # placed | partially_filled | filled | canceled | modified | rejected | triggered
    order_id: int,
    symbol: str,
    side: str,
    type: str,
    status: str,
    price: float | None = None,
    quantity: float | None = None,
    filled_quantity: float | None = None,
    remaining_quantity: float | None = None,
    avg_fill_price: float | None = None,
    last_trade_qty: float | None = None,
    last_trade_price: float | None = None,
    client_order_id: str | None = None,
    bulk_id: str | None = None,
) -> int:
    """Publish a per-user order status update to the private orders channel.

    The envelope discriminator is ``event="order"`` (set by `_envelope`).
    The domain-specific lifecycle event (e.g. "partially_filled") is stored
    under the ``status_event`` key to avoid collision with the envelope.
    """
    channel = orders_channel(user_id)
    data: dict[str, Any] = {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "type": type,                # OrderType (limit, market, etc.)
        "status": status,            # OrderStatus (new, partially_filled, etc.)
        "status_event": event,       # lifecycle event: placed/filled/canceled/...
    }
    if price is not None:
        data["price"] = price
    if quantity is not None:
        data["quantity"] = quantity
    if filled_quantity is not None:
        data["filled_quantity"] = filled_quantity
    if remaining_quantity is not None:
        data["remaining_quantity"] = remaining_quantity
    if avg_fill_price is not None:
        data["avg_fill_price"] = avg_fill_price
    if last_trade_qty is not None:
        data["last_trade_qty"] = last_trade_qty
    if last_trade_price is not None:
        data["last_trade_price"] = last_trade_price
    if client_order_id is not None:
        data["client_order_id"] = client_order_id
    if bulk_id is not None:
        data["bulk_id"] = bulk_id
    payload = _envelope("order", data)
    return await redis.publish(channel, payload)


async def publish_balance_update(
    redis: Redis,
    user_id: int,
    asset: str,
    total: float,
    locked: float,
    available: float,
    change: float | None = None,
    reason: str | None = None,
    order_id: int | None = None,
) -> int:
    """Publish a per-user balance update to the private balances channel."""
    channel = balances_channel(user_id)
    data: dict[str, Any] = {
        "asset": asset,
        "total": total,
        "locked": locked,
        "available": available,
    }
    if change is not None:
        data["change"] = change
    if reason is not None:
        data["reason"] = reason
    if order_id is not None:
        data["order_id"] = order_id
    payload = _envelope("balance", data)
    return await redis.publish(channel, payload)


async def publish_bulk_result(
    redis: Redis,
    user_id: int,
    bulk_id: str,
    action: str,            # "place" | "cancel"
    total: int,
    succeeded: int,
    failed: list[dict[str, Any]] | None = None,
) -> int:
    """Publish the result of a bulk operation to the private bulk channel."""
    channel = bulk_channel(user_id)
    data = {
        "bulk_id": bulk_id,
        "action": action,
        "total": total,
        "succeeded": succeeded,
        "failed": failed or [],
    }
    payload = _envelope("bulk_result", data)
    return await redis.publish(channel, payload)


async def publish_sl_tp_activated(
    redis: Redis,
    user_id: int,
    parent_order_id: int,
    sl_order_id: int | None = None,
    tp_order_id: int | None = None,
) -> int:
    """Notify the user that their SL/TP children have been activated."""
    channel = orders_channel(user_id)
    data: dict[str, Any] = {
        "parent_order_id": parent_order_id,
    }
    if sl_order_id is not None:
        data["sl_order_id"] = sl_order_id
    if tp_order_id is not None:
        data["tp_order_id"] = tp_order_id
    payload = _envelope("sl_tp_activated", data)
    return await redis.publish(channel, payload)


# ─── Subscriber helpers ──────────────────────────────────────────────────────

async def subscribe(
    *channels: str,
) -> PubSub:
    """Subscribe to one or more channels using a DEDICATED connection.

    IMPORTANT: PubSub ``listen()`` is a blocking operation (like BRPOP).
    Using the shared connection pool would cause:
      1. Pool starvation — listen() holds a connection indefinitely.
      2. Health check PINGs (every 30s) interfere with the blocking read,
         causing CancelledError → ConnectionError: "Bad response from PING".

    To avoid this, we create a SEPARATE Redis instance (not from the pool)
    with:
      - ``socket_timeout=60`` (no race with blocking read)
      - ``retry_on_timeout=False`` (don't retry blocking reads)
      - ``health_check_interval=0`` (no PINGs on blocking connection)

    Usage::

        pubsub = await subscribe(redis, orderbook_channel("BTC/USDT"))
        async for message in pubsub.listen():
            if message["type"] == "message":
                payload = json.loads(message["data"])
                ...

    Remember to call ``await pubsub.unsubscribe()`` and ``await pubsub.aclose()``
    when done to release the underlying connection.
    """
    from app.config import get_settings
    settings = get_settings()

    # Create a dedicated Redis instance (NOT from the shared pool)
    # socket_timeout=None (infinite) is REQUIRED for PubSub listen(), which
    # blocks indefinitely waiting for messages. A finite timeout would fire
    # after N seconds of silence, raising TimeoutError and breaking the
    # subscription loop. socket_keepalive=True detects dropped connections.
    dedicated_redis = aioredis.Redis.from_url(
        str(settings.redis_url),
        decode_responses=False,
        socket_connect_timeout=10,
        socket_timeout=None,      # infinite — PubSub listen() blocks forever
        socket_keepalive=True,    # detect dropped connections at TCP level
        retry_on_timeout=False,   # never retry blocking reads
        health_check_interval=0,  # no PINGs on blocking connection
    )

    pubsub = dedicated_redis.pubsub()
    await pubsub.subscribe(*channels)
    # Attach the dedicated client so unsubscribe() can close it
    pubsub._dedicated_redis = dedicated_redis  # type: ignore[attr-defined]
    return pubsub


async def unsubscribe(pubsub: PubSub, *channels: str) -> None:
    """Unsubscribe from channels and close the dedicated connection."""
    if channels:
        await pubsub.unsubscribe(*channels)
    else:
        await pubsub.unsubscribe()
    # Close the PubSub connection
    await pubsub.aclose()
    # Close the dedicated Redis instance we created in subscribe()
    dedicated = getattr(pubsub, "_dedicated_redis", None)
    if dedicated is not None:
        try:
            await dedicated.aclose()
        except Exception:
            pass


async def iter_messages(pubsub: PubSub, ignore_subscribe: bool = True) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over PubSub messages.

    Yields decoded JSON dicts. Messages of type ``subscribe`` /
    ``unsubscribe`` are skipped unless `ignore_subscribe=False`.

    Usage::

        pubsub = await subscribe(redis, channel)
        async for msg in iter_messages(pubsub):
            print(msg)
    """
    async for raw in pubsub.listen():
        if raw["type"] not in ("message", "pmessage"):
            if ignore_subscribe:
                continue
        data = raw["data"]
        if isinstance(data, bytes):
            data = data.decode()
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            # Pass through raw if not JSON (some legacy publishers)
            yield {"_raw": data}


__all__ = [
    # Channel builders
    "orderbook_channel", "trades_channel", "orders_channel",
    "balances_channel", "bulk_channel",
    # Publishers
    "publish_orderbook_snapshot", "publish_orderbook_update",
    "publish_trade", "publish_order_update", "publish_balance_update",
    "publish_bulk_result", "publish_sl_tp_activated",
    # Subscribers
    "subscribe", "unsubscribe", "iter_messages",
]