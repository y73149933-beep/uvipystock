"""Admin router — aggregates all admin sub-routers."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.admin import api_keys, auth, balances, emulator, market, users

router = APIRouter(prefix="/api/admin")
# Auth routes (login) must NOT require JWT — they're the entry point
router.include_router(auth.router)
# All other admin routes require JWT (via AdminUser dependency)
router.include_router(users.router)
router.include_router(balances.router)
router.include_router(market.router)
router.include_router(api_keys.router)
router.include_router(emulator.router)


__all__ = ["router"]
