"""Regression tests: SL/TP children activation must respect order types.

Locked-down bug: when a parent order filled, ``_activate_sltp_children``
enqueued ALL children straight into ``queue:orders`` with their raw types.
The bridge's ``_TYPE_MAP`` silently converts ``stop_market`` → C_MARKET and
``stop_limit`` → C_LIMIT, so a Stop-Loss child executed IMMEDIATELY whenever
the book had liquidity — the reported "Stop-Market/Stop-Limit fire instantly"
regression, surviving the standalone-stop fixes because standalone stops go
through the correct registration path while SL/TP children bypassed it.

Correct behavior asserted here:

* conditional children (stop_market / stop_limit) register in the stop
  queue (``stops:{symbol}``), stay PENDING, get the order HASH (the trigger
  scan reads ``side`` from it), invalidate the public ladder and NEVER
  touch the matching queue;
* plain children (TP limit) still go to the engine immediately — but now
  with the order HASH (settlement resolves maker user_id from it);
* the matching worker REFUSES stop-type payloads outright (invariant
  guard): a stop order must fire via the stop monitor, never via the
  matcher.

Infra: in-memory SQLite (aiosqlite) + fakeredis.aioredis, mirroring the
existing unit-test conventions in this package.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.models.enums import OrderSide, OrderStatus, OrderType
from app.models.order import Order
from app.redis_client import orderbook, orders_index
from app.redis_client.orderbook import SCORE_MULTIPLIER

SYM = "BTC/USDT"
QUEUE = "queue:orders"

DDL = [
    """
    CREATE TABLE trading_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(20) NOT NULL UNIQUE,
        base_asset VARCHAR(20) NOT NULL,
        quote_asset VARCHAR(20) NOT NULL,
        price_precision INTEGER NOT NULL,
        quantity_precision INTEGER NOT NULL,
        min_lot_size NUMERIC(36,18) NOT NULL,
        max_lot_size NUMERIC(36,18) NOT NULL,
        tick_size NUMERIC(36,18) NOT NULL,
        maker_fee_bps NUMERIC(10,6) DEFAULT 0 NOT NULL,
        taker_fee_bps NUMERIC(10,6) DEFAULT 0 NOT NULL,
        is_active BOOLEAN DEFAULT 1 NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        symbol VARCHAR(20) NOT NULL,
        side VARCHAR(10) NOT NULL,
        type VARCHAR(20) NOT NULL,
        status VARCHAR(20) DEFAULT 'new' NOT NULL,
        price NUMERIC(36,18),
        stop_price NUMERIC(36,18),
        trailing_delta NUMERIC(36,18),
        quantity NUMERIC(36,18) NOT NULL,
        filled_quantity NUMERIC(36,18) DEFAULT 0 NOT NULL,
        filled_quote_qty NUMERIC(36,18) DEFAULT 0 NOT NULL,
        visible_quantity NUMERIC(36,18),
        hidden_quantity NUMERIC(36,18),
        locked_amount NUMERIC(36,18) DEFAULT 0 NOT NULL,
        replace_count INTEGER DEFAULT 0 NOT NULL,
        replaces_id INTEGER,
        replaced_by_id INTEGER,
        parent_order_id INTEGER,
        sl_order_id INTEGER,
        tp_order_id INTEGER,
        bulk_id VARCHAR(36),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
        version INTEGER DEFAULT 1 NOT NULL
    )
    """,
]


@pytest_asyncio.fixture
async def redis():
    r = FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.aclose()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        for stmt in DDL:
            await conn.exec_driver_sql(stmt)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def fake_get_redis(redis, monkeypatch):
    """Route trade_service.get_redis() to the FakeRedis instance."""
    monkeypatch.setattr("app.services.trade_service.get_redis", lambda: redis)
    return redis


@pytest.fixture
def spies(monkeypatch):
    """Capture sl_tp_activated / stops_changed / order_update publications."""
    from app.redis_client import pubsub as pubsub_mod

    calls = {"sl_tp": [], "stops_changed": [], "order_update": []}

    async def spy_sl_tp(redis, **kw):
        calls["sl_tp"].append(kw)

    async def spy_stops_changed(redis, symbol, **kw):
        calls["stops_changed"].append(symbol)

    async def spy_order_update(redis, **kw):
        calls["order_update"].append(kw)

    monkeypatch.setattr(pubsub_mod, "publish_sl_tp_activated", spy_sl_tp)
    monkeypatch.setattr(pubsub_mod, "publish_stops_changed", spy_stops_changed)
    monkeypatch.setattr(pubsub_mod, "publish_order_update", spy_order_update)
    return calls


async def _make_parent_and_child(
    db,
    *,
    child_type: OrderType,
    stop_price: Decimal | None = None,
    child_price: Decimal | None = None,
    child_side: OrderSide = OrderSide.SELL,
) -> tuple[int, int]:
    """Insert a FILLED limit-buy parent with one PENDING SL/TP child."""
    async with db() as s:
        async with s.begin():
            parent = Order(
                user_id=7, symbol=SYM, side=OrderSide.BUY, type=OrderType.LIMIT,
                status=OrderStatus.FILLED, price=Decimal("100"),
                quantity=Decimal("1"), visible_quantity=Decimal("1"),
                locked_amount=Decimal("100"),
            )
            s.add(parent)
            await s.flush()
            child = Order(
                user_id=7, symbol=SYM, side=child_side, type=child_type,
                status=OrderStatus.PENDING, price=child_price,
                stop_price=stop_price, quantity=Decimal("1"),
                parent_order_id=parent.id,
            )
            s.add(child)
            await s.flush()
            parent.sl_order_id = child.id
            return parent.id, child.id


async def _reload(db, order_id: int) -> Order:
    async with db() as s:
        return await s.get(Order, order_id)


async def _run_activation(db, parent_id: int):
    from app.services.trade_service import TradeService

    async with db() as s:
        parent = await s.get(Order, parent_id)
        svc = TradeService(s)
        await svc._activate_sltp_children(parent)
        await s.commit()


# ─── Conditional children: register in the stop queue, NOT the engine ───────

async def test_stop_market_child_registers_in_stop_queue(db, fake_get_redis, spies):
    parent_id, child_id = await _make_parent_and_child(
        db, child_type=OrderType.STOP_MARKET, stop_price=Decimal("90"),
    )

    await _run_activation(db, parent_id)

    child = await _reload(db, child_id)
    assert child.status == OrderStatus.PENDING  # waiting for the trigger

    # Stop queue membership with the trigger price as score
    score = await fake_get_redis.zscore("stops:" + SYM, str(child_id))
    assert score == pytest.approx(90.0 * SCORE_MULTIPLIER)

    # HASH present: the trigger scan reads `side` from it (no HASH → phantom)
    h = await orderbook.get_order_hash(fake_get_redis, child_id)
    assert h["side"] == "sell"
    assert h["type"] == "stop_market"
    assert Decimal(h["stop_price"]) == Decimal("90")

    # NEVER enqueued to the matching engine
    assert await fake_get_redis.llen(QUEUE) == 0

    # Index + notifications
    assert child_id in await orders_index.get_symbol_open_orders(fake_get_redis, SYM)
    assert len(spies["sl_tp"]) == 1
    assert spies["stops_changed"] == [SYM]


async def test_stop_limit_child_keeps_trigger_and_limit(db, fake_get_redis, spies):
    parent_id, child_id = await _make_parent_and_child(
        db, child_type=OrderType.STOP_LIMIT,
        stop_price=Decimal("90"), child_price=Decimal("89.5"),
    )

    await _run_activation(db, parent_id)

    child = await _reload(db, child_id)
    assert child.status == OrderStatus.PENDING

    score = await fake_get_redis.zscore("stops:" + SYM, str(child_id))
    assert score == pytest.approx(90.0 * SCORE_MULTIPLIER)

    h = await orderbook.get_order_hash(fake_get_redis, child_id)
    assert h["type"] == "stop_limit"
    assert Decimal(h["stop_price"]) == Decimal("90")
    assert Decimal(h["price"]) == Decimal("89.5")

    assert await fake_get_redis.llen(QUEUE) == 0
    assert len(spies["sl_tp"]) == 1


async def test_plain_limit_child_still_goes_to_engine(db, fake_get_redis, spies):
    """TP limit child: immediate engine route preserved (regression guard),
    now WITH the order HASH."""
    parent_id, child_id = await _make_parent_and_child(
        db, child_type=OrderType.LIMIT, child_price=Decimal("110"),
        child_side=OrderSide.SELL,
    )

    await _run_activation(db, parent_id)

    child = await _reload(db, child_id)
    assert child.status == OrderStatus.NEW

    # Enqueued exactly once for the engine, NOT in the stop queue
    assert await fake_get_redis.llen(QUEUE) == 1
    assert await fake_get_redis.zcard("stops:" + SYM) == 0

    h = await orderbook.get_order_hash(fake_get_redis, child_id)
    assert h["side"] == "sell"
    assert Decimal(h["price"]) == Decimal("110")
    assert child_id in await orders_index.get_symbol_open_orders(fake_get_redis, SYM)

    assert len(spies["sl_tp"]) == 1
    assert spies["stops_changed"] == []  # book ladder unaffected


async def test_mixed_children_branch_independently(db, fake_get_redis, spies):
    """A bracket order (SL stop + TP limit) activates both ways in one pass."""
    async with db() as s:
        async with s.begin():
            parent = Order(
                user_id=7, symbol=SYM, side=OrderSide.BUY, type=OrderType.LIMIT,
                status=OrderStatus.FILLED, price=Decimal("100"),
                quantity=Decimal("1"), visible_quantity=Decimal("1"),
                locked_amount=Decimal("100"),
            )
            s.add(parent)
            await s.flush()
            sl_child = Order(
                user_id=7, symbol=SYM, side=OrderSide.SELL,
                type=OrderType.STOP_MARKET, status=OrderStatus.PENDING,
                stop_price=Decimal("90"), quantity=Decimal("1"),
                parent_order_id=parent.id,
            )
            tp_child = Order(
                user_id=7, symbol=SYM, side=OrderSide.SELL,
                type=OrderType.LIMIT, status=OrderStatus.PENDING,
                price=Decimal("110"), quantity=Decimal("1"),
                parent_order_id=parent.id,
            )
            s.add(sl_child)
            s.add(tp_child)
            await s.flush()
            parent_id, sl_id, tp_id = parent.id, sl_child.id, tp_child.id

    await _run_activation(db, parent_id)

    sl = await _reload(db, sl_id)
    tp = await _reload(db, tp_id)
    assert sl.status == OrderStatus.PENDING
    assert tp.status == OrderStatus.NEW

    assert await fake_get_redis.zscore("stops:" + SYM, str(sl_id)) is not None
    assert await fake_get_redis.zscore("stops:" + SYM, str(tp_id)) is None
    assert await fake_get_redis.llen(QUEUE) == 1

    assert len(spies["sl_tp"]) == 2
    assert spies["stops_changed"] == [SYM]


# ─── Worker invariant guard: stop types never reach the matcher ─────────────

def _stop_payload(otype: str) -> dict:
    from app.matching.worker import OrderActionPayload

    return OrderActionPayload(
        action="place", order_id=42, symbol=SYM, user_id=7,
        side="sell", type=otype, price=None, stop_price=None,
        quantity=1.0, is_iceberg=False, visible_qty=None,
        hidden_qty=None, parent_order_id=None, replaces_id=None,
        bulk_id=None, ts=0,
    )


async def test_worker_refuses_stop_market_payload(redis, spies):
    from app.matching.worker import MatchingWorker

    worker = MatchingWorker()
    worker._redis = redis

    result = await worker.process_action(_stop_payload("stop_market"))

    assert result["outcome"] == "refused_stop_type"
    assert result["rejected"] is True
    assert result["trades"] == []

    # Loud rejection event, nothing enqueued behind the refusal
    assert len(spies["order_update"]) == 1
    assert spies["order_update"][0]["event"] == "rejected"
    assert spies["order_update"][0]["type"] == "stop_market"
    assert await redis.llen(QUEUE) == 0


async def test_worker_refuses_stop_limit_payload(redis, spies):
    from app.matching.worker import MatchingWorker

    worker = MatchingWorker()
    worker._redis = redis

    result = await worker.process_action(_stop_payload("stop_limit"))

    assert result["outcome"] == "refused_stop_type"
    assert result["rejected"] is True
    assert len(spies["order_update"]) == 1
    assert spies["order_update"][0]["event"] == "rejected"


async def test_worker_guard_does_not_consume_liquidity(redis, spies):
    """Even with a FULL book of resting asks, a stop-type payload must not
    produce a single trade — the exact reported symptom."""
    from app.matching.worker import MatchingWorker

    # Seed the book: 5 asks @ 50 with deep volume (a stop_market buy @100
    # mapped to MARKET would eat all of this instantly)
    await orderbook.store_order_hash(redis, 900, {
        "order_id": "900", "user_id": "999", "symbol": SYM,
        "side": "sell", "type": "limit", "price": "50",
        "quantity": "10", "is_iceberg": "0",
    })
    await redis.zadd(orderbook.asks_key(SYM),
                     {"900": orderbook.encode_score(Decimal("50"), 1)})

    worker = MatchingWorker()
    worker._redis = redis
    payload = _stop_payload("stop_market")
    payload.side = "buy"

    result = await worker.process_action(payload)

    assert result["outcome"] == "refused_stop_type"
    assert result["trades"] == []
    assert await redis.llen(QUEUE) == 0
    # The resting ask is untouched
    assert await redis.zcard(orderbook.asks_key(SYM)) == 1
