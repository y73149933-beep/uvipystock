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

NOTE: triggers are driven exclusively by trade prints. The live book
(best bid/ask) NEVER arms a stop — book depth matters only for executing
the child order AFTER activation. Touch/book-driven arming deliberately
does not exist anymore: coupling activation to resting liquidity turns
every beyond-market stop into an executable-now taker ("stop-as-limit").

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
from app.redis_client.orderbook import ORDER_HASH_KEY, SCORE_MULTIPLIER, order_hash_key


def _to_str(value: Any) -> str:
    """Decode a Redis reply (bytes or str) into a plain str."""
    return value.decode() if isinstance(value, bytes) else str(value)


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
) -> list[tuple[int, str]]:
    """Return ``(order_id, side)`` pairs for stops whose trigger has FIRED.

    Directional rules (one shared ZSET holds both sides, score = stop_price):
      * SELL-stop fires when ``last_trade_price <= stop_price``
        → candidates live at ``score >= price_score``;
      * BUY-stop fires when ``last_trade_price >= stop_price``
        → candidates live at ``score <= price_score``.

    A range query alone cannot discriminate the side — every member of the
    ZSET falls into one of the two ranges regardless of whether it fired.
    Therefore each candidate's order HASH (``order:{id}.side``) is checked
    before it is reported. Members without a HASH (phantom entries left by
    a rolled-back placement) are skipped here so the monitor never fires
    them; reconcile flows may clean them up separately.
    """
    key = stops_key(symbol)
    price_score = float(last_trade_price) * SCORE_MULTIPLIER

    # Candidates by price position (both sides mixed together)
    above = await redis.zrangebyscore(key, price_score, "+inf")  # sell candidates
    below = await redis.zrangebyscore(key, 0, price_score)       # buy candidates

    # Preserve ascending-score traversal order and deduplicate ids
    ordered_ids: list[int] = []
    seen: set[int] = set()
    for m in list(above) + list(below):
        oid = int(m)
        if oid not in seen:
            seen.add(oid)
            ordered_ids.append(oid)
    if not ordered_ids:
        return []

    sides_raw = await _get_order_sides(redis, ordered_ids)

    sell_candidates = {int(m) for m in above}
    buy_candidates = {int(m) for m in below}

    triggered: list[tuple[int, str]] = []
    for oid in ordered_ids:
        side_raw = sides_raw.get(oid)
        if side_raw is None:
            continue  # no HASH → phantom entry, skip
        side = _to_str(side_raw).lower()
        if side == OrderSide.SELL.value and oid in sell_candidates:
            triggered.append((oid, side))
        elif side == OrderSide.BUY.value and oid in buy_candidates:
            triggered.append((oid, side))
    return triggered


async def _get_order_sides(
    redis: Redis,
    order_ids: list[int],
) -> dict[int, Any]:
    """Bulk-load ``order:{id}.side`` for the given ids (single round-trip).

    Returns ``{order_id: raw_side_or_None}``; raw value stays bytes/str as
    returned by the client so callers can decode consistently.
    """
    pipe = redis.pipeline(transaction=False)
    for oid in order_ids:
        pipe.hget(ORDER_HASH_KEY.format(order_id=oid), "side")
    replies = await pipe.execute() if order_ids else []
    return dict(zip(order_ids, replies, strict=True))


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
) -> list[tuple[int, str]]:
    """Return ``(order_id, side)`` pairs for trailing stops that have fired.

    Same directional filtering as :func:`get_all_triggered_stops`, but the
    authoritative side lives in the trailing-state HASH
    (``trailing_state:{id}.side``), which tracks the drifting trigger.
    """
    t_key = trailing_key(symbol)
    price_score = float(last_trade_price) * SCORE_MULTIPLIER

    above = await redis.zrangebyscore(t_key, price_score, "+inf")  # sell candidates
    below = await redis.zrangebyscore(t_key, 0, price_score)       # buy candidates

    ordered_ids: list[int] = []
    seen: set[int] = set()
    for m in list(above) + list(below):
        oid = int(m)
        if oid not in seen:
            seen.add(oid)
            ordered_ids.append(oid)
    if not ordered_ids:
        return []

    pipe = redis.pipeline(transaction=False)
    for oid in ordered_ids:
        pipe.hget(trailing_state_key(oid), "side")
    sides_raw = await pipe.execute()

    sell_candidates = {int(m) for m in above}
    buy_candidates = {int(m) for m in below}

    triggered: list[tuple[int, str]] = []
    for oid, side_raw in zip(ordered_ids, sides_raw, strict=True):
        if side_raw is None:
            continue  # state lost — cannot verify direction
        side = _to_str(side_raw).lower()
        if side == OrderSide.SELL.value and oid in sell_candidates:
            triggered.append((oid, side))
        elif side == OrderSide.BUY.value and oid in buy_candidates:
            triggered.append((oid, side))
    return triggered


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


