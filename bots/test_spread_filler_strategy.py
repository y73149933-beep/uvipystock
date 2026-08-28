"""Unit tests for the spread-filler strategy logic.

Verifies all three market states (both sides, only bid, only ask),
direction bias behavior, tick snapping, and edge cases.

Run with::

    cd /path/to/uvipystock
    python -m pytest bots/test_spread_filler_strategy.py -v
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.spread_filler.strategy import (  # noqa: E402
    SpreadGridPlan,
    calculate_spread_grid,
    level_to_order_request,
    plan_to_order_requests,
)


# ─── Case 1: both sides present ─────────────────────────────────────────────

def test_both_sides_basic_shape():
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5",
    )
    assert plan.best_bid == Decimal("100.00")
    assert plan.best_ask == Decimal("102.00")
    assert plan.mid == Decimal("101.00")
    assert plan.synthetic_side is None
    assert plan.total_orders == 10


def test_both_sides_buys_below_boundary_sells_above():
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5",
    )
    for lvl in plan.buy_levels:
        assert lvl.side == "buy"
        assert lvl.price < plan.boundary
    for lvl in plan.sell_levels:
        assert lvl.side == "sell"
        assert lvl.price > plan.boundary


def test_both_sides_no_bias_is_symmetric():
    """With no last trade and bias=0, buys and sells should be equal count."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5", bias="0",
    )
    assert len(plan.buy_levels) == 5
    assert len(plan.sell_levels) == 5
    assert plan.boundary == plan.mid


# ─── Direction bias ─────────────────────────────────────────────────────────

def test_buy_bias_more_buys_higher_boundary():
    """Last trade = buy → more buy levels, boundary shifts up."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        last_trade_side="buy",
        levels=10, quantity="0.5", bias="0.4",
    )
    # With bias=0.4: buy_ratio = 0.5 + 0.2 = 0.7 → 7 buys, 3 sells
    assert len(plan.buy_levels) == 7
    assert len(plan.sell_levels) == 3
    # Boundary should be above mid
    assert plan.boundary > plan.mid


def test_sell_bias_more_sells_lower_boundary():
    """Last trade = sell → more sell levels, boundary shifts down."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        last_trade_side="sell",
        levels=10, quantity="0.5", bias="0.4",
    )
    # With bias=0.4: buy_ratio = 0.5 - 0.2 = 0.3 → 3 buys, 7 sells
    assert len(plan.buy_levels) == 3
    assert len(plan.sell_levels) == 7
    # Boundary should be below mid
    assert plan.boundary < plan.mid


def test_buy_bias_pulls_buys_up():
    """With buy bias, the highest buy should be closer to the ask than without bias."""
    plan_neutral = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5", bias="0",
    )
    plan_buy = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        last_trade_side="buy",
        levels=10, quantity="0.5", bias="0.6",
    )
    # Highest buy = the buy level with the max price
    max_buy_neutral = max(lvl.price for lvl in plan_neutral.buy_levels)
    max_buy_biased = max(lvl.price for lvl in plan_buy.buy_levels)
    assert max_buy_biased > max_buy_neutral, (
        f"Buy bias should pull buys UP: neutral max={max_buy_neutral}, "
        f"biased max={max_buy_biased}"
    )


def test_zero_bias_ignores_last_trade():
    """bias=0 means the last trade direction has no effect."""
    plan_a = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        last_trade_side="buy",
        levels=10, quantity="0.5", bias="0",
    )
    plan_b = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        last_trade_side="sell",
        levels=10, quantity="0.5", bias="0",
    )
    assert len(plan_a.buy_levels) == len(plan_b.buy_levels)
    assert plan_a.boundary == plan_b.boundary


# ─── Case 2: only bid present ───────────────────────────────────────────────

