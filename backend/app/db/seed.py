"""Database seed script — creates default admin, trading pairs, demo traders.

Runs AFTER Alembic migrations on backend startup (called from main.py lifespan).
Idempotent: uses ON CONFLICT DO NOTHING so it's safe to run multiple times.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import Balance
from app.models.trading_pair import TradingPair
from app.models.user import User

logger = logging.getLogger(__name__)

DEFAULT_PASSWORD_HASH = "$2b$12$YoOvlVHqBqmjojR56Om.WOjhr6ILQj.0mmZXs8bwBfDidn.2t4bla"
TEST_PASSWORD_HASH = "$2b$12$Eq1doW2/woemUOk4jajY4OuK/28ynaYs26KD1BNcB8px73BPQOSZW"


async def _ensure_user(session: AsyncSession, login: str, password_hash: str, is_admin: bool = False) -> User:
    from sqlalchemy import select
    result = await session.execute(select(User).where(User.email == login))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=login, password_hash=password_hash, is_active=True, is_admin=is_admin)
        session.add(user)
        await session.flush()
        logger.info("Seeded user: %s (admin=%s)", login, is_admin)
    else:
        if not user.password_hash or not user.password_hash.startswith("$2b$"):
            user.password_hash = password_hash
    return user


async def _ensure_balance(session: AsyncSession, user_id: int, asset: str, amount: Decimal) -> None:
    from sqlalchemy import select
    result = await session.execute(
        select(Balance).where(Balance.user_id == user_id, Balance.asset == asset)
    )
    if result.scalar_one_or_none() is None:
        session.add(Balance(
            user_id=user_id, asset=asset,
            total_balance=amount, locked_balance=Decimal("0"), available_balance=amount,
        ))
        logger.info("Seeded balance: user=%d %s=%s", user_id, asset, amount)


async def seed_database(session: AsyncSession) -> None:
    await _ensure_user(session, "admin", DEFAULT_PASSWORD_HASH, is_admin=True)
    test_user = await _ensure_user(session, "test", TEST_PASSWORD_HASH, is_admin=False)
    test2_user = await _ensure_user(session, "test2", TEST_PASSWORD_HASH, is_admin=False)

    await _ensure_balance(session, test_user.id, "USDT", Decimal("100000"))
    await _ensure_balance(session, test_user.id, "BTC", Decimal("10"))
    await _ensure_balance(session, test_user.id, "RUR", Decimal("5000000"))
    await _ensure_balance(session, test_user.id, "ORION", Decimal("1000"))
    await _ensure_balance(session, test2_user.id, "USDT", Decimal("100000"))
    await _ensure_balance(session, test2_user.id, "BTC", Decimal("10"))
    await _ensure_balance(session, test2_user.id, "RUR", Decimal("5000000"))
    await _ensure_balance(session, test2_user.id, "ORION", Decimal("1000"))

    pairs_data = [
        # Криптовалюты
        ("BTC/USDT", "BTC", "USDT", 2, 8),
        ("ETH/USDT", "ETH", "USDT", 2, 8),
        ("SOL/USDT", "SOL", "USDT", 2, 8),
        ("BNB/USDT", "BNB", "USDT", 2, 8),
        # Фиатные пары
        ("BTC/RUR", "BTC", "RUR", 2, 8),
        # Акции
        ("ORION/USDT", "ORION", "USDT", 2, 4),
        ("ORION/RUR", "ORION", "RUR", 2, 4),
    ]
    for symbol, base, quote, price_p, qty_p in pairs_data:
        from sqlalchemy import select
        result = await session.execute(select(TradingPair).where(TradingPair.symbol == symbol))
        if result.scalar_one_or_none() is None:
            session.add(TradingPair(
                symbol=symbol, base_asset=base, quote_asset=quote,
                price_precision=price_p, quantity_precision=qty_p,
                min_lot_size=Decimal("0.0001"), max_lot_size=Decimal("1000"),
                tick_size=Decimal("0.01"),
                maker_fee_bps=Decimal("0"), taker_fee_bps=Decimal("0"), is_active=True,
            ))
            logger.info("Seeded trading pair: %s", symbol)

    await session.commit()
    logger.info("Database seed completed")


__all__ = ["seed_database"]
