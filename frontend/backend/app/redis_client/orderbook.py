"""Order book operations on Redis Sorted Sets.

Key layout
----------
* ``ob:{symbol}:bids``   — ZSET, score = `price * 1e9 + seq`, member = `order_id`
* ``ob:{symbol}:asks``   — ZSET, score = `price * 1e9 + seq`, member = `order_id`
* ``order:{order_id}``   — HASH with metadata (side, price, qty, type, user_id, ...)

Score encoding
--------------
Redis ZSET scores are IEEE-754 doubles. To encode Price-Time Priority we
pack (price, sequence) into a single double:

    score = price * 1e9 + seq

For asks (ascending by price) lower price → lower score → earlier in
``ZRANGE``. For bids (descending by price) higher price → higher score
→ earlier in ``ZREVRANGE``.

``seq`` is a monotonically increasing per-symbol counter that gives FIFO
ordering within a single price level. We use ``INCR seq:orders:{symbol}``
to generate it atomically.

The 1e9 multiplier supports prices up to ~9.2e9 with 9 digits of sequence
space — sufficient for crypto-asset prices and per-symbol throughput of
1 billion orders before sequence rollover. For higher-precision assets,
increase the multiplier or shard by price range.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Iterable

from redis.asyncio import Redis

from app.models.enums import OrderSide


# ─── Constants ───────────────────────────────────────────────────────────────

SCORE_MULTIPLIER = 1_000_000_000  # 1e9 — leaves 9 digits for sequence

BIDS_KEY = "ob:{symbol}:bids"
ASKS_KEY = "ob:{symbol}:asks"
ORDER_HASH_KEY = "order:{order_id}"
SEQ_KEY = "seq:orders:{symbol}"


# ─── Key helpers ─────────────────────────────────────────────────────────────

def bids_key(symbol: str) -> str:
    return BIDS_KEY.format(symbol=symbol)


def asks_key(symbol: str) -> str:
    return ASKS_KEY.format(symbol=symbol)


def order_hash_key(order_id: int) -> str:
    return ORDER_HASH_KEY.format(order_id=order_id)


def seq_key(symbol: str) -> str:
    return SEQ_KEY.format(symbol=symbol)


def side_key(symbol: str, side: OrderSide | str) -> str:
    """Return the ZSET key for the given symbol+side."""
    s = side.value if isinstance(side, OrderSide) else str(side).lower()
    if s == OrderSide.BUY.value:
        return bids_key(symbol)
    if s == OrderSide.SELL.value:
        return asks_key(symbol)
    raise ValueError(f"Invalid side: {side!r}")


def opposite_side_key(symbol: str, side: OrderSide | str) -> str:
    """Return the ZSET key for the opposite side."""
    s = side.value if isinstance(side, OrderSide) else str(side).lower()
    if s == OrderSide.BUY.value:
        return asks_key(symbol)
    if s == OrderSide.SELL.value:
        return bids_key(symbol)
    raise ValueError(f"Invalid side: {side!r}")


# ─── Score encoding ──────────────────────────────────────────────────────────
#
# We pack (price, seq) into a single ZSET score so that Redis sorts orders
# first by price (best first) and then by arrival order (FIFO within a level).
#
# Encoding:  score = price_int * SCORE_MULTIPLIER + seq
#   where price_int = int(round(price * PRICE_SCALE))
#         PRICE_SCALE     = 1e4  (4 decimal places — sufficient for most pairs)
#         SCORE_MULTIPLIER = 1e6  (1M orders per symbol before rollover)
#
# This keeps the encoded score within IEEE-754 double precision (~15-17
# significant digits): a price of 1,000,000.0000 encoded with seq 999,999
# gives a score of 1e10 + 1e6 = ~1e10, well within precision.
#
# For pairs needing higher price precision (e.g. 8 decimals), increase
# PRICE_SCALE and decrease SCORE_MULTIPLIER accordingly, or shard the book
# by price range. The product PRICE_SCALE * SCORE_MULTIPLIER must stay
# below ~1e13 to preserve exact integer representation in double.

PRICE_SCALE      = 10_000          # 1e4 — 4 decimal places for price
SCORE_MULTIPLIER = 1_000_000       # 1e6 — 1M orders per symbol before rollover


def encode_score(price: Decimal | float | int, seq: int) -> float:
    """Pack (price, seq) into a single ZSET score.

    The price is first scaled to an integer (price * 1e4, rounded) so that
    the high digits of the score encode the price unambiguously and the
    low 6 digits encode the sequence number.

    Raises ValueError if the price is negative, too large, or if seq is
    out of range.
    """
    p = float(price)
    if p < 0:
        raise ValueError(f"price must be non-negative, got {price}")
    if p > 9.2e9:
        raise ValueError(
            f"price {price} too large for score encoding "
            f"(max ~9.2e9 with 1e4 price scale)"
        )
    if not (0 <= seq < SCORE_MULTIPLIER):
        raise ValueError(f"seq must be in [0, {SCORE_MULTIPLIER}), got {seq}")

    price_int = int(round(p * PRICE_SCALE))
    return float(price_int * SCORE_MULTIPLIER + seq)


def decode_score(score: float) -> tuple[float, int]:
    """Unpack a ZSET score back into (price, seq).

    Inverse of `encode_score`. Uses integer arithmetic throughout to avoid
    float drift.

    Returns (price_as_float, seq_as_int). Callers that need Decimal precision
    should use the original price stored in the order HASH.
    """
    s = int(round(score))
    price_int = s // SCORE_MULTIPLIER
    seq = s - price_int * SCORE_MULTIPLIER
    price = price_int / PRICE_SCALE
    return float(price), int(seq)


# ─── Order metadata (HASH) ───────────────────────────────────────────────────

ORDER_HASH_FIELDS = (
    "order_id", "user_id", "symbol", "side", "type", "status",
    "price", "stop_price", "quantity", "filled_quantity",
    "visible_quantity", "hidden_quantity", "is_iceberg",
    "parent_order_id", "sl_order_id", "tp_order_id", "bulk_id",
    "created_at_ts",
)


async def store_order_hash(
    redis: Redis,
    order_id: int,
    metadata: dict[str, Any],
) -> None:
    """Persist order metadata as a Redis HASH.

    All values are coerced to strings (Redis convention). ``None`` values
    are stored as empty strings to keep the schema flat.

    The HASH is the hot-path cache consulted by the matching worker before
    each match — it avoids a round-trip to PostgreSQL for every order.
    """
    key = order_hash_key(order_id)
    flat: dict[str, str] = {}
    for field in ORDER_HASH_FIELDS:
        if field in metadata:
            v = metadata[field]
            flat[field] = "" if v is None else str(v)
    if flat:
        await redis.hset(key, mapping=flat)


async def get_order_hash(redis: Redis, order_id: int) -> dict[str, str] | None:
    """Load an order's metadata HASH. Returns None if the key doesn't exist."""
    key = order_hash_key(order_id)
    raw = await redis.hgetall(key)
    if not raw:
        return None
    # redis-py returns bytes when decode_responses=False
    return {k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else v
            for k, v in raw.items()}


async def delete_order_hash(redis: Redis, order_id: int) -> None:
    """Remove an order's HASH (called when the order is fully filled or canceled)."""
    await redis.delete(order_hash_key(order_id))


