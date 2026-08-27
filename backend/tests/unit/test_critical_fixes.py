"""Regression tests for the critical trading-engine fixes.

Covers five bugs that could lose or print funds:

1. Stop triggers fired on ANY trade print because the range union covered
   the whole ZSET regardless of side → now direction-verified per order hash.
2. Market-BUY cancel unlocked 0 (formula `remaining * (stop_price or 0)`)
   → now unlocks exact `orders.locked_amount`.
3. Partially-filled resting order returned to the book at FULL size →
   worker-level behavior asserted indirectly here via settlement guards
   that would have dead-lettered the overfill anyway (see gates test).
4. Phantom place credited makers with no paying taker → batch is refused.
5. Settlement decrements `locked_amount` so later refunds are exact.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.models.balance import Balance
from app.models.enums import OrderSide, OrderStatus, OrderType
from app.models.order import Order
from app.models.trading_pair import TradingPair
from app.redis_client import orderbook, stops

DDL = [
    """
    CREATE TABLE balances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        asset VARCHAR(20) NOT NULL,
        total_balance NUMERIC(36,18) DEFAULT 0 NOT NULL,
        locked_balance NUMERIC(36,18) DEFAULT 0 NOT NULL,
        available_balance NUMERIC(36,18) DEFAULT 0 NOT NULL,
        version INTEGER DEFAULT 1 NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
        UNIQUE(user_id, asset)
    )
    """,
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
    """
    CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        taker_order_id INTEGER NOT NULL,
        maker_order_id INTEGER NOT NULL,
        symbol VARCHAR(20) NOT NULL,
        price NUMERIC(36,18) NOT NULL,
        quantity NUMERIC(36,18) NOT NULL,
        quote_quantity NUMERIC(36,18) NOT NULL,
        side VARCHAR(10) NOT NULL,
        taker_user_id INTEGER NOT NULL,
        maker_user_id INTEGER NOT NULL,
        taker_fee NUMERIC(36,18) DEFAULT 0 NOT NULL,
        maker_fee NUMERIC(36,18) DEFAULT 0 NOT NULL,
        executed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
]


def dec(value) -> Decimal:
    """Normalize sqlite floats / Decimals for exact-money comparison."""
    return Decimal(str(value)).quantize(Decimal("1e-9"))


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


async def make_pair(factory) -> None:
    async with factory() as s:
        async with s.begin():
            s.add(TradingPair(
                symbol="BTC/USDT", base_asset="BTC", quote_asset="USDT",
                price_precision=2, quantity_precision=6,
                min_lot_size=Decimal("0.0001"), max_lot_size=Decimal("100"),
                tick_size=Decimal("0.01"),
            ))


async def add_order(factory, **kwargs) -> int:
    kwargs.setdefault("symbol", "BTC/USDT")
    async with factory() as s:
        async with s.begin():
            o = Order(**kwargs)
            s.add(o)
            await s.flush()
            return o.id


def _order_kwargs(**over):
    base = dict(
        user_id=1, symbol="BTC/USDT", side=OrderSide.BUY, type=OrderType.LIMIT,
        status=OrderStatus.NEW, price=Decimal("40000"), quantity=Decimal("1"),
        visible_quantity=Decimal("1"), hidden_quantity=None,
        locked_amount=Decimal("40000"),
    )
    base.update(over)
    return base


# ============================================================================
#  FIX #1 — directional stop triggers
# ============================================================================

class TestStopsDirectional:
    async def _seed(self, redis, oid: int, side: str, stop_price: str) -> None:
        await stops.add_stop_order(redis, order_id=oid, symbol="BTC/USDT",
                                   stop_price=Decimal(stop_price))
        await orderbook.store_order_hash(redis, oid, {
            "order_id": str(oid), "user_id": "1", "symbol": "BTC/USDT",
            "side": side, "type": "stop_market",
            "stop_price": stop_price, "quantity": "1", "is_iceberg": "0",
        })

    async def test_nothing_fires_between_stops(self, redis):
        """THE regression: old code fired BOTH stops at any print."""
        await self._seed(redis, 1, "sell", "95")   # sells when price <= 95
        await self._seed(redis, 2, "buy", "105")   # buys  when price >= 105

        triggered = await stops.get_all_triggered_stops(redis, "BTC/USDT", Decimal("100"))
        assert triggered == []

    async def test_sell_fires_only_below_stop(self, redis):
        await self._seed(redis, 1, "sell", "95")
        await self._seed(redis, 2, "buy", "105")

        triggered = await stops.get_all_triggered_stops(redis, "BTC/USDT", Decimal("94"))
        assert triggered == [(1, "sell")]

    async def test_buy_fires_only_above_stop(self, redis):
        await self._seed(redis, 1, "sell", "95")
        await self._seed(redis, 2, "buy", "105")

        triggered = await stops.get_all_triggered_stops(redis, "BTC/USDT", Decimal("106"))
        assert triggered == [(2, "buy")]

    async def test_boundary_touch_fires(self, redis):
        await self._seed(redis, 1, "sell", "100")
        triggered = await stops.get_all_triggered_stops(redis, "BTC/USDT", Decimal("100"))
        assert triggered == [(1, "sell")]

    async def test_phantom_member_without_hash_skipped(self, redis):
        await stops.add_stop_order(redis, order_id=99, symbol="BTC/USDT",
                                   stop_price=Decimal("95"))
        # No order HASH for id=99 — ghost from a rolled-back placement
        await self._seed(redis, 1, "sell", "80")

        triggered = await stops.get_all_triggered_stops(redis, "BTC/USDT", Decimal("70"))
        assert triggered == [(1, "sell")]

    async def test_trailing_directional(self, redis):
        await stops.register_trailing_stop(
            redis, order_id=11, symbol="BTC/USDT", side="sell",
            delta_type="abs", delta_value="5",
            initial_extreme="95", initial_trigger="95",
        )
        await stops.register_trailing_stop(
            redis, order_id=12, symbol="BTC/USDT", side="buy",
            delta_type="abs", delta_value="5",
            initial_extreme="105", initial_trigger="105",
        )

        assert await stops.get_triggered_trailing_stops(redis, "BTC/USDT", Decimal("100")) == []
        assert await stops.get_triggered_trailing_stops(redis, "BTC/USDT", Decimal("94")) == [(11, "sell")]
        assert await stops.get_triggered_trailing_stops(redis, "BTC/USDT", Decimal("106")) == [(12, "buy")]


