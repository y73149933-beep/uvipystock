"""Balance API schemas."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class BalanceResponse(BaseModel):
    """Single asset balance."""
    model_config = ConfigDict(from_attributes=True)

    asset: str
    total: Decimal
    locked: Decimal
    available: Decimal
    updated_at: datetime | None = None


class BalanceListResponse(BaseModel):
    """Response for GET /api/v1/balance."""
    balances: list[BalanceResponse]


__all__ = ["BalanceResponse", "BalanceListResponse"]
