"""Sliding-window rate limiter backed by Redis.

Strategy
--------
We use a **sorted-set sliding window**: for each API key, store the
timestamps of recent requests as ZSET members, with the timestamp as
the score. On each request:

  1. ``ZREMRANGEBYSCORE key 0 (now - window)``  — evict old entries
  2. ``ZCARD key``                              — count current entries
  3. If count < limit: ``ZADD key {uniq_member} {now}`` and allow
  4. Else: reject with 429

We also set a TTL on the key so it expires if no requests arrive for
a while (avoids unbounded key growth).

This is more accurate than a fixed-window counter and atomic enough
for our purposes (the small race window between ZCARD and ZADD is
acceptable — a few extra requests per minute is fine).

For higher precision, wrap steps 1-3 in a Lua script (TODO).
"""
from __future__ import annotations

import time
from typing import Literal

from redis.asyncio import Redis


# ─── Keys ────────────────────────────────────────────────────────────────────

RATELIMIT_KEY = "ratelimit:{api_key}"


def ratelimit_key(api_key: str) -> str:
    return RATELIMIT_KEY.format(api_key=api_key)


# ─── Result type ─────────────────────────────────────────────────────────────

RateLimitResult = Literal["allowed", "rejected"]


# ─── Core check ──────────────────────────────────────────────────────────────

async def check_and_consume(
    redis: Redis,
    api_key: str,
    limit_per_min: int,
    window_seconds: int = 60,
) -> tuple[RateLimitResult, int, int]:
    """Consume one unit from the rate-limit bucket.

    Returns ``(result, current_count, retry_after_seconds)``:
      * ``result``           — "allowed" or "rejected"
      * ``current_count``    — number of requests in the current window
      * ``retry_after_seconds`` — seconds until the oldest entry expires
        (only meaningful when rejected; 0 when allowed)

    The caller should set ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``,
    and ``Retry-After`` headers on the HTTP response based on these values.
    """
    key = ratelimit_key(api_key)
    now = time.time()
    window_start = now - window_seconds

    pipe = redis.pipeline()
    # 1. Evict entries older than the window
    pipe.zremrangebyscore(key, 0, window_start)
    # 2. Count current entries
    pipe.zcard(key)
    # 3. Set TTL to clean up idle keys (window + small buffer)
    pipe.expire(key, window_seconds + 10)
    results = await pipe.execute()

    current_count = int(results[1])

    if current_count < limit_per_min:
        # Allowed: add this request
        member = f"{now:.6f}"  # microsecond-precision unique member
        await redis.zadd(key, {member: now})
        return "allowed", current_count + 1, 0

    # Rejected: find the oldest entry to compute retry_after
    oldest = await redis.zrange(key, 0, 0, withscores=True)
    if oldest:
        oldest_score = oldest[0][1]
        retry_after = max(1, int(oldest_score + window_seconds - now) + 1)
    else:
        retry_after = 1
    return "rejected", current_count, retry_after


async def get_current_count(
    redis: Redis,
    api_key: str,
    window_seconds: int = 60,
) -> int:
    """Return the current request count for `api_key` without consuming.

    Useful for dashboards / metrics. Evicts expired entries first.
    """
    key = ratelimit_key(api_key)
    now = time.time()
    window_start = now - window_seconds

    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zcard(key)
    results = await pipe.execute()
    return int(results[1])


async def reset(redis: Redis, api_key: str) -> None:
    """Clear the rate-limit bucket for an API key (admin operation)."""
    await redis.delete(ratelimit_key(api_key))


__all__ = [
    "ratelimit_key",
    "check_and_consume",
    "get_current_count",
    "reset",
]
