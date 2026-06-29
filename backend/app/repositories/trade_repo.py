"""Trade repository."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderSide
from app.models.trade import Trade
from app.repositories.base import BaseRepository


class TradeRepository(BaseRepository[Trade]):
    model = Trade

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def list_user_trades(
        self,
        user_id: int,
        *,
        symbol: str | None = None,
        side: OrderSide | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Sequence[Trade]:
        """List trades where the user is either taker or maker."""
        stmt = (
            select(Trade)
            .where((Trade.taker_user_id == user_id) | (Trade.maker_user_id == user_id))
        )
        if symbol is not None:
            stmt = stmt.where(Trade.symbol == symbol)
        if side is not None:
            stmt = stmt.where(Trade.side == side)
        if start is not None:
            stmt = stmt.where(Trade.executed_at >= start)
        if end is not None:
            stmt = stmt.where(Trade.executed_at <= end)
        stmt = stmt.order_by(Trade.executed_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_recent_by_symbol(
        self,
        symbol: str,
        limit: int = 100,
    ) -> Sequence[Trade]:
        """Most recent trades for a symbol (used for chart / stop monitor)."""
        stmt = (
            select(Trade)
            .where(Trade.symbol == symbol)
            .order_by(Trade.executed_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_last_price(self, symbol: str) -> float | None:
        """Get the most recent trade price for a symbol."""
        stmt = (
            select(Trade.price)
            .where(Trade.symbol == symbol)
            .order_by(Trade.executed_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return float(row) if row is not None else None
