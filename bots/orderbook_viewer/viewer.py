"""Concrete viewer: wires `ExchangeWSClient` callbacks to `OrderbookDisplay`."""
from __future__ import annotations

import asyncio
import logging

from .client import ExchangeWSClient
from .display import OrderbookDisplay

logger = logging.getLogger(__name__)


class OrderbookViewer(ExchangeWSClient):
    """WS client that feeds inbound events into a live Rich display.

    Inheritance rather than composition because the market-maker bot will
    later subclass `ExchangeWSClient` the same way — keeping the pattern
    consistent makes the codebase easy to scan.
    """

    def __init__(
        self,
        *,
        url: str,
        symbol: str,
        depth: int = 10,
        price_precision: int = 2,
        qty_precision: int = 4,
    ) -> None:
        super().__init__(url=url, symbol=symbol)
        self.display = OrderbookDisplay(
            symbol=symbol,
            depth=depth,
            price_precision=price_precision,
            qty_precision=qty_precision,
        )

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the display, then enter the WS reconnect loop."""
        self.display.start()
        render_task: asyncio.Task | None = None
        try:
            # Render loop runs concurrently with the WS reconnect loop
            render_task = asyncio.create_task(self.display.run_render_loop())
            await super().run()
        finally:
            if render_task is not None and not render_task.done():
                render_task.cancel()
                try:
                    await render_task
                except asyncio.CancelledError:
                    pass
            self.display.stop()

    # ─── Callbacks ───────────────────────────────────────────────────────

    async def on_connect(self, *, connect_count: int) -> None:
        self.display.connect_count = connect_count
        self.display.status = "connected"
        self.display.last_disconnect_reason = None
        logger.info("WS connected (count=%d)", connect_count)

    async def on_disconnect(self, *, reason: str) -> None:
        self.display.status = "disconnected"
        self.display.last_disconnect_reason = reason
        logger.warning("WS disconnected: %s", reason)

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


__all__ = ["OrderbookViewer"]
