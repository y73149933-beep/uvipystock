"""SpreadFillerBot: continuous spread-filling market maker.

Lifecycle (loop)
----------------
1. Login via REST → obtain HMAC credentials.
2. Subscribe to public WS for the symbol → receive orderbook snapshots +
   deltas + trade prints.
3. Track: best_bid, best_ask, last_trade_side (updated on every WS event).
4. Rebalance loop (every `throttle_seconds` OR when market moves significantly):
   a. Read current best_bid / best_ask from the local book (EXCLUDING
      our own orders, to avoid feedback loops).
   b. Compute a new plan via `calculate_spread_grid(...)`.
   c. If the new plan is identical to the current one → skip.
   d. Cancel all our open orders on the symbol.
   e. Bulk-place the new plan.
5. Listen on private WS for fill events → log them (and optionally
   track PnL).
6. On Ctrl+C: cancel all orders, print summary, exit.

Rebalance triggers
------------------
* **Price move**: mid-price moved by more than `rebalance_threshold_pct`
  since the last placement.
* **Trade direction flip**: last trade side changed (buy→sell or vice
  versa) since the last placement.
* **Throttle**: at most one rebalance per `throttle_seconds` (default 5s)
  to avoid spamming the API.
* **Book becomes empty / one-sided**: re-evaluate immediately.

Feedback loop avoidance
-----------------------
The public WS shows the aggregated book — including our own orders. If
we naively read best_bid/ask from it, we'd see our own orders as the
"market" and never rebalance. Solution: when reading the local book,
skip price levels that match our own open orders.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Any

from bots.orderbook_viewer.hmac_rest_client import (
    ApiError,
    ExchangeAuth,
    ExchangeRESTClient,
)
from bots.orderbook_viewer.private_client import PrivateChannelClient
from bots.orderbook_viewer.book import LocalOrderBook
from bots.orderbook_viewer.client import ExchangeWSClient

from .strategy import (
    SpreadGridPlan,
    SpreadLevel,
    calculate_spread_grid,
    plan_to_order_requests,
)

logger = logging.getLogger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _parse_asset(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    return base.strip().upper(), quote.strip().upper()


# ─── Public WS listener (feeds book + trades into the bot) ──────────────────

class _MarketListener(ExchangeWSClient):
    """Public WS client that feeds orderbook + trade events into the bot.

    Subclass of `ExchangeWSClient` — overrides the typed callbacks to
    update the bot's local state.
    """

    def __init__(self, *, url: str, symbol: str, bot: "SpreadFillerBot") -> None:
        super().__init__(url=url, symbol=symbol)
        self._bot = bot

    async def on_snapshot(self, *, symbol, bids, asks, last_trade_price, ts):
        self._bot._book.apply_snapshot(bids, asks, last_trade_price, ts)
        self._bot._market_dirty = True

    async def on_update(self, *, symbol, changes, ts):
        self._bot._book.apply_update(changes, ts)
        self._bot._market_dirty = True

    async def on_trade(self, *, symbol, trade_id, price, quantity, side, ts):
        self._bot._book.apply_trade(price, ts)
        # Track last trade side — this drives the bias
        if side in ("buy", "sell"):
            self._bot._last_trade_side = side
            self._bot._trade_side_dirty = True


# ─── Private WS listener (feeds order events into the bot) ──────────────────

class _OrderListener(PrivateChannelClient):
    """Private WS client that feeds order lifecycle events into the bot."""

    def __init__(self, *, url: str, auth: ExchangeAuth, bot: "SpreadFillerBot") -> None:
        super().__init__(url=url, auth=auth)
        self._bot = bot

    async def on_auth_ok(self, *, user_id: int | None) -> None:
        logger.info("Private WS authenticated (user_id=%s)", user_id)

    async def on_order_update(self, **kwargs) -> None:
        await self._bot._on_order_event(kwargs)


# ─── Bot ────────────────────────────────────────────────────────────────────

class SpreadFillerBot:
    """Continuous spread-filling market maker."""

    def __init__(
        self,
        *,
        symbol: str,
        login: str,
        password: str,
        http_url: str = "http://localhost:8000",
        ws_url: str = "ws://localhost:8000",
        levels: int = 10,
        quantity: Decimal | float | str = Decimal("0.01"),
        bias: Decimal | float | str = Decimal("0.3"),
        tick_size: Decimal | float | str = Decimal("0.01"),
        price_precision: int = 2,
        qty_precision: int = 6,
        rebalance_threshold_pct: Decimal | float | str = Decimal("0.3"),
        throttle_seconds: float = 5.0,
        single_side_buffer_pct: Decimal | float | str = Decimal("1.0"),
        dry_run: bool = False,
        cancel_on_exit: bool = True,
    ) -> None:
        self.symbol = symbol
        self.login_name = login
        self.password = password
        self.http_url = http_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.levels = levels
        self.quantity = Decimal(str(quantity))
        self.bias = Decimal(str(bias))
        self.tick_size = Decimal(str(tick_size))
        self.price_precision = price_precision
        self.qty_precision = qty_precision
        self.rebalance_threshold_pct = Decimal(str(rebalance_threshold_pct))
        self.throttle_seconds = throttle_seconds
        self.single_side_buffer_pct = Decimal(str(single_side_buffer_pct))
        self.dry_run = dry_run
        self.cancel_on_exit = cancel_on_exit

        self.base_asset, self.quote_asset = _parse_asset(symbol)

        # State
        self._rest: ExchangeRESTClient | None = None
        self._auth: ExchangeAuth | None = None
        self._book = LocalOrderBook(symbol=symbol)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        # Market tracking
        self._last_trade_side: str | None = None
        self._market_dirty = False       # book changed since last rebalance
        self._trade_side_dirty = False   # trade side changed since last rebalance

        # Our own orders: order_id → price (so we can exclude them from
        # best_bid/ask reads)
        self._my_order_prices: dict[int, Decimal] = {}
        self._my_buy_prices: set[Decimal] = set()
        self._my_sell_prices: set[Decimal] = set()

        # Last placement state (to detect "plan unchanged" → skip)
        self._last_plan: SpreadGridPlan | None = None
        self._last_mid: Decimal | None = None

        # Counters
        self.rebalance_count = 0
        self.placed_count = 0
        self.canceled_count = 0
        self.filled_count = 0
        self.skipped_count = 0

    # ─── Public API ──────────────────────────────────────────────────────

    async def run(self) -> int:
        try:
            return await self._run_inner()
        except asyncio.CancelledError:
            logger.info("Bot cancelled")
            return 130
        except Exception as e:
            logger.exception("Bot crashed: %s", e)
            return 1
        finally:
            await self._cleanup()

    def stop(self) -> None:
        self._stop.set()

    # ─── Internal: main flow ─────────────────────────────────────────────

    async def _run_inner(self) -> int:
        # 1. Login
        await self._login()
        if self._stop.is_set() or self._auth is None:
            return 0

        # 2. Start public WS (orderbook + trades)
        market_listener = _MarketListener(
            url=self.ws_url, symbol=self.symbol, bot=self,
        )
        self._tasks.append(asyncio.create_task(market_listener.run(), name="public_ws"))

        # 3. Start private WS (order events) — non-blocking
        order_listener = _OrderListener(
            url=self.ws_url, auth=self._auth, bot=self,
        )
        self._tasks.append(asyncio.create_task(order_listener.run(), name="private_ws"))

        # 4. Wait for first orderbook snapshot before rebalancing
        logger.info("Waiting for orderbook snapshot ...")
        while not self._book.snapshot_count and not self._stop.is_set():
            await asyncio.sleep(0.2)
        if self._stop.is_set():
            return 0

        # 5. Rebalance loop
        await self._rebalance_loop()

        return 0

    # ─── Login ───────────────────────────────────────────────────────────

    async def _login(self) -> None:
        logger.info("Logging in as %s ...", self.login_name)
        self._rest = ExchangeRESTClient(base_url=self.http_url)
        self._auth = await self._rest.login(self.login_name, self.password)
        logger.info("Logged in (user_id=%d)", self._auth.user_id)

    # ─── Rebalance loop ──────────────────────────────────────────────────

    async def _rebalance_loop(self) -> None:
        """Main loop: rebalance when market moves or trade direction flips."""
        last_rebalance_time = 0.0

        while not self._stop.is_set():
            # Wait for the next market event or throttle interval
            await asyncio.sleep(0.5)

            now = asyncio.get_event_loop().time()
            elapsed = now - last_rebalance_time

            # Decide whether to rebalance
            should_rebalance = False
            reason = ""

            if self._market_dirty or self._trade_side_dirty:
                # Market or trade direction changed
                if elapsed >= self.throttle_seconds:
                    should_rebalance = True
                    reason = "market/trade change (throttled)"
                # else: wait for throttle

            if not should_rebalance:
                continue

            # Read the market, excluding our own orders
            best_bid, best_ask = self._read_market_excluding_own_orders()
            mid = self._book.mid_price()

            # Check if mid moved enough to warrant rebalance
            if self._last_mid is not None and mid is not None:
                move_pct = abs(mid - self._last_mid) / self._last_mid * 100
                if move_pct < self.rebalance_threshold_pct and not self._trade_side_dirty:
                    # Mid didn't move enough and trade side didn't flip — skip
                    self._market_dirty = False
                    self.skipped_count += 1
                    continue

            try:
                plan = calculate_spread_grid(
                    best_bid=best_bid,
                    best_ask=best_ask,
                    last_trade_side=self._last_trade_side,
                    levels=self.levels,
                    quantity=self.quantity,
                    bias=self.bias,
                    tick_size=self.tick_size,
                    price_precision=self.price_precision,
                    qty_precision=self.qty_precision,
                    single_side_buffer_pct=self.single_side_buffer_pct,
                )
            except ValueError as e:
                logger.warning("Cannot compute plan: %s — waiting ...", e)
                self._market_dirty = False
                self._trade_side_dirty = False
                continue

            # If plan is empty (spread too tight), skip
            if plan.total_orders == 0:
                logger.info("Spread too tight — skipping rebalance (waiting for widening)")
                self._market_dirty = False
                self._trade_side_dirty = False
                self.skipped_count += 1
                continue

            # If plan is identical to last, skip
            if self._plans_equal(plan, self._last_plan):
                self._market_dirty = False
                self._trade_side_dirty = False
                self.skipped_count += 1
                continue

            # Rebalance!
            logger.info("Rebalancing (%s): bid=%s ask=%s last=%s → %d buys, %d sells",
                        reason, best_bid, best_ask, self._last_trade_side,
                        len(plan.buy_levels), len(plan.sell_levels))

            if not self.dry_run:
                ok = await self._cancel_and_replace(plan)
                if not ok:
                    # Failed — wait longer before retrying
                    last_rebalance_time = now + self.throttle_seconds
                    continue
            else:
                self._log_plan(plan)

            self._last_plan = plan
            self._last_mid = mid
            self._market_dirty = False
            self._trade_side_dirty = False
            last_rebalance_time = now
            self.rebalance_count += 1

    # ─── Read market excluding own orders ────────────────────────────────

    def _read_market_excluding_own_orders(self) -> tuple[Decimal | None, Decimal | None]:
        """Read best_bid/ask from the local book, skipping our own prices.

        This prevents feedback loops: without this, our own orders would
        appear as the "market" and we'd never detect that the real market
        moved.
        """
        bids = self._book.top_bids(depth=50)
        asks = self._book.top_asks(depth=50)

        best_bid: Decimal | None = None
        for lvl in bids:
            if lvl.price not in self._my_buy_prices:
                best_bid = lvl.price
                break

        best_ask: Decimal | None = None
        for lvl in asks:
            if lvl.price not in self._my_sell_prices:
                best_ask = lvl.price
                break

        return best_bid, best_ask

    # ─── Cancel + replace ────────────────────────────────────────────────

    async def _cancel_and_replace(self, plan: SpreadGridPlan) -> bool:
        """Cancel all our orders on the symbol, then place the new plan."""
        # 1. Cancel all
        if self._my_order_prices:
            try:
                result = await self._rest.cancel_all_on_symbol(self.symbol)  # type: ignore[union-attr]
                canceled = result.get("canceled_count", 0)
                self.canceled_count += canceled
                logger.info("✓ Canceled %d previous orders", canceled)
            except ApiError as e:
                logger.error("Cancel-all failed: [%s] %s", e.status_code, e.message)
                return False
            # Clear our tracking (the private WS will confirm cancels,
            # but we clear optimistically to avoid stale state)
            self._my_order_prices.clear()
            self._my_buy_prices.clear()
            self._my_sell_prices.clear()

        # 2. Place new plan
        orders = plan_to_order_requests(plan, symbol=self.symbol)
        if not orders:
            return True

        bulk_id = f"spread_{uuid.uuid4().hex[:8]}"
        try:
            result = await self._rest.place_bulk_orders(orders, bulk_id=bulk_id)  # type: ignore[union-attr]
        except ApiError as e:
            logger.error("Bulk place failed: [%s] %s", e.status_code, e.message)
            if e.body:
                logger.error("  Body: %s", e.body)
            return False

        if result.get("result") != "success":
            logger.error("Bulk place rejected: %s", result)
            return False

        placed = result.get("orders", [])
        self.placed_count += len(placed)

        # Track our order prices
        for order_row, level in zip(placed, plan.all_levels):
            oid = order_row["id"]
            self._my_order_prices[oid] = level.price
            if level.side == "buy":
                self._my_buy_prices.add(level.price)
            else:
                self._my_sell_prices.add(level.price)

        logger.info("✓ Placed %d orders (bulk_id=%s)", len(placed), bulk_id)
        self._log_plan(plan)
        return True

    # ─── Order event handler (from private WS) ───────────────────────────

    async def _on_order_event(self, payload: dict) -> None:
        """Handle a private-WS order event."""
        order_id = payload.get("order_id")
        status = str(payload.get("status", ""))
        status_event = str(payload.get("status_event", ""))

        if order_id not in self._my_order_prices:
            return

        price = self._my_order_prices.get(order_id)

        if status == "filled" or status_event == "filled":
            self.filled_count += 1
            avg = payload.get("avg_fill_price") or payload.get("last_trade_price")
            logger.info("✓ FILLED  order #%d  @ %s  (price %s)", order_id, avg, price)
            self._untrack_order(order_id)
            # A fill means the market moved through our price — force rebalance
            self._market_dirty = True

        elif status == "canceled" or status_event == "canceled":
            self.canceled_count += 1
            self._untrack_order(order_id)

        elif status_event == "partially_filled":
            filled = payload.get("filled_quantity")
            remaining = payload.get("remaining_quantity")
            logger.info("• PARTIAL order #%d: filled=%s remaining=%s",
                        order_id, filled, remaining)

    def _untrack_order(self, order_id: int) -> None:
        """Remove an order from our tracking sets."""
        price = self._my_order_prices.pop(order_id, None)
        if price is not None:
            self._my_buy_prices.discard(price)
            self._my_sell_prices.discard(price)

    # ─── Plan comparison ─────────────────────────────────────────────────

    def _plans_equal(self, a: SpreadGridPlan, b: SpreadGridPlan | None) -> bool:
        """Check if two plans are functionally identical (same orders)."""
        if b is None:
            return False
        a_prices = {(lvl.side, lvl.price) for lvl in a.all_levels}
        b_prices = {(lvl.side, lvl.price) for lvl in b.all_levels}
        return a_prices == b_prices

    # ─── Logging ─────────────────────────────────────────────────────────

    def _log_plan(self, plan: SpreadGridPlan) -> None:
        logger.info("-" * 70)
        logger.info("  bid=%s  ask=%s  mid=%s  boundary=%s  last_trade=%s  bias=%s",
                    plan.best_bid, plan.best_ask, plan.mid,
                    plan.boundary, plan.last_trade_side, plan.bias)
        if plan.synthetic_side:
            logger.info("  ⚠ %s (estimated the missing side)", plan.synthetic_side)
        logger.info("  %-5s %-5s %12s %12s", "side", "lvl", "price", "qty")
        for lvl in plan.all_levels:
            logger.info("  %-5s %-5d %12s %12s",
                        lvl.side.upper(), lvl.level, lvl.price, lvl.quantity)
        logger.info("-" * 70)

    # ─── Cleanup ─────────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        logger.info("=" * 70)
        logger.info("SPREAD FILLER BOT SUMMARY")
        logger.info("  rebalances:   %d", self.rebalance_count)
        logger.info("  placed:       %d", self.placed_count)
        logger.info("  filled:       %d", self.filled_count)
        logger.info("  canceled:     %d", self.canceled_count)
        logger.info("  skipped:      %d (no change)", self.skipped_count)
        remaining = len(self._my_order_prices)
        logger.info("  still open:   %d", remaining)
        logger.info("=" * 70)

        if self.cancel_on_exit and remaining > 0 and self._rest is not None:
            logger.info("Canceling all remaining orders on %s ...", self.symbol)
            try:
                result = await self._rest.cancel_all_on_symbol(self.symbol)
                canceled = result.get("canceled_count", 0)
                logger.info("✓ Canceled %d orders", canceled)
            except Exception as e:
                logger.error("Failed to cancel remaining orders: %s", e)

        if self._rest is not None:
            await self._rest.aclose()


__all__ = ["SpreadFillerBot"]
