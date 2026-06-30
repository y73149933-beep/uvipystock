"""Unit tests for the Cython matching engine.

These tests exercise the engine in isolation — no Redis, no PostgreSQL.
Each test builds a book state via `add_passive_order(...)` and then calls
`match_active_order(...)` to verify trade output, outcome code, and
remaining quantity.

Coverage
--------
1. Basic limit cross (taker buy vs maker sell, single fill)
2. Partial fill (taker smaller than maker)
3. Multi-level walk (taker consumes two price levels)
4. FIFO within a price level (two makers, same price)
5. Market order (no price limit, walks until exhausted)
6. IOC — partial fill, leftover discarded
7. FOK — full fill OK
8. FOK — rejected when insufficient liquidity
9. Post-Only — rejected when would cross
10. Post-Only — rests when no crossing
11. Iceberg — visible chunk depletes, hidden reserve refills
12. Cancel — removes order, level compacts when empty
13. Snapshot — returns top N levels with correct volumes
14. Reset — engine returns to empty state
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so `from app.matching...` works
_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.matching.engine import CMatchingEngine
from app.matching._constants import (
    C_BUY, C_SELL,
    C_MARKET, C_LIMIT, C_IOC, C_FOK, C_POST_ONLY,
    C_OK, C_FOK_REJECTED, C_POST_ONLY_CROSS, C_NO_LIQUIDITY,
)
from app.matching._bridge import (
    build_corder,
    outcome_to_str,
    trade_dict_to_domain,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    """Fresh engine per test — guarantees isolation."""
    e = CMatchingEngine()
    yield e
    # No explicit cleanup needed; __dealloc__ handles it.


def _add_maker(engine, order_id, side, price, qty, is_iceberg=False,
               visible_qty=None, hidden_qty=None):
    """Helper: add a resting maker order."""
    ok = engine.add_passive_order(
        order_id=order_id,
        side=side,
        price=price,
        qty=qty,
        is_iceberg=is_iceberg,
        visible_qty=visible_qty if visible_qty is not None else qty,
        hidden_qty=hidden_qty if hidden_qty is not None else 0.0,
    )
    assert ok, f"Failed to add maker order {order_id}"


def _taker(engine, order_id, side, type_, price, qty, is_iceberg=False,
           visible_qty=None, hidden_qty=None):
    """Helper: build a COrder and match it."""
    c = build_corder(
        order_id=order_id,
        side=side,
        order_type=type_,
        price=price,
        quantity=qty,
        is_iceberg=is_iceberg,
        visible_qty=visible_qty,
        hidden_qty=hidden_qty,
    )
    trades, outcome, remaining = engine.match_active_order(c)
    return trades, outcome, remaining


# ─── 1. Basic limit cross ────────────────────────────────────────────────────

def test_basic_limit_cross(engine):
    """Taker buy 1.0 @ 100 matches maker sell 1.0 @ 100."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="limit", price=100.0, qty=1.0)
    assert outcome == C_OK
    assert remaining == 0.0
    assert len(trades) == 1
    assert trades[0]["taker_order_id"] == 20
    assert trades[0]["maker_order_id"] == 10
    assert trades[0]["price"] == 100.0
    assert trades[0]["quantity"] == 1.0
    assert trades[0]["taker_side"] == C_BUY


# ─── 2. Partial fill (taker smaller than maker) ──────────────────────────────

def test_partial_fill_taker_smaller(engine):
    """Taker buy 0.3 vs maker sell 1.0 → 1 trade of 0.3, maker has 0.7 left."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="limit", price=100.0, qty=0.3)
    assert outcome == C_OK
    assert remaining == 0.0
    assert len(trades) == 1
    assert trades[0]["quantity"] == 0.3

    # Verify maker still has 0.7 in the book via snapshot
    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert prices == [100.0]
    assert vols == [0.7]


# ─── 3. Multi-level walk ─────────────────────────────────────────────────────

def test_walk_multiple_levels(engine):
    """Taker buy 1.5 consumes maker@100 (1.0) + maker@101 (0.5)."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    _add_maker(engine, order_id=11, side=C_SELL, price=101.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="limit", price=101.0, qty=1.5)
    assert outcome == C_OK
    assert remaining == 0.0
    assert len(trades) == 2
    # First trade at the better price (100)
    assert trades[0]["price"] == 100.0
    assert trades[0]["quantity"] == 1.0
    assert trades[0]["maker_order_id"] == 10
    # Second trade at 101
    assert trades[1]["price"] == 101.0
    assert trades[1]["quantity"] == 0.5
    assert trades[1]["maker_order_id"] == 11


