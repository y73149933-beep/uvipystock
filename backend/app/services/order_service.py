"""Order service — place / cancel / modify / bulk with atomic balance locking.

This is the entry point for all order operations from the API layer. Each
method:

  1. Validates order parameters against the trading pair rules.
  2. Locks the required balance (optimistic locking with retries).
  3. Inserts the Order row in PostgreSQL.
  4. Pushes an action onto `queue:orders` for the matching worker.
  5. Publishes order/balance events to Redis Pub/Sub.

All operations are atomic: if any step fails, the transaction rolls back
and no balance is locked.

Order-type-specific locking rules (from Step 1 design)
------------------------------------------------------
* LIMIT / POST_ONLY / IOC / FOK / ICEBERG → lock immediately at placement
  * Sell: lock base asset (qty)
  * Buy:  lock quote asset (price * qty)
* ICEBERG: lock the FULL hidden volume upfront
* MARKET BUY: lock worst-case quote (best_ask_price * qty); refund after match
* MARKET SELL: lock base asset (qty) — exact amount known
* STOP_MARKET / STOP_LIMIT / TRAILING_STOP: NO lock at placement; lock only
  after the trigger fires and creates the child market/limit order
* SL/TP children: created as PENDING, no lock until parent fills

Cancel-Replace (Modify)
-----------------------
Implemented as Cancel(old) + Place(new) in a single PG transaction:
  1. Load old order with FOR UPDATE
  2. Unlock old remaining volume
  3. Validate new params, lock new volume
  4. Mark old as CANCELED, insert new with replaces_id = old.id
  5. Enqueue modify action to matching worker

Bulk operations
---------------
* Batch placement: validate ALL orders first, sum balance requirements,
  then lock + insert in one transaction (All-or-Nothing).
* Batch cancel: list of order IDs or cancel-all by symbol; unlock each
  in one transaction.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderSide, OrderStatus, OrderType
from app.models.order import Order
from app.models.trading_pair import TradingPair
from app.redis_client import get_redis
from app.redis_client import orderbook, orders_index, queues, pubsub, stops
from app.repositories.order_repo import OrderRepository
from app.repositories.trading_pair_repo import TradingPairRepository
from app.services.balance_service import (
    BalanceService,
    InsufficientBalanceError,
)

logger = logging.getLogger(__name__)


# ─── Exceptions ──────────────────────────────────────────────────────────────

class OrderValidationError(Exception):
    """Raised when order parameters violate trading pair rules."""


class OrderNotFoundError(Exception):
    """Raised when an order ID does not exist or doesn't belong to the user."""


class OrderNotCancelableError(Exception):
    """Raised when trying to cancel/modify an order that's not active."""


class PostOnlyCrossError(Exception):
    """Raised when a Post-Only order would cross the spread."""


# ─── DTOs ────────────────────────────────────────────────────────────────────

