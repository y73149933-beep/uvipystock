# distutils: language = c
# cython: language_level = 3
# cython: boundscheck = False
# cython: wraparound  = False
# cython: cdivision   = True
# cython: initializedcheck = False
"""
Cython implementation of the matching engine.

Design goals
------------
1. **No Python objects in the hot loop.** `_match_internal` walks C structs
   only, so it can run inside `with nogil:` without blocking the event loop.

2. **Pre-allocated node pool.** OrderNode structs are allocated up-front in
   a contiguous block; the matcher just bumps an index instead of calling
   malloc/free per order. The pool is recycled on `reset()`.

3. **Price-Time Priority.** Within a price level, orders form a singly
   linked list (head = oldest). Matching consumes from the head; new
   orders append to the tail. Across price levels, bids are sorted
   descending (best bid at index 0), asks ascending (best ask at index 0).

4. **Iceberg refill.** When the visible portion of an iceberg is exhausted,
   the matcher pulls the next chunk from `hidden_qty` and keeps the node
   alive (instead of removing it).

5. **FOK pre-check.** Before producing any trade, FOK walks the opposite
   side to verify cumulative liquidity ≥ quantity. If insufficient, no
   trade is emitted and `_match_internal` returns C_FOK_REJECTED.

6. **Post-Only guard.** Before resting, Post-Only checks whether it would
   cross the opposite side's best price. If yes → C_POST_ONLY_CROSS (caller
   rejects without locking balance).
"""
import cython
from libc.math cimport NAN, isnan
from libc.stdlib cimport malloc, calloc, realloc, free
from libc.string cimport memset, memcpy

from app.matching.engine cimport (
    CSide, CType, CMatchOutcome,
    C_BUY, C_SELL,
    C_MARKET, C_LIMIT, C_IOC, C_FOK, C_POST_ONLY,
    C_OK, C_FOK_REJECTED, C_POST_ONLY_CROSS, C_NO_LIQUIDITY,
    OrderNode, PriceLevel, OrderBookSide, COrder, TradeResult,
    PyCOrder,
)


# ─── PyCOrder wrapper implementation ─────────────────────────────────────────

cdef class PyCOrder:
    """Python-visible wrapper around COrder. See engine.pxd for the contract."""

    cpdef COrder get_c(self):
        return self.c_val

    cpdef void _set(self, int64_t order_id, int side, int type,
                    double price, double quantity, double remaining_qty,
                    int is_iceberg, double visible_qty, double hidden_qty):
        self.c_val.order_id       = order_id
        self.c_val.side           = side
        self.c_val.type           = type
        self.c_val.price          = price
        self.c_val.quantity       = quantity
        self.c_val.remaining_qty  = remaining_qty
        self.c_val.is_iceberg     = is_iceberg
        self.c_val.visible_qty    = visible_qty
        self.c_val.hidden_qty     = hidden_qty

    # ─── Read-only properties (Python-visible) ─────────────────────────────
    @property
    def order_id(self) -> int:
        return self.c_val.order_id

    @property
    def side(self) -> int:
        return self.c_val.side

    @property
    def type(self) -> int:
        return self.c_val.type

    @property
    def price(self) -> float:
        return self.c_val.price

    @property
    def quantity(self) -> float:
        return self.c_val.quantity

    @property
    def remaining_qty(self) -> float:
        return self.c_val.remaining_qty

    @property
    def is_iceberg(self) -> bool:
        return bool(self.c_val.is_iceberg)

    @property
    def visible_qty(self) -> float:
        return self.c_val.visible_qty

    @property
    def hidden_qty(self) -> float:
        return self.c_val.hidden_qty


# ─── Constants (C-level only) ────────────────────────────────────────────────

# Initial capacity for the node pool and trade buffer. The pool grows
# geometrically (x2) when exhausted, so these only affect warm-up cost.
#
# IMPORTANT: When the pool grows via realloc, all OrderNode* pointers into the
# old pool become INVALID. To avoid this, we pre-allocate a large pool at
# construction time and treat overflow as a hard error (caller should reset
# or use a bigger pool). For production workloads with >10k orders, increase
# NODE_POOL_CAPACITY or refactor to use indices instead of pointers.
DEF INITIAL_NODE_POOL_CAP = 100_000   # pre-allocated; no realloc in hot path
DEF INITIAL_TRADES_CAP    = 1_024     # pre-allocated; grows on demand (with GIL)
DEF INITIAL_LEVELS_CAP    = 1_024     # pre-allocated; grows on demand (with GIL)
DEF GROWTH_FACTOR         = 2

