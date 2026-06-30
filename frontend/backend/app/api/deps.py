"""FastAPI dependencies: HMAC auth, DB session, rate limiting.

Auth flow
---------
1. Extract X-API-Key, X-Timestamp, X-Signature from headers.
2. Look up the ApiKey row by `api_key` (cache in Redis for speed).
3. Verify the timestamp is within the replay window.
4. Re-compute the HMAC signature using the stored `secret_hash` as the key
   and compare with `X-Signature`.
5. Attach `request.state.api_key`, `request.state.user_id`,
   `request.state.rate_limit_per_min` for downstream dependencies.
6. Enforce rate limiting.

The dependency yields the authenticated `user_id` so route handlers can
use it directly.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InsufficientPermissionsError,
    InvalidSignatureError,
    MissingApiKeyError,
    RevokedApiKeyError,
    ExpiredTimestampError,
)
from app.core.rate_limit import enforce_rate_limit
from app.core.security import verify_signature
from app.db.session import get_session
from app.models.api_key import ApiKey
from app.models.user import User
from app.redis_client import get_redis
from app.redis_client import orderbook as orderbook_redis
from app.repositories.api_key_repo import ApiKeyRepository
from app.repositories.user_repo import UserRepository


# ─── DB session dependency ───────────────────────────────────────────────────

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ─── HMAC auth ───────────────────────────────────────────────────────────────

async def _lookup_api_key(session: AsyncSession, api_key: str) -> ApiKey | None:
    """Look up an ApiKey row, with Redis caching."""
    repo = ApiKeyRepository(session)
    return await repo.get_by_api_key(api_key)


async def authenticate_request(
    request: Request,
    session: SessionDep,
    x_api_key: Annotated[str | None, Header()] = None,
    x_timestamp: Annotated[str | None, Header()] = None,
    x_signature: Annotated[str | None, Header()] = None,
) -> int:
    """Verify HMAC signature and return the authenticated user_id.

    Sets the following on `request.state` for downstream deps:
      * api_key            — the public key string
      * user_id            — the authenticated user ID
      * api_key_id         — the ApiKey row ID
      * rate_limit_per_min — per-key rate limit
      * permissions        — list of permission strings

    Raises AuthenticationError subclasses on failure.
    """
    # 1. Header presence
    if not x_api_key or not x_timestamp or not x_signature:
        raise MissingApiKeyError("Missing X-API-Key, X-Timestamp, or X-Signature header")

    # 2. Parse timestamp
    try:
        timestamp = int(x_timestamp)
    except ValueError:
        raise ExpiredTimestampError(f"Invalid X-Timestamp: {x_timestamp!r}")

    # 3. Look up the API key
    api_key_row = await _lookup_api_key(session, x_api_key)
    if api_key_row is None:
        raise MissingApiKeyError(f"Unknown API key: {x_api_key[:8]}...")
    if api_key_row.is_revoked:
        raise RevokedApiKeyError(f"API key {x_api_key[:8]}... has been revoked")

    # 4. Read the raw body (for signature computation)
    # FastAPI caches the body on first read; we read it here.
    body_bytes = await request.body()
    body = body_bytes.decode("utf-8") if body_bytes else ""

    # 5. Verify signature
    # The signature payload is f"{METHOD}\n{PATH}\n{TIMESTAMP}\n{BODY}"
    # PATH should be the raw path including query string.
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"

    # Debug logging (remove in production)
    import logging
    _log = logging.getLogger(__name__)
    _log.debug("Verifying signature: method=%s path=%r ts=%s body_len=%d",
               request.method, path, timestamp, len(body))

    # The HMAC key is the stored secret_hash (see app.core.security docstring)
    if not verify_signature(
        secret=api_key_row.secret_hash,
        method=request.method,
        path=path,
        timestamp=timestamp,
        body=body,
        provided_signature=x_signature,
    ):
        raise InvalidSignatureError("HMAC signature verification failed")

    # 6. Attach to request.state
    # permissions may be stored as a list (PostgreSQL ARRAY) or as a
    # comma-separated string (SQLite tests). Normalize to a list.
    perms_raw = api_key_row.permissions
    if isinstance(perms_raw, str):
        perms_list = [p.strip() for p in perms_raw.split(",") if p.strip()]
    elif isinstance(perms_raw, (list, tuple)):
        perms_list = list(perms_raw)
    else:
        perms_list = []

    request.state.api_key = x_api_key
    request.state.user_id = api_key_row.user_id
    request.state.api_key_id = api_key_row.id
    request.state.rate_limit_per_min = api_key_row.rate_limit_per_min
    request.state.permissions = perms_list

    # 7. Enforce rate limit
    await enforce_rate_limit(request, x_api_key)

    # 8. Update last_used_at (fire-and-forget — don't block the request or
    # interfere with the route handler's transaction).
    # We use a separate session for this to avoid "transaction already begun" errors.
    try:
        from datetime import datetime, timezone
        from app.db.session import async_session_factory
        async with async_session_factory() as bg_session:
            api_key_row.last_used_at = datetime.now(timezone.utc)
            # Re-fetch + update to avoid stale-object issues
            from sqlalchemy import update as sa_update
            from app.models.api_key import ApiKey as ApiKeyModel
            await bg_session.execute(
                sa_update(ApiKeyModel)
                .where(ApiKeyModel.id == api_key_row.id)
                .values(last_used_at=datetime.now(timezone.utc))
            )
            await bg_session.commit()
    except Exception:
        pass  # best-effort; don't fail the request

    return api_key_row.user_id


# Dependency shortcut
AuthenticatedUser = Annotated[int, Depends(authenticate_request)]


# ─── Permission check ────────────────────────────────────────────────────────

def require_permission(perm: str):
    """Factory: returns a dependency that checks for `perm` on the request.

    MUST be used as a parameter dependency (not in ``dependencies=[...]``)
    so that it runs AFTER ``authenticate_request``. The ``user_id`` parameter
    enforces ordering: FastAPI resolves it first (via authenticate_request),
    which sets ``request.state.permissions``, then this dep reads it.

    Usage::

        @router.post("/orders")
        async def place_order(
            user_id: AuthenticatedUser,
            session: SessionDep,
            body: OrderCreateRequest,
            _perm: None = Depends(require_permission("trade")),
        ):
            ...
    """
    async def _check(request: Request, user_id: AuthenticatedUser) -> None:
        perms = getattr(request.state, "permissions", [])
        if perm not in perms:
            raise InsufficientPermissionsError(
                f"API key lacks required permission: {perm!r}"
            )

    return _check


# ─── Admin auth (JWT) ────────────────────────────────────────────────────────

async def authenticate_admin(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Verify a JWT bearer token and return the admin User.

    Raises AuthenticationError if the token is missing, invalid, or the
    user is not an admin.
    """
    from app.core.exceptions import AuthenticationError
    from app.core.security import decode_jwt_token

    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_jwt_token(token)
    if payload is None:
        raise AuthenticationError("Invalid or expired JWT")

    user_id = int(payload.get("sub", "0"))
    is_admin = bool(payload.get("is_admin", False))
    if not is_admin:
        raise AuthenticationError("User is not an admin")

    user_repo = UserRepository(session)
    user = await user_repo.get(user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Admin user not found or inactive")

    request.state.user_id = user.id
    request.state.is_admin = True
    return user


AdminUser = Annotated[User, Depends(authenticate_admin)]


__all__ = [
    "SessionDep",
    "authenticate_request",
    "AuthenticatedUser",
    "require_permission",
    "authenticate_admin",
    "AdminUser",
]
