"""Pure spread-filling strategy logic.

The core idea: given the current best bid / best ask / last trade
direction, compute a set of limit orders that fill the spread between
them, skewed toward the side of the last trade.

Three market states are handled:

1. **Both sides present** (best_bid and best_ask both exist):
   Fill the spread [best_bid, best_ask]. Buys go in the lower portion,
   sells in the upper portion. The boundary between them shifts up
   (more room for buys) when the last trade was a buy, and down when
   it was a sell.

2. **Only bid present** (someone is bidding, nobody is asking):
   Estimate a synthetic ask at ``best_bid * (1 + buffer_pct/100)`` and
   proceed as case 1. The bot becomes the first seller.

3. **Only ask present** (mirror of case 2):
   Estimate a synthetic bid at ``best_ask * (1 - buffer_pct/100)`` and
   proceed as case 1.

4. **Empty book**: raise ``ValueError`` — the bot can't price anything.

Direction bias
--------------
``last_trade_side`` is the taker side of the most recent public trade:

* ``"buy"``  → taker bought → price pressure upward → we **pull buys up**
  (more buy levels, boundary shifts toward the ask)
* ``"sell"`` → taker sold → price pressure downward → we push sells down
  (more sell levels, boundary shifts toward the bid)
* ``None``   → no trades yet → symmetric 50/50 split

The ``bias`` parameter (0.0–1.0) controls how strong the skew is.
At ``bias=0`` the bot is always symmetric; at ``bias=1`` it goes
fully one-sided (all levels on the bias side).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

Side = Literal["buy", "sell"]
TradeSide = Literal["buy", "sell"] | None


# ─── Data classes ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpreadLevel:
    """One planned limit order inside the spread."""
    side: Side
    price: Decimal
    quantity: Decimal
    level: int          # 1-based, 1 = closest to the boundary


@dataclass(frozen=True)
class SpreadGridPlan:
    """A complete spread-filling plan."""
    best_bid: Decimal | None
    best_ask: Decimal | None
    synthetic_side: str | None       # None | "ask_synthetic" | "bid_synthetic"
    mid: Decimal
    boundary: Decimal                # price separating buys (below) from sells (above)
    buy_levels: list[SpreadLevel]
    sell_levels: list[SpreadLevel]
    last_trade_side: TradeSide
    bias: Decimal

    @property
    def all_levels(self) -> list[SpreadLevel]:
        """All levels, sells first (highest price), then buys (lowest price)."""
        return list(reversed(self.sell_levels)) + self.buy_levels

    @property
    def total_orders(self) -> int:
        return len(self.buy_levels) + len(self.sell_levels)

    @property
    def buy_quote_capital(self) -> Decimal:
        return sum((lvl.price * lvl.quantity for lvl in self.buy_levels), Decimal("0"))

    @property
    def sell_base_capital(self) -> Decimal:
        return sum((lvl.quantity for lvl in self.sell_levels), Decimal("0"))


# ─── Main entry point ───────────────────────────────────────────────────────

def calculate_spread_grid(
    *,
    best_bid: Decimal | float | str | None,
    best_ask: Decimal | float | str | None,
    last_trade_side: TradeSide = None,
    levels: int = 10,
    quantity: Decimal | float | str = Decimal("0.01"),
    bias: Decimal | float | str = Decimal("0.3"),
    tick_size: Decimal | float | str = Decimal("0.01"),
    price_precision: int = 2,
    qty_precision: int = 6,
    single_side_buffer_pct: Decimal | float | str = Decimal("1.0"),
    min_spread_ticks: int = 2,
) -> SpreadGridPlan:
    """Compute a spread-filling order plan.

    :param best_bid: Current best bid price, or None if no bids.
    :param best_ask: Current best ask price, or None if no asks.
    :param last_trade_side: Side of the last public trade ("buy"/"sell"/None).
    :param levels: Total number of orders to place (split between sides).
    :param quantity: Base-asset quantity per order.
    :param bias: 0.0–1.0 — how strongly to skew toward the last-trade side.
    :param tick_size: Minimum price increment. Levels are snapped to ticks.
    :param price_precision: Decimal places for price rounding.
    :param qty_precision: Decimal places for quantity rounding.
    :param single_side_buffer_pct: When only one side exists, estimate the
           other side this far away (as % of the known price).
    :param min_spread_ticks: If the spread is narrower than this many ticks,
           return an empty plan (can't fit orders without crossing).
    """
    # ── Validate inputs ─────────────────────────────────────────────────
    if not 1 <= levels <= 100:
        raise ValueError(f"levels must be in [1, 100], got {levels}")
    b = Decimal(str(bias))
    if not (0 <= b <= 1):
        raise ValueError(f"bias must be in [0, 1], got {b}")
    q = Decimal(str(quantity))
    if q <= 0:
        raise ValueError(f"quantity must be > 0, got {q}")
    tick = Decimal(str(tick_size))
    if tick <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick}")
    buffer_pct = Decimal(str(single_side_buffer_pct))
    if buffer_pct <= 0:
        raise ValueError(f"single_side_buffer_pct must be > 0, got {buffer_pct}")

    # ── Resolve the price range [low, high] ─────────────────────────────
    bid_d: Decimal | None = (
        Decimal(str(best_bid)) if best_bid is not None else None
    )
    ask_d: Decimal | None = (
        Decimal(str(best_ask)) if best_ask is not None else None
    )
    synthetic_side: str | None = None

    if bid_d is not None and ask_d is not None:
        # Case 1: both sides present
        if ask_d <= bid_d:
            raise ValueError(
                f"Crossed/locked book: best_ask ({ask_d}) <= best_bid ({bid_d})"
            )
        low, high = bid_d, ask_d
    elif bid_d is not None and ask_d is None:
        # Case 2: only bid — synthesize an ask
        high = bid_d * (Decimal("1") + buffer_pct / Decimal("100"))
        low = bid_d
        synthetic_side = "ask_synthetic"
    elif bid_d is None and ask_d is not None:
        # Case 3: only ask — synthesize a bid
        low = ask_d * (Decimal("1") - buffer_pct / Decimal("100"))
        high = ask_d
        synthetic_side = "bid_synthetic"
    else:
        # Case 4: empty book
        raise ValueError("Both best_bid and best_ask are None — empty book")

    mid = (low + high) / 2
    spread = high - low

    # ── Check minimum spread ────────────────────────────────────────────
    if spread < tick * min_spread_ticks:
        # Spread too tight — return an empty plan (bot will wait)
        return SpreadGridPlan(
            best_bid=bid_d, best_ask=ask_d, synthetic_side=synthetic_side,
            mid=mid, boundary=mid,
            buy_levels=[], sell_levels=[],
            last_trade_side=last_trade_side, bias=b,
        )

    # ── Determine buy/sell split based on last trade direction ──────────
    if last_trade_side == "buy":
        # Price pressure upward → more buys, boundary shifts up
        buy_ratio = Decimal("0.5") + b / Decimal("2")
        boundary_shift = (spread * b / Decimal("2"))
        boundary = mid + boundary_shift
    elif last_trade_side == "sell":
        # Price pressure downward → more sells, boundary shifts down
        buy_ratio = Decimal("0.5") - b / Decimal("2")
        boundary_shift = (spread * b / Decimal("2"))
        boundary = mid - boundary_shift
    else:
        # No trades → symmetric
        buy_ratio = Decimal("0.5")
        boundary = mid

    # Clamp boundary within [low + tick, high - tick]
    boundary = max(low + tick, min(high - tick, boundary))

    buy_count = max(1, round(float(levels) * float(buy_ratio)))
    sell_count = max(1, levels - buy_count)
    # Ensure we don't exceed total
    if buy_count + sell_count > levels:
        sell_count = levels - buy_count

    # ── Place buy levels: from (low + tick) to (boundary - tick) ────────
    buy_levels = _place_levels(
        side="buy",
        count=buy_count,
        price_low=low + tick,
        price_high=boundary - tick,
        quantity=q,
        tick=tick,
        price_precision=price_precision,
        qty_precision=qty_precision,
    )

    # ── Place sell levels: from (boundary + tick) to (high - tick) ──────
    sell_levels = _place_levels(
        side="sell",
        count=sell_count,
        price_low=boundary + tick,
        price_high=high - tick,
        quantity=q,
        tick=tick,
        price_precision=price_precision,
        qty_precision=qty_precision,
    )

    return SpreadGridPlan(
        best_bid=bid_d, best_ask=ask_d, synthetic_side=synthetic_side,
        mid=mid, boundary=boundary,
        buy_levels=buy_levels, sell_levels=sell_levels,
        last_trade_side=last_trade_side, bias=b,
    )


# ─── Helper: place N evenly-spaced levels in a range ────────────────────────

def _place_levels(
    *,
    side: Side,
    count: int,
    price_low: Decimal,
    price_high: Decimal,
    quantity: Decimal,
    tick: Decimal,
    price_precision: int,
    qty_precision: int,
) -> list[SpreadLevel]:
    """Place `count` levels evenly spaced in [price_low, price_high].

    Level 1 is closest to the boundary (price_high for buys, price_low
    for sells), level N is furthest. Prices are snapped to tick boundaries
    and rounded down to price_precision.

    If the range is too small to fit `count` distinct tick-separated
    levels, fewer levels are placed (we never place two orders at the
    same price).
    """
    if count <= 0:
        return []
    if price_high < price_low:
        return []

    price_q = Decimal(10) ** -price_precision
    qty_q = Decimal(10) ** -qty_precision
    q_quantized = quantity.quantize(qty_q, rounding=ROUND_DOWN)

    # Even spacing: for N levels in [low, high], the step is (high-low)/(N-1)
    # if N > 1, else just place at the midpoint.
    if count == 1:
        price = ((price_low + price_high) / 2).quantize(price_q, rounding=ROUND_DOWN)
        price = _snap_to_tick(price, tick)
        return [SpreadLevel(side=side, price=price, quantity=q_quantized, level=1)]

    step = (price_high - price_low) / (count - 1)
    levels_out: list[SpreadLevel] = []
    seen_prices: set[Decimal] = set()

    # Generate from closest to boundary outward
    # Buys: level 1 = highest price (closest to boundary), level N = lowest
    # Sells: level 1 = lowest price (closest to boundary), level N = highest
    for i in range(count):
        if side == "buy":
            # level 1 at price_high, level N at price_low
            raw_price = price_high - step * i
        else:
            # level 1 at price_low, level N at price_high
            raw_price = price_low + step * i

        price = raw_price.quantize(price_q, rounding=ROUND_DOWN)
        price = _snap_to_tick(price, tick)

        # Skip duplicates (can happen when range is tight)
        if price in seen_prices:
            continue
        seen_prices.add(price)

        # Skip prices outside the valid range
        if side == "buy" and price >= price_high + tick:
            continue
        if side == "sell" and price <= price_low - tick:
            continue

        levels_out.append(SpreadLevel(
            side=side, price=price, quantity=q_quantized,
            level=len(levels_out) + 1,
        ))

    return levels_out


def _snap_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Snap a price down to the nearest tick boundary.

    E.g., snap_to_tick(100.007, 0.01) = 100.00.
    """
    if tick <= 0:
        return price
    ticks = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return (ticks * tick).quantize(price.normalize() if price != 0 else tick)


# ─── Order payload generation ───────────────────────────────────────────────

def level_to_order_request(level: SpreadLevel, *, symbol: str) -> dict:
    """Convert a SpreadLevel to an OrderCreateRequest payload."""
    return {
        "symbol": symbol,
        "side": level.side,
        "type": "limit",
        "price": str(level.price),
        "quantity": str(level.quantity),
        "time_in_force": "gtc",
    }


def plan_to_order_requests(plan: SpreadGridPlan, *, symbol: str) -> list[dict]:
    """Convert an entire plan to a list of order payloads."""
    return [level_to_order_request(lvl, symbol=symbol) for lvl in plan.all_levels]


__all__ = [
    "Side",
    "TradeSide",
    "SpreadLevel",
    "SpreadGridPlan",
    "calculate_spread_grid",
    "level_to_order_request",
    "plan_to_order_requests",
]
