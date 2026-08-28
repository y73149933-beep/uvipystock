"""Concrete viewer: wires public + private WS + REST auth into a live display.

Lifecycle::

    viewer = AuthenticatedOrderbookViewer(
        url="ws://localhost:8000",
        http_url="http://localhost:8000",
        symbol="BTC/USDT",
        login="trader@exchange.local",
        password="admin123",
    )
    await viewer.run()   # blocks until Ctrl+C / cancel

Concurrency
-----------
Three long-running asyncio tasks run in parallel:

1. **REST login + balance snapshot** — one-shot at startup, then
   periodic re-fetch (every 30s) as a safety net in case a WS update
   is missed (Redis Pub/Sub is fire-and-forget).
2. **Public WS** — orderbook + trade prints (via parent class
   `ExchangeWSClient.run()`).
3. **Private WS** — balance + order updates (via `PrivateChannelClient`).

The display is shared; all three tasks write to it through callbacks.
"""
from __future__ import annotations

import asyncio
import logging

from .client import ExchangeWSClient
from .display import OrderbookDisplay
from .hmac_rest_client import ExchangeAuth, ExchangeRESTClient
from .private_client import PrivateChannelClient

logger = logging.getLogger(__name__)


class _BalanceListener(PrivateChannelClient):
    """PrivateChannelClient that feeds balance/order events into the display.

    We use composition (hold a reference to the display) rather than
    inheritance-from-display because the display is a Rich rendering
    object, not a WS client — mixing the two would be a mess.
    """

    def __init__(self, *, url: str, auth: ExchangeAuth, display: OrderbookDisplay) -> None:
        super().__init__(url=url, auth=auth)
        self._display = display

    async def on_connect(self, *, connect_count: int) -> None:
        self._display.private_connect_count = connect_count
        self._display.private_status = "connecting"

    async def on_auth_ok(self, *, user_id: int | None) -> None:
        self._display.private_status = "auth_ok"
        logger.info("Private WS auth_ok (user_id=%s)", user_id)

    async def on_auth_failed(self, *, message: str) -> None:
        self._display.private_status = f"failed: {message}"
        logger.error("Private WS auth failed: %s", message)

    async def on_disconnect(self, *, reason: str) -> None:
        self._display.private_status = "disconnected"
        logger.warning("Private WS disconnected: %s", reason)

    async def on_balance_update(
        self, *, asset: str, total: float, locked: float, available: float,
        change: float | None = None, reason: str | None = None,
        order_id: int | None = None,
    ) -> None:
        self._display.balances.apply_ws_update(
            asset=asset, total=total, locked=locked, available=available,
        )

    async def on_order_update(self, **kwargs) -> None:
        """Forward order lifecycle events to the OrderTracker.

        The market maker will override this method to additionally react
        to its own fills (e.g. re-quote when a maker order is filled).
        """
        action = self._display.orders.apply_ws_update(kwargs)
        if action == "placed":
            logger.info("Order placed: id=%s %s %s %s @ %s",
                        kwargs.get("order_id"), kwargs.get("side"),
                        kwargs.get("symbol"), kwargs.get("quantity"),
                        kwargs.get("price"))
        elif action == "filled":
            logger.info("Order filled: id=%s %s %s @ %s (avg=%s)",
                        kwargs.get("order_id"), kwargs.get("side"),
                        kwargs.get("symbol"),
                        kwargs.get("last_trade_price"),
                        kwargs.get("avg_fill_price"))
        elif action == "canceled":
            logger.info("Order canceled: id=%s", kwargs.get("order_id"))
        elif action == "modified":
            logger.info("Order modified: id=%s price=%s qty=%s",
                        kwargs.get("order_id"), kwargs.get("price"),
                        kwargs.get("quantity"))
        # 'updated' (partial fills) and 'removed' are too chatty — debug only
        elif logger.isEnabledFor(10):  # DEBUG
            logger.debug("Order %s: %s", action, kwargs)


