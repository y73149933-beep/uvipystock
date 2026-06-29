"""Domain enums used across models, schemas, and the matching engine.

All enums inherit from `str` so they JSON-serialize natively as their value
and are stored as TEXT in PostgreSQL via SQLAlchemy's `SAEnum(values_callable=...)`.
"""
from __future__ import annotations

from enum import Enum


class OrderSide(str, Enum):
    """Side of an order."""
    BUY  = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Supported order types.

    * `market`        — executes immediately against the book, no price specified.
    * `limit`         — rests in the book at a specified price.
    * `stop_market`   — triggers a market order when `stop_price` is crossed.
    * `stop_limit`    — triggers a limit order when `stop_price` is crossed.
    * `post_only`     — limit order that must rest; rejected if it would cross.
    * `ioc`           — Immediate-Or-Cancel limit order.
    * `fok`           — Fill-Or-Kill limit order (atomic full-fill or cancel).
    * `trailing_stop` — stop price trails the local extreme by `trailing_delta`.
    * `iceberg`       — limit order with a hidden reserve; only `visible_quantity` shows.
    """
    MARKET        = "market"
    LIMIT         = "limit"
    STOP_MARKET   = "stop_market"
    STOP_LIMIT    = "stop_limit"
    POST_ONLY     = "post_only"
    IOC           = "ioc"
    FOK           = "fok"
    TRAILING_STOP = "trailing_stop"
    ICEBERG       = "iceberg"


class OrderStatus(str, Enum):
    """Lifecycle states of an order.

    * `pending`           — stop-type order awaiting trigger (not in book).
    * `new`               — resting in book, 0 fills.
    * `partially_filled`  — resting in book, has some fills.
    * `filled`            — fully executed, removed from book.
    * `canceled`          — user/admin canceled; remaining balance unlocked.
    * `rejected`          — pre-trade validation failed (no balance locked).
    * `expired`           — IOC/FOK that could not fill, or post-only crossed.
    """
    PENDING          = "pending"
    NEW              = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED           = "filled"
    CANCELED         = "canceled"
    REJECTED         = "rejected"
    EXPIRED          = "expired"


class OrderAction(str, Enum):
    """Action tags for messages pushed onto `queue:orders`.

    The matching worker branches on this field; it does NOT correspond to
    a database column but lives in the queue payload.
    """
    PLACE  = "place"
    CANCEL = "cancel"
    MODIFY = "modify"  # Cancel-Replace


class TimeInForce(str, Enum):
    """TIF for limit-type orders (orthogonal to OrderType for clarity)."""
    GTC       = "gtc"        # Good-Til-Canceled
    IOC       = "ioc"        # Immediate-Or-Cancel
    FOK       = "fok"        # Fill-Or-Kill
    POST_ONLY = "post_only"  # Post-Only


class TradeRole(str, Enum):
    """Role of the user in a given trade."""
    TAKER = "taker"
    MAKER = "maker"


class SLTPKind(str, Enum):
    """Which leg of an SL/TP pair this order represents."""
    NONE = "none"
    SL   = "sl"
    TP   = "tp"