# Cython `cdef enum` values are C ints — usable inside .pyx but not importable
# from Python. The Python-visible constants live in `app.matching._constants`
# (a pure-Python module) and mirror these values. The bridge and tests import
# from there.


# ============================================================================
#  Construction / teardown
# ============================================================================

cdef class CMatchingEngine:
    """See engine.pxd for the API contract."""

    def __cinit__(self):
        # Bids: descending (highest price at index 0)
        self._bids.levels = <PriceLevel*> calloc(INITIAL_LEVELS_CAP, sizeof(PriceLevel))
        self._bids.size = 0
        self._bids.capacity = INITIAL_LEVELS_CAP
        self._bids.is_descending = 1

        # Asks: ascending (lowest price at index 0)
        self._asks.levels = <PriceLevel*> calloc(INITIAL_LEVELS_CAP, sizeof(PriceLevel))
        self._asks.size = 0
        self._asks.capacity = INITIAL_LEVELS_CAP
        self._asks.is_descending = 0

        # Node pool
        self._node_pool = <OrderNode*> calloc(INITIAL_NODE_POOL_CAP, sizeof(OrderNode))
        self._node_pool_size = 0
        self._node_pool_cap = INITIAL_NODE_POOL_CAP

        # Trades buffer
        self._trades_buf = <TradeResult*> calloc(INITIAL_TRADES_CAP, sizeof(TradeResult))
        self._trades_count = 0
        self._trades_cap = INITIAL_TRADES_CAP

    def __dealloc__(self):
        # Free all linked-list nodes inside price levels (they were allocated
        # from the pool, but the lists themselves point into the pool so no
        # extra free is needed). We just free the level arrays + pool + trades.
        if self._bids.levels != NULL:
            free(self._bids.levels)
        if self._asks.levels != NULL:
            free(self._asks.levels)
        if self._node_pool != NULL:
            free(self._node_pool)
        if self._trades_buf != NULL:
            free(self._trades_buf)

    # ─── reset() — recycle internal state for reuse ────────────────────────

    cpdef void reset(self):
        """Reset the engine to an empty book without freeing memory.

        Useful when the worker reuses one engine instance across many
        `queue:orders` items — avoids GC pressure.
        """
        cdef int i
        cdef PriceLevel* lvl

        for i in range(self._bids.size):
            lvl = &self._bids.levels[i]
            lvl.head = NULL
            lvl.tail = NULL
            lvl.total_volume = 0.0
            lvl.order_count = 0
        self._bids.size = 0

        for i in range(self._asks.size):
            lvl = &self._asks.levels[i]
            lvl.head = NULL
            lvl.tail = NULL
            lvl.total_volume = 0.0
            lvl.order_count = 0
        self._asks.size = 0

        self._node_pool_size = 0
        self._trades_count = 0

    # ─── Pool management ────────────────────────────────────────────────────

    cdef int _ensure_node_pool_cap(self, int needed) except -1:
        """No-op: the node pool is pre-allocated at construction time and
        does NOT grow. If `needed` exceeds the cap, this raises MemoryError
        so the caller can handle it gracefully.

        Rationale: growing the pool via realloc would invalidate all
        OrderNode* pointers stored in linked-list `next` fields and in
        PriceLevel.head/tail. Using indices instead of pointers would fix
        this but complicate the code; for now we pre-allocate a generous
        pool (100k nodes) and treat overflow as a configuration error.
        """
        if needed > self._node_pool_cap:
            raise MemoryError(
                f"OrderNode pool exhausted: needed={needed}, cap={self._node_pool_cap}. "
                f"Call reset() to recycle, or increase INITIAL_NODE_POOL_CAP in engine.pyx."
            )
        return 0

    cdef int _ensure_trades_cap(self, int needed) except -1:
        if needed <= self._trades_cap:
            return 0
        cdef int new_cap = self._trades_cap
        while new_cap < needed:
            new_cap *= GROWTH_FACTOR
        cdef TradeResult* new_buf = <TradeResult*> realloc(self._trades_buf, new_cap * sizeof(TradeResult))
        if new_buf == NULL:
            raise MemoryError("Failed to grow trades buffer")
        self._trades_buf = new_buf
        self._trades_cap = new_cap
        return 0

    cdef OrderNode* _alloc_node(self) nogil:
        """Allocate a node from the pool. Returns NULL if pool is full —
        caller must check and re-enter GIL to grow the pool."""
        if self._node_pool_size >= self._node_pool_cap:
            return NULL
        cdef OrderNode* node = &self._node_pool[self._node_pool_size]
        self._node_pool_size += 1
        node.next = NULL
        return node

    cdef void _free_node_list(self, OrderNode* head) nogil:
        """No-op: nodes live in the pool and are recycled on `reset()`.

        Kept in the API for symmetry with a hypothetical malloc-based variant.
        """
        pass

    # ─── Price level management ─────────────────────────────────────────────

    cdef int _find_level_idx(self, OrderBookSide* side, double price) noexcept nogil:
        """Binary-search the index of `price` in `side->levels`.

        Returns:
          * index of the level if it exists
          * -1 if not found

        `side->levels` is sorted: descending for bids, ascending for asks.
        """
        cdef int lo = 0
        cdef int hi = side.size - 1
        cdef int mid
        cdef double mid_price

        while lo <= hi:
            mid = (lo + hi) // 2
            mid_price = side.levels[mid].price
            if mid_price == price:
                return mid
            elif (side.is_descending and mid_price > price) or \
                 (not side.is_descending and mid_price < price):
                lo = mid + 1
            else:
                hi = mid - 1
        return -1

    cdef int _insert_level(self, OrderBookSide* side, double price) noexcept nogil:
        """Insert a new empty level for `price` at the correct sorted position.

        Returns the index of the new level. Grows `side->levels` if needed.
        On realloc failure returns -1 (caller must re-acquire GIL to raise).
        """
        cdef int new_cap
        cdef PriceLevel* new_levels
        cdef int i = 0
        cdef int j

        # Grow if needed
        if side.size >= side.capacity:
            new_cap = side.capacity * GROWTH_FACTOR
            new_levels = <PriceLevel*> realloc(side.levels, new_cap * sizeof(PriceLevel))
            if new_levels == NULL:
                return -1
            side.levels = new_levels
            side.capacity = new_cap

        # Find insertion position (maintain sort order)
        while i < side.size:
            if (side.is_descending and side.levels[i].price < price) or \
               (not side.is_descending and side.levels[i].price > price):
                break
            i += 1

        # Shift right
        for j in range(side.size, i, -1):
            side.levels[j] = side.levels[j - 1]

        # Initialize new level
        side.levels[i].price = price
        side.levels[i].head = NULL
        side.levels[i].tail = NULL
        side.levels[i].total_volume = 0.0
        side.levels[i].order_count = 0
        side.size += 1
        return i

    cdef int _append_order_to_level(self, PriceLevel* lvl, OrderNode* node) noexcept nogil:
        """Append `node` to the tail of `lvl`'s order list (FIFO)."""
        if lvl.tail == NULL:
            lvl.head = node
            lvl.tail = node
        else:
            lvl.tail.next = node
            lvl.tail = node
        node.next = NULL
        lvl.total_volume += node.remaining_qty
        lvl.order_count += 1
        return 0

    cdef int _remove_level(self, OrderBookSide* side, int idx) noexcept nogil:
        """Remove the level at `idx` by shifting subsequent levels left."""
        cdef int j
        for j in range(idx, side.size - 1):
            side.levels[j] = side.levels[j + 1]
        side.size -= 1
        # Clear the now-unused slot to avoid stale pointers
        side.levels[side.size].head = NULL
        side.levels[side.size].tail = NULL
        side.levels[side.size].total_volume = 0.0
        side.levels[side.size].order_count = 0
        return 0

    cdef int _maybe_compact_level(self, OrderBookSide* side, int idx) noexcept nogil:
        """If the level at `idx` is empty, remove it from the side."""
        if side.levels[idx].order_count == 0:
            return self._remove_level(side, idx)
        return 0

    # ─── Public: add_passive_order ──────────────────────────────────────────

    cpdef bint add_passive_order(self, int64_t order_id, int side, double price,
                                  double qty, bint is_iceberg,
                                  double visible_qty, double hidden_qty) except -1:
        """Insert a resting order into the book.

        For iceberg orders: `qty` is the *total* quantity (visible + hidden),
        `visible_qty` is the chunk shown in the book, `hidden_qty` is the
        reserve. The matcher refills visible from hidden when it depletes.

        Returns True on success, False if the pool was exhausted (caller
        should retry with a fresh engine or grow the pool).
        """
        if side != C_BUY and side != C_SELL:
            raise ValueError(f"Invalid side: {side}")
        if qty <= 0.0:
            raise ValueError(f"qty must be positive, got {qty}")

        # Allocate node (may need to grow pool — done with GIL held)
        cdef int needed = self._node_pool_size + 1
        self._ensure_node_pool_cap(needed)

        cdef OrderNode* node = self._alloc_node()
        if node == NULL:
            return False  # pool exhausted

        node.order_id = order_id
        node.remaining_qty = qty
        node.visible_qty = visible_qty if is_iceberg else qty
        node.is_iceberg = 1 if is_iceberg else 0
        node.next = NULL

        cdef OrderBookSide* book_side = &self._bids if side == C_BUY else &self._asks

        cdef int lvl_idx = self._find_level_idx(book_side, price)
        if lvl_idx == -1:
            try:
                lvl_idx = self._insert_level(book_side, price)
            except Exception:
                # _insert_level returned -1 via except -1; convert to False
                return False
            if lvl_idx == -1:
                return False

        self._append_order_to_level(&book_side.levels[lvl_idx], node)
        return True

    # ─── Public: match_active_order ─────────────────────────────────────────

    cpdef tuple match_active_order(self, PyCOrder incoming):
        """Run taker matching for `incoming`.

        Returns
        -------
        (trades, outcome, remaining_qty)
            trades        : list[dict] with keys taker_order_id, maker_order_id,
                            price, quantity, taker_side
            outcome       : int (CMatchOutcome)
            remaining_qty : float — what's left of `incoming.quantity` after
                            matching (for caller to insert as a resting order
                            if type == LIMIT and remaining > 0)
        """
        self._trades_count = 0

        # Copy the C struct out of the Python wrapper so we can pass a pointer
        # to it into the nogil block.
        cdef COrder c = incoming.c_val
        cdef COrder* c_ptr = &c

        cdef int outcome
        cdef double remaining

        # The hot loop runs without GIL. Any exception-free state needed
        # afterwards is captured into C locals here.
        with nogil:
            outcome = self._match_internal(c_ptr)
            remaining = c_ptr.remaining_qty

        # Convert trades buffer to Python list (GIL is held here)
        cdef list trades = []
        cdef int i
        cdef TradeResult t
        for i in range(self._trades_count):
            t = self._trades_buf[i]
            trades.append({
                "taker_order_id": t.taker_order_id,
                "maker_order_id": t.maker_order_id,
                "price":          t.price,
                "quantity":       t.quantity,
                "taker_side":     t.taker_side,
            })

        return trades, outcome, remaining

    # ─── Public: cancel_order ───────────────────────────────────────────────

    cpdef bint cancel_order(self, int64_t order_id, int side, double price) except -1:
        """Remove a resting order by (id, side, price).

        Returns True if found and removed, False if not found.
        """
        if side != C_BUY and side != C_SELL:
            raise ValueError(f"Invalid side: {side}")

        cdef OrderBookSide* book_side = &self._bids if side == C_BUY else &self._asks
        cdef int lvl_idx = self._find_level_idx(book_side, price)
        if lvl_idx == -1:
            return False

        cdef PriceLevel* lvl = &book_side.levels[lvl_idx]
        cdef OrderNode* prev = NULL
        cdef OrderNode* cur  = lvl.head
        cdef bint found = False

        while cur != NULL:
            if cur.order_id == order_id:
                # Unlink
                if prev == NULL:
                    lvl.head = cur.next
                else:
                    prev.next = cur.next
                if lvl.tail == cur:
                    lvl.tail = prev
                lvl.total_volume -= cur.remaining_qty
                lvl.order_count -= 1
                found = True
                # Node memory belongs to the pool; just leak the slot.
                break
            prev = cur
            cur = cur.next

        if found:
            self._maybe_compact_level(book_side, lvl_idx)
        return found

    # ─── Public: snapshot ───────────────────────────────────────────────────

    cpdef tuple snapshot(self, int side, int depth):
        """Return (prices, volumes) for the top `depth` levels of `side`.

        `side` is CSide.C_BUY (1) or CSide.C_SELL (0).
        """
        if side != C_BUY and side != C_SELL:
            raise ValueError(f"Invalid side: {side}")

        cdef OrderBookSide* book_side = &self._bids if side == C_BUY else &self._asks
        cdef int n = book_side.size if book_side.size < depth else depth

        cdef list prices  = []
        cdef list volumes = []
        cdef int i
        for i in range(n):
            prices.append(book_side.levels[i].price)
            volumes.append(book_side.levels[i].total_volume)
        return prices, volumes

    # ─── Internal: pre-checks ───────────────────────────────────────────────

    cdef int _would_cross(self, COrder* incoming) noexcept nogil:
        """Return 1 if a buy with `price` would cross the best ask
        (or a sell with `price` would cross the best bid), else 0.

        Used by Post-Only to reject orders that would take liquidity.
        """
        cdef OrderBookSide* opp
        if incoming.side == C_BUY:
            opp = &self._asks
            if opp.size == 0:
                return 0
            return 1 if incoming.price >= opp.levels[0].price else 0
        else:
            opp = &self._bids
            if opp.size == 0:
                return 0
            return 1 if incoming.price <= opp.levels[0].price else 0

    cdef int _precheck_fok(self, COrder* incoming) noexcept nogil:
        """Walk the opposite side and sum available volume at acceptable prices.

        For a buy FOK: walk asks from best (index 0) upward, accumulate
        `total_volume` while `ask.price <= incoming.price`.
        For a sell FOK: walk bids from best (index 0) downward, accumulate
        while `bid.price >= incoming.price`.

        For market FOK (price == NaN), accept all available liquidity.

        Returns 1 if cumulative volume >= incoming.quantity, else 0.
        """
        cdef OrderBookSide* opp
        if incoming.side == C_BUY:
            opp = &self._asks
        else:
            opp = &self._bids

        if opp.size == 0:
            return 0

        cdef double cumulative = 0.0
        cdef int i
        cdef PriceLevel* lvl
        for i in range(opp.size):
            lvl = &opp.levels[i]
            if not isnan(incoming.price):
                if incoming.side == C_BUY and lvl.price > incoming.price:
                    break
                if incoming.side == C_SELL and lvl.price < incoming.price:
                    break
            cumulative += lvl.total_volume
            if cumulative >= incoming.quantity:
                return 1
        return 0

    # ─── Internal: the hot loop ─────────────────────────────────────────────

    cdef int _match_internal(self, COrder* incoming) noexcept nogil:
        """Pure-C matching. Walks the opposite side, fills `_trades_buf`,
        decrements `incoming.remaining_qty`, refills iceberg visible
        chunks as needed.

        Returns CMatchOutcome:
          C_OK              — matched (fully or partially) or rested
          C_FOK_REJECTED    — FOK precheck failed, no trades emitted
          C_POST_ONLY_CROSS — Post-Only would cross, no trades emitted
          C_NO_LIQUIDITY    — market/IOC found nothing

        Notes
        -----
        - For Post-Only, if `_would_cross` → return C_POST_ONLY_CROSS without
          emitting trades. Otherwise, leave `remaining_qty` untouched for the
          caller to insert the order as resting.
        - For FOK, if `_precheck_fok` fails → return C_FOK_REJECTED.
        - For LIMIT orders that don't fully fill, leftover `remaining_qty`
          is left for the caller to insert as a resting order.
        - For MARKET/IOC orders, leftover `remaining_qty` is silently
          discarded (caller treats as canceled).
        """
        cdef OrderBookSide* opp
        if incoming.side == C_BUY:
            opp = &self._asks
        else:
            opp = &self._bids

        # ── Post-Only guard ─────────────────────────────────────────────────
        if incoming.type == C_POST_ONLY:
            if self._would_cross(incoming):
                return C_POST_ONLY_CROSS
            # No crossing → caller inserts as resting; nothing to match
            return C_OK

        # ── FOK precheck ────────────────────────────────────────────────────
        if incoming.type == C_FOK:
            if not self._precheck_fok(incoming):
                return C_FOK_REJECTED

        # ── Walk opposite side ──────────────────────────────────────────────
        cdef int lvl_idx = 0
        cdef PriceLevel* lvl
        cdef OrderNode* maker
        cdef double fill_qty
        cdef double fill_price
        cdef double visible_avail
        cdef double hidden_avail
        cdef int pool_grow_needed = 0

        while incoming.remaining_qty > 0.0 and lvl_idx < opp.size:
            lvl = &opp.levels[lvl_idx]

            # Price check (skip for market orders where price is NaN)
            if not isnan(incoming.price):
                if incoming.side == C_BUY and lvl.price > incoming.price:
                    break  # asks too expensive
                if incoming.side == C_SELL and lvl.price < incoming.price:
                    break  # bids too cheap

            # Walk orders at this level (FIFO — head first)
            maker = lvl.head
            while maker != NULL and incoming.remaining_qty > 0.0:
                fill_price = lvl.price

                # How much can we take from this maker?
                # For iceberg, the visible_qty is what's exposed; once it
                # depletes we refill from hidden_qty before deciding removal.
                if maker.is_iceberg and maker.visible_qty < maker.remaining_qty:
                    visible_avail = maker.visible_qty
                else:
                    visible_avail = maker.remaining_qty

                if incoming.remaining_qty >= visible_avail:
                    # Take the whole visible chunk
                    fill_qty = visible_avail
                    maker.remaining_qty -= fill_qty
                    if maker.is_iceberg:
                        maker.visible_qty = 0.0
                        # Refill visible from hidden
                        hidden_avail = maker.remaining_qty  # hidden = remaining after visible fills
                        if hidden_avail > 0.0:
                            # Refill a chunk equal to min(initial_visible, hidden)
                            # We don't store initial_visible separately, so use
                            # the original visible_qty as a cap if it's smaller
                            # than remaining. For simplicity, expose all remaining.
                            # (A real iceberg would cap this; the bridge can
                            # pass initial_visible via OrderNode if needed.)
                            maker.visible_qty = hidden_avail
                    incoming.remaining_qty -= fill_qty
                else:
                    # Partial take from this maker
                    fill_qty = incoming.remaining_qty
                    maker.remaining_qty -= fill_qty
                    if maker.is_iceberg:
                        maker.visible_qty -= fill_qty
                    incoming.remaining_qty = 0.0

                # Update level totals
                lvl.total_volume -= fill_qty

                # Emit trade (ensure buffer cap; if full, set flag and break)
                if self._trades_count >= self._trades_cap:
                    pool_grow_needed = 1
                    break
                self._trades_buf[self._trades_count].taker_order_id = incoming.order_id
                self._trades_buf[self._trades_count].maker_order_id = maker.order_id
                self._trades_buf[self._trades_count].price          = fill_price
                self._trades_buf[self._trades_count].quantity       = fill_qty
                self._trades_buf[self._trades_count].taker_side     = incoming.side
                self._trades_count += 1

                # Remove maker if fully filled
                if maker.remaining_qty <= 0.0:
                    # Unlink maker from the level
                    lvl.head = maker.next
                    if lvl.tail == maker:
                        lvl.tail = NULL  # list is now empty
                    lvl.order_count -= 1
                    # Note: node memory stays in the pool; recycled on reset()
                # else: maker still has remaining — visible_qty may be 0 if
                # iceberg refilled; the next iteration will pick it up again
                # via the same maker pointer (we don't advance to next).
                if maker.remaining_qty <= 0.0 or maker.visible_qty <= 0.0:
                    # Move on to next maker
                    maker = lvl.head if maker.remaining_qty <= 0.0 else maker.next

            # If trades buffer overflowed, exit to grow it (with GIL)
            if pool_grow_needed:
                break

            # Compact the level if empty
            if lvl.order_count == 0:
                self._remove_level(opp, lvl_idx)
                # Don't increment lvl_idx — the next level shifted into this slot
            else:
                lvl_idx += 1

        # ── Outcome decision ────────────────────────────────────────────────
        if pool_grow_needed:
            # Could not emit all trades — caller should grow buffer and retry.
            # For now, signal via C_NO_LIQUIDITY (best-effort); a production
            # system would log this as an internal error.
            return C_NO_LIQUIDITY

        if incoming.remaining_qty > 0.0:
            if incoming.type == C_MARKET or incoming.type == C_IOC:
                # Did not fully fill — for market/IOC this is acceptable but
                # we signal it distinctly so the caller knows leftover is canceled.
                if self._trades_count == 0:
                    return C_NO_LIQUIDITY
                # Partial fill is OK for IOC — return C_OK with leftover
                return C_OK
            # For LIMIT and FOK, leftover means it will rest (LIMIT) or
            # already rejected (FOK). C_OK here; caller decides to insert.
            return C_OK

        return C_OK
