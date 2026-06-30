"""Custom exception classes and FastAPI exception handlers.

All exceptions inherit from `AppError` and carry an HTTP status code +
error code string. The handlers convert them to a uniform JSON shape:

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# ─── Base exception ──────────────────────────────────────────────────────────

class AppError(Exception):
    """Base class for all application errors.

    Attributes
    ----------
    status_code : int
        HTTP status code to return.
    code : str
        Machine-readable error code (e.g. "insufficient_balance").
    message : str
        Human-readable error message.
    details : dict | None
        Optional additional context.
    """

    status_code: int = 400
    code: str = "app_error"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.__class__.__doc__ or self.code
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


# ─── Auth errors ─────────────────────────────────────────────────────────────

class AuthenticationError(AppError):
    status_code = 401
    code = "unauthorized"


class InvalidSignatureError(AuthenticationError):
    code = "invalid_signature"


class ExpiredTimestampError(AuthenticationError):
    code = "expired_timestamp"


class MissingApiKeyError(AuthenticationError):
    code = "missing_api_key"


class RevokedApiKeyError(AuthenticationError):
    code = "revoked_api_key"


class InsufficientPermissionsError(AppError):
    status_code = 403
    code = "insufficient_permissions"


# ─── Rate limit ──────────────────────────────────────────────────────────────

class RateLimitExceededError(AppError):
    status_code = 429
    code = "rate_limit_exceeded"


# ─── Domain errors (mapped from services) ────────────────────────────────────

class InsufficientBalanceHTTPError(AppError):
    status_code = 402  # Payment Required — semantically "not enough funds"
    code = "insufficient_balance"


class OrderNotFoundHTTPError(AppError):
    status_code = 404
    code = "order_not_found"


class OrderNotCancelableHTTPError(AppError):
    status_code = 409
    code = "order_not_cancelable"


class OrderValidationHTTPError(AppError):
    status_code = 400
    code = "order_validation_error"


class PostOnlyCrossHTTPError(AppError):
    status_code = 409
    code = "post_only_cross"


class TradingPairNotFoundHTTPError(AppError):
    status_code = 404
    code = "trading_pair_not_found"


class UserNotFoundHTTPError(AppError):
    status_code = 404
    code = "user_not_found"


# ─── Exception handlers ──────────────────────────────────────────────────────

async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Handle all AppError subclasses uniformly."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected exceptions. Returns 500 without leaking internals."""
    import logging
    logging.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred",
                "details": {},
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on a FastAPI app."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "AppError",
    "AuthenticationError",
    "InvalidSignatureError",
    "ExpiredTimestampError",
    "MissingApiKeyError",
    "RevokedApiKeyError",
    "InsufficientPermissionsError",
    "RateLimitExceededError",
    "InsufficientBalanceHTTPError",
    "OrderNotFoundHTTPError",
    "OrderNotCancelableHTTPError",
    "OrderValidationHTTPError",
    "PostOnlyCrossHTTPError",
    "TradingPairNotFoundHTTPError",
    "UserNotFoundHTTPError",
    "register_exception_handlers",
]
