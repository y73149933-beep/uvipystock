"""Contract tests for the trade-tape time pipeline.

Locked-down bug: the trades feed rendered clock values shifted by hours.
Root causes fixed together:

* ``pubsub.publish_trade`` sent NO timestamp — live WS prints fell back
  to garbage on the client;
* REST public trades emit epoch-ms, but the client formatter read
  seconds (unit mismatch across the two feed sources);
* WS snapshot/pong stamps used ``asyncio`` monotonic loop time instead
  of wall-clock epoch ms.

Canonical contract asserted here:

* every live print carries ``ts`` as an INTEGER in epoch MILLISECONDS;
* REST serialization (``_trade_to_public``) produces the SAME unit and
  interprets naive datetimes as UTC (SQLite doubles), aware datetimes
  keep their offset.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest_asyncio
from fakeredis.aioredis import FakeRedis

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

SYM = "BTC/USD"


class _SpyRedis(FakeRedis):
    """FakeRedis that records published channel payloads."""

    def __init__(self, *a, **k):
        super().__init__(*a, decode_responses=True, **k)
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, message, *args, **kwargs):
        self.published.append(
            (channel, message.decode() if isinstance(message, bytes) else message)
        )
        return 1


@pytest_asyncio.fixture
async def spy_redis():
    r = _SpyRedis()
    yield r
    await r.flushall()


# ─── WS live prints ──────────────────────────────────────────────────────────

async def test_publish_trade_carries_epoch_ms_ts(spy_redis):
    from app.redis_client import pubsub

    before_ms = int(time.time() * 1000) - 5000
    await pubsub.publish_trade(
        spy_redis, SYM, trade_id=42,
        price=100.5, quantity=2.0, side="buy",
        taker_order_id=1, maker_order_id=2,
    )
    after_ms = int(time.time() * 1000) + 5000

    assert len(spy_redis.published) == 1
    ch, raw = spy_redis.published[0]
    assert ch == pubsub.trades_channel(SYM)

    msg = json.loads(raw)
    assert msg["event"] == "trade"
    assert msg["trade_id"] == 42

    ts = msg["ts"]
    assert isinstance(ts, int), "ts must be an integer"
    assert ts > 10**12, "ts must be epoch MILLISECONDS, not seconds"
    assert before_ms <= ts <= after_ms


async def test_publish_trade_payload_keys_stable(spy_redis):
    """Existing consumers (worker relay, client store) rely on these keys."""
    from app.redis_client import pubsub

    await pubsub.publish_trade(spy_redis, SYM, trade_id=7,
                               price=1.0, quantity=1.0, side="sell")
    msg = json.loads(spy_redis.published[0][1])
    for key in ("symbol", "trade_id", "price", "quantity", "side", "ts"):
        assert key in msg


# ─── REST serialization ──────────────────────────────────────────────────────

class _Row:
    """Duck-typed Trade row for _trade_to_public."""

    def __init__(self, executed_at: datetime):
        self.id = 11
        self.symbol = SYM
        self.price = Decimal("100.25")
        self.quantity = Decimal("3")
        self.side = type("S", (), {"value": "buy"})()

        self.executed_at = executed_at


def _expected_ms(dt_aware: datetime) -> int:
    return int(dt_aware.timestamp() * 1000)


def test_rest_serialization_naive_datetime_treated_as_utc():
    from app.api.v1.trades import _trade_to_public

    naive = datetime(2026, 8, 27, 2, 30, 0)          # no tzinfo (sqlite-like)
    out = _trade_to_public(_Row(naive))

    expected = _expected_ms(naive.replace(tzinfo=UTC))
    assert out["ts"] == expected
    assert out["ts"] > 10**12
    assert out["trade_id"] == 11
    assert out["side"] == "buy"


def test_rest_serialization_aware_datetime_keeps_instant():
    from app.api.v1.trades import _trade_to_public

    aware = datetime(2026, 8, 27, 9, 30, 0, tzinfo=UTC)
    out = _trade_to_public(_Row(aware))
    assert out["ts"] == _expected_ms(aware)

    # Equivalent instant expressed in a non-UTC offset serializes identically.
    from datetime import timedelta as _td
    kras = timezone(_td(hours=7))
    same_moment = datetime(2026, 8, 27, 16, 30, 0, tzinfo=kras)   # == 09:30Z
    assert _trade_to_public(_Row(same_moment))["ts"] == out["ts"]
