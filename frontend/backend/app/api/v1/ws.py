"""WebSocket route stubs — actual handlers live in app.ws package.

This module exposes the WS routes on the v1 router so the API surface is
discoverable in one place. The handlers delegate to app.ws.public / app.ws.private.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws.manager import ws_manager
from app.ws.public import handle_public_orderbook
from app.ws.private import handle_private_channel

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/orderbook/{symbol:path}")
async def ws_orderbook(websocket: WebSocket, symbol: str) -> None:
    """Public L2 orderbook stream for `symbol`.

    Uses {symbol:path} so symbols containing '/' (e.g. 'BTC/USDT') are
    captured as a single parameter.
    """
    await handle_public_orderbook(websocket, symbol)


@router.websocket("/ws/private")
async def ws_private(websocket: WebSocket) -> None:
    """Private channel — requires HMAC auth on handshake."""
    await handle_private_channel(websocket)


__all__ = ["router"]
