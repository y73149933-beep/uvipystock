"""Order repository with domain-specific queries."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, OrderType
from app.models.order import Order
from app.repositories.base import BaseRepository


class OrderRepository(BaseRepository[Order]):
    model = Order

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ─── Single-order reads ─────────────────────────────────────────────────

    async def get_for_update(self, order_id: int) -> Order | None:
        """Fetch an order with SELECT ... FOR UPDATE."""
        stmt = select(Order).where(Order.id == order_id).with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_user(self, order_id: int, user_id: int) -> Order | None:
        """Fetch an order, scoped to a user (security check)."""
        stmt = select(Order).where(Order.id == order_id, Order.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ─── List queries ───────────────────────────────────────────────────────

    async def list_user_orders(
        self,
        user_id: int,
        *,
        symbol: str | None = None,
        statuses: list[OrderStatus] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Sequence[Order]:
        """List a user's orders with optional filters."""
        stmt = select(Order).where(Order.user_id == user_id)
        if symbol is not None:
            stmt = stmt.where(Order.symbol == symbol)
        if statuses:
            stmt = stmt.where(Order.status.in_(statuses))
        stmt = stmt.order_by(Order.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_open_orders_by_symbol(self, symbol: str) -> Sequence[Order]:
        """All active orders for a symbol (used by matching worker on restart)."""
        active = [OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED]
        stmt = (
            select(Order)
            .where(Order.symbol == symbol, Order.status.in_(active))
            .order_by(Order.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_pending_stops_by_symbol(self, symbol: str) -> Sequence[Order]:
        """All stop-type orders awaiting trigger (status=PENDING)."""
        stop_types = [OrderType.STOP_MARKET, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP]
        stmt = (
            select(Order)
            .where(
                Order.symbol == symbol,
                Order.type.in_(stop_types),
                Order.status == OrderStatus.PENDING,
            )
            .order_by(Order.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_children_by_parent(self, parent_order_id: int) -> Sequence[Order]:
        """All child orders (SL/TP) linked to a parent."""
        stmt = select(Order).where(Order.parent_order_id == parent_order_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_bulk_orders(self, bulk_id: str) -> Sequence[Order]:
        """All orders in a bulk batch."""
        stmt = select(Order).where(Order.bulk_id == bulk_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    # ─── Updates ────────────────────────────────────────────────────────────

    async def update_fill(
        self,
        order_id: int,
        filled_qty_delta: Decimal,
        filled_quote_delta: Decimal,
        new_status: OrderStatus,
    ) -> bool:
        """Atomically update filled quantities and status.

        Uses an atomic UPDATE to avoid race conditions between concurrent
        trade events for the same order.
        """
        stmt = (
            sa_update(Order)
            .where(Order.id == order_id)
            .values(
                filled_quantity=Order.filled_quantity + filled_qty_delta,
                filled_quote_qty=Order.filled_quote_qty + filled_quote_delta,
                status=new_status,
                version=Order.version + 1,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def update_status(
        self,
        order_id: int,
        status: OrderStatus,
    ) -> bool:
        """Update only the status field (e.g., cancel, reject)."""
        stmt = (
            sa_update(Order)
            .where(Order.id == order_id)
            .values(status=status, version=Order.version + 1)
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def cancel_children(self, parent_order_id: int) -> int:
        """Cancel all SL/TP children of a parent order. Returns count canceled."""
        active = [OrderStatus.PENDING, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED]
        stmt = (
            sa_update(Order)
            .where(Order.parent_order_id == parent_order_id, Order.status.in_(active))
            .values(status=OrderStatus.CANCELED, version=Order.version + 1)
        )
        result = await self.session.execute(stmt)
        return result.rowcount
