"""FIFO queues on Redis Lists.

Key layout
----------
* ``queue:orders``           — LPUSH/BRPOP, payload = JSON order action
* ``queue:trades``           — LPUSH/BRPOP, payload = JSON trade batch
* ``queue:events.deadletter`` — LPUSH only, payload = failed action + traceback

The matching worker is the sole consumer of ``queue:orders``. It uses
``BRPOP`` with a timeout to avoid busy-polling. Producers (FastAPI
handlers) use ``LPUSH`` which is O(1) and returns immediately.

For higher throughput, the queue can be sharded by symbol:
``queue:orders:{symbol}`` with one worker per shard. The unsharded form
is the default for simplicity.
"""
from __future__ import annotations

import json
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError, TimeoutError


# ─── Keys ────────────────────────────────────────────────────────────────────

ORDERS_QUEUE = "queue:orders"
TRADES_QUEUE = "queue:trades"
DEADLETTER_QUEUE = "queue:events.deadletter"


# ─── Order-action payload schema ─────────────────────────────────────────────
# These helpers centralize the JSON shape so producers and consumers stay
# in sync. The matching worker's `OrderActionPayload.from_redis` (in
# `app.matching.worker`) mirrors this structure.

def build_order_action_payload(
    *,
    action: str,             # "place" | "cancel" | "modify"
    order_id: int,
    symbol: str,
    user_id: int,
    side: str,               # "buy" | "sell"
    type: str,               # OrderType value
    price: float | None = None,
    stop_price: float | None = None,
    quantity: float | None = None,
    is_iceberg: bool = False,
    visible_qty: float | None = None,
    hidden_qty: float | None = None,
    parent_order_id: int | None = None,
    replaces_id: int | None = None,
    bulk_id: str | None = None,
) -> dict[str, Any]:
    """Construct an order-action payload ready for JSON serialization."""
    payload: dict[str, Any] = {
        "action": action,
        "order_id": order_id,
        "symbol": symbol,
        "user_id": user_id,
        "side": side,
        "type": type,
        "ts": int(time.time() * 1000),  # ms-precision timestamp for ordering
    }
    if price is not None:
        payload["price"] = price
    if stop_price is not None:
        payload["stop_price"] = stop_price
    # quantity is ALWAYS required for place/modify actions — include even if 0
    payload["quantity"] = quantity if quantity is not None else 0.0
    if is_iceberg:
        payload["is_iceberg"] = True
        payload["visible_qty"] = visible_qty
        payload["hidden_qty"] = hidden_qty
    if parent_order_id is not None:
        payload["parent_order_id"] = parent_order_id
    if replaces_id is not None:
        payload["replaces_id"] = replaces_id
    if bulk_id is not None:
        payload["bulk_id"] = bulk_id
    return payload


# ─── Producers (LPUSH) ───────────────────────────────────────────────────────

async def enqueue_order_action(redis: Redis, payload: dict[str, Any]) -> None:
    """Push an order action onto the queue for the matching worker.

    The payload is JSON-serialized with compact separators to minimize
    network bytes. Keys with None values should be omitted by the caller
    (use `build_order_action_payload` which does this automatically).
    """
    raw = json.dumps(payload, separators=(",", ":"), default=str)
    await redis.lpush(ORDERS_QUEUE, raw)


async def enqueue_trade_batch(redis: Redis, trades: list[dict[str, Any]]) -> None:
    """Push a batch of trades onto the persistence queue.

    The persistence worker (separate from the matcher) consumes this and
    inserts rows into the `trades` PostgreSQL table. Batching reduces
    per-trade DB round-trips.
    """
    if not trades:
        return
    raw = json.dumps({"trades": trades, "ts": int(time.time() * 1000)},
                     separators=(",", ":"), default=str)
    await redis.lpush(TRADES_QUEUE, raw)


async def enqueue_deadletter(
    redis: Redis,
    original_payload: dict[str, Any],
    error: str,
    queue_name: str,
) -> None:
    """Push a failed action onto the dead-letter queue for manual inspection.

    The dead-letter queue is consumed by ops dashboards / alerting; it is
    NOT automatically retried (retry logic lives in the worker).
    """
    entry = {
        "queue": queue_name,
        "original": original_payload,
        "error": error,
        "ts": int(time.time() * 1000),
    }
    raw = json.dumps(entry, separators=(",", ":"), default=str)
    await redis.lpush(DEADLETTER_QUEUE, raw)


# ─── Consumer (BRPOP) ────────────────────────────────────────────────────────

async def brpop_order_action(
    redis: Redis,
    timeout: int = 5,
) -> dict[str, Any] | None:
    """Blocking-pop the next order action from the queue.

    Returns None if no item arrives within `timeout` seconds. The caller
    (matching worker) loops on this in an asyncio task.

    BRPOP pops from the RIGHT (oldest entry), giving true FIFO order
    when paired with LPUSH on the left.
    """
    result = await redis.brpop(ORDERS_QUEUE, timeout=timeout)
    if result is None:
        return None
    # result is a tuple (queue_name, payload_bytes)
    _, raw = result
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


async def brpop_trade_batch(
    redis: Redis,
    timeout: int = 5,
) -> dict[str, Any] | None:
    """Blocking-pop the next trade batch from the persistence queue."""
    result = await redis.brpop(TRADES_QUEUE, timeout=timeout)
    if result is None:
        return None
    _, raw = result
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


# ─── Queue introspection ─────────────────────────────────────────────────────

async def queue_length(redis: Redis, queue: str = ORDERS_QUEUE) -> int:
    """Return the current depth of a queue (LLEN)."""
    return await redis.llen(queue)


async def peek_queue(redis: Redis, queue: str = ORDERS_QUEUE, n: int = 10) -> list[dict[str, Any]]:
    """Peek at the next `n` items in a queue without removing them (LRANGE).

    Useful for admin dashboards and debugging.
    """
    items = await redis.lrange(queue, 0, n - 1)
    result = []
    for raw in items:
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            result.append(json.loads(raw))
        except json.JSONDecodeError:
            result.append({"_raw": raw, "_error": "invalid_json"})
    return result


# ─── Maintenance ─────────────────────────────────────────────────────────────

async def clear_queue(redis: Redis, queue: str) -> int:
    """Delete a queue entirely. Returns the number of items removed."""
    return await redis.llen(queue) if await redis.delete(queue) else 0


__all__ = [
    # Keys
    "ORDERS_QUEUE", "TRADES_QUEUE", "DEADLETTER_QUEUE",
    # Payload builder
    "build_order_action_payload",
    # Producers
    "enqueue_order_action", "enqueue_trade_batch", "enqueue_deadletter",
    # Consumers
    "brpop_order_action", "brpop_trade_batch",
    # Introspection
    "queue_length", "peek_queue",
    # Maintenance
    "clear_queue",
]