"""Trade service — persist trades and update order fill state.

Called by the matching worker after each match. The worker produces a list
of trade dicts (from the Cython matcher) and passes them here for:

  1. INSERT into the `trades` table (batched)
  2. UPDATE orders: filled_quantity, filled_quote_qty, status
  3. UPDATE balances: settle taker's locked, credit maker's available
  4. PUBLISH trade + order + balance events to Redis Pub/Sub

All updates for a single match batch are in ONE PostgreSQL transaction
to guarantee atomicity.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderSide, OrderStatus, OrderType
from app.models.trade import Trade
from app.redis_client import get_redis
from app.redis_client import orderbook, pubsub
from app.repositories.balance_repo import BalanceRepository
from app.repositories.order_repo import OrderRepository
from app.repositories.trade_repo import TradeRepository
from app.services.balance_service import BalanceService

logger = logging.getLogger(__name__)


class TradeService:
    """Persist trades and update balances + orders atomically."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.trade_repo = TradeRepository(session)
        self.order_repo = OrderRepository(session)
        self.balance_repo = BalanceRepository(session)
        self.balance_svc = BalanceService(session)

    async def persist_trade_batch(
        self,
        trades: list[dict[str, Any]],
        taker_user_id: int,
        taker_order_id: int,
        taker_side: OrderSide,
        taker_type: OrderType,
        symbol: str,
        base_asset: str,
        quote_asset: str,
    ) -> list[Trade]:
        """Persist a batch of trades from a single match.

        For each trade:
          - INSERT Trade row
          - UPDATE maker order (filled_quantity, status)
          - UPDATE taker order (filled_quantity, status) — once per batch
          - Settle taker's locked asset, credit maker's available asset

        Balance flow for a BUY taker (taker buys base, pays quote):
          - Taker: locked quote was held at placement → settle (deduct) quote
          - Taker: credit base (the asset they bought)
          - Maker: locked base was held → settle (deduct) base
          - Maker: credit quote (the asset they sold for)

        Balance flow for a SELL taker (mirror of above):
          - Taker: locked base → settle base
          - Taker: credit quote
          - Maker: locked quote → settle quote
          - Maker: credit base

        Returns the list of persisted Trade objects.
        """
        if not trades:
            return []

        persisted: list[Trade] = []
        redis = get_redis()

        # Aggregate per-order fill deltas for batched updates
        maker_fill_deltas: dict[int, tuple[Decimal, Decimal]] = {}  # order_id → (qty, quote_qty)
        taker_total_qty = Decimal("0")
        taker_total_quote = Decimal("0")

        for trade_dict in trades:
            maker_id = int(trade_dict["maker_order_id"])
            price = Decimal(str(trade_dict["price"]))
            qty = Decimal(str(trade_dict["quantity"]))
            quote_qty = price * qty

            # Insert Trade row
            trade = Trade(
                taker_order_id=taker_order_id,
                maker_order_id=maker_id,
                symbol=symbol,
                price=price,
                quantity=qty,
                quote_quantity=quote_qty,
                side=taker_side,
                taker_user_id=taker_user_id,
                maker_user_id=int(trade_dict.get("maker_user_id", 0)),  # set below
                taker_fee=Decimal("0"),   # TODO: compute from pair fees
                maker_fee=Decimal("0"),
            )
            self.session.add(trade)
            persisted.append(trade)

            # Accumulate maker fill delta
            if maker_id in maker_fill_deltas:
                prev_qty, prev_quote = maker_fill_deltas[maker_id]
                maker_fill_deltas[maker_id] = (prev_qty + qty, prev_quote + quote_qty)
            else:
                maker_fill_deltas[maker_id] = (qty, quote_qty)

            taker_total_qty += qty
            taker_total_quote += quote_qty

        await self.session.flush()

        # Load all involved orders + balances with FOR UPDATE (pessimistic)
        maker_orders: dict[int, Any] = {}
        for maker_id in maker_fill_deltas:
            mo = await self.order_repo.get_for_update(maker_id)
            if mo is not None:
                maker_orders[maker_id] = mo

        taker_order = await self.order_repo.get_for_update(taker_order_id)

        # Update maker orders + settle balances
        for maker_id, (qty_delta, quote_delta) in maker_fill_deltas.items():
            mo = maker_orders.get(maker_id)
            if mo is None:
                continue

            mo.filled_quantity += qty_delta
            mo.filled_quote_qty += quote_delta
            mo.version += 1

            # Determine maker status
            if mo.filled_quantity >= mo.quantity:
                mo.status = OrderStatus.FILLED
            else:
                mo.status = OrderStatus.PARTIALLY_FILLED

            # Set the maker_user_id on the Trade rows (now that we have the order)
            for t in persisted:
                if t.maker_order_id == maker_id:
                    t.maker_user_id = mo.user_id

            # Balance flow
            await self._settle_trade_balances(
                taker_side=taker_side,
                is_taker=False,
                maker_order=mo,
                qty=qty_delta,
                quote_qty=quote_delta,
                base_asset=base_asset,
                quote_asset=quote_asset,
            )

            # Update Redis maker order HASH + remove from book if filled
            await self._update_redis_maker(redis, mo)

        # Update taker order
        if taker_order is not None:
            taker_order.filled_quantity += taker_total_qty
            taker_order.filled_quote_qty += taker_total_quote
            taker_order.version += 1

            if taker_order.filled_quantity >= taker_order.quantity:
                taker_order.status = OrderStatus.FILLED
            elif taker_order.filled_quantity > 0:
                taker_order.status = OrderStatus.PARTIALLY_FILLED

            # Taker balance flow
            await self._settle_trade_balances(
                taker_side=taker_side,
                is_taker=True,
                taker_order=taker_order,
                qty=taker_total_qty,
                quote_qty=taker_total_quote,
                base_asset=base_asset,
                quote_asset=quote_asset,
            )

            # If taker is fully filled and has SL/TP children → activate them
            if taker_order.status == OrderStatus.FILLED:
                await self._activate_sltp_children(taker_order)

        await self.session.flush()

        # Publish trade events (public)
        for trade in persisted:
            await pubsub.publish_trade(
                redis, symbol=symbol, trade_id=trade.id,
                price=float(trade.price), quantity=float(trade.quantity),
                side=trade.side.value,
                taker_order_id=taker_order_id,
                maker_order_id=trade.maker_order_id,
            )

        # Publish order updates (private, for both taker and makers)
        if taker_order is not None:
            event = "filled" if taker_order.status == OrderStatus.FILLED else "partially_filled"
            await pubsub.publish_order_update(
                redis, user_id=taker_user_id, event=event,
                order_id=taker_order.id, symbol=taker_order.symbol,
                side=taker_order.side.value, type=taker_order.type.value,
                status=taker_order.status.value,
                filled_quantity=float(taker_order.filled_quantity),
                remaining_quantity=float(taker_order.remaining_quantity),
                avg_fill_price=float(taker_order.avg_fill_price) if taker_order.avg_fill_price else None,
            )
        for maker_id, mo in maker_orders.items():
            event = "filled" if mo.status == OrderStatus.FILLED else "partially_filled"
            await pubsub.publish_order_update(
                redis, user_id=mo.user_id, event=event,
                order_id=mo.id, symbol=mo.symbol,
                side=mo.side.value, type=mo.type.value,
                status=mo.status.value,
                filled_quantity=float(mo.filled_quantity),
                remaining_quantity=float(mo.remaining_quantity),
                avg_fill_price=float(mo.avg_fill_price) if mo.avg_fill_price else None,
            )

        return persisted

    async def _settle_trade_balances(
        self,
        *,
        taker_side: OrderSide,
        is_taker: bool,
        taker_order: Any = None,
        maker_order: Any = None,
        qty: Decimal,
        quote_qty: Decimal,
        base_asset: str,
        quote_asset: str,
    ) -> None:
        """Settle balances for one side of a trade (taker or maker).

        For a BUY taker (taker buys base, pays quote):
          - Taker: settle quote (locked → deducted), credit base
          - Maker: settle base (locked → deducted), credit quote

        For a SELL taker (mirror):
          - Taker: settle base, credit quote
          - Maker: settle quote, credit base
        """
        if is_taker:
            assert taker_order is not None
            user_id = taker_order.user_id
            if taker_side == OrderSide.BUY:
                # Taker pays quote, receives base
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                if quote_bal:
                    await self.balance_svc.settle_pessimistic(quote_bal, quote_qty, "trade_settled", taker_order.id)
                if base_bal:
                    await self.balance_svc.credit_pessimistic(base_bal, qty, "trade_received", taker_order.id)
            else:
                # Taker pays base, receives quote
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                if base_bal:
                    await self.balance_svc.settle_pessimistic(base_bal, qty, "trade_settled", taker_order.id)
                if quote_bal:
                    await self.balance_svc.credit_pessimistic(quote_bal, quote_qty, "trade_received", taker_order.id)
        else:
            assert maker_order is not None
            user_id = maker_order.user_id
            if taker_side == OrderSide.BUY:
                # Maker sells base, receives quote
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                if base_bal:
                    await self.balance_svc.settle_pessimistic(base_bal, qty, "trade_settled", maker_order.id)
                if quote_bal:
                    await self.balance_svc.credit_pessimistic(quote_bal, quote_qty, "trade_received", maker_order.id)
            else:
                # Maker buys base, pays quote
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                if quote_bal:
                    await self.balance_svc.settle_pessimistic(quote_bal, quote_qty, "trade_settled", maker_order.id)
                if base_bal:
                    await self.balance_svc.credit_pessimistic(base_bal, qty, "trade_received", maker_order.id)

    async def _update_redis_maker(self, redis, maker_order) -> None:
        """Update Redis HASH for a maker order after a fill.

        If the maker is fully filled, remove from book + indexes.
        Otherwise, update the HASH's filled_quantity AND visible_quantity
        (so the order book snapshot shows the correct remaining volume).
        """
        if maker_order.status == OrderStatus.FILLED:
            await orderbook.remove_resting_order(
                redis, maker_order.id, maker_order.symbol, maker_order.side,
                maker_order.price or Decimal("0"), 0,
            )
            await orderbook.delete_order_hash(redis, maker_order.id)
            from app.redis_client import orders_index
            await orders_index.remove_open_order(
                redis, maker_order.user_id, maker_order.symbol, maker_order.id,
            )
        else:
            # Update HASH with new filled_quantity + visible_quantity
            # visible_quantity is what the order book snapshot uses to display
            # the remaining volume. Without this update, the book shows the
            # original quantity even after a partial fill.
            remaining = maker_order.quantity - maker_order.filled_quantity
            await orderbook.update_order_hash_fields(redis, maker_order.id, {
                "filled_quantity": str(maker_order.filled_quantity),
                "visible_quantity": str(remaining),
                "quantity": str(remaining),
                "status": maker_order.status.value,
            })

    async def _activate_sltp_children(self, parent_order) -> None:
        """When a parent order fills, activate its SL/TP children.

        Children move from PENDING → NEW (or are enqueued as market/limit).
        For simplicity, this enqueues them as new orders via the matching
        worker. A full implementation would also register stop-type children
        in the stop queue.
        """
        from app.redis_client import queues
        redis = get_redis()

        # Load children
        children = await self.order_repo.list_children_by_parent(parent_order.id)
        for child in children:
            if child.status != OrderStatus.PENDING:
                continue
            child.status = OrderStatus.NEW
            child.version += 1

            # Enqueue place action
            payload = queues.build_order_action_payload(
                action="place",
                order_id=child.id,
                symbol=child.symbol,
                user_id=child.user_id,
                side=child.side.value,
                type=child.type.value,
                price=float(child.price) if child.price else None,
                quantity=float(child.quantity),
                parent_order_id=parent_order.id,
            )
            await queues.enqueue_order_action(redis, payload)

            # Publish SL/TP activation event
            await pubsub.publish_sl_tp_activated(
                redis, user_id=child.user_id,
                parent_order_id=parent_order.id,
                sl_order_id=parent_order.sl_order_id,
                tp_order_id=parent_order.tp_order_id,
            )


__all__ = ["TradeService"]
