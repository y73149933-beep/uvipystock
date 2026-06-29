"""WebSocket message schemas (for documentation + validation)."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


# ─── Public orderbook channel ────────────────────────────────────────────────

class OrderBookSnapshot(BaseModel):
    """Full L2 snapshot sent on initial subscription."""
    event: Literal["orderbook_snapshot"] = "orderbook_snapshot"
    symbol: str
    bids: list[list[Decimal]]  # [[price, volume], ...]
    asks: list[list[Decimal]]
    last_trade_price: Decimal | None = None
    ts: int


class OrderBookUpdate(BaseModel):
    """Incremental L2 update (delta)."""
    event: Literal["orderbook_update"] = "orderbook_update"
    symbol: str
    changes: list[dict]  # [{"side": "bid"|"ask", "price": ..., "qty": ...}]
    ts: int


class TradePrint(BaseModel):
    """A single trade print broadcast on the public trades channel."""
    event: Literal["trade"] = "trade"
    symbol: str
    trade_id: int
    price: Decimal
    quantity: Decimal
    side: str  # taker side
    ts: int


class WSPing(BaseModel):
    """Heartbeat ping."""
    event: Literal["ping"] = "ping"
    ts: int


# ─── Private channel ─────────────────────────────────────────────────────────

class OrderUpdate(BaseModel):
    """Per-user order status update."""
    event: Literal["order"] = "order"
    order_id: int
    symbol: str
    side: str
    type: str
    status: str
    status_event: str  # placed | partially_filled | filled | canceled | modified | rejected | triggered
    price: Decimal | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    avg_fill_price: Decimal | None = None
    last_trade_qty: Decimal | None = None
    last_trade_price: Decimal | None = None
    client_order_id: str | None = None
    bulk_id: str | None = None
    ts: int


class BalanceUpdate(BaseModel):
    """Per-user balance update."""
    event: Literal["balance"] = "balance"
    asset: str
    total: Decimal
    locked: Decimal
    available: Decimal
    change: Decimal | None = None
    reason: str | None = None
    order_id: int | None = None
    ts: int


class BulkResult(BaseModel):
    """Bulk operation result notification."""
    event: Literal["bulk_result"] = "bulk_result"
    bulk_id: str
    action: str
    total: int
    succeeded: int
    failed: list[dict]
    ts: int


class SLTPActivated(BaseModel):
    """Notification that SL/TP children have been activated."""
    event: Literal["sl_tp_activated"] = "sl_tp_activated"
    parent_order_id: int
    sl_order_id: int | None = None
    tp_order_id: int | None = None
    ts: int


# ─── Subscribe / auth messages ───────────────────────────────────────────────

class WSSubscribe(BaseModel):
    """Client → server subscription message."""
    action: Literal["subscribe"]
    channel: str
    symbol: str | None = None
    depth: int = 20


class WSAuth(BaseModel):
    """Client → server auth message (for private channel)."""
    action: Literal["auth"]
    api_key: str
    timestamp: int
    signature: str


__all__ = [
    "OrderBookSnapshot",
    "OrderBookUpdate",
    "TradePrint",
    "WSPing",
    "OrderUpdate",
    "BalanceUpdate",
    "BulkResult",
    "SLTPActivated",
    "WSSubscribe",
    "WSAuth",
]
