"""Smoke / unit tests for the orderbook_viewer bot.

Run with::

    cd /path/to/uvipystock
    python -m pytest bots/test_orderbook_viewer.py -v
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Make the repo root importable when running pytest from there
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.orderbook_viewer.book import LocalOrderBook, Level  # noqa: E402


# ─── LocalOrderBook ─────────────────────────────────────────────────────────

def test_snapshot_replaces_state():
    book = LocalOrderBook(symbol="BTC/USDT")
    book.apply_snapshot(
        bids=[("100.00", "1.5"), ("99.50", "0.5")],
        asks=[("100.50", "2.0"), ("101.00", "0.0")],  # 0 qty must be dropped
        last_trade_price="100.25",
        ts=1,
    )
    assert book.bid_levels_count() == 2
    assert book.ask_levels_count() == 1  # 101.00 was dropped
    bb = book.best_bid()
    ba = book.best_ask()
    assert bb == Level(Decimal("100.00"), Decimal("1.5"))
    assert ba == Level(Decimal("100.50"), Decimal("2.0"))
    assert book.last_trade_price == Decimal("100.25")


def test_update_adds_and_removes_levels():
    book = LocalOrderBook(symbol="BTC/USDT")
    book.apply_snapshot(
        bids=[("100.00", "1.0")],
        asks=[("100.50", "1.0")],
    )
    book.apply_update([
        {"side": "bid", "price": "100.00", "qty": "2.5"},  # update existing
        {"side": "bid", "price": "99.50", "qty": "0.8"},   # add new level
        {"side": "ask", "price": "100.50", "qty": "0"},    # remove level
        {"side": "ask", "price": "101.00", "qty": "3.0"},  # add new level
    ], ts=2)
    assert book.bid_levels_count() == 2
    assert book.ask_levels_count() == 1  # 100.50 removed, 101.00 added
    assert book.best_bid().quantity == Decimal("2.5")
    assert book.best_ask().price == Decimal("101.00")


def test_spread_and_mid():
    book = LocalOrderBook(symbol="BTC/USDT")
    book.apply_snapshot(
        bids=[("100.00", "1.0")],
        asks=[("100.10", "1.0")],
    )
    spread = book.spread()
    assert spread is not None
    abs_spread, pct = spread
    assert abs_spread == Decimal("0.10")
    # mid = (100.00 + 100.10) / 2 = 100.05;  pct = 0.10 / 100.05 * 100
    assert pct == Decimal("0.09995002498750624687656171914")
    mid = book.mid_price()
    assert mid == Decimal("100.05")


def test_top_n_respects_side_ordering():
    book = LocalOrderBook(symbol="BTC/USDT")
    book.apply_snapshot(
        bids=[("100.00", "1.0"), ("101.00", "2.0"), ("99.00", "0.5")],
        asks=[("100.50", "1.0"), ("100.40", "2.0"), ("101.00", "0.5")],
    )
    bids, asks = book.top(2)
    # Bids: highest first
    assert [b.price for b in bids] == [Decimal("101.00"), Decimal("100.00")]
    # Asks: lowest first
    assert [a.price for a in asks] == [Decimal("100.40"), Decimal("100.50")]


def test_empty_book_returns_none():
    book = LocalOrderBook(symbol="BTC/USDT")
    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.spread() is None
    assert book.mid_price() is None
    assert book.top(10) == ([], [])


def test_trade_does_not_mutate_book():
    book = LocalOrderBook(symbol="BTC/USDT")
    book.apply_snapshot(
        bids=[("100.00", "1.0")],
        asks=[("100.50", "1.0")],
    )
    book.apply_trade("100.20", ts=42)
    assert book.last_trade_price == Decimal("100.20")
    assert book.last_trade_ts == 42
    # Book state untouched
    assert book.best_bid().price == Decimal("100.00")
    assert book.best_ask().price == Decimal("100.50")


def test_snapshot_overwrites_previous_state():
    """A new snapshot must fully replace the book, not merge."""
    book = LocalOrderBook(symbol="BTC/USDT")
    book.apply_snapshot(
        bids=[("100.00", "1.0"), ("99.00", "0.5")],
        asks=[("100.50", "1.0"), ("101.00", "0.5")],
    )
    book.apply_snapshot(
        bids=[("200.00", "2.0")],
        asks=[("201.00", "3.0")],
    )
    assert book.bid_levels_count() == 1
    assert book.ask_levels_count() == 1
    assert book.best_bid().price == Decimal("200.00")


if __name__ == "__main__":
    # Run without pytest if needed
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
