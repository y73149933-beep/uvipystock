"""Pure grid-strategy logic: level calculation + order plan.

This module is intentionally I/O-free so the math can be unit-tested
in isolation. The actual order placement / monitoring lives in `bot.py`.

Grid layout
-----------
Given a center price ``C``, ``N`` levels per side, and a spacing of
``s`` percent, the grid is::

    sell_N  C * (1 + s*N/100)        ← highest ask
    ...
    sell_2  C * (1 + s*2/100)
    sell_1  C * (1 + s*1/100)        ← lowest ask, just above center
    ─────── center C ───────
    buy_1   C * (1 - s*1/100)        ← highest bid, just below center
    buy_2   C * (1 - s*2/100)
    ...
    buy_N   C * (1 - s*N/100)        ← lowest bid

Each level is a fixed-size LIMIT order for ``quantity`` of the base
asset. The total quote-capital required to place the buy side is::

    sum(buy_qty_i * buy_price_i for i in 1..N)

and the total base-capital required for the sell side is::

    sum(sell_qty_i for i in 1..N) = N * quantity

We expose both as helper functions so the bot can pre-flight check
against the user's balances before placing anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Literal


Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class GridLevel:
    """One row of the grid — a planned limit order."""
    side: Side
    level: int          # 1-based index from the center (1 = closest)
    price: Decimal
    quantity: Decimal

    @property
    def quote_value(self) -> Decimal:
        """Price × quantity — how much quote currency this order is worth."""
        return self.price * self.quantity


@dataclass(frozen=True)
class GridPlan:
    """A complete grid plan: center + all levels + capital requirements."""
    center: Decimal
    levels_per_side: int
    spacing_pct: Decimal
    quantity: Decimal
    buy_levels: list[GridLevel]
    sell_levels: list[GridLevel]

    @property
    def all_levels(self) -> list[GridLevel]:
        """All levels, sell side first (highest price → lowest), then buy side."""
        # Sell levels: highest price first (level N → 1)
        return list(reversed(self.sell_levels)) + self.buy_levels

    @property
    def buy_quote_capital(self) -> Decimal:
        """Total quote currency locked if all buy orders are placed."""
        return sum((lvl.quote_value for lvl in self.buy_levels), Decimal("0"))

    @property
    def sell_base_capital(self) -> Decimal:
        """Total base currency locked if all sell orders are placed."""
        return sum((lvl.quantity for lvl in self.sell_levels), Decimal("0"))

    @property
    def total_orders(self) -> int:
        return len(self.buy_levels) + len(self.sell_levels)


def calculate_grid(
    *,
    center: Decimal | float | str,
    levels: int,
    spacing_pct: Decimal | float | str,
    quantity: Decimal | float | str,
    price_precision: int = 2,
    qty_precision: int = 6,
) -> GridPlan:
    """Compute the grid levels around `center`.

    :param center: Center price. Use the current mid-price for a symmetric
                   grid, or a fixed value if you want to anchor the grid.
    :param levels: Number of levels per side (1..50).
    :param spacing_pct: Distance between adjacent levels as a percentage
                        of the center price. 0.5 means 0.5%.
    :param quantity: Base-asset quantity per level (same for all levels).
    :param price_precision: Decimal places to round prices to. Should match
                            the trading pair's ``price_precision``.
    :param qty_precision: Decimal places to round quantities to.
    """
    if not 1 <= levels <= 50:
        raise ValueError(f"levels must be in [1, 50], got {levels}")
    s = Decimal(str(spacing_pct))
    if s <= 0:
        raise ValueError(f"spacing_pct must be > 0, got {s}")
    q = Decimal(str(quantity))
    if q <= 0:
        raise ValueError(f"quantity must be > 0, got {q}")
    c = Decimal(str(center))
    if c <= 0:
        raise ValueError(f"center must be > 0, got {c}")

    # Build a quantizer string like "0.01" for precision=2
    price_q = Decimal(10) ** -price_precision
    qty_q = Decimal(10) ** -qty_precision

    buy_levels: list[GridLevel] = []
    sell_levels: list[GridLevel] = []

    for i in range(1, levels + 1):
        offset_pct = s * i                       # e.g. 0.5% * 1, 0.5% * 2, ...
        # buy:  C * (1 - offset_pct/100)
        buy_price = (c * (Decimal("1") - offset_pct / Decimal("100"))).quantize(
            price_q, rounding=ROUND_DOWN,
        )
        # sell: C * (1 + offset_pct/100)
        sell_price = (c * (Decimal("1") + offset_pct / Decimal("100"))).quantize(
            price_q, rounding=ROUND_DOWN,
        )
        q_quantized = q.quantize(qty_q, rounding=ROUND_DOWN)

        buy_levels.append(GridLevel(
            side="buy", level=i, price=buy_price, quantity=q_quantized,
        ))
        sell_levels.append(GridLevel(
            side="sell", level=i, price=sell_price, quantity=q_quantized,
        ))

    return GridPlan(
        center=c,
        levels_per_side=levels,
        spacing_pct=s,
        quantity=q,
        buy_levels=buy_levels,
        sell_levels=sell_levels,
    )


def level_to_order_request(
    level: GridLevel,
    *,
    symbol: str,
) -> dict:
    """Convert a `GridLevel` to an ``OrderCreateRequest`` payload for the API.

    The grid bot uses LIMIT orders with GTC time-in-force so they rest in
    the book until filled or canceled.
    """
    return {
        "symbol": symbol,
        "side": level.side,
        "type": "limit",
        "price": str(level.price),
        "quantity": str(level.quantity),
        "time_in_force": "gtc",
    }


def plan_to_order_requests(
    plan: GridPlan,
    *,
    symbol: str,
) -> list[dict]:
    """Convert an entire plan to a list of order payloads (for bulk placement)."""
    return [level_to_order_request(lvl, symbol=symbol) for lvl in plan.all_levels]


__all__ = [
    "Side",
    "GridLevel",
    "GridPlan",
    "calculate_grid",
    "level_to_order_request",
    "plan_to_order_requests",
]
