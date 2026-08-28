"""Unit tests for OrderTracker.

Verifies that REST snapshots + WS lifecycle events produce the correct
open-orders state. The lifecycle is non-trivial (placed / partial fill /
modify / fill / cancel), so we cover each transition explicitly.

Run with::

    cd /path/to/uvipystock
    python -m pytest bots/test_orders.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.orderbook_viewer.orders import (  # noqa: E402
    Action,
    Order,
    OrderTracker,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _ws_payload(
    *,
    order_id: int,
    status: str = "new",
    status_event: str = "placed",
    symbol: str = "BTC/USDT",
    side: str = "buy",
    type_: str = "limit",
    price: str = "100.00",
    quantity: str = "1.0",
    filled_quantity: str = "0",
    remaining_quantity: str | None = None,
    **extra,
) -> dict:
    """Build a private-WS order event payload.

    Extra kwargs (e.g. last_trade_qty, last_trade_price) are merged in
    so tests can simulate realistic event payloads.
    """
    p: dict = {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "type": type_,
        "status": status,
        "status_event": status_event,
        "price": price,
        "quantity": quantity,
        "filled_quantity": filled_quantity,
    }
    if remaining_quantity is not None:
        p["remaining_quantity"] = remaining_quantity
    p.update(extra)
    return p


def _rest_row(
    *,
    order_id: int,
    status: str = "new",
    side: str = "buy",
    type_: str = "limit",
    price: str = "100.00",
    quantity: str = "1.0",
    filled_quantity: str = "0",
    remaining_quantity: str = "1.0",
    created_at: str = "2024-01-01T00:00:00+00:00",
) -> dict:
    return {
        "id": order_id,
        "user_id": 1,
        "symbol": "BTC/USDT",
        "side": side,
        "type": type_,
        "status": status,
        "price": price,
        "stop_price": None,
        "trailing_delta": None,
        "quantity": quantity,
        "filled_quantity": filled_quantity,
        "filled_quote_qty": "0",
        "remaining_quantity": remaining_quantity,
        "avg_fill_price": None,
        "visible_quantity": None,
        "hidden_quantity": None,
        "parent_order_id": None,
        "sl_order_id": None,
        "tp_order_id": None,
        "replaces_id": None,
        "replaced_by_id": None,
        "bulk_id": None,
        "replace_count": 0,
        "created_at": created_at,
        "updated_at": created_at,
    }


# ─── REST snapshot ──────────────────────────────────────────────────────────

def test_rest_snapshot_loads_open_orders():
    bt = OrderTracker()
    bt.apply_rest_snapshot([
        _rest_row(order_id=1, status="new"),
        _rest_row(order_id=2, status="partially_filled",
                  filled_quantity="0.5", remaining_quantity="0.5"),
        _rest_row(order_id=3, status="pending", type_="stop_market", price=None),
        # Terminal orders should be filtered out defensively
        _rest_row(order_id=4, status="filled"),
        _rest_row(order_id=5, status="canceled"),
    ])
    assert bt.snapshot_count == 1
    assert bt.open_count() == 3
    assert bt.get(1) is not None
    assert bt.get(2) is not None
    assert bt.get(3) is not None
    assert bt.get(4) is None  # filtered
    assert bt.get(5) is None  # filtered


def test_rest_snapshot_replaces_state():
    """A new snapshot fully replaces previous state."""
    bt = OrderTracker()
    bt.apply_rest_snapshot([_rest_row(order_id=1)])
    bt.apply_rest_snapshot([_rest_row(order_id=2)])
    assert bt.get(1) is None  # removed
    assert bt.get(2) is not None


def test_rest_snapshot_empty_clears_state():
    bt = OrderTracker()
    bt.apply_rest_snapshot([_rest_row(order_id=1)])
    bt.apply_rest_snapshot([])
    assert bt.open_count() == 0


# ─── WS lifecycle: placed ───────────────────────────────────────────────────

def test_ws_placed_adds_order():
    bt = OrderTracker()
    action = bt.apply_ws_update(_ws_payload(order_id=10, status_event="placed"))
    assert action == Action.PLACED
    assert bt.open_count() == 1
    o = bt.get(10)
    assert o is not None
    assert o.side == "buy"
    assert o.price == Decimal("100.00")
    assert o.quantity == Decimal("1.0")


# ─── WS lifecycle: partially_filled ─────────────────────────────────────────

def test_ws_partial_fill_updates_filled_qty():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(order_id=10, status_event="placed"))

    action = bt.apply_ws_update(_ws_payload(
        order_id=10,
        status="partially_filled",
        status_event="partially_filled",
        filled_quantity="0.3",
        remaining_quantity="0.7",
        last_trade_qty="0.3",
        last_trade_price="100.00",
    ))
    assert action == Action.UPDATED
    o = bt.get(10)
    assert o is not None
    assert o.filled_quantity == Decimal("0.3")
    assert o.remaining_quantity == Decimal("0.7")
    assert o.status == "partially_filled"
    # Order should still be open
    assert o.is_open


def test_ws_partial_fill_preserves_missing_fields():
    """A partial-fill event may omit price/quantity — preserve them."""
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(
        order_id=10, price="100.00", quantity="1.0", status_event="placed",
    ))
    # Partial fill event with no price/quantity keys
    bt.apply_ws_update({
        "order_id": 10,
        "status": "partially_filled",
        "status_event": "partially_filled",
        "filled_quantity": "0.5",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
    })
    o = bt.get(10)
    assert o is not None
    assert o.price == Decimal("100.00")  # preserved
    assert o.quantity == Decimal("1.0")  # preserved
    assert o.filled_quantity == Decimal("0.5")


# ─── WS lifecycle: modified (Cancel-Replace) ────────────────────────────────

def test_ws_modified_updates_price_and_quantity():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(
        order_id=10, price="100.00", quantity="1.0", status_event="placed",
    ))
    action = bt.apply_ws_update({
        "order_id": 10,
        "status": "new",
        "status_event": "modified",
        "price": "101.00",
        "quantity": "2.0",
        "filled_quantity": "0",
        "remaining_quantity": "2.0",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
    })
    assert action == Action.MODIFIED
    o = bt.get(10)
    assert o is not None
    assert o.price == Decimal("101.00")
    assert o.quantity == Decimal("2.0")


# ─── WS lifecycle: filled (terminal) ────────────────────────────────────────

def test_ws_filled_removes_order():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(order_id=10, status_event="placed"))
    assert bt.open_count() == 1

    action = bt.apply_ws_update({
        "order_id": 10,
        "status": "filled",
        "status_event": "filled",
        "filled_quantity": "1.0",
        "remaining_quantity": "0",
        "last_trade_price": "100.00",
        "avg_fill_price": "100.00",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
    })
    assert action == Action.FILLED
    assert bt.open_count() == 0
    assert bt.get(10) is None
    assert bt.filled_count == 1


# ─── WS lifecycle: canceled (terminal) ──────────────────────────────────────

def test_ws_canceled_removes_order():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(order_id=10, status_event="placed"))

    action = bt.apply_ws_update({
        "order_id": 10,
        "status": "canceled",
        "status_event": "canceled",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
    })
    assert action == Action.CANCELED
    assert bt.open_count() == 0
    assert bt.canceled_count == 1


def test_ws_expired_removes_order():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(order_id=10, status_event="placed"))

    action = bt.apply_ws_update({
        "order_id": 10,
        "status": "expired",
        "status_event": "expired",
        "symbol": "BTC/USDT", "side": "buy", "type": "ioc",
    })
    # expired is terminal but not "canceled" — generic REMOVED
    assert action == Action.REMOVED
    assert bt.open_count() == 0


# ─── all_orders sorting ─────────────────────────────────────────────────────

def test_all_orders_sorted_newest_first():
    bt = OrderTracker()
    bt.apply_ws_update({
        "order_id": 1, "status": "new", "status_event": "placed",
        "symbol": "BTC/USDT", "side": "buy", "type": "limit",
        "price": "100", "quantity": "1", "filled_quantity": "0",
        "created_at": "2024-01-01T00:00:00+00:00",
    })
    bt.apply_ws_update({
        "order_id": 2, "status": "new", "status_event": "placed",
        "symbol": "BTC/USDT", "side": "sell", "type": "limit",
        "price": "101", "quantity": "1", "filled_quantity": "0",
        "created_at": "2024-01-02T00:00:00+00:00",
    })
    bt.apply_ws_update({
        "order_id": 3, "status": "new", "status_event": "placed",
        "symbol": "BTC/USDT", "side": "buy", "type": "limit",
        "price": "99", "quantity": "1", "filled_quantity": "0",
        "created_at": "2024-01-03T00:00:00+00:00",
    })
    orders = bt.all_orders()
    # Newest first → order_id 3, 2, 1
    assert [o.id for o in orders] == [3, 2, 1]


# ─── count_for_symbol ───────────────────────────────────────────────────────

def test_count_for_symbol():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(order_id=1, symbol="BTC/USDT"))
    bt.apply_ws_update(_ws_payload(order_id=2, symbol="ETH/USDT"))
    bt.apply_ws_update(_ws_payload(order_id=3, symbol="BTC/USDT"))
    assert bt.count_for_symbol("BTC/USDT") == 2
    assert bt.count_for_symbol("ETH/USDT") == 1
    assert bt.count_for_symbol("SOL/USDT") == 0


# ─── Order helpers ──────────────────────────────────────────────────────────

def test_order_is_buy_and_is_open():
    buy = Order(id=1, symbol="X", side="buy", type="limit", status="new",
                price=Decimal("100"), quantity=Decimal("1"),
                filled_quantity=Decimal("0"), remaining_quantity=Decimal("1"))
    sell = Order(id=2, symbol="X", side="sell", type="limit", status="new",
                 price=Decimal("100"), quantity=Decimal("1"),
                 filled_quantity=Decimal("0"), remaining_quantity=Decimal("1"))
    filled = Order(id=3, symbol="X", side="buy", type="limit", status="filled",
                   price=Decimal("100"), quantity=Decimal("1"),
                   filled_quantity=Decimal("1"), remaining_quantity=Decimal("0"))
    assert buy.is_buy
    assert not sell.is_buy
    assert buy.is_open
    assert sell.is_open
    assert not filled.is_open


# ─── Snapshot isolation ─────────────────────────────────────────────────────

def test_snapshot_is_a_copy():
    bt = OrderTracker()
    bt.apply_ws_update(_ws_payload(order_id=1))
    snap = bt.snapshot()
    # Mutate the copy
    snap.clear()
    # Original tracker is unaffected
    assert bt.open_count() == 1


# ─── Unknown payloads ───────────────────────────────────────────────────────

def test_ws_unknown_payload_returns_unknown_action():
    bt = OrderTracker()
    action = bt.apply_ws_update({"foo": "bar"})  # no order_id
    assert action == Action.UNKNOWN
    assert bt.open_count() == 0


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
