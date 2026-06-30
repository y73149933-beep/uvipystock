"""Unit tests for the Redis client modules.

Uses `fakeredis.aioredis` for an in-memory async Redis that supports
all the operations we use (ZSET, HASH, LIST, SET, PUBSUB, INCR, etc.).

Coverage
--------
1. orderbook: score encoding, add/remove resting orders, snapshot, best bid/ask
2. stops: add/remove stop, query triggered by price, trailing state updates
3. queues: LPUSH/BRPOP round-trip, deadletter, payload builder
4. pubsub: publish + subscribe round-trip for all 5 channels
5. orders_index: add/remove, count, cancel-all, per-symbol queries
6. rate_limit: allow within limit, reject over limit, sliding window eviction
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

# Ensure backend/ on path
_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.models.enums import OrderSide
from app.redis_client import orderbook, queues, pubsub, orders_index, rate_limit, stops


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def redis():
    """Fresh FakeRedis per test."""
    r = FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.aclose()


# ============================================================================
#  1. ORDERBOOK
# ============================================================================

class TestOrderbook:
    """Tests for app.redis_client.orderbook."""

    async def test_score_encoding_decoding_roundtrip(self):
        """encode_score → decode_score preserves price and seq."""
        score = orderbook.encode_score(Decimal("42150.50"), 12345)
        price, seq = orderbook.decode_score(score)
        assert price == 42150.5
        assert seq == 12345

    async def test_score_encoding_rejects_negative_price(self):
        with pytest.raises(ValueError, match="non-negative"):
            orderbook.encode_score(Decimal("-1"), 0)

    async def test_score_encoding_rejects_huge_price(self):
        with pytest.raises(ValueError, match="too large"):
            orderbook.encode_score(Decimal("1e10"), 0)

    async def test_score_encoding_rejects_out_of_range_seq(self):
        with pytest.raises(ValueError, match="seq must be"):
            orderbook.encode_score(100.0, orderbook.SCORE_MULTIPLIER)

    async def test_side_key_helpers(self):
        assert orderbook.side_key("BTC/USDT", OrderSide.BUY) == "ob:BTC/USDT:bids"
        assert orderbook.side_key("BTC/USDT", OrderSide.SELL) == "ob:BTC/USDT:asks"
        assert orderbook.side_key("BTC/USDT", "buy") == "ob:BTC/USDT:bids"

    async def test_opposite_side_key(self):
        assert orderbook.opposite_side_key("BTC/USDT", "buy") == "ob:BTC/USDT:asks"
        assert orderbook.opposite_side_key("BTC/USDT", "sell") == "ob:BTC/USDT:bids"

    async def test_add_resting_order_assigns_seq(self, redis):
        """add_resting_order returns the assigned seq and stores in ZSET."""
        seq = await orderbook.add_resting_order(
            redis, order_id=42, symbol="BTC/USDT", side=OrderSide.SELL,
            price=Decimal("42150.50"), quantity=Decimal("1.0"),
            metadata={"order_id": "42", "user_id": "1", "symbol": "BTC/USDT",
                      "side": "sell", "type": "limit", "price": "42150.50",
                      "quantity": "1.0"},
        )
        assert isinstance(seq, int)
        assert 0 <= seq < orderbook.SCORE_MULTIPLIER

        # Verify the order is in the asks ZSET
        members = await redis.zrange("ob:BTC/USDT:asks", 0, -1, withscores=True)
        assert len(members) == 1
        member, score = members[0]
        assert int(member) == 42
        # Verify score decodes correctly
        price, decoded_seq = orderbook.decode_score(score)
        assert price == 42150.5
        assert decoded_seq == seq

    async def test_add_resting_order_stores_hash(self, redis):
        """The order HASH is populated alongside the ZSET entry."""
        await orderbook.add_resting_order(
            redis, order_id=42, symbol="BTC/USDT", side=OrderSide.SELL,
            price=Decimal("100.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "42", "user_id": "7", "side": "sell",
                      "price": "100.0", "quantity": "1.0"},
        )
        h = await orderbook.get_order_hash(redis, 42)
        assert h is not None
        assert h["order_id"] == "42"
        assert h["user_id"] == "7"
        assert h["side"] == "sell"
        assert h["price"] == "100.0"

    async def test_get_order_hash_returns_none_for_missing(self, redis):
        h = await orderbook.get_order_hash(redis, 999)
        assert h is None

    async def test_remove_resting_order(self, redis):
        await orderbook.add_resting_order(
            redis, order_id=10, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("100.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "10"},
        )
        removed = await orderbook.remove_resting_order(
            redis, order_id=10, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("100.0"), seq=0,
        )
        assert removed is True
        # Second call returns False (already removed)
        removed2 = await orderbook.remove_resting_order(
            redis, order_id=10, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("100.0"), seq=0,
        )
        assert removed2 is False

    async def test_get_best_bid_and_ask(self, redis):
        """Best bid = highest price; best ask = lowest price."""
        await orderbook.add_resting_order(
            redis, order_id=1, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("99.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "1"},
        )
        await orderbook.add_resting_order(
            redis, order_id=2, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("101.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "2"},
        )
        await orderbook.add_resting_order(
            redis, order_id=3, symbol="BTC/USDT", side=OrderSide.SELL,
            price=Decimal("103.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "3"},
        )
        await orderbook.add_resting_order(
            redis, order_id=4, symbol="BTC/USDT", side=OrderSide.SELL,
            price=Decimal("102.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "4"},
        )

        bid = await orderbook.get_best_bid(redis, "BTC/USDT")
        assert bid is not None
        assert bid[0] == 101.0
        assert bid[1] == 2

        ask = await orderbook.get_best_ask(redis, "BTC/USDT")
        assert ask is not None
        assert ask[0] == 102.0
        assert ask[1] == 4

    async def test_get_spread(self, redis):
        await orderbook.add_resting_order(
            redis, order_id=1, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("100.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "1"},
        )
        await orderbook.add_resting_order(
            redis, order_id=2, symbol="BTC/USDT", side=OrderSide.SELL,
            price=Decimal("102.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "2"},
        )
        bid, ask, spread = await orderbook.get_spread(redis, "BTC/USDT")
        assert bid == 100.0
        assert ask == 102.0
        assert spread == 2.0

    async def test_get_spread_empty_side(self, redis):
        """When one side is empty, spread is None."""
        await orderbook.add_resting_order(
            redis, order_id=1, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("100.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "1"},
        )
        bid, ask, spread = await orderbook.get_spread(redis, "BTC/USDT")
        assert bid == 100.0
        assert ask is None
        assert spread is None

    async def test_get_book_snapshot_aggregates_by_price(self, redis):
        """Two orders at the same price are aggregated into one level."""
        for oid, price in [(1, 100.0), (2, 100.0), (3, 101.0)]:
            await orderbook.add_resting_order(
                redis, order_id=oid, symbol="BTC/USDT", side=OrderSide.SELL,
                price=Decimal(str(price)), quantity=Decimal("1.0"),
                metadata={"order_id": str(oid), "quantity": "1.0",
                          "visible_quantity": "1.0"},
            )
        snap = await orderbook.get_book_snapshot(redis, "BTC/USDT", depth=10)
        # Asks ascending: 100.0 (vol 2.0), 101.0 (vol 1.0)
        assert len(snap["asks"]) == 2
        assert snap["asks"][0] == (100.0, 2.0)
        assert snap["asks"][1] == (101.0, 1.0)
        # Bids should be empty
        assert snap["bids"] == []

    async def test_get_opposite_orders_for_match(self, redis):
        """Buy taker gets asks (ascending); sell taker gets bids (descending)."""
        for oid, price in [(1, 100.0), (2, 101.0), (3, 102.0)]:
            await orderbook.add_resting_order(
                redis, order_id=oid, symbol="BTC/USDT", side=OrderSide.SELL,
                price=Decimal(str(price)), quantity=Decimal("1.0"),
                metadata={"order_id": str(oid), "quantity": "1.0",
                          "visible_quantity": "1.0", "is_iceberg": "0",
                          "side": "sell"},
            )

        # Buy taker: opposite = asks, ascending by price
        result = await orderbook.get_opposite_orders_for_match(
            redis, symbol="BTC/USDT", taker_side=OrderSide.BUY, limit=10,
        )
        assert len(result) == 3
        assert result[0]["price"] == 100.0
        assert result[1]["price"] == 101.0
        assert result[2]["price"] == 102.0

    async def test_clear_symbol_book(self, redis):
        await orderbook.add_resting_order(
            redis, order_id=1, symbol="BTC/USDT", side=OrderSide.BUY,
            price=Decimal("100.0"), quantity=Decimal("1.0"),
            metadata={"order_id": "1"},
        )
        deleted = await orderbook.clear_symbol_book(redis, "BTC/USDT")
        assert deleted > 0
        # ZSET should be empty
        bids = await redis.zrange("ob:BTC/USDT:bids", 0, -1)
        assert bids == []


# ============================================================================
#  2. STOPS
# ============================================================================

class TestStops:
    """Tests for app.redis_client.stops."""

    async def test_add_and_remove_stop_order(self, redis):
        await stops.add_stop_order(redis, order_id=10, symbol="BTC/USDT",
                                    stop_price=Decimal("41000"))
        # Verify it's in the ZSET
        members = await redis.zrange("stops:BTC/USDT", 0, -1)
        assert len(members) == 1

        removed = await stops.remove_stop_order(redis, order_id=10, symbol="BTC/USDT")
        assert removed is True

        removed2 = await stops.remove_stop_order(redis, order_id=10, symbol="BTC/USDT")
        assert removed2 is False

    async def test_get_triggered_sell_stops(self, redis):
        """Sell-stops fire when last_price <= stop_price (price fell to/below stop).

        Setup: stops at 41000, 40000, 42000; last_price = 40500.
        Triggered sell-stops: those with stop_price >= 40500 → 41000, 42000.
        """
        await stops.add_stop_order(redis, order_id=1, symbol="BTC/USDT",
                                    stop_price=Decimal("41000"))
        await stops.add_stop_order(redis, order_id=2, symbol="BTC/USDT",
                                    stop_price=Decimal("40000"))
        await stops.add_stop_order(redis, order_id=3, symbol="BTC/USDT",
                                    stop_price=Decimal("42000"))

        triggered = await stops.get_triggered_sell_stops(
            redis, symbol="BTC/USDT", last_trade_price=Decimal("40500"),
        )
        assert set(triggered) == {1, 3}  # 41000 and 42000 are >= 40500

    async def test_get_triggered_buy_stops(self, redis):
        """Buy-stops fire when last_price >= stop_price (price rose to/above stop).

        Setup: stops at 41000, 40000, 42000; last_price = 40500.
        Triggered buy-stops: those with stop_price <= 40500 → 40000.
        """
        await stops.add_stop_order(redis, order_id=1, symbol="BTC/USDT",
                                    stop_price=Decimal("41000"))
        await stops.add_stop_order(redis, order_id=2, symbol="BTC/USDT",
                                    stop_price=Decimal("40000"))
        await stops.add_stop_order(redis, order_id=3, symbol="BTC/USDT",
                                    stop_price=Decimal("42000"))

        triggered = await stops.get_triggered_buy_stops(
            redis, symbol="BTC/USDT", last_trade_price=Decimal("40500"),
        )
        assert set(triggered) == {2}  # only 40000 is <= 40500

    async def test_register_and_get_trailing_state(self, redis):
        await stops.register_trailing_stop(
            redis, order_id=42, symbol="BTC/USDT", side=OrderSide.SELL,
            delta_type="abs", delta_value=Decimal("500"),
            initial_extreme=Decimal("42000"),
            initial_trigger=Decimal("41500"),
        )
        state = await stops.get_trailing_state(redis, order_id=42)
        assert state is not None
        assert state["side"] == "sell"
        assert state["delta_type"] == "abs"
        assert state["delta_value"] == "500"
        assert state["current_extreme"] == "42000"
        assert state["current_trigger"] == "41500"

    async def test_update_trailing_trigger(self, redis):
        await stops.register_trailing_stop(
            redis, order_id=42, symbol="BTC/USDT", side=OrderSide.SELL,
            delta_type="abs", delta_value=Decimal("500"),
            initial_extreme=Decimal("42000"),
            initial_trigger=Decimal("41500"),
        )
        # New high: 42500 → new trigger = 42000
        await stops.update_trailing_trigger(
            redis, order_id=42, symbol="BTC/USDT",
            new_extreme=Decimal("42500"), new_trigger=Decimal("42000"),
        )
        state = await stops.get_trailing_state(redis, order_id=42)
        assert state["current_extreme"] == "42500"
        assert state["current_trigger"] == "42000"

    async def test_remove_trailing_stop(self, redis):
        await stops.register_trailing_stop(
            redis, order_id=42, symbol="BTC/USDT", side=OrderSide.SELL,
            delta_type="abs", delta_value=Decimal("500"),
            initial_extreme=Decimal("42000"),
            initial_trigger=Decimal("41500"),
        )
        removed = await stops.remove_trailing_stop(redis, order_id=42, symbol="BTC/USDT")
        assert removed is True
        state = await stops.get_trailing_state(redis, order_id=42)
        assert state is None

    async def test_update_trailing_extremes_sell_side(self, redis):
        """When a new high arrives, sell trailing's trigger moves up."""
        await stops.register_trailing_stop(
            redis, order_id=1, symbol="BTC/USDT", side=OrderSide.SELL,
            delta_type="abs", delta_value=Decimal("500"),
            initial_extreme=Decimal("42000"),
            initial_trigger=Decimal("41500"),
        )
        # New high 42500 → new trigger = 42000
        updated = await stops.update_trailing_extremes(
            redis, symbol="BTC/USDT", new_high=Decimal("42500"), new_low=None,
        )
        assert 1 in updated
        state = await stops.get_trailing_state(redis, 1)
        assert float(state["current_extreme"]) == pytest.approx(42500.0)
        assert float(state["current_trigger"]) == pytest.approx(42000.0)

    async def test_update_trailing_extremes_buy_side(self, redis):
        """When a new low arrives, buy trailing's trigger moves down."""
        await stops.register_trailing_stop(
            redis, order_id=2, symbol="BTC/USDT", side=OrderSide.BUY,
            delta_type="abs", delta_value=Decimal("500"),
            initial_extreme=Decimal("40000"),
            initial_trigger=Decimal("40500"),
        )
        # New low 39500 → new trigger = 40000
        updated = await stops.update_trailing_extremes(
            redis, symbol="BTC/USDT", new_high=None, new_low=Decimal("39500"),
        )
        assert 2 in updated
        state = await stops.get_trailing_state(redis, 2)
        assert float(state["current_extreme"]) == pytest.approx(39500.0)
        assert float(state["current_trigger"]) == pytest.approx(40000.0)

    async def test_update_trailing_extremes_pct_delta(self, redis):
        """Pct delta = percentage of the extreme."""
        await stops.register_trailing_stop(
            redis, order_id=3, symbol="BTC/USDT", side=OrderSide.SELL,
            delta_type="pct", delta_value=Decimal("1.0"),  # 1%
            initial_extreme=Decimal("42000"),
            initial_trigger=Decimal("41580"),  # 42000 * 0.99
        )
        # New high 44000 → new trigger = 44000 * 0.99 = 43560
        await stops.update_trailing_extremes(
            redis, symbol="BTC/USDT", new_high=Decimal("44000"), new_low=None,
        )
        state = await stops.get_trailing_state(redis, 3)
        assert float(state["current_extreme"]) == pytest.approx(44000.0)
        assert float(state["current_trigger"]) == pytest.approx(43560.0)


