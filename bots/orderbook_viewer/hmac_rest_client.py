"""HMAC-authenticated REST client for the exchange.

Login flow
----------
1. ``POST /api/v1/auth/login`` with ``{login, password}`` → server returns
   ``{user_id, api_key, api_secret, permissions}``.
2. We store ``api_key`` + ``api_secret`` in memory.
3. Every subsequent request is signed with HMAC-SHA256 using three headers:
   - ``X-API-Key``     — public key
   - ``X-Timestamp``   — unix seconds (replay window is ±30s by default)
   - ``X-Signature``   — hex(HMAC-SHA256(secret, "{METHOD}\n{PATH}\n{TS}\n{BODY}"))

The signature's secret key is the **raw** api_secret returned by /login
(not the SHA-256 hash that's stored in the DB). On the server side, the
raw secret is hashed once (SHA-256) and that hash is used as the HMAC
key — so we never store the raw secret anywhere except in this client's
memory.

Why httpx
---------
* Native async, matches the rest of the bot.
* Connection pooling — the market maker will fire many REST calls
  (place/cancel/modify orders); reusing a single client avoids
  per-request TCP/TLS overhead.
* Built-in timeout/retry primitives.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ─── Defaults ───────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 10.0
DEFAULT_REPLAY_WINDOW = 30  # seconds — must match server's hmac_replay_window_seconds


# ─── Exceptions ─────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Login or signature verification failed."""


