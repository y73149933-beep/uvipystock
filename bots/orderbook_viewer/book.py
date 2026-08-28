"""In-memory L2 orderbook state.

Maintains two sorted maps (bids descending, asks ascending) updated from
`orderbook_snapshot` and `orderbook_update` events published by the
exchange's public WebSocket channel.

Design notes
------------
* The exchange sends `bids`/`asks` as ``[[price, volume], ...]`` pairs.
  A volume of ``0`` on an update means "remove the level".
* We keep `Decimal` for prices and quantities — the exchange uses
  arbitrary-precision numerics (tick_size, lot_size), and float drift
  would break level-equality checks.
* `top(depth)` returns the best N levels on each side, suitable for
  rendering or for fair-price computation in the market maker.

This module is deliberately I/O-free so it can be unit-tested in
isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class Level:
    """A single aggregated price level.

    Aggregated means "sum of all resting orders at this price" — the
    exchange already coalesces individual orders into per-price volume
    in its snapshots/updates.
    """
    price: Decimal
    quantity: Decimal


@dataclass
class LocalOrderBook:
    """Maintains L2 state from snapshot + delta events.

    Thread-safety: NOT thread-safe. Designed to be owned by a single
    asyncio task (the WS reader). Other tasks must read snapshots via
    `top()` which returns a copy.
    """

    symbol: str
    _bids: dict[Decimal, Decimal] = field(default_factory=dict)
    _asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_trade_price: Decimal | None = None
    last_trade_ts: int | None = None
    last_update_ts: int | None = None
    snapshot_count: int = 0
    update_count: int = 0

    # ─── Mutation: snapshot ──────────────────────────────────────────────

    def apply_snapshot(
        self,
        bids: Iterable[tuple[Decimal | float | str, Decimal | float | str]],
        asks: Iterable[tuple[Decimal | float | str, Decimal | float | str]],
        last_trade_price: Decimal | float | str | None = None,
        ts: int | None = None,
    ) -> None:
        """Replace the entire book with a fresh snapshot."""
        self._bids = {
            Decimal(str(p)): Decimal(str(q))
            for p, q in bids
            if Decimal(str(q)) > 0
        }
        self._asks = {
            Decimal(str(p)): Decimal(str(q))
            for p, q in asks
            if Decimal(str(q)) > 0
        }
        if last_trade_price is not None:
            self.last_trade_price = Decimal(str(last_trade_price))
        self.last_update_ts = ts
        self.snapshot_count += 1

    # ─── Mutation: incremental update ────────────────────────────────────

    def apply_update(
        self,
        changes: Iterable[dict],
        ts: int | None = None,
    ) -> None:
        """Apply a list of ``{"side", "price", "qty"}`` changes.

        A `qty` of 0 removes the level.
        """
        for change in changes:
            side = change.get("side") or change.get("s")
            price = Decimal(str(change["price"]))
            qty = Decimal(str(change.get("qty", change.get("quantity", 0))))

            book = self._bids if side in ("bid", "b", "buy") else self._asks
            if qty <= 0:
                book.pop(price, None)
            else:
                book[price] = qty

        self.last_update_ts = ts
        self.update_count += 1

    # ─── Mutation: trade print ───────────────────────────────────────────

    def apply_trade(
        self,
        price: Decimal | float | str,
        ts: int | None = None,
    ) -> None:
        """Record the last trade price (does not mutate the book).

        The exchange publishes trade prints on a separate channel; we
        only use them for display and fair-price smoothing.
        """
        self.last_trade_price = Decimal(str(price))
        self.last_trade_ts = ts

    # ─── Reads ───────────────────────────────────────────────────────────

    def top_bids(self, depth: int = 10) -> list[Level]:
        """Return top-N bid levels, highest price first."""
        return [
            Level(p, q)
            for p, q in sorted(self._bids.items(), reverse=True)[:depth]
        ]

    def top_asks(self, depth: int = 10) -> list[Level]:
        """Return top-N ask levels, lowest price first."""
        return [
            Level(p, q)
            for p, q in sorted(self._asks.items())[:depth]
        ]

    def top(self, depth: int = 10) -> tuple[list[Level], list[Level]]:
        """Convenience: ``(top_bids, top_asks)``."""
        return self.top_bids(depth), self.top_asks(depth)

    def best_bid(self) -> Level | None:
        if not self._bids:
            return None
        p = max(self._bids)
        return Level(p, self._bids[p])

    def best_ask(self) -> Level | None:
        if not self._asks:
            return None
        p = min(self._asks)
        return Level(p, self._asks[p])

    def mid_price(self) -> Decimal | None:
        """Return the mid price, or last trade if one side is empty."""
        bb = self.best_bid()
        ba = self.best_ask()
        if bb and ba:
            return (bb.price + ba.price) / 2
        return self.last_trade_price

    def spread(self) -> tuple[Decimal, Decimal] | None:
        """Return ``(spread_abs, spread_pct)`` or None if book is one-sided.

        `spread_pct` is in percent (e.g. 0.12 means 0.12%).
        """
        bb = self.best_bid()
        ba = self.best_ask()
        if not bb or not ba:
            return None
        abs_spread = ba.price - bb.price
        mid = (bb.price + ba.price) / 2
        if mid == 0:
            return None
        return abs_spread, (abs_spread / mid) * 100

    def bid_levels_count(self) -> int:
        return len(self._bids)

    def ask_levels_count(self) -> int:
        return len(self._asks)


__all__ = ["Level", "LocalOrderBook"]
