"""Regression tests: DELETE /orders/bulk must not be shadowed by /{order_id}.

Bug: the `DELETE /{order_id}` route was registered BEFORE `DELETE /bulk`,
so Starlette (which matches routes in registration order) routed the literal
path ``/orders/bulk`` into ``cancel_order`` and failed request validation
parsing "bulk" as an int → HTTP 422 before the bulk handler ever ran.

Fix under test:
  * bulk-cancel section is registered above the parameterized route;
  * parameterized routes use the Starlette ``:int`` path convertor, so a
    non-int segment can never match them regardless of registration order.

Infra: fresh FastAPI app + the real orders router; auth and DB-session
dependencies overridden; ``OrderService`` cancel methods replaced with
spies — no DB / Redis needed (mirrors test_stop_ladder.py conventions).
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.api.deps import authenticate_request
from app.db.session import get_session
from app.models.enums import OrderStatus
from app.services.order_service import OrderService


class _FakeSession:
    async def commit(self) -> None:
        return None

    async def refresh(self, obj) -> None:
        return None


@pytest.fixture
def spies(monkeypatch):
    """Replace OrderService cancel methods with call recorders."""
    bulk_calls: list[dict] = []
    single_calls: list[tuple[int, int]] = []

    async def fake_bulk(self, user_id, order_ids=None, symbol=None, cancel_all=False):
        bulk_calls.append({
            "user_id": user_id,
            "order_ids": order_ids,
            "symbol": symbol,
            "cancel_all": cancel_all,
        })
        return list(order_ids or [])

    async def fake_single(self, user_id, order_id):
        single_calls.append((user_id, order_id))
        return SimpleNamespace(
            id=order_id,
            status=OrderStatus.CANCELED,
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(OrderService, "cancel_bulk_orders", fake_bulk)
    monkeypatch.setattr(OrderService, "cancel_order", fake_single)
    return SimpleNamespace(bulk=bulk_calls, single=single_calls)


@pytest.fixture
def app():
    from app.api.v1 import orders as orders_route

    application = FastAPI()
    application.include_router(orders_route.router, prefix="/api/v1")

    async def fake_auth(request: Request) -> int:
        request.state.permissions = ["trade", "read"]
        return 1

    application.dependency_overrides[authenticate_request] = fake_auth
    application.dependency_overrides[get_session] = lambda: _FakeSession()
    return application


async def _delete(client, path, json=None):
    # httpx's .delete() shortcut does not accept a body — use request().
    return await client.request("DELETE", path, json=json)


async def test_bulk_cancel_by_ids_not_shadowed(app, spies):
    """The exact failing call from the bug report: DELETE /orders/bulk 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _delete(client, "/api/v1/orders/bulk", {"order_ids": [11, 22]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["canceled_count"] == 2
    assert body["canceled_orders"] == [11, 22]
    assert len(spies.bulk) == 1
    assert spies.bulk[0]["order_ids"] == [11, 22]
    assert spies.bulk[0]["user_id"] == 1
    assert spies.single == []  # must NOT have hit the single-cancel handler


async def test_bulk_cancel_all_by_symbol_reaches_handler(app, spies):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _delete(
            client, "/api/v1/orders/bulk", {"cancel_all": True, "symbol": "BTC/USDT"},
        )

    assert resp.status_code == 200, resp.text
    assert len(spies.bulk) == 1
    assert spies.bulk[0]["cancel_all"] is True
    assert spies.bulk[0]["symbol"] == "BTC/USDT"
    assert spies.single == []


async def test_single_cancel_still_routes_to_single_handler(app, spies):
    """`:int` convertor keeps numeric ids on the single-cancel route."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _delete(client, "/api/v1/orders/42")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order_id"] == 42
    assert body["status"] == OrderStatus.CANCELED
    assert spies.single == [(1, 42)]
    assert spies.bulk == []


async def test_bulk_route_registered_before_param_route(app):
    """Routing must stay order-independent: /bulk resolves before /{order_id}."""
    delete_formats = [
        getattr(r, "path_format", getattr(r, "path", ""))
        for r in app.routes
        if getattr(r, "methods", None) and "DELETE" in r.methods
    ]
    assert delete_formats.index("/api/v1/orders/bulk") < delete_formats.index(
        "/api/v1/orders/{order_id}"
    )


async def test_openapi_docs_not_broken_by_int_convertor(app):
    """/{order_id:int} must render as /{order_id} in the OpenAPI schema."""
    spec = app.openapi()
    assert "/api/v1/orders/{order_id}" in spec["paths"]
    assert "/api/v1/orders/bulk" in spec["paths"]
