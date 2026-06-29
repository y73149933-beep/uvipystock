"""Admin API key management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AdminUser, SessionDep
from app.schemas.admin import AdminApiKeyCreateRequest, AdminApiKeyCreateResponse, AdminApiKeyResponse
from app.services.admin_service import AdminService

router = APIRouter(prefix="/api-keys", tags=["admin-api-keys"])


@router.post("", response_model=AdminApiKeyCreateResponse, status_code=201)
async def create_api_key(
    admin: AdminUser,
    session: SessionDep,
    body: AdminApiKeyCreateRequest,
) -> AdminApiKeyCreateResponse:
    """Generate a new API keypair for a user. Returns the raw secret ONCE."""
    svc = AdminService(session)
    api_key, raw_secret = await svc.create_api_key(
        user_id=body.user_id,
        label=body.label,
        permissions=body.permissions,
        rate_limit_per_min=body.rate_limit_per_min,
    )
    await session.commit()
    await session.refresh(api_key)
    return AdminApiKeyCreateResponse(
        id=api_key.id,
        user_id=api_key.user_id,
        api_key=api_key.api_key,
        label=api_key.label,
        permissions=api_key.permissions,
        rate_limit_per_min=api_key.rate_limit_per_min,
        is_revoked=api_key.is_revoked,
        created_at=api_key.created_at,
        secret=raw_secret,
    )


@router.delete("/{api_key_id}")
async def revoke_api_key(
    admin: AdminUser,
    session: SessionDep,
    api_key_id: int,
) -> dict:
    """Revoke an API key."""
    svc = AdminService(session)
    success = await svc.revoke_api_key(api_key_id)
    await session.commit()
    return {"revoked": success, "api_key_id": api_key_id}


__all__ = ["router"]
