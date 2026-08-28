"""In-memory balance state for the authenticated user.

Combines two sources:
1. **Initial snapshot** — fetched from ``GET /api/v1/balance`` right after
   login. This gives us the full current state of every asset.
2. **Live updates** — pushed by the exchange's private WS channel as
   ``event=balance`` messages whenever a balance changes (order placed,
   trade filled, order canceled, admin adjustment, ...).

The live update is a *full replacement* of the asset's three fields
(``total``, ``locked``, ``available``) — the server publishes the new
absolute values, not a delta. So we just overwrite.

This module is I/O-free so it can be unit-tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class Balance:
    """Snapshot of a single asset's balance."""
    asset: str
    total: Decimal
    locked: Decimal
    available: Decimal

    @property
    def is_zero(self) -> bool:
        """True if all three fields are zero (asset effectively unused)."""
        return self.total == 0 and self.locked == 0 and self.available == 0


@dataclass
class BalanceTracker:
    """Maintains the user's per-asset balances.

    Thread-safety: NOT thread-safe. Owned by a single asyncio task.
    Other tasks should call `snapshot()` which returns a copy.
    """

    _balances: dict[str, Balance] = field(default_factory=dict)
    update_count: int = 0
    snapshot_count: int = 0
    last_update_ts: int | None = None

    # ─── Mutation ────────────────────────────────────────────────────────

    def apply_rest_snapshot(self, rows: Iterable[dict]) -> None:
        """Replace the entire state with the result of GET /api/v1/balance.

        Each row has the shape ``{"asset", "total", "locked", "available", ...}``.
        Assets present in the API response but absent here are added;
        assets absent in the API response are removed (the API is the
        source of truth at snapshot time).
        """
        new_state: dict[str, Balance] = {}
        for row in rows:
            asset = row["asset"]
            new_state[asset] = Balance(
                asset=asset,
                total=Decimal(str(row.get("total", 0))),
                locked=Decimal(str(row.get("locked", 0))),
                available=Decimal(str(row.get("available", 0))),
            )
        self._balances = new_state
        self.snapshot_count += 1

    def apply_ws_update(
        self,
        *,
        asset: str,
        total: float | int | str | Decimal,
        locked: float | int | str | Decimal,
        available: float | int | str | Decimal,
        ts: int | None = None,
    ) -> None:
        """Apply a single balance update from the private WS channel.

        The server publishes absolute values (not deltas), so we
        overwrite the asset's row in place.
        """
        self._balances[asset] = Balance(
            asset=asset,
            total=Decimal(str(total)),
            locked=Decimal(str(locked)),
            available=Decimal(str(available)),
        )
        self.update_count += 1
        self.last_update_ts = ts

    # ─── Reads ───────────────────────────────────────────────────────────

    def get(self, asset: str) -> Balance | None:
        return self._balances.get(asset)

    def all_balances(self) -> list[Balance]:
        """Return all balances, sorted by asset name."""
        return [self._balances[a] for a in sorted(self._balances)]

    def non_zero_balances(self) -> list[Balance]:
        """Return only balances where the user actually has something.

        Useful for display — most users have a handful of assets funded,
        not the full list the exchange supports.
        """
        return [b for b in self.all_balances() if not b.is_zero]

    def snapshot(self) -> dict[str, Balance]:
        """Return a shallow copy of the internal state (for safe reads)."""
        return dict(self._balances)


__all__ = ["Balance", "BalanceTracker"]
