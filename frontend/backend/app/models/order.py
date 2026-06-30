"""Order model — the core domain entity.

Design highlights
-----------------
1. **Self-referential FKs for SL/TP linkage**
   A Market or Limit parent order can carry optional Stop-Loss (`sl_order_id`)
   and Take-Profit (`tp_order_id`) children. Children are created with
   `status=PENDING` and only become active after the parent reaches `FILLED`.
   If the parent is canceled (partially or fully), the children are
   auto-canceled by the service layer.

2. **Self-referential FKs for Cancel-Replace traceability**
   When `PUT /orders/{id}` is called, the old order is canceled and a new one
   is created. To preserve strict Price-Time Priority, the new order has a
   fresh `created_at` timestamp (so it goes to the back of the queue at its
   price level). The chain is reconstructed via `replaces_id` → `replaced_by_id`.

3. **Bulk operations**
   `bulk_id` groups orders placed/canceled atomically in one API call.

4. **Iceberg**
   `visible_quantity` is the part shown in the order book; `hidden_quantity`
   is the reserve. The full hidden volume is locked upfront.

5. **Optimistic locking**
   `version` is incremented on every UPDATE — same pattern as `Balance`.

6. **Partial fill tracking**
   `filled_quantity` and `filled_quote_qty` enable computing `avg_fill_price`
   on the fly without denormalizing it.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import OrderSide, OrderStatus, OrderType

if TYPE_CHECKING:
    from app.models.trade import Trade
    from app.models.trading_pair import TradingPair
    from app.models.user import User


class Order(Base):
    """A trading order in any lifecycle state."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_symbol_status", "user_id", "symbol", "status"),
        Index("ix_orders_status_symbol",      "status",  "symbol"),
        Index("ix_orders_parent_id",          "parent_order_id"),
        Index("ix_orders_bulk_id",            "bulk_id"),
        Index("ix_orders_replaces_id",        "replaces_id"),
    )

    # ─── Identity ────────────────────────────────────────────────────────────
    id:      Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol:  Mapped[str]      = mapped_column(
        String(20), ForeignKey("trading_pairs.symbol"), nullable=False
    )

    # ─── Type & state ────────────────────────────────────────────────────────
    side:   Mapped[OrderSide]   = mapped_column(
        SAEnum(OrderSide, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    type:   Mapped[OrderType]   = mapped_column(
        SAEnum(OrderType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=OrderStatus.NEW,
    )

    # ─── Pricing ─────────────────────────────────────────────────────────────
    price:          Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18), nullable=True)
    stop_price:     Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18), nullable=True)
    trailing_delta: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18), nullable=True)

    # ─── Quantities ──────────────────────────────────────────────────────────
    quantity:         Mapped[Decimal]         = mapped_column(Numeric(36, 18), nullable=False)
    filled_quantity:  Mapped[Decimal]         = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    filled_quote_qty: Mapped[Decimal]         = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))

    # Iceberg-specific (nullable for non-iceberg orders)
    visible_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18), nullable=True)
    hidden_quantity:  Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18), nullable=True)

    # ─── Cancel-Replace traceability ─────────────────────────────────────────
    replace_count:  Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    replaces_id:    Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=True)
    replaced_by_id: Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=True)

    # ─── SL/TP linkage ───────────────────────────────────────────────────────
    parent_order_id: Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=True)
    sl_order_id:     Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=True)
    tp_order_id:     Mapped[Optional[int]]  = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=True)

    # ─── Bulk operations ─────────────────────────────────────────────────────
    bulk_id:         Mapped[Optional[str]]  = mapped_column(String(36), nullable=True)

    # ─── Audit / versioning ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    version:    Mapped[int]      = mapped_column(BigInteger, nullable=False, default=1)

    # ─── Relationships ───────────────────────────────────────────────────────
    # NOTE: `foreign_keys=[...]` is mandatory here because Order has multiple
    # self-referential FK columns; SQLAlchemy cannot infer which FK each
    # relationship binds to without it.
    user:         Mapped["User"]            = relationship("User", foreign_keys=[user_id])
    trading_pair: Mapped["TradingPair"]     = relationship(back_populates="orders", foreign_keys=[symbol])

    parent:       Mapped[Optional["Order"]] = relationship(
        "Order", remote_side="Order.id", foreign_keys="Order.parent_order_id", backref="children"
    )
    sl_order:     Mapped[Optional["Order"]] = relationship(
        "Order", remote_side="Order.id", foreign_keys="Order.sl_order_id"
    )
    tp_order:     Mapped[Optional["Order"]] = relationship(
        "Order", remote_side="Order.id", foreign_keys="Order.tp_order_id"
    )
    replaces:     Mapped[Optional["Order"]] = relationship(
        "Order", remote_side="Order.id", foreign_keys="Order.replaces_id", backref="replaced_by"
    )

    # ─── Derived properties ──────────────────────────────────────────────────

    @property
    def remaining_quantity(self) -> Decimal:
        """Quantity still resting in the book (or pending trigger)."""
        return self.quantity - self.filled_quantity

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        """Volume-weighted average price of fills so far.

        Returns None if no fills yet (avoids division by zero).
        """
        if self.filled_quantity == 0:
            return None
        return self.filled_quote_qty / self.filled_quantity

    @property
    def is_iceberg(self) -> bool:
        return self.type == OrderType.ICEBERG

    @property
    def is_stop_type(self) -> bool:
        """True for stop_market / stop_limit / trailing_stop."""
        return self.type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP)

    @property
    def is_active(self) -> bool:
        """True if the order still consumes balance / occupies a book slot."""
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING)

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} {self.side.value} {self.quantity} {self.symbol} "
            f"@ {self.price} type={self.type.value} status={self.status.value}>"
        )
