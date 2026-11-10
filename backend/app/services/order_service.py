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
from functools import partial
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
from app.services.post_commit import defer_after_commit

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
        user_id: int,
    ) -> tuple[str, Decimal]:
        """For MARKET BUY, compute the actual cost by walking the ask side.

        Walks the order book from best ask upward, accumulating volume
        until the requested quantity is covered. The total cost = sum of
        (price * volume) at each level. If the book doesn't have enough
        volume, rejects the order.

        Self-trade prevention: the taker's OWN asks are excluded from the
        walk — the matcher will never fill against them, so counting them
        here would under-price the lock and make the real (worse) fill
        fail settlement with "Insufficient locked balance".

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
            if user_id and ask.get("user_id") == user_id:
                continue  # self-trade prevention: matcher skips these
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

    async def _require_market_sell_liquidity(
        self,
        dto: "OrderCreateDTO",
        pair: TradingPair,
        user_id: int,
    ) -> None:
        """Symmetric counterpart of the MARKET BUY book walk: a MARKET SELL
        against an empty/thin bid side would otherwise settle as 'expired'
        moments after appearing as 'new' in the UI. Reject upfront instead.

        Self-trade prevention mirror: the taker's OWN bids are excluded —
        the matcher will never fill against them.
        """
        redis = get_redis()
        bid_orders = await orderbook.get_opposite_orders_for_match(
            redis, dto.symbol, "sell", limit=1000,
        )
        if not bid_orders:
            raise OrderValidationError(
                f"Cannot place MARKET SELL on {dto.symbol}: no bids available"
            )

        remaining_qty = dto.quantity
        for bid in bid_orders:
            if remaining_qty <= 0:
                break
            if user_id and bid.get("user_id") == user_id:
                continue  # self-trade prevention: matcher skips these
            bid_qty = Decimal(str(bid["visible_quantity"] if bid.get("is_iceberg") else bid["quantity"]))
            remaining_qty -= bid_qty

        if remaining_qty > 0:
            raise OrderValidationError(
                f"Cannot place MARKET SELL {dto.quantity} {pair.base_asset} on {dto.symbol}: "
                f"insufficient bid volume in the order book"
            )

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
            lock_asset, lock_amount = await self._compute_market_buy_lock(dto, pair, user_id)
        elif dto.type == "market" and dto.side == "sell":
            await self._require_market_sell_liquidity(dto, pair, user_id)
            lock_asset, lock_amount = self._compute_lock_asset_and_amount(dto, pair)
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

        # Insert order. `locked_amount` records the exact reservation this
        # order owns so cancel/modify can refund it precisely (0 for stops —
        # they lock nothing at placement).
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
            locked_amount=lock_amount if not is_stop else Decimal("0"),
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

        # Redis side effects (queue push, open-orders index, order HASH,
        # pub/sub events) are DEFERRED until after the DB COMMIT.
        # Enqueuing before commit raced the matching worker: it BRPOPed the
        # place action, read the taker row as MISSING (uncommitted INSERT),
        # refused the match and stranded the order in 'new' with funds locked.
        # Same rationale for publish: pub/sub is fire-and-forget, a rollback
        # after publish leaves ghost events clients can never un-see.
        # See app/services/post_commit.py.
        redis = get_redis()
        if is_stop:
            defer_after_commit(
                self.session,
                partial(self._register_stop_order, redis,
                        self._stop_order_snapshot(order, user_id)),
            )
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
            defer_after_commit(
                self.session,
                partial(self._place_redis_effects, redis, user_id, payload, metadata),
            )

        # Publish order event (deferred; runs after commit)
        defer_after_commit(self.session, partial(
            pubsub.publish_order_update, redis,
            user_id=user_id,
            event="placed" if not is_stop else "pending",
            order_id=order.id, symbol=order.symbol,
            side=order.side.value, type=order.type.value,
            status=order.status.value,
            price=float(order.price) if order.price else None,
            quantity=float(order.quantity),
            client_order_id=dto.client_order_id,
            bulk_id=order.bulk_id,
        ))

        return order

    async def _place_redis_effects(
        self,
        redis,
        user_id: int,
        payload: dict[str, Any],
        metadata: dict[str, str],
    ) -> None:
        """Post-commit Redis mutations for a freshly placed order.

        Order matters: enqueue first (worker only matches rows that are
        already committed by definition now), then index + HASH.
        """
        await queues.enqueue_order_action(redis, payload)
        await orders_index.add_open_order(redis, user_id, payload["symbol"], payload["order_id"])
        await orderbook.store_order_hash(redis, payload["order_id"], metadata)

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
        od: dict[str, Any],
    ) -> None:
        """Register a stop-type order in the Redis stop queue.

        Takes a PLAIN VALUE SNAPSHOT (not the ORM object): this runs as a
        post-commit deferred effect, and by then the request session may
        already be closed — expired instances would explode on attribute
        access (found by P5 coverage: stops silently failed to arm).
        """
        if od["type"] == OrderType.TRAILING_STOP.value:
            # Trailing stop: register with trailing state
            await stops.register_trailing_stop(
                redis,
                order_id=od["order_id"],
                symbol=od["symbol"],
                side=OrderSide(od["side"]),
                delta_type="abs",  # TODO: support pct
                delta_value=od["trailing_delta"],
                initial_extreme=od["stop_price"],  # will be updated by monitor
                initial_trigger=od["stop_price"],
            )
        else:
            # Plain stop: add to stops ZSET
            await stops.add_stop_order(
                redis,
                order_id=od["order_id"],
                symbol=od["symbol"],
                stop_price=od["stop_price"],
            )
        # Add to open-orders index (stop orders are "open" while pending)
        await orders_index.add_open_order(redis, od["user_id"], od["symbol"], od["order_id"])
        # Store order HASH
        metadata = {
            "order_id": str(od["order_id"]),
            "user_id": str(od["user_id"]),
            "symbol": od["symbol"],
            "side": od["side"],
            "type": od["type"],
            "stop_price": str(od["stop_price"]) if od["stop_price"] else "",
            "quantity": str(od["quantity"]),
            "is_iceberg": "0",
        }
        await orderbook.store_order_hash(redis, od["order_id"], metadata)

        # Public stop-ladder visibility: this level just appeared
        await pubsub.publish_stops_changed(redis, od["symbol"])

    def _stop_order_snapshot(self, order: Order, user_id: int) -> dict[str, Any]:
        """Value snapshot for _register_stop_order (safe after session close)."""
        return {
            "order_id": order.id,
            "user_id": user_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.type.value,
            "stop_price": order.stop_price or Decimal("0"),
            "trailing_delta": order.trailing_delta or Decimal("0"),
            "quantity": order.quantity,
        }

    # ─── CANCEL ─────────────────────────────────────────────────────────────

    async def cancel_order(
        self,
        user_id: int,
        order_id: int,
    ) -> Order:
        """Cancel a single order and unlock remaining balance.

        Steps:
          1. Load order WITH FOR UPDATE (serializes vs worker settlement)
          2. Verify it's active (NEW / PARTIALLY_FILLED / PENDING)
          3. Unlock exactly `locked_amount` (per-order reservation)
          4. Unlock balance
          5. Update order status to CANCELED
          6. Cancel SL/TP children
          7. Remove from Redis book + indexes
          8. Enqueue cancel action to matching worker (for in-flight matches)
          9. Publish events
        """
        # FOR UPDATE: the matching worker may be settling a fill on this row
        # right now (decrementing locked_amount). Row lock serializes us.
        order = await self.repo.get_by_user_for_update(order_id, user_id)
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} not found for user {user_id}")

        if not order.is_active:
            raise OrderNotCancelableError(
                f"Order {order_id} is in status {order.status.value} — cannot cancel"
            )

        pair = await self.pair_repo.get_by_symbol(order.symbol)
        assert pair is not None

        # Unlock EXACTLY what this order still reserves.
        # Recomputing from remaining*price leaks funds:
        #   * market buy has no price → unlock 0 (funds stuck forever);
        #   * limit buy filled at better prices → over-unlocks the savings.
        # `locked_amount` is maintained by settlement on every fill and is
        # therefore the authoritative residual reservation.
        is_stop = order.type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP)
        if order.side == OrderSide.SELL:
            unlock_asset = pair.base_asset
        else:
            unlock_asset = pair.quote_asset
        if is_stop:
            # Stops lock nothing at placement; their funded children carry
            # their own locked_amount and are canceled separately.
            unlock_amount = Decimal("0")
        elif order.status == OrderStatus.PENDING:
            unlock_amount = Decimal("0")
        else:
            unlock_amount = order.locked_amount or Decimal("0")

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

        # Redis cleanup — ALL deferred until after commit (see post_commit.py):
        # if this transaction rolls back, the order must stay on the book and
        # no ghost 'canceled' event may reach the client. Values are captured
        # now (pre-commit) so the deferred task needs no live ORM state.
        redis = get_redis()
        _oid, _sym, _side = order.id, order.symbol, order.side
        _price = order.price or Decimal("0")
        _type = order.type.value
        _side_s = order.side.value
        _filled = float(order.filled_quantity)
        _is_stop = is_stop
        _is_trailing = order.type == OrderType.TRAILING_STOP

        async def _cancel_redis_effects() -> None:
            if _is_stop:
                if _is_trailing:
                    await stops.remove_trailing_stop(redis, _oid, _sym)
                else:
                    await stops.remove_stop_order(redis, _oid, _sym)
            else:
                await orderbook.remove_resting_order(
                    redis, _oid, _sym, _side, _price, 0,
                )
                await orderbook.delete_order_hash(redis, _oid)
            await orders_index.remove_open_order(redis, user_id, _sym, _oid)

            if _is_stop:
                # Public stop-ladder visibility: the level lost volume/vanished
                await pubsub.publish_stops_changed(redis, _sym)

            # Enqueue cancel action (so matching worker can skip in-flight matches)
            payload = queues.build_order_action_payload(
                action="cancel",
                order_id=_oid,
                symbol=_sym,
                user_id=user_id,
                side=_side_s,
                type=_type,
            )
            await queues.enqueue_order_action(redis, payload)

            await pubsub.publish_order_update(
                redis, user_id=user_id, event="canceled",
                order_id=_oid, symbol=_sym,
                side=_side_s, type=_type,
                status=OrderStatus.CANCELED.value,
                filled_quantity=_filled,
                remaining_quantity=0.0,
            )

            # Publish orderbook snapshot so the frontend updates the order book
            # immediately after an order is removed (not just on new trades).
            # Read AFTER the removal above → snapshot reflects truth.
            snap = await orderbook.get_book_snapshot(redis, _sym, depth=20)
            await pubsub.publish_orderbook_snapshot(
                redis, _sym,
                bids=[[p, q] for p, q in snap["bids"]],
                asks=[[p, q] for p, q in snap["asks"]],
            )

        defer_after_commit(self.session, _cancel_redis_effects)

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

        # 1. Unlock old remaining balance — exact per-order reservation
        # (locked_amount already accounts for partial fills settled by the
        # worker; recomputing from remaining*price would over/under-unlock).
        if old_order.side == OrderSide.SELL:
            old_unlock_asset = pair.base_asset
        else:
            old_unlock_asset = pair.quote_asset
        old_unlock_amount = old_order.locked_amount or Decimal("0")

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

        # 4. Insert new order (fresh reservation for the new parameters)
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
            locked_amount=new_lock_amount,
        )
        self.session.add(new_order)
        await self.session.flush()

        # Link old → new
        old_order.replaced_by_id = new_order.id
        await self.session.flush()

        # 5-7. Redis updates + worker actions + events — ALL deferred until
        # after commit (see post_commit.py). Values captured now; the new
        # order row MUST be committed before the worker may see its action.
        redis = get_redis()
        _old_id, _new_id = old_order.id, new_order.id
        _sym, _old_side, _old_side_s = old_order.symbol, old_order.side, old_order.side.value
        _old_price = old_order.price or Decimal("0")
        _old_type = old_order.type.value
        _new_side_s = new_order.side.value
        _new_type = new_order.type.value
        _new_price = new_order.price
        _new_quantity = new_order.quantity
        _new_status = new_order.status.value
        _is_iceberg = new_order.type == OrderType.ICEBERG
        _vis = float(new_order.visible_quantity) if new_order.visible_quantity else None
        _hid = float(new_order.hidden_quantity) if new_order.hidden_quantity else None
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

        async def _modify_redis_effects() -> None:
            # Remove old from book + index
            await orderbook.remove_resting_order(
                redis, _old_id, _sym, _old_side, _old_price, 0,
            )
            await orderbook.delete_order_hash(redis, _old_id)
            await orders_index.remove_open_order(redis, user_id, _sym, _old_id)

            # Add new to book + index
            await orders_index.add_open_order(redis, user_id, _sym, _new_id)
            await orderbook.store_order_hash(redis, _new_id, metadata)

            # 6. Enqueue worker actions.
            # The worker pipeline understands "place" end-to-end (match → settle
            # → rest remainder). A dedicated "modify" action was never implemented
            # there — the new order stayed NEW forever, invisible to matching while
            # its funds stayed locked. So we enqueue the new order as a regular
            # placement, preceded by a cancel no-op for the old id (ack'ed by the
            # worker) to keep in-flight pipelines consistent.
            cancel_payload = queues.build_order_action_payload(
                action="cancel",
                order_id=_old_id,
                symbol=_sym,
                user_id=user_id,
                side=_old_side_s,
                type=_old_type,
            )
            await queues.enqueue_order_action(redis, cancel_payload)

            payload = queues.build_order_action_payload(
                action="place",
                order_id=_new_id,
                symbol=_sym,
                user_id=user_id,
                side=_new_side_s,
                type=_new_type,
                price=float(_new_price) if _new_price else None,
                quantity=float(_new_quantity),
                is_iceberg=_is_iceberg,
                visible_qty=_vis,
                hidden_qty=_hid,
                replaces_id=_old_id,
            )
            await queues.enqueue_order_action(redis, payload)

            # 7. Publish events
            await pubsub.publish_order_update(
                redis, user_id=user_id, event="modified",
                order_id=_new_id, symbol=_sym,
                side=_new_side_s, type=_new_type,
                status=_new_status,
                price=float(_new_price),
                quantity=float(_new_quantity),
            )

        defer_after_commit(self.session, _modify_redis_effects)

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
                lock_asset, lock_amount = await self._compute_market_buy_lock(dto, pair, user_id)
            elif dto.type == "market" and dto.side == "sell":
                await self._require_market_sell_liquidity(dto, pair, user_id)
                lock_asset, lock_amount = self._compute_lock_asset_and_amount(dto, pair)
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
                locked_amount=lock_amount if not is_stop else Decimal("0"),
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
                defer_after_commit(
                    self.session, partial(self._register_stop_order, redis,
                                          self._stop_order_snapshot(order, user_id)),
                )
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
                # Deferred to after the single bulk COMMIT — per-order enqueue
                # before commit would race the worker exactly like the
                # single-place path did.
                defer_after_commit(
                    self.session,
                    partial(self._place_redis_effects, redis, user_id, payload, metadata),
                )

        # 4. Publish bulk success (after commit — the orders must be real)
        defer_after_commit(self.session, partial(
            pubsub.publish_bulk_result,
            redis, user_id, bulk_id, action="place",
            total=len(dtos), succeeded=len(orders), failed=[],
        ))

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

        # Publish bulk cancel result (after commit)
        defer_after_commit(self.session, partial(
            pubsub.publish_bulk_result,
            redis, user_id, str(uuid.uuid4()), action="cancel",
            total=len(ids_to_cancel), succeeded=len(canceled),
            failed=failed,
        ))

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
