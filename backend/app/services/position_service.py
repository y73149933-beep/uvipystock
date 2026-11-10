"""Position / PnL aggregation over the user's trade history.

Pure computation (no I/O) so it is unit-testable: a moving weighted-average
cost basis per symbol. Buys add cost; sells realize PnL against the average
and shrink the position. Fees are currently always 0 in this system and are
deliberately ignored here; when fee settlement lands, subtract them from
realized PnL in one place (this module).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

ZERO = Decimal("0")


@dataclass
class PositionAgg:
    symbol: str
    quantity: Decimal = ZERO
    cost: Decimal = ZERO          # total cost basis of the open position
    realized_pnl: Decimal = ZERO
    last_trade_price: Decimal = ZERO

    @property
    def avg_cost(self) -> Decimal:
        if self.quantity > 0:
            return self.cost / self.quantity
        return ZERO


@dataclass
class TradeLike:
    """Minimal structural view of a Trade row for the fold."""

    symbol: str
    side: str                 # "buy" | "sell" — taker side
    price: Decimal
    quantity: Decimal
    taker_user_id: int
    maker_user_id: int


def compute_positions(
    trades: Sequence[TradeLike], user_id: int
) -> dict[str, PositionAgg]:
    """Fold chronological trade history (MUST be ascending by executed_at)
    into per-symbol moving-average position state for ``user_id``.

    The caller's side is the trade side when they were the taker and the
    opposite when they were the maker. STP guarantees a user never trades
    against themselves, so each row touches exactly one leg of the user.
    """
    out: dict[str, PositionAgg] = {}
    for t in trades:
        if t.taker_user_id == user_id:
            buy = t.side == "buy"
        elif t.maker_user_id == user_id:
            buy = t.side != "buy"
        else:
            continue
        agg = out.setdefault(t.symbol, PositionAgg(symbol=t.symbol))
        qty = Decimal(t.quantity)
        px = Decimal(t.price)
        agg.last_trade_price = px
        if buy:
            agg.cost += qty * px
            agg.quantity += qty
        else:
            prev_avg = agg.avg_cost  # BEFORE mutating qty/cost
            sell_qty = min(qty, agg.quantity) if agg.quantity > 0 else ZERO
            agg.realized_pnl += sell_qty * (px - prev_avg)
            agg.quantity -= sell_qty
            agg.cost -= sell_qty * prev_avg
            if agg.quantity <= 0:
                # fully closed: cost basis resets so a fresh round-trip starts clean
                agg.quantity = ZERO
                agg.cost = ZERO
    return out