# ============================================================================
#  3. QUEUES
# ============================================================================

class TestQueues:
    """Tests for app.redis_client.queues."""

    async def test_build_order_action_payload_minimal(self):
        payload = queues.build_order_action_payload(
            action="place", order_id=42, symbol="BTC/USDT",
            user_id=1, side="buy", type="limit",
        )
        assert payload["action"] == "place"
        assert payload["order_id"] == 42
        assert payload["symbol"] == "BTC/USDT"
        assert "ts" in payload
        assert "price" not in payload  # None values omitted

    async def test_build_order_action_payload_full(self):
        payload = queues.build_order_action_payload(
            action="place", order_id=42, symbol="BTC/USDT",
            user_id=1, side="buy", type="iceberg",
            price=100.0, quantity=1.0,
            is_iceberg=True, visible_qty=0.1, hidden_qty=0.9,
            parent_order_id=10, bulk_id="uuid-123",
        )
        assert payload["is_iceberg"] is True
        assert payload["visible_qty"] == 0.1
        assert payload["hidden_qty"] == 0.9
        assert payload["parent_order_id"] == 10
        assert payload["bulk_id"] == "uuid-123"

    async def test_enqueue_and_brpop_order_action(self, redis):
        """LPUSH + BRPOP gives FIFO order."""
        await queues.enqueue_order_action(redis, {"action": "place", "order_id": 1})
        await queues.enqueue_order_action(redis, {"action": "place", "order_id": 2})

        # BRPOP returns oldest first (FIFO)
        item1 = await queues.brpop_order_action(redis, timeout=1)
        item2 = await queues.brpop_order_action(redis, timeout=1)
        assert item1["order_id"] == 1
        assert item2["order_id"] == 2

    async def test_brpop_returns_none_on_timeout(self, redis):
        """When queue is empty, BRPOP returns None after timeout."""
        item = await queues.brpop_order_action(redis, timeout=1)
        assert item is None

    async def test_enqueue_trade_batch(self, redis):
        trades = [
            {"trade_id": 1, "price": 100.0, "quantity": 0.5},
            {"trade_id": 2, "price": 101.0, "quantity": 0.3},
        ]
        await queues.enqueue_trade_batch(redis, trades)
        batch = await queues.brpop_trade_batch(redis, timeout=1)
        assert batch is not None
        assert len(batch["trades"]) == 2

    async def test_enqueue_trade_batch_empty_is_noop(self, redis):
        await queues.enqueue_trade_batch(redis, [])
        # Queue should be empty
        depth = await queues.queue_length(redis, queues.TRADES_QUEUE)
        assert depth == 0

    async def test_enqueue_deadletter(self, redis):
        await queues.enqueue_deadletter(
            redis, original_payload={"action": "place"}, error="boom",
            queue_name="queue:orders",
        )
        depth = await queues.queue_length(redis, queues.DEADLETTER_QUEUE)
        assert depth == 1

    async def test_queue_length(self, redis):
        await queues.enqueue_order_action(redis, {"order_id": 1})
        await queues.enqueue_order_action(redis, {"order_id": 2})
        depth = await queues.queue_length(redis)
        assert depth == 2

    async def test_peek_queue(self, redis):
        await queues.enqueue_order_action(redis, {"order_id": 1, "action": "place"})
        items = await queues.peek_queue(redis, n=10)
        assert len(items) == 1
        assert items[0]["order_id"] == 1


