"""Re-export all ORM models so that `from app.models import User, Order, ...`
works in one line, and Alembic autogenerate sees the full metadata.

Importing this module ensures every mapper is registered against
`Base.metadata` before Alembic compares model state to DB state.
"""
from __future__ import annotations

from app.models.api_key import ApiKey
from app.models.balance import Balance
from app.models.enums import (
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    SLTPKind,
    TimeInForce,
    TradeRole,
)
from app.models.order import Order
from app.models.trade import Trade
from app.models.trading_pair import TradingPair
from app.models.user import User

__all__ = [
    # Models
    "User",
    "ApiKey",
    "Balance",
    "TradingPair",
    "Order",
    "Trade",
    # Enums
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "OrderAction",
    "TimeInForce",
    "TradeRole",
    "SLTPKind",
]
