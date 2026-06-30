"""Trade history REST endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.api.deps import AuthenticatedUser, SessionDep, require_permission
from app.models.enums import OrderSide
from app.models.trade import Trade
from app.repositories.trade_repo import TradeRepository
from app.schemas.trade import TradeListResponse, TradeResponse

router = APIRouter(prefix="/trades", tags=["trades"])


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
    return {
        "trades": [
            {
                "trade_id": t.id,
                "symbol": t.symbol,
                "price": float(t.price),
                "quantity": float(t.quantity),
                "side": t.side.value,
                "ts": int(t.executed_at.timestamp() * 1000),  # ms
            }
            for t in reversed(trades)  # most recent first
        ],
    }


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
) -> dict:
    """Aggregate historical trades into OHLCV candles.

    Queries the trades table for the last `limit * timeframe_seconds` period,
    groups them into time buckets, and returns OHLCV data for charting.
    """
    tf_map = {
        "1m": 60, "5m": 300, "15m": 900,
        "1h": 3600, "4h": 14400, "1d": 86400,
    }
    tf_seconds = tf_map.get(timeframe, 60)

    # Calculate the start time: limit candles back from now
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=tf_seconds * limit)

    # Query all trades for this symbol since `start`
    stmt = (
        select(Trade)
        .where(Trade.symbol == symbol, Trade.executed_at >= start)
        .order_by(Trade.executed_at.asc())
    )
    result = await session.execute(stmt)
    trades = result.scalars().all()

    if not trades:
        return {"candles": [], "symbol": symbol, "timeframe": timeframe}

    # Aggregate trades into candles
    candles: list[dict] = []
    current_bucket: int | None = None
    current_candle: dict | None = None

    for trade in trades:
        # Calculate bucket time (unix seconds, floored to timeframe)
        trade_ts = int(trade.executed_at.timestamp())
        bucket = (trade_ts // tf_seconds) * tf_seconds

        if current_bucket is None or bucket != current_bucket:
            # Close previous candle
            if current_candle is not None:
                candles.append(current_candle)

            # Start new candle
            current_bucket = bucket
            current_candle = {
                "time": bucket,
                "open": float(trade.price),
                "high": float(trade.price),
                "low": float(trade.price),
                "close": float(trade.price),
                "volume": float(trade.quantity),
            }
        else:
            # Update current candle
            price = float(trade.price)
            current_candle["high"] = max(current_candle["high"], price)
            current_candle["low"] = min(current_candle["low"], price)
            current_candle["close"] = price
            current_candle["volume"] += float(trade.quantity)

    # Don't forget the last candle
    if current_candle is not None:
        candles.append(current_candle)

    # Limit to `limit` candles (keep the most recent)
    if len(candles) > limit:
        candles = candles[-limit:]

    return {
        "candles": candles,
        "symbol": symbol,
        "timeframe": timeframe,
    }


__all__ = ["router"]
