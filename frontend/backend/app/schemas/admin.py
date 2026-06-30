"""Admin panel API schemas."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ─── User management ─────────────────────────────────────────────────────────

class AdminUserCreateRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    is_admin: bool = False


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    is_active: bool
    is_admin: bool
    created_at: datetime


class AdminUserListResponse(BaseModel):
    users: list[AdminUserResponse]
    pagination: dict


# ─── Balance management ──────────────────────────────────────────────────────

class AdminBalanceAdjustRequest(BaseModel):
    user_id: int
    asset: str
    delta: Decimal = Field(..., description="Signed: positive=credit, negative=debit")
    reason: str = "admin_adjustment"


class AdminBalanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: int
    asset: str
    total: Decimal
    locked: Decimal
    available: Decimal
    updated_at: datetime


# ─── Market management ───────────────────────────────────────────────────────

class AdminTradingPairCreateRequest(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=20)
    base_asset: str
    quote_asset: str
    price_precision: int = Field(..., ge=0, le=18)
    quantity_precision: int = Field(..., ge=0, le=18)
    min_lot_size: Decimal = Field(..., gt=0)
    max_lot_size: Decimal = Field(..., gt=0)
    tick_size: Decimal = Field(..., gt=0)
    maker_fee_bps: Decimal = Decimal("0")
    taker_fee_bps: Decimal = Decimal("0")


class AdminTradingPairResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    symbol: str
    base_asset: str
    quote_asset: str
    price_precision: int
    quantity_precision: int
    min_lot_size: Decimal
    max_lot_size: Decimal
    tick_size: Decimal
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    is_active: bool
    created_at: datetime


# ─── API key management ──────────────────────────────────────────────────────

class AdminApiKeyCreateRequest(BaseModel):
    user_id: int
    label: str | None = None
    permissions: list[Literal["trade", "read", "ws"]] = ["trade", "read", "ws"]
    rate_limit_per_min: int = 120


class AdminApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    api_key: str
    label: str | None
    permissions: list[str]
    rate_limit_per_min: int
    is_revoked: bool
    created_at: datetime


class AdminApiKeyCreateResponse(AdminApiKeyResponse):
    """Includes the raw secret — only available at creation time."""
    secret: str


# ─── Emulator ────────────────────────────────────────────────────────────────

class AdminEmulatorRandomWalkRequest(BaseModel):
    symbol: str
    start_price: Decimal
    volatility_pct: Decimal = Field(Decimal("0.5"), description="Std dev per step as % of price")
    steps: int = Field(100, ge=1, le=10000)
    interval_ms: int = Field(100, ge=10, le=60000)


class AdminEmulatorTradeInjectRequest(BaseModel):
    symbol: str
    price: Decimal
    quantity: Decimal
    side: Literal["buy", "sell"]


__all__ = [
    "AdminUserCreateRequest",
    "AdminUserResponse",
    "AdminUserListResponse",
    "AdminBalanceAdjustRequest",
    "AdminBalanceResponse",
    "AdminTradingPairCreateRequest",
    "AdminTradingPairResponse",
    "AdminApiKeyCreateRequest",
    "AdminApiKeyResponse",
    "AdminApiKeyCreateResponse",
    "AdminEmulatorRandomWalkRequest",
    "AdminEmulatorTradeInjectRequest",
]
