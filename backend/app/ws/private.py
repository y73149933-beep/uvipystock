"""Private WebSocket handler: /ws/private.

Auth flow (two options):
  A) HMAC headers on the HTTP upgrade request (X-API-Key, X-Timestamp, X-Signature)
  B) Send an `auth` message within 5 seconds of connection

We support both. Option A is cleaner for browser clients that can set
headers; option B is for clients that can't (e.g., raw browser WebSocket).

After auth:
  1. Register with ws_manager for `user_id`.
  2. Start a private Redis Pub/Sub subscriber for the user's channels.
  3. Listen for client messages (ping).
  4. The subscriber pushes order/balance/bulk events as they arrive.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidSignatureError,
    MissingApiKeyError,
    ExpiredTimestampError,
)
from app.core.security import verify_ws_signature
from app.db.session import async_session_factory
from app.redis_client import get_redis
from app.redis_client import pubsub
from app.repositories.api_key_repo import ApiKeyRepository
from app.ws.manager import ws_manager

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_SECONDS = 5


async def handle_private_channel(websocket: WebSocket) -> None:
    """Handle a private WS connection with HMAC auth."""
    # Try header-based auth first
    user_id = await _try_header_auth(websocket)

    if user_id is None:
        # Accept and wait for auth message
        await websocket.accept()
        try:
            await websocket.send_json({
                "event": "auth_required",
                "message": "Send auth message within 5 seconds",
                "ts": int(asyncio.get_event_loop().time()),
            })
        except Exception:
            return  # client disconnected before we could send

        try:
            user_id = await asyncio.wait_for(
                _wait_for_auth_message(websocket),
                timeout=AUTH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                await websocket.send_json({
                    "event": "auth_timeout",
                    "message": "Authentication timed out",
                })
                await websocket.close(code=4001)
            except Exception:
                pass
            return
        except WebSocketDisconnect:
            # Client disconnected during auth — don't try to send, just return
            return
        except Exception as e:
            # Only send error if the connection is still open
            try:
                await websocket.send_json({
                    "event": "auth_failed",
                    "message": str(e),
                })
                await websocket.close(code=4003)
            except Exception:
                pass
            return
    else:
        await websocket.accept()

    # Auth succeeded — register + start subscriber
    await ws_manager.connect_private(websocket, user_id)
    await websocket.send_json({
        "event": "auth_ok",
        "user_id": user_id,
        "ts": int(asyncio.get_event_loop().time()),
    })

    # Start private Redis subscriber for this user
    subscriber_task = await ws_manager.subscribe_private_channel(user_id)

    # Listen for client messages (ping)
    try:
        while True:
            msg = await websocket.receive_json()
            if msg.get("action") == "ping":
                await websocket.send_json({
                    "event": "pong",
                    "ts": int(asyncio.get_event_loop().time()),
                })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Private WS error for user %d: %s", user_id, e)
    finally:
        await ws_manager.disconnect_private(websocket, user_id)
        subscriber_task.cancel()
        try:
            await subscriber_task
        except asyncio.CancelledError:
            pass


async def _try_header_auth(websocket: WebSocket) -> int | None:
    """Attempt to authenticate via X-API-Key / X-Timestamp / X-Signature headers.

    Returns user_id on success, None if headers are absent.
    Raises on invalid signature / expired timestamp.
    """
    headers = websocket.headers
    api_key = headers.get("x-api-key")
    timestamp = headers.get("x-timestamp")
    signature = headers.get("x-signature")

    if not api_key or not timestamp or not signature:
        return None

    try:
        ts = int(timestamp)
    except ValueError:
        raise ExpiredTimestampError(f"Invalid timestamp: {timestamp!r}")

    # Look up the API key in DB
    async with async_session_factory() as session:
        repo = ApiKeyRepository(session)
        api_key_row = await repo.get_by_api_key(api_key)
        if api_key_row is None:
            raise MissingApiKeyError(f"Unknown API key: {api_key[:8]}...")
        if api_key_row.is_revoked:
            raise MissingApiKeyError("API key revoked")

        # Verify signature
        if not verify_ws_signature(
            secret=api_key_row.secret_hash,
            api_key=api_key,
            timestamp=ts,
            provided_signature=signature,
        ):
            raise InvalidSignatureError("WS signature verification failed")

        return api_key_row.user_id


async def _wait_for_auth_message(websocket: WebSocket) -> int:
    """Wait for the client to send an auth message and verify it."""
    msg = await websocket.receive_json()
    if msg.get("action") != "auth":
        raise ValueError("Expected 'auth' message")

    api_key = msg.get("api_key")
    timestamp = msg.get("timestamp")
    signature = msg.get("signature")
    if not api_key or not timestamp or not signature:
        raise ValueError("Missing api_key/timestamp/signature in auth message")

    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        raise ExpiredTimestampError(f"Invalid timestamp: {timestamp!r}")

    async with async_session_factory() as session:
        repo = ApiKeyRepository(session)
        api_key_row = await repo.get_by_api_key(api_key)
        if api_key_row is None:
            raise MissingApiKeyError(f"Unknown API key: {api_key[:8]}...")
        if api_key_row.is_revoked:
            raise MissingApiKeyError("API key revoked")

        if not verify_ws_signature(
            secret=api_key_row.secret_hash,
            api_key=api_key,
            timestamp=ts,
            provided_signature=signature,
        ):
            raise InvalidSignatureError("WS signature verification failed")

        return api_key_row.user_id


__all__ = ["handle_private_channel"]
