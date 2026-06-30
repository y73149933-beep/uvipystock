# distutils: language = c
# cython: language_level=3
"""
Cython declarations for the matching engine.

The .pxd file is the *contract* — it declares the C-level structs and the
cdef class API. The .pyx file contains the implementation.

All hot-path structures are pure C (no Python objects) so the matcher can
release the GIL via `with nogil:`.

Layout summary
--------------
```
COrder           — incoming/taker order, value type (cheap to copy)
OrderNode        — single order at a price level, linked-list node
PriceLevel       — price bucket, holds head/tail of OrderNode chain
OrderBookSide    — dynamic array of PriceLevel, sorted by price
TradeResult      — single executed trade, value type
CMatchingEngine  — Python-visible cdef class wrapping all of the above
```
"""
from libc.stdint cimport int64_t


# ─── Enums (mirror app.models.enums but as C ints for speed) ────────────────

cdef enum CSide:
    C_BUY  = 0
    C_SELL = 1

cdef enum CType:
    C_MARKET    = 0
    C_LIMIT     = 1
    C_IOC       = 2
    C_FOK       = 3
    C_POST_ONLY = 4
    # stop_market / stop_limit / trailing_stop are NOT handled by the matcher
    # directly — they are converted to market/limit by the stop monitor
    # *after* their trigger fires, so they never reach CMatchingEngine as such.

cdef enum CMatchOutcome:
    C_OK            = 0   # matched (fully or partially, or rested as maker)
    C_FOK_REJECTED  = 1   # FOK could not be fully filled → no trades
    C_POST_ONLY_CROSS = 2 # Post-Only would cross spread → reject without lock
    C_NO_LIQUIDITY  = 3   # market/IOC found nothing to match


# ─── C-structs (all nogil-safe: no Python objects) ──────────────────────────

cdef struct OrderNode:
    int64_t     order_id
    double      remaining_qty     # what's left to fill (incl. hidden for iceberg)
    double      visible_qty       # currently visible portion (== remaining for non-iceberg)
    int         is_iceberg        # 0 / 1
    OrderNode*  next              # FIFO within price level (NULL = tail)


cdef struct PriceLevel:
    double       price
    OrderNode*   head             # oldest order at this price
    OrderNode*   tail             # newest order at this price (O(1) append)
    double       total_volume     # sum of remaining_qty across all orders
    int          order_count


cdef struct OrderBookSide:
    PriceLevel*  levels           # contiguous array, sorted by price
    int          size             # current number of active levels
    int          capacity         # allocated slots in `levels`
    int          is_descending    # 1 = bids (highest price first), 0 = asks (lowest first)


# Incoming order to match (taker) or insert (maker).
cdef struct COrder:
    int64_t  order_id
    int      side                 # CSide
    int      type                 # CType
    double   price                # limit price (NaN for market)
    double   quantity             # total requested quantity
    double   remaining_qty        # mutable: starts at `quantity`, decremented as fills occur
    int      is_iceberg
    double   visible_qty          # iceberg visible chunk
    double   hidden_qty           # iceberg hidden reserve


# A single executed trade between taker and maker.
cdef struct TradeResult:
    int64_t  taker_order_id
    int64_t  maker_order_id
    double   price
    double   quantity
    int      taker_side           # CSide


# ─── Python-visible COrder wrapper ───────────────────────────────────────────
# A plain C struct cannot be constructed from Python directly. We expose a
# `cdef class` wrapper that holds a COrder value and lets the bridge build
# it from Python-typed arguments. The matcher dereferences `.c_val` to get
# the underlying C struct.

cdef class PyCOrder:
    """Python-visible wrapper around the C `COrder` struct.

    Use `app.matching._bridge.build_corder(...)` to construct instances;
    do not instantiate directly (validation lives in the bridge).
    """
    cdef COrder c_val

    cpdef COrder get_c(self)
    cpdef void _set(self, int64_t order_id, int side, int type,
                    double price, double quantity, double remaining_qty,
                    int is_iceberg, double visible_qty, double hidden_qty)

    # Read-only Python properties are declared in the .pyx (Cython supports
    # @property on cdef class methods without .pxd declarations).


# ─── cdef class — Python-visible API ─────────────────────────────────────────

cdef class CMatchingEngine:
    """Stateless-per-call matching engine.

    Lifecycle
    ---------
    1. Caller constructs a fresh `CMatchingEngine()` (or calls `.reset()`).
    2. Caller loads the opposite-side book snapshot via `add_passive_order(...)`.
    3. Caller invokes `match_active_order(COrder)` for the incoming taker.
    4. Caller reads `.trades` and `.outcome`.
    5. For Post-Only / pure-Limit that didn't fully fill, caller reads
       `remaining_qty` from the returned COrder and inserts the residual
       back into the persistent book (Redis).

    All hot-path methods release the GIL.
    """

    cdef:
        OrderBookSide  _bids
        OrderBookSide  _asks

        # Pre-allocated pool of OrderNode to avoid malloc in hot loop.
        # Pool grows geometrically; nodes are recycled on `reset()`.
        OrderNode*     _node_pool
        int            _node_pool_size
        int            _node_pool_cap

        # Trade result buffer (also pre-allocated).
        TradeResult*   _trades_buf
        int            _trades_count
        int            _trades_cap

    # ─── Internal C-level helpers (nogil) ──────────────────────────────────

    cdef int  _ensure_node_pool_cap(self, int needed) except -1
    cdef int  _ensure_trades_cap(self, int needed) except -1

    cdef OrderNode* _alloc_node(self) nogil
    cdef void       _free_node_list(self, OrderNode* head) nogil

    cdef int  _find_level_idx(self, OrderBookSide* side, double price) noexcept nogil
    cdef int  _insert_level(self, OrderBookSide* side, double price) noexcept nogil
    cdef int  _append_order_to_level(self, PriceLevel* lvl, OrderNode* node) noexcept nogil
    cdef int  _remove_level(self, OrderBookSide* side, int idx) noexcept nogil
    cdef int  _maybe_compact_level(self, OrderBookSide* side, int idx) noexcept nogil

    cdef int  _match_internal(self, COrder* incoming) noexcept nogil
    cdef int  _precheck_fok(self, COrder* incoming) noexcept nogil
    cdef int  _would_cross(self, COrder* incoming) noexcept nogil

    # ─── Python-visible API ───────────────────────────────────────────────

    cpdef bint add_passive_order(self, int64_t order_id, int side, double price,
                                  double qty, bint is_iceberg,
                                  double visible_qty, double hidden_qty) except -1

    cpdef tuple match_active_order(self, PyCOrder incoming)
    """Run taker matching.

    Returns (trades_list, outcome_int, remaining_qty).
    `trades_list` is a Python list of dicts; `outcome_int` maps to CMatchOutcome.
    """

    cpdef bint cancel_order(self, int64_t order_id, int side, double price) except -1

    cpdef tuple snapshot(self, int side, int depth)
    """Returns (list_of_prices, list_of_total_volumes) for top N levels."""

    cpdef void reset(self)
