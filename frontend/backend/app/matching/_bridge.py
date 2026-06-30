"""Bridge between Python domain types and Cython C structs.

Responsibilities
----------------
1. Convert `app.models.Order` (with `Decimal` fields) → `COrder` (with `double`).
2. Convert `COrder` → plain Python dict for JSON serialization (REST/WS).
3. Convert Cython trade dict → `app.models.Trade`-shaped dict ready for DB insert.
4. Map domain `OrderType` → Cython `CType` (with special handling for
   stop-type orders which never reach the matcher directly).

Why a separate module?
----------------------
Keeping conversion logic out of `engine.pyx` ensures the Cython module
remains pure-C and importable without SQLAlchemy / Pydantic. The bridge
is the only place that imports both `app.models` and `app.matching.engine`.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Literal

from app.models.enums import OrderSide, OrderType
from app.matching.engine import PyCOrder
from app.matching._constants import (
    C_BUY, C_SELL,
    C_MARKET, C_LIMIT, C_IOC, C_FOK, C_POST_ONLY,
    C_OK, C_FOK_REJECTED, C_POST_ONLY_CROSS, C_NO_LIQUIDITY,
)

# ─── Type aliases ────────────────────────────────────────────────────────────
SideLiteral = Literal["buy", "sell"]
TypeLiteral = Literal[
    "market", "limit", "ioc", "fok", "post_only",
    "stop_market", "stop_limit", "trailing_stop", "iceberg",
]


# ─── Domain → Cython mappers ─────────────────────────────────────────────────

_SIDE_MAP: dict[str, int] = {
    OrderSide.BUY.value:  C_BUY,
    OrderSide.SELL.value: C_SELL,
}

_TYPE_MAP: dict[str, int] = {
    OrderType.MARKET.value:    C_MARKET,
    OrderType.LIMIT.value:     C_LIMIT,
    OrderType.IOC.value:       C_IOC,
    OrderType.FOK.value:       C_FOK,
    OrderType.POST_ONLY.value: C_POST_ONLY,
    OrderType.ICEBERG.value:   C_LIMIT,  # iceberg behaves like a limit at the matcher level
    # stop_market / stop_limit / trailing_stop are handled by the stop monitor
    # and converted to MARKET / LIMIT before reaching the matcher.
    OrderType.STOP_MARKET.value:   C_MARKET,
    OrderType.STOP_LIMIT.value:    C_LIMIT,
    OrderType.TRAILING_STOP.value: C_MARKET,  # triggered trailing stop → market
}


def to_c_side(side: str | OrderSide) -> int:
    """Convert domain side → Cython CSide int."""
    s = side.value if isinstance(side, OrderSide) else str(side).lower()
    if s not in _SIDE_MAP:
        raise ValueError(f"Unknown side: {side!r}")
    return _SIDE_MAP[s]


def to_c_type(order_type: str | OrderType) -> int:
    """Convert domain OrderType → Cython CType int.

    For stop-type orders, this returns the post-trigger type
    (stop_market → MARKET, stop_limit → LIMIT). Stop-type orders are
    never passed to the matcher directly — they wait in `stops:{symbol}`
    until the stop monitor triggers them.
    """
    t = order_type.value if isinstance(order_type, OrderType) else str(order_type).lower()
    if t not in _TYPE_MAP:
        raise ValueError(f"Unknown order type: {order_type!r}")
    return _TYPE_MAP[t]


def decimal_to_double(d: Decimal | float | int | str | None) -> float:
    """Convert a Decimal-or-string to a C double.

    Returns NaN for None (used by matcher to mean "no price" / market order).
    Raises ValueError if the input is non-numeric.
    """
    if d is None:
        return float("nan")
    try:
        return float(d)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Cannot convert {d!r} to float: {e}") from e


def build_corder(
    *,
    order_id: int,
    side: str | OrderSide,
    order_type: str | OrderType,
    price: Decimal | float | None,
    quantity: Decimal | float,
    is_iceberg: bool = False,
    visible_qty: Decimal | float | None = None,
    hidden_qty: Decimal | float | None = None,
) -> PyCOrder:
    """Construct a `PyCOrder` wrapper from Python-typed arguments.

    For market orders, `price` should be None (becomes NaN inside COrder).
    For iceberg orders, both `visible_qty` and `hidden_qty` must be provided.
    """
    cdef_side = to_c_side(side)
    cdef_type = to_c_type(order_type)
    price_d = decimal_to_double(price)
    qty_d = decimal_to_double(quantity)

    if qty_d <= 0.0:
        raise ValueError(f"quantity must be positive, got {quantity}")

    vis_d = decimal_to_double(visible_qty) if is_iceberg else qty_d
    hid_d = decimal_to_double(hidden_qty) if is_iceberg else 0.0

    if is_iceberg and vis_d <= 0.0:
        raise ValueError(f"iceberg visible_qty must be positive, got {visible_qty}")
    if is_iceberg and hid_d < 0.0:
        raise ValueError(f"iceberg hidden_qty must be non-negative, got {hidden_qty}")
    if is_iceberg and (vis_d + hid_d) != qty_d:
        # Strictly: total = visible + hidden. Allow some float slack.
        if not math.isclose(vis_d + hid_d, qty_d, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(
                f"iceberg invariant violated: visible({vis_d}) + hidden({hid_d}) != quantity({qty_d})"
            )

    c = PyCOrder()
    c._set(
        order_id=order_id,
        side=cdef_side,
        type=cdef_type,
        price=price_d,
        quantity=qty_d,
        remaining_qty=qty_d,
        is_iceberg=1 if is_iceberg else 0,
        visible_qty=vis_d,
        hidden_qty=hid_d,
    )
    return c


# ─── Cython → Domain mappers ─────────────────────────────────────────────────

_OUTCOME_MAP: dict[int, str] = {
    C_OK:              "ok",
    C_FOK_REJECTED:    "fok_rejected",
    C_POST_ONLY_CROSS: "post_only_cross",
    C_NO_LIQUIDITY:    "no_liquidity",
}


def outcome_to_str(outcome: int) -> str:
    """Map Cython CMatchOutcome → human-readable string."""
    if outcome not in _OUTCOME_MAP:
        raise ValueError(f"Unknown outcome code: {outcome}")
    return _OUTCOME_MAP[outcome]


def trade_dict_to_domain(trade: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Convert a raw trade dict from the matcher into a DB-ready dict.

    The matcher returns:
        {taker_order_id, maker_order_id, price, quantity, taker_side}

    The persistence layer additionally needs:
        - symbol
        - quote_quantity (price * quantity)
        - taker_side as OrderSide enum
    """
    price = Decimal(str(trade["price"]))
    qty   = Decimal(str(trade["quantity"]))
    side_int = int(trade["taker_side"])
    return {
        "taker_order_id": int(trade["taker_order_id"]),
        "maker_order_id": int(trade["maker_order_id"]),
        "symbol":         symbol,
        "price":          price,
        "quantity":       qty,
        "quote_quantity": price * qty,
        "side":           OrderSide.BUY if side_int == C_BUY else OrderSide.SELL,
    }


def is_outcome_terminal(outcome: int) -> bool:
    """True if the matcher produced a final decision (no retry needed)."""
    return outcome in (C_OK, C_FOK_REJECTED, C_POST_ONLY_CROSS, C_NO_LIQUIDITY)


def is_outcome_reject(outcome: int) -> bool:
    """True if the outcome means the order was rejected (no trades emitted)."""
    return outcome in (C_FOK_REJECTED, C_POST_ONLY_CROSS, C_NO_LIQUIDITY)


# ─── Constants re-exported for convenience ───────────────────────────────────
# Allows other modules to do `from app.matching._bridge import C_BUY, C_SELL`
# without coupling to engine.pxd internals.
__all__ = [
    # Builders
    "build_corder",
    "to_c_side",
    "to_c_type",
    "decimal_to_double",
    # Mappers
    "outcome_to_str",
    "trade_dict_to_domain",
    "is_outcome_terminal",
    "is_outcome_reject",
    # Constants
    "C_BUY", "C_SELL",
    "C_MARKET", "C_LIMIT", "C_IOC", "C_FOK", "C_POST_ONLY",
    "C_OK", "C_FOK_REJECTED", "C_POST_ONLY_CROSS", "C_NO_LIQUIDITY",
]
