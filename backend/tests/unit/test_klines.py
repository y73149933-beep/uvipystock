"""Unit tests for the OHLCV kline cache (app.redis_client.klines).

Pure folding logic + Redis-backed cache behavior via fakeredis. The PG
backfill (aggregate_window/ensure_window) needs a real session and is
covered by the e2e harness; the cache-coverage short-circuit of
ensure_window is tested here (it must not touch the DB when the window
is already materialized).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.redis_client import klines


@pytest_asyncio.fixture
async def redis():
    r = FakeRedis()
    yield r
    await r.aclose()


def test_bucket_of_aligns_to_tf():
    assert klines.bucket_of(1234, 60) == 1200
    assert klines.bucket_of(1259, 60) == 1200
    assert klines.bucket_of(1260, 60) == 1260
    assert klines.bucket_of(1234, 3600) == 0
    assert klines.bucket_of(1794313699, 900) == 1794312900


def test_bump_new_and_fold():
    c = klines.bump(None, 100.0, 1.0)
    assert c == {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}
    c = klines.bump(c, 95.0, 2.0)   # low/close move
    assert c == {"open": 100.0, "high": 100.0, "low": 95.0, "close": 95.0, "volume": 3.0}
    c = klines.bump(c, 105.0, 0.5)  # high/close move
    assert c == {"open": 100.0, "high": 105.0, "low": 95.0, "close": 105.0, "volume": 3.5}


@pytest.mark.asyncio
async def test_apply_trades_merges_within_bucket(redis):
    base = 1_794_313_500  # aligned to 900s too? use 1m
    base = base - (base % 60)
    await klines.apply_trades(redis, "BTC/USD", [
        (100.0, 1.0, base),
        (95.0, 2.0, base + 5),
        (97.0, 0.5, base + 59),
    ])
    members = await redis.zrangebyscore(klines.kline_key("BTC/USD", "1m"), base, base)
    assert len(members) == 1  # one member per bucket, old ones replaced
    candle = json.loads(members[0])
    assert candle["t"] == base
    assert candle["open"] == 100.0 and candle["close"] == 97.0
    assert candle["high"] == 100.0 and candle["low"] == 95.0
    assert abs(candle["volume"] - 3.5) < 1e-12


@pytest.mark.asyncio
async def test_apply_trades_spreads_across_timeframes(redis):
    base = 1_794_313_500 - (1_794_313_500 % 3600)
    await klines.apply_trades(redis, "BTC/USD", [(50.0, 1.0, base + 10)])
    for tf in klines.TF_SECONDS:
        tfs = klines.TF_SECONDS[tf]
        bucket = (base + 10) // tfs * tfs
        members = await redis.zrangebyscore(klines.kline_key("BTC/USD", tf), bucket, bucket)
        assert len(members) == 1, f"{tf} bucket not written"


@pytest.mark.asyncio
async def test_get_range_window_and_order(redis):
    base = 6000
    await klines.apply_trades(redis, "BTC/USD", [(10.0, 1.0, base + s) for s in (0, 120, 180)])
    # [6000..6180] ascending
    got = await klines.get_range(redis, "BTC/USD", "1m", 6000, 6180, 10)
    # the idle 6060 bucket is filled with a synthetic flat bar
    assert [c["time"] for c in got] == [6000, 6060, 6120, 6180]
    flat = got[1]
    assert flat["volume"] == 0.0 and flat["open"] == flat["close"] == 10.0
    # exclusive upper bound at 6120 keeps it out; trailing 6060 is flat-filled
    got = await klines.get_range(redis, "BTC/USD", "1m", 6000, 6060, 10)
    assert [c["time"] for c in got] == [6000, 6060]
    assert got[1]["volume"] == 0.0
    # limit keeps the most recent
    got = await klines.get_range(redis, "BTC/USD", "1m", 6000, 6180, 2)
    assert [c["time"] for c in got] == [6120, 6180]


@pytest.mark.asyncio
async def test_ensure_window_skips_db_when_covered(redis):
    """A window at/after the seeded floor must not hit the DB (session
    would raise if touched)."""
    await redis.set(klines.seeded_key("BTC/USD", "1m"), "6000")
    await klines.ensure_window(None, redis, "BTC/USD", "1m", 6060, 6240)  # type: ignore[arg-type]
    # still one sentinel, untouched
    assert int(await redis.get(klines.seeded_key("BTC/USD", "1m"))) == 6000


@pytest.mark.asyncio
async def test_trim_keeps_recent_buckets(redis, monkeypatch):
    monkeypatch.setattr(klines, "MAX_CANDLES_PER_TF", 3)
    base = 6000
    for i in range(6):
        await klines.apply_trades(redis, "BTC/USD", [(10.0, 1.0, base + i * 60)])
    n = await redis.zcard(klines.kline_key("BTC/USD", "1m"))
    assert n == 3
    got = await klines.get_range(redis, "BTC/USD", "1m", 6000, base + 300, 100)
    assert [c["time"] for c in got] == [base + 180, base + 240, base + 300]


# ---------- fill_gaps (synthetic idle bars) ----------

def test_fill_gaps_middle_and_trailing():
    tf = 60
    c = [
        {"time": 600, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        {"time": 900, "open": 10.5, "high": 12, "low": 10, "close": 11, "volume": 2},
    ]
    out = klines.fill_gaps(c, tf, 600, 1020, None)
    times = [x["time"] for x in out]
    assert times == [600, 660, 720, 780, 840, 900, 960, 1020]
    mid = [x for x in out if x["volume"] == 0.0]
    assert len(mid) == 6  # 4 middle + 2 trailing
    # middle bars carry the previous real close (10.5), trailing bars the
    # last real close (11.0)
    assert all(x["open"] == x["close"] == 10.5 for x in mid[:4])
    assert all(x["open"] == x["close"] == 11.0 for x in mid[4:])


def test_fill_gaps_no_invention_before_first_trade():
    tf = 60
    c = [{"time": 600, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1}]
    out = klines.fill_gaps(c, tf, 300, 720, None)
    assert [x["time"] for x in out] == [600, 660, 720]


def test_fill_gaps_leading_only_with_seed():
    tf = 60
    c = [{"time": 480, "open": 12, "high": 12, "low": 12, "close": 12, "volume": 1}]
    out = klines.fill_gaps(c, tf, 300, 480, 11.0)
    assert [x["time"] for x in out] == [300, 360, 420, 480]
    assert out[0]["close"] == 11.0 and out[0]["volume"] == 0.0


def test_fill_gaps_empty_page_with_seed_is_all_flat():
    out = klines.fill_gaps([], 60, 600, 780, 9.0)
    assert [x["time"] for x in out] == [600, 660, 720, 780]
    assert all(x["volume"] == 0.0 and x["close"] == 9.0 for x in out)


def test_fill_gaps_empty_page_no_seed_stays_empty():
    assert klines.fill_gaps([], 60, 600, 780, None) == []
