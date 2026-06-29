"""Order REST endpoints.

POST   /api/v1/orders          — place single order
POST   /api/v1/orders/bulk     — place multiple orders (All-or-Nothing)
PUT    /api/v1/orders/{id}     — Cancel-Replace modify
DELETE /api/v1/orders/{id}     — cancel single
DELETE /api/v1/orders/bulk     — bulk cancel (by IDs or cancel-all)
GET    /api/v1/orders          — list user's orders with filters
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import AuthenticatedUser, SessionDep, require_permission
from app.core.exceptions import (
    InsufficientBalanceHTTPError,
    OrderNotFoundHTTPError,
    OrderNotCancelableHTTPError,
    OrderValidationHTTPError,
    PostOnlyCrossHTTPError,
)
from app.models.enums import OrderStatus
from app.schemas.order import (
    OrderBulkCancelRequest,
    OrderBulkCancelResponse,
    OrderBulkCreateRequest,
    OrderBulkCreateResponse,
    OrderCancelResponse,
    OrderCreateRequest,
    OrderCreateResponse,
    OrderListResponse,
    OrderModifyRequest,
    OrderResponse,
)
from app.services.balance_service import InsufficientBalanceError
from app.services.order_service import (
    OrderCreateDTO,
    OrderNotFoundError,
    OrderNotCancelableError,
    OrderService,
    OrderValidationError,
    PostOnlyCrossError,
    SLTPConfig,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


# ─── Helper: DTO conversion ──────────────────────────────────────────────────

def _request_to_dto(req: OrderCreateRequest, bulk_id: str | None = None) -> OrderCreateDTO:
    """Convert API request schema → service-layer DTO."""
    sl_cfg = None
    if req.sl is not None:
        sl_cfg = SLTPConfig(
            type=req.sl.type,
            stop_price=req.sl.stop_price,
            price=req.sl.price,
            quantity=req.sl.quantity,
        )
    tp_cfg = None
    if req.tp is not None:
        tp_cfg = SLTPConfig(
            type=req.tp.type,
            stop_price=req.tp.stop_price,
            price=req.tp.price,
            quantity=req.tp.quantity,
        )
    return OrderCreateDTO(
        symbol=req.symbol,
        side=req.side,
        type=req.type,
        price=req.price,
        stop_price=req.stop_price,
        trailing_delta=req.trailing_delta,
        quantity=req.quantity,
        visible_quantity=req.iceberg_visible_quantity,
        hidden_quantity=req.iceberg_hidden_quantity,
        sl=sl_cfg,
        tp=tp_cfg,
        client_order_id=req.client_order_id,
        bulk_id=bulk_id,
    )


def _order_to_response(order) -> OrderResponse:
    """Convert an Order model → API response schema."""
    return OrderResponse(
        id=order.id,
        user_id=order.user_id,
        symbol=order.symbol,
        side=order.side,
        type=order.type,
        status=order.status,
        price=order.price,
        stop_price=order.stop_price,
        trailing_delta=order.trailing_delta,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        filled_quote_qty=order.filled_quote_qty,
        remaining_quantity=order.remaining_quantity,
        avg_fill_price=order.avg_fill_price,
        visible_quantity=order.visible_quantity,
        hidden_quantity=order.hidden_quantity,
        parent_order_id=order.parent_order_id,
        sl_order_id=order.sl_order_id,
        tp_order_id=order.tp_order_id,
        replaces_id=order.replaces_id,
        replaced_by_id=order.replaced_by_id,
        bulk_id=order.bulk_id,
        replace_count=order.replace_count,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


# ─── POST /orders ────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=OrderCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("trade"))],
)
async def place_order(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    body: OrderCreateRequest,
) -> OrderCreateResponse:
    """Place a single order."""
    dto = _request_to_dto(body)
    svc = OrderService(session)
    try:
        order = await svc.place_order(user_id, dto)
        await session.commit()
        await session.refresh(order)
    except InsufficientBalanceError as e:
        await session.rollback()
        raise InsufficientBalanceHTTPError(
            message=str(e),
            details={"asset": e.asset, "needed": str(e.needed), "available": str(e.available)},
        )
    except OrderValidationError as e:
        await session.rollback()
        raise OrderValidationHTTPError(message=str(e))
    except PostOnlyCrossError as e:
        await session.rollback()
        raise PostOnlyCrossHTTPError(message=str(e))

    resp = OrderCreateResponse.model_validate(_order_to_response(order))
    resp.client_order_id = body.client_order_id
    return resp


# ─── POST /orders/bulk ───────────────────────────────────────────────────────

@router.post(
    "/bulk",
    response_model=OrderBulkCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("trade"))],
)
async def place_bulk_orders(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    body: OrderBulkCreateRequest,
) -> OrderBulkCreateResponse:
    """Place multiple orders atomically (All-or-Nothing)."""
    dtos = [_request_to_dto(req, bulk_id=body.bulk_id) for req in body.orders]
    svc = OrderService(session)
    try:
        orders = await svc.place_bulk_orders(user_id, dtos, bulk_id=body.bulk_id)
        await session.commit()
    except InsufficientBalanceError as e:
        # All-or-Nothing: nothing was created
        return OrderBulkCreateResponse(
            bulk_id=body.bulk_id or "",
            result="rejected",
            total=len(body.orders),
            succeeded=0,
            orders=[],
            errors=[{"code": "insufficient_balance", "message": str(e)}],
        )
    except OrderValidationError as e:
        return OrderBulkCreateResponse(
            bulk_id=body.bulk_id or "",
            result="rejected",
            total=len(body.orders),
            succeeded=0,
            orders=[],
            errors=[{"code": "validation_error", "message": str(e)}],
        )

    return OrderBulkCreateResponse(
        bulk_id=orders[0].bulk_id if orders else "",
        result="success",
        total=len(body.orders),
        succeeded=len(orders),
        orders=[_order_to_response(o) for o in orders],
        errors=[],
    )


# ─── PUT /orders/{id} (Cancel-Replace) ───────────────────────────────────────

@router.put(
    "/{order_id}",
    response_model=OrderResponse,
    dependencies=[Depends(require_permission("trade"))],
)
async def modify_order(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    order_id: int,
    body: OrderModifyRequest,
) -> OrderResponse:
    """Cancel-Replace: cancel old order, place new one in single transaction."""
    svc = OrderService(session)
    try:
        order = await svc.modify_order(user_id, order_id, body.price, body.quantity)
        await session.commit()
        await session.refresh(order)
    except OrderNotFoundError as e:
        raise OrderNotFoundHTTPError(message=str(e))
    except OrderNotCancelableError as e:
        await session.rollback()
        raise OrderNotCancelableHTTPError(message=str(e))
    except OrderValidationError as e:
        await session.rollback()
        raise OrderValidationHTTPError(message=str(e))
    except InsufficientBalanceError as e:
        await session.rollback()
        raise InsufficientBalanceHTTPError(
            message=str(e),
            details={"asset": e.asset, "needed": str(e.needed), "available": str(e.available)},
        )
    return _order_to_response(order)


# ─── DELETE /orders/{id} ─────────────────────────────────────────────────────

@router.delete(
    "/{order_id}",
    response_model=OrderCancelResponse,
    dependencies=[Depends(require_permission("trade"))],
)
async def cancel_order(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    order_id: int,
) -> OrderCancelResponse:
    """Cancel a single order."""
    svc = OrderService(session)
    try:
        order = await svc.cancel_order(user_id, order_id)
        await session.commit()
        await session.refresh(order)
    except OrderNotFoundError as e:
        raise OrderNotFoundHTTPError(message=str(e))
    except OrderNotCancelableError as e:
        raise OrderNotCancelableHTTPError(message=str(e))

    return OrderCancelResponse(
        order_id=order.id,
        status=order.status,
        canceled_at=order.updated_at,
    )


# ─── DELETE /orders/bulk ─────────────────────────────────────────────────────

@router.delete(
    "/bulk",
    response_model=OrderBulkCancelResponse,
    dependencies=[Depends(require_permission("trade"))],
)
async def cancel_bulk_orders(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    body: OrderBulkCancelRequest,
) -> OrderBulkCancelResponse:
    """Cancel multiple orders by IDs or cancel-all by symbol."""
    svc = OrderService(session)
    try:
        canceled_ids = await svc.cancel_bulk_orders(
            user_id,
            order_ids=body.order_ids,
            symbol=body.symbol,
            cancel_all=body.cancel_all,
        )
        await session.commit()
    except Exception as e:
        logger.exception("Bulk cancel failed: %s", e)
        raise

    return OrderBulkCancelResponse(
        canceled_count=len(canceled_ids),
        canceled_orders=canceled_ids,
        failed=[],
        total_unlocked=[],
    )


# ─── GET /orders ─────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=OrderListResponse,
    dependencies=[Depends(require_permission("read"))],
)
async def list_orders(
    request: Request,
    user_id: AuthenticatedUser,
    session: SessionDep,
    symbol: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status", description="Comma-separated statuses"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> OrderListResponse:
    """List the user's orders with optional filters."""
    statuses: list[OrderStatus] | None = None
    if status_filter:
        try:
            statuses = [OrderStatus(s.strip()) for s in status_filter.split(",")]
        except ValueError as e:
            raise OrderValidationHTTPError(message=f"Invalid status filter: {e}")

    svc = OrderService(session)
    orders = await svc.list_orders(
        user_id, symbol=symbol, statuses=statuses, offset=offset, limit=limit,
    )
    return OrderListResponse(
        orders=[_order_to_response(o) for o in orders],
        pagination={"offset": offset, "limit": limit, "count": len(orders)},
    )


__all__ = ["router"]
