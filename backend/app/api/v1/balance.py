"""Balance REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import AuthenticatedUser, SessionDep, require_permission
from app.schemas.balance import BalanceListResponse, BalanceResponse
from app.services.balance_service import BalanceService

router = APIRouter(prefix="/balance", tags=["balance"])


@router.get(
    "",
    response_model=BalanceListResponse,
    dependencies=[Depends(require_permission("read"))],
)
async def get_balance(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    asset: str | None = Query(None, description="Comma-separated asset filter"),
) -> BalanceListResponse:
    """Get the authenticated user's balances."""
    svc = BalanceService(session)
    balances = await svc.get_all_balances(user_id)

    if asset:
        wanted = {a.strip().upper() for a in asset.split(",")}
        balances = [b for b in balances if b.asset.upper() in wanted]

    return BalanceListResponse(
        balances=[
            BalanceResponse(
                asset=b.asset,
                total=b.total_balance,
                locked=b.locked_balance,
                available=b.available_balance,
                updated_at=b.updated_at,
            )
            for b in balances
        ]
    )


__all__ = ["router"]
