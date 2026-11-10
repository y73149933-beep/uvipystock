"""Unit tests for position/PnL aggregation."""
from decimal import Decimal

from app.services.position_service import TradeLike, compute_positions

UID = 7
OTHER = 8


def T(side, price, qty, taker=UID, maker=OTHER, symbol="BTC/USD"):
    return TradeLike(
        symbol=symbol,
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        taker_user_id=taker,
        maker_user_id=maker,
    )


def test_taker_buys_build_average_cost():
    agg = compute_positions(
        [T("buy", "100", "1"), T("buy", "120", "1")], UID
    )["BTC/USD"]
    assert agg.quantity == Decimal("2")
    assert agg.avg_cost == Decimal("110")
    assert agg.realized_pnl == 0


def test_maker_of_sell_is_a_buy_for_us():
    # we made a buy: taker sold into us
    agg = compute_positions(
        [T("sell", "100", "2", taker=OTHER, maker=UID)], UID
    )["BTC/USD"]
    assert agg.quantity == Decimal("2")
    assert agg.avg_cost == Decimal("100")


def test_partial_sell_realizes_against_average():
    trades = [T("buy", "100", "2"), T("sell", "130", "1")]
    agg = compute_positions(trades, UID)["BTC/USD"]
    assert agg.realized_pnl == Decimal("30")
    assert agg.quantity == Decimal("1")
    assert agg.avg_cost == Decimal("100")  # average unchanged on sell


def test_full_close_resets_basis_keeps_realized():
    trades = [T("buy", "100", "1"), T("sell", "90", "1"), T("buy", "150", "2")]
    agg = compute_positions(trades, UID)["BTC/USD"]
    assert agg.realized_pnl == Decimal("-10")
    assert agg.quantity == Decimal("2")
    assert agg.avg_cost == Decimal("150")  # fresh cycle after flat


def test_unrelated_trades_ignored():
    agg = compute_positions([T("buy", "100", "1", taker=9, maker=10)], UID)
    assert agg == {}


def test_multiple_symbols_independent():
    trades = [
        T("buy", "100", "1", symbol="BTC/USD"),
        T("buy", "10", "5", symbol="ETH/USD"),
        T("sell", "110", "1", symbol="BTC/USD"),
    ]
    out = compute_positions(trades, UID)
    assert out["BTC/USD"].realized_pnl == Decimal("10")
    assert out["BTC/USD"].quantity == 0
    assert out["ETH/USD"].avg_cost == Decimal("10")
    assert out["ETH/USD"].quantity == Decimal("5")


def test_round_trip_symmetry():
    up = compute_positions([T("buy", "100", "1"), T("sell", "105", "1")], UID)["BTC/USD"]
    down = compute_positions([T("buy", "105", "1"), T("sell", "100", "1")], UID)["BTC/USD"]
    assert up.realized_pnl == Decimal("5")
    assert down.realized_pnl == Decimal("-5")
