"""Admin trading pair management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AdminUser, SessionDep
from app.schemas.admin import AdminTradingPairCreateRequest, AdminTradingPairResponse
from app.services.admin_service import AdminService

router = APIRouter(prefix="/market", tags=["admin-market"])


@router.post("/pairs", response_model=AdminTradingPairResponse, status_code=201)
async def create_trading_pair(
    admin: AdminUser,
    session: SessionDep,
    body: AdminTradingPairCreateRequest,
) -> AdminTradingPairResponse:
    """Create a new trading pair."""
    svc = AdminService(session)
    pair = await svc.create_trading_pair(
        symbol=body.symbol,
        base_asset=body.base_asset,
        quote_asset=body.quote_asset,
        price_precision=body.price_precision,
        quantity_precision=body.quantity_precision,
        min_lot_size=body.min_lot_size,
        max_lot_size=body.max_lot_size,
        tick_size=body.tick_size,
        maker_fee_bps=body.maker_fee_bps,
        taker_fee_bps=body.taker_fee_bps,
    )
    await session.commit()
    await session.refresh(pair)
    return AdminTradingPairResponse.model_validate(pair)


@router.get("/pairs", response_model=list[AdminTradingPairResponse])
async def list_trading_pairs(
    admin: AdminUser,
    session: SessionDep,
) -> list[AdminTradingPairResponse]:
    """List all active trading pairs."""
    svc = AdminService(session)
    pairs = await svc.list_trading_pairs()
    return [AdminTradingPairResponse.model_validate(p) for p in pairs]


@router.patch("/pairs/{pair_id}/active", response_model=AdminTradingPairResponse)
async def toggle_pair_active(
    admin: AdminUser,
    session: SessionDep,
    pair_id: int,
    is_active: bool = True,
) -> AdminTradingPairResponse:
    """Activate or deactivate a trading pair."""
    svc = AdminService(session)
    pair = await svc.toggle_pair_active(pair_id, is_active)
    await session.commit()
    if pair is None:
        from app.core.exceptions import TradingPairNotFoundHTTPError
        raise TradingPairNotFoundHTTPError(f"Trading pair {pair_id} not found")
    await session.refresh(pair)
    return AdminTradingPairResponse.model_validate(pair)


__all__ = ["router"]
