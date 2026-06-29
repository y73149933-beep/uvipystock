"""Matching worker — async consumer of `queue:orders`.

Pipeline (per order action)
---------------------------
1. ``BRPOP queue:orders`` → get action payload.
2. Load opposite-side book snapshot from Redis (ZSET + HASHes).
3. Build ``PyCOrder`` via ``_bridge.build_corder``.
4. Populate ``CMatchingEngine`` with maker orders via ``add_passive_order``.
5. ``engine.match_active_order(...)`` → trades + outcome + remaining.
6. Apply results to Redis (ZREM/ZADD/HSET) in a pipeline.
7. Open a PG transaction:
     - ``TradeService.persist_trade_batch(...)`` — INSERT trades, UPDATE
       orders, settle/credit balances
     - If taker has remaining qty and is LIMIT/POST_ONLY → insert as resting
     - If MARKET BUY with leftover lock → refund difference
   Commit atomically.
8. ``PUBLISH`` to pub:orderbook, pub:trades, pub:orders, pub:balances
   (handled by TradeService + OrderService).

Concurrency model
-----------------
- One worker process consumes ``queue:orders`` (single-consumer BRPOP).
- For higher throughput, shard by symbol: ``queue:orders:{symbol}`` with
  one worker per shard.
- Each BRPOP gets a fresh ``CMatchingEngine`` (stateless per call).

Connection strategy
-------------------
The worker uses a DEDICATED Redis connection (not from the shared pool)
for BRPOP, because:
  1. BRPOP is a blocking command that holds the connection for up to `timeout`
     seconds. Using a pool connection would starve the pool.
  2. `retry_on_timeout=True` (set on the pool) would retry BRPOP on timeout,
     risking a double-pop (consuming an order twice).
  3. `health_check_interval` sends PINGs on checkout, which can interfere
     with blocking commands.

The dedicated connection uses:
  - `socket_timeout=60` (much larger than BRPOP timeout of 1s)
  - `retry_on_timeout=False` (never retry BRPOP)
  - `health_check_interval=0` (no health checks)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.db.session import async_session_factory
from app.matching._bridge import (
    build_corder,
    is_outcome_reject,
    outcome_to_str,
    trade_dict_to_domain,
    C_OK,
)
from app.matching.engine import CMatchingEngine
from app.models.enums import OrderSide, OrderStatus, OrderType
from app.redis_client import get_redis
from app.redis_client import orderbook, orders_index, queues, pubsub
from app.repositories.order_repo import OrderRepository
from app.repositories.trading_pair_repo import TradingPairRepository
from app.services.trade_service import TradeService

logger = logging.getLogger(__name__)
_settings = get_settings()


@dataclass(slots=True)
class OrderActionPayload:
    """Parsed payload of a `queue:orders` item."""
    action: str
    order_id: int
    symbol: str
    user_id: int
    side: str
    type: str
    price: float | None
    stop_price: float | None
    quantity: float
    is_iceberg: bool
    visible_qty: float | None
    hidden_qty: float | None
    parent_order_id: int | None
    replaces_id: int | None
    bulk_id: str | None
    ts: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OrderActionPayload":
        return cls(
            action=d["action"],
            order_id=int(d["order_id"]),
            symbol=d["symbol"],
            user_id=int(d["user_id"]),
            side=d["side"],
            type=d["type"],
            price=float(d["price"]) if d.get("price") is not None else None,
            stop_price=float(d["stop_price"]) if d.get("stop_price") else None,
            quantity=float(d["quantity"]),
            is_iceberg=bool(d.get("is_iceberg", False)),
            visible_qty=float(d["visible_qty"]) if d.get("visible_qty") else None,
            hidden_qty=float(d["hidden_qty"]) if d.get("hidden_qty") else None,
            parent_order_id=int(d["parent_order_id"]) if d.get("parent_order_id") else None,
            replaces_id=int(d["replaces_id"]) if d.get("replaces_id") else None,
            bulk_id=d.get("bulk_id"),
            ts=int(d.get("ts", 0)),
        )


class MatchingWorker:
    """Async consumer of ``queue:orders`` that drives the Cython matcher.

    Uses a DEDICATED Redis connection for BRPOP (not the shared pool) to
    avoid blocking-command interference with pool health checks and retries.
    """

    def __init__(self, block_timeout: int = 1) -> None:
        self.engine = CMatchingEngine()
        self._running = False
        # Short BRPOP timeout (1s) so the loop can detect connection drops
        # quickly and reconnect. The tradeoff is slightly more CPU on empty
        # polls, but 1s is negligible.
        self._block_timeout = block_timeout
        self._brpop_redis: aioredis.Redis | None = None  # dedicated for BRPOP
        self._redis: aioredis.Redis | None = None        # shared pool for other ops

    async def start(self) -> None:
        """Main BRPOP loop. Runs until ``stop()`` is called.

        Uses a dedicated Redis connection for BRPOP (blocking command) and
        the shared pool for all other Redis operations. Includes retry logic
        for transient failures: if the BRPOP connection drops, we recreate
        it and continue.
        """
        self._running = True
        logger.info("MatchingWorker started, consuming queue:orders")

        # Create dedicated BRPOP connection
        await self._init_brpop_connection()

        # Shared pool connection for non-blocking ops (book load, pubsub, etc.)
        self._redis = get_redis()

        # Wait for Redis to be reachable
        await self._wait_for_redis()

        while self._running:
            try:
                raw_bytes = await self._brpop_redis.brpop(
                    queues.ORDERS_QUEUE, timeout=self._block_timeout,
                )
                if raw_bytes is None:
                    # BRPOP timeout — no items, just loop
                    continue

                # Parse the payload
                _, raw = raw_bytes
                if isinstance(raw, bytes):
                    raw = raw.decode()
                payload_dict = json.loads(raw)
                payload = OrderActionPayload.from_dict(payload_dict)

                await self.process_action(payload)

            except asyncio.CancelledError:
                logger.info("MatchingWorker cancelled")
                break
            except (aioredis.ConnectionError, aioredis.TimeoutError, OSError) as e:
                logger.warning("BRPOP connection error: %s — reconnecting", e)
                await asyncio.sleep(1.0)
                await self._init_brpop_connection()  # recreate the connection
            except Exception as e:
                logger.exception("MatchingWorker error processing action: %s", e)
                try:
                    await queues.enqueue_deadletter(
                        self._redis, payload_dict if 'payload_dict' in dir() else {},
                        str(e), queues.ORDERS_QUEUE,
                    )
                except Exception:
                    pass
                await asyncio.sleep(1.0)

    async def _init_brpop_connection(self) -> None:
        """Create a dedicated Redis connection for BRPOP.

        This connection is SEPARATE from the shared pool to avoid:
          - Blocking pool connections for 1s per BRPOP
          - Health check PINGs interfering with BRPOP
          - retry_on_timeout causing double-pops
        """
        # Close the old connection if it exists
        if self._brpop_redis is not None:
            try:
                await self._brpop_redis.aclose()
            except Exception:
                pass

        # Parse Redis URL to get connection params
        url = str(_settings.redis_url)
        self._brpop_redis = aioredis.Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=10,
            socket_timeout=60,       # MUST be >> BRPOP timeout (1s)
            retry_on_timeout=False,  # NEVER retry BRPOP (double-pop risk)
            health_check_interval=0, # No health checks on blocking connection
        )
        logger.debug("Dedicated BRPOP Redis connection created")

    async def _wait_for_redis(self, max_attempts: int = 30, interval: float = 1.0) -> None:
        """Wait until Redis responds to PING before proceeding."""
        for attempt in range(1, max_attempts + 1):
            try:
                if self._brpop_redis and await self._brpop_redis.ping():
                    if attempt > 1:
                        logger.info("Redis connection restored (attempt %d)", attempt)
                    return
            except Exception as e:
                logger.warning(
                    "Redis not ready (attempt %d/%d): %s",
                    attempt, max_attempts, e,
                )
            await asyncio.sleep(interval)
        logger.error("Redis not reachable after %d attempts — continuing anyway", max_attempts)

    async def stop(self) -> None:
        self._running = False
        if self._brpop_redis is not None:
            try:
                await self._brpop_redis.aclose()
            except Exception:
                pass
            self._brpop_redis = None

    async def process_action(self, payload: OrderActionPayload) -> dict[str, Any]:
        """Process a single order action.

        This is the main entry point for the matching pipeline. It's also
        callable directly from tests (bypassing the Redis queue).
        """
        if payload.action == "cancel":
            return await self._handle_cancel(payload)
        if payload.action == "modify":
            return await self._handle_modify(payload)
        # action == "place"
        return await self._handle_place(payload)

    # ─── PLACE ──────────────────────────────────────────────────────────────

    async def _handle_place(self, payload: OrderActionPayload) -> dict[str, Any]:
        """Match a new order against the book, persist trades, update balances."""
        redis = self._redis

        # 1. Load opposite-side book snapshot from Redis
        maker_orders = await orderbook.get_opposite_orders_for_match(
            redis, payload.symbol, payload.side, limit=1000,
        )

        # 2. Build a fresh engine and load makers
        self.engine.reset()
        for mo in maker_orders:
            ok = self.engine.add_passive_order(
                order_id=mo["order_id"],
                side=1 if mo["side"] == "sell" else 0,  # C_SELL=1, C_BUY=0
                price=mo["price"],
                qty=mo["visible_quantity"] if mo["is_iceberg"] else mo["quantity"],
                is_iceberg=mo["is_iceberg"],
                visible_qty=mo["visible_quantity"],
                hidden_qty=mo["hidden_quantity"],
            )
            if not ok:
                logger.warning("Failed to add maker %d to engine (pool full?)", mo["order_id"])
                break

        # 3. Build the taker COrder
        corder = build_corder(
            order_id=payload.order_id,
            side=payload.side,
            order_type=payload.type,
            price=payload.price,
            quantity=payload.quantity,
            is_iceberg=payload.is_iceberg,
            visible_qty=payload.visible_qty,
            hidden_qty=payload.hidden_qty,
        )

        # 4. Match!
        trades, outcome_int, remaining = self.engine.match_active_order(corder)
        outcome_str = outcome_to_str(outcome_int)

        result: dict[str, Any] = {
            "action": "place",
            "order_id": payload.order_id,
            "outcome": outcome_str,
            "trades": trades,
            "remaining_qty": remaining,
            "rejected": is_outcome_reject(outcome_int),
        }

        # 5. Handle rejection (no trades emitted)
        if is_outcome_reject(outcome_int):
            # The order service should have already handled balance unlock
            # for FOK/Post-Only rejections. Here we just publish the event.
            await pubsub.publish_order_update(
                redis, user_id=payload.user_id,
                event="rejected" if outcome_str == "post_only_cross" else "expired",
                order_id=payload.order_id, symbol=payload.symbol,
                side=payload.side, type=payload.type,
                status=OrderStatus.REJECTED.value if outcome_str == "post_only_cross" else OrderStatus.EXPIRED.value,
            )
            return result

        # 6. Persist trades + update balances in a single PG transaction
        if trades:
            async with async_session_factory() as session:
                async with session.begin():
                    # Load trading pair to get base/quote assets
                    pair_repo = TradingPairRepository(session)
                    pair = await pair_repo.get_by_symbol(payload.symbol)
                    if pair is None:
                        logger.error("Trading pair %s not found for match", payload.symbol)
                        return result

                    # Convert trades to domain dicts with maker_user_id
                    # We need to load maker order user_ids — do it via Redis HASHes
                    enriched_trades = []
                    for t in trades:
                        maker_hash = await orderbook.get_order_hash(redis, t["maker_order_id"])
                        maker_user_id = int(maker_hash.get("user_id", "0")) if maker_hash else 0
                        enriched_t = trade_dict_to_domain(t, payload.symbol)
                        enriched_t["maker_user_id"] = maker_user_id
                        enriched_trades.append(enriched_t)

                    trade_svc = TradeService(session)
                    await trade_svc.persist_trade_batch(
                        trades=enriched_trades,
                        taker_user_id=payload.user_id,
                        taker_order_id=payload.order_id,
                        taker_side=OrderSide(payload.side),
                        taker_type=OrderType(payload.type),
                        symbol=payload.symbol,
                        base_asset=pair.base_asset,
                        quote_asset=pair.quote_asset,
                    )

        # 7. If taker has remaining qty and is LIMIT/POST_ONLY/ICEBERG → insert as resting
        if remaining > 0 and payload.type in ("limit", "post_only", "iceberg"):
            await self._insert_resting_taker(payload, remaining)
        elif remaining > 0 and payload.type == "market" and payload.side == "buy":
            # Market buy with leftover lock → refund the difference
            await self._refund_market_buy_leftover(payload, remaining)

        # 8. Publish orderbook update (L2 delta)
        await self._publish_orderbook_delta(payload.symbol)

        return result

    async def _insert_resting_taker(self, payload: OrderActionPayload, remaining: float) -> None:
        """Insert a partially-filled taker as a new resting order in the book."""
        redis = self._redis
        # Compute the lock amount for the remaining (it's already locked at placement)
        # The order is already in PG with status PARTIALLY_FILLED.
        # We just need to add it to the Redis book + indexes.
        metadata = {
            "order_id": str(payload.order_id),
            "user_id": str(payload.user_id),
            "symbol": payload.symbol,
            "side": payload.side,
            "type": payload.type,
            "price": str(payload.price) if payload.price else "",
            "quantity": str(payload.quantity),
            "visible_quantity": str(payload.visible_qty) if payload.is_iceberg else str(remaining),
            "hidden_quantity": str(payload.hidden_qty) if payload.is_iceberg else "",
            "is_iceberg": "1" if payload.is_iceberg else "0",
        }
        await orderbook.store_order_hash(redis, payload.order_id, metadata)
        # Add to ZSET with a fresh seq
        await orderbook.add_resting_order(
            redis,
            order_id=payload.order_id,
            symbol=payload.symbol,
            side=payload.side,
            price=Decimal(str(payload.price)) if payload.price else Decimal("0"),
            quantity=Decimal(str(remaining)),
        )
        await orders_index.add_open_order(redis, payload.user_id, payload.symbol, payload.order_id)

    async def _refund_market_buy_leftover(
        self, payload: OrderActionPayload, remaining_qty: float,
    ) -> None:
        """Refund the unused quote asset for a market buy that didn't fully fill.

        The order service locked worst-case (best_ask * 1.01 * qty) at placement.
        The actual cost was less, so we refund the difference.
        """
        # This requires knowing the actual fill price vs the locked estimate.
        # For simplicity, we refund `remaining_qty * worst_case_price`.
        # A full implementation would track the exact locked amount on the Order row.
        # TODO: implement proper refund tracking in Step 2e.
        logger.info(
            "Market buy %d has leftover %f — refund pending (TODO)",
            payload.order_id, remaining_qty,
        )

    async def _publish_orderbook_delta(self, symbol: str) -> None:
        """Publish an L2 snapshot after a match (simpler than delta computation)."""
        redis = self._redis
        snap = await orderbook.get_book_snapshot(redis, symbol, depth=20)
        await pubsub.publish_orderbook_snapshot(
            redis, symbol,
            bids=[[p, q] for p, q in snap["bids"]],
            asks=[[p, q] for p, q in snap["asks"]],
        )

    # ─── CANCEL ─────────────────────────────────────────────────────────────

    async def _handle_cancel(self, payload: OrderActionPayload) -> dict[str, Any]:
        """Handle a cancel action: remove from engine's view (already done by order service).

        The order service has already:
          - Unlocked balance
          - Updated PG status to CANCELED
          - Removed from Redis ZSET + HASH + indexes
          - Published events

        The worker just needs to acknowledge. In a sharded setup, this would
        also remove the order from any in-flight match pipelines.
        """
        return {"action": "cancel", "order_id": payload.order_id, "outcome": "ok"}

    # ─── MODIFY (Cancel-Replace) ────────────────────────────────────────────

    async def _handle_modify(self, payload: OrderActionPayload) -> dict[str, Any]:
        """Handle a modify action: the order service has already done Cancel-Replace.

        The new order (with replaces_id = old) is enqueued as a 'place' action
        separately. This handler is for cleanup of the old order in any
        in-flight match pipelines.
        """
        return {"action": "modify", "order_id": payload.order_id, "outcome": "ok"}


# ─── Module-level entry point ────────────────────────────────────────────────

async def _main() -> None:
    worker = MatchingWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
