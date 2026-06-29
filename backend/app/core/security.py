"""Security utilities: HMAC-SHA256 signing for API keys, JWT for admin sessions.

HMAC API auth
-------------
Trading bots authenticate REST requests with three headers:
  * X-API-Key     — public key (UUID-like)
  * X-Timestamp   — unix seconds; rejected if |now - ts| > 30s (replay protection)
  * X-Signature   — hex(HMAC-SHA256(secret, f"{METHOD}\n{PATH}\n{TIMESTAMP}\n{BODY}"))

The secret is stored as a SHA-256 hash in the database (see hash_api_secret).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import get_settings

_settings = get_settings()


# ─── API key secret hashing ──────────────────────────────────────────────────

def hash_api_secret(raw_secret: str) -> str:
    """Hash an API key secret for storage.

    Uses SHA-256 (not bcrypt) so the hash can be used as the HMAC key
    for stateless signature verification. See module docstring.
    """
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


def verify_api_secret(raw_secret: str, stored_hash: str) -> bool:
    """Verify a raw secret against the stored SHA-256 hash.

    Uses constant-time comparison to prevent timing attacks.
    """
    computed = hash_api_secret(raw_secret)
    return hmac.compare_digest(computed, stored_hash)


# ─── HMAC signature ──────────────────────────────────────────────────────────

def compute_signature(
    secret: str,
    method: str,
    path: str,
    timestamp: int,
    body: str,
) -> str:
    """Compute the HMAC-SHA256 signature for a REST request.

    The payload is: ``{METHOD}\n{PATH}\n{TIMESTAMP}\n{BODY}``
    where BODY is the raw request body string (empty string for GET).
    """
    payload = f"{method.upper()}\n{path}\n{timestamp}\n{body}"
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(
    secret: str,
    method: str,
    path: str,
    timestamp: int,
    body: str,
    provided_signature: str,
    replay_window_seconds: int | None = None,
) -> bool:
    """Verify an HMAC-SHA256 signature."""
    if replay_window_seconds is None:
        replay_window_seconds = _settings.hmac_replay_window_seconds

    now = int(time.time())
    if abs(now - timestamp) > replay_window_seconds:
        return False

    expected = compute_signature(secret, method, path, timestamp, body)
    return hmac.compare_digest(expected, provided_signature)


def compute_ws_signature(secret: str, api_key: str, timestamp: int) -> str:
    """Compute the HMAC signature for a WebSocket handshake."""
    payload = f"WS_HANDSHAKE\n{timestamp}\n{api_key}"
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_ws_signature(
    secret: str,
    api_key: str,
    timestamp: int,
    provided_signature: str,
    replay_window_seconds: int | None = None,
) -> bool:
    """Verify a WebSocket handshake signature."""
    if replay_window_seconds is None:
        replay_window_seconds = _settings.hmac_replay_window_seconds

    now = int(time.time())
    if abs(now - timestamp) > replay_window_seconds:
        return False

    expected = compute_ws_signature(secret, api_key, timestamp)
    return hmac.compare_digest(expected, provided_signature)


# ─── Password hashing (for user login) ──────────────────────────────────────
# We use bcrypt directly (not passlib) because passlib is incompatible with
# bcrypt 5.0+ (missing __about__ attribute + 72-byte limit enforcement).

def hash_password(password: str) -> str:
    """Hash a user password with bcrypt (rounds=12)."""
    # bcrypt has a 72-byte limit; truncate to avoid ValueError on long passwords
    password_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash."""
    password_bytes = password.encode("utf-8")[:72]
    hash_bytes = password_hash.encode("utf-8")
    try:
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except (ValueError, TypeError):
        return False


# ─── JWT (for admin panel sessions) ──────────────────────────────────────────

def create_jwt_token(
    user_id: int,
    is_admin: bool,
    expires_minutes: int | None = None,
) -> str:
    """Create a JWT for an admin user session."""
    if expires_minutes is None:
        expires_minutes = _settings.jwt_expires_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "is_admin": is_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, _settings.app_secret, algorithm=_settings.jwt_algorithm)


def decode_jwt_token(token: str) -> dict[str, Any] | None:
    """Decode and verify a JWT. Returns the payload or None if invalid."""
    try:
        return jwt.decode(token, _settings.app_secret, algorithms=[_settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None


__all__ = [
    "hash_api_secret",
    "verify_api_secret",
    "compute_signature",
    "verify_signature",
    "compute_ws_signature",
    "verify_ws_signature",
    "hash_password",
    "verify_password",
    "create_jwt_token",
    "decode_jwt_token",
]
