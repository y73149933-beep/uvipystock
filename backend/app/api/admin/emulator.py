"""Admin market emulator endpoints — Random Walk + manual trade injection."""
from __future__ import annotations

import asyncio
import logging
import random
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends

from app.api.deps import AdminUser, SessionDep
from app.db.session import async_session_factory
from app.models.enums import OrderSide
from app.redis_client import get_redis
from app.redis_client import pubsub
from app.schemas.admin import AdminEmulatorRandomWalkRequest, AdminEmulatorTradeInjectRequest
from app.repositories.trade_repo import TradeRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/emulator", tags=["admin-emulator"])


@router.post("/random-walk")
async def random_walk(
    admin: AdminUser,
    session: SessionDep,
    body: AdminEmulatorRandomWalkRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Generate random price movement by injecting synthetic trades.

    Runs in the background; returns immediately with a job ID.
    """
    job_id = f"rw_{body.symbol}_{int(__import__('time').time())}"
    background_tasks.add_task(_run_random_walk, body)
    return {"job_id": job_id, "status": "started", "symbol": body.symbol, "steps": body.steps}


async def _run_random_walk(req: AdminEmulatorRandomWalkRequest) -> None:
    """Background task: inject `steps` synthetic trades with random prices."""
    redis = get_redis()
    price = float(req.start_price)
    vol = float(req.volatility_pct) / 100.0

    for step in range(req.steps):
        # Random walk: price *= (1 + N(0, vol))
        shock = random.gauss(0, vol)
        price = max(0.01, price * (1 + shock))
        qty = round(random.uniform(0.001, 0.5), 6)
        side = "buy" if random.random() > 0.5 else "sell"

        await pubsub.publish_trade(
            redis, symbol=req.symbol, trade_id=-1,  # synthetic ID
            price=price, quantity=qty, side=side,
        )

        await asyncio.sleep(req.interval_ms / 1000.0)

    logger.info("Random walk completed for %s (%d steps)", req.symbol, req.steps)


@router.post("/trade-inject")
async def inject_trade(
    admin: AdminUser,
    session: SessionDep,
    body: AdminEmulatorTradeInjectRequest,
) -> dict:
    """Inject a single synthetic trade print (for testing stop triggers + charts)."""
    redis = get_redis()
    await pubsub.publish_trade(
        redis, symbol=body.symbol, trade_id=-1,
        price=float(body.price), quantity=float(body.quantity),
        side=body.side,
    )
    return {"status": "injected", "symbol": body.symbol, "price": str(body.price)}


__all__ = ["router"]