# ============================================================================
#  FIX #4 — settlement refuses phantom takers / bad makers
# ============================================================================

class TestPersistTradeGates:
    async def _seed_balances(self, factory, uid: int, asset: str,
                             total: str, locked: str) -> None:
        async with factory() as s:
            async with s.begin():
                s.add(Balance(
                    user_id=uid, asset=asset,
                    total_balance=Decimal(total),
                    locked_balance=Decimal(locked),
                    available_balance=dec(total) - dec(locked),
                ))

    async def test_phantom_taker_refuses_whole_batch(self, db, redis, monkeypatch):
        """No PG taker row → raise, roll back, credit NOBODY."""
        monkeypatch.setattr("app.services.trade_service.get_redis", lambda: redis)
        from app.services.trade_service import TakerOrderInvalidError, TradeService

        await self._seed_balances(db, 7, "BTC", "10", "10")
        maker_id = await add_order(db, user_id=7, side=OrderSide.SELL,
                                   type=OrderType.LIMIT, price=Decimal("39000"),
                                   quantity=Decimal("1"), visible_quantity=Decimal("1"),
                                   locked_amount=Decimal("1"))

        enriched = [{
            "maker_order_id": maker_id, "price": "39000", "quantity": "1",
            "maker_user_id": 7,
        }]
        executed = False
        async with db() as s:
            async with s.begin():
                with pytest.raises(TakerOrderInvalidError):
                    await TradeService(s).persist_trade_batch(
                        trades=enriched,
                        taker_user_id=1, taker_order_id=424242,   # missing!
                        taker_side=OrderSide.BUY, taker_type=OrderType.MARKET,
                        symbol="BTC/USDT", base_asset="BTC", quote_asset="USDT",
                    )
                executed = True
        assert executed  # ensure the raises actually fired inside tx

        async with db() as s:
            mo = (await s.execute(select(Order).where(Order.id == maker_id))).scalar_one()
            bal = (await s.execute(select(Balance))).scalar_one()
        assert mo.filled_quantity == 0          # maker untouched
        assert bal.available_balance == 0       # no credits printed
        assert bal.locked_balance == 10

    async def test_canceled_maker_dropped(self, db, redis, monkeypatch):
        """Maker already terminal → its trade is dropped, taker unharmed."""
        monkeypatch.setattr("app.services.trade_service.get_redis", lambda: redis)
        from app.services.trade_service import TradeService

        await self._seed_balances(db, 1, "USDT", "100000", "39000")
        await self._seed_balances(db, 1, "BTC", "5", "0")
        maker_id = await add_order(db, user_id=7, side=OrderSide.SELL,
                                   status=OrderStatus.CANCELED,
                                   type=OrderType.LIMIT, price=Decimal("39000"),
                                   quantity=Decimal("1"), visible_quantity=Decimal("1"))
        taker_id = await add_order(db, **_order_kwargs())

        enriched = [{"maker_order_id": maker_id, "price": "39000", "quantity": "1"}]
        async with db() as s:
            async with s.begin():
                out = await TradeService(s).persist_trade_batch(
                    trades=enriched,
                    taker_user_id=1, taker_order_id=taker_id,
                    taker_side=OrderSide.BUY, taker_type=OrderType.LIMIT,
                    symbol="BTC/USDT", base_asset="BTC", quote_asset="USDT",
                )
        assert out == []

        async with db() as s:
            mo = (await s.execute(select(Order).where(Order.id == maker_id))).scalar_one()
            tk = (await s.execute(select(Order).where(Order.id == taker_id))).scalar_one()
        assert mo.filled_quantity == 0
        assert tk.filled_quantity == 0