def test_only_bid_synthesizes_ask():
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask=None,
        levels=6, quantity="0.5",
        single_side_buffer_pct="1.0",
    )
    assert plan.best_bid == Decimal("100.00")
    assert plan.best_ask is None
    assert plan.synthetic_side == "ask_synthetic"
    # Synthesized ask = 100 * 1.01 = 101
    assert plan.mid == Decimal("100.5")  # (100 + 101) / 2
    # Should still place both buys and sells
    assert len(plan.buy_levels) > 0
    assert len(plan.sell_levels) > 0


def test_only_bid_sells_are_above_bid():
    """When only bid exists, our sells should be above the bid."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask=None,
        levels=6, quantity="0.5",
    )
    for lvl in plan.sell_levels:
        assert lvl.price > Decimal("100.00"), \
            f"Sell at {lvl.price} should be above bid 100.00"


# ─── Case 3: only ask present ───────────────────────────────────────────────

def test_only_ask_synthesizes_bid():
    plan = calculate_spread_grid(
        best_bid=None, best_ask="102.00",
        levels=6, quantity="0.5",
        single_side_buffer_pct="1.0",
    )
    assert plan.best_bid is None
    assert plan.best_ask == Decimal("102.00")
    assert plan.synthetic_side == "bid_synthetic"
    # Synthesized bid = 102 * 0.99 = 100.98
    assert plan.mid == Decimal("101.49")  # (100.98 + 102) / 2
    assert len(plan.buy_levels) > 0
    assert len(plan.sell_levels) > 0


def test_only_ask_buys_are_below_ask():
    """When only ask exists, our buys should be below the ask."""
    plan = calculate_spread_grid(
        best_bid=None, best_ask="102.00",
        levels=6, quantity="0.5",
    )
    for lvl in plan.buy_levels:
        assert lvl.price < Decimal("102.00"), \
            f"Buy at {lvl.price} should be below ask 102.00"


# ─── Case 4: empty book ─────────────────────────────────────────────────────

def test_empty_book_raises():
    with pytest.raises(ValueError, match="empty book"):
        calculate_spread_grid(
            best_bid=None, best_ask=None,
            levels=6, quantity="0.5",
        )


# ─── Crossed book ───────────────────────────────────────────────────────────

def test_crossed_book_raises():
    """best_ask <= best_bid is invalid."""
    with pytest.raises(ValueError, match="Crossed"):
        calculate_spread_grid(
            best_bid="102.00", best_ask="100.00",
            levels=6, quantity="0.5",
        )


def test_locked_book_raises():
    """best_ask == best_bid (locked) is also invalid."""
    with pytest.raises(ValueError, match="Crossed"):
        calculate_spread_grid(
            best_bid="100.00", best_ask="100.00",
            levels=6, quantity="0.5",
        )


# ─── Min spread check ───────────────────────────────────────────────────────

def test_too_tight_spread_returns_empty_plan():
    """If spread < min_spread_ticks * tick_size, return an empty plan."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="100.01",  # 1 tick spread
        levels=10, quantity="0.5",
        tick_size="0.01",
        min_spread_ticks=2,
    )
    assert plan.total_orders == 0
    assert plan.buy_levels == []
    assert plan.sell_levels == []


# ─── Tick snapping ───────────────────────────────────────────────────────────

def test_prices_are_on_tick_boundaries():
    """All prices should be multiples of tick_size."""
    tick = Decimal("0.05")
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="101.00",
        levels=8, quantity="0.5",
        tick_size=str(tick),
    )
    for lvl in plan.all_levels:
        # price / tick should be a whole number (within rounding tolerance)
        ratio = lvl.price / tick
        assert ratio == ratio.to_integral_value(), (
            f"Price {lvl.price} is not on a tick boundary (tick={tick})"
        )


# ─── Level ordering ─────────────────────────────────────────────────────────

