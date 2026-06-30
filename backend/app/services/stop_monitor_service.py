"""Stop monitor service — evaluate stop triggers and recompute trailing stops.

Runs as a background asyncio task that subscribes to `pub:trades:{symbol}`
channels. On each new trade print:

  1. Query `stops:{symbol}` for orders whose trigger has been crossed.
  2. For each triggered stop:
     a. Load the order from PG (or Redis HASH)
     b. Verify side matches (sell-stop vs buy-stop logic)
     c. Convert to a market or limit order (per OrderType)
     d. Lock balance (now that the trigger has fired)
     e. Enqueue the new order to `queue:orders`
     f. Remove from stop queue
  3. Update trailing stops: recompute trigger based on new high/low.

For trailing stops, the monitor also tracks local extremes (high/low)
per symbol and calls `update_trailing_extremes()` when a new extreme is
observed.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.enums import OrderSide, OrderStatus, OrderType
from app.redis_client import get_redis
from app.redis_client import orderbook, pubsub, queues, stops
from app.repositories.order_repo import OrderRepository
from app.repositories.trading_pair_repo import TradingPairRepository
from app.services.balance_service import BalanceService
from app.services.order_service import OrderCreateDTO, OrderService

logger = logging.getLogger(__name__)


class StopMonitorService:
    """Background task that monitors trade prints and triggers stop orders.

    Lifecycle
    ----------
    1. `start()` — spawns a background asyncio task per active symbol.
    2. Each task subscribes to `pub:trades:{symbol}` and processes prints.
    3. `stop()` — cancels all background tasks.

    The monitor is idempotent: if it crashes and restarts, it re-scans
    pending stops from PostgreSQL and re-evaluates them against the last
    trade price.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}  # symbol → task
        self._running = False
        # Track local extremes per symbol for trailing stops
        self._local_high: dict[str, Decimal] = {}
        self._local_low: dict[str, Decimal] = {}
        self._refresh_task: asyncio.Task | None = None

    async def start(self, symbols: list[str]) -> None:
        """Start monitoring the given symbols + periodic refresh for new pairs."""
        self._running = True
        for symbol in symbols:
            if symbol not in self._tasks:
                task = asyncio.create_task(self._monitor_symbol(symbol))
                self._tasks[symbol] = task
                logger.info("Stop monitor started for %s", symbol)

        # Re-evaluate existing pending stops against last trade price
        await self._rescan_pending_stops(symbols)

        # Start periodic refresh — discovers new trading pairs created via admin
        self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def _periodic_refresh(self) -> None:
        """Periodically check for new active trading pairs and start monitoring them.

        This ensures stop orders work on pairs created via admin after startup.
        Runs every 30 seconds.
        """
        while self._running:
            try:
                await asyncio.sleep(30)
                if not self._running:
                    break

                from app.db.session import async_session_factory
                from app.repositories.trading_pair_repo import TradingPairRepository

                async with async_session_factory() as session:
                    repo = TradingPairRepository(session)
                    pairs = await repo.list_active()

                for pair in pairs:
                    if pair.symbol not in self._tasks:
                        task = asyncio.create_task(self._monitor_symbol(pair.symbol))
                        self._tasks[pair.symbol] = task
                        logger.info("Stop monitor auto-started for new pair: %s", pair.symbol)
                        # Rescan pending stops for this new symbol
                        await self._rescan_pending_stops([pair.symbol])

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Stop monitor refresh error: %s", e)

    async def stop(self) -> None:
        """Stop all monitoring tasks."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        logger.info("Stop monitor stopped")

    async def _monitor_symbol(self, symbol: str) -> None:
        """Subscribe to trades for one symbol and process triggers.

        Uses a dedicated Redis connection (via pubsub.subscribe()) to avoid
        blocking-command interference with the shared pool's health checks.
        Retries on connection errors with exponential backoff.
        """
        channel = pubsub.trades_channel(symbol)

        while self._running:
            pubsub_obj = None
            try:
                pubsub_obj = await pubsub.subscribe(channel)
                logger.info("Stop monitor subscribed to %s", channel)

                async for msg in pubsub.iter_messages(pubsub_obj):
                    if not self._running:
                        break
                    if msg.get("event") != "trade":
                        continue
                    price = Decimal(str(msg["price"]))
                    await self._process_trade_print(symbol, price)

            except asyncio.CancelledError:
                logger.info("Stop monitor for %s cancelled", symbol)
                break
            except Exception as e:
                logger.warning("Stop monitor for %s error: %s — retrying in 2s", symbol, e)
                await asyncio.sleep(2.0)
            finally:
                if pubsub_obj is not None:
                    try:
                        await pubsub.unsubscribe(pubsub_obj)
                    except Exception:
                        pass

    async def _process_trade_print(self, symbol: str, price: Decimal) -> None:
        """Process a single trade print: evaluate stops + update trailing."""
        redis = get_redis()

        # Update local extremes
        prev_high = self._local_high.get(symbol)
        prev_low = self._local_low.get(symbol)
        new_high = price if prev_high is None or price > prev_high else None
        new_low = price if prev_low is None or price < prev_low else None
        if new_high is not None:
            self._local_high[symbol] = price
        if new_low is not None:
            self._local_low[symbol] = price

        # Update trailing stops (recompute triggers)
        if new_high is not None or new_low is not None:
            await stops.update_trailing_extremes(
                redis, symbol,
                new_high=float(new_high) if new_high else None,
                new_low=float(new_low) if new_low else None,
            )

        # Check for triggered plain stops
        triggered = await stops.get_all_triggered_stops(redis, symbol, price)
        for order_id in triggered:
            try:
                await self._trigger_stop_order(order_id, price)
            except Exception as e:
                logger.exception("Failed to trigger stop order %d: %s", order_id, e)

        # Check for triggered trailing stops (after extreme update)
        triggered_trailing = await stops.get_triggered_trailing_stops(redis, symbol, price)
        for order_id in triggered_trailing:
            try:
                await self._trigger_stop_order(order_id, price, is_trailing=True)
            except Exception as e:
                logger.exception("Failed to trigger trailing stop %d: %s", order_id, e)

    async def _trigger_stop_order(
        self,
        order_id: int,
        trigger_price: Decimal,
        is_trailing: bool = False,
    ) -> None:
        """Trigger a stop order: convert to market/limit, lock balance, enqueue."""
        redis = get_redis()
        async with async_session_factory() as session:
            async with session.begin():
                repo = OrderRepository(session)
                order = await repo.get_for_update(order_id)
                if order is None:
                    logger.warning("Stop order %d not found", order_id)
                    return
                if order.status != OrderStatus.PENDING:
                    # Already triggered or canceled
                    return

                # Verify the trigger direction matches the order's side
                # Sell-stop: trigger when price <= stop_price (already verified by range query)
                # Buy-stop: trigger when price >= stop_price (already verified)
                # The range query handles this, so we just proceed.

                # Determine the child order type
                if order.type in (OrderType.STOP_MARKET, OrderType.TRAILING_STOP):
                    child_type = OrderType.MARKET
                    child_price = None
                else:  # STOP_LIMIT
                    child_type = OrderType.LIMIT
                    child_price = order.price  # the limit price set at placement

                # Lock balance for the child order
                pair_repo = TradingPairRepository(session)
                pair = await pair_repo.get_by_symbol(order.symbol)
                if pair is None:
                    logger.error("Trading pair %s not found for stop order %d", order.symbol, order_id)
                    return

                balance_svc = BalanceService(session)
                if order.side == OrderSide.SELL:
                    lock_asset = pair.base_asset
                    lock_amount = order.quantity
                else:
                    lock_asset = pair.quote_asset
                    if child_type == OrderType.LIMIT and child_price is not None:
                        lock_amount = child_price * order.quantity
                    else:
                        # Market buy: walk the ask side to compute actual cost
                        # (same logic as OrderService._compute_market_buy_lock)
                        ask_orders = await orderbook.get_opposite_orders_for_match(
                            redis, order.symbol, "buy", limit=1000,
                        )
                        if not ask_orders:
                            logger.warning("Cannot trigger market buy stop %d: no asks", order_id)
                            return

                        remaining_qty = order.quantity
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
                            logger.warning("Cannot trigger market buy stop %d: insufficient ask volume", order_id)
                            return

                        lock_amount = total_cost * Decimal("1.001")  # 0.1% buffer

                try:
                    await balance_svc.lock_optimistic(
                        order.user_id, lock_asset, lock_amount,
                        reason="stop_triggered", order_id=order.id,
                    )
                except Exception as e:
                    logger.error("Failed to lock for stop %d: %s", order_id, e)
                    return

                # Mark the stop order as triggered (FILLED, since it "completed" its job)
                order.status = OrderStatus.FILLED
                order.version += 1

                # Create the child market/limit order
                from app.models.order import Order as OrderModel
                child = OrderModel(
                    user_id=order.user_id,
                    symbol=order.symbol,
                    side=order.side,
                    type=child_type,
                    status=OrderStatus.NEW,
                    price=child_price,
                    quantity=order.quantity,
                    parent_order_id=order.id,
                )
                session.add(child)
                await session.flush()

                # Enqueue child order to matching worker
                payload = queues.build_order_action_payload(
                    action="place",
                    order_id=child.id,
                    symbol=child.symbol,
                    user_id=child.user_id,
                    side=child.side.value,
                    type=child.type.value,
                    price=float(child.price) if child.price else None,
                    quantity=float(child.quantity),
                    parent_order_id=order.id,
                )
                await queues.enqueue_order_action(redis, payload)

                # Add to open-orders index + HASH
                from app.redis_client import orders_index
                await orders_index.add_open_order(redis, child.user_id, child.symbol, child.id)
                metadata = {
                    "order_id": str(child.id),
                    "user_id": str(child.user_id),
                    "symbol": child.symbol,
                    "side": child.side.value,
                    "type": child.type.value,
                    "price": str(child.price) if child.price else "",
                    "quantity": str(child.quantity),
                    "is_iceberg": "0",
                }
                await orderbook.store_order_hash(redis, child.id, metadata)

                # Remove from stop queue
                if is_trailing:
                    await stops.remove_trailing_stop(redis, order_id, order.symbol)
                else:
                    await stops.remove_stop_order(redis, order_id, order.symbol)

                # Publish order triggered event
                await pubsub.publish_order_update(
                    redis, user_id=order.user_id, event="triggered",
                    order_id=order.id, symbol=order.symbol,
                    side=order.side.value, type=order.type.value,
                    status=order.status.value,
                )

    async def _rescan_pending_stops(self, symbols: list[str]) -> None:
        """On startup, re-evaluate all pending stops against last trade price.

        This catches any stops that should have triggered while the monitor
        was offline.
        """
        async with async_session_factory() as session:
            repo = OrderRepository(session)
            trade_repo = OrderRepository(session)  # reuse for trade queries
            from app.repositories.trade_repo import TradeRepository
            trade_repo_proper = TradeRepository(session)

            for symbol in symbols:
                # Get last trade price
                last_price = await trade_repo_proper.get_last_price(symbol)
                if last_price is None:
                    continue

                # Load pending stops from PG
                pending = await repo.list_pending_stops_by_symbol(symbol)
                for order in pending:
                    should_trigger = False
                    if order.side == OrderSide.SELL:
                        # Sell-stop: trigger if last_price <= stop_price
                        if order.stop_price and Decimal(str(last_price)) <= order.stop_price:
                            should_trigger = True
                    else:
                        # Buy-stop: trigger if last_price >= stop_price
                        if order.stop_price and Decimal(str(last_price)) >= order.stop_price:
                            should_trigger = True

                    if should_trigger:
                        try:
                            await self._trigger_stop_order(order.id, Decimal(str(last_price)))
                        except Exception as e:
                            logger.exception("Failed to trigger stop %d on rescan: %s", order.id, e)


__all__ = ["StopMonitorService"]
