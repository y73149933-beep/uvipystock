"""Rich-based terminal renderer for the live orderbook viewer.

Renders a two-column layout:
  ┌─────────────── Asks (red, descending so best ask is at the bottom) ───┐
  │  price    qty        depth-bar                                         │
  ├─────────────── Spread / mid / last trade ──────────────────────────────┤
  │  price    qty        depth-bar                                         │
  └─────────────── Bids (green, best bid on top) ──────────────────────────┘

Plus a small footer with connection stats and a rolling trade tape.

Why Rich
--------
`rich.live.Live` does the heavy lifting of differential terminal updates
so we don't have to manage cursor positioning ourselves. The dependency
is small (pure-Python, no native code) and worth it for a viewer.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from decimal import Decimal
from typing import Sequence

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .book import Level, LocalOrderBook
from .balances import BalanceTracker
from .orders import OrderTracker

logger = logging.getLogger(__name__)


# ─── Formatting helpers ─────────────────────────────────────────────────────

def _fmt_price(p: Decimal | float | None, precision: int = 2) -> str:
    if p is None:
        return "—"
    return f"{Decimal(str(p)):.{precision}f}"


def _fmt_qty(q: Decimal | float | None, precision: int = 4) -> str:
    if q is None:
        return "—"
    return f"{Decimal(str(q)):.{precision}f}"


def _depth_bar(qty: Decimal, max_qty: Decimal, width: int = 20) -> str:
    """Return a textual depth bar like '█████████░░░░░░░░░░░' scaled to max_qty."""
    if max_qty <= 0:
        return " " * width
    ratio = min(1.0, float(qty) / float(max_qty))
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _balance_bar(total: Decimal, locked: Decimal, max_total: Decimal, width: int = 12) -> str:
    """A two-tone bar: green = available, yellow = locked.

    Visualizes how much of the asset is locked vs free. Width is split
    proportionally: filled portion = total/max_total (overall scale),
    of which the locked part is yellow and the available part is green.
    """
    if max_total <= 0 or total <= 0:
        return "░" * width
    total_ratio = min(1.0, float(total) / float(max_total))
    total_filled = int(total_ratio * width)
    if total > 0:
        locked_ratio = float(locked) / float(total)
    else:
        locked_ratio = 0.0
    locked_filled = int(locked_ratio * total_filled)
    available_filled = total_filled - locked_filled
    return "█" * available_filled + "▒" * locked_filled + "░" * (width - total_filled)


# ─── Trade tape ─────────────────────────────────────────────────────────────

class TradeTape:
    """Rolling window of recent trade prints for the footer."""

    def __init__(self, max_size: int = 12) -> None:
        self._items: deque[dict] = deque(maxlen=max_size)

    def push(self, *, price: float, quantity: float, side: str, ts: int | None) -> None:
        self._items.appendleft({
            "price": price, "qty": quantity, "side": side, "ts": ts,
        })

    def rows(self) -> list[dict]:
        return list(self._items)


# ─── Renderer ───────────────────────────────────────────────────────────────

class OrderbookDisplay:
    """Renders the local book + trade tape to a Rich Live panel.

    Usage::

        display = OrderbookDisplay(symbol="BTC/USDT", depth=10)
        display.start()
        # ... feed updates via display.book.apply_snapshot(...) etc.
        display.stop()
    """

    def __init__(
        self,
        *,
        symbol: str,
        depth: int = 10,
        price_precision: int = 2,
        qty_precision: int = 4,
        refresh_hz: float = 10.0,
    ) -> None:
        self.symbol = symbol
        self.depth = depth
        self.price_precision = price_precision
        self.qty_precision = qty_precision
        self.refresh_interval = 1.0 / refresh_hz

        self.book = LocalOrderBook(symbol=symbol)
        self.tape = TradeTape()
        self.balances = BalanceTracker()
        self.orders = OrderTracker()

        # Optional: highlight base/quote of the symbol in the balance panel.
        # Parsed from "BTC/USDT" → base="BTC", quote="USDT".
        self._base_asset: str | None = None
        self._quote_asset: str | None = None
        if "/" in symbol:
            base, _, quote = symbol.partition("/")
            self._base_asset = base.strip().upper()
            self._quote_asset = quote.strip().upper()

        self._console = Console()
        self._live: Live | None = None
        self._stop = asyncio.Event()
        self._render_task: asyncio.Task | None = None

        # Status line info (set by the WS client callbacks)
        self.status: str = "connecting…"
        self.connect_count: int = 0
        self.last_disconnect_reason: str | None = None

        # Auth + private-channel status
        self.login: str | None = None        # set after successful login
        self.user_id: int | None = None
        self.private_status: str = "off"     # off | connecting | auth_ok | failed
        self.private_connect_count: int = 0

    # ─── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin rendering. Safe to call once; idempotent."""
        if self._live is not None:
            return
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=int(1.0 / self.refresh_interval * 2),
            screen=True,  # full-screen alternate buffer — clears the terminal
        )
        self._live.start()

    async def run_render_loop(self) -> None:
        """Async render loop — call from the main task.

        We use an explicit loop instead of relying solely on Rich's
        internal refresh so we can also tick status text and avoid
        blocking forever when there are no updates.
        """
        while not self._stop.is_set():
            if self._live is not None:
                self._live.update(self._render())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ─── Rendering ───────────────────────────────────────────────────────

    def _render_balances_panel(self) -> Table:
        """Build the balances sub-table.

        Shows one row per asset with non-zero total OR the symbol's
        base/quote (so the user always sees their BTC/USDT balances
        even if currently zero). Locked amount is shown only when > 0.
        """
        table = Table.grid(expand=True)
        table.add_column("asset", width=10, style="bold")
        table.add_column("total", justify="right", width=14)
        table.add_column("available", justify="right", width=14)
        table.add_column("locked", justify="right", width=12)
        table.add_column("bar", justify="left", width=14)

        # Choose which assets to show: non-zero ones + base/quote of the symbol
        all_bal = self.balances.all_balances()
        shown: list = []
        seen: set[str] = set()
        # Always include base/quote first (in that order)
        for asset in (self._base_asset, self._quote_asset):
            if asset and asset not in seen:
                b = self.balances.get(asset)
                if b is not None:
                    shown.append(b)
                else:
                    # Asset not in balances yet — show zero placeholder
                    from .balances import Balance
                    shown.append(Balance(
                        asset=asset,
                        total=Decimal("0"), locked=Decimal("0"), available=Decimal("0"),
                    ))
                seen.add(asset)
        # Then all non-zero balances
        for b in all_bal:
            if b.asset in seen:
                continue
            if not b.is_zero:
                shown.append(b)
                seen.add(b.asset)

        if not shown:
            table.add_row(
                Text("no balances", style="dim italic"),
                "", "", "", "",
            )
            return table

        # Find max total for the depth-bar scaling
        max_total = max((b.total for b in shown), default=Decimal("0"))

        for b in shown:
            # Highlight the symbol's base/quote
            is_pair_asset = (
                b.asset == self._base_asset or b.asset == self._quote_asset
            )
            asset_style = "bold cyan" if is_pair_asset else "bold"
            asset_text = Text(b.asset, style=asset_style)

            total_text = Text(_fmt_qty(b.total, self.qty_precision),
                              style="white" if b.total > 0 else "dim")
            avail_text = Text(_fmt_qty(b.available, self.qty_precision),
                              style="green" if b.available > 0 else "dim")
            if b.locked > 0:
                locked_text = Text(_fmt_qty(b.locked, self.qty_precision),
                                   style="yellow")
            else:
                locked_text = Text("—", style="dim")

            # Bar: locked fraction of total (yellow on green)
            bar = _balance_bar(b.total, b.locked, max_total)

            table.add_row(asset_text, total_text, avail_text, locked_text,
                          Text(bar))

        return table

    def _render_orders_panel(self, max_rows: int = 8) -> Table:
        """Build the open-orders sub-table.

        Shows up to ``max_rows`` most recent open orders. Columns:
        id | side | type | price | qty | filled | status

        Buy side is green, sell side is red — matches the book coloring.
        """
        table = Table.grid(expand=True)
        table.add_column("id", width=8, style="dim")
        table.add_column("side", width=5)
        table.add_column("type", width=10, style="dim")
        table.add_column("price", justify="right", width=12)
        table.add_column("qty", justify="right", width=10)
        table.add_column("filled", justify="right", width=10, style="dim")
        table.add_column("status", width=10)

        orders = self.orders.all_orders()[:max_rows]

        if not orders:
            table.add_row(
                Text("no open orders", style="dim italic"),
                "", "", "", "", "", "",
            )
            return table

        for o in orders:
            # Side with color
            if o.is_buy:
                side_text = Text("BUY", style="bright_green")
            else:
                side_text = Text("SELL", style="bright_red")

            # Price (some types like market have no price)
            if o.price is not None:
                price_text = Text(_fmt_price(o.price, self.price_precision))
            else:
                price_text = Text("mkt", style="dim italic")

            qty_text = Text(_fmt_qty(o.quantity, self.qty_precision))
            filled_text = Text(_fmt_qty(o.filled_quantity, self.qty_precision))

            # Status with color
            if o.status == "new":
                status_text = Text(o.status, style="cyan")
            elif o.status == "partially_filled":
                status_text = Text("partial", style="yellow")
            elif o.status == "pending":
                status_text = Text(o.status, style="magenta")
            else:
                status_text = Text(o.status, style="dim")

            # Type abbreviation to save space
            type_short = {
                "limit": "LMT",
                "market": "MKT",
                "post_only": "POST",
                "ioc": "IOC",
                "fok": "FOK",
                "iceberg": "ICE",
                "stop_market": "STOP",
                "stop_limit": "STPL",
                "trailing_stop": "TRAIL",
            }.get(o.type, o.type[:4].upper())

            table.add_row(
                Text(str(o.id), style="dim"),
                side_text,
                Text(type_short, style="dim"),
                price_text,
                qty_text,
                filled_text,
                status_text,
            )

        # If we truncated, show a count indicator
        total = self.orders.open_count()
        if total > max_rows:
            table.add_row(
                Text(f"+{total - max_rows} more…", style="dim italic"),
                "", "", "", "", "", "",
            )

        return table

    def _render(self) -> Panel:
        bids, asks = self.book.top(self.depth)
        spread = self.book.spread()
        mid = self.book.mid_price()

        # Determine the max qty on the visible levels for the depth-bar scale
        max_qty = Decimal("0")
        for lvl in bids + asks:
            if lvl.quantity > max_qty:
                max_qty = lvl.quantity

        # ── Asks table (reversed so best ask sits right above the spread) ──
        asks_table = Table.grid(expand=True)
        asks_table.add_column("price", justify="right", width=14, style="bright_red")
        asks_table.add_column("qty", justify="right", width=14, style="red")
        asks_table.add_column("depth", justify="left", width=22)

        for lvl in reversed(asks):  # worst ask at top, best at bottom
            asks_table.add_row(
                _fmt_price(lvl.price, self.price_precision),
                _fmt_qty(lvl.quantity, self.qty_precision),
                Text(_depth_bar(lvl.quantity, max_qty), style="red"),
            )
        # Pad empty rows so the layout doesn't jump when book is shallow
        for _ in range(self.depth - len(asks)):
            asks_table.add_row("", "", "")

        # ── Spread / mid row ─────────────────────────────────────────────
        if spread is not None:
            abs_spread, pct_spread = spread
            spread_text = (
                f"  spread: {_fmt_price(abs_spread, self.price_precision)} "
                f"({pct_spread:.3f}%)   "
                f"mid: {_fmt_price(mid, self.price_precision)}   "
                f"last: {_fmt_price(self.book.last_trade_price, self.price_precision)}"
            )
        else:
            spread_text = (
                f"  spread: —   mid: {_fmt_price(mid, self.price_precision)}   "
                f"last: {_fmt_price(self.book.last_trade_price, self.price_precision)}"
            )
        spread_row = Text(spread_text, style="bold yellow", justify="center")

        # ── Bids table ───────────────────────────────────────────────────
        bids_table = Table.grid(expand=True)
        bids_table.add_column("price", justify="right", width=14, style="bright_green")
        bids_table.add_column("qty", justify="right", width=14, style="green")
        bids_table.add_column("depth", justify="left", width=22)

        for lvl in bids:  # best bid at top
            bids_table.add_row(
                _fmt_price(lvl.price, self.price_precision),
                _fmt_qty(lvl.quantity, self.qty_precision),
                Text(_depth_bar(lvl.quantity, max_qty), style="green"),
            )
        for _ in range(self.depth - len(bids)):
            bids_table.add_row("", "", "")

        # ── Footer: status + trade tape ──────────────────────────────────
        tape_table = Table.grid(expand=True)
        tape_table.add_column("side", width=6)
        tape_table.add_column("price", justify="right", width=14)
        tape_table.add_column("qty", justify="right", width=14)
        tape_table.add_column("ts", justify="right", width=14)
        for t in self.tape.rows()[:6]:
            color = "bright_red" if t["side"] == "sell" else "bright_green"
            tape_table.add_row(
                Text(t["side"].upper(), style=color),
                _fmt_price(t["price"], self.price_precision),
                _fmt_qty(t["qty"], self.qty_precision),
                str(t["ts"] or ""),
            )

        status_line = (
            f"{self.symbol}  |  public: {self.status}  |  "
            f"connects: {self.connect_count}  |  "
            f"snapshots: {self.book.snapshot_count}  updates: {self.book.update_count}  |  "
            f"bid_levels: {self.book.bid_levels_count()}  ask_levels: {self.book.ask_levels_count()}"
        )
        if self.last_disconnect_reason:
            status_line += f"  |  last disconnect: {self.last_disconnect_reason}"

        # Private-channel status line (only shown if login was attempted)
        auth_line = ""
        if self.login is not None:
            auth_line = (
                f"user: {self.login} (id={self.user_id})  |  "
                f"private: {self.private_status}  |  "
                f"bal_updates: {self.balances.update_count}  "
                f"bal_snapshots: {self.balances.snapshot_count}  |  "
                f"open_orders: {self.orders.open_count()}  "
                f"order_updates: {self.orders.update_count}  "
                f"filled: {self.orders.filled_count}  canceled: {self.orders.canceled_count}"
            )

        footer_parts: list = [
            Text("─ balances ─", style="dim"),
            self._render_balances_panel(),
            Text("─ open orders ─", style="dim"),
            self._render_orders_panel(),
            Text("─ recent trades ─", style="dim"),
            tape_table,
            Text(status_line, style="dim cyan"),
        ]
        if auth_line:
            footer_parts.append(Text(auth_line, style="dim magenta"))

        footer = Group(*footer_parts)

        body = Group(
            asks_table,
            spread_row,
            bids_table,
            footer,
        )

        title_text = f"  {self.symbol}  orderbook"
        if self.login:
            title_text += f"  ·  {self.login}"
        title = Text(title_text + "  ", style="bold white on blue")
        return Panel(
            body,
            title=title,
            border_style="blue",
            subtitle="press Ctrl+C to exit",
            subtitle_align="right",
        )


__all__ = ["OrderbookDisplay", "TradeTape"]
