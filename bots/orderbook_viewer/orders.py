"""In-memory open-orders state for the authenticated user.

Combines two sources:
1. **Initial snapshot** — fetched from ``GET /api/v1/orders?status=new,partially_filled,pending``
   right after login. This gives us the full current state of every
   active order.
2. **Live updates** — pushed by the exchange's private WS channel as
   ``event=order`` messages whenever an order's status changes (placed,
   partially_filled, filled, canceled, modified, rejected, triggered).

Lifecycle handling
------------------
The WS ``status_event`` field tells us what happened. We update the
tracker accordingly:

* ``placed``               — add to tracker
* ``partially_filled``     — update filled_quantity / remaining_quantity
* ``modified``             — replace price/quantity (Cancel-Replace)
* ``filled`` / ``canceled`` / ``rejected`` / ``expired`` — REMOVE from tracker
  (these are terminal states; the order is no longer "open")

This module is I/O-free so it can be unit-tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable

# Order statuses that count as "open" (i.e. take up slot in the panel).
# Matches the lifecycle in `app/models/enums.py`.
OPEN_STATUSES = frozenset({"new", "partially_filled", "pending"})

# Terminal statuses — order leaves the tracker when it enters one of these.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})


@dataclass
class Order:
    """Snapshot of a single order (only the fields we display)."""
    id: int
    symbol: str
    side: str            # "buy" | "sell"
    type: str            # "limit" | "market" | "post_only" | ...
    status: str          # "new" | "partially_filled" | "pending" | ...
    price: Decimal | None
    quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    created_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_buy(self) -> bool:
        return self.side == "buy"


def _parse_decimal(v) -> Decimal:
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


def _parse_dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    # ISO 8601 string from the API
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _order_from_dict(row: dict) -> Order:
    """Build an Order from a REST or WS payload."""
    return Order(
        id=int(row["id"]),
        symbol=str(row.get("symbol", "")),
        side=str(row.get("side", "")),
        type=str(row.get("type", "")),
        status=str(row.get("status", "")),
        price=_parse_decimal(row.get("price")),
        quantity=_parse_decimal(row.get("quantity")),
        filled_quantity=_parse_decimal(row.get("filled_quantity")),
        remaining_quantity=_parse_decimal(row.get("remaining_quantity")),
        created_at=_parse_dt(row.get("created_at")),
    )


@dataclass
class OrderTracker:
    """Maintains the user's open orders.

    Thread-safety: NOT thread-safe. Owned by a single asyncio task.
    """

    _orders: dict[int, Order] = field(default_factory=dict)
    update_count: int = 0
    snapshot_count: int = 0
    last_update_ts: int | None = None
    # Counters for terminal events (for the status line)
    filled_count: int = 0
    canceled_count: int = 0

    # ─── Mutation: REST snapshot ─────────────────────────────────────────

    def apply_rest_snapshot(self, rows: Iterable[dict]) -> None:
        """Replace the entire open-orders state.

        Only orders with an "open" status are kept; everything else is
        dropped (the REST endpoint already filters, but we double-check
        defensively in case the caller passes an unfiltered list).
        """
        new_state: dict[int, Order] = {}
        for row in rows:
            order = _order_from_dict(row)
            if order.is_open:
                new_state[order.id] = order
        self._orders = new_state
        self.snapshot_count += 1

    # ─── Mutation: WS update ─────────────────────────────────────────────

    def apply_ws_update(self, payload: dict, *, ts: int | None = None) -> Action:
        """Apply a single ``event=order`` message from the private WS.

        Returns an ``Action`` describing what happened, so the caller
        can log / react (e.g. play a sound on fill).

        The payload keys we care about:
          order_id, symbol, side, type, status, status_event,
          price, quantity, filled_quantity, remaining_quantity,
          created_at (not always present on WS events).
        """
        order_id_raw = payload.get("order_id")
        if order_id_raw is None:
            return Action.UNKNOWN
        try:
            order_id = int(order_id_raw)
        except (TypeError, ValueError):
            return Action.UNKNOWN

        status = str(payload.get("status", ""))
        status_event = str(payload.get("status_event", ""))

        # Terminal → remove from tracker
        if status in TERMINAL_STATUSES or status_event in TERMINAL_STATUSES:
            removed = self._orders.pop(order_id, None)
            if status == "filled" or status_event == "filled":
                self.filled_count += 1
            elif status == "canceled" or status_event == "canceled":
                self.canceled_count += 1
            self.update_count += 1
            self.last_update_ts = ts
            return Action.FILLED if (status == "filled" or status_event == "filled") \
                else (Action.CANCELED if (status == "canceled" or status_event == "canceled")
                      else Action.REMOVED)

        # Otherwise — upsert. For "placed" we add; for "partially_filled" /
        # "modified" we update in place.
        # The WS payload may not include all fields (e.g. created_at is
        # missing on updates) — preserve existing values for missing keys.
        existing = self._orders.get(order_id)
        merged: dict = {}
        if existing is not None:
            merged["id"] = existing.id
            merged["symbol"] = existing.symbol
            merged["side"] = existing.side
            merged["type"] = existing.type
            merged["created_at"] = existing.created_at
        merged.update({k: v for k, v in payload.items() if k != "order_id"})
        merged["id"] = order_id
        # price/quantity may be missing on partial-fill events — preserve
        for fld in ("symbol", "side", "type"):
            if fld not in merged or not merged[fld]:
                merged[fld] = getattr(existing, fld, "") if existing else ""
        if "price" not in merged:
            merged["price"] = str(existing.price) if existing and existing.price is not None else None
        if "quantity" not in merged:
            merged["quantity"] = str(existing.quantity) if existing else "0"
        if "filled_quantity" not in merged:
            merged["filled_quantity"] = str(existing.filled_quantity) if existing else "0"
        if "remaining_quantity" not in merged:
            merged["remaining_quantity"] = str(existing.remaining_quantity) if existing else "0"

        order = _order_from_dict(merged)
        if order.is_open:
            self._orders[order_id] = order
            self.update_count += 1
            self.last_update_ts = ts
            if existing is None:
                return Action.PLACED
            elif status_event == "modified":
                return Action.MODIFIED
            return Action.UPDATED
        # Status is something unexpected — drop it
        self._orders.pop(order_id, None)
        return Action.REMOVED

    # ─── Reads ───────────────────────────────────────────────────────────

    def get(self, order_id: int) -> Order | None:
        return self._orders.get(order_id)

    def all_orders(self) -> list[Order]:
        """Return all open orders, sorted by creation time (newest first)."""
        def _sort_key(o: Order) -> tuple:
            # Newest first; orders without created_at go to the end
            return (o.created_at is None, -(o.created_at.timestamp() if o.created_at else 0))
        return sorted(self._orders.values(), key=_sort_key)

    def open_count(self) -> int:
        return len(self._orders)

    def count_for_symbol(self, symbol: str) -> int:
        return sum(1 for o in self._orders.values() if o.symbol == symbol)

    def snapshot(self) -> dict[int, Order]:
        """Return a shallow copy of the internal state (for safe reads)."""
        return dict(self._orders)


# ─── Action enum (returned by apply_ws_update) ──────────────────────────────

class Action:
    """What `apply_ws_update` did. Plain string constants (not an Enum)
    so they're easy to log and compare.
    """
    UNKNOWN = "unknown"
    PLACED = "placed"
    UPDATED = "updated"
    MODIFIED = "modified"
    FILLED = "filled"
    CANCELED = "canceled"
    REMOVED = "removed"  # removed for some other reason (expired/rejected)


__all__ = ["Order", "OrderTracker", "Action", "OPEN_STATUSES", "TERMINAL_STATUSES"]
