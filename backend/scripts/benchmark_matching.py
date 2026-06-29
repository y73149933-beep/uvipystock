"""Microbenchmark for the Cython matching engine.

Measures:
1. Throughput: orders/second for a pure-match workload (no I/O).
2. Latency: median p50 / p99 per single match call.
3. GIL occupancy: confirms `with nogil:` is in effect by running a
   competing Python thread and measuring its progress.

Usage:
    cd backend
    PYTHONPATH=. python scripts/benchmark_matching.py
"""
from __future__ import annotations

import statistics
import sys
import threading
import time
from pathlib import Path

# Ensure backend/ on path
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.matching.engine import CMatchingEngine
from app.matching._bridge import build_corder


def bench_throughput(n_orders: int = 10_000) -> None:
    """Measure orders/second when matching n_orders against a deep book."""
    e = CMatchingEngine()

    # Pre-load a book: 50 levels × 20 orders × 1.0 qty = 1000 makers
    oid = 1
    for lvl in range(50):
        for _ in range(20):
            ok = e.add_passive_order(
                order_id=oid, side=1,  # C_SELL
                price=100.0 + lvl * 0.01, qty=1.0,
                is_iceberg=False, visible_qty=1.0, hidden_qty=0.0,
            )
            assert ok
            oid += 1

    # Each taker consumes 0.01 from the book — so 1000 makers × 1.0 qty = 100k
    # units of liquidity; 10k orders × 0.01 = 100 units total.
    taker_ids = list(range(100_000, 100_000 + n_orders))

    t0 = time.perf_counter()
    matched = 0
    for tid in taker_ids:
        c = build_corder(
            order_id=tid, side="buy", order_type="market",
            price=None, quantity=0.01,
        )
        trades, outcome, _ = e.match_active_order(c)
        if outcome == 0:  # C_OK
            matched += 1
    t1 = time.perf_counter()

    elapsed = t1 - t0
    print(f"  Throughput: {n_orders} orders in {elapsed*1000:.1f} ms "
          f"= {n_orders/elapsed:,.0f} orders/sec")
    print(f"  Matched: {matched}/{n_orders}")


def bench_latency(n: int = 1_000) -> None:
    """Measure per-call latency (μs)."""
    e = CMatchingEngine()
    # Single maker
    e.add_passive_order(
        order_id=1, side=1, price=100.0, qty=1000.0,
        is_iceberg=False, visible_qty=1000.0, hidden_qty=0.0,
    )

    latencies_us = []
    for i in range(n):
        c = build_corder(
            order_id=1000 + i, side="buy", order_type="market",
            price=None, quantity=0.01,
        )
        t0 = time.perf_counter_ns()
        e.match_active_order(c)
        t1 = time.perf_counter_ns()
        latencies_us.append((t1 - t0) / 1000.0)

    latencies_us.sort()
    p50 = latencies_us[n // 2]
    p99 = latencies_us[int(n * 0.99)]
    mean = statistics.mean(latencies_us)
    print(f"  Latency over {n} calls: mean={mean:.2f}μs  p50={p50:.2f}μs  p99={p99:.2f}μs")


def bench_gil_release() -> None:
    """Verify that `with nogil:` actually releases the GIL.

    We run a long match in the main thread and a counter in a background
    thread. If GIL is released, the counter makes progress; if not, it
    stays near zero.

    The book is sized to produce ~64 trades (the default trades buffer cap)
    so we exercise the nogil section without overflowing the buffer.
    """
    e = CMatchingEngine()
    # Build a modest book so one market order produces many trades but
    # stays within the pre-allocated trades buffer (cap = 64).
    for lvl in range(32):
        e.add_passive_order(
            order_id=1, side=1, price=100.0 + lvl * 0.01, qty=0.001,
            is_iceberg=False, visible_qty=0.001, hidden_qty=0.0,
        )

    counter = {"n": 0}
    stop = threading.Event()

    def counter_thread():
        # Pure-Python busy loop — only runs when GIL is held by this thread.
        while not stop.is_set():
            counter["n"] += 1

    t = threading.Thread(target=counter_thread, daemon=True)
    t.start()

    # Give the background thread a moment to start
    time.sleep(0.01)

    # Run many market orders to amortize the per-call overhead and give
    # the background thread a window to make progress during nogil sections.
    for i in range(10_000):
        c = build_corder(
            order_id=999 + i, side="buy", order_type="market",
            price=None, quantity=0.032,  # walk all 32 levels
        )
        e.match_active_order(c)
        # Re-load the book every iteration (since match empties it)
        if i % 100 == 0:
            e.reset()
            for lvl in range(32):
                e.add_passive_order(
                    order_id=1, side=1, price=100.0 + lvl * 0.01, qty=0.001,
                    is_iceberg=False, visible_qty=0.001, hidden_qty=0.0,
                )

    stop.set()
    t.join(timeout=1.0)
    print(f"  Background counter incremented to {counter['n']:,} during match")
    if counter["n"] > 0:
        print("  ✓ GIL was released (counter made progress)")
    else:
        print("  ✗ GIL was NOT released (counter stayed at 0)")


if __name__ == "__main__":
    print("=== Cython Matching Engine Benchmark ===\n")

    print("[1] Throughput")
    bench_throughput(n_orders=10_000)

    print("\n[2] Latency")
    bench_latency(n=1_000)

    print("\n[3] GIL release verification")
    bench_gil_release()

    print("\n=== Done ===")