class AuthenticatedOrderbookViewer(ExchangeWSClient):
    """Public WS + REST login + private WS, all feeding one display."""

    def __init__(
        self,
        *,
        url: str,
        http_url: str,
        symbol: str,
        login: str,
        password: str,
        depth: int = 10,
        price_precision: int = 2,
        qty_precision: int = 4,
        balance_refresh_interval: float = 30.0,
    ) -> None:
        super().__init__(url=url, symbol=symbol)
        self.http_url = http_url.rstrip("/")
        self.login_name = login
        self.password = password
        self.balance_refresh_interval = balance_refresh_interval

        self.display = OrderbookDisplay(
            symbol=symbol,
            depth=depth,
            price_precision=price_precision,
            qty_precision=qty_precision,
        )
        self._rest: ExchangeRESTClient | None = None
        self._auth: ExchangeAuth | None = None
        self._private: _BalanceListener | None = None
        self._tasks: list[asyncio.Task] = []

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Login, fetch initial balances, then run public + private WS in parallel."""
        self.display.start()
        try:
            # 1. Login + initial balance snapshot
            try:
                await self._login_and_fetch_balances()
            except Exception as e:
                logger.error("Login or initial balance fetch failed: %s", e)
                self.display.private_status = f"login failed: {e}"
                # Continue running public-only — the user still sees the book.

            # 2. Launch parallel tasks
            self._tasks = [
                asyncio.create_task(self._run_public_ws(), name="public_ws"),
                asyncio.create_task(self._run_private_ws(), name="private_ws"),
            ]
            if self._auth is not None:
                self._tasks.append(
                    asyncio.create_task(
                        self._refresh_loop(),
                        name="rest_refresh",
                    )
                )
            # Render loop
            render_task = asyncio.create_task(
                self.display.run_render_loop(), name="render",
            )
            self._tasks.append(render_task)

            # Wait for any task to exit (or for cancellation)
            done, pending = await asyncio.wait(
                self._tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel the rest
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    logger.error("Task %s crashed: %s", t.get_name(), exc)
        finally:
            await self._cleanup()
            self.display.stop()

    async def _cleanup(self) -> None:
        """Close REST client + cancel any running tasks."""
        for t in self._tasks:
            if not t.done():
                t.cancel()
        # Wait for cancellations to settle
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._rest is not None:
            try:
                await self._rest.aclose()
            except Exception:
                pass
            self._rest = None

    # ─── Auth + initial state ────────────────────────────────────────────

    async def _login_and_fetch_balances(self) -> None:
        """Login via REST and fetch the initial state (balances + orders)."""
        self._rest = ExchangeRESTClient(base_url=self.http_url)
        self._auth = await self._rest.login(self.login_name, self.password)

        # Update display with auth info
        self.display.login = self._auth.login
        self.display.user_id = self._auth.user_id

        # Fetch initial balances + open orders in parallel
        await asyncio.gather(
            self._refresh_balances(),
            self._refresh_orders(),
            return_exceptions=True,
        )

    async def _refresh_balances(self) -> None:
        """Re-fetch balances from REST. Safe to call repeatedly."""
        if self._rest is None or self._auth is None:
            return
        try:
            balances = await self._rest.get_balances()
            self.display.balances.apply_rest_snapshot(balances)
            logger.debug("Refreshed balances: %d assets", len(balances))
        except Exception as e:
            logger.warning("Balance refresh failed: %s", e)

    async def _refresh_orders(self) -> None:
        """Re-fetch open orders from REST. Safe to call repeatedly.

        Asks the API for ``status=new,partially_filled,pending`` (the
        three "open" states from `app/models/enums.py`). The
        ``OrderTracker.apply_rest_snapshot`` will additionally filter
        defensively in case the API ever returns a terminal-status row.
        """
        if self._rest is None or self._auth is None:
            return
        try:
            orders = await self._rest.get_orders(
                status="new,partially_filled,pending",
                limit=500,
            )
            self.display.orders.apply_rest_snapshot(orders)
            logger.debug("Refreshed orders: %d open", len(orders))
        except Exception as e:
            logger.warning("Order refresh failed: %s", e)

    async def _refresh_loop(self) -> None:
        """Periodically re-fetch balances AND orders as a safety net.

        Redis Pub/Sub is fire-and-forget; if a WS frame is dropped, the
        display would otherwise drift from reality. A periodic REST
        re-fetch keeps everything in sync. We fetch both balances and
        orders in parallel to keep latency low.
        """
        while True:
            await asyncio.sleep(self.balance_refresh_interval)
            await asyncio.gather(
                self._refresh_balances(),
                self._refresh_orders(),
                return_exceptions=True,
            )

    # ─── WS runners ──────────────────────────────────────────────────────

    async def _run_public_ws(self) -> None:
        """Run the public orderbook WS (parent class)."""
        await super().run()

    async def _run_private_ws(self) -> None:
        """Run the private WS (only if login succeeded)."""
        if self._auth is None:
            return
        self._private = _BalanceListener(
            url=self.base_url, auth=self._auth, display=self.display,
        )
        await self._private.run()

    # ─── Public WS callbacks (override ExchangeWSClient) ─────────────────

    async def on_connect(self, *, connect_count: int) -> None:
        self.display.connect_count = connect_count
        self.display.status = "connected"
        self.display.last_disconnect_reason = None
        logger.info("Public WS connected (count=%d)", connect_count)

    async def on_disconnect(self, *, reason: str) -> None:
        self.display.status = "disconnected"
        self.display.last_disconnect_reason = reason
        logger.warning("Public WS disconnected: %s", reason)

    async def on_snapshot(
        self, *, symbol: str, bids: list, asks: list,
        last_trade_price: float | None, ts: int | None,
    ) -> None:
        self.display.book.apply_snapshot(bids, asks, last_trade_price, ts)

    async def on_update(
        self, *, symbol: str, changes: list[dict], ts: int | None,
    ) -> None:
        self.display.book.apply_update(changes, ts)

    async def on_trade(
        self, *, symbol: str, trade_id: int | None, price: float,
        quantity: float, side: str, ts: int | None,
    ) -> None:
        self.display.book.apply_trade(price, ts)
        self.display.tape.push(price=price, quantity=quantity, side=side, ts=ts)


__all__ = ["AuthenticatedOrderbookViewer"]