# ============================================================================
#  4. PUBSUB
# ============================================================================

class TestPubSub:
    """Tests for app.redis_client.pubsub."""

    async def test_publish_and_subscribe_orderbook_snapshot(self, redis):
        """Publish a snapshot and verify a subscriber receives it."""
        channel = pubsub.orderbook_channel("BTC/USDT")
        pubsub_obj = await pubsub.subscribe(redis, channel)
        try:
            # Give subscription a moment to register
            await asyncio.sleep(0.05)

            await pubsub.publish_orderbook_snapshot(
                redis, symbol="BTC/USDT",
                bids=[[100.0, 1.0]], asks=[[101.0, 0.5]],
                last_trade_price=100.5,
            )

            # Read the message
            messages = []
            async for msg in pubsub.iter_messages(pubsub_obj):
                messages.append(msg)
                break  # only one message expected

            assert len(messages) == 1
            assert messages[0]["event"] == "orderbook_snapshot"
            assert messages[0]["symbol"] == "BTC/USDT"
            assert messages[0]["bids"] == [[100.0, 1.0]]
            assert messages[0]["last_trade_price"] == 100.5
        finally:
            await pubsub.unsubscribe(pubsub_obj)

    async def test_publish_trade(self, redis):
        channel = pubsub.trades_channel("BTC/USDT")
        pubsub_obj = await pubsub.subscribe(redis, channel)
        try:
            await asyncio.sleep(0.05)
            await pubsub.publish_trade(
                redis, symbol="BTC/USDT", trade_id=42,
                price=100.5, quantity=0.3, side="buy",
                taker_order_id=10, maker_order_id=5,
            )
            messages = []
            async for msg in pubsub.iter_messages(pubsub_obj):
                messages.append(msg)
                break
            assert messages[0]["event"] == "trade"
            assert messages[0]["trade_id"] == 42
            assert messages[0]["price"] == 100.5
            assert messages[0]["side"] == "buy"
        finally:
            await pubsub.unsubscribe(pubsub_obj)

    async def test_publish_order_update(self, redis):
        channel = pubsub.orders_channel(user_id=7)
        pubsub_obj = await pubsub.subscribe(redis, channel)
        try:
            await asyncio.sleep(0.05)
            await pubsub.publish_order_update(
                redis, user_id=7, event="partially_filled",
                order_id=42, symbol="BTC/USDT", side="buy", type="limit",
                status="partially_filled", price=100.0, quantity=1.0,
                filled_quantity=0.3, remaining_quantity=0.7,
                avg_fill_price=100.0,
            )
            messages = []
            async for msg in pubsub.iter_messages(pubsub_obj):
                messages.append(msg)
                break
            assert messages[0]["event"] == "order"  # envelope discriminator
            assert messages[0]["status_event"] == "partially_filled"  # lifecycle
            assert messages[0]["type"] == "limit"  # OrderType preserved
            assert messages[0]["filled_quantity"] == 0.3
        finally:
            await pubsub.unsubscribe(pubsub_obj)

    async def test_publish_balance_update(self, redis):
        channel = pubsub.balances_channel(user_id=7)
        pubsub_obj = await pubsub.subscribe(redis, channel)
        try:
            await asyncio.sleep(0.05)
            await pubsub.publish_balance_update(
                redis, user_id=7, asset="USDT",
                total=50000.0, locked=1000.0, available=49000.0,
                change=-100.0, reason="order_placed", order_id=42,
            )
            messages = []
            async for msg in pubsub.iter_messages(pubsub_obj):
                messages.append(msg)
                break
            assert messages[0]["event"] == "balance"
            assert messages[0]["asset"] == "USDT"
            assert messages[0]["change"] == -100.0
        finally:
            await pubsub.unsubscribe(pubsub_obj)

    async def test_publish_bulk_result(self, redis):
        channel = pubsub.bulk_channel(user_id=7)
        pubsub_obj = await pubsub.subscribe(redis, channel)
        try:
            await asyncio.sleep(0.05)
            await pubsub.publish_bulk_result(
                redis, user_id=7, bulk_id="uuid-1", action="place",
                total=10, succeeded=8,
                failed=[{"index": 3, "code": "insufficient_balance"}],
            )
            messages = []
            async for msg in pubsub.iter_messages(pubsub_obj):
                messages.append(msg)
                break
            assert messages[0]["event"] == "bulk_result"
            assert messages[0]["succeeded"] == 8
            assert len(messages[0]["failed"]) == 1
        finally:
            await pubsub.unsubscribe(pubsub_obj)

    async def test_publish_returns_subscriber_count(self, redis):
        """publish() returns the number of subscribers that received the message."""
        # No subscribers → returns 0
        count = await pubsub.publish_trade(
            redis, symbol="BTC/USDT", trade_id=1,
            price=100.0, quantity=1.0, side="buy",
        )
        assert count == 0


