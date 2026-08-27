"""Regression tests for last-trade-price stop triggering.

Locked-down bug: ANY pending stop fired INSTANTLY whenever opposing
liquidity could fill it, regardless of what the market price did.
Root cause was book-driven "touch" arming (buy-stop fired when
``best_ask <= stop``), degenerating every beyond-market stop into an
executable-now taker.

Correct semantics asserted here:

* the ONLY arming input is the LAST TRADE PRICE
  (sell: ``last <= stop``; buy: ``last >= stop``, equality included);
* the live book NEVER arms a stop — resting liquidity at/beyond the
  trigger stays inert until a print crosses the level;
* placement-time activation still exists but ONLY when the condition
  genuinely holds under the last-price rule;
* arming decisions never read ``orderbook.get_spread``;
* the PG-side numeric re-check blocks stale/wrong-direction references.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest_asyncio
from fakeredis.aioredis import FakeRedis

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

SYM = "BTC/USD"


@pytest_asyncio.fixture
async def redis():
    r = FakeRedis(decode_responses=True)
    yield r
    await r.flushall()


async def seed_resting_limit(redis, oid: int, side: str, price: str,
                             qty: str = "1") -> None:
    """Insert a resting LIMIT order into the public book (maker)."""
    from app.redis_client import orderbook

    key = orderbook.bids_key(SYM) if side == "buy" else orderbook.asks_key(SYM)
    await redis.zadd(key, {str(oid): orderbook.encode_score(Decimal(price), 1)})
    await redis.hset(orderbook.order_hash_key(oid), mapping={
        "order_id": str(oid), "user_id": "999", "symbol": SYM,
        "side": side, "type": "limit", "price": price,
        "quantity": qty, "is_iceberg": "0",
    })


async def seed_pending_stop(redis, oid: int, side: str, stop_price: str,
                            otype: str = "stop_market") -> None:
    """Register a pending conditional order exactly like the API would."""
    from app.redis_client import orderbook, stops

    await orderbook.store_order_hash(redis, oid, {
        "order_id": str(oid), "user_id": "111", "symbol": SYM,
        "side": side, "type": otype, "stop_price": stop_price,
        "quantity": "2", "is_iceberg": "0",
    })
    await stops.add_stop_order(redis, oid, SYM, Decimal(stop_price))


# ─── Redis layer: pure last-trade rules ──────────────────────────────────────

async def test_touch_helpers_removed():
    from app.redis_client import stops

    assert not hasattr(stops, "get_touch_trigger_candidates")
    assert not hasattr(stops, "TOUCH_EPS")
    assert not hasattr(stops, "_load_plain_stop_meta")


async def test_boundary_equality_fires_both_sides_at_once(redis):
    """At last == stop BOTH directional rules hold simultaneously."""
    from app.redis_client import stops

    await seed_pending_stop(redis, 501, "buy", "100")
    await seed_pending_stop(redis, 502, "sell", "100")

    got = await stops.get_all_triggered_stops(redis, SYM, Decimal("100"))
    assert set(got) == {(501, "buy"), (502, "sell")}


async def test_book_depth_cannot_arm_near_triggers(redis):
    """Adversarial book: an ask one tick BELOW a buy trigger and a bid
    one tick ABOVE a sell trigger — exactly what touch semantics armed
    on. Under last-price rules NOTHING fires while prints stay away."""
    from app.redis_client import stops

    await seed_resting_limit(redis, 22, "sell", "104")   # just under buy lvl
    await seed_resting_limit(redis, 23, "buy", "86")     # just over sell lvl
    await seed_pending_stop(redis, 601, "sell", "85")    # fires when last<=85
    await seed_pending_stop(redis, 602, "buy", "105")    # fires when last>=105

    got = await stops.get_all_triggered_stops(redis, SYM, Decimal("95"))
    assert got == []


async def test_gap_down_fires_sell_ladder_ascending(redis):
    """A print below several sell levels fires ALL of them, ascending."""
    from app.redis_client import stops

    await seed_pending_stop(redis, 701, "sell", "105")
    await seed_pending_stop(redis, 702, "sell", "100")
    await seed_pending_stop(redis, 703, "buy", "90")

    got = await stops.get_all_triggered_stops(redis, SYM, Decimal("97"))
    # 97 crosses sells @100/@105 downward AND the buy @90 upward
    assert got == [(702, "sell"), (701, "sell"), (703, "buy")]


# ─── Service wiring ──────────────────────────────────────────────────────────

def _make_monitor():
    from app.services.stop_monitor_service import StopMonitorService
    return StopMonitorService()


def _capture_trigger(monkeypatch, monitor):
    captured: list[tuple] = []

    async def fake_trigger(order_id, trigger_price, is_trailing=False,
                           expected_side=None):
        captured.append((
            order_id,
            trigger_price,
            expected_side.value if expected_side else None,
        ))

    monkeypatch.setattr(monitor, "_trigger_stop_order", fake_trigger)
    return captured


def _wire(monkeypatch, redis):
    import app.services.stop_monitor_service as sms_mod

    monitor = _make_monitor()
    monkeypatch.setattr(sms_mod, "get_redis", lambda: redis)
    return monitor


async def test_user_scenario_resting_ask_does_not_arm(monkeypatch, redis):
    """THE regression scenario: A's Buy Stop-market @100 + B's resting
    Sell Limit @100 — WITHOUT a print the stop must NOT fire even though
    exact liquidity sits at the trigger level."""
    monitor = _wire(monkeypatch, redis)

    await seed_resting_limit(redis, 22, "sell", "100")   # account B
    await seed_pending_stop(redis, 501, "buy", "100")    # account A

    captured = _capture_trigger(monkeypatch, monitor)
    await monitor._evaluate_symbol_stops(SYM)
    assert captured == []


async def test_ltp_below_stop_ignores_matching_book(monkeypatch, redis):
    """Market traded @95 while an ask rests exactly @100 — no arm."""
    monitor = _wire(monkeypatch, redis)
    monitor._last_price[SYM] = Decimal("95")

    await seed_resting_limit(redis, 22, "sell", "100")
    await seed_pending_stop(redis, 501, "buy", "100")

    captured = _capture_trigger(monkeypatch, monitor)
    await monitor._evaluate_symbol_stops(SYM)
    assert captured == []


async def test_print_at_exact_level_arms_buy(monkeypatch, redis):
    """A trade AT the trigger level (inclusive equality) arms the buy."""
    monitor = _wire(monkeypatch, redis)
    monitor._last_price[SYM] = Decimal("100")

    await seed_resting_limit(redis, 22, "sell", "100")
    await seed_pending_stop(redis, 501, "buy", "100")

    captured = _capture_trigger(monkeypatch, monitor)
    await monitor._evaluate_symbol_stops(SYM)
    assert captured == [(501, Decimal("100"), "buy")]


async def test_mirror_sell_side(monkeypatch, redis):
    """Sell Stop @101 inert next to resting bid@101 until a print ≤101."""
    monitor = _wire(monkeypatch, redis)

    await seed_resting_limit(redis, 33, "buy", "101")
    await seed_pending_stop(redis, 502, "sell", "101")

    captured = _capture_trigger(monkeypatch, monitor)
    await monitor._evaluate_symbol_stops(SYM)
    assert captured == []                      # bid present, no print yet

    monitor._last_price[SYM] = Decimal("101")
    await monitor._evaluate_symbol_stops(SYM)
    assert captured == [(502, Decimal("101"), "sell")]


async def test_placement_time_activation_is_price_genuine(monkeypatch, redis):
    """Buy-stop placed BELOW current last price activates on first
    evaluation (`stops_changed` pass) — instant, but price-genuine."""
    monitor = _wire(monkeypatch, redis)
    monitor._last_price[SYM] = Decimal("105")

    await seed_pending_stop(redis, 503, "buy", "100")

    captured = _capture_trigger(monkeypatch, monitor)
    await monitor._evaluate_symbol_stops(SYM)
    assert captured == [(503, Decimal("105"), "buy")]


async def test_evaluate_never_consults_live_spread(monkeypatch, redis):
    """Guard against reintroducing book-driven arming."""
    from app.redis_client import orderbook

    spread_calls: list[tuple] = []

    async def spy_get_spread(r, symbol):
        spread_calls.append((symbol,))
        raise AssertionError("get_spread must not be consulted for arming")

    monkeypatch.setattr(orderbook, "get_spread", spy_get_spread)

    monitor = _wire(monkeypatch, redis)
    await seed_resting_limit(redis, 22, "sell", "100")
    await seed_pending_stop(redis, 501, "buy", "100")

    await monitor._evaluate_symbol_stops(SYM)
    assert spread_calls == []


async def test_trade_print_drives_full_pipeline(monkeypatch, redis):
    """Print → extremes/cache update + arming via the pubsub-loop path."""
    monitor = _wire(monkeypatch, redis)

    await seed_resting_limit(redis, 22, "sell", "100")
    await seed_pending_stop(redis, 501, "buy", "100")

    captured = _capture_trigger(monkeypatch, monitor)
    await monitor._process_trade_print(SYM, Decimal("101"))

    assert monitor._last_price[SYM] == Decimal("101")
    assert monitor._local_high[SYM] == Decimal("101")
    assert len(captured) == 1
    oid, ref, side = captured[0]
    assert (oid, float(ref), side) == (501, 101.0, "buy")


# ─── Numeric re-check gate inside the real _trigger_stop_order ───────────────

def _rig_trigger(monkeypatch, redis, *, side, stop):
    """Rig module deps so escaping the gate block trips an AssertionError.

    Returns an awaitable runner for ``_trigger_stop_order(777, ref)``.
    """
    import contextlib
    from types import SimpleNamespace

    import app.services.stop_monitor_service as sms_mod
    from app.models.enums import OrderSide, OrderStatus, OrderType

    class _FakeBegin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def begin(self):
            return _FakeBegin()

    @contextlib.asynccontextmanager
    async def _fake_session_factory():
        yield _FakeSession()

    class _FakeRepo:
        def __init__(self, *a, **k):
            pass

        async def get_for_update(self, oid):
            return SimpleNamespace(
                id=oid, status=OrderStatus.PENDING, side=OrderSide(side),
                type=OrderType.STOP_MARKET, symbol=SYM,
                stop_price=Decimal(stop), quantity=Decimal("2"),
                user_id=111, version=1, price=None,
                filled_quantity=Decimal("0"), trailing_delta=None,
                parent_order_id=None, locked_amount=Decimal("0"),
            )

    class _BoomPairRepo:
        def __init__(self, *a, **k):
            pass

        async def get_by_symbol(self, symbol):
            raise AssertionError("gate must block before any conversion work")

    monkeypatch.setattr(sms_mod, "get_redis", lambda: redis)
    monkeypatch.setattr(sms_mod, "async_session_factory", _fake_session_factory)
    monkeypatch.setattr(sms_mod, "OrderRepository", _FakeRepo)
    monkeypatch.setattr(sms_mod, "TradingPairRepository", _BoomPairRepo)

    monitor = _make_monitor()

    async def run(ref: str):
        return await monitor._trigger_stop_order(
            777, Decimal(ref), expected_side=OrderSide(side),
        )

    return run


async def test_gate_blocks_buy_reference_below_stop(monkeypatch, redis):
    """BUY stop @100 with reference 95 must be rejected by the numeric
    gate — before ANY locking/conversion work happens."""
    run = _rig_trigger(monkeypatch, redis, side="buy", stop="100")
    await run("95")  # silent return = blocked


async def test_gate_passes_reference_equal_to_stop(monkeypatch, redis):
    """Equality passes the gate (flow proceeds into conversion and hits
    the rigged pair-repo boom → proves the gate itself let it through)."""
    import pytest

    run = _rig_trigger(monkeypatch, redis, side="buy", stop="100")
    with pytest.raises(AssertionError, match="gate must block"):
        await run("100")


async def test_gate_passes_reference_above_stop(monkeypatch, redis):
    """Above the stop the BUY flow likewise escapes the gate."""
    import pytest

    run = _rig_trigger(monkeypatch, redis, side="buy", stop="100")
    with pytest.raises(AssertionError, match="gate must block"):
        await run("101")
