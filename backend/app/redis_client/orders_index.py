"""User open-orders index on Redis Sets.

Key layout
----------
* ``user:{uid}:open_orders``                — SET of all open order IDs for user
* ``user:{uid}:open_orders:{symbol}``        — SET of open order IDs per symbol
* ``symbol:{symbol}:open_orders``            — SET of all open order IDs per symbol

These indexes let us answer:
  * "Cancel all my orders"  → SMEMBERS user:{uid}:open_orders
  * "Cancel all my BTC/USDT orders" → SMEMBERS user:{uid}:open_orders:BTC/USDT
  * "How many orders are open on ETH/USDT?" → SCARD symbol:ETH/USDT:open_orders

Without these indexes we'd have to query PostgreSQL for every cancel-all
request, which is slow and would also require filtering out already-filled
orders.

Atomicity
---------
Membership updates are issued in a pipeline alongside the order HASH and
ZSET updates. They are NOT atomic across the three operations; the matching
worker reconciles drift on startup by re-syncing from PostgreSQL.
"""
from __future__ import annotations

from redis.asyncio import Redis


# ─── Key builders ────────────────────────────────────────────────────────────

USER_OPEN_ORDERS_KEY = "user:{uid}:open_orders"
USER_OPEN_ORDERS_BY_SYMBOL_KEY = "user:{uid}:open_orders:{symbol}"
SYMBOL_OPEN_ORDERS_KEY = "symbol:{symbol}:open_orders"


def user_open_orders_key(user_id: int) -> str:
    return USER_OPEN_ORDERS_KEY.format(uid=user_id)


def user_open_orders_by_symbol_key(user_id: int, symbol: str) -> str:
    return USER_OPEN_ORDERS_BY_SYMBOL_KEY.format(uid=user_id, symbol=symbol)


def symbol_open_orders_key(symbol: str) -> str:
    return SYMBOL_OPEN_ORDERS_KEY.format(symbol=symbol)


# ─── Add / remove ────────────────────────────────────────────────────────────

async def add_open_order(
    redis: Redis,
    user_id: int,
    symbol: str,
    order_id: int,
) -> None:
    """Register an order as open in all three indexes."""
    pipe = redis.pipeline()
    pipe.sadd(user_open_orders_key(user_id), str(order_id))
    pipe.sadd(user_open_orders_by_symbol_key(user_id, symbol), str(order_id))
    pipe.sadd(symbol_open_orders_key(symbol), str(order_id))
    await pipe.execute()


async def remove_open_order(
    redis: Redis,
    user_id: int,
    symbol: str,
    order_id: int,
) -> None:
    """Remove an order from all three indexes (called on fill / cancel)."""
    pipe = redis.pipeline()
    pipe.srem(user_open_orders_key(user_id), str(order_id))
    pipe.srem(user_open_orders_by_symbol_key(user_id, symbol), str(order_id))
    pipe.srem(symbol_open_orders_key(symbol), str(order_id))
    await pipe.execute()


# ─── Reads ───────────────────────────────────────────────────────────────────

async def get_user_open_orders(redis: Redis, user_id: int) -> list[int]:
    """Return all open order IDs for a user (across all symbols)."""
    members = await redis.smembers(user_open_orders_key(user_id))
    return sorted(int(m) for m in members)


async def get_user_open_orders_for_symbol(
    redis: Redis,
    user_id: int,
    symbol: str,
) -> list[int]:
    """Return open order IDs for a specific (user, symbol) pair."""
    members = await redis.smembers(user_open_orders_by_symbol_key(user_id, symbol))
    return sorted(int(m) for m in members)


async def get_symbol_open_orders(redis: Redis, symbol: str) -> list[int]:
    """Return all open order IDs for a symbol (across all users)."""
    members = await redis.smembers(symbol_open_orders_key(symbol))
    return sorted(int(m) for m in members)


async def count_user_open_orders(redis: Redis, user_id: int) -> int:
    """O(1) count of open orders for a user."""
    return await redis.scard(user_open_orders_key(user_id))