class ApiError(Exception):
    """A non-2xx response from the exchange REST API."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code
        self.message = message
        self.body = body


# ─── HMAC signature ─────────────────────────────────────────────────────────

def _signing_key(raw_secret: str) -> str:
    """Derive the HMAC signing key from the raw API secret.

    The backend stores ``secret_hash = sha256(raw_secret)`` and uses that
    hash as the HMAC key (see ``app.core.security.compute_signature`` and
    ``app.api.deps.authenticate_request`` — the verify step passes
    ``api_key_row.secret_hash`` as the ``secret`` argument). The raw
    secret itself is never stored on the server.

    Therefore the client must compute the same SHA-256 of the raw secret
    and use THAT as the HMAC key — not the raw secret directly.

    This mirrors the frontend's ``sha256Hex(rawSecret)`` step in
    ``frontend/src/lib/hmac.ts``.
    """
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


def compute_signature(
    *,
    secret: str,
    method: str,
    path: str,
    timestamp: int,
    body: str,
) -> str:
    """Compute the HMAC-SHA256 signature for a REST request.

    Payload format mirrors `app.core.security.compute_signature` on the
    server: ``"{METHOD}\\n{PATH}\\n{TIMESTAMP}\\n{BODY}"``.

    `path` must include the query string (e.g. ``/api/v1/balance?asset=BTC``).

    `secret` is the **raw** api_secret returned by /auth/login. We hash
    it once with SHA-256 to derive the signing key — see `_signing_key`.
    """
    signing_key = _signing_key(secret)
    payload = f"{method.upper()}\n{path}\n{timestamp}\n{body}"
    return hmac.new(
        signing_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def compute_ws_signature(*, secret: str, api_key: str, timestamp: int) -> str:
    """Compute the HMAC signature for a WebSocket handshake.

    Payload format: ``"WS_HANDSHAKE\\n{timestamp}\\n{api_key}"``
    (mirrors `app.core.security.compute_ws_signature`).

    Like `compute_signature`, `secret` is the raw api_secret and we
    derive the signing key via SHA-256.
    """
    signing_key = _signing_key(secret)
    payload = f"WS_HANDSHAKE\n{timestamp}\n{api_key}"
    return hmac.new(
        signing_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ─── Client ─────────────────────────────────────────────────────────────────

class ExchangeAuth:
    """Container for credentials obtained from /auth/login.

    Kept separate from `ExchangeRESTClient` so it can be shared between
    the REST client and the WS client (which needs api_key + secret for
    the private-channel handshake).
    """

    def __init__(
        self,
        *,
        user_id: int,
        login: str,
        api_key: str,
        api_secret: str,
        permissions: list[str],
        is_admin: bool = False,
    ) -> None:
        self.user_id = user_id
        self.login = login
        self.api_key = api_key
        self.api_secret = api_secret
        self.permissions = list(permissions)
        self.is_admin = is_admin

    def __repr__(self) -> str:
        return (
            f"ExchangeAuth(user_id={self.user_id}, login={self.login!r}, "
            f"api_key={self.api_key[:8]}..., permissions={self.permissions})"
        )


class ExchangeRESTClient:
    """Async REST client with HMAC-SHA256 request signing.

    Typical use::

        client = ExchangeRESTClient(base_url="http://localhost:8000")
        auth = await client.login("trader@exchange.local", "admin123")
        balances = await client.get_balances()
        # ... place orders, list trades, etc.
        await client.aclose()
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        :param base_url: Base HTTP URL, e.g. ``http://localhost:8000``.
        :param timeout: Per-request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
        )
        self.auth: ExchangeAuth | None = None

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ExchangeRESTClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ─── Login ───────────────────────────────────────────────────────────

    async def login(self, login: str, password: str) -> ExchangeAuth:
        """Authenticate with login/password and store credentials.

        The server creates (or rotates) an API key on each login and
        returns the raw secret in the response — we never see it again,
        so we cache it in memory.
        """
        resp = await self._http.post(
            "/api/v1/auth/login",
            json={"login": login, "password": password},
        )
        if resp.status_code != 200:
            raise AuthError(f"Login failed: [{resp.status_code}] {resp.text}")
        data = resp.json()
        self.auth = ExchangeAuth(
            user_id=data["user_id"],
            login=data["login"],
            api_key=data["api_key"],
            api_secret=data["api_secret"],
            permissions=list(data.get("permissions", [])),
            is_admin=bool(data.get("is_admin", False)),
        )
        logger.info("Logged in as %s (user_id=%d)", self.auth.login, self.auth.user_id)
        return self.auth

    # ─── Signed request ──────────────────────────────────────────────────

    async def _signed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Send an HMAC-signed request and return the parsed JSON body.

        Raises:
            AuthError       — if not logged in.
            ApiError        — on any non-2xx response.
        """
        if self.auth is None:
            raise AuthError("Not logged in — call .login() first")

        # Build the full path INCLUDING the query string, because the
        # server's signature payload uses request.url.path + query.
        # httpx lets us pass params=... separately; we need to serialize
        # them ourselves to compute the signature over the same string.
        request_path = path
        if params:
            # Use httpx.QueryParams to get the same encoding the request will use
            qs = str(httpx.QueryParams(params))
            if qs:
                request_path = f"{path}?{qs}"

        body_str = ""
        if json_body is not None:
            # Match the server: it reads the raw body. We must serialize
            # with the SAME separators the server expects — httpx defaults
            # to json.dumps with ', ' and ': ' separators, but FastAPI's
            # request.body() returns whatever bytes we sent. Since we sign
            # the body string we send, we just need to use the same string
            # for both signing and the actual request body.
            import json as _json
            body_str = _json.dumps(json_body, separators=(",", ":"))

        timestamp = int(time.time())
        signature = compute_signature(
            secret=self.auth.api_secret,
            method=method,
            path=request_path,
            timestamp=timestamp,
            body=body_str,
        )

        headers = {
            "X-API-Key": self.auth.api_key,
            "X-Timestamp": str(timestamp),
            "X-Signature": signature,
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        # Send the request — use the EXACT same body string we signed
        # (content=... instead of json=... so httpx doesn't re-serialize).
        resp = await self._http.request(
            method,
            request_path,
            headers=headers,
            content=body_str.encode("utf-8") if body_str else b"",
        )

        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                msg = err_body.get("detail", {}).get("message", resp.text) \
                    if isinstance(err_body, dict) else resp.text
            except Exception:
                err_body = resp.text
                msg = resp.text
            raise ApiError(resp.status_code, msg, err_body)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ─── Public endpoints ────────────────────────────────────────────────

    async def get_balances(self, asset: str | None = None) -> list[dict]:
        """GET /api/v1/balance — list the user's balances.

        Returns a list of ``{"asset", "total", "locked", "available", "updated_at"}`` dicts.
        """
        params: dict[str, Any] = {}
        if asset:
            params["asset"] = asset
        data = await self._signed_request("GET", "/api/v1/balance", params=params or None)
        return data.get("balances", []) if data else []

    async def get_orders(
        self,
        *,
        symbol: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        """GET /api/v1/orders — list the user's orders."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if status:
            params["status"] = status
        data = await self._signed_request("GET", "/api/v1/orders", params=params)
        return data.get("orders", []) if data else []

    async def get_me(self) -> dict:
        """GET /api/v1/auth/me — current user info."""
        return await self._signed_request("GET", "/api/v1/auth/me")

    # ─── Trading endpoints (placeholders for the MM bot) ─────────────────

    async def place_order(self, order: dict) -> dict:
        """POST /api/v1/orders — place a single order.

        `order` must match the ``OrderCreateRequest`` schema:
        ``{symbol, side, type, price?, quantity, ...}``.
        """
        return await self._signed_request("POST", "/api/v1/orders", json_body=order)

    async def place_bulk_orders(
        self,
        orders: list[dict],
        *,
        bulk_id: str | None = None,
    ) -> dict:
        """POST /api/v1/orders/bulk — place multiple orders atomically (All-or-Nothing).

        If any order in the batch fails validation or balance check,
        the entire batch is rejected (no orders are placed).

        Returns the raw response dict:
        ``{"bulk_id", "result": "success"|"rejected", "total", "succeeded",
           "orders": [...], "errors": [...]}``.

        `orders` is a list of dicts each matching the ``OrderCreateRequest`` schema.
        """
        body: dict[str, Any] = {"orders": orders}
        if bulk_id:
            body["bulk_id"] = bulk_id
        return await self._signed_request("POST", "/api/v1/orders/bulk", json_body=body)

    async def cancel_order(self, order_id: int) -> dict:
        """DELETE /api/v1/orders/{order_id} — cancel a single order."""
        return await self._signed_request("DELETE", f"/api/v1/orders/{order_id}")

    async def cancel_bulk_orders(
        self,
        *,
        order_ids: list[int] | None = None,
        symbol: str | None = None,
        cancel_all: bool = False,
    ) -> dict:
        """DELETE /api/v1/orders/bulk — cancel by IDs, by symbol, or cancel-all.

        Returns ``{"canceled_count", "canceled_orders": [...], "failed": [...],
        "total_unlocked": [...]}``.
        """
        body: dict[str, Any] = {}
        if order_ids:
            body["order_ids"] = order_ids
        if symbol:
            body["symbol"] = symbol
        if cancel_all:
            body["cancel_all"] = True
        return await self._signed_request("DELETE", "/api/v1/orders/bulk", json_body=body)

    async def cancel_all_on_symbol(self, symbol: str) -> dict:
        """Convenience: cancel all the user's orders on `symbol`."""
        return await self.cancel_bulk_orders(symbol=symbol, cancel_all=True)

    async def modify_order(self, order_id: int, *, price: str, quantity: str) -> dict:
        """PUT /api/v1/orders/{order_id} — Cancel-Replace modify."""
        return await self._signed_request(
            "PUT",
            f"/api/v1/orders/{order_id}",
            json_body={"price": price, "quantity": quantity},
        )


__all__ = [
    "ExchangeAuth",
    "ExchangeRESTClient",
    "AuthError",
    "ApiError",
    "compute_signature",
    "compute_ws_signature",
]
