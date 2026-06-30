"""Admin authentication endpoints.

POST /api/admin/login — login + password → JWT token (for admin panel)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SessionDep
from app.core.exceptions import AuthenticationError
from app.core.security import verify_password, create_jwt_token
from app.repositories.user_repo import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-auth"])


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    login: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=1, max_length=255)


class AdminLoginResponse(BaseModel):
    token: str
    user_id: int
    login: str
    is_admin: bool


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(session: SessionDep, body: AdminLoginRequest) -> AdminLoginResponse:
    """Login as admin → returns JWT token for admin panel."""
    user_repo = UserRepository(session)
    user = await user_repo.get_by_email(body.login)

    if user is None or not user.is_active:
        raise AuthenticationError("Invalid login or password")
    if not verify_password(body.password, user.password_hash):
        raise AuthenticationError("Invalid login or password")
    if not user.is_admin:
        raise AuthenticationError("Access denied: admin privileges required")

    token = create_jwt_token(user.id, is_admin=True)
    return AdminLoginResponse(token=token, user_id=user.id, login=user.email, is_admin=True)


__all__ = ["router"]
