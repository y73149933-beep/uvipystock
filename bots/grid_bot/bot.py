"""GridBot: orchestrates login → plan → bulk-place → monitor → maintain.

Lifecycle
---------
1. Login via REST → obtain HMAC credentials.
2. (Optional) Fetch current open orders on the symbol — if any exist,
   refuse to start (avoid stacking two grids on top of each other).
3. Determine the center price:
   - From `--center` CLI arg (explicit), OR
   - From the public WS snapshot's mid-price (auto).
4. Compute the grid plan via `calculate_grid(...)`.
5. Pre-flight: check balances can cover the buy-side quote capital +
   sell-side base capital. Refuse if not.
6. Place all orders in a single bulk request (atomic All-or-Nothing).
7. Open private WS to monitor fills.
8. For each fill:
   - If `--maintain` is set, place a new LIMIT order at the same price
     (so the grid always has the same shape).
   - Otherwise, just log it (the grid depletes over time).
9. On Ctrl+C: cancel all orders on the symbol (if `--cancel-on-exit`),
   close WS, exit.

The bot is **stateful** but **single-threaded** — everything runs in one
asyncio event loop. Concurrent operations (REST + WS) are coordinated
via tasks.
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
    GridLevel,
    GridPlan,
    calculate_grid,
    level_to_order_request,
    plan_to_order_requests,
)

logger = logging.getLogger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _parse_asset(symbol: str) -> tuple[str, str]:
    """Parse "BTC/USDT" → ("BTC", "USDT")."""
    base, _, quote = symbol.partition("/")
    return base.strip().upper(), quote.strip().upper()


# ─── Bot ────────────────────────────────────────────────────────────────────

class GridBot:
    """One-shot grid placement + live fill monitor.

    Construction is cheap; call `.run()` to start the bot. The bot runs
    until cancelled (Ctrl+C) or until a fatal error occurs.
    """

    def __init__(
        self,
        *,
        symbol: str,
        login: str,
        password: str,
        http_url: str = "http://localhost:8000",
        ws_url: str = "ws://localhost:8000",
        center: Decimal | None = None,
        levels: int = 5,
        spacing_pct: Decimal | float | str = Decimal("0.5"),
        quantity: Decimal | float | str = Decimal("0.01"),
        maintain: bool = False,
        cancel_on_exit: bool = True,
        dry_run: bool = False,
        price_precision: int = 2,
        qty_precision: int = 6,
    ) -> None:
        self.symbol = symbol
        self.login_name = login
        self.password = password
        self.http_url = http_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.center_override = center
        self.levels = levels
        self.spacing_pct = Decimal(str(spacing_pct))
        self.quantity = Decimal(str(quantity))
        self.maintain = maintain
        self.cancel_on_exit = cancel_on_exit
        self.dry_run = dry_run
        self.price_precision = price_precision
        self.qty_precision = qty_precision

        self.base_asset, self.quote_asset = _parse_asset(symbol)

        # State (filled in during .run())
        self._rest: ExchangeRESTClient | None = None
        self._auth: ExchangeAuth | None = None
        self._plan: GridPlan | None = None
        self._book = LocalOrderBook(symbol=symbol)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        # Track our own orders by order_id → original level info, so we can
        # re-place them when filled (if --maintain).
        self._my_orders: dict[int, GridLevel] = {}

        # Counters for the summary
        self.placed_count = 0
        self.filled_count = 0
        self.canceled_count = 0
        self.replenished_count = 0

    # ─── Public API ──────────────────────────────────────────────────────

    async def run(self) -> int:
        """Main entry point. Returns exit code (0 = ok)."""
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
        """Signal the bot to shut down (graceful)."""
        self._stop.set()

    # ─── Internal: main flow ─────────────────────────────────────────────

    async def _run_inner(self) -> int:
        # 1. Login
        await self._login()
        if self._stop.is_set():
            return 0

        # 2. Determine center price
        center = await self._resolve_center()
        if center is None:
            logger.error("Could not determine center price — aborting")
            return 1

        # 3. Compute the grid plan
        self._plan = calculate_grid(
            center=center,
            levels=self.levels,
            spacing_pct=self.spacing_pct,
            quantity=self.quantity,
            price_precision=self.price_precision,
            qty_precision=self.qty_precision,
        )
        self._log_plan(self._plan)

        if self.dry_run:
            logger.info("Dry-run mode — no orders will be placed. Exiting.")
            return 0

        # 4. Pre-flight balance check
        await self._preflight_balances(self._plan)

        # 5. Place the grid (atomic bulk)
        ok = await self._place_grid(self._plan)
        if not ok:
            return 1

        # 6. Monitor fills via private WS (until stop)
        await self._monitor_fills()

        return 0

    # ─── Step 1: Login ───────────────────────────────────────────────────

    async def _login(self) -> None:
        logger.info("Logging in as %s ...", self.login_name)
        self._rest = ExchangeRESTClient(base_url=self.http_url)
        self._auth = await self._rest.login(self.login_name, self.password)
        logger.info("Logged in (user_id=%d)", self._auth.user_id)

    # ─── Step 2: Center price ────────────────────────────────────────────

    async def _resolve_center(self) -> Decimal | None:
        """Determine the grid center: explicit override or live mid-price."""
        if self.center_override is not None:
            logger.info("Using explicit center price: %s", self.center_override)
            return Decimal(str(self.center_override))

        # Auto: subscribe to public WS, wait for the first snapshot, read mid
        logger.info("No explicit center — fetching mid-price from public WS ...")
        snapshot_received = asyncio.Event()

        class _SnapshotGrabber(ExchangeWSClient):
            def __init__(slf, *a, **kw) -> None:  # noqa: ANN001
                super().__init__(*a, **kw)
                slf.parent: GridBot = kw.pop("parent")  # type: ignore[arg-type]

            async def on_snapshot(slf, *, symbol, bids, asks, last_trade_price, ts):
                slf.parent._book.apply_snapshot(bids, asks, last_trade_price, ts)
                snapshot_received.set()

        grabber = _SnapshotGrabber(
            url=self.ws_url, symbol=self.symbol, parent=self,  # type: ignore[arg-type]
        )
        grabber_task = asyncio.create_task(grabber.run())

        try:
            # Wait up to 5s for the first snapshot
            await asyncio.wait_for(snapshot_received.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for orderbook snapshot")
            grabber.stop()
            grabber_task.cancel()
            return None

        grabber.stop()
        grabber_task.cancel()
        try:
            await grabber_task
        except (asyncio.CancelledError, Exception):
            pass

        mid = self._book.mid_price()
        if mid is None:
            logger.error("Orderbook is empty on both sides — cannot determine mid")
            return None
        logger.info("Mid-price resolved: %s", mid)
        return mid

    # ─── Step 3: Log the plan ────────────────────────────────────────────

    def _log_plan(self, plan: GridPlan) -> None:
        logger.info("=" * 70)
        logger.info("GRID PLAN for %s", self.symbol)
        logger.info("  center:        %s", plan.center)
        logger.info("  levels/side:   %d", plan.levels_per_side)
        logger.info("  spacing:       %s%%", plan.spacing_pct)
        logger.info("  quantity/level:%s %s", plan.quantity, self.base_asset)
        logger.info("  total orders:  %d", plan.total_orders)
        logger.info("  buy  capital:  %s %s (sum of price×qty)",
                    plan.buy_quote_capital, self.quote_asset)
        logger.info("  sell capital:  %s %s (sum of qty)",
                    plan.sell_base_capital, self.base_asset)
        logger.info("-" * 70)
        logger.info("  %-6s %-5s %-5s %12s %12s", "side", "lvl", "id#", "price", "qty")
        for i, lvl in enumerate(plan.all_levels, 1):
            logger.info("  %-6s %-5d %-5d %12s %12s",
                        lvl.side.upper(), lvl.level, i, lvl.price, lvl.quantity)
        logger.info("=" * 70)

    # ─── Step 4: Pre-flight balance check ────────────────────────────────

    async def _preflight_balances(self, plan: GridPlan) -> None:
        """Verify the user has enough base + quote to place the whole grid.

        Logs a warning but does NOT abort — the API will reject the bulk
        request atomically if there's not enough, which is the actual
        source of truth. This is just a friendly pre-check.
        """
        try:
            balances = await self._rest.get_balances()  # type: ignore[union-attr]
        except Exception as e:
            logger.warning("Could not fetch balances for pre-flight: %s", e)
            return

        by_asset = {b["asset"]: b for b in balances}
        needed_quote = plan.buy_quote_capital
        needed_base = plan.sell_base_capital

        quote_bal = by_asset.get(self.quote_asset, {})
        base_bal = by_asset.get(self.base_asset, {})
        avail_quote = Decimal(str(quote_bal.get("available", "0")))
        avail_base = Decimal(str(base_bal.get("available", "0")))

        logger.info("Pre-flight: have %s %s available, need %s for buy side",
                    avail_quote, self.quote_asset, needed_quote)
        logger.info("Pre-flight: have %s %s available, need %s for sell side",
                    avail_base, self.base_asset, needed_base)

        if avail_quote < needed_quote:
            logger.warning(
                "⚠ Insufficient %s: have %s, need %s. Bulk place will be rejected.",
                self.quote_asset, avail_quote, needed_quote,
            )
        if avail_base < needed_base:
            logger.warning(
                "⚠ Insufficient %s: have %s, need %s. Bulk place will be rejected.",
                self.base_asset, avail_base, needed_base,
            )

    # ─── Step 5: Place the grid ──────────────────────────────────────────

    async def _place_grid(self, plan: GridPlan) -> bool:
        """Bulk-place all orders atomically. Returns True on success."""
        orders = plan_to_order_requests(plan, symbol=self.symbol)
        bulk_id = f"grid_{uuid.uuid4().hex[:8]}"
        logger.info("Placing %d orders (bulk_id=%s) ...", len(orders), bulk_id)

        try:
            result = await self._rest.place_bulk_orders(orders, bulk_id=bulk_id)  # type: ignore[union-attr]
        except ApiError as e:
            logger.error("Bulk place failed: [%s] %s", e.status_code, e.message)
            if e.body:
                logger.error("  Response body: %s", e.body)
            return False

        if result.get("result") != "success":
            logger.error("Bulk place rejected: %s", result)
            return False

        placed = result.get("orders", [])
        self.placed_count = len(placed)

        # Map order_id → GridLevel so we can replenish on fill
        # The API returns orders in the same order we sent them
        for order_row, level in zip(placed, plan.all_levels):
            self._my_orders[order_row["id"]] = level

        logger.info("✓ Placed %d orders (bulk_id=%s)", self.placed_count, bulk_id)
        return True

    # ─── Step 6: Monitor fills ───────────────────────────────────────────

    async def _monitor_fills(self) -> None:
        """Open the private WS and react to order events until stopped."""
        if self._auth is None:
            return

        listener = _GridListener(
            url=self.ws_url, auth=self._auth, bot=self,
        )
        listener_task = asyncio.create_task(listener.run(), name="private_ws")

        # Wait for stop signal
        await self._stop.wait()

        # Cancel the listener
        listener.stop()
        listener_task.cancel()
        try:
            await listener_task
        except (asyncio.CancelledError, Exception):
            pass

    # ─── Internal: called by _GridListener on order events ───────────────

    async def on_order_event(self, payload: dict) -> None:
        """React to a private-WS order event."""
        order_id = payload.get("order_id")
        status = str(payload.get("status", ""))
        status_event = str(payload.get("status_event", ""))

        if order_id not in self._my_orders:
            # Not our order (or we already cleaned it up) — ignore
            return

        level = self._my_orders[order_id]

        if status == "filled" or status_event == "filled":
            self.filled_count += 1
            avg_price = payload.get("avg_fill_price") or payload.get("last_trade_price")
            logger.info(
                "✓ FILLED  order #%d  %s %s %s  @ avg %s  (level %d, side %s)",
                order_id, level.side.upper(), level.quantity, self.symbol,
                avg_price, level.level, level.side,
            )
            del self._my_orders[order_id]
            if self.maintain:
                await self._replenish_level(level)

        elif status == "canceled" or status_event == "canceled":
            self.canceled_count += 1
            logger.info("✗ CANCELED order #%d (level %d %s)",
                        order_id, level.level, level.side)
            self._my_orders.pop(order_id, None)

        elif status_event == "partially_filled":
            filled_qty = payload.get("filled_quantity")
            remaining = payload.get("remaining_quantity")
            logger.info("• PARTIAL order #%d: filled=%s remaining=%s",
                        order_id, filled_qty, remaining)

    async def _replenish_level(self, level: GridLevel) -> None:
        """Place a new LIMIT order at the same price/qty as `level`.

        Used when --maintain is set: the grid keeps its shape as orders
        fill. Without this, the grid depletes over time.
        """
        order_req = level_to_order_request(level, symbol=self.symbol)
        try:
            result = await self._rest.place_order(order_req)  # type: ignore[union-attr]
            new_id = result.get("id")
            if new_id is not None:
                self._my_orders[int(new_id)] = level
                self.replenished_count += 1
                logger.info("↻ Replenished level %d %s → new order #%d @ %s",
                            level.level, level.side, new_id, level.price)
        except ApiError as e:
            logger.warning("Replenish failed for level %d: [%s] %s",
                           level.level, e.status_code, e.message)

    # ─── Cleanup ─────────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        """Cancel any running tasks + optionally cancel remaining orders."""
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        # Print summary
        logger.info("=" * 70)
        logger.info("GRID BOT SUMMARY")
        logger.info("  placed:       %d", self.placed_count)
        logger.info("  filled:       %d", self.filled_count)
        logger.info("  canceled:     %d", self.canceled_count)
        if self.maintain:
            logger.info("  replenished:  %d", self.replenished_count)
        remaining = len(self._my_orders)
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
                logger.error("  ⚠ You may have lingering orders — check the admin panel.")

        if self._rest is not None:
            await self._rest.aclose()


# ─── Private WS listener (helper class) ─────────────────────────────────────

class _GridListener(PrivateChannelClient):
    """PrivateChannelClient that forwards order events to the GridBot."""

    def __init__(self, *, url: str, auth: ExchangeAuth, bot: GridBot) -> None:
        super().__init__(url=url, auth=auth)
        self._bot = bot

    async def on_auth_ok(self, *, user_id: int | None) -> None:
        logger.info("Private WS authenticated (user_id=%s) — monitoring fills", user_id)

    async def on_auth_failed(self, *, message: str) -> None:
        logger.error("Private WS auth failed: %s", message)

    async def on_disconnect(self, *, reason: str) -> None:
        logger.warning("Private WS disconnected: %s — will reconnect", reason)

    async def on_order_update(self, **kwargs) -> None:
        # Forward to the bot — it decides what to do
        try:
            await self._bot.on_order_event(kwargs)
        except Exception as e:
            logger.exception("Error handling order event: %s", e)


__all__ = ["GridBot"]
