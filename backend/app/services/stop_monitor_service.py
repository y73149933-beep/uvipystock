"""Stop monitor service — evaluate stop triggers and recompute trailing stops.

Trigger-source policy (IMPORTANT)
---------------------------------
A pending conditional order is armed by EXACTLY ONE input: the market's
**last trade price** (a print on `pub:trades:{symbol}`):

  * SELL-side stop fires when ``last_trade_price <= stop_price``;
  * BUY-side  stop fires when ``last_trade_price >= stop_price``.

The live order book NEVER arms a stop. Book depth decides only how the
resulting child market/limit order executes AFTER activation. Arming
against best bid/ask ("touch" semantics) degenerates any stop whose level
lies beyond the market into an executable-now taker: a BUY stop @100 while
the market trades at 50 would fill instantly at ~50 the moment ANY ask
rests at/below 100 — destroying breakout/protection semantics entirely.
That was a reported production regression; it is deliberately NOT
reimplemented (a user who wants immediate execution should place a plain
limit/market order instead of a conditional one).

The monitor still subscribes to `pub:orderbook:{symbol}`, but non-trade
events serve ONLY two auxiliary purposes:

  * snapshot payloads carrying ``last_trade_price`` keep the local price
    cache fresh between prints;
  * ``stops_changed`` (published when a NEW stop registers) runs an
    immediate evaluation pass so a stop whose condition is ALREADY true
    under the last-price rule activates at placement instead of waiting
    for the next print. Book-only changes cannot arm anything.

On each evaluation tick:

  1. Query `stops:{symbol}` for orders whose trigger crossed the cached
     last trade price (directional scan — see redis_client/stops.py).
  2. For each triggered stop:
     a. Load the order from PG (FOR UPDATE)
     b. Verify side matches the Redis-side query (defense-in-depth)
     c. Re-verify the numeric condition against the reference price
     d. Convert to a market or limit order (per OrderType), lock balance,
        enqueue to `queue:orders`, remove from the stop queue
  3. Update trailing stops: recompute trigger based on new high/low
     (trailing stops keep the trade-print rule for firing).

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
from app.models.order import Order as OrderModel
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
        # Last traded price per symbol (seeded from PG on start(), kept fresh
        # by trade prints and snapshot payloads carrying last_trade_price).
        self._last_price: dict[str, Decimal] = {}
        # Track local extremes per symbol for trailing stops
        self._local_high: dict[str, Decimal] = {}
        # Throttle for public stop-ladder invalidation on trailing drift:
        # symbol → monotonic timestamp of the last published event.
        self._last_ladder_publish: dict[str, float] = {}
        self._local_low: dict[str, Decimal] = {}
        self._refresh_task: asyncio.Task | None = None

    async def _publish_stops_changed_throttled(
        self,
        redis,
        symbol: str,
        min_interval_s: float = 0.5,
    ) -> None:
        """Publish the public stop-ladder invalidation at most once per
        `min_interval_s` per symbol.

        Used for high-frequency drift updates (trailing trigger moves on
        every new extreme); placement/cancel/trigger fire events publish
        unconditionally instead.
        """
        now = asyncio.get_event_loop().time()
        last = self._last_ladder_publish.get(symbol)
        if last is not None and (now - last) < min_interval_s:
            return
        self._last_ladder_publish[symbol] = now
        await pubsub.publish_stops_changed(redis, symbol)

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
        trades_ch = pubsub.trades_channel(symbol)
        book_ch = pubsub.orderbook_channel(symbol)

        while self._running:
            pubsub_obj = None
            try:
                pubsub_obj = await pubsub.subscribe(trades_ch, book_ch)
                logger.info(
                    "Stop monitor subscribed to %s + %s", trades_ch, book_ch,
                )

                async for msg in pubsub.iter_messages(pubsub_obj):
                    if not self._running:
                        break
                    event = msg.get("event")
                    if event == "trade":
                        price = Decimal(str(msg["price"]))
                        await self._process_trade_print(symbol, price)
                    elif event in (
                        "orderbook_snapshot", "orderbook_update", "stops_changed",
                    ):
                        # Auxiliary passes over non-trade events only:
                        #   * snapshot payloads refresh the last-price cache;
                        #   * stops_changed evaluates freshly placed stops at
                        #     registration (fires ONLY if the last-price rule
                        #     already holds — never from book depth).
                        ltp = msg.get("last_trade_price")
                        if ltp:
                            try:
                                p = Decimal(str(ltp))
                                if p > 0:
                                    prev = self._last_price.get(symbol)
                                    if prev is None or abs(p - prev) >= Decimal("1e-12"):
                                        self._last_price[symbol] = p
                            except Exception:
                                pass
                        await self._evaluate_symbol_stops(symbol)

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
        self._last_price[symbol] = price

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
            moved = await stops.update_trailing_extremes(
                redis, symbol,
                new_high=float(new_high) if new_high else None,
                new_low=float(new_low) if new_low else None,
            )
            if moved:
                await self._publish_stops_changed_throttled(redis, symbol)

        # Plain stops: pure last-trade rule (evaluation is idempotent —
        # the status guard inside _trigger_stop_order blocks double fires)
        await self._evaluate_symbol_stops(symbol)

        # Check for triggered trailing stops (after extreme update)
        triggered_trailing = await stops.get_triggered_trailing_stops(redis, symbol, price)
        for order_id, side in triggered_trailing:
            try:
                expected_side = OrderSide(side)
                await self._trigger_stop_order(
                    order_id, price, is_trailing=True, expected_side=expected_side,
                )
            except Exception as e:
                logger.exception("Failed to trigger trailing stop %d: %s", order_id, e)

    async def _evaluate_symbol_stops(self, symbol: str) -> None:
        """Evaluate ALL plain stops for `symbol` against the last trade price.

        Single firing rule, re-verified inside the trigger transaction
        (defense-in-depth): the classic last-trade rule. The live order
        book plays NO part in arming stops — see the module docstring for
        why touch/book-driven triggering was removed.
        """
        redis = get_redis()

        last = self._last_price.get(symbol)
        triggered: list[tuple[int, str]] = []
        if last is not None and last > 0:
            try:
                triggered = await stops.get_all_triggered_stops(redis, symbol, last)
            except Exception as e:
                logger.exception("Stop monitor %s: last-rule scan failed: %s", symbol, e)

        for order_id, side in triggered:
            try:
                expected_side = OrderSide(side)
                await self._trigger_stop_order(order_id, last, expected_side=expected_side)
            except Exception as e:
                logger.exception("Failed to trigger stop order %d: %s", order_id, e)

    async def _trigger_stop_order(
        self,
        order_id: int,
        trigger_price: Decimal | None,
        is_trailing: bool = False,
        expected_side: OrderSide | None = None,
    ) -> None:
        """Trigger a stop order: convert to market/limit, lock balance, enqueue.

        ``expected_side`` comes from the Redis-side directional query and is
        re-verified here against the PG row (defense-in-depth). For plain
        stops the numeric condition is re-checked against the reference
        price that armed this evaluation: sell-stop needs ``price <= stop``,
        buy-stop needs ``price >= stop``. Trailing stops are exempt because
        their live trigger drifts away from the stored ``stop_price`` (the
        trailing ZSET score IS the drifted trigger).
        """
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

                # Verify the side reported by the Redis queue matches PG
                if expected_side is not None and order.side != expected_side:
                    logger.warning(
                        "Stop %d skipped: Redis side %s != PG side %s",
                        order_id, expected_side.value, order.side.value,
                    )
                    return

                # Defensive directional re-check against the reference
                # price that armed this evaluation (Redis/PG drift guard):
                #   sell fires when price <= stop; buy when price >= stop.
                if not is_trailing and order.stop_price is not None:
                    stop_dec = Decimal(str(order.stop_price))
                    ref = Decimal(str(trigger_price)) if trigger_price is not None else None
                    if ref is None:
                        return
                    if order.side == OrderSide.SELL and ref > stop_dec:
                        logger.info(
                            "Sell stop %d skipped: price %s above stop %s",
                            order_id, ref, order.stop_price,
                        )
                        return
                    if order.side == OrderSide.BUY and ref < stop_dec:
                        logger.info(
                            "Buy stop %d skipped: price %s below stop %s",
                            order_id, ref, order.stop_price,
                        )
                        return

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

                # Create the child market/limit order.
                # The child owns its own reservation (locked_amount) so a later
                # cancel / worker refund releases exactly what it locked, not
                # the whole user balance (that was the stuck-funds bug).
                child = OrderModel(
                    user_id=order.user_id,
                    symbol=order.symbol,
                    side=order.side,
                    type=child_type,
                    status=OrderStatus.NEW,
                    price=child_price,
                    quantity=order.quantity,
                    parent_order_id=order.id,
                    locked_amount=lock_amount,
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
                # Public stop-ladder visibility: the fired order left the queue
                await pubsub.publish_stops_changed(redis, order.symbol)

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

                # Seed the in-memory last price so the last-trade rule works
                # before the first pub/sub print arrives.
                self._last_price[symbol] = Decimal(str(last_price))

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