class OrderCreateDTO:
    """Input DTO for placing a single order.

    Kept as a plain class (not Pydantic) so the service layer doesn't
    depend on the API layer's schema definitions.
    """

    def __init__(
        self,
        *,
        symbol: str,
        side: OrderSide | str,
        type: OrderType | str,
        price: Decimal | float | None = None,
        stop_price: Decimal | float | None = None,
        trailing_delta: Decimal | float | None = None,
        quantity: Decimal | float,
        visible_quantity: Decimal | float | None = None,
        hidden_quantity: Decimal | float | None = None,
        sl: "SLTPConfig | None" = None,
        tp: "SLTPConfig | None" = None,
        client_order_id: str | None = None,
        bulk_id: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.side = side if isinstance(side, str) else side.value
        self.type = type if isinstance(type, str) else type.value
        self.price = Decimal(str(price)) if price is not None else None
        self.stop_price = Decimal(str(stop_price)) if stop_price is not None else None
        self.trailing_delta = Decimal(str(trailing_delta)) if trailing_delta is not None else None
        self.quantity = Decimal(str(quantity))
        self.visible_quantity = Decimal(str(visible_quantity)) if visible_quantity is not None else None
        self.hidden_quantity = Decimal(str(hidden_quantity)) if hidden_quantity is not None else None
        self.sl = sl
        self.tp = tp
        self.client_order_id = client_order_id
        self.bulk_id = bulk_id


class SLTPConfig:
    """SL or TP child order configuration."""

    def __init__(
        self,
        *,
        type: OrderType | str,           # stop_market | stop_limit | limit
        stop_price: Decimal | float | None = None,
        price: Decimal | float | None = None,
        quantity: Decimal | float | None = None,
    ) -> None:
        self.type = type if isinstance(type, str) else type.value
        self.stop_price = Decimal(str(stop_price)) if stop_price is not None else None
        self.price = Decimal(str(price)) if price is not None else None
        self.quantity = Decimal(str(quantity)) if quantity is not None else None


# ─── Service ─────────────────────────────────────────────────────────────────

class OrderService:
    """Order placement, cancellation, modification, and bulk operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = OrderRepository(session)
        self.pair_repo = TradingPairRepository(session)
        self.balance_svc = BalanceService(session)

    # ─── Validation ─────────────────────────────────────────────────────────

    async def _validate_and_get_pair(self, symbol: str) -> TradingPair:
        """Fetch the trading pair and verify it's active."""
        pair = await self.pair_repo.get_by_symbol(symbol)
        if pair is None:
            raise OrderValidationError(f"Trading pair {symbol!r} not found")
        if not pair.is_active:
            raise OrderValidationError(f"Trading pair {symbol!r} is not active")
        return pair

    def _validate_params(self, dto: OrderCreateDTO, pair: TradingPair) -> None:
        """Validate order parameters against trading pair rules."""
        # Side
        if dto.side not in ("buy", "sell"):
            raise OrderValidationError(f"Invalid side: {dto.side!r}")

        # Quantity
        if dto.quantity <= 0:
            raise OrderValidationError(f"quantity must be positive, got {dto.quantity}")
        if dto.quantity < pair.min_lot_size:
            raise OrderValidationError(
                f"quantity {dto.quantity} below min_lot_size {pair.min_lot_size}"
            )
        if dto.quantity > pair.max_lot_size:
            raise OrderValidationError(
                f"quantity {dto.quantity} above max_lot_size {pair.max_lot_size}"
            )

        # Type-specific
        needs_price = dto.type in ("limit", "post_only", "ioc", "fok", "iceberg", "stop_limit")
        if needs_price and dto.price is None:
            raise OrderValidationError(f"Order type {dto.type!r} requires price")
        if dto.price is not None and dto.price <= 0:
            raise OrderValidationError(f"price must be positive, got {dto.price}")

        needs_stop = dto.type in ("stop_market", "stop_limit", "trailing_stop")
        if needs_stop:
            if dto.type != "trailing_stop" and dto.stop_price is None:
                raise OrderValidationError(f"Order type {dto.type!r} requires stop_price")
            if dto.type == "trailing_stop" and dto.trailing_delta is None:
                raise OrderValidationError("trailing_stop requires trailing_delta")

        # Iceberg
        if dto.type == "iceberg":
            if dto.visible_quantity is None or dto.hidden_quantity is None:
                raise OrderValidationError("iceberg requires visible_quantity and hidden_quantity")
            if dto.visible_quantity + dto.hidden_quantity != dto.quantity:
                raise OrderValidationError(
                    f"iceberg: visible({dto.visible_quantity}) + hidden({dto.hidden_quantity}) "
                    f"!= quantity({dto.quantity})"
                )
            if dto.visible_quantity <= 0:
                raise OrderValidationError("iceberg visible_quantity must be positive")

        # Tick size check (price must be a multiple of tick_size)
        if dto.price is not None and pair.tick_size > 0:
            remainder = dto.price % pair.tick_size
            if remainder != 0:
                raise OrderValidationError(
                    f"price {dto.price} not a multiple of tick_size {pair.tick_size}"
                )

    # ─── Lock amount calculation ────────────────────────────────────────────

    def _compute_lock_asset_and_amount(
        self,
        dto: OrderCreateDTO,
        pair: TradingPair,
    ) -> tuple[str, Decimal]:
        """Determine which asset to lock and how much.

        Returns (asset, amount). For sell orders, base asset is locked.
        For buy orders, quote asset is locked.

        Special cases:
        - MARKET BUY: requires checking the current best ask to compute
          worst-case lock. This is done separately in `_compute_market_buy_lock`.
        - STOP-type: returns ("", 0) — no lock at placement.
        """
        if dto.type in ("stop_market", "stop_limit", "trailing_stop"):
            return ("", Decimal("0"))  # no lock until trigger

        if dto.side == "sell":
            # Sell: lock base asset (the quantity being sold)
            return (pair.base_asset, dto.quantity)

        # Buy: lock quote asset
        if dto.type in ("limit", "post_only", "ioc", "fok", "iceberg"):
            assert dto.price is not None
            return (pair.quote_asset, dto.price * dto.quantity)

        # Market buy: handled separately (needs Redis lookup)
        return (pair.quote_asset, Decimal("0"))

    async def _compute_market_buy_lock(
        self,
        dto: OrderCreateDTO,
        pair: TradingPair,
    ) -> tuple[str, Decimal]:
        """For MARKET BUY, compute the actual cost by walking the ask side.

        Walks the order book from best ask upward, accumulating volume
        until the requested quantity is covered. The total cost = sum of
        (price * volume) at each level. If the book doesn't have enough
        volume, rejects the order.

        A small buffer (0.1%) is added to handle race conditions where
        the book changes between lock and match.
        """
        redis = get_redis()
        # Load asks from Redis (ascending price order)
        ask_orders = await orderbook.get_opposite_orders_for_match(
            redis, dto.symbol, "buy", limit=1000,
        )

        if not ask_orders:
            raise OrderValidationError(
                f"Cannot place MARKET BUY on {dto.symbol}: no asks available"
            )

        # Walk the asks and accumulate cost
        remaining_qty = dto.quantity
        total_cost = Decimal("0")

        for ask in ask_orders:
            if remaining_qty <= 0:
                break
            ask_price = Decimal(str(ask["price"]))
            ask_qty = Decimal(str(ask["visible_quantity"] if ask.get("is_iceberg") else ask["quantity"]))

            fill_qty = min(remaining_qty, ask_qty)
            total_cost += ask_price * fill_qty
            remaining_qty -= fill_qty

        if remaining_qty > 0:
            raise OrderValidationError(
                f"Cannot place MARKET BUY {dto.quantity} {pair.base_asset} on {dto.symbol}: "
                f"insufficient ask volume in the order book"
            )

        # Small buffer (0.1%) for race conditions between lock and match
        buffer = total_cost * Decimal("1.001")
        return (pair.quote_asset, buffer)

    # ─── PLACE ──────────────────────────────────────────────────────────────

    async def place_order(
        self,
        user_id: int,
        dto: OrderCreateDTO,
    ) -> Order:
        """Place a single order with atomic balance locking.

        Steps:
          1. Validate params
          2. Compute lock asset + amount (skip for stop-type)
          3. Lock balance (optimistic with retries)
          4. Insert Order row
          5. Insert SL/TP children (if any) as PENDING
          6. Enqueue action to matching worker
          7. Publish order/balance events

        All steps are in the caller's transaction; if any fails, the
        caller's `async with session.begin():` rolls back.
        """
        pair = await self._validate_and_get_pair(dto.symbol)
        self._validate_params(dto, pair)

        # Compute lock
        is_stop = dto.type in ("stop_market", "stop_limit", "trailing_stop")
        if is_stop:
            lock_asset, lock_amount = ("", Decimal("0"))
        elif dto.type == "market" and dto.side == "buy":
            lock_asset, lock_amount = await self._compute_market_buy_lock(dto, pair)
        else:
            lock_asset, lock_amount = self._compute_lock_asset_and_amount(dto, pair)

        # Lock balance (skip for stop-type)
        if not is_stop and lock_amount > 0:
            try:
                await self.balance_svc.lock_optimistic(
                    user_id, lock_asset, lock_amount,
                    reason="market_buy_estimate" if (dto.type == "market" and dto.side == "buy") else "order_placed",
                )
            except InsufficientBalanceError:
                raise

        # Determine initial status
        initial_status = OrderStatus.PENDING if is_stop else OrderStatus.NEW

        # Insert order
        order = Order(
            user_id=user_id,
            symbol=dto.symbol,
            side=OrderSide(dto.side),
            type=OrderType(dto.type),
            status=initial_status,
            price=dto.price,
            stop_price=dto.stop_price,
            trailing_delta=dto.trailing_delta,
            quantity=dto.quantity,
            visible_quantity=dto.visible_quantity if dto.type == "iceberg" else dto.quantity,
            hidden_quantity=dto.hidden_quantity if dto.type == "iceberg" else None,
            bulk_id=dto.bulk_id,
        )
        self.session.add(order)
        await self.session.flush()  # get order.id

        # Insert SL/TP children as PENDING
        if dto.sl is not None:
            sl_order = await self._create_sltp_child(
                user_id, dto, dto.sl, order, pair, is_sl=True,
            )
            order.sl_order_id = sl_order.id
        if dto.tp is not None:
            tp_order = await self._create_sltp_child(
                user_id, dto, dto.tp, order, pair, is_sl=False,
            )
            order.tp_order_id = tp_order.id

        await self.session.flush()

        # Enqueue action to matching worker (or stop queue)
        redis = get_redis()
        if is_stop:
            await self._register_stop_order(redis, order, dto)
        else:
            payload = queues.build_order_action_payload(
                action="place",
                order_id=order.id,
                symbol=order.symbol,
                user_id=user_id,
                side=order.side.value,
                type=order.type.value,
                price=float(order.price) if order.price else None,
                quantity=float(order.quantity),
                is_iceberg=(order.type == OrderType.ICEBERG),
                visible_qty=float(order.visible_quantity) if order.visible_quantity else None,
                hidden_qty=float(order.hidden_quantity) if order.hidden_quantity else None,
                parent_order_id=None,
                bulk_id=order.bulk_id,
            )
            await queues.enqueue_order_action(redis, payload)

            # Also add to Redis open-orders index + order HASH
            await orders_index.add_open_order(redis, user_id, order.symbol, order.id)
            metadata = {
                "order_id": str(order.id),
                "user_id": str(user_id),
                "symbol": order.symbol,
                "side": order.side.value,
                "type": order.type.value,
                "price": str(order.price) if order.price else "",
                "quantity": str(order.quantity),
                "visible_quantity": str(order.visible_quantity) if order.visible_quantity else "",
                "hidden_quantity": str(order.hidden_quantity) if order.hidden_quantity else "",
                "is_iceberg": "1" if order.type == OrderType.ICEBERG else "0",
            }
            await orderbook.store_order_hash(redis, order.id, metadata)

        # Publish order event
        await pubsub.publish_order_update(
            redis, user_id=user_id,
            event="placed" if not is_stop else "pending",
            order_id=order.id, symbol=order.symbol,
            side=order.side.value, type=order.type.value,
            status=order.status.value,
            price=float(order.price) if order.price else None,
            quantity=float(order.quantity),
            client_order_id=dto.client_order_id,
            bulk_id=order.bulk_id,
        )

        return order

    async def _create_sltp_child(
        self,
        user_id: int,
        parent_dto: OrderCreateDTO,
        config: SLTPConfig,
        parent: Order,
        pair: TradingPair,
        is_sl: bool,
    ) -> Order:
        """Create a pending SL or TP child order linked to `parent`."""
        # The child's side is opposite of the parent
        child_side = OrderSide.SELL if parent.side == OrderSide.BUY else OrderSide.BUY
        # Quantity defaults to parent's quantity
        child_qty = config.quantity if config.quantity is not None else parent.quantity

        child = Order(
            user_id=user_id,
            symbol=parent.symbol,
            side=child_side,
            type=OrderType(config.type),
            status=OrderStatus.PENDING,
            price=config.price,
            stop_price=config.stop_price,
            quantity=child_qty,
            parent_order_id=parent.id,
        )
        self.session.add(child)
        await self.session.flush()
        return child

    async def _register_stop_order(
        self,
        redis,
        order: Order,
        dto: OrderCreateDTO,
    ) -> None:
        """Register a stop-type order in the Redis stop queue."""
        if order.type == OrderType.TRAILING_STOP:
            # Trailing stop: register with trailing state
            await stops.register_trailing_stop(
                redis,
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                delta_type="abs",  # TODO: support pct via dto
                delta_value=order.trailing_delta or Decimal("0"),
                initial_extreme=order.stop_price or Decimal("0"),  # will be updated by monitor
                initial_trigger=order.stop_price or Decimal("0"),
            )
        else:
            # Plain stop: add to stops ZSET
            await stops.add_stop_order(
                redis,
                order_id=order.id,
                symbol=order.symbol,
                stop_price=order.stop_price or Decimal("0"),
            )
        # Add to open-orders index (stop orders are "open" while pending)
        await orders_index.add_open_order(redis, order.user_id, order.symbol, order.id)
        # Store order HASH
        metadata = {
            "order_id": str(order.id),
            "user_id": str(order.user_id),
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.type.value,
            "stop_price": str(order.stop_price) if order.stop_price else "",
            "quantity": str(order.quantity),
            "is_iceberg": "0",
        }
        await orderbook.store_order_hash(redis, order.id, metadata)

    # ─── CANCEL ─────────────────────────────────────────────────────────────

    async def cancel_order(
        self,
        user_id: int,
        order_id: int,
    ) -> Order:
        """Cancel a single order and unlock remaining balance.

        Steps:
          1. Load order (verify ownership)
          2. Verify it's active (NEW / PARTIALLY_FILLED / PENDING)
          3. Compute unlock amount based on remaining quantity
          4. Unlock balance
          5. Update order status to CANCELED
          6. Cancel SL/TP children
          7. Remove from Redis book + indexes
          8. Enqueue cancel action to matching worker (for in-flight matches)
          9. Publish events
        """
        order = await self.repo.get_by_user(order_id, user_id)
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} not found for user {user_id}")

        if not order.is_active:
            raise OrderNotCancelableError(
                f"Order {order_id} is in status {order.status.value} — cannot cancel"
            )

        pair = await self.pair_repo.get_by_symbol(order.symbol)
        assert pair is not None

        # Compute unlock amount
        remaining = order.remaining_quantity
        if order.side == OrderSide.SELL:
            unlock_asset = pair.base_asset
            unlock_amount = remaining
        else:
            unlock_asset = pair.quote_asset
            if order.price is not None:
                unlock_amount = remaining * order.price
            else:
                # Market buy: unlock the worst-case estimate (stored nowhere —
                # we need to look it up from Redis or recompute). For simplicity,
                # assume the full locked amount is the worst-case.
                # TODO: track locked_amount on the Order row.
                unlock_amount = remaining * (order.stop_price or Decimal("0"))

        # Only unlock if this is not a stop-type (stops don't lock at placement)
        is_stop = order.type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP)
        if not is_stop and unlock_amount > 0:
            # Use PESSIMISTIC locking (FOR UPDATE) instead of optimistic.
            # The matching worker may have just modified the balance (version bump),
            # causing optimistic lock to fail with BalanceVersionConflict.
            # Pessimistic locking waits for the worker's transaction to commit,
            # then reads the current locked_balance and unlocks correctly.
            balance = await self.balance_svc.repo.get_for_update(user_id, unlock_asset)
            if balance is not None:
                # Only unlock what's actually locked (may be less than calculated
                # if the worker partially settled)
                actual_unlock = min(unlock_amount, balance.locked_balance)
                if actual_unlock > 0:
                    await self.balance_svc.unlock_pessimistic(
                        balance, actual_unlock,
                        reason="order_canceled", order_id=order.id,
                    )

        # Update status
        await self.repo.update_status(order.id, OrderStatus.CANCELED)

        # Cancel SL/TP children
        await self.repo.cancel_children(order.id)

        # Redis cleanup
        redis = get_redis()
        if is_stop:
            if order.type == OrderType.TRAILING_STOP:
                await stops.remove_trailing_stop(redis, order.id, order.symbol)
            else:
                await stops.remove_stop_order(redis, order.id, order.symbol)
        else:
            await orderbook.remove_resting_order(
                redis, order.id, order.symbol, order.side,
                order.price or Decimal("0"), 0,
            )
            await orderbook.delete_order_hash(redis, order.id)
        await orders_index.remove_open_order(redis, user_id, order.symbol, order.id)

        # Enqueue cancel action (so matching worker can skip in-flight matches)
        payload = queues.build_order_action_payload(
            action="cancel",
            order_id=order.id,
            symbol=order.symbol,
            user_id=user_id,
            side=order.side.value,
            type=order.type.value,
        )
        await queues.enqueue_order_action(redis, payload)

        # Publish events
        await pubsub.publish_order_update(
            redis, user_id=user_id, event="canceled",
            order_id=order.id, symbol=order.symbol,
            side=order.side.value, type=order.type.value,
            status=OrderStatus.CANCELED.value,
            filled_quantity=float(order.filled_quantity),
            remaining_quantity=0.0,
        )

        # Publish orderbook snapshot so the frontend updates the order book
        # immediately after an order is removed (not just on new trades).
        snap = await orderbook.get_book_snapshot(redis, order.symbol, depth=20)
        await pubsub.publish_orderbook_snapshot(
            redis, order.symbol,
            bids=[[p, q] for p, q in snap["bids"]],
            asks=[[p, q] for p, q in snap["asks"]],
        )

        # Reload to get final state
        await self.session.refresh(order)
        return order

    # ─── MODIFY (Cancel-Replace) ────────────────────────────────────────────

    async def modify_order(
        self,
        user_id: int,
        order_id: int,
        new_price: Decimal | float,
        new_quantity: Decimal | float,
    ) -> Order:
        """Cancel-Replace: cancel old order, place new one in single transaction.

        Steps:
          1. Load old order with FOR UPDATE
          2. Verify active
          3. Unlock old remaining balance
          4. Validate new params, lock new balance
          5. Mark old as CANCELED (with replaced_by_id)
          6. Insert new order (with replaces_id = old.id)
          7. Enqueue modify action
          8. Publish events

        The new order gets a fresh `created_at` so it goes to the back of
        the queue at its price level (strict Price-Time Priority).
        """
        old_order = await self.repo.get_for_update(order_id)
        if old_order is None or old_order.user_id != user_id:
            raise OrderNotFoundError(f"Order {order_id} not found for user {user_id}")

        if not old_order.is_active:
            raise OrderNotCancelableError(
                f"Order {order_id} is in status {old_order.status.value} — cannot modify"
            )

        if old_order.type not in (OrderType.LIMIT, OrderType.POST_ONLY, OrderType.ICEBERG):
            raise OrderNotCancelableError(
                f"Cannot modify order type {old_order.type.value} (only limit/post_only/iceberg)"
            )

        pair = await self.pair_repo.get_by_symbol(old_order.symbol)
        assert pair is not None

        # 1. Unlock old remaining balance
        remaining = old_order.remaining_quantity
        if old_order.side == OrderSide.SELL:
            old_unlock_asset = pair.base_asset
            old_unlock_amount = remaining
        else:
            old_unlock_asset = pair.quote_asset
            old_unlock_amount = remaining * (old_order.price or Decimal("0"))

        if old_unlock_amount > 0:
            await self.balance_svc.unlock_optimistic(
                user_id, old_unlock_asset, old_unlock_amount,
                reason="order_modified", order_id=old_order.id,
            )

        # 2. Validate new params + lock new balance
        new_price_d = Decimal(str(new_price))
        new_qty_d = Decimal(str(new_quantity))

        if new_price_d <= 0:
            raise OrderValidationError(f"new price must be positive, got {new_price}")
        if new_qty_d <= 0:
            raise OrderValidationError(f"new quantity must be positive, got {new_quantity}")
        if new_qty_d < pair.min_lot_size:
            raise OrderValidationError(
                f"new quantity {new_qty_d} below min_lot_size {pair.min_lot_size}"
            )

        # Lock for new params
        if old_order.side == OrderSide.SELL:
            new_lock_asset = pair.base_asset
            new_lock_amount = new_qty_d
        else:
            new_lock_asset = pair.quote_asset
            new_lock_amount = new_price_d * new_qty_d

        try:
            await self.balance_svc.lock_optimistic(
                user_id, new_lock_asset, new_lock_amount,
                reason="order_modified", order_id=old_order.id,
            )
        except InsufficientBalanceError:
            # Rollback: re-lock the old amount and reject
            await self.balance_svc.lock_optimistic(
                user_id, old_unlock_asset, old_unlock_amount,
                reason="order_placed", order_id=old_order.id,
            )
            raise

        # 3. Mark old as CANCELED
        old_order.replaced_by_id = None  # will set after new is created
        await self.repo.update_status(old_order.id, OrderStatus.CANCELED)

        # 4. Insert new order
        new_order = Order(
            user_id=user_id,
            symbol=old_order.symbol,
            side=old_order.side,
            type=old_order.type,
            status=OrderStatus.NEW,
            price=new_price_d,
            quantity=new_qty_d,
            visible_quantity=(old_order.visible_quantity if old_order.type == OrderType.ICEBERG else new_qty_d),
            hidden_quantity=(old_order.hidden_quantity if old_order.type == OrderType.ICEBERG else None),
            replaces_id=old_order.id,
            replace_count=old_order.replace_count + 1,
        )
        self.session.add(new_order)
        await self.session.flush()

        # Link old → new
        old_order.replaced_by_id = new_order.id
        await self.session.flush()

        # 5. Redis updates: remove old, add new
        redis = get_redis()
        await orderbook.remove_resting_order(
            redis, old_order.id, old_order.symbol, old_order.side,
            old_order.price or Decimal("0"), 0,
        )
        await orderbook.delete_order_hash(redis, old_order.id)
        await orders_index.remove_open_order(redis, user_id, old_order.symbol, old_order.id)

        # Add new to book + index
        await orders_index.add_open_order(redis, user_id, new_order.symbol, new_order.id)
        metadata = {
            "order_id": str(new_order.id),
            "user_id": str(user_id),
            "symbol": new_order.symbol,
            "side": new_order.side.value,
            "type": new_order.type.value,
            "price": str(new_order.price),
            "quantity": str(new_order.quantity),
            "visible_quantity": str(new_order.visible_quantity) if new_order.visible_quantity else "",
            "hidden_quantity": str(new_order.hidden_quantity) if new_order.hidden_quantity else "",
            "is_iceberg": "1" if new_order.type == OrderType.ICEBERG else "0",
        }
        await orderbook.store_order_hash(redis, new_order.id, metadata)

        # 6. Enqueue modify action
        payload = queues.build_order_action_payload(
            action="modify",
            order_id=new_order.id,
            symbol=new_order.symbol,
            user_id=user_id,
            side=new_order.side.value,
            type=new_order.type.value,
            price=float(new_order.price) if new_order.price else None,
            quantity=float(new_order.quantity),
            replaces_id=old_order.id,
        )
        await queues.enqueue_order_action(redis, payload)

        # 7. Publish events
        await pubsub.publish_order_update(
            redis, user_id=user_id, event="modified",
            order_id=new_order.id, symbol=new_order.symbol,
            side=new_order.side.value, type=new_order.type.value,
            status=new_order.status.value,
            price=float(new_order.price),
            quantity=float(new_order.quantity),
        )

        return new_order

    # ─── BULK PLACE ─────────────────────────────────────────────────────────

    async def place_bulk_orders(
        self,
        user_id: int,
        dtos: list[OrderCreateDTO],
        bulk_id: str | None = None,
    ) -> list[Order]:
        """Place multiple orders atomically (All-or-Nothing).

        1. Validate ALL orders first.
        2. Sum balance requirements per asset.
        3. Lock all required balances in one pass.
           If ANY asset is insufficient → reject the entire batch (no locks).
        4. Insert all orders.
        5. Enqueue all actions.
        6. Publish bulk result.

        Returns the list of created Order objects on success.
        Raises InsufficientBalanceError on failure (no orders created).
        """
        if not dtos:
            return []

        bulk_id = bulk_id or str(uuid.uuid4())
        for dto in dtos:
            dto.bulk_id = bulk_id

        # 1. Validate all + compute lock requirements
        lock_requirements: dict[str, Decimal] = {}  # asset → total amount
        validated: list[tuple[OrderCreateDTO, TradingPair, tuple[str, Decimal]]] = []

        for dto in dtos:
            pair = await self._validate_and_get_pair(dto.symbol)
            self._validate_params(dto, pair)

            is_stop = dto.type in ("stop_market", "stop_limit", "trailing_stop")
            if is_stop:
                lock_asset, lock_amount = ("", Decimal("0"))
            elif dto.type == "market" and dto.side == "buy":
                lock_asset, lock_amount = await self._compute_market_buy_lock(dto, pair)
            else:
                lock_asset, lock_amount = self._compute_lock_asset_and_amount(dto, pair)

            if lock_asset and lock_amount > 0:
                lock_requirements[lock_asset] = (
                    lock_requirements.get(lock_asset, Decimal("0")) + lock_amount
                )
            validated.append((dto, pair, (lock_asset, lock_amount)))

        # 2. Lock all required balances (sorted by asset to avoid deadlocks)
        for asset in sorted(lock_requirements.keys()):
            amount = lock_requirements[asset]
            try:
                await self.balance_svc.lock_optimistic(
                    user_id, asset, amount, reason="order_placed",
                )
            except InsufficientBalanceError:
                # Rollback: unlock everything we've locked so far
                for prev_asset in sorted(lock_requirements.keys()):
                    if prev_asset == asset:
                        break
                    try:
                        await self.balance_svc.unlock_optimistic(
                            user_id, prev_asset, lock_requirements[prev_asset],
                            reason="order_rejected",
                        )
                    except Exception as e:
                        logger.error("Failed to unlock during bulk rollback: %s", e)
                # Publish bulk failure
                redis = get_redis()
                await pubsub.publish_bulk_result(
                    redis, user_id, bulk_id, action="place",
                    total=len(dtos), succeeded=0,
                    failed=[{"index": 0, "code": "insufficient_balance",
                             "message": f"Not enough {asset}"}],
                )
                raise

        # 3. Insert all orders
        orders: list[Order] = []
        redis = get_redis()
        for dto, pair, (lock_asset, lock_amount) in validated:
            is_stop = dto.type in ("stop_market", "stop_limit", "trailing_stop")
            initial_status = OrderStatus.PENDING if is_stop else OrderStatus.NEW

            order = Order(
                user_id=user_id,
                symbol=dto.symbol,
                side=OrderSide(dto.side),
                type=OrderType(dto.type),
                status=initial_status,
                price=dto.price,
                stop_price=dto.stop_price,
                trailing_delta=dto.trailing_delta,
                quantity=dto.quantity,
                visible_quantity=dto.visible_quantity if dto.type == "iceberg" else dto.quantity,
                hidden_quantity=dto.hidden_quantity if dto.type == "iceberg" else None,
                bulk_id=bulk_id,
            )
            self.session.add(order)
            await self.session.flush()
            orders.append(order)

            # SL/TP children
            if dto.sl is not None:
                sl_child = await self._create_sltp_child(user_id, dto, dto.sl, order, pair, is_sl=True)
                order.sl_order_id = sl_child.id
            if dto.tp is not None:
                tp_child = await self._create_sltp_child(user_id, dto, dto.tp, order, pair, is_sl=False)
                order.tp_order_id = tp_child.id

            await self.session.flush()

            # Redis registration
            if is_stop:
                await self._register_stop_order(redis, order, dto)
            else:
                payload = queues.build_order_action_payload(
                    action="place",
                    order_id=order.id,
                    symbol=order.symbol,
                    user_id=user_id,
                    side=order.side.value,
                    type=order.type.value,
                    price=float(order.price) if order.price else None,
                    quantity=float(order.quantity),
                    is_iceberg=(order.type == OrderType.ICEBERG),
                    visible_qty=float(order.visible_quantity) if order.visible_quantity else None,
                    hidden_qty=float(order.hidden_quantity) if order.hidden_quantity else None,
                    bulk_id=bulk_id,
                )
                await queues.enqueue_order_action(redis, payload)
                await orders_index.add_open_order(redis, user_id, order.symbol, order.id)
                metadata = {
                    "order_id": str(order.id),
                    "user_id": str(user_id),
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "type": order.type.value,
                    "price": str(order.price) if order.price else "",
                    "quantity": str(order.quantity),
                    "visible_quantity": str(order.visible_quantity) if order.visible_quantity else "",
                    "hidden_quantity": str(order.hidden_quantity) if order.hidden_quantity else "",
                    "is_iceberg": "1" if order.type == OrderType.ICEBERG else "0",
                    "bulk_id": bulk_id,
                }
                await orderbook.store_order_hash(redis, order.id, metadata)

        # 4. Publish bulk success
        await pubsub.publish_bulk_result(
            redis, user_id, bulk_id, action="place",
            total=len(dtos), succeeded=len(orders), failed=[],
        )

        return orders

    # ─── BULK CANCEL ────────────────────────────────────────────────────────

    async def cancel_bulk_orders(
        self,
        user_id: int,
        order_ids: list[int] | None = None,
        symbol: str | None = None,
        cancel_all: bool = False,
    ) -> list[int]:
        """Cancel multiple orders atomically.

        Modes:
          - `order_ids` provided → cancel those specific orders
          - `cancel_all=True, symbol=X` → cancel all user's orders on symbol X
          - `cancel_all=True, symbol=None` → cancel all user's orders

        Returns the list of canceled order IDs.
        """
        redis = get_redis()

        # Determine which orders to cancel
        if cancel_all:
            if symbol:
                ids_to_cancel = await orders_index.get_user_open_orders_for_symbol(
                    redis, user_id, symbol,
                )
            else:
                ids_to_cancel = await orders_index.get_user_open_orders(redis, user_id)
        else:
            ids_to_cancel = order_ids or []

        if not ids_to_cancel:
            return []

        canceled: list[int] = []
        failed: list[dict[str, Any]] = []
        total_unlocked: dict[str, Decimal] = {}

        for oid in ids_to_cancel:
            try:
                order = await self.cancel_order(user_id, oid)
                canceled.append(oid)
                # Track unlocked (for bulk result event)
                # Note: cancel_order already publishes per-order events
            except OrderNotFoundError:
                failed.append({"index": oid, "code": "not_found", "message": "Order not found"})
            except OrderNotCancelableError as e:
                failed.append({"index": oid, "code": "not_cancelable", "message": str(e)})

        # Publish bulk cancel result
        await pubsub.publish_bulk_result(
            redis, user_id, str(uuid.uuid4()), action="cancel",
            total=len(ids_to_cancel), succeeded=len(canceled),
            failed=failed,
        )

        return canceled

    # ─── LIST ───────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        user_id: int,
        *,
        symbol: str | None = None,
        statuses: list[OrderStatus] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Order]:
        """List user's orders with optional filters."""
        return list(await self.repo.list_user_orders(
            user_id, symbol=symbol, statuses=statuses,
            offset=offset, limit=limit,
        ))


__all__ = [
    "OrderService",
    "OrderCreateDTO",
    "SLTPConfig",
    "OrderValidationError",
    "OrderNotFoundError",
    "OrderNotCancelableError",
    "PostOnlyCrossError",
]
