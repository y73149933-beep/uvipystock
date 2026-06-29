"""Redis client package: connection pool, order book, queues, pub/sub, indexes.

Public API
----------
* ``get_redis()``                 — async Redis singleton
* ``orderbook``                   — ZSET ops for the limit order book
* ``stops``                       — stop queue + trailing stop state
* ``queues``                      — LPUSH/BRPOP for order/trade queues
* ``pubsub``                      — Pub/Sub publishers + subscribers
* ``orders_index``                — SET-based user/symbol open-orders index
* ``rate_limit``                  — Sliding-window rate limiter

Typical usage in a FastAPI handler::

    from app.redis_client import get_redis, orderbook, queues

    @router.post("/orders")
    async def place_order(...):
        redis = get_redis()
        ...
        await orderbook.add_resting_order(redis, ...)
        await queues.enqueue_order_action(redis, payload)
"""
from __future__ import annotations

from app.redis_client.client import (
    close_redis,
    get_redis,
    get_redis_dep,
    init_redis,
)
from app.redis_client import (
    orderbook,
    orders_index,
    pubsub,
    queues,
    rate_limit,
    stops,
)

__all__ = [
    # Connection
    "get_redis", "get_redis_dep", "close_redis", "init_redis",
    # Submodules
    "orderbook", "stops", "queues", "pubsub", "orders_index", "rate_limit",
]