# ─── Public stop ladder (aggregated visibility) ─────────────────────────────

# The stops ZSET encodes its score through the shared SCORE_MULTIPLIER
SCORE_MULTIPLIER_STOPS = SCORE_MULTIPLIER


def _fmt_dec(value: str | float | Decimal) -> str:
    """Render a numeric value as a fixed-point string without exponent."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(d, "f")


async def get_stop_ladder(redis: Redis, symbol: str) -> list[dict[str, Any]]:
    """Aggregate ALL pending conditional orders of `symbol` into a public
    stop ladder (like a "stop book").

    Groups plain stops (``stops:{symbol}``) and trailing stops
    (``trailing:{symbol}``) by ``(side, type, trigger_price)`` and returns
    rows ``{"side","type","price","volume","count"}`` sorted:

      * SELL rows first, descending by price (sits above the market);
      * BUY rows after, descending by price (below the market, firing up).

    Volume = quantity - filled_quantity from the order HASH (stops do not
    partially fill before triggering, so this is normally the full size).
    Members without an order HASH (phantoms) are skipped.

    NOTE: this is intentionally anonymous — no user ids are exposed, only
    aggregated sizes per trigger level.
    """
    ladder: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def _absorb(
        members_scores: list[tuple[Any, float]],
        *,
        trailing: bool,
    ) -> None:
        if not members_scores:
            return
        pipe = redis.pipeline(transaction=False)
        for member, _score in members_scores:
            oid = int(member)
            pipe.hgetall(order_hash_key(oid))
            if trailing:
                pipe.hgetall(trailing_state_key(oid))
        replies = await pipe.execute()

        idx = 0
        for member, score in members_scores:
            oid = int(member)
            meta_raw = replies[idx]
            idx += 1
            state_raw: dict[Any, Any] = {}
            if trailing:
                state_raw = replies[idx]
                idx += 1

            meta = {
                k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                for k, v in (meta_raw or {}).items()
            }

            side = meta.get("side")
            otype = meta.get("type") or ("trailing_stop" if trailing else None)
            if side not in ("buy", "sell"):
                continue

            # Trigger price resolution:
            #   * plain stop    → HASH stop_price (fallback: ZSET score)
            #   * trailing stop → trailing_state current_trigger, else score
            trigger: Decimal | None = None
            if trailing and state_raw:
                ct = _to_str(state_raw.get(b"current_trigger") or
                             state_raw.get("current_trigger") or "")
                if ct:
                    try:
                        trigger = Decimal(ct)
                    except Exception:
                        trigger = None
            if trigger is None:
                sp = meta.get("stop_price")
                if sp:
                    try:
                        trigger = Decimal(sp)
                    except Exception:
                        continue
                elif not trailing:
                    # recover from ZSET score (price*1e9 in stops key context;
                    # NOTE: for ob-encoded hashes the score encoding differs,
                    # so this fallback is best-effort for plain stops only)
                    trigger = Decimal(str(score / SCORE_MULTIPLIER_STOPS))
                else:
                    continue

            qty_str = meta.get("quantity") or "0"
            filled_str = meta.get("filled_quantity") or "0"
            try:
                volume = Decimal(qty_str) - Decimal(filled_str)
            except Exception:
                continue
            if volume <= 0 or trigger <= 0:
                continue

            tside = str(side)
            ttype = str(otype) if otype in (
                "stop_market", "stop_limit", "trailing_stop",
            ) else "stop_market"
            key = (tside, ttype, _fmt_dec(trigger))
            row = ladder.get(key)
            if row is None:
                row = {
                    "side": tside, "type": ttype,
                    "price": key[2], "volume": Decimal("0"), "count": 0,
                }
                ladder[key] = row
            row["volume"] += volume
            row["count"] += 1

    plain = await redis.zrange(stops_key(symbol), 0, -1, withscores=True)
    await _absorb(plain, trailing=False)

    trails = await redis.zrange(trailing_key(symbol), 0, -1, withscores=True)
    await _absorb(trails, trailing=True)

    rows = list(ladder.values())
    sells = sorted(
        (r for r in rows if r["side"] == "sell"),
        key=lambda r: Decimal(r["price"]), reverse=True,
    )
    buys = sorted(
        (r for r in rows if r["side"] == "buy"),
        key=lambda r: Decimal(r["price"]), reverse=True,
    )
    out: list[dict[str, Any]] = []
    for r in [*sells, *buys]:
        out.append({
            "side": r["side"], "type": r["type"],
            "price": r["price"],
            "volume": _fmt_dec(r["volume"]),
            "count": r["count"],
        })
    return out


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
    # Public stop ladder
    "get_stop_ladder",
    # Maintenance
    "clear_stops_for_symbol",
]
