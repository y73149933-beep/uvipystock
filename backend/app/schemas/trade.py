"""Trade API schemas."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.enums import OrderSide


class TradeResponse(BaseModel):
    """Single trade in API responses."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    role: Literal["taker", "maker"]
    fee: Decimal
    order_id: int
    executed_at: datetime


class TradeListResponse(BaseModel):
    """Response for GET /api/v1/trades."""
    trades: list[TradeResponse]
    pagination: dict


__all__ = ["TradeResponse", "TradeListResponse"]
