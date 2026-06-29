"""Balance repository with row-level locking helpers.

Critical: every mutation of `locked_balance` / `available_balance` MUST
go through one of these helpers, which:

  1. Load the row with `SELECT ... FOR UPDATE` (pessimistic) OR use
     `version`-based optimistic locking (UPDATE ... WHERE version = X).
  2. Maintain the invariant `total = locked + available`.
  3. Bump `version` on every UPDATE.

The service layer chooses between pessimistic and optimistic locking
based on the contention profile. For the matching worker (high contention
on the maker's balance), pessimistic `FOR UPDATE` is safer; for the API
layer's order placement (low contention), optimistic locking is faster.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import Balance
from app.repositories.base import BaseRepository


class BalanceRepository(BaseRepository[Balance]):
    model = Balance

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ─── Reads ───────────────────────────────────────────────────────────────

    async def get_by_user_asset(
        self,
        user_id: int,
        asset: str,
    ) -> Balance | None:
        """Fetch a balance by (user_id, asset)."""
        stmt = select(Balance).where(
            Balance.user_id == user_id,
            Balance.asset == asset,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_update(
        self,
        user_id: int,
        asset: str,
    ) -> Balance | None:
        """Fetch a balance with SELECT ... FOR UPDATE (pessimistic lock).

        MUST be called inside a transaction. The lock is held until commit
        or rollback.
        """
        stmt = (
            select(Balance)
            .where(Balance.user_id == user_id, Balance.asset == asset)
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_for_user(self, user_id: int) -> Sequence[Balance]:
        """All balances for a user."""
        stmt = select(Balance).where(Balance.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_for_update_multi(
        self,
        user_id: int,
        assets: list[str],
    ) -> dict[str, Balance]:
        """Lock multiple balances atomically.

        IMPORTANT: to avoid deadlocks, sort `assets` before locking so all
        transactions acquire locks in the same order. The caller is
        responsible for passing a sorted list.
        """
        stmt = (
            select(Balance)
            .where(Balance.user_id == user_id, Balance.asset.in_(assets))
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        return {b.asset: b for b in result.scalars().all()}

    # ─── Optimistic-lock updates ────────────────────────────────────────────
    # These issue a single UPDATE ... WHERE version = X and check rowcount.
    # If rowcount == 0, another transaction modified the row → caller retries.

    async def optimistic_lock(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        expected_version: int,
    ) -> bool:
        """Atomically move `amount` from available → locked.

        Returns True on success (row updated), False if the version check
        failed (concurrent modification — caller should retry).

        The UPDATE also enforces `available_balance >= amount` to prevent
        overdraft even if the version check passes.
        """
        stmt = (
            update(Balance)
            .where(
                Balance.user_id == user_id,
                Balance.asset == asset,
                Balance.version == expected_version,
                Balance.available_balance >= amount,
            )
            .values(
                locked_balance=Balance.locked_balance + amount,
                available_balance=Balance.available_balance - amount,
                version=Balance.version + 1,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def optimistic_unlock(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        expected_version: int,
    ) -> bool:
        """Atomically move `amount` from locked → available (cancel order)."""
        stmt = (
            update(Balance)
            .where(
                Balance.user_id == user_id,
                Balance.asset == asset,
                Balance.version == expected_version,
                Balance.locked_balance >= amount,
            )
            .values(
                locked_balance=Balance.locked_balance - amount,
                available_balance=Balance.available_balance + amount,
                version=Balance.version + 1,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def optimistic_settle(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        expected_version: int,
    ) -> bool:
        """Atomically deduct `amount` from locked (trade settled, taker side).

        Used when a taker order fills: the locked amount is consumed and
        the user no longer owns that asset (it moves to the counterparty).
        """
        stmt = (
            update(Balance)
            .where(
                Balance.user_id == user_id,
                Balance.asset == asset,
                Balance.version == expected_version,
                Balance.locked_balance >= amount,
            )
            .values(
                locked_balance=Balance.locked_balance - amount,
                total_balance=Balance.total_balance - amount,
                version=Balance.version + 1,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def optimistic_credit(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        expected_version: int,
    ) -> bool:
        """Atomically credit `amount` to total + available (maker received asset)."""
        stmt = (
            update(Balance)
            .where(
                Balance.user_id == user_id,
                Balance.asset == asset,
                Balance.version == expected_version,
            )
            .values(
                total_balance=Balance.total_balance + amount,
                available_balance=Balance.available_balance + amount,
                version=Balance.version + 1,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    # ─── Pessimistic-lock helpers ───────────────────────────────────────────
    # These operate on an already-loaded, FOR UPDATE-locked Balance object.
    # The caller is responsible for acquiring the lock via get_for_update().

    @staticmethod
    def lock_pessimistic(balance: Balance, amount: Decimal) -> None:
        """Move `amount` from available → locked on a locked Balance row.

        Raises ValueError if insufficient available balance (overdraft).
        The caller must have loaded `balance` with `get_for_update()`.
        """
        if amount < 0:
            raise ValueError(f"lock amount must be non-negative, got {amount}")
        if balance.available_balance < amount:
            raise ValueError(
                f"Insufficient available balance for user={balance.user_id} "
                f"asset={balance.asset}: need {amount}, have {balance.available_balance}"
            )
        balance.available_balance -= amount
        balance.locked_balance += amount
        balance.version += 1
        balance.check_invariant()

    @staticmethod
    def unlock_pessimistic(balance: Balance, amount: Decimal) -> None:
        """Move `amount` from locked → available (cancel)."""
        if amount < 0:
            raise ValueError(f"unlock amount must be non-negative, got {amount}")
        if balance.locked_balance < amount:
            raise ValueError(
                f"Insufficient locked balance for user={balance.user_id} "
                f"asset={balance.asset}: need {amount}, have {balance.locked_balance}"
            )
        balance.locked_balance -= amount
        balance.available_balance += amount
        balance.version += 1
        balance.check_invariant()

    @staticmethod
    def settle_pessimistic(balance: Balance, amount: Decimal) -> None:
        """Deduct `amount` from locked + total (taker's locked asset consumed)."""
        if amount < 0:
            raise ValueError(f"settle amount must be non-negative, got {amount}")
        if balance.locked_balance < amount:
            raise ValueError(
                f"Insufficient locked balance for settle: user={balance.user_id} "
                f"asset={balance.asset}: need {amount}, have {balance.locked_balance}"
            )
        balance.locked_balance -= amount
        balance.total_balance -= amount
        balance.version += 1
        balance.check_invariant()

    @staticmethod
    def credit_pessimistic(balance: Balance, amount: Decimal) -> None:
        """Credit `amount` to total + available (maker received asset)."""
        if amount < 0:
            raise ValueError(f"credit amount must be non-negative, got {amount}")
        balance.total_balance += amount
        balance.available_balance += amount
        balance.version += 1
        balance.check_invariant()

    # ─── Creation ────────────────────────────────────────────────────────────

    async def get_or_create(self, user_id: int, asset: str) -> Balance:
        """Fetch a balance or create a zero-balance row if missing."""
        balance = await self.get_by_user_asset(user_id, asset)
        if balance is None:
            balance = Balance(
                user_id=user_id,
                asset=asset,
                total_balance=Decimal("0"),
                locked_balance=Decimal("0"),
                available_balance=Decimal("0"),
            )
            self.session.add(balance)
            await self.session.flush()
        return balance
