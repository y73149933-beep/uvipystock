"""Trade history REST endpoints."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import AuthenticatedUser, SessionDep, require_permission
from app.models.enums import OrderSide
from app.models.trade import Trade
from app.redis_client import get_redis, klines
from app.repositories.trade_repo import TradeRepository
from app.schemas.trade import TradeListResponse, TradeResponse

router = APIRouter(prefix="/trades", tags=["trades"])


def _trade_to_public(t: Trade) -> dict:
    """Serialize a Trade row into the public-feed contract.

    ``ts`` is epoch MILLISECONDS (canonical client-side unit, shared with
    WS trade prints). Naive datetimes (possible from SQLite test doubles)
    are interpreted as UTC; PostgreSQL's timestamptz returns aware values
    that keep their offset.
    """
    executed = t.executed_at
    if executed.tzinfo is None:
        executed = executed.replace(tzinfo=UTC)
    return {
        "trade_id": t.id,
        "symbol": t.symbol,
        "price": float(t.price),
        "quantity": float(t.quantity),
        "side": t.side.value,
        "ts": int(executed.timestamp() * 1000),
    }



@router.get(
    "",
    response_model=TradeListResponse,
    dependencies=[Depends(require_permission("read"))],
)
async def list_trades(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    symbol: str | None = Query(None),
    side: OrderSide | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> TradeListResponse:
    """List the user's trade history (as taker or maker)."""
    repo = TradeRepository(session)
    trades = await repo.list_user_trades(
        user_id,
        symbol=symbol,
        side=side,
        start=start,
        end=end,
        offset=offset,
        limit=limit,
    )

    result = []
    for t in trades:
        if t.taker_user_id == user_id:
            role = "taker"
            order_id = t.taker_order_id
            fee = t.taker_fee
        else:
            role = "maker"
            order_id = t.maker_order_id
            fee = t.maker_fee

        result.append(TradeResponse(
            id=t.id,
            symbol=t.symbol,
            side=t.side,
            price=t.price,
            quantity=t.quantity,
            quote_quantity=t.quote_quantity,
            role=role,  # type: ignore[arg-type]
            fee=fee,
            order_id=order_id,
            executed_at=t.executed_at,
        ))

    return TradeListResponse(
        trades=result,
        pagination={"offset": offset, "limit": limit, "count": len(result)},
    )


@router.get(
    "/public/{symbol:path}",
    dependencies=[Depends(require_permission("read"))],
)
async def list_public_trades(
    request: Request,
    user_id: AuthenticatedUser,  # require auth but ignore user_id
    session: SessionDep,
    symbol: str,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """List recent public trades for a symbol (all users, not just the caller).

    Used by the frontend to populate the trades feed on page load (before
    WebSocket events start arriving).
    """
    repo = TradeRepository(session)
    trades = await repo.list_recent_by_symbol(symbol, limit=limit)
    return {"trades": [_trade_to_public(t) for t in reversed(trades)]}


@router.get(
    "/candles/{symbol:path}",
    dependencies=[Depends(require_permission("read"))],
)
async def get_candles(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    symbol: str,
    timeframe: str = Query("1m", description="1m, 5m, 15m, 1h, 4h, 1d"),
    limit: int = Query(500, ge=1, le=1000),
    end: int | None = Query(
        None, ge=0,
        description="Pagination cursor (unix seconds). When set, returns the "
                    "`limit` candles strictly older than this bucket.",
    ),
) -> dict:
    """OHLCV candles for `symbol`, served from the Redis kline cache.

    The cache (app.redis_client.klines) is kept warm incrementally by the
    settlement path; windows older than anything cached are aggregated
    from the trades table exactly once and materialized into the cache.

    Pagination: pass ``end`` = ``time`` of the oldest candle you have to
    page further back. The response carries ``has_more`` (does PG hold
    trades older than the returned window) so the client knows when to
    stop requesting.
    """
    tf_seconds = klines.TF_SECONDS.get(timeframe, 60)
    redis = get_redis()

    now_bucket = klines.bucket_of(datetime.now(UTC).timestamp(), tf_seconds)
    if end is not None:
        hi_exclusive = klines.bucket_of(end, tf_seconds)
        if hi_exclusive > now_bucket + tf_seconds:
            hi_exclusive = now_bucket + tf_seconds
    else:
        hi_exclusive = now_bucket + tf_seconds
    lo = hi_exclusive - tf_seconds * limit

    await klines.ensure_window(session, redis, symbol, timeframe, lo, hi_exclusive)
    candles = await klines.get_range(redis, symbol, timeframe, lo, hi_exclusive - tf_seconds, limit)
    more = await klines.has_older_trades(session, symbol, lo)

    return {
        "candles": candles,
        "symbol": symbol,
        "timeframe": timeframe,
        "has_more": more,
        # cursor for the next (older) page = left edge of this window, even
        # if the window contained no trades (sparse history must still page
        # forward — otherwise clients loop forever on an empty window)
        "next_end": lo,
    }


__all__ = ["router"]