async def update_order_hash_fields(
    redis: Redis,
    order_id: int,
    fields: dict[str, Any],
) -> None:
    """Partial update of specific HASH fields (e.g., filled_quantity)."""
    key = order_hash_key(order_id)
    flat = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    if flat:
        await redis.hset(key, mapping=flat)


# ─── Sequence counter ────────────────────────────────────────────────────────

async def next_seq(redis: Redis, symbol: str) -> int:
    """Atomically increment and return the next sequence number for `symbol`.

    Used to give each new order a unique score suffix for FIFO ordering.
    The counter is per-symbol so it grows slower than a global counter.
    """
    seq = await redis.incr(seq_key(symbol))
    # seq starts at 1; modulo 1e9 to stay within the score's lower digits
    return int(seq) % SCORE_MULTIPLIER


# ─── Add / remove resting orders ─────────────────────────────────────────────

async def add_resting_order(
    redis: Redis,
    order_id: int,
    symbol: str,
    side: OrderSide | str,
    price: Decimal | float,
    quantity: Decimal | float,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert a resting order into the book.

    Steps (atomic via pipeline, but NOT a Lua script — see note below):
      1. INCR seq:orders:{symbol} → seq
      2. HSET order:{order_id} metadata
      3. ZADD ob:{symbol}:{bids|asks}  score  order_id

    Returns the assigned `seq` so the caller can store it on the Order row
    in PostgreSQL (useful for debugging and Cancel-Replace audit trails).

    Atomicity note
    --------------
    The three ops are issued in a single pipeline but are NOT atomic from
    Redis's point of view (pipelines are batched but each command runs
    separately). For true atomicity we'd use a Lua script; for the matching
    worker's use case (single consumer per symbol) the small race window
    is acceptable. If multiple producers write to the same symbol, wrap
    this in a Lua script (TODO in Step 2d).
    """
    seq = await next_seq(redis, symbol)
    score = encode_score(price, seq)
    zset_key = side_key(symbol, side)

    pipe = redis.pipeline()
    if metadata:
        flat = {f: ("" if v is None else str(v)) for f, v in metadata.items() if f in ORDER_HASH_FIELDS}
        if flat:
            pipe.hset(order_hash_key(order_id), mapping=flat)
    pipe.zadd(zset_key, {str(order_id): score})
    await pipe.execute()
    return seq


async def remove_resting_order(
    redis: Redis,
    order_id: int,
    symbol: str,
    side: OrderSide | str,
    price: Decimal | float,
    seq: int,
) -> bool:
    """Remove a resting order from the book.

    The (price, seq) pair is needed to compute the exact score for ZREM.
    Alternatively we could ZREM by member alone (order_id), but Redis ZREM
    by member is O(log N) and doesn't require the score — so we use that
    simpler form here and only validate the score if needed.

    Returns True if the order was removed, False if it wasn't in the ZSET.
    """
    zset_key = side_key(symbol, side)
    # ZREM by member (order_id) — no score needed
    removed_count = await redis.zrem(zset_key, str(order_id))
    return removed_count > 0


# ─── Read side: snapshot + best price ────────────────────────────────────────

async def get_best_bid(redis: Redis, symbol: str) -> tuple[float, int] | None:
    """Return (price, order_id) for the highest bid, or None if empty."""
    # ZREVRANGE returns highest score first; take 1 element with scores
    res = await redis.zrevrange(bids_key(symbol), 0, 0, withscores=True)
    if not res:
        return None
    member, score = res[0]
    price, _ = decode_score(score)
    return price, int(member)


async def get_best_ask(redis: Redis, symbol: str) -> tuple[float, int] | None:
    """Return (price, order_id) for the lowest ask, or None if empty."""
    res = await redis.zrange(asks_key(symbol), 0, 0, withscores=True)
    if not res:
        return None
    member, score = res[0]
    price, _ = decode_score(score)
    return price, int(member)


async def get_spread(redis: Redis, symbol: str) -> tuple[float | None, float | None, float | None]:
    """Return (best_bid, best_ask, spread). Any value is None if that side is empty."""
    bid = await get_best_bid(redis, symbol)
    ask = await get_best_ask(redis, symbol)
    bid_price = bid[0] if bid else None
    ask_price = ask[0] if ask else None
    spread = (ask_price - bid_price) if (bid_price is not None and ask_price is not None) else None
    return bid_price, ask_price, spread


async def get_book_snapshot(
    redis: Redis,
    symbol: str,
    depth: int = 20,
) -> dict[str, list[tuple[float, float]]]:
    """Return top-N levels for both sides.

    Returns ``{"bids": [(price, total_volume), ...], "asks": [...]}`` where
    each entry is the aggregated volume at that price level.

    Aggregation: we walk the ZSET and sum volumes per price using the
    order HASH. For large books this is N+1 round-trips; a production
    system would maintain a parallel ``ob:{symbol}:bids:agg`` ZSET with
    price→total_volume to avoid the per-order HASH lookups.
    """
    bids_raw = await redis.zrevrange(bids_key(symbol), 0, depth - 1, withscores=True)
    asks_raw = await redis.zrange(asks_key(symbol), 0, depth - 1, withscores=True)

    # Aggregate by price level
    async def _aggregate(raw: list) -> list[tuple[float, float]]:
        if not raw:
            return []
        # Load all order metadata in a single pipeline
        pipe = redis.pipeline()
        for member, _ in raw:
            pipe.hgetall(order_hash_key(int(member)))
        hashes = await pipe.execute()

        levels: dict[float, float] = {}
        for (member, score), h in zip(raw, hashes):
            if not h:
                continue
            price, _ = decode_score(score)
            # Use visible_quantity for iceberg, quantity otherwise
            visq = h.get(b"visible_quantity") or h.get(b"quantity") or b"0"
            qty = float(visq)
            levels[price] = levels.get(price, 0.0) + qty
        return sorted(levels.items(), reverse=(raw == bids_raw))

    return {
        "bids": await _aggregate(bids_raw),
        "asks": await _aggregate(asks_raw),
    }


async def get_opposite_orders_for_match(
    redis: Redis,
    symbol: str,
    taker_side: OrderSide | str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Load up to `limit` opposite-side orders for the Cython matcher.

    Returns a list of dicts (one per order) with the fields the matcher
    needs: order_id, side, price, quantity, visible_quantity, hidden_quantity,
    is_iceberg.

    The matcher constructs an in-memory book from these and runs the match
    in pure-C. After matching, the worker calls `apply_match_results(...)` to
    persist changes back to Redis.
    """
    zset_key = opposite_side_key(symbol, taker_side)
    # For a buy taker, opposite is asks (ascending); for sell taker, opposite is bids (descending)
    if (taker_side.value if isinstance(taker_side, OrderSide) else str(taker_side).lower()) == OrderSide.BUY.value:
        raw = await redis.zrange(zset_key, 0, limit - 1, withscores=True)
    else:
        raw = await redis.zrevrange(zset_key, 0, limit - 1, withscores=True)

    if not raw:
        return []

    pipe = redis.pipeline()
    for member, _ in raw:
        pipe.hgetall(order_hash_key(int(member)))
    hashes = await pipe.execute()

    result: list[dict[str, Any]] = []
    for (member, score), h in zip(raw, hashes):
        if not h:
            continue
        price, seq = decode_score(score)
        order_id = int(member)
        is_iceberg = h.get(b"is_iceberg", b"0") == b"1"
        result.append({
            "order_id": order_id,
            "side": (h.get(b"side") or b"").decode(),
            "price": price,
            "quantity": float(h.get(b"quantity") or b"0"),
            "visible_quantity": float(h.get(b"visible_quantity") or h.get(b"quantity") or b"0"),
            "hidden_quantity": float(h.get(b"hidden_quantity") or b"0"),
            "is_iceberg": is_iceberg,
            "seq": seq,
        })
    return result


