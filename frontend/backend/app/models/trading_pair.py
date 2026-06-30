"""TradingPair model — a market between a base and a quote asset."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.trade import Trade


class TradingPair(Base):
    """A tradeable market, e.g. BTC/USDT.

    Fields
    ------
    * `price_precision`    — decimals allowed in price (e.g. 2 → 42150.50).
    * `quantity_precision` — decimals allowed in quantity (e.g. 8 → 0.12345678).
    * `min_lot_size`       — minimum quantity per order.
    * `max_lot_size`       — maximum quantity per order (anti-fat-finger).
    * `tick_size`          — minimum price increment; orders are snapped to it.
    * `maker_fee_bps`      — fee in basis points for liquidity makers (negative = rebate).
    * `taker_fee_bps`      — fee in basis points for liquidity takers.
    """

    __tablename__ = "trading_pairs"

    id:                 Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:             Mapped[str]      = mapped_column(String(20), unique=True, nullable=False, index=True)
    base_asset:         Mapped[str]      = mapped_column(String(20), nullable=False)
    quote_asset:        Mapped[str]      = mapped_column(String(20), nullable=False)
    price_precision:    Mapped[int]      = mapped_column(Integer, nullable=False)
    quantity_precision: Mapped[int]      = mapped_column(Integer, nullable=False)
    min_lot_size:       Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    max_lot_size:       Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    tick_size:          Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    maker_fee_bps:      Mapped[Decimal]  = mapped_column(Numeric(10, 6), default=Decimal("0"), nullable=False)
    taker_fee_bps:      Mapped[Decimal]  = mapped_column(Numeric(10, 6), default=Decimal("0"), nullable=False)
    is_active:          Mapped[bool]     = mapped_column(Boolean, default=True, nullable=False)
    created_at:         Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    orders: Mapped[list["Order"]] = relationship(back_populates="trading_pair")
    trades: Mapped[list["Trade"]] = relationship(back_populates="trading_pair")

    def __repr__(self) -> str:
        return f"<TradingPair symbol={self.symbol!r} active={self.is_active}>"
