"""Trade service — persist trades and update order fill state.

Called by the matching worker after each match. The worker produces a list
of trade dicts (from the Cython matcher) and passes them here for:

  1. INSERT into the `trades` table (batched)
  2. UPDATE orders: filled_quantity, filled_quote_qty, status
  3. UPDATE balances: settle taker's locked, credit maker's available
  4. PUBLISH trade + order + balance events to Redis Pub/Sub

All updates for a single match batch are in ONE PostgreSQL transaction
to guarantee atomicity.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderSide, OrderStatus, OrderType
from app.models.trade import Trade
from app.redis_client import get_redis
from app.redis_client import orderbook, orders_index, pubsub, queues, stops
from app.repositories.balance_repo import BalanceRepository
from app.repositories.order_repo import OrderRepository
from app.repositories.trade_repo import TradeRepository
from app.services.balance_service import BalanceService
from app.services.post_commit import defer_after_commit

logger = logging.getLogger(__name__)


class TakerOrderInvalidError(Exception):
    """Raised when a match batch references a missing/invalid taker order.

    This happens when a placement action reaches the worker but its PG
    transaction was rolled back afterward (phantom place), or when a duplicate
    delivery races a cancel. Settling makers against such a taker would
    credit counterparties without anyone paying (money printing), so the
    whole batch is refused instead.
    """


class TradeService:
    """Persist trades and update balances + orders atomically."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.trade_repo = TradeRepository(session)
        self.order_repo = OrderRepository(session)
        self.balance_repo = BalanceRepository(session)
        self.balance_svc = BalanceService(session)

    # Statuses of an order that may still absorb fills from a match.
    _MATCHABLE = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    async def persist_trade_batch(
        self,
        trades: list[dict[str, Any]],
        taker_user_id: int,
        taker_order_id: int,
        taker_side: OrderSide,
        taker_type: OrderType,
        symbol: str,
        base_asset: str,
        quote_asset: str,
    ) -> list[Trade]:
        """Persist a batch of trades from a single match.

        For each trade:
          - INSERT Trade row
          - UPDATE maker order (filled_quantity, status)
          - UPDATE taker order (filled_quantity, status) — once per batch
          - Settle taker's locked asset, credit maker's available asset

        Balance flow for a BUY taker (taker buys base, pays quote):
          - Taker: locked quote was held at placement → settle (deduct) quote
          - Taker: credit base (the asset they bought)
          - Maker: locked base was held → settle (deduct) base
          - Maker: credit quote (the asset they sold for)

        Balance flow for a SELL taker (mirror of above):
          - Taker: locked base → settle base
          - Taker: credit quote
          - Maker: locked quote → settle quote
          - Maker: credit base

        Returns the list of persisted Trade objects.
        Raises :class:`TakerOrderInvalidError` if the taker gate fails.
        """
        if not trades:
            return []

        redis = get_redis()

        # ── Phase 1: aggregate raw fill deltas per maker ────────────────────
        raw_maker_deltas: dict[int, tuple[Decimal, Decimal]] = {}  # id → (qty, quote)
        taker_qty_total_preview = Decimal("0")
        for trade_dict in trades:
            maker_id = int(trade_dict["maker_order_id"])
            price = Decimal(str(trade_dict["price"]))
            qty = Decimal(str(trade_dict["quantity"]))
            quote_qty = price * qty

            if maker_id in raw_maker_deltas:
                prev_qty, prev_quote = raw_maker_deltas[maker_id]
                raw_maker_deltas[maker_id] = (prev_qty + qty, prev_quote + quote_qty)
            else:
                raw_maker_deltas[maker_id] = (qty, quote_qty)
            taker_qty_total_preview += qty

        # ── Phase 2: lock involved rows FIRST, then validate ────────────────
        maker_orders: dict[int, Any] = {}
        for maker_id in raw_maker_deltas:
            mo = await self.order_repo.get_for_update(maker_id)
            if mo is not None:
                maker_orders[maker_id] = mo

        taker_order = await self.order_repo.get_for_update(taker_order_id)

        # Taker gate — refuse the batch rather than print money.
        if (
            taker_order is None
            or taker_order.status not in self._MATCHABLE
            or taker_order.user_id != taker_user_id
        ):
            raise TakerOrderInvalidError(
                f"taker order {taker_order_id} "
                f"(status={taker_order.status.value if taker_order else 'MISSING'}) "
                f"cannot absorb fills — refusing {len(trades)} trade(s)"
            )

        # Taker overfill guard: a replayed delivery must not double-count.
        if taker_order.remaining_quantity < taker_qty_total_preview - Decimal("1e-12"):
            raise TakerOrderInvalidError(
                f"taker order {taker_order_id}: batch qty {taker_qty_total_preview} "
                f"exceeds remaining {taker_order.remaining_quantity}"
            )

        # ── Phase 3: filter invalid makers out of the trade list ────────────
        # Epsilon tolerates double↔decimal round-trip noise from the matcher.
        _EPS = Decimal("1e-12")
        valid_trades: list[dict[str, Any]] = []
        for trade_dict in trades:
            maker_id = int(trade_dict["maker_order_id"])
            qty = Decimal(str(trade_dict["quantity"]))
            mo = maker_orders.get(maker_id)

            if mo is None:
                await self._drop_stale_maker(redis, maker_id,
                                             "PG row missing (ghost book entry)")
                continue
            if mo.status not in self._MATCHABLE:
                logger.warning(
                    "Dropping trade: maker %d in non-matchable status %s",
                    maker_id, mo.status.value,
                )
                continue
            if mo.remaining_quantity < qty - _EPS:
                logger.warning(
                    "Dropping trade: maker %d overfill %s > remaining %s",
                    maker_id, qty, mo.remaining_quantity,
                )
                continue

            valid_trades.append(trade_dict)

        if not valid_trades:
            logger.error("All %d trades dropped by maker gates", len(trades))
            return []

        # ── Phase 4: persist the valid subset ────────────────────────────────
        maker_fill_deltas: dict[int, tuple[Decimal, Decimal]] = {}
        taker_total_qty = Decimal("0")
        taker_total_quote = Decimal("0")

        persisted: list[Trade] = []
        for trade_dict in valid_trades:
            maker_id = int(trade_dict["maker_order_id"])
            price = Decimal(str(trade_dict["price"]))
            qty = Decimal(str(trade_dict["quantity"]))
            quote_qty = price * qty

            trade = Trade(
                taker_order_id=taker_order_id,
                maker_order_id=maker_id,
                symbol=symbol,
                price=price,
                quantity=qty,
                quote_quantity=quote_qty,
                side=taker_side,
                taker_user_id=taker_user_id,
                maker_user_id=maker_orders[maker_id].user_id,
                taker_fee=Decimal("0"),   # TODO: compute from pair fees
                maker_fee=Decimal("0"),
            )
            self.session.add(trade)
            persisted.append(trade)

            if maker_id in maker_fill_deltas:
                prev_qty, prev_quote = maker_fill_deltas[maker_id]
                maker_fill_deltas[maker_id] = (prev_qty + qty, prev_quote + quote_qty)
            else:
                maker_fill_deltas[maker_id] = (qty, quote_qty)

            taker_total_qty += qty
            taker_total_quote += quote_qty

        await self.session.flush()

        # Update maker orders + settle balances
        for maker_id, (qty_delta, quote_delta) in maker_fill_deltas.items():
            mo = maker_orders[maker_id]

            mo.filled_quantity += qty_delta
            mo.filled_quote_qty += quote_delta
            # Decrement this maker's own reservation by exactly what the trade
            # consumed from its lock (base for sells, quote for buys). Keeps
            # locked_amount authoritative for later cancel/modify refunds.
            mo.locked_amount = max(
                (mo.locked_amount or Decimal("0"))
                - (qty_delta if mo.side == OrderSide.SELL else quote_delta),
                Decimal("0"),
            )
            mo.version += 1

            # Determine maker status
            if mo.filled_quantity >= mo.quantity:
                mo.status = OrderStatus.FILLED
            else:
                mo.status = OrderStatus.PARTIALLY_FILLED

            # Balance flow
            await self._settle_trade_balances(
                taker_side=taker_side,
                is_taker=False,
                maker_order=mo,
                qty=qty_delta,
                quote_qty=quote_delta,
                base_asset=base_asset,
                quote_asset=quote_asset,
            )

            # Update Redis maker order HASH + remove from book if filled
            await self._update_redis_maker(redis, mo)

        # Update taker order
        taker_order.filled_quantity += taker_total_qty
        taker_order.filled_quote_qty += taker_total_quote
        # Same per-order reservation accounting on the taker leg.
        taker_order.locked_amount = max(
            (taker_order.locked_amount or Decimal("0"))
            - (taker_total_quote if taker_side == OrderSide.BUY else taker_total_qty),
            Decimal("0"),
        )
        taker_order.version += 1

        if taker_order.filled_quantity >= taker_order.quantity:
            taker_order.status = OrderStatus.FILLED
        elif taker_order.filled_quantity > 0:
            taker_order.status = OrderStatus.PARTIALLY_FILLED

        # Taker balance flow
        await self._settle_trade_balances(
            taker_side=taker_side,
            is_taker=True,
            taker_order=taker_order,
            qty=taker_total_qty,
            quote_qty=taker_total_quote,
            base_asset=base_asset,
            quote_asset=quote_asset,
        )

        # If taker is fully filled and has SL/TP children → activate them
        if taker_order.status == OrderStatus.FILLED:
            await self._activate_sltp_children(taker_order)

        await self.session.flush()

        # Publish trade events (public) + order updates (private) AFTER the
        # settlement transaction commits. These are fire-and-forget pub/sub:
        # publishing them inside the transaction means a rollback (e.g. an
        # overfilled maker caught by a later guard) emits ghost fills that
        # clients can never un-see. All scalars are captured now; the
        # deferred task performs only Redis calls.
        # See app/services/post_commit.py.
        trade_publish_args = [
            dict(symbol=symbol, trade_id=trade.id,
                 price=float(trade.price), quantity=float(trade.quantity),
                 side=trade.side.value,
                 taker_order_id=taker_order_id,
                 maker_order_id=trade.maker_order_id)
            for trade in persisted
        ]
        order_publish_args = []
        if taker_order is not None:
            event = "filled" if taker_order.status == OrderStatus.FILLED else "partially_filled"
            order_publish_args.append(dict(
                user_id=taker_user_id, event=event,
                order_id=taker_order.id, symbol=taker_order.symbol,
                side=taker_order.side.value, type=taker_order.type.value,
                status=taker_order.status.value,
                filled_quantity=float(taker_order.filled_quantity),
                remaining_quantity=float(taker_order.remaining_quantity),
                avg_fill_price=float(taker_order.avg_fill_price) if taker_order.avg_fill_price else None,
            ))
        for maker_id, mo in maker_orders.items():
            event = "filled" if mo.status == OrderStatus.FILLED else "partially_filled"
            order_publish_args.append(dict(
                user_id=mo.user_id, event=event,
                order_id=mo.id, symbol=mo.symbol,
                side=mo.side.value, type=mo.type.value,
                status=mo.status.value,
                filled_quantity=float(mo.filled_quantity),
                remaining_quantity=float(mo.remaining_quantity),
                avg_fill_price=float(mo.avg_fill_price) if mo.avg_fill_price else None,
            ))

        async def _publish_fill_events() -> None:
            for kwargs in trade_publish_args:
                await pubsub.publish_trade(redis, **kwargs)
            for kwargs in order_publish_args:
                await pubsub.publish_order_update(redis, **kwargs)

        defer_after_commit(self.session, _publish_fill_events)

        return persisted

    async def _drop_stale_maker(self, redis, maker_id: int, reason: str) -> None:
        """Best-effort removal of a maker book entry that cannot settle.

        A trade referencing a non-existent PG row means the Redis ZSET/HASH
        is a ghost left by a rolled-back placement. Removing it here keeps
        the book self-healing instead of poisoning every future match.
        Failures are swallowed — reconcile flows may clean up later.
        """
        logger.warning("Dropping stale maker %d from book: %s", maker_id, reason)
        try:
            h = await orderbook.get_order_hash(redis, maker_id)
            if not h:
                return
            side = h.get("side", "buy")
            price = Decimal(h.get("price") or "0")
            await orderbook.remove_resting_order(
                redis, maker_id, h.get("symbol", ""), side, price, 0,
            )
            await orderbook.delete_order_hash(redis, maker_id)
            from app.redis_client import orders_index
            uid = int(h.get("user_id") or 0)
            if uid and h.get("symbol"):
                await orders_index.remove_open_order(redis, uid, h["symbol"], maker_id)
        except Exception as e:  # noqa: BLE001 — cleanup must never break settlement
            logger.warning("Stale-maker cleanup for %d failed: %s", maker_id, e)

    async def _settle_trade_balances(
        self,
        *,
        taker_side: OrderSide,
        is_taker: bool,
        taker_order: Any = None,
        maker_order: Any = None,
        qty: Decimal,
        quote_qty: Decimal,
        base_asset: str,
        quote_asset: str,
    ) -> None:
        """Settle balances for one side of a trade (taker or maker).

        For a BUY taker (taker buys base, pays quote):
          - Taker: settle quote (locked → deducted), credit base
          - Maker: settle base (locked → deducted), credit quote

        For a SELL taker (mirror):
          - Taker: settle base, credit quote
          - Maker: settle quote, credit base
        """
        if is_taker:
            assert taker_order is not None
            user_id = taker_order.user_id
            if taker_side == OrderSide.BUY:
                # Taker pays quote, receives base
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                if quote_bal:
                    await self.balance_svc.settle_pessimistic(quote_bal, quote_qty, "trade_settled", taker_order.id)
                if base_bal:
                    await self.balance_svc.credit_pessimistic(base_bal, qty, "trade_received", taker_order.id)
            else:
                # Taker pays base, receives quote
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                if base_bal:
                    await self.balance_svc.settle_pessimistic(base_bal, qty, "trade_settled", taker_order.id)
                if quote_bal:
                    await self.balance_svc.credit_pessimistic(quote_bal, quote_qty, "trade_received", taker_order.id)
        else:
            assert maker_order is not None
            user_id = maker_order.user_id
            if taker_side == OrderSide.BUY:
                # Maker sells base, receives quote
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                if base_bal:
                    await self.balance_svc.settle_pessimistic(base_bal, qty, "trade_settled", maker_order.id)
                if quote_bal:
                    await self.balance_svc.credit_pessimistic(quote_bal, quote_qty, "trade_received", maker_order.id)
            else:
                # Maker buys base, pays quote
                quote_bal = await self.balance_repo.get_for_update(user_id, quote_asset)
                base_bal = await self.balance_repo.get_for_update(user_id, base_asset)
                if quote_bal:
                    await self.balance_svc.settle_pessimistic(quote_bal, quote_qty, "trade_settled", maker_order.id)
                if base_bal:
                    await self.balance_svc.credit_pessimistic(base_bal, qty, "trade_received", maker_order.id)

    async def _update_redis_maker(self, redis, maker_order) -> None:
        """Update Redis HASH for a maker order after a fill.

        If the maker is fully filled, remove from book + indexes.
        Otherwise, update the HASH's filled_quantity AND visible_quantity
        (so the order book snapshot shows the correct remaining volume).
        """
        if maker_order.status == OrderStatus.FILLED:
            await orderbook.remove_resting_order(
                redis, maker_order.id, maker_order.symbol, maker_order.side,
                maker_order.price or Decimal("0"), 0,
            )
            await orderbook.delete_order_hash(redis, maker_order.id)
            from app.redis_client import orders_index
            await orders_index.remove_open_order(
                redis, maker_order.user_id, maker_order.symbol, maker_order.id,
            )
        else:
            # Update HASH with new filled_quantity + visible_quantity
            # visible_quantity is what the order book snapshot uses to display
            # the remaining volume. Without this update, the book shows the
            # original quantity even after a partial fill.
            remaining = maker_order.quantity - maker_order.filled_quantity
            await orderbook.update_order_hash_fields(redis, maker_order.id, {
                "filled_quantity": str(maker_order.filled_quantity),
                "visible_quantity": str(remaining),
                "quantity": str(remaining),
                "status": maker_order.status.value,
            })

    async def _activate_sltp_children(self, parent_order) -> None:
        """When a parent order fills, activate its SL/TP children.

        Conditional children (stop_market / stop_limit) are registered in
        the STOP QUEUE (``stops:{symbol}``) and stay PENDING until the
        monitor's last-trade rule fires them — exactly like standalone
        conditional orders placed via the API. The previous behavior
        enqueued them straight into ``queue:orders``, where the bridge's
        _TYPE_MAP silently converted stop_market→MARKET / stop_limit→LIMIT,
        so every SL executed immediately whenever the book had liquidity
        (the reported "stops fire instantly" regression).

        Plain children (TP limit) still go to the engine right away, but
        now also get the Redis order HASH + open-orders index: the trigger
        scan reads `side` from the HASH, settlement resolves maker user_id
        from it, and the public ladder reads stop_price/quantity.

        Both branches publish sl_tp_activated; conditional registration
        additionally invalidates the public stop ladder.
        """
        redis = get_redis()

        # Load children
        children = await self.order_repo.list_children_by_parent(parent_order.id)
        for child in children:
            if child.status != OrderStatus.PENDING:
                continue

            is_conditional = child.type in (
                OrderType.STOP_MARKET, OrderType.STOP_LIMIT,
            )

            # Capture scalars now; register the Redis effects to run after
            # this settlement transaction commits (worker must never see a
            # place action for an uncommitted child row — see post_commit.py).
            _cid, _csym = child.id, child.symbol
            _cuser, _cside_s, _ctype = child.user_id, child.side.value, child.type.value
            _cprice = float(child.price) if child.price else None
            _cqty = float(child.quantity)
            _cstop = child.stop_price or Decimal("0")
            _cparent = parent_order.id

            if not is_conditional:
                # Plain child → straight to the matching engine.
                child.status = OrderStatus.NEW
                _payload = queues.build_order_action_payload(
                    action="place",
                    order_id=_cid,
                    symbol=_csym,
                    user_id=_cuser,
                    side=_cside_s,
                    type=_ctype,
                    price=_cprice,
                    quantity=_cqty,
                    parent_order_id=_cparent,
                )
            else:
                _payload = None

            _hash_meta = {
                "order_id": str(_cid),
                "user_id": str(_cuser),
                "symbol": _csym,
                "side": _cside_s,
                "type": _ctype,
                "price": str(child.price) if child.price else "",
                "stop_price": str(child.stop_price) if child.stop_price else "",
                "quantity": str(child.quantity),
                "is_iceberg": "0",
            }
            _is_cond = is_conditional

            # Default-args bind THIS iteration's values (a plain closure
            # would see the last child's ids after the loop ends).
            async def _child_redis_effects(
                cond=_is_cond, cid=_cid, csym=_csym, cuser=_cuser,
                cstop=_cstop, payload_=_payload, meta_=_hash_meta,
            ) -> None:
                if cond:
                    # Arm the conditional child: stays PENDING until its stop
                    # trigger fires (the monitor only converts PENDING rows).
                    await stops.add_stop_order(
                        redis, order_id=cid, symbol=csym, stop_price=cstop,
                    )
                else:
                    await queues.enqueue_order_action(redis, payload_)

                # Shared registration (both branches). Without the HASH a
                # conditional child is a phantom in the trigger scan (skipped,
                # never fires) and a resting TP child settles under user 0.
                await orders_index.add_open_order(redis, cuser, csym, cid)
                await orderbook.store_order_hash(redis, cid, meta_)

            defer_after_commit(self.session, _child_redis_effects)
            child.version += 1

            # Publish SL/TP activation event (after commit — real activation only)
            _sl_id, _tp_id = parent_order.sl_order_id, parent_order.tp_order_id
            defer_after_commit(self.session, partial(
                pubsub.publish_sl_tp_activated, redis,
                user_id=_cuser,
                parent_order_id=_cparent,
                sl_order_id=_sl_id,
                tp_order_id=_tp_id,
            ))
            if _is_cond:
                # Public stop-ladder visibility: this level just appeared
                defer_after_commit(
                    self.session, partial(pubsub.publish_stops_changed, redis, _csym),
                )


__all__ = [
    "TradeService",
    "TakerOrderInvalidError",
]
