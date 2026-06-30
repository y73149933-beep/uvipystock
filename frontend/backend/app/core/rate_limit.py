"""Rate limiting FastAPI dependency.

Uses the Redis-backed sliding-window rate limiter from
`app.redis_client.rate_limit`. Returns a 429 with `Retry-After` header
when the limit is exceeded.
"""
from __future__ import annotations

from fastapi import Depends, Request

from app.config import get_settings
from app.core.exceptions import RateLimitExceededError
from app.redis_client import get_redis, rate_limit

_settings = get_settings()


async def enforce_rate_limit(
    request: Request,
    api_key: str = "",  # filled by the auth dependency
) -> None:
    """FastAPI dependency: enforce per-API-key rate limiting.

    Reads `api_key` from `request.state.api_key` (set by the auth dep).
    """
    # The api_key is set on request.state by the HMAC auth dependency.
    key = getattr(request.state, "api_key", None) or "anonymous"
    limit = getattr(request.state, "rate_limit_per_min", _settings.default_rate_limit_per_min)

    redis = get_redis()
    result, current_count, retry_after = await rate_limit.check_and_consume(
        redis, api_key=key, limit_per_min=limit,
    )

    # Attach headers to the response via request.state (the response middleware
    # or the route handler can read these)
    request.state.rate_limit_limit = limit
    request.state.rate_limit_remaining = max(0, limit - current_count)
    request.state.rate_limit_retry_after = retry_after if result == "rejected" else 0

    if result == "rejected":
        raise RateLimitExceededError(
            message=f"Rate limit exceeded: {current_count}/{limit} requests per minute",
            details={
                "limit": limit,
                "current": current_count,
                "retry_after_seconds": retry_after,
            },
        )


__all__ = ["enforce_rate_limit"]
