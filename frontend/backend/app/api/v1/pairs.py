"""Public trading pair endpoints (no auth required)."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.deps import SessionDep
from app.models.trading_pair import TradingPair
from app.repositories.trading_pair_repo import TradingPairRepository

router = APIRouter(prefix="/pairs", tags=["market-data"])


class TradingPairPublicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    symbol: str
    base_asset: str
    quote_asset: str
    price_precision: int
    quantity_precision: int
    min_lot_size: Decimal
    max_lot_size: Decimal
    tick_size: Decimal
    is_active: bool


@router.get("", response_model=list[TradingPairPublicResponse])
async def list_public_pairs(session: SessionDep) -> list[TradingPairPublicResponse]:
    """List all active trading pairs. No auth required.

    Used by the frontend to populate the symbol dropdown dynamically.
    """
    repo = TradingPairRepository(session)
    pairs = await repo.list_active()
    return [TradingPairPublicResponse.model_validate(p) for p in pairs]


__all__ = ["router"]