async def count_symbol_open_orders(redis: Redis, symbol: str) -> int:
    """O(1) count of open orders on a symbol."""
    return await redis.scard(symbol_open_orders_key(symbol))


async def is_order_open(
    redis: Redis,
    user_id: int,
    order_id: int,
) -> bool:
    """Check if a specific order is in the user's open-orders index."""
    return bool(await redis.sismember(user_open_orders_key(user_id), str(order_id)))


# ─── Bulk operations ─────────────────────────────────────────────────────────

async def cancel_all_user_orders(
    redis: Redis,
    user_id: int,
    symbol: str | None = None,
) -> list[int]:
    """Return the list of order IDs to cancel, and clear the indexes.

    This does NOT actually cancel the orders (that requires PG updates +
    Redis ZREM). The caller (order service) iterates the returned IDs and
    performs the per-order cancel logic.

    If `symbol` is None, all symbols are included; otherwise only the
    specified symbol's orders are returned.

    After this call, the relevant index entries are removed from Redis.
    The per-symbol and per-user indexes are kept consistent.
    """
    if symbol is None:
        # All symbols: drain the user's global set
        members = await redis.smembers(user_open_orders_key(user_id))
        order_ids = [int(m) for m in members]

        pipe = redis.pipeline()
        # Clear the global set
        pipe.delete(user_open_orders_key(user_id))
        # Clear per-symbol subsets and symbol sets for each order
        # We need each order's symbol; load from order HASH
        for oid in order_ids:
            pipe.hget(f"order:{oid}", "symbol")
        symbols = await pipe.execute()
        # symbols[0] is None (the delete result); rest are the hashes
        symbols = [s for s in symbols[1:]]

        pipe2 = redis.pipeline()
        for oid, sym in zip(order_ids, symbols):
            if sym:
                sym_str = sym.decode() if isinstance(sym, bytes) else sym
                pipe2.srem(user_open_orders_by_symbol_key(user_id, sym_str), str(oid))
                pipe2.srem(symbol_open_orders_key(sym_str), str(oid))
        await pipe2.execute()
        return sorted(order_ids)
    else:
        # Single symbol: drain the per-symbol subset only
        members = await redis.smembers(user_open_orders_by_symbol_key(user_id, symbol))
        order_ids = [int(m) for m in members]

        pipe = redis.pipeline()
        pipe.delete(user_open_orders_by_symbol_key(user_id, symbol))
        for oid in order_ids:
            pipe.srem(user_open_orders_key(user_id), str(oid))
            pipe.srem(symbol_open_orders_key(symbol), str(oid))
        await pipe.execute()
        return sorted(order_ids)


# ─── Maintenance ─────────────────────────────────────────────────────────────

async def clear_user_indexes(redis: Redis, user_id: int) -> int:
    """Delete all index keys for a user. Returns keys deleted."""
    # This is best-effort: we can't enumerate per-symbol subsets without
    # scanning. Use SCAN to find them.
    pipe = redis.pipeline()
    pipe.delete(user_open_orders_key(user_id))
    # SCAN for per-symbol subsets
    deleted = 0
    async for key in redis.scan_iter(
        match=user_open_orders_by_symbol_key(user_id, "*"),
        count=100,
    ):
        pipe.delete(key)
    results = await pipe.execute()
    return sum(1 for r in results if r)


async def clear_symbol_indexes(redis: Redis, symbol: str) -> int:
    """Delete the symbol-level open-orders index. Returns keys deleted."""
    return await redis.delete(symbol_open_orders_key(symbol))


__all__ = [
    # Keys
    "user_open_orders_key", "user_open_orders_by_symbol_key", "symbol_open_orders_key",
    # Add / remove
    "add_open_order", "remove_open_order",
    # Reads
    "get_user_open_orders", "get_user_open_orders_for_symbol", "get_symbol_open_orders",
    "count_user_open_orders", "count_symbol_open_orders", "is_order_open",
    # Bulk
    "cancel_all_user_orders",
    # Maintenance
    "clear_user_indexes", "clear_symbol_indexes",
]