# ============================================================================
#  5. ORDERS_INDEX
# ============================================================================

class TestOrdersIndex:
    """Tests for app.redis_client.orders_index."""

    async def test_add_and_get_user_open_orders(self, redis):
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        await orders_index.add_open_order(redis, user_id=1, symbol="ETH/USDT", order_id=11)

        orders = await orders_index.get_user_open_orders(redis, user_id=1)
        assert orders == [10, 11]

    async def test_get_user_open_orders_for_symbol(self, redis):
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=11)
        await orders_index.add_open_order(redis, user_id=1, symbol="ETH/USDT", order_id=12)

        btc_orders = await orders_index.get_user_open_orders_for_symbol(
            redis, user_id=1, symbol="BTC/USDT",
        )
        assert btc_orders == [10, 11]

    async def test_count_user_open_orders(self, redis):
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=11)
        count = await orders_index.count_user_open_orders(redis, user_id=1)
        assert count == 2

    async def test_is_order_open(self, redis):
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        assert await orders_index.is_order_open(redis, user_id=1, order_id=10) is True
        assert await orders_index.is_order_open(redis, user_id=1, order_id=99) is False

    async def test_remove_open_order(self, redis):
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        await orders_index.remove_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        assert await orders_index.is_order_open(redis, user_id=1, order_id=10) is False
        # Symbol-level index should also be empty
        symbol_orders = await orders_index.get_symbol_open_orders(redis, "BTC/USDT")
        assert symbol_orders == []

    async def test_cancel_all_user_orders_for_symbol(self, redis):
        """cancel_all_user_orders with symbol drains only that symbol's orders."""
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=10)
        await orders_index.add_open_order(redis, user_id=1, symbol="BTC/USDT", order_id=11)
        await orders_index.add_open_order(redis, user_id=1, symbol="ETH/USDT", order_id=12)

        # Need to set the order:{id} HASH symbol field for cancel_all
        await redis.hset("order:10", "symbol", "BTC/USDT")
        await redis.hset("order:11", "symbol", "BTC/USDT")
        await redis.hset("order:12", "symbol", "ETH/USDT")

        canceled = await orders_index.cancel_all_user_orders(
            redis, user_id=1, symbol="BTC/USDT",
        )
        assert set(canceled) == {10, 11}
        # ETH/USDT order should still be open
        eth_orders = await orders_index.get_user_open_orders_for_symbol(
            redis, user_id=1, symbol="ETH/USDT",
        )
        assert eth_orders == [12]


