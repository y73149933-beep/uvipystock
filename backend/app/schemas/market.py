"""Public market data schemas."""
from __future__ import annotations

from pydantic import BaseModel


class StopLadderEntry(BaseModel):
    """One aggregated row of the public conditional-order ladder.

    Price/volume are fixed-point strings to keep exact Decimal precision
    end-to-end (matches the order API convention).
    """
    side: str          # "buy" | "sell"
    type: str          # "stop_market" | "stop_limit" | "trailing_stop"
    price: str         # trigger price
    volume: str        # aggregated remaining size at this level
    count: int         # number of orders behind the level


class StopLadderResponse(BaseModel):
    """Response for GET /api/v1/market/{symbol}/stops.

    Anonymous by design: only aggregated sizes per trigger level, no user
    attribution. SELL rows come first (descending price), then BUY rows
    (descending price).
    """
    symbol: str
    levels: list[StopLadderEntry]
    total_count: int
    total_volume: str


__all__ = ["StopLadderEntry", "StopLadderResponse"]
