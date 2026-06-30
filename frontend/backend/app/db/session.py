"""Async SQLAlchemy session factory.

Usage
-----
.. code-block:: python

    from app.db.session import async_session_factory

    async with async_session_factory() as session:
        async with session.begin():
            # ... do work ...
            pass

The session factory is created once at import time. The engine pool size
is configurable via `Settings.db_pool_size`.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_settings = get_settings()

# ─── Engine ──────────────────────────────────────────────────────────────────
engine: AsyncEngine = create_async_engine(
    _settings.effective_database_url,
    echo=_settings.db_echo,
    pool_pre_ping=True,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout,
    future=True,
)

# ─── Session factory ─────────────────────────────────────────────────────────
# `expire_on_commit=False` so attributes remain accessible after commit.
# SQLAlchemy 2.0 async sessions auto-begin a transaction on first query;
# route handlers that need an explicit transaction should use
# ``async with session.begin():`` which handles nested transactions correctly.
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncSession:
    """FastAPI dependency yielding an async session.

    Usage in a route::

        @router.get("/...")
        async def handler(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Call at app shutdown to release all pooled connections."""
    await engine.dispose()