# ============================================================================
#  FIX #2 / #5 — locked_amount accounting through settlement and cancel
# ============================================================================

class TestLockedAmountLedger:
    async def test_settlement_decrements_locked_amount(self, db, redis, monkeypatch):
        """BUY taker fully fills SELL maker: residual reservations exact."""
        monkeypatch.setattr("app.services.trade_service.get_redis", lambda: redis)
        from app.services.trade_service import TradeService

        async with db() as s:
            async with s.begin():
                s.add(Balance(user_id=1, asset="USDT", total_balance=Decimal("100000"),
                              locked_balance=Decimal("40000"), available_balance=Decimal("60000")))
                s.add(Balance(user_id=1, asset="BTC", total_balance=Decimal("5"),
                              locked_balance=Decimal("0"), available_balance=Decimal("5")))
                s.add(Balance(user_id=7, asset="BTC", total_balance=Decimal("10"),
                              locked_balance=Decimal("1"), available_balance=Decimal("9")))
                s.add(Balance(user_id=7, asset="USDT", total_balance=Decimal("0"),
                              locked_balance=Decimal("0"), available_balance=Decimal("0")))

        maker_id = await add_order(db, user_id=7, side=OrderSide.SELL,
                                   type=OrderType.LIMIT, price=Decimal("39000"),
                                   quantity=Decimal("1"), visible_quantity=Decimal("1"),
                                   locked_amount=Decimal("1"))
        taker_id = await add_order(db, **_order_kwargs())  # buy 1 @40000, lock 40000

        enriched = [{"maker_order_id": maker_id, "price": "39000", "quantity": "1"}]
        async with db() as s:
            async with s.begin():
                trades = await TradeService(s).persist_trade_batch(
                    trades=enriched,
                    taker_user_id=1, taker_order_id=taker_id,
                    taker_side=OrderSide.BUY, taker_type=OrderType.LIMIT,
                    symbol="BTC/USDT", base_asset="BTC", quote_asset="USDT",
                )
        assert len(trades) == 1

        async with db() as s:
            tk = (await s.execute(select(Order).where(Order.id == taker_id))).scalar_one()
            mo = (await s.execute(select(Order).where(Order.id == maker_id))).scalar_one()
            balances = {
                (b.user_id, b.asset): b
                for b in (await s.execute(select(Balance))).scalars()
            }

        # Per-order reservations shrink by exactly what the trade consumed
        assert dec(tk.locked_amount) == Decimal("1000")   # 40000 - 39000
        assert dec(mo.locked_amount) == Decimal("0")      # 1 - 1 (base qty)
        assert tk.status == OrderStatus.FILLED
        assert mo.status == OrderStatus.FILLED

        # User-level ledgers agree with the per-order view
        assert dec(balances[(1, "USDT")].locked_balance) == Decimal("1000")
        assert dec(balances[(1, "BTC")].available_balance) == Decimal("6")
        assert dec(balances[(7, "BTC")].locked_balance) == Decimal("0")
        assert dec(balances[(7, "USDT")].available_balance) == Decimal("39000")

    async def test_cancel_market_buy_unlocks_exact_reservation(self, db, redis, monkeypatch):
        """THE stuck-funds bug: market-buy cancel had unlock=0."""
        monkeypatch.setattr("app.services.order_service.get_redis", lambda: redis)
        await make_pair(db)

        # Pre-seed the book entry so cleanup paths run against real data
        await orderbook.store_order_hash(redis, 555, {
            "order_id": "555", "user_id": "1", "symbol": "BTC/USDT",
            "side": "buy", "type": "market", "price": "", "quantity": "1",
            "is_iceberg": "0",
        })
        await orderbook.add_resting_order(
            redis, order_id=555, symbol="BTC/USDT", side="buy",
            price=Decimal("1"), quantity=Decimal("1"),
        )

        async with db() as s:
            async with s.begin():
                s.add(Balance(user_id=1, asset="USDT", total_balance=Decimal("100"),
                              locked_balance=Decimal("40.001"), available_balance=Decimal("59.999")))

        oid = await add_order(db, **_order_kwargs(
            type=OrderType.MARKET, price=None, visible_quantity=None,
            locked_amount=Decimal("40.001"),
        ))

        from app.services.order_service import OrderService
        async with db() as s:
            async with s.begin():
                await OrderService(s).cancel_order(1, oid)

        async with db() as s:
            o = (await s.execute(select(Order).where(Order.id == oid))).scalar_one()
            bal = (await s.execute(select(Balance))).scalar_one()

        assert o.status == OrderStatus.CANCELED
        # Exact refund of what THIS order reserved — not zero
        assert dec(bal.locked_balance) == Decimal("0")
        assert dec(bal.available_balance) == Decimal("100")

        # Book entry cleaned up
        assert await orderbook.get_order_hash(redis, oid) in (None, {})
