"""Trading pair repository."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading_pair import TradingPair
from app.repositories.base import BaseRepository


class TradingPairRepository(BaseRepository[TradingPair]):
    model = TradingPair

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_symbol(self, symbol: str) -> TradingPair | None:
        """Fetch a trading pair by its symbol (e.g. 'BTC/USDT')."""
        stmt = select(TradingPair).where(TradingPair.symbol == symbol)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self) -> Sequence[TradingPair]:
        """All active trading pairs."""
        stmt = select(TradingPair).where(TradingPair.is_active == True).order_by(TradingPair.symbol)  # noqa: E712
        result = await self.session.execute(stmt)
        return result.scalars().all()
