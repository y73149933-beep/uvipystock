"""Balance service — atomic lock / unlock / settle / credit with invariant maintenance.

This is the financial source-of-truth layer. Every mutation of `locked_balance`
or `available_balance` MUST go through this service to preserve the invariant:

    total_balance == locked_balance + available_balance

Locking strategy
----------------
The service offers two strategies, chosen by the caller:

1. **Optimistic locking** (default for API-layer order placement)
   - Read balance + version
   - Issue `UPDATE ... WHERE version = X AND available >= amount`
   - Check rowcount; if 0 → either version mismatch or insufficient funds
   - Retry up to `max_retries` times on version mismatch

2. **Pessimistic locking** (used by matching worker for high-contention makers)
   - `SELECT ... FOR UPDATE` to hold a row lock
   - Mutate in Python, bump version
   - Commit releases the lock

The matching worker uses pessimistic locking because it processes trades
sequentially and benefits from holding the lock through the whole match.
The API layer uses optimistic locking because order placement is sporadic
and retries are cheap.

Decimal precision
-----------------
All amounts are `Decimal` to preserve exact precision. Conversion to float
happens only at the Cython boundary (in the matching worker).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import Balance
from app.redis_client import get_redis
from app.redis_client import pubsub
from app.repositories.balance_repo import BalanceRepository

logger = logging.getLogger(__name__)


# ─── Exceptions ──────────────────────────────────────────────────────────────

class InsufficientBalanceError(Exception):
    """Raised when a lock/credit operation would result in negative balance."""

    def __init__(self, user_id: int, asset: str, needed: Decimal, available: Decimal):
        self.user_id = user_id
        self.asset = asset
        self.needed = needed
        self.available = available
        super().__init__(
            f"Insufficient {asset} balance for user {user_id}: "
            f"need {needed}, have {available}"
        )


class BalanceVersionConflict(Exception):
    """Raised when optimistic locking fails after max retries."""

    def __init__(self, user_id: int, asset: str, retries: int):
        self.user_id = user_id
        self.asset = asset
        self.retries = retries
        super().__init__(
            f"Balance version conflict for user={user_id} asset={asset} "
            f"after {retries} retries"
        )


# ─── Lock reasons (for audit trail + WS events) ──────────────────────────────

LockReason = Literal[
    "order_placed",          # limit/post_only/iceberg/IOC/FOK placed
    "market_buy_estimate",   # market buy worst-case lock
    "stop_triggered",        # stop order triggered, locking for child order
    "order_modified",        # Cancel-Replace: locking for new params
]

UnlockReason = Literal[
    "order_canceled",        # user/admin canceled
    "order_modified",        # Cancel-Replace: unlocking old params
    "order_rejected",        # post-only crossed, FOK failed
    "market_buy_refund",     # market buy: refund unused worst-case
]

SettleReason = Literal[
    "trade_settled",         # taker's locked asset consumed by trade
]

CreditReason = Literal[
    "trade_received",        # maker received asset from taker
    "admin_adjustment",      # admin credited funds
    "deposit",               # (future) external deposit
]


# ─── Service ─────────────────────────────────────────────────────────────────

class BalanceService:
    """Atomic balance operations with invariant maintenance.

    Each method opens its own transaction scope (the caller passes an
    AsyncSession that is already in a transaction, e.g. via
    `async with session.begin():`).
    """

    MAX_OPTIMISTIC_RETRIES = 3

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = BalanceRepository(session)

    # ─── Optimistic-lock operations ─────────────────────────────────────────

    async def lock_optimistic(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        reason: LockReason = "order_placed",
        order_id: int | None = None,
    ) -> Balance:
        """Move `amount` from available → locked using optimistic locking.

        Retries on version conflict up to MAX_OPTIMISTIC_RETRIES.
        Raises InsufficientBalanceError if the available balance is too low
        (even after retries).
        """
        if amount <= 0:
            raise ValueError(f"lock amount must be positive, got {amount}")

        last_error: Exception | None = None
        for attempt in range(self.MAX_OPTIMISTIC_RETRIES):
            balance = await self.repo.get_by_user_asset(user_id, asset)
            if balance is None:
                # Auto-create a zero balance so the lock fails clearly
                balance = await self.repo.get_or_create(user_id, asset)

            if balance.available_balance < amount:
                raise InsufficientBalanceError(
                    user_id, asset, amount, balance.available_balance
                )

            success = await self.repo.optimistic_lock(
                user_id, asset, amount, balance.version,
            )
            if success:
                await self.session.flush()
                await self._publish_balance_update(balance, -amount, reason, order_id)
                return balance

            logger.debug(
                "Optimistic lock retry %d for user=%d asset=%s",
                attempt + 1, user_id, asset,
            )
            last_error = BalanceVersionConflict(user_id, asset, attempt + 1)

        raise last_error  # type: ignore[misc]

    async def unlock_optimistic(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        reason: UnlockReason = "order_canceled",
        order_id: int | None = None,
    ) -> Balance:
        """Move `amount` from locked → available (cancel order)."""
        if amount <= 0:
            raise ValueError(f"unlock amount must be positive, got {amount}")

        for attempt in range(self.MAX_OPTIMISTIC_RETRIES):
            balance = await self.repo.get_by_user_asset(user_id, asset)
            if balance is None:
                raise ValueError(
                    f"Balance not found for user={user_id} asset={asset} "
                    f"(cannot unlock {amount})"
                )

            success = await self.repo.optimistic_unlock(
                user_id, asset, amount, balance.version,
            )
            if success:
                await self.session.flush()
                await self._publish_balance_update(balance, amount, reason, order_id)
                return balance

            logger.debug(
                "Optimistic unlock retry %d for user=%d asset=%s",
                attempt + 1, user_id, asset,
            )

        raise BalanceVersionConflict(user_id, asset, self.MAX_OPTIMISTIC_RETRIES)

    async def settle_optimistic(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        reason: SettleReason = "trade_settled",
        order_id: int | None = None,
    ) -> Balance:
        """Deduct `amount` from locked + total (taker's asset consumed by trade)."""
        if amount <= 0:
            raise ValueError(f"settle amount must be positive, got {amount}")

        for attempt in range(self.MAX_OPTIMISTIC_RETRIES):
            balance = await self.repo.get_by_user_asset(user_id, asset)
            if balance is None:
                raise ValueError(
                    f"Balance not found for user={user_id} asset={asset}"
                )

            success = await self.repo.optimistic_settle(
                user_id, asset, amount, balance.version,
            )
            if success:
                await self.session.flush()
                # Settle doesn't change available, but total decreased
                await self._publish_balance_update(balance, Decimal("0"), reason, order_id)
                return balance

        raise BalanceVersionConflict(user_id, asset, self.MAX_OPTIMISTIC_RETRIES)

    async def credit_optimistic(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        reason: CreditReason = "trade_received",
        order_id: int | None = None,
    ) -> Balance:
        """Credit `amount` to total + available (maker received asset)."""
        if amount <= 0:
            raise ValueError(f"credit amount must be positive, got {amount}")

        for attempt in range(self.MAX_OPTIMISTIC_RETRIES):
            balance = await self.repo.get_by_user_asset(user_id, asset)
            if balance is None:
                balance = await self.repo.get_or_create(user_id, asset)
                # get_or_create may have changed version; reload
                continue

            success = await self.repo.optimistic_credit(
                user_id, asset, amount, balance.version,
            )
            if success:
                await self.session.flush()
                await self._publish_balance_update(balance, amount, reason, order_id)
                return balance

        raise BalanceVersionConflict(user_id, asset, self.MAX_OPTIMISTIC_RETRIES)

    # ─── Pessimistic-lock operations ────────────────────────────────────────
    # Used by the matching worker. The caller MUST hold a transaction with
    # FOR UPDATE locks acquired via repo.get_for_update().

    async def lock_pessimistic(
        self,
        balance: Balance,
        amount: Decimal,
        reason: LockReason = "order_placed",
        order_id: int | None = None,
    ) -> None:
        """Lock `amount` on an already-locked Balance row."""
        self.repo.lock_pessimistic(balance, amount)
        await self.session.flush()
        await self._publish_balance_update(balance, -amount, reason, order_id)

    async def unlock_pessimistic(
        self,
        balance: Balance,
        amount: Decimal,
        reason: UnlockReason = "order_canceled",
        order_id: int | None = None,
    ) -> None:
        """Unlock `amount` on an already-locked Balance row."""
        self.repo.unlock_pessimistic(balance, amount)
        await self.session.flush()
        await self._publish_balance_update(balance, amount, reason, order_id)

    async def settle_pessimistic(
        self,
        balance: Balance,
        amount: Decimal,
        reason: SettleReason = "trade_settled",
        order_id: int | None = None,
    ) -> None:
        """Settle `amount` on an already-locked Balance row."""
        self.repo.settle_pessimistic(balance, amount)
        await self.session.flush()
        await self._publish_balance_update(balance, Decimal("0"), reason, order_id)

    async def credit_pessimistic(
        self,
        balance: Balance,
        amount: Decimal,
        reason: CreditReason = "trade_received",
        order_id: int | None = None,
    ) -> None:
        """Credit `amount` on an already-locked Balance row."""
        self.repo.credit_pessimistic(balance, amount)
        await self.session.flush()
        await self._publish_balance_update(balance, amount, reason, order_id)

    # ─── Combined trade-settlement helpers ──────────────────────────────────
    # These wrap the common 2-step patterns used by the matching worker:
    #   - settle taker's locked quote, credit maker's available quote
    #   - settle taker's locked base, credit maker's available base

    async def settle_trade_pessimistic(
        self,
        taker_balance: Balance,
        maker_balance: Balance,
        amount: Decimal,
        taker_order_id: int,
        maker_order_id: int,
    ) -> None:
        """Atomically move `amount` from taker's locked → maker's available.

        Used by the matching worker when a trade executes:
          1. Taker's locked asset is settled (deducted from locked + total)
          2. Maker's same asset is credited (added to total + available)

        Both balances must already be locked with FOR UPDATE.
        """
        self.repo.settle_pessimistic(taker_balance, amount)
        self.repo.credit_pessimistic(maker_balance, amount)
        await self.session.flush()
        # Publish both updates
        await self._publish_balance_update(taker_balance, Decimal("0"), "trade_settled", taker_order_id)
        await self._publish_balance_update(maker_balance, amount, "trade_received", maker_order_id)

    # ─── Reads ──────────────────────────────────────────────────────────────

    async def get_balance(self, user_id: int, asset: str) -> Balance | None:
        """Read a balance (no lock)."""
        return await self.repo.get_by_user_asset(user_id, asset)

    async def get_all_balances(self, user_id: int) -> list[Balance]:
        """All balances for a user."""
        return list(await self.repo.get_all_for_user(user_id))

    # ─── Admin operations ───────────────────────────────────────────────────

    async def admin_adjust(
        self,
        user_id: int,
        asset: str,
        delta: Decimal,
        reason: str = "admin_adjustment",
    ) -> Balance:
        """Admin: adjust a user's total_balance by `delta` (signed).

        Positive delta = credit (total + available increase).
        Negative delta = debit (total + available decrease, fails if insufficient).

        If the user has locked balance (open orders), the available balance
        is adjusted by the same delta, but locked is untouched. This means:
          - Credit: available += |delta|, total += |delta|
          - Debit:  available -= |delta|, total -= |delta| (fails if available < |delta|)
        """
        balance = await self.repo.get_for_update(user_id, asset)
        if balance is None:
            balance = await self.repo.get_or_create(user_id, asset)
            # Re-lock after create
            balance = await self.repo.get_for_update(user_id, asset)
            assert balance is not None

        if delta >= 0:
            balance.total_balance += delta
            balance.available_balance += delta
        else:
            abs_delta = -delta
            if balance.available_balance < abs_delta:
                raise InsufficientBalanceError(
                    user_id, asset, abs_delta, balance.available_balance
                )
            balance.total_balance -= abs_delta
            balance.available_balance -= abs_delta

        balance.version += 1
        balance.check_invariant()
        await self.session.flush()

        await self._publish_balance_update(
            balance, delta, "admin_adjustment" if reason == "admin_adjustment" else "deposit", None,  # type: ignore[arg-type]
        )
        return balance

    # ─── Pub/Sub ────────────────────────────────────────────────────────────

    async def _publish_balance_update(
        self,
        balance: Balance,
        change: Decimal,
        reason: str,
        order_id: int | None,
    ) -> None:
        """Publish a balance update to the user's private WS channel.

        Best-effort: if Redis is down, we log but don't fail the operation
        (the DB state is the source of truth).
        """
        try:
            redis = get_redis()
            await pubsub.publish_balance_update(
                redis,
                user_id=balance.user_id,
                asset=balance.asset,
                total=float(balance.total_balance),
                locked=float(balance.locked_balance),
                available=float(balance.available_balance),
                change=float(change) if change != Decimal("0") else None,
                reason=reason,
                order_id=order_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to publish balance update for user=%d asset=%s: %s",
                balance.user_id, balance.asset, e,
            )


__all__ = [
    "BalanceService",
    "InsufficientBalanceError",
    "BalanceVersionConflict",
    "LockReason",
    "UnlockReason",
    "SettleReason",
    "CreditReason",
]
