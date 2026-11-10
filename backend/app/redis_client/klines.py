"""OHLCV candle cache keyed by (symbol, timeframe).

The ``trades`` table is the source of truth; this module keeps an
incremental Redis view of it: one sorted set per (symbol, timeframe) with
``score = bucket start (unix seconds)`` and ``member = compact JSON
candle``. The settlement path bumps the affected bucket on every fill
(:func:`apply_trades`), and :func:`ensure_window` lazily backfills any
window that has never been materialized from PG, so a chart request never
re-aggregates the same span of trades twice. A sentinel key
``kline:{symbol}:{tf}:seeded_from`` records the oldest bucket (unix
seconds) ever materialized — but coverage is verified against the cache
itself, never the sentinel alone: the 50k-bucket cap trims old buckets,
so a sentinel that merely claims coverage would permanently hide those
windows from re-materialization. The cache key therefore carries no idle
TTL: memory is bounded by the cap (6 timeframes x 50k compact candles
is ~10MB), while an expiry would strand the sentinel over a dead key.

Concurrency model: exactly one worker settles trades per deployment
(single-writer assumption), so bucket read-modify-write needs no lock.
Window backfill overwrites cached buckets with PG aggregates; a live bump
that races the backfill by milliseconds is overwritten but self-heals —
the trade row is already committed, so the next read of that bucket comes
from truth. Readers (REST) only ever see complete JSON members.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade import Trade
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

TF_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

# Hard cap per (symbol, timeframe) so the cache cannot grow unbounded on
# 1m buckets; oldest buckets are dropped first.
MAX_CANDLES_PER_TF = 50_000


def bucket_of(ts_seconds: float, tf_seconds: int) -> int:
    return (int(ts_seconds) // tf_seconds) * tf_seconds


def kline_key(symbol: str, tf: str) -> str:
    return f"kline:{symbol}:{tf}"


def seeded_key(symbol: str, tf: str) -> str:
    return f"kline:{symbol}:{tf}:seeded_from"


def bump(candle: dict[str, float] | None, price: float, qty: float) -> dict[str, float]:
    """Fold one trade into a candle (pure; unit-testable)."""
    if candle is None:
        return {"open": price, "high": price, "low": price, "close": price, "volume": qty}
    return {
        "open": candle["open"],
        "high": max(candle["high"], price),
        "low": min(candle["low"], price),
        "close": price,
        "volume": candle["volume"] + qty,
    }


async def apply_trades(
    redis: Redis, symbol: str, items: Sequence[tuple[float, float, float]]
) -> list[dict[str, Any]]:
    """Fold fills into the current bucket of every timeframe.

    ``items`` is a sequence of ``(price, quantity, ts_seconds)``.
    Returns the updated candle dicts (one per (tf, bucket) touched) —
    ready to be published as kline events later.
    """
    if not items:
        return []

    # Group trades by (tf, bucket) so each touched bucket costs one fetch
    # and one write regardless of how many fills landed in it.
    groups: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for price, qty, ts in items:
        for tf, tfs in TF_SECONDS.items():
            groups.setdefault((tf, bucket_of(ts, tfs)), []).append((price, qty))

    order = list(groups)
    pipe = redis.pipeline(transaction=False)
    for tf, bucket in order:
        pipe.zrangebyscore(kline_key(symbol, tf), bucket, bucket)
    current = await pipe.execute()

    updated: list[dict[str, Any]] = []
    pipe = redis.pipeline(transaction=False)
    for (tf, bucket), existing in zip(order, current, strict=True):
        candle: dict[str, float] | None = None
        if existing:
            try:
                candle = json.loads(existing[0])
            except (ValueError, TypeError):
                candle = None
        old_member = existing[0] if existing else None
        for price, qty in groups[(tf, bucket)]:
            candle = bump(candle, price, qty)
        assert candle is not None
        key = kline_key(symbol, tf)
        if old_member is not None:
            pipe.zrem(key, old_member)
        member = json.dumps({"t": bucket, **candle}, separators=(",", ":"))
        pipe.zadd(key, {member: bucket})
        pipe.zremrangebyrank(key, 0, -(MAX_CANDLES_PER_TF + 1))
        updated.append({"tf": tf, "bucket": bucket, **candle})
    await pipe.execute()
    return updated


async def aggregate_window(
    session: AsyncSession, redis: Redis, symbol: str, tf: str,
    lo_bucket: int, hi_bucket_exclusive: int,
) -> int:
    """Materialize candles for [lo, hi) from PG into the cache.

    Returns the number of buckets written. Existing cached buckets inside
    the window are replaced by the PG truth (see module docstring).
    """
    tfs = TF_SECONDS[tf]
    stmt = (
        select(Trade.executed_at, Trade.price, Trade.quantity)
        .where(
            Trade.symbol == symbol,
            Trade.executed_at >= _ts(lo_bucket),
            Trade.executed_at < _ts(hi_bucket_exclusive),
        )
        .order_by(Trade.executed_at.asc())
    )
    rows = (await session.execute(stmt)).all()

    candles: dict[int, dict[str, float]] = {}
    for executed_at, price, quantity in rows:
        b = bucket_of(executed_at.timestamp(), tfs)
        candles[b] = bump(candles.get(b), float(price), float(quantity))

    if candles:
        key = kline_key(symbol, tf)
        pipe = redis.pipeline(transaction=False)
        for b, candle in candles.items():
            member = json.dumps({"t": b, **candle}, separators=(",", ":"))
            # drop any stale cached member for this bucket, then write truth
            pipe.zremrangebyscore(key, b, b)
            pipe.zadd(key, {member: b})
        pipe.zremrangebyrank(key, 0, -(MAX_CANDLES_PER_TF + 1))
        await pipe.execute()

    prev = await redis.get(seeded_key(symbol, tf))
    prev_val = int(prev) if prev is not None else None
    new_val = lo_bucket if prev_val is None else min(prev_val, lo_bucket)
    await redis.set(seeded_key(symbol, tf), str(new_val))
    return len(candles)


async def ensure_window(
    session: AsyncSession, redis: Redis, symbol: str, tf: str,
    lo_bucket: int, hi_bucket_exclusive: int,
) -> None:
    """Backfill the cache only if the requested window reaches older than
    anything ever materialized."""
    prev = await redis.get(seeded_key(symbol, tf))
    if prev is not None and lo_bucket >= int(prev):
        oldest = await redis.zrange(kline_key(symbol, tf), 0, 0, withscores=True)
        if oldest and float(oldest[0][1]) <= lo_bucket:
            return  # window truly covered: trim only ever drops the oldest
        if not oldest and not await has_older_trades(session, symbol, hi_bucket_exclusive):
            return  # empty cache over a symbol that simply has no older trades
        # else: the cap trimmed into this window (or the key died): the
        # sentinel must not block backfill. Windows outside the cap pay
        # one PG re-aggregation per read — that is the cap's price.
    written = await aggregate_window(session, redis, symbol, tf, lo_bucket, hi_bucket_exclusive)
    logger.debug(
        "klines seeded %s %s [%d,%d): %d buckets", symbol, tf, lo_bucket, hi_bucket_exclusive, written,
    )


def fill_gaps(
    candles: list[dict[str, Any]], tf_seconds: int,
    lo_bucket: int, hi_bucket_inclusive: int, seed_close: float | None,
) -> list[dict[str, Any]]:
    """Insert synthetic flat bars into buckets that hold no trades.

    A gap is filled only where trading had already started: between two
    returned candles, after the last candle up to the window edge, and at
    the window start when ``seed_close`` (close of the last candle just
    older than ``lo_bucket``) is known. A synthetic bar carries
    open=high=low=close=previous close with volume=0, so idle periods
    render as a continuous flat stretch instead of missing time.
    Buckets before the very first trade are never invented.
    Pure function; output stays ascending.
    """
    def flat(bucket: int, close: float) -> dict[str, Any]:
        return {"time": bucket, "open": close, "high": close,
                "low": close, "close": close, "volume": 0.0}

    out: list[dict[str, Any]] = []
    prev_close = seed_close
    prev_t = lo_bucket - tf_seconds  # leading fill starts at lo_bucket
    for c in candles:
        if prev_close is not None:
            b = prev_t + tf_seconds
            while b < c["time"]:
                out.append(flat(b, prev_close))
                b += tf_seconds
        out.append(c)
        prev_close = c["close"]
        prev_t = c["time"]
    if prev_close is not None:
        b = prev_t + tf_seconds
        while b <= hi_bucket_inclusive:
            out.append(flat(b, prev_close))
            b += tf_seconds
    return out


async def get_range(
    redis: Redis, symbol: str, tf: str, lo_bucket: int, hi_bucket_inclusive: int, limit: int,
) -> list[dict[str, Any]]:
    """Candles in [lo, hi] ascending (at most ``limit`` real candles, plus
    synthetic flat bars filling trade-less buckets - see :func:`fill_gaps`,
    so the synthetic output can exceed ``limit`` by up to the window's
    bucket count)."""
    key = kline_key(symbol, tf)
    tf_s = TF_SECONDS[tf]
    # Guard against caller windows far beyond the cache capacity: an
    # unbounded window would turn gap filling into a memory bomb.
    if hi_bucket_inclusive - lo_bucket > tf_s * MAX_CANDLES_PER_TF:
        lo_bucket = hi_bucket_inclusive - tf_s * MAX_CANDLES_PER_TF
    members = await redis.zrevrangebyscore(
        key, hi_bucket_inclusive, lo_bucket, start=0, num=limit,
    )
    out: list[dict[str, Any]] = []
    for m in reversed(members):
        try:
            d = json.loads(m)
            out.append({
                "time": d["t"], "open": d["open"], "high": d["high"],
                "low": d["low"], "close": d["close"], "volume": d["volume"],
            })
        except (ValueError, TypeError, KeyError):
            logger.warning("dropping malformed kline member for %s %s", symbol, tf)

    # Close of the newest candle before the window: seeds the leading
    # synthetic run (trading already started) and page-join continuity.
    seed_close: float | None = None
    seed = await redis.zrevrangebyscore(key, lo_bucket - 1, "-inf", start=0, num=1)
    if seed:
        try:
            seed_close = float(json.loads(seed[0])["close"])
        except (ValueError, TypeError, KeyError):
            seed_close = None

    return fill_gaps(out, tf_s, lo_bucket, hi_bucket_inclusive, seed_close)


async def has_older_trades(
    session: AsyncSession, symbol: str, before_bucket: int
) -> bool:
    """True if PG holds any trade for the symbol older than ``before_bucket``."""
    stmt = (
        select(1).where(Trade.symbol == symbol, Trade.executed_at < _ts(before_bucket)).limit(1)
    )
    return (await session.execute(stmt)).first() is not None


def _ts(bucket: int):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(bucket, tz=UTC)


__all__ = [
    "TF_SECONDS", "MAX_CANDLES_PER_TF", "bucket_of", "kline_key", "seeded_key",
    "bump", "apply_trades", "aggregate_window", "ensure_window", "get_range",
    "fill_gaps",
    "has_older_trades",
]
