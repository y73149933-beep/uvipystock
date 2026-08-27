"""Public market-data REST endpoints.

Currently hosts the aggregated conditional-order ladder ("stop book").
Anonymous read-only endpoint — no authentication required.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query

from app.redis_client import get_redis
from app.redis_client import stops as stops_redis
from app.schemas.market import StopLadderEntry, StopLadderResponse

router = APIRouter(prefix="/market", tags=["market"])


@router.get(
    "/stops",
    response_model=StopLadderResponse,
)
async def get_stop_ladder(
    symbol: str = Query(..., min_length=1, max_length=30),
) -> StopLadderResponse:
    """Aggregated public ladder of pending conditional orders for `symbol`.

    Groups every pending stop_market / stop_limit / trailing_stop (all
    users, anonymized) by trigger price and side. Volume is the sum of
    remaining quantities behind each level.

    The trading pair is passed as a query parameter because symbols like
    ``BTC/USD`` contain a slash, which should not appear inside a path.
    """
    redis = get_redis()
    rows = await stops_redis.get_stop_ladder(redis, symbol)

    entries = [StopLadderEntry(**r) for r in rows]
    total_volume = sum((Decimal(e.volume) for e in entries), Decimal("0"))
    return StopLadderResponse(
        symbol=symbol,
        levels=entries,
        total_count=sum(e.count for e in entries),
        total_volume=format(total_volume, "f"),
    )


__all__ = ["router"]
