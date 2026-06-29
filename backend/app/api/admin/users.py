"""Admin user management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import AdminUser, SessionDep
from app.core.exceptions import UserNotFoundHTTPError
from app.core.security import hash_password
from app.schemas.admin import (
    AdminUserCreateRequest,
    AdminUserListResponse,
    AdminUserResponse,
)
from app.services.admin_service import AdminService

router = APIRouter(prefix="/users", tags=["admin-users"])


@router.post("", response_model=AdminUserResponse, status_code=201)
async def create_user(
    admin: AdminUser,
    session: SessionDep,
    body: AdminUserCreateRequest,
) -> AdminUserResponse:
    """Create a new user."""
    svc = AdminService(session)
    user = await svc.create_user(
        email=body.email,
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
    )
    await session.commit()
    await session.refresh(user)
    return AdminUserResponse.model_validate(user)


@router.get("", response_model=AdminUserListResponse)
async def list_users(
    admin: AdminUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> AdminUserListResponse:
    """List all users."""
    svc = AdminService(session)
    users = await svc.list_users(offset=offset, limit=limit)
    return AdminUserListResponse(
        users=[AdminUserResponse.model_validate(u) for u in users],
        pagination={"offset": offset, "limit": limit, "count": len(users)},
    )


@router.get("/{user_id}", response_model=AdminUserResponse)
async def get_user(
    admin: AdminUser,
    session: SessionDep,
    user_id: int,
) -> AdminUserResponse:
    """Get a user by ID."""
    svc = AdminService(session)
    user = await svc.get_user(user_id)
    if user is None:
        raise UserNotFoundHTTPError(f"User {user_id} not found")
    return AdminUserResponse.model_validate(user)


@router.patch("/{user_id}/active", response_model=AdminUserResponse)
async def toggle_user_active(
    admin: AdminUser,
    session: SessionDep,
    user_id: int,
    is_active: bool = True,
) -> AdminUserResponse:
    """Activate or deactivate a user."""
    svc = AdminService(session)
    user = await svc.toggle_user_active(user_id, is_active)
    await session.commit()
    if user is None:
        raise UserNotFoundHTTPError(f"User {user_id} not found")
    await session.refresh(user)
    return AdminUserResponse.model_validate(user)


__all__ = ["router"]
