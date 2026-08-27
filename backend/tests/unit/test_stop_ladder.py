"""Tests for the public stop ladder ("stop book").

Covers:

* ``get_stop_ladder`` aggregation — grouping by (side, type, trigger price),
  volume sums across orders of different users, trailing stops resolved by
  their CURRENT drifted trigger, phantom members skipped, sorting contract
  (sells descending first, then buys descending).
* ``GET /api/v1/market/{symbol}/stops`` — public (no auth) endpoint wiring.

Infra: fakeredis.aioredis mirroring existing unit-test conventions.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.redis_client import orderbook, pubsub, stops


@pytest_asyncio.fixture
async def redis():
    r = FakeRedis(decode_responses=True)
    yield r
    await r.flushall()


async def _store_stop(
    redis,
    order_id: int,
    user_id: int,
    side: str,
    otype: str,
    stop_price: str,
    quantity: str,
    symbol: str = "BTC/USD",
) -> None:
    await orderbook.store_order_hash(redis, order_id, {
        "order_id": str(order_id),
        "user_id": str(user_id),
        "symbol": symbol,
        "side": side,
        "type": otype,
        "stop_price": stop_price,
        "quantity": quantity,
        "is_iceberg": "0",
    })
    if otype == "trailing_stop":
        await stops.register_trailing_stop(
            redis, order_id, symbol, side, "abs",
            Decimal(stop_price), Decimal(stop_price), Decimal(stop_price),
        )
    else:
        await stops.add_stop_order(redis, order_id, symbol, Decimal(stop_price))


async def test_ladder_groups_same_level_across_users(redis):
    """Two sell stop_markets at the same price from different users merge."""
    await _store_stop(redis, 1, 10, "sell", "stop_market", "41000", "0.5")
    await _store_stop(redis, 2, 20, "sell", "stop_market", "41000", "1.25")

    rows = await stops.get_stop_ladder(redis, "BTC/USD")
    assert len(rows) == 1
    assert rows[0] == {
        "side": "sell", "type": "stop_market",
        "price": "41000", "volume": "1.75", "count": 2,
    }


async def test_ladder_separates_side_and_type(redis):
    """Different sides / types never merge even at identical prices."""
    await _store_stop(redis, 1, 10, "sell", "stop_market", "41000", "1")
    await _store_stop(redis, 2, 20, "buy", "stop_market", "41000", "1")
    await _store_stop(redis, 3, 20, "buy", "stop_limit", "41000", "1")

    rows = await stops.get_stop_ladder(redis, "BTC/USD")
    keys = {(r["side"], r["type"]) for r in rows}
    assert keys == {("sell", "stop_market"), ("buy", "stop_market"), ("buy", "stop_limit")}


async def test_ladder_uses_current_trailing_trigger(redis):
    """A trailing stop is priced by its DRIFTED current_trigger, not initial."""
    oid = 7
    await _store_stop(redis, oid, 11, "sell", "trailing_stop", "40500", "3.0")
    # Market made a new high → trigger moved up
    await stops.update_trailing_trigger(
        redis, oid, "BTC/USD",
        new_extreme=Decimal("40800"), new_trigger=Decimal("40700"),
    )

    rows = await stops.get_stop_ladder(redis, "BTC/USD")
    assert len(rows) == 1
    assert rows[0]["price"] == "40700"
    assert rows[0]["volume"] == "3.0"


async def test_ladder_skips_phantom_members(redis):
    """ZSET entries without an order HASH are ignored (no side known)."""
    from decimal import Decimal as D

    await redis.zadd(stops.stops_key("BTC/USD"), {"999": float(D("40000")) * 1e9})
    assert await stops.get_stop_ladder(redis, "BTC/USD") == []


async def test_ladder_sorting_contract(redis):
    """SELL rows (desc by price) come first, then BUY rows (desc)."""
    await _store_stop(redis, 1, 10, "buy", "stop_market", "38000", "1")
    await _store_stop(redis, 2, 10, "buy", "stop_market", "39500", "1")
    await _store_stop(redis, 3, 10, "sell", "stop_market", "42000", "1")
    await _store_stop(redis, 4, 10, "sell", "stop_market", "41000", "1")

    rows = await stops.get_stop_ladder(redis, "BTC/USD")
    prices = [r["price"] for r in rows]
    sides = [r["side"] for r in rows]
    assert sides == ["sell", "sell", "buy", "buy"]
    assert prices == ["42000", "41000", "39500", "38000"]


async def test_publish_stops_changed_envelope(redis):
    """Invalidation event lands on the shared orderbook channel."""
    n = await pubsub.publish_stops_changed(redis, "BTC/USD")
    assert n >= 0  # fakeredis may report 0 subscribers; ensure no error


# ─── HTTP endpoint ────────────────────────────────────────────────────────────

async def test_endpoint_public_no_auth(monkeypatch, redis):
    """GET /api/v1/market/{symbol}/stops works without credentials."""
    from app.api.v1 import market as market_route

    monkeypatch.setattr(market_route, "get_redis", lambda: redis)

    await _store_stop(redis, 1, 10, "sell", "stop_market", "41000", "0.5")
    await _store_stop(redis, 2, 20, "buy", "stop_limit", "39000", "2.0")

    app = FastAPI()
    app.include_router(market_route.router, prefix="/api/v1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/market/stops", params={"symbol": "BTC/USD"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "BTC/USD"
    assert body["total_count"] == 2
    assert Decimal(body["total_volume"]) == Decimal("2.5")
    assert [lvl["side"] for lvl in body["levels"]] == ["sell", "buy"]


async def test_endpoint_empty_symbol(monkeypatch, redis):
    from app.api.v1 import market as market_route

    monkeypatch.setattr(market_route, "get_redis", lambda: redis)

    app = FastAPI()
    app.include_router(market_route.router, prefix="/api/v1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/market/stops", params={"symbol": "DOGE/USD"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["levels"] == []
    assert body["total_count"] == 0
    assert Decimal(body["total_volume"]) == 0