def test_buy_levels_ordered_by_proximity_to_boundary():
    """Buy level 1 should be closest to boundary (highest buy price)."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5", bias="0",
    )
    for i in range(len(plan.buy_levels) - 1):
        assert plan.buy_levels[i].price > plan.buy_levels[i + 1].price, \
            f"Buy level {i+1} ({plan.buy_levels[i].price}) should be > " \
            f"level {i+2} ({plan.buy_levels[i+1].price})"


def test_sell_levels_ordered_by_proximity_to_boundary():
    """Sell level 1 should be closest to boundary (lowest sell price)."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5", bias="0",
    )
    for i in range(len(plan.sell_levels) - 1):
        assert plan.sell_levels[i].price < plan.sell_levels[i + 1].price, \
            f"Sell level {i+1} ({plan.sell_levels[i].price}) should be < " \
            f"level {i+2} ({plan.sell_levels[i+1].price})"


# ─── Order payload generation ───────────────────────────────────────────────

def test_level_to_order_request():
    from bots.spread_filler.strategy import SpreadLevel
    lvl = SpreadLevel(side="buy", price=Decimal("100.50"), quantity=Decimal("0.5"),
                      level=2)
    req = level_to_order_request(lvl, symbol="BTC/USDT")
    assert req == {
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
        "price": "100.50",
        "quantity": "0.5",
        "time_in_force": "gtc",
    }


def test_plan_to_order_requests_count():
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5",
    )
    reqs = plan_to_order_requests(plan, symbol="BTC/USDT")
    assert len(reqs) == plan.total_orders
    for r in reqs:
        assert r["symbol"] == "BTC/USDT"
        assert r["type"] == "limit"
        assert r["time_in_force"] == "gtc"


# ─── Input validation ───────────────────────────────────────────────────────

def test_invalid_levels_raises():
    with pytest.raises(ValueError):
        calculate_spread_grid(best_bid="100", best_ask="102",
                              levels=0, quantity="0.5")


def test_invalid_bias_raises():
    with pytest.raises(ValueError):
        calculate_spread_grid(best_bid="100", best_ask="102",
                              levels=5, quantity="0.5", bias="1.5")


def test_negative_quantity_raises():
    with pytest.raises(ValueError):
        calculate_spread_grid(best_bid="100", best_ask="102",
                              levels=5, quantity="-1")


# ─── Capital requirements ───────────────────────────────────────────────────

def test_capital_calculations():
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=4, quantity="0.5", bias="0",
    )
    # buy_quote_capital = sum(price * qty) for buys
    expected_buy = sum((lvl.price * lvl.quantity for lvl in plan.buy_levels), Decimal("0"))
    assert plan.buy_quote_capital == expected_buy
    # sell_base_capital = sum(qty) for sells
    expected_sell = sum((lvl.quantity for lvl in plan.sell_levels), Decimal("0"))
    assert plan.sell_base_capital == expected_sell


# ─── all_levels ordering ────────────────────────────────────────────────────

def test_all_levels_descending_price():
    """all_levels should be sorted from highest (sell) to lowest (buy)."""
    plan = calculate_spread_grid(
        best_bid="100.00", best_ask="102.00",
        levels=10, quantity="0.5", bias="0",
    )
    prices = [lvl.price for lvl in plan.all_levels]
    for i in range(len(prices) - 1):
        assert prices[i] > prices[i + 1], \
            f"Level {i} ({prices[i]}) should be > level {i+1} ({prices[i+1]})"


# ─── Integration: buy + sell never cross ────────────────────────────────────

def test_buys_and_sells_never_cross():
    """No buy price should be >= any sell price."""
    for bias_val in ("0", "0.3", "0.6", "1.0"):
        for side in ("buy", "sell", None):
            plan = calculate_spread_grid(
                best_bid="100.00", best_ask="102.00",
                last_trade_side=side,
                levels=10, quantity="0.5", bias=bias_val,
            )
            max_buy = max((l.price for l in plan.buy_levels), default=Decimal("0"))
            min_sell = min((l.price for l in plan.sell_levels), default=Decimal("999999"))
            assert max_buy < min_sell, (
                f"Cross! bias={bias_val} side={side}: "
                f"max_buy={max_buy} >= min_sell={min_sell}"
            )


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
    sys.exit(0 if passed == len(tests) else 1)
