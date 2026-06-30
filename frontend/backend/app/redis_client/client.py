"""Async Redis connection pool (singleton).

Provides:
  * `get_redis()`  — FastAPI dependency yielding an `aioredis.Redis` instance.
  * `redis_client` — module-level singleton for use outside FastAPI deps
                     (matching worker, stop monitor, etc.).
  * `close_redis()` — call at app shutdown to release pooled connections.

Design notes
------------
* Uses `redis.asyncio` (redis-py 5.x native async; `aioredis` is merged in).
* `decode_responses=False` so we get raw `bytes` for performance-critical
  paths (order book, queues). Callers that need strings can decode manually
  or use the helper wrappers in `orderbook.py` / `queues.py`.
* Pool size is configurable via `Settings.redis_pool_size`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from redis.asyncio import Redis, from_url
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import RedisError

from app.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

_settings = get_settings()

# ─── Module-level singleton ──────────────────────────────────────────────────
# Lazily created on first access; reused across the process. The pool is
# thread-safe (redis-py uses asyncio locks internally), so the same client
# can be shared between the FastAPI event loop and the matching worker's
# event loop AS LONG AS they're the same loop. For separate processes
# (e.g., matching worker as a separate container), each process creates
# its own client via `get_redis_for_worker()`.

_redis_client: Redis | None = None
_redis_pool: ConnectionPool | None = None


def _build_pool() -> ConnectionPool:
    """Construct a connection pool from settings.

    Note: socket_timeout must be LARGER than the BRPOP timeout (5s) used by
    the matching worker, otherwise the socket timeout fires before BRPOP
    returns None, causing a spurious TimeoutError. We use 30s to give
    ample headroom for blocking commands + network latency.
    """
    return ConnectionPool.from_url(
        str(_settings.redis_url),
        decode_responses=False,
        max_connections=_settings.redis_pool_size,
        socket_keepalive=True,
        socket_connect_timeout=10,
        socket_timeout=30,
        retry_on_timeout=True,
        health_check_interval=30,
    )


def get_redis() -> Redis:
    """Return the module-level Redis singleton.

    Creates the client lazily on first call. Safe to call from any async
    context (FastAPI handler, matching worker, background task).
    """
    global _redis_client, _redis_pool
    if _redis_client is None:
        _redis_pool = _build_pool()
        _redis_client = Redis(connection_pool=_redis_pool)
    return _redis_client


async def get_redis_dep() -> Redis:
    """FastAPI dependency yielding the Redis singleton.

    Usage in a router::

        @router.get("/foo")
        async def handler(redis: Redis = Depends(get_redis_dep)):
            await redis.get("key")
    """
    yield get_redis()


async def close_redis() -> None:
    """Release all pooled connections. Call at app shutdown."""
    global _redis_client, _redis_pool
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None


def init_redis(app: "FastAPI") -> None:
    """Register startup/shutdown hooks on a FastAPI app.

    Usage::

        app = FastAPI()
        init_redis(app)
    """
    @app.on_event("startup")
    async def _startup() -> None:
        # Touch the client to force pool creation
        client = get_redis()
        # Verify connectivity (raises if Redis is down)
        try:
            await client.ping()
        except RedisError as e:
            raise RuntimeError(f"Cannot connect to Redis at {_settings.redis_url}: {e}") from e

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await close_redis()


__all__ = [
    "get_redis",
    "get_redis_dep",
    "close_redis",
    "init_redis",
]