# ============================================================================
#  6. RATE_LIMIT
# ============================================================================

class TestRateLimit:
    """Tests for app.redis_client.rate_limit."""

    async def test_allows_under_limit(self, redis):
        """First 5 requests within a 60s window are allowed."""
        for i in range(5):
            result, count, retry = await rate_limit.check_and_consume(
                redis, api_key="key1", limit_per_min=5,
            )
            assert result == "allowed"
            assert count == i + 1
            assert retry == 0

    async def test_rejects_over_limit(self, redis):
        """6th request after 5 allowed is rejected."""
        for _ in range(3):
            await rate_limit.check_and_consume(redis, api_key="key2", limit_per_min=3)
        result, count, retry = await rate_limit.check_and_consume(
            redis, api_key="key2", limit_per_min=3,
        )
        assert result == "rejected"
        assert count == 3
        assert retry >= 1

    async def test_different_api_keys_are_independent(self, redis):
        """Each API key has its own bucket."""
        for _ in range(2):
            await rate_limit.check_and_consume(redis, api_key="A", limit_per_min=2)
        # Key B should still have full budget
        result, count, _ = await rate_limit.check_and_consume(
            redis, api_key="B", limit_per_min=2,
        )
        assert result == "allowed"
        assert count == 1

    async def test_reset_clears_bucket(self, redis):
        for _ in range(3):
            await rate_limit.check_and_consume(redis, api_key="C", limit_per_min=3)
        await rate_limit.reset(redis, api_key="C")
        count = await rate_limit.get_current_count(redis, api_key="C")
        assert count == 0

    async def test_get_current_count_does_not_consume(self, redis):
        """get_current_count is read-only."""
        await rate_limit.check_and_consume(redis, api_key="D", limit_per_min=5)
        await rate_limit.check_and_consume(redis, api_key="D", limit_per_min=5)
        count = await rate_limit.get_current_count(redis, api_key="D")
        assert count == 2
        # Verify no extra request was consumed
        count2 = await rate_limit.get_current_count(redis, api_key="D")
        assert count2 == 2
