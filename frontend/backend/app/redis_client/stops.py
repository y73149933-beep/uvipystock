"""Stop-order queue and trailing-stop state on Redis.

Key layout
----------
* ``stops:{symbol}``         — ZSET, score = `stop_price * 1e9`, member = `order_id`
* ``trailing:{symbol}``      — ZSET, score = `current_trigger * 1e9`, member = `order_id`
* ``trailing_state:{order_id}`` — HASH with fields: side, delta_type, delta_value,
                                  current_extreme, current_trigger

Stop trigger logic
------------------
For **sell-stop** orders (stop loss for a long position):
  Trigger fires when ``last_trade_price <= stop_price``.
  We use ``ZRANGEBYSCORE stops:{symbol} 0 last_price`` to find them.

For **buy-stop** orders (stop loss for a short position, or stop-entry):
  Trigger fires when ``last_trade_price >= stop_price``.
  We use ``ZRANGEBYSCORE stops:{symbol} last_price +inf`` to find them.

The side is stored in the order HASH (``order:{order_id}.side``) so the
stop monitor can decide direction after fetching candidates.

Trailing stops
--------------
A trailing stop's trigger price moves with the market:
  * For a long (sell-side trailing): trigger = local_high - delta
  * For a short (buy-side trailing): trigger = local_low + delta

The ``trailing_state:{order_id}`` HASH stores the current extreme and
trigger. On each new high/low, the monitor updates the score in
``trailing:{symbol}`` to reflect the new trigger.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from app.models.enums import OrderSide
from app.redis_client.orderbook import SCORE_MULTIPLIER


# ─── Keys ────────────────────────────────────────────────────────────────────

STOPS_KEY = "stops:{symbol}"
TRAILING_KEY = "trailing:{symbol}"
TRAILING_STATE_KEY = "trailing_state:{order_id}"


def stops_key(symbol: str) -> str:
    return STOPS_KEY.format(symbol=symbol)


def trailing_key(symbol: str) -> str:
    return TRAILING_KEY.format(symbol=symbol)


def trailing_state_key(order_id: int) -> str:
    return TRAILING_STATE_KEY.format(order_id=order_id)


# ─── Stop-queue operations ───────────────────────────────────────────────────

async def add_stop_order(
    redis: Redis,
    order_id: int,
    symbol: str,
    stop_price: Decimal | float,
) -> None:
    """Register a stop-order in the trigger queue.

    The order HASH must already exist (created by the order service before
    calling this). The score encodes stop_price so we can range-query by
    last trade price.
    """
    score = float(stop_price) * SCORE_MULTIPLIER
    await redis.zadd(stops_key(symbol), {str(order_id): score})


async def remove_stop_order(redis: Redis, order_id: int, symbol: str) -> bool:
    """Remove a stop-order from the queue (e.g., user canceled)."""
    removed = await redis.zrem(stops_key(symbol), str(order_id))
    return removed > 0


async def get_triggered_sell_stops(
    redis: Redis,
    symbol: str,
    last_trade_price: Decimal | float,
) -> list[int]:
    """Find sell-stop orders whose trigger price has been crossed.

    A sell-stop fires when ``last_trade_price <= stop_price`` (price fell
    to or below the stop). So we look for orders with
    ``stop_price >= last_trade_price``, i.e. ``score >= last_price * 1e9``.

    Returns ALL such orders; the caller must verify each order's HASH has
    ``side == "sell"`` before triggering (a buy-stop in this range would
    NOT fire — it fires when price rises TO its stop, not falls).
    """
    min_score = float(last_trade_price) * SCORE_MULTIPLIER
    members = await redis.zrangebyscore(stops_key(symbol), min_score, "+inf")
    return [int(m) for m in members]


async def get_triggered_buy_stops(
    redis: Redis,
    symbol: str,
    last_trade_price: Decimal | float,
) -> list[int]:
    """Find buy-stop orders whose trigger price has been crossed.

    A buy-stop fires when ``last_trade_price >= stop_price`` (price rose
    to or above the stop). So we look for orders with
    ``stop_price <= last_trade_price``, i.e. ``score <= last_price * 1e9``.

    Returns ALL such orders; the caller must verify each order's HASH has
    ``side == "buy"`` before triggering.
    """
    max_score = float(last_trade_price) * SCORE_MULTIPLIER
    members = await redis.zrangebyscore(stops_key(symbol), 0, max_score)
    return [int(m) for m in members]


async def get_all_triggered_stops(
    redis: Redis,
    symbol: str,
    last_trade_price: Decimal | float,
) -> list[int]:
    """Return all triggered stop order IDs (both buy and sell sides).

    The caller must look up each order's HASH to determine its side and
    decide whether the trigger actually fires (sell-stop vs buy-stop logic).
    """
    price_score = float(last_trade_price) * SCORE_MULTIPLIER
    # Sell-stops: score <= price_score
    sell_stops = await redis.zrangebyscore(stops_key(symbol), 0, price_score)
    # Buy-stops: score >= price_score
    buy_stops = await redis.zrangebyscore(stops_key(symbol), price_score, "+inf")
    # Deduplicate (an order exactly at the price will appear in both)
    seen = set()
    result = []
    for m in sell_stops + buy_stops:
        oid = int(m)
        if oid not in seen:
            seen.add(oid)
            result.append(oid)
    return result


# ─── Trailing-stop state ─────────────────────────────────────────────────────

TRAILING_STATE_FIELDS = (
    "order_id", "symbol", "side", "delta_type", "delta_value",
    "current_extreme", "current_trigger",
)


async def register_trailing_stop(
    redis: Redis,
    order_id: int,
    symbol: str,
    side: OrderSide | str,
    delta_type: str,           # "pct" or "abs"
    delta_value: Decimal | float,
    initial_extreme: Decimal | float,
    initial_trigger: Decimal | float,
) -> None:
    """Register a trailing-stop order and initialize its state.

    The state HASH lets the stop monitor update `current_trigger` as the
    market moves. The ZSET score in ``trailing:{symbol}`` mirrors
    ``current_trigger`` so the same range-query logic as plain stops works.

    For a SELL trailing (long position protection):
      * `side` = "sell"
      * Tracks the local HIGH; trigger = high - delta
      * Initial extreme = current price (will be updated upward)

    For a BUY trailing (short position protection):
      * `side` = "buy"
      * Tracks the local LOW; trigger = low + delta
      * Initial extreme = current price (will be updated downward)
    """
    s = side.value if isinstance(side, OrderSide) else str(side).lower()
    score = float(initial_trigger) * SCORE_MULTIPLIER
    pipe = redis.pipeline()
    pipe.zadd(trailing_key(symbol), {str(order_id): score})
    pipe.hset(trailing_state_key(order_id), mapping={
        "order_id": str(order_id),
        "symbol": symbol,
        "side": s,
        "delta_type": delta_type,
        "delta_value": str(delta_value),
        "current_extreme": str(initial_extreme),
        "current_trigger": str(initial_trigger),
    })
    await pipe.execute()


async def remove_trailing_stop(redis: Redis, order_id: int, symbol: str) -> bool:
    """Remove a trailing-stop from the queue and clear its state."""
    pipe = redis.pipeline()
    pipe.zrem(trailing_key(symbol), str(order_id))
    pipe.delete(trailing_state_key(order_id))
    results = await pipe.execute()
    return results[0] > 0


async def get_trailing_state(redis: Redis, order_id: int) -> dict[str, str] | None:
    """Load the trailing-stop state HASH for an order."""
    raw = await redis.hgetall(trailing_state_key(order_id))
    if not raw:
        return None
    return {k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else v
            for k, v in raw.items()}


async def update_trailing_trigger(
    redis: Redis,
    order_id: int,
    symbol: str,
    new_extreme: Decimal | float,
    new_trigger: Decimal | float,
) -> None:
    """Update the trailing-stop's extreme and trigger.

    Called by the stop monitor when a new local high/low is observed.
    The ZSET score is updated atomically with the state HASH.
    """
    score = float(new_trigger) * SCORE_MULTIPLIER
    pipe = redis.pipeline()
    pipe.zadd(trailing_key(symbol), {str(order_id): score})  # ZADD updates score if member exists
    pipe.hset(trailing_state_key(order_id), mapping={
        "current_extreme": str(new_extreme),
        "current_trigger": str(new_trigger),
    })
    await pipe.execute()


async def get_triggered_trailing_stops(
    redis: Redis,
    symbol: str,
    last_trade_price: Decimal | float,
) -> list[int]:
    """Find trailing-stop orders whose current trigger has been crossed.

    Returns order IDs; caller must look up state HASHes to determine side
    and apply sell-vs-buy trigger logic.
    """
    price_score = float(last_trade_price) * SCORE_MULTIPLIER
    sell_triggered = await redis.zrangebyscore(trailing_key(symbol), 0, price_score)
    buy_triggered = await redis.zrangebyscore(trailing_key(symbol), price_score, "+inf")
    seen = set()
    result = []
    for m in sell_triggered + buy_triggered:
        oid = int(m)
        if oid not in seen:
            seen.add(oid)
            result.append(oid)
    return result


async def update_trailing_extremes(
    redis: Redis,
    symbol: str,
    new_high: Decimal | float | None = None,
    new_low: Decimal | float | None = None,
) -> list[int]:
    """Walk all trailing stops for `symbol` and update triggers.

    Called by the stop monitor on each new high/low. For each trailing
    stop:
      * If SELL trailing and new_high > current_extreme:
          new_trigger = new_high - delta
          update ZSET score + state HASH
      * If BUY trailing and new_low < current_extreme:
          new_trigger = new_low + delta
          update ZSET score + state HASH

    Returns the list of order_ids whose triggers were updated.
    """
    # Load all trailing stop members for this symbol
    members = await redis.zrange(trailing_key(symbol), 0, -1)
    if not members:
        return []

    # Bulk-load state HASHes
    pipe = redis.pipeline()
    for m in members:
        pipe.hgetall(trailing_state_key(int(m)))
    states = await pipe.execute()

    updated: list[int] = []
    updates_pipe = redis.pipeline()

    for member, state in zip(members, states):
        if not state:
            continue
        oid = int(member)
        side = (state.get(b"side") or b"").decode()
        delta_type = (state.get(b"delta_type") or b"abs").decode()
        delta_value = float(state.get(b"delta_value") or b"0")
        current_extreme = float(state.get(b"current_extreme") or b"0")

        new_extreme = current_extreme
        new_trigger = None

        if side == OrderSide.SELL.value and new_high is not None:
            if float(new_high) > current_extreme:
                new_extreme = float(new_high)
                if delta_type == "pct":
                    new_trigger = new_extreme * (1.0 - delta_value / 100.0)
                else:  # abs
                    new_trigger = new_extreme - delta_value

        elif side == OrderSide.BUY.value and new_low is not None:
            if float(new_low) < current_extreme:
                new_extreme = float(new_low)
                if delta_type == "pct":
                    new_trigger = new_extreme * (1.0 + delta_value / 100.0)
                else:  # abs
                    new_trigger = new_extreme + delta_value

        if new_trigger is not None:
            score = new_trigger * SCORE_MULTIPLIER
            updates_pipe.zadd(trailing_key(symbol), {str(oid): score})
            updates_pipe.hset(trailing_state_key(oid), mapping={
                "current_extreme": str(new_extreme),
                "current_trigger": str(new_trigger),
            })
            updated.append(oid)

    if updates_pipe:
        await updates_pipe.execute()
    return updated


# ─── Maintenance ─────────────────────────────────────────────────────────────

async def clear_stops_for_symbol(redis: Redis, symbol: str) -> int:
    """Delete all stop and trailing state for a symbol. Returns keys deleted."""
    pipe = redis.pipeline()
    pipe.delete(stops_key(symbol))
    pipe.delete(trailing_key(symbol))
    # Clear trailing state HASHes
    members = await redis.zrange(trailing_key(symbol), 0, -1)
    for m in members:
        pipe.delete(trailing_state_key(int(m)))
    results = await pipe.execute()
    return sum(results)


__all__ = [
    # Keys
    "stops_key", "trailing_key", "trailing_state_key",
    # Stop queue
    "add_stop_order", "remove_stop_order",
    "get_triggered_sell_stops", "get_triggered_buy_stops", "get_all_triggered_stops",
    # Trailing
    "register_trailing_stop", "remove_trailing_stop", "get_trailing_state",
    "update_trailing_trigger", "get_triggered_trailing_stops", "update_trailing_extremes",
    # Maintenance
    "clear_stops_for_symbol",
]
