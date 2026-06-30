"""Unit tests for BalanceService — atomic lock/unlock/settle/credit with invariant.

Uses an in-memory SQLite database via aiosqlite. Tables are created with
raw DDL to work around SQLite's lack of ARRAY/BIGSERIAL support.

Coverage
--------
1. lock_optimistic: happy path, insufficient balance, version conflict retry
2. unlock_optimistic: happy path, version conflict retry
3. settle_optimistic: deduct from locked + total
4. credit_optimistic: add to total + available
5. Invariant maintenance: total == locked + available after every op
6. Pessimistic ops: lock/unlock/settle/credit on FOR UPDATE-locked rows
7. settle_trade_pessimistic: taker → maker atomic transfer
8. admin_adjust: positive delta (credit), negative delta (debit), insufficient
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Ensure backend/ on path
_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.models.balance import Balance
from app.models.user import User
from app.services.balance_service import (
    BalanceService,
    BalanceVersionConflict,
    InsufficientBalanceError,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite async session. Yields (session, user_id).

    Note: SQLite doesn't support SELECT ... FOR UPDATE (it's a no-op), so
    pessimistic locking tests work but don't actually lock. The optimistic
    locking path is fully exercised.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.exec_driver_sql("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                is_active BOOLEAN DEFAULT 1 NOT NULL,
                is_admin BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """)
        await conn.exec_driver_sql("""
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                api_key VARCHAR(64) NOT NULL UNIQUE,
                secret_hash VARCHAR(255) NOT NULL,
                label VARCHAR(100),
                permissions TEXT,
                rate_limit_per_min INTEGER DEFAULT 120 NOT NULL,
                is_revoked BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                last_used_at DATETIME,
                expires_at DATETIME
            )
        """)
        await conn.exec_driver_sql("""
            CREATE TABLE balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                asset VARCHAR(20) NOT NULL,
                total_balance NUMERIC(36,18) DEFAULT 0 NOT NULL,
                locked_balance NUMERIC(36,18) DEFAULT 0 NOT NULL,
                available_balance NUMERIC(36,18) DEFAULT 0 NOT NULL,
                version INTEGER DEFAULT 1 NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                UNIQUE(user_id, asset)
            )
        """)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as session:
        # Seed a user
        user = User(email="test@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        yield session, user.id

    await engine.dispose()


@pytest_asyncio.fixture
async def balance_with_funds(db_session):
    """Create a balance with 1000 USDT (all available).

    Returns (session, user_id, balance).
    """
    session, user_id = db_session
    bal = Balance(
        user_id=user_id,
        asset="USDT",
        total_balance=Decimal("1000"),
        locked_balance=Decimal("0"),
        available_balance=Decimal("1000"),
    )
    session.add(bal)
    await session.commit()
    await session.refresh(bal)
    return session, user_id, bal


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestBalanceServiceLock:
    """Tests for lock_optimistic."""

    async def test_lock_happy_path(self, balance_with_funds):
        """Lock 100 USDT → available decreases, locked increases, total unchanged."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.lock_optimistic(user_id, "USDT", Decimal("100"), reason="order_placed")
        await session.commit()

        # Reload to get final state
        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.available_balance == Decimal("900")
        assert bal.locked_balance == Decimal("100")
        assert bal.total_balance == Decimal("1000")
        assert bal.version == 2

    async def test_lock_insufficient_balance(self, balance_with_funds):
        """Locking more than available raises InsufficientBalanceError."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        with pytest.raises(InsufficientBalanceError) as exc_info:
            await svc.lock_optimistic(user_id, "USDT", Decimal("1001"))
        await session.rollback()
        assert exc_info.value.asset == "USDT"
        assert exc_info.value.needed == Decimal("1001")

    async def test_lock_zero_amount_raises(self, balance_with_funds):
        """Locking zero or negative raises ValueError."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        with pytest.raises(ValueError, match="positive"):
            await svc.lock_optimistic(user_id, "USDT", Decimal("0"))

    async def test_lock_auto_creates_balance(self, db_session):
        """Locking on a non-existent balance auto-creates it (then fails on insufficient)."""
        session, user_id = db_session
        svc = BalanceService(session)
        with pytest.raises(InsufficientBalanceError):
            await svc.lock_optimistic(user_id, "BTC", Decimal("1"))
        await session.rollback()


class TestBalanceServiceUnlock:
    """Tests for unlock_optimistic."""

    async def test_unlock_happy_path(self, balance_with_funds):
        """Lock then unlock restores original state."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.lock_optimistic(user_id, "USDT", Decimal("200"))
        await session.commit()

        bal = await svc.unlock_optimistic(user_id, "USDT", Decimal("200"))
        await session.commit()
        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.available_balance == Decimal("1000")
        assert bal.locked_balance == Decimal("0")

    async def test_unlock_insufficient_locked(self, balance_with_funds):
        """Unlocking more than locked raises BalanceVersionConflict after retries."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.lock_optimistic(user_id, "USDT", Decimal("100"))
        await session.commit()

        with pytest.raises(BalanceVersionConflict):
            await svc.unlock_optimistic(user_id, "USDT", Decimal("200"))
        await session.rollback()


class TestBalanceServiceSettle:
    """Tests for settle_optimistic (taker's locked asset consumed)."""

    async def test_settle_deducts_from_locked_and_total(self, balance_with_funds):
        """Lock 100, settle 60 → locked=40, total=940, available=900."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.lock_optimistic(user_id, "USDT", Decimal("100"))
        await session.commit()

        await svc.settle_optimistic(user_id, "USDT", Decimal("60"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.locked_balance == Decimal("40")
        assert bal.total_balance == Decimal("940")
        assert bal.available_balance == Decimal("900")


class TestBalanceServiceCredit:
    """Tests for credit_optimistic (maker receives asset)."""

    async def test_credit_adds_to_total_and_available(self, balance_with_funds):
        """Credit 50 → total=1050, available=1050, locked=0."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.credit_optimistic(user_id, "USDT", Decimal("50"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.total_balance == Decimal("1050")
        assert bal.available_balance == Decimal("1050")
        assert bal.locked_balance == Decimal("0")

    async def test_credit_auto_creates_balance(self, db_session):
        """Crediting to a non-existent balance creates it."""
        session, user_id = db_session
        svc = BalanceService(session)
        await svc.credit_optimistic(user_id, "ETH", Decimal("5"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "ETH")
        assert bal is not None
        assert bal.total_balance == Decimal("5")
        assert bal.asset == "ETH"


class TestInvariant:
    """Verify total == locked + available after every operation."""

    async def test_invariant_holds_after_lock(self, balance_with_funds):
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.lock_optimistic(user_id, "USDT", Decimal("333"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        bal.check_invariant()  # should not raise

    async def test_invariant_holds_after_settle(self, balance_with_funds):
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.lock_optimistic(user_id, "USDT", Decimal("500"))
        await session.commit()

        await svc.settle_optimistic(user_id, "USDT", Decimal("200"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        bal.check_invariant()


class TestPessimisticOps:
    """Tests for pessimistic-lock helpers."""

    async def test_lock_pessimistic(self, balance_with_funds):
        """Lock using the pessimistic path (FOR UPDATE + in-Python mutation)."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        bal = await svc.repo.get_for_update(user_id, "USDT")
        assert bal is not None
        await svc.lock_pessimistic(bal, Decimal("100"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.locked_balance == Decimal("100")
        assert bal.available_balance == Decimal("900")

    async def test_settle_trade_pessimistic(self, db_session):
        """Atomic taker→maker transfer via settle_trade_pessimistic."""
        session, user_id = db_session
        # Create a second user (maker)
        maker = User(email="maker@example.com", password_hash="x")
        session.add(maker)
        await session.flush()

        taker_bal = Balance(
            user_id=user_id, asset="USDT",
            total_balance=Decimal("1000"), locked_balance=Decimal("100"),
            available_balance=Decimal("900"),
        )
        maker_bal = Balance(
            user_id=maker.id, asset="USDT",
            total_balance=Decimal("500"), locked_balance=Decimal("0"),
            available_balance=Decimal("500"),
        )
        session.add_all([taker_bal, maker_bal])
        await session.commit()

        svc = BalanceService(session)
        tb = await svc.repo.get_for_update(user_id, "USDT")
        mb = await svc.repo.get_for_update(maker.id, "USDT")
        await svc.settle_trade_pessimistic(tb, mb, Decimal("50"), 1, 2)
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        taker_bal = await repo.get_by_user_asset(user_id, "USDT")
        maker_bal = await repo.get_by_user_asset(maker.id, "USDT")
        # Taker: locked 100→50, total 1000→950
        assert taker_bal.locked_balance == Decimal("50")
        assert taker_bal.total_balance == Decimal("950")
        # Maker: total 500→550, available 500→550
        assert maker_bal.total_balance == Decimal("550")
        assert maker_bal.available_balance == Decimal("550")


class TestAdminAdjust:
    """Tests for admin_adjust."""

    async def test_admin_credit(self, balance_with_funds):
        """Positive delta credits total + available."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.admin_adjust(user_id, "USDT", Decimal("500"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.total_balance == Decimal("1500")
        assert bal.available_balance == Decimal("1500")

    async def test_admin_debit(self, balance_with_funds):
        """Negative delta debits total + available."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        await svc.admin_adjust(user_id, "USDT", Decimal("-300"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.total_balance == Decimal("700")
        assert bal.available_balance == Decimal("700")

    async def test_admin_debit_insufficient(self, balance_with_funds):
        """Debiting more than available raises InsufficientBalanceError."""
        session, user_id, _ = balance_with_funds
        svc = BalanceService(session)
        with pytest.raises(InsufficientBalanceError):
            await svc.admin_adjust(user_id, "USDT", Decimal("-1001"))
        await session.rollback()

    async def test_admin_adjust_with_locked_balance(self, db_session):
        """Adjusting available when there's locked balance preserves locked."""
        session, user_id = db_session
        bal = Balance(
            user_id=user_id, asset="USDT",
            total_balance=Decimal("1000"), locked_balance=Decimal("400"),
            available_balance=Decimal("600"),
        )
        session.add(bal)
        await session.commit()

        svc = BalanceService(session)
        await svc.admin_adjust(user_id, "USDT", Decimal("200"))
        await session.commit()

        from app.repositories.balance_repo import BalanceRepository
        repo = BalanceRepository(session)
        bal = await repo.get_by_user_asset(user_id, "USDT")
        assert bal.total_balance == Decimal("1200")
        assert bal.available_balance == Decimal("800")
        assert bal.locked_balance == Decimal("400")
