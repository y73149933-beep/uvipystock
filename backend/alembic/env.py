"""Alembic environment configured for async SQLAlchemy 2.0 + asyncpg.

The migration URL is read from `app.config.Settings` (which itself reads
from env / .env), so the same Docker image can be pointed at any DB by
changing a single env var.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from sqlalchemy.engine import make_url

# ─── Alembic config objects ──────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ─── Pull in metadata from all models ────────────────────────────────────────
# IMPORTANT: importing `app.models` registers every mapper on `Base.metadata`.
# Without this import, autogenerate would emit empty migrations.
import sys  # noqa: E402

from pathlib import Path  # noqa: E402

# Make `backend/` importable when running `alembic` from project root.
_BACKEND_DIR = str(Path(__file__).resolve().parents[2])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  — registers mappers

target_metadata = Base.metadata

# ─── Override URL from Settings (so .env / env vars win) ─────────────────────
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.effective_database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout without a DB.

    Useful for CI pipelines that need to review generated SQL.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure context and run migrations inside an existing connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _describe_db_target() -> str:
    """Masked connection string for error messages (password hidden)."""
    try:
        url = make_url(config.get_main_option("sqlalchemy.url"))
        return url.render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — diagnostics must never mask the real error
        return "<unparseable sqlalchemy.url>"


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    except OSError as exc:
        # socket.gaierror (-2 "Name or service not known"), ConnectionRefused, etc.
        # → infrastructure-level failure: fail fast with actionable hints instead
        # of a raw asyncpg traceback.
        raise RuntimeError(
            f"Cannot connect to PostgreSQL. Target: {_describe_db_target()}\n"
            "Hints:\n"
            "  1) Is the DB container up and healthy?   -> docker compose ps postgres\n"
            "     (Docker DNS resolves service aliases only for RUNNING containers)\n"
            "  2) Does 'backend' resolve the DB host?   -> "
            "docker compose exec backend getent hosts postgres\n"
            "  3) Are DATABASE_URL / POSTGRES_HOST set to the compose SERVICE name,\n"
            "     and were both services raised by the same 'docker compose up'?\n"
            f"Original error: {exc!r}"
        ) from exc
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online mode — dispatches to the async runner."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
