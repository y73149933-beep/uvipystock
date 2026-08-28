"""Unit tests for the grid strategy logic (pure functions, no I/O).

Verifies that level calculation, capital requirements, and order
payload generation all behave correctly. The bot.py orchestration is
tested separately via integration tests against a live backend.

Run with::

    cd /path/to/uvipystock
    python -m pytest bots/test_grid_strategy.py -v
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.grid_bot.strategy import (  # noqa: E402
    GridLevel,
    GridPlan,
    calculate_grid,
    level_to_order_request,
    plan_to_order_requests,
)


# ─── calculate_grid: basic shape ────────────────────────────────────────────

def test_grid_basic_shape_5_levels():
    plan = calculate_grid(
        center="100000", levels=5, spacing_pct="0.5",
        quantity="0.01", price_precision=2, qty_precision=6,
    )
    assert plan.levels_per_side == 5
    assert plan.total_orders == 10
    assert len(plan.buy_levels) == 5
    assert len(plan.sell_levels) == 5
    assert plan.center == Decimal("100000")


def test_grid_buy_levels_below_center():
    """All buy levels must be below the center price."""
    plan = calculate_grid(
        center="100", levels=3, spacing_pct="1.0", quantity="1",
    )
    for lvl in plan.buy_levels:
        assert lvl.side == "buy"
        assert lvl.price < plan.center
    # Level 1 is closest to center; level 3 is furthest
    assert plan.buy_levels[0].level == 1
    assert plan.buy_levels[2].level == 3
    assert plan.buy_levels[0].price > plan.buy_levels[1].price > plan.buy_levels[2].price


def test_grid_sell_levels_above_center():
    """All sell levels must be above the center price."""
    plan = calculate_grid(
        center="100", levels=3, spacing_pct="1.0", quantity="1",
    )
    for lvl in plan.sell_levels:
        assert lvl.side == "sell"
        assert lvl.price > plan.center
    assert plan.sell_levels[0].level == 1
    assert plan.sell_levels[2].level == 3
    # Level 1 closest to center; level 3 furthest
    assert plan.sell_levels[0].price < plan.sell_levels[1].price < plan.sell_levels[2].price


def test_grid_spacing_progression():
    """Level i should be at center * (1 ± i*spacing/100)."""
    plan = calculate_grid(
        center=Decimal("1000"), levels=4, spacing_pct=Decimal("0.5"),
        quantity=Decimal("1"),
    )
    # Level 1 buy: 1000 * (1 - 0.005) = 995.00
    assert plan.buy_levels[0].price == Decimal("995.00")
    # Level 2 buy: 1000 * (1 - 0.010) = 990.00
    assert plan.buy_levels[1].price == Decimal("990.00")
    # Level 4 buy: 1000 * (1 - 0.020) = 980.00
    assert plan.buy_levels[3].price == Decimal("980.00")

    # Level 1 sell: 1000 * (1 + 0.005) = 1005.00
    assert plan.sell_levels[0].price == Decimal("1005.00")
    # Level 4 sell: 1000 * (1 + 0.020) = 1020.00
    assert plan.sell_levels[3].price == Decimal("1020.00")


def test_grid_quantizes_prices_and_quantities():
    """Prices and quantities should be rounded down to the given precision."""
    plan = calculate_grid(
        center="333.33333", levels=1, spacing_pct="0.123",
        quantity="0.987654", price_precision=2, qty_precision=4,
    )
    # Buy: 333.33333 * (1 - 0.00123) = 332.92318... → rounded DOWN to 332.92
    assert plan.buy_levels[0].price == Decimal("332.92")
    # Quantity 0.987654 → rounded DOWN to 0.9876
    assert plan.buy_levels[0].quantity == Decimal("0.9876")
    assert plan.sell_levels[0].quantity == Decimal("0.9876")


# ─── Capital requirements ───────────────────────────────────────────────────

def test_grid_buy_quote_capital():
    """buy_quote_capital = sum(price_i * qty_i) for all buy levels."""
    plan = calculate_grid(
        center="100", levels=2, spacing_pct="1.0", quantity="0.5",
        price_precision=2, qty_precision=6,
    )
    # Buy levels: 99.00 @ 0.5, 98.00 @ 0.5
    # Capital = 99.00 * 0.5 + 98.00 * 0.5 = 49.5 + 49.0 = 98.5
    assert plan.buy_quote_capital == Decimal("98.50")


def test_grid_sell_base_capital():
    """sell_base_capital = sum(quantity_i) for all sell levels."""
    plan = calculate_grid(
        center="100", levels=3, spacing_pct="1.0", quantity="0.5",
    )
    # 3 sell levels × 0.5 = 1.5
    assert plan.sell_base_capital == Decimal("1.5")


def test_grid_capital_zero_for_zero_levels():
    """An empty grid (levels=0 isn't allowed, but check 1-level edge case)."""
    plan = calculate_grid(
        center="100", levels=1, spacing_pct="1.0", quantity="1.0",
    )
    assert plan.buy_quote_capital > 0
    assert plan.sell_base_capital == Decimal("1.0")


# ─── all_levels ordering ────────────────────────────────────────────────────

def test_all_levels_ordering():
    """all_levels should be sorted from highest price (sell N) to lowest (buy N)."""
    plan = calculate_grid(
        center="100", levels=3, spacing_pct="1.0", quantity="1",
    )
    all_lvls = plan.all_levels
    # 6 levels total: 3 sells + 3 buys
    assert len(all_lvls) == 6
    # Prices should be strictly descending
    prices = [lvl.price for lvl in all_lvls]
    for i in range(len(prices) - 1):
        assert prices[i] > prices[i + 1], \
            f"Level {i} price {prices[i]} should be > level {i+1} price {prices[i+1]}"


# ─── Order payload generation ───────────────────────────────────────────────

def test_level_to_order_request_buy():
    lvl = GridLevel(side="buy", level=2, price=Decimal("98.00"),
                    quantity=Decimal("0.5"))
    req = level_to_order_request(lvl, symbol="BTC/USDT")
    assert req == {
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
        "price": "98.00",
        "quantity": "0.5",
        "time_in_force": "gtc",
    }


def test_level_to_order_request_sell():
    lvl = GridLevel(side="sell", level=1, price=Decimal("101.00"),
                    quantity=Decimal("0.25"))
    req = level_to_order_request(lvl, symbol="ETH/USDT")
    assert req["side"] == "sell"
    assert req["price"] == "101.00"
    assert req["quantity"] == "0.25"


def test_plan_to_order_requests_count_matches_total():
    plan = calculate_grid(
        center="100", levels=4, spacing_pct="0.5", quantity="1",
    )
    reqs = plan_to_order_requests(plan, symbol="BTC/USDT")
    assert len(reqs) == plan.total_orders == 8
    # Every request should be valid (have all required keys)
    for r in reqs:
        assert {"symbol", "side", "type", "price", "quantity", "time_in_force"} <= set(r)


def test_plan_to_order_requests_all_use_correct_symbol():
    plan = calculate_grid(
        center="100", levels=2, spacing_pct="0.5", quantity="1",
    )
    reqs = plan_to_order_requests(plan, symbol="SOL/USDC")
    for r in reqs:
        assert r["symbol"] == "SOL/USDC"


# ─── Input validation ───────────────────────────────────────────────────────

def test_calculate_grid_rejects_zero_levels():
    import pytest
    with pytest.raises(ValueError):
        calculate_grid(center="100", levels=0, spacing_pct="1", quantity="1")


def test_calculate_grid_rejects_too_many_levels():
    import pytest
    with pytest.raises(ValueError):
        calculate_grid(center="100", levels=51, spacing_pct="1", quantity="1")


def test_calculate_grid_rejects_zero_spacing():
    import pytest
    with pytest.raises(ValueError):
        calculate_grid(center="100", levels=3, spacing_pct="0", quantity="1")


def test_calculate_grid_rejects_negative_spacing():
    import pytest
    with pytest.raises(ValueError):
        calculate_grid(center="100", levels=3, spacing_pct="-0.5", quantity="1")


def test_calculate_grid_rejects_zero_quantity():
    import pytest
    with pytest.raises(ValueError):
        calculate_grid(center="100", levels=3, spacing_pct="1", quantity="0")


def test_calculate_grid_rejects_zero_center():
    import pytest
    with pytest.raises(ValueError):
        calculate_grid(center="0", levels=3, spacing_pct="1", quantity="1")


# ─── Edge case: 1 level ─────────────────────────────────────────────────────

def test_grid_single_level_each_side():
    """Edge case: levels=1 produces exactly one buy and one sell."""
    plan = calculate_grid(
        center="100", levels=1, spacing_pct="1.0", quantity="1",
    )
    assert plan.total_orders == 2
    assert len(plan.buy_levels) == 1
    assert len(plan.sell_levels) == 1
    assert plan.buy_levels[0].price == Decimal("99.00")
    assert plan.sell_levels[0].price == Decimal("101.00")


# ─── Float / str input acceptance ───────────────────────────────────────────

def test_grid_accepts_float_inputs():
    """The function should accept float and str inputs (auto-converted)."""
    plan = calculate_grid(
        center=100.0, levels=2, spacing_pct=0.5, quantity="0.1",
    )
    assert plan.center == Decimal("100.0")
    assert plan.spacing_pct == Decimal("0.5")
    assert plan.quantity == Decimal("0.1")


def test_grid_level_quote_value():
    """Level.quote_value = price * quantity."""
    lvl = GridLevel(side="buy", level=1,
                    price=Decimal("99.50"), quantity=Decimal("2.0"))
    assert lvl.quote_value == Decimal("199.00")


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
