"""Authentication endpoints for trading terminal users.

POST /api/v1/auth/login     — login + password → API key + secret (for HMAC)
POST /api/v1/auth/register  — login + password → new user account
GET  /api/v1/auth/me        — current user info (requires HMAC auth)
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import SessionDep, AuthenticatedUser
from app.core.security import hash_api_secret, hash_password, verify_password
from app.core.exceptions import AuthenticationError, AppError
from app.models.api_key import ApiKey
from app.models.user import User
from app.repositories.user_repo import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    login: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=1, max_length=255)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    login: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=4, max_length=255)


class LoginResponse(BaseModel):
    user_id: int
    login: str
    is_admin: bool
    api_key: str
    api_secret: str
    permissions: list[str]


class RegisterResponse(BaseModel):
    user_id: int
    login: str
    message: str


class MeResponse(BaseModel):
    user_id: int
    login: str
    is_admin: bool


@router.post("/login", response_model=LoginResponse)
async def login(session: SessionDep, body: LoginRequest) -> LoginResponse:
    user_repo = UserRepository(session)
    user = await user_repo.get_by_email(body.login)

    if user is None or not user.is_active:
        raise AuthenticationError("Invalid login or password")
    if not verify_password(body.password, user.password_hash):
        raise AuthenticationError("Invalid login or password")

    stmt = select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.is_revoked == False)  # noqa: E712
    result = await session.execute(stmt)
    api_key_row = result.scalar_one_or_none()

    if api_key_row is None:
        raw_secret = secrets.token_hex(32)
        api_key_row = ApiKey(
            user_id=user.id,
            api_key=secrets.token_hex(16),
            secret_hash=hash_api_secret(raw_secret),
            label="auto-generated at login",
            permissions=["trade", "read", "ws"],
            rate_limit_per_min=120,
        )
        session.add(api_key_row)
        await session.commit()
        await session.refresh(api_key_row)
    else:
        raw_secret = secrets.token_hex(32)
        api_key_row.secret_hash = hash_api_secret(raw_secret)
        await session.commit()

    return LoginResponse(
        user_id=user.id,
        login=user.email,
        is_admin=user.is_admin,
        api_key=api_key_row.api_key,
        api_secret=raw_secret,
        permissions=list(api_key_row.permissions or ["trade", "read", "ws"]),
    )


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(session: SessionDep, body: RegisterRequest) -> RegisterResponse:
    user_repo = UserRepository(session)
    existing = await user_repo.get_by_email(body.login)
    if existing is not None:
        raise AppError(status_code=409, code="login_exists",
                       message=f"User '{body.login}' already exists")

    user = await user_repo.create(
        email=body.login,
        password_hash=hash_password(body.password),
        is_admin=False, is_active=True,
    )
    await session.commit()
    return RegisterResponse(user_id=user.id, login=user.email, message="Account created. Please login.")


@router.get("/me", response_model=MeResponse)
async def get_me(user_id: AuthenticatedUser, session: SessionDep) -> MeResponse:
    user_repo = UserRepository(session)
    user = await user_repo.get(user_id)
    if user is None:
        raise AuthenticationError("User not found")
    return MeResponse(user_id=user.id, login=user.email, is_admin=user.is_admin)


__all__ = ["router"]