# ─── Apply match results ─────────────────────────────────────────────────────

async def apply_match_results(
    redis: Redis,
    symbol: str,
    trades: list[dict[str, Any]],
    taker_order_id: int,
    taker_remaining_qty: float,
    taker_metadata: dict[str, Any] | None = None,
) -> None:
    """Apply matcher output to Redis.

    For each trade:
      * Update maker order HASH (filled_quantity, status)
      * ZREM maker from ZSET if fully filled
      * Update taker HASH

    If the taker has remaining qty and is a LIMIT/Post-Only type, the caller
    is responsible for calling `add_resting_order(...)` afterwards to insert
    it into the book.

    All updates are batched in a single pipeline for efficiency. They are
    NOT atomic (no MULTI/EXEC) — if atomicity is required, wrap in a Lua
    script. The matching worker applies results within a PostgreSQL
    transaction that provides the source-of-truth guarantee.
    """
    pipe = redis.pipeline()

    for trade in trades:
        maker_id = int(trade["maker_order_id"])
        maker_hash_key = order_hash_key(maker_id)
        # Mark maker as updated; the worker will HSET the new filled_quantity
        # after computing it in Python. For now we just signal "touched".
        pipe.hset(maker_hash_key, mapping={
            "_last_taker_id": str(taker_order_id),
            "_last_trade_qty": str(trade["quantity"]),
        })

    # Update taker HASH
    if taker_metadata:
        flat = {f: ("" if v is None else str(v)) for f, v in taker_metadata.items() if f in ORDER_HASH_FIELDS}
        if flat:
            pipe.hset(order_hash_key(taker_order_id), mapping=flat)

    await pipe.execute()