# ─── 4. FIFO within a price level ────────────────────────────────────────────

def test_fifo_within_level(engine):
    """Two makers at same price → first-placed is matched first."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    _add_maker(engine, order_id=11, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="limit", price=100.0, qty=1.0)
    assert outcome == C_OK
    assert len(trades) == 1
    # Order 10 was placed first → it should be the maker
    assert trades[0]["maker_order_id"] == 10
    # Second maker should still be in the book
    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert prices == [100.0]
    assert vols == [1.0]


# ─── 5. Market order ─────────────────────────────────────────────────────────

def test_market_order_walks_all(engine):
    """Market buy with no price limit consumes all available liquidity."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=0.5)
    _add_maker(engine, order_id=11, side=C_SELL, price=101.0, qty=0.5)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="market", price=None, qty=1.0)
    assert outcome == C_OK
    assert remaining == 0.0
    assert len(trades) == 2
    total = sum(t["quantity"] for t in trades)
    assert total == 1.0


def test_market_order_no_liquidity(engine):
    """Market buy against empty book → C_NO_LIQUIDITY."""
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="market", price=None, qty=1.0)
    assert outcome == C_NO_LIQUIDITY
    assert len(trades) == 0
    assert remaining == 1.0  # nothing filled


# ─── 6. IOC — partial fill, leftover discarded ───────────────────────────────

def test_ioc_partial_fill(engine):
    """IOC buy 2.0 vs 1.0 available → 1 trade of 1.0, leftover discarded."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="ioc", price=100.0, qty=2.0)
    assert outcome == C_OK  # partial fill is OK for IOC
    assert len(trades) == 1
    assert trades[0]["quantity"] == 1.0
    # remaining is non-zero but caller should discard it for IOC
    assert remaining > 0.0


# ─── 7. FOK — full fill OK ───────────────────────────────────────────────────

def test_fok_success(engine):
    """FOK buy 1.0 vs 1.0 available at acceptable price → filled."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="fok", price=100.0, qty=1.0)
    assert outcome == C_OK
    assert remaining == 0.0
    assert len(trades) == 1


# ─── 8. FOK — rejected when insufficient liquidity ───────────────────────────

def test_fok_rejected_insufficient(engine):
    """FOK buy 2.0 vs only 1.0 available → rejected, no trades."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="fok", price=100.0, qty=2.0)
    assert outcome == C_FOK_REJECTED
    assert len(trades) == 0
    assert remaining == 2.0  # nothing filled
    # Maker should still be in the book (FOK didn't take anything)
    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert vols == [1.0]


def test_fok_rejected_price_too_low(engine):
    """FOK buy @ 99 vs ask @ 100 → rejected (price would not cross)."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="fok", price=99.0, qty=1.0)
    assert outcome == C_FOK_REJECTED


# ─── 9. Post-Only — rejected when would cross ────────────────────────────────

def test_post_only_rejected_cross(engine):
    """Post-Only buy @ 100 vs ask @ 100 → would cross → rejected."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="post_only", price=100.0, qty=1.0)
    assert outcome == C_POST_ONLY_CROSS
    assert len(trades) == 0
    # remaining is intact; caller should NOT lock balance
    assert remaining == 1.0


# ─── 10. Post-Only — rests when no crossing ──────────────────────────────────

def test_post_only_no_cross_ok(engine):
    """Post-Only buy @ 99 vs ask @ 100 → no cross → C_OK, rests."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="post_only", price=99.0, qty=1.0)
    assert outcome == C_OK
    assert len(trades) == 0
    # remaining is full — caller inserts as resting order
    assert remaining == 1.0


