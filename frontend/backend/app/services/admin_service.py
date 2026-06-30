"""Admin service — user management, balance adjustments, market management.

All methods require an admin-privileged session (verified by the API layer
before calling these services).
"""
from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey
from app.models.balance import Balance
from app.models.trading_pair import TradingPair
from app.models.user import User
from app.redis_client import get_redis
from app.redis_client import orderbook
from app.repositories.api_key_repo import ApiKeyRepository  # type: ignore  # noqa: F401
from app.repositories.balance_repo import BalanceRepository
from app.repositories.trading_pair_repo import TradingPairRepository
from app.repositories.user_repo import UserRepository
from app.services.balance_service import BalanceService

logger = logging.getLogger(__name__)


class AdminService:
    """Admin operations: users, balances, market, emulation."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.user_repo = UserRepository(session)
        self.balance_repo = BalanceRepository(session)
        self.pair_repo = TradingPairRepository(session)
        self.balance_svc = BalanceService(session)

    # ─── User management ────────────────────────────────────────────────────

    async def create_user(
        self,
        email: str,
        password_hash: str,
        is_admin: bool = False,
    ) -> User:
        """Create a new user."""
        existing = await self.user_repo.get_by_email(email)
        if existing is not None:
            raise ValueError(f"User with email {email!r} already exists")
        user = await self.user_repo.create(
            email=email,
            password_hash=password_hash,
            is_admin=is_admin,
            is_active=True,
        )
        return user

    async def get_user(self, user_id: int) -> User | None:
        return await self.user_repo.get(user_id)

    async def list_users(self, offset: int = 0, limit: int = 50) -> Sequence[User]:
        return await self.user_repo.get_multi(offset=offset, limit=limit)

    async def toggle_user_active(self, user_id: int, is_active: bool) -> User | None:
        """Activate or deactivate a user."""
        return await self.user_repo.update(user_id, is_active=is_active)

    # ─── Balance management ─────────────────────────────────────────────────

    async def adjust_balance(
        self,
        user_id: int,
        asset: str,
        delta: Decimal,
        reason: str = "admin_adjustment",
    ) -> Balance:
        """Adjust a user's balance by `delta` (signed).

        Positive = credit, negative = debit. Maintains the
        total = locked + available invariant. If the user has locked
        balance (open orders), only available is adjusted.
        """
        return await self.balance_svc.admin_adjust(user_id, asset, delta, reason)

    async def get_user_balances(self, user_id: int) -> list[Balance]:
        return await self.balance_svc.get_all_balances(user_id)

    async def credit_user(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
    ) -> Balance:
        """Convenience: credit a positive amount."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        return await self.adjust_balance(user_id, asset, amount, reason="admin_credit")

    async def debit_user(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
    ) -> Balance:
        """Convenience: debit a positive amount."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        return await self.adjust_balance(user_id, asset, -amount, reason="admin_debit")

    # ─── Market management ──────────────────────────────────────────────────

    async def create_trading_pair(
        self,
        symbol: str,
        base_asset: str,
        quote_asset: str,
        price_precision: int,
        quantity_precision: int,
        min_lot_size: Decimal,
        max_lot_size: Decimal,
        tick_size: Decimal,
        maker_fee_bps: Decimal = Decimal("0"),
        taker_fee_bps: Decimal = Decimal("0"),
    ) -> TradingPair:
        """Create a new trading pair."""
        existing = await self.pair_repo.get_by_symbol(symbol)
        if existing is not None:
            raise ValueError(f"Trading pair {symbol!r} already exists")

        pair = await self.pair_repo.create(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            price_precision=price_precision,
            quantity_precision=quantity_precision,
            min_lot_size=min_lot_size,
            max_lot_size=max_lot_size,
            tick_size=tick_size,
            maker_fee_bps=maker_fee_bps,
            taker_fee_bps=taker_fee_bps,
            is_active=True,
        )
        return pair

    async def toggle_pair_active(self, pair_id: int, is_active: bool) -> TradingPair | None:
        """Activate or deactivate a trading pair."""
        return await self.pair_repo.update(pair_id, is_active=is_active)

    async def list_trading_pairs(self) -> Sequence[TradingPair]:
        return await self.pair_repo.list_active()

    # ─── API key management ─────────────────────────────────────────────────

    async def create_api_key(
        self,
        user_id: int,
        label: str | None = None,
        permissions: list[str] | None = None,
        rate_limit_per_min: int = 120,
    ) -> tuple[ApiKey, str]:
        """Generate a new API keypair for a user.

        Returns (ApiKey_row, raw_secret). The raw_secret is only available
        at creation time — it's stored as a bcrypt hash in the DB.
        """
        raw_key = secrets.token_hex(16)  # 32-char hex
        raw_secret = secrets.token_hex(32)  # 64-char hex

        # In production, hash the secret with bcrypt. For the sandbox we
        # store a simple hash (the security module will handle this properly
        # in Step 2e).
        from app.core.security import hash_api_secret  # type: ignore
        try:
            secret_hash = hash_api_secret(raw_secret)
        except ImportError:
            # Fallback if security module not yet implemented
            import hashlib
            secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()

        api_key = ApiKey(
            user_id=user_id,
            api_key=raw_key,
            secret_hash=secret_hash,
            label=label,
            permissions=permissions or ["trade", "read", "ws"],
            rate_limit_per_min=rate_limit_per_min,
        )
        self.session.add(api_key)
        await self.session.flush()
        return api_key, raw_secret

    async def revoke_api_key(self, api_key_id: int) -> bool:
        """Revoke an API key."""
        from sqlalchemy import update as sa_update
        from app.models.api_key import ApiKey as ApiKeyModel
        stmt = (
            sa_update(ApiKeyModel)
            .where(ApiKeyModel.id == api_key_id)
            .values(is_revoked=True)
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1


__all__ = ["AdminService"]