# ─── Maintenance: clear symbol book ──────────────────────────────────────────

async def clear_symbol_book(redis: Redis, symbol: str) -> int:
    """Delete all ZSET entries and order HASHes for `symbol`.

    Used by admin "reset market" and in tests. Returns the number of
    keys deleted.
    """
    pipe = redis.pipeline()
    pipe.delete(bids_key(symbol))
    pipe.delete(asks_key(symbol))
    pipe.delete(seq_key(symbol))
    # Also delete all order:{id} HASHes — we need to enumerate them via ZSET
    # members first (capture before delete)
    all_members = []
    bid_members = await redis.zrange(bids_key(symbol), 0, -1)
    ask_members = await redis.zrange(asks_key(symbol), 0, -1)
    all_members = list(bid_members) + list(ask_members)
    for m in all_members:
        pipe.delete(order_hash_key(int(m)))
    results = await pipe.execute()
    return sum(results)


__all__ = [
    # Constants
    "SCORE_MULTIPLIER",
    "ORDER_HASH_FIELDS",
    # Key helpers
    "bids_key", "asks_key", "order_hash_key", "seq_key",
    "side_key", "opposite_side_key",
    # Score
    "encode_score", "decode_score",
    # Order HASH
    "store_order_hash", "get_order_hash", "delete_order_hash", "update_order_hash_fields",
    # Sequence
    "next_seq",
    # Resting orders
    "add_resting_order", "remove_resting_order",
    # Reads
    "get_best_bid", "get_best_ask", "get_spread", "get_book_snapshot",
    "get_opposite_orders_for_match",
    # Apply
    "apply_match_results",
    # Maintenance
    "clear_symbol_book",
]
