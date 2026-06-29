"""Trade model — a single executed match between two orders."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import OrderSide

if TYPE_CHECKING:
    from app.models.trading_pair import TradingPair
    from app.models.user import User


class Trade(Base):
    """A completed match between a taker order and a maker order.

    Two user IDs are stored so that the matching worker can credit/debit
    both sides in a single DB transaction without joins.

    `side` records the *taker* side — i.e. whether the aggressor was buying
    or selling. The maker is implicitly the opposite side.
    """

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_symbol_executed_at", "symbol", "executed_at"),
        Index("ix_trades_taker_user_id",      "taker_user_id"),
        Index("ix_trades_maker_user_id",      "maker_user_id"),
        Index("ix_trades_taker_order_id",     "taker_order_id"),
    )

    id:             Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    taker_order_id: Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    maker_order_id: Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    symbol:         Mapped[str]      = mapped_column(
        String(20), ForeignKey("trading_pairs.symbol"), nullable=False
    )

    # Execution details
    price:          Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    quantity:       Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    quote_quantity: Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    side:           Mapped[OrderSide] = mapped_column(
        SAEnum(OrderSide, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="Taker side (buy/sell)",
    )

    # Counterparties
    taker_user_id:  Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    maker_user_id:  Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    taker_fee:      Mapped[Decimal]  = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    maker_fee:      Mapped[Decimal]  = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)

    executed_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # ─── Relationships ───────────────────────────────────────────────────────
    trading_pair: Mapped["TradingPair"] = relationship(back_populates="trades", foreign_keys=[symbol])
    taker_user:   Mapped["User"]        = relationship("User", foreign_keys=[taker_user_id])
    maker_user:   Mapped["User"]        = relationship("User", foreign_keys=[maker_user_id])

    def __repr__(self) -> str:
        return (
            f"<Trade id={self.id} {self.side.value} {self.quantity} {self.symbol} "
            f"@ {self.price} taker_uid={self.taker_user_id} maker_uid={self.maker_user_id}>"
        )
