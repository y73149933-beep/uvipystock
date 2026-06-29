"""FastAPI application entry point.

Wires up:
  * V1 API router (orders, balance, trades, WS)
  * Admin API router (users, balances, market, api-keys, emulator)
  * Exception handlers
  * Redis connection pool + WS subscriber startup
  * Stop monitor service startup
  * Logging configuration
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin.router import router as admin_router
from app.api.v1.router import router as v1_router
from app.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.redis_client import init_redis
from app.services.stop_monitor_service import StopMonitorService
from app.ws.manager import ws_manager

logger = logging.getLogger(__name__)
_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup + shutdown hooks."""
    setup_logging()
    logger.info("Starting %s (env=%s)", _settings.app_env, _settings.app_env)

    # 1. Initialize Redis
    init_redis(app)
    # Manually trigger startup event since we're using lifespan
    from app.redis_client.client import get_redis
    redis = get_redis()
    try:
        await redis.ping()
        logger.info("Redis connected: %s", _settings.redis_url)
    except Exception as e:
        logger.error("Redis connection failed: %s", e)

    # 2. Run database seed (creates default admin + trading pairs + demo trader)
    #    This runs AFTER Alembic migrations (which are run by the Dockerfile CMD
    #    before uvicorn starts). The seed is idempotent.
    try:
        from app.db.session import async_session_factory
        from app.db.seed import seed_database
        async with async_session_factory() as session:
            await seed_database(session)
    except Exception as e:
        logger.warning("Database seed failed (tables may not exist yet): %s", e)

    # 3. Start WS subscribers for active symbols
    from app.repositories.trading_pair_repo import TradingPairRepository
    from app.db.session import async_session_factory
    async with async_session_factory() as session:
        pair_repo = TradingPairRepository(session)
        pairs = await pair_repo.list_active()
        symbols = [p.symbol for p in pairs]

    if symbols:
        await ws_manager.start_subscribers(symbols)
        logger.info("WS subscribers started for %d symbols", len(symbols))

    # 4. Start stop monitor
    stop_monitor = StopMonitorService()
    if symbols:
        await stop_monitor.start(symbols)
        app.state.stop_monitor = stop_monitor
        logger.info("Stop monitor started for %d symbols", len(symbols))

    app.state.ws_manager = ws_manager

    yield

    # Shutdown
    logger.info("Shutting down...")
    if hasattr(app.state, "stop_monitor"):
        await app.state.stop_monitor.stop()
    await ws_manager.stop_subscribers()
    from app.redis_client.client import close_redis
    await close_redis()
    from app.db.session import dispose_engine
    await dispose_engine()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Crypto Exchange Sandbox",
        description="Educational paper-trading crypto exchange with Cython matching engine",
        version="0.1.0",
        lifespan=lifespan,
        # Disable default 500 handler so ours takes over
        exception_handlers={},
    )

    # Register routers
    app.include_router(v1_router)
    app.include_router(admin_router)

    # Register exception handlers
    register_exception_handlers(app)

    # CORS (permissive for development; tighten in production)
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # TODO: restrict in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # NOTE: Do NOT use @app.middleware("http") — it breaks WebSocket (403).
    # Rate limiting is enforced via the enforce_rate_limit dependency.

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=_settings.app_host,
        port=_settings.app_port,
        reload=_settings.is_dev,
        log_level=_settings.app_log_level.lower(),
    )