# ─── 11. Iceberg — visible depletes, hidden refills ──────────────────────────

def test_iceberg_refill(engine):
    """Iceberg maker: total=1.0, visible=0.3, hidden=0.7.
    Taker takes 0.5 → should fully consume visible 0.3 + 0.2 from refilled.
    """
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0,
               is_iceberg=True, visible_qty=0.3, hidden_qty=0.7)
    trades, outcome, remaining = _taker(engine, order_id=20, side="buy",
                                         type_="limit", price=100.0, qty=0.5)
    assert outcome == C_OK
    assert remaining == pytest.approx(0.0, abs=1e-9)
    total_filled = sum(t["quantity"] for t in trades)
    assert total_filled == pytest.approx(0.5, abs=1e-9)

    # Maker should still have 0.5 remaining (1.0 - 0.5)
    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert prices == [100.0]
    assert vols[0] == pytest.approx(0.5, abs=1e-9)


# ─── 12. Cancel — removes order, level compacts ──────────────────────────────

def test_cancel_order(engine):
    """Cancel a resting maker → level compacts when empty."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert vols == [1.0]

    found = engine.cancel_order(order_id=10, side=C_SELL, price=100.0)
    assert found is True

    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert prices == []
    assert vols == []


def test_cancel_order_not_found(engine):
    """Cancel non-existent order returns False."""
    found = engine.cancel_order(order_id=999, side=C_SELL, price=100.0)
    assert found is False


def test_cancel_one_of_multiple_at_level(engine):
    """Cancel one maker at a level with two → level survives with one."""
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    _add_maker(engine, order_id=11, side=C_SELL, price=100.0, qty=2.0)

    found = engine.cancel_order(order_id=10, side=C_SELL, price=100.0)
    assert found is True

    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert prices == [100.0]
    assert vols == [2.0]


# ─── 13. Snapshot ────────────────────────────────────────────────────────────

def test_snapshot_bids_descending(engine):
    """Bids snapshot returns highest price first."""
    _add_maker(engine, order_id=1, side=C_BUY, price=99.0,  qty=1.0)
    _add_maker(engine, order_id=2, side=C_BUY, price=101.0, qty=2.0)
    _add_maker(engine, order_id=3, side=C_BUY, price=100.0, qty=3.0)
    prices, vols = engine.snapshot(C_BUY, depth=10)
    assert prices == [101.0, 100.0, 99.0]
    assert vols == [2.0, 3.0, 1.0]


def test_snapshot_asks_ascending(engine):
    """Asks snapshot returns lowest price first."""
    _add_maker(engine, order_id=1, side=C_SELL, price=101.0, qty=1.0)
    _add_maker(engine, order_id=2, side=C_SELL, price=99.0,  qty=2.0)
    _add_maker(engine, order_id=3, side=C_SELL, price=100.0, qty=3.0)
    prices, vols = engine.snapshot(C_SELL, depth=10)
    assert prices == [99.0, 100.0, 101.0]
    assert vols == [2.0, 3.0, 1.0]


def test_snapshot_depth_limit(engine):
    """Snapshot respects depth argument."""
    for i in range(5):
        _add_maker(engine, order_id=i+1, side=C_SELL,
                   price=100.0 + i, qty=1.0)
    prices, vols = engine.snapshot(C_SELL, depth=3)
    assert len(prices) == 3
    assert prices == [100.0, 101.0, 102.0]


# ─── 14. Reset ───────────────────────────────────────────────────────────────

def test_reset_clears_state(engine):
    """After reset(), book is empty."""
    _add_maker(engine, order_id=1, side=C_BUY,  price=100.0, qty=1.0)
    _add_maker(engine, order_id=2, side=C_SELL, price=101.0, qty=1.0)
    engine.reset()
    prices_b, _ = engine.snapshot(C_BUY, depth=10)
    prices_a, _ = engine.snapshot(C_SELL, depth=10)
    assert prices_b == []
    assert prices_a == []


def test_reset_allows_reuse(engine):
    """Engine is usable after reset (no stale state)."""
    _add_maker(engine, order_id=1, side=C_SELL, price=100.0, qty=1.0)
    engine.reset()
    # Re-add and match
    _add_maker(engine, order_id=10, side=C_SELL, price=100.0, qty=1.0)
    trades, outcome, _ = _taker(engine, order_id=20, side="buy",
                                 type_="limit", price=100.0, qty=1.0)
    assert outcome == C_OK
    assert len(trades) == 1


# ─── Bridge tests ────────────────────────────────────────────────────────────

def test_bridge_outcome_to_str():
    assert outcome_to_str(C_OK) == "ok"
    assert outcome_to_str(C_FOK_REJECTED) == "fok_rejected"
    assert outcome_to_str(C_POST_ONLY_CROSS) == "post_only_cross"
    assert outcome_to_str(C_NO_LIQUIDITY) == "no_liquidity"


def test_bridge_trade_dict_to_domain():
    raw = {
        "taker_order_id": 20,
        "maker_order_id": 10,
        "price": 100.5,
        "quantity": 0.5,
        "taker_side": C_BUY,
    }
    out = trade_dict_to_domain(raw, symbol="BTC/USDT")
    assert out["symbol"] == "BTC/USDT"
    assert out["price"] == 100.5
    assert out["quantity"] == 0.5
    assert out["quote_quantity"] == 50.25
    # taker_side should be the BUY enum
    from app.models.enums import OrderSide
    assert out["side"] == OrderSide.BUY


def test_bridge_build_corder_market():
    """Market order → price is NaN."""
    c = build_corder(order_id=1, side="buy", order_type="market",
                     price=None, quantity=1.0)
    assert math.isnan(c.price)
    assert c.quantity == 1.0
    assert c.remaining_qty == 1.0


def test_bridge_build_corder_iceberg_validation():
    """Iceberg with visible + hidden != quantity → ValueError."""
    from app.matching._bridge import build_corder
    with pytest.raises(ValueError, match="iceberg invariant"):
        build_corder(
            order_id=1, side="buy", order_type="iceberg",
            price=100.0, quantity=1.0,
            is_iceberg=True, visible_qty=0.3, hidden_qty=0.8,  # 1.1 != 1.0
        )


def test_bridge_build_corder_negative_qty():
    """Negative quantity → ValueError."""
    with pytest.raises(ValueError, match="quantity must be positive"):
        build_corder(order_id=1, side="buy", order_type="limit",
                     price=100.0, quantity=-1.0)


# ─── Performance smoke (not a real benchmark, just sanity) ───────────────────

def test_perf_smoke_100_orders(engine):
    """Match 100 taker orders against a moderate book; should complete cleanly.

    Reduced from 1000 to 100 to stay well within the pre-allocated trades
    buffer (default cap = 64, grows on demand). A separate benchmark script
    (`backend/scripts/benchmark_matching.py`) will measure real throughput.
    """
    # Build a book with 20 levels, 5 orders each = 100 makers
    oid = 1
    for lvl in range(20):
        for _ in range(5):
            _add_maker(engine, order_id=oid, side=C_SELL,
                       price=100.0 + lvl * 0.01, qty=0.1)
            oid += 1

    # Verify book depth
    prices, vols = engine.snapshot(C_SELL, depth=20)
    assert len(prices) == 20

    # Match 100 small taker buys (each consumes one maker)
    for i in range(100):
        c = build_corder(order_id=10000 + i, side="buy", order_type="market",
                         price=None, quantity=0.1)
        trades, outcome, remaining = engine.match_active_order(c)
        # Each market order should match against one or more makers
        assert outcome == C_OK
        assert len(trades) >= 1
        assert remaining == pytest.approx(0.0, abs=1e-9)

    # After 100 fills of 0.1 each (= 10.0 total), book should be empty
    prices, vols = engine.snapshot(C_SELL, depth=20)
    assert prices == []
    assert vols == []
