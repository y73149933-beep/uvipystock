"""Pure-Python constants that mirror the C-level enums in engine.pxd.

Cython `cdef enum` values are C ints — usable inside .pyx but not importable
from Python. This module re-declares them as Python ints so that the bridge,
tests, and any pure-Python code can do:

    from app.matching._constants import C_BUY, C_FOK_REJECTED

The numeric values MUST match those in engine.pxd. A unit test
(`test_constants_match_cython`) verifies this at runtime.
"""
from __future__ import annotations

# ─── Sides ───────────────────────────────────────────────────────────────────
C_BUY:  int = 0
C_SELL: int = 1

# ─── Order types (as seen by the Cython matcher) ────────────────────────────
# Note: stop_market / stop_limit / trailing_stop are handled by the stop
# monitor and converted to MARKET / LIMIT before reaching the matcher.
C_MARKET:    int = 0
C_LIMIT:     int = 1
C_IOC:       int = 2
C_FOK:       int = 3
C_POST_ONLY: int = 4

# ─── Match outcomes ──────────────────────────────────────────────────────────
C_OK:               int = 0  # matched (fully or partially) or rested as maker
C_FOK_REJECTED:     int = 1  # FOK precheck failed, no trades emitted
C_POST_ONLY_CROSS:  int = 2  # Post-Only would cross spread, reject without lock
C_NO_LIQUIDITY:     int = 3  # market/IOC found nothing to match


__all__ = [
    "C_BUY", "C_SELL",
    "C_MARKET", "C_LIMIT", "C_IOC", "C_FOK", "C_POST_ONLY",
    "C_OK", "C_FOK_REJECTED", "C_POST_ONLY_CROSS", "C_NO_LIQUIDITY",
]
