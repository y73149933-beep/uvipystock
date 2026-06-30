"""Admin balance management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AdminUser, SessionDep
from app.core.exceptions import InsufficientBalanceHTTPError, UserNotFoundHTTPError
from app.schemas.admin import AdminBalanceAdjustRequest, AdminBalanceResponse
from app.services.admin_service import AdminService
from app.services.balance_service import InsufficientBalanceError

router = APIRouter(prefix="/balances", tags=["admin-balances"])


@router.post("/adjust", response_model=AdminBalanceResponse)
async def adjust_balance(
    admin: AdminUser,
    session: SessionDep,
    body: AdminBalanceAdjustRequest,
) -> AdminBalanceResponse:
    """Adjust a user's balance by a signed delta."""
    svc = AdminService(session)
    try:
        bal = await svc.adjust_balance(body.user_id, body.asset, body.delta, body.reason)
        await session.commit()
        # Refresh to load all attributes before session closes
        await session.refresh(bal)
    except InsufficientBalanceError as e:
        await session.rollback()
        raise InsufficientBalanceHTTPError(message=str(e))

    return AdminBalanceResponse(
        user_id=bal.user_id,
        asset=bal.asset,
        total=bal.total_balance,
        locked=bal.locked_balance,
        available=bal.available_balance,
        updated_at=bal.updated_at,
    )


@router.get("/{user_id}", response_model=list[AdminBalanceResponse])
async def get_user_balances(
    admin: AdminUser,
    session: SessionDep,
    user_id: int,
) -> list[AdminBalanceResponse]:
    """Get all balances for a user."""
    svc = AdminService(session)
    balances = await svc.get_user_balances(user_id)
    return [
        AdminBalanceResponse(
            user_id=b.user_id,
            asset=b.asset,
            total=b.total_balance,
            locked=b.locked_balance,
            available=b.available_balance,
            updated_at=b.updated_at,
        )
        for b in balances
    ]


__all__ = ["router"]
