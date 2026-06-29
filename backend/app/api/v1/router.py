"""V1 API router — aggregates all v1 sub-routers."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, balance, orders, pairs, trades, ws

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(pairs.router)
router.include_router(orders.router)
router.include_router(balance.router)
router.include_router(trades.router)
router.include_router(ws.router)


__all__ = ["router"]
