"""Pydantic v2 schemas for order API requests and responses."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import OrderSide, OrderStatus, OrderType, TimeInForce


# ─── SL/TP child config ──────────────────────────────────────────────────────

class SLTPConfigSchema(BaseModel):
    """Configuration for a Stop-Loss or Take-Profit child order."""
    model_config = ConfigDict(str_strip_whitespace=True)

    type: Literal["stop_market", "stop_limit", "limit"]
    stop_price: Decimal | None = Field(None, description="Required for stop_market / stop_limit")
    price: Decimal | None = Field(None, description="Limit price (for stop_limit / limit)")
    quantity: Decimal | None = Field(None, description="Defaults to parent quantity")

    @field_validator("stop_price", "price", "quantity")
    @classmethod
    def must_be_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("must be positive")
        return v


# ─── Create ──────────────────────────────────────────────────────────────────

class OrderCreateRequest(BaseModel):
    """POST /api/v1/orders request body."""
    model_config = ConfigDict(str_strip_whitespace=True)

    symbol: str = Field(..., min_length=3, max_length=20, examples=["BTC/USDT"])
    side: OrderSide
    type: OrderType
    price: Decimal | None = Field(None, description="Limit price (required for limit/post_only/ioc/fok/iceberg/stop_limit)")
    stop_price: Decimal | None = Field(None, description="Stop trigger price (for stop_market/stop_limit)")
    trailing_delta: Decimal | None = Field(None, description="Trailing offset (for trailing_stop)")
    quantity: Decimal = Field(..., gt=0, description="Total order quantity")
    time_in_force: TimeInForce = Field(default=TimeInForce.GTC)
    client_order_id: str | None = Field(None, max_length=64, description="Idempotency key")
    post_only: bool = False
    iceberg_visible_quantity: Decimal | None = Field(None, description="Visible chunk for iceberg orders")
    iceberg_hidden_quantity: Decimal | None = Field(None, description="Hidden reserve for iceberg orders")
    sl: SLTPConfigSchema | None = None
    tp: SLTPConfigSchema | None = None

    @field_validator("price", "stop_price", "trailing_delta")
    @classmethod
    def must_be_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("must be positive")
        return v


class OrderBulkCreateRequest(BaseModel):
    """POST /api/v1/orders/bulk request body."""
    model_config = ConfigDict(str_strip_whitespace=True)

    bulk_id: str | None = Field(None, max_length=36, description="Optional UUID; server generates if absent")
    orders: list[OrderCreateRequest] = Field(..., min_length=1, max_length=50)


# ─── Modify (Cancel-Replace) ─────────────────────────────────────────────────

class OrderModifyRequest(BaseModel):
    """PUT /api/v1/orders/{order_id} request body."""
    model_config = ConfigDict(str_strip_whitespace=True)

    price: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., gt=0)
    time_in_force: TimeInForce = Field(default=TimeInForce.GTC)


# ─── Bulk cancel ─────────────────────────────────────────────────────────────

class OrderBulkCancelRequest(BaseModel):
    """DELETE /api/v1/orders/bulk request body."""
    model_config = ConfigDict(str_strip_whitespace=True)

    order_ids: list[int] | None = Field(None, description="Specific order IDs to cancel")
    symbol: str | None = Field(None, description="Cancel all orders on this symbol")
    cancel_all: bool = Field(False, description="If true with symbol, cancel all on symbol; without symbol, cancel all")


# ─── Responses ───────────────────────────────────────────────────────────────

class OrderResponse(BaseModel):
    """Single order in API responses."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    symbol: str
    side: OrderSide
    type: OrderType
    status: OrderStatus
    price: Decimal | None
    stop_price: Decimal | None
    trailing_delta: Decimal | None
    quantity: Decimal
    filled_quantity: Decimal
    filled_quote_qty: Decimal
    remaining_quantity: Decimal
    avg_fill_price: Decimal | None
    visible_quantity: Decimal | None
    hidden_quantity: Decimal | None
    parent_order_id: int | None
    sl_order_id: int | None
    tp_order_id: int | None
    replaces_id: int | None
    replaced_by_id: int | None
    bulk_id: str | None
    replace_count: int
    created_at: datetime
    updated_at: datetime


class OrderCreateResponse(OrderResponse):
    """Response for POST /orders (extends OrderResponse with SL/TP IDs)."""
    client_order_id: str | None = None


class OrderBulkCreateResponse(BaseModel):
    """Response for POST /orders/bulk."""
    bulk_id: str
    result: Literal["success", "rejected"]
    total: int
    succeeded: int
    orders: list[OrderResponse] = []
    errors: list[dict] = []


class OrderCancelResponse(BaseModel):
    """Response for DELETE /orders/{id}."""
    order_id: int
    status: OrderStatus
    unlocked: dict[str, str] | None = None
    canceled_at: datetime


class OrderBulkCancelResponse(BaseModel):
    """Response for DELETE /orders/bulk."""
    canceled_count: int
    canceled_orders: list[int]
    failed: list[dict]
    total_unlocked: list[dict[str, str]]


class OrderListResponse(BaseModel):
    """Response for GET /orders."""
    orders: list[OrderResponse]
    pagination: dict


__all__ = [
    "SLTPConfigSchema",
    "OrderCreateRequest",
    "OrderBulkCreateRequest",
    "OrderModifyRequest",
    "OrderBulkCancelRequest",
    "OrderResponse",
    "OrderCreateResponse",
    "OrderBulkCreateResponse",
    "OrderCancelResponse",
    "OrderBulkCancelResponse",
    "OrderListResponse",
]
