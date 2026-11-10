
## Update: "lost filled events" — RESOLVED
Root cause was in the test harness, not the server:
1. P3 wait loop mixed clocks (`deadline = perf_counter()+60` vs `while time.time() < deadline`)
   so it never waited; counters were sampled ~2-3s after submit while WS fan-out of a
   100-order burst (≈1400 events across 5 private + 1 public channel on one event loop)
   completes in ~3.5s. Fills arrived late, not lost.
2. Cross-run contamination: phase0 balance reset wiped maker locks while stale orders
   stayed in the Redis book → worker settle ValueError → dead-letter + sleep(1) stall.
   Fixed by cancel-all + Redis book purge before any balance writes.

Clean run (perf21): 100/100 taker filled delivered over private WS, 200/200 trade events,
placed+filled 50/50 per user, 0 worker errors.

Remaining REAL server issues (measured honestly now):
- e2e burst latency tail: p50 1480ms, p99 2189ms (fan-out serialization: single subscriber
  task per user, sequential await send_json per connection on one event loop).
- Race: worker can BRPOP a place action before the API transaction commits → taker row
  MISSING → "Match refused" → order stranded in status=new with funds locked
  (observed orders 10159, 11158). Fix: commit before enqueue, or requeue with backoff
  instead of dropping.
- Publishes happen INSIDE the settlement DB transaction: a rollback after publish
  creates ghost events (fire-and-forget pubsub cannot be undone).
- Worker dead-letter path sleeps 1s per poisoned action → queue stalls under backlog.

## FIX: enqueue-before-commit race (implemented + verified)
Root: place()/SL-TP/stop-trigger pushed queue:orders + published WS events
INSIDE the open DB transaction. Worker BRPOPed the action before commit,
read taker row as MISSING, refused the match and stranded the order in
"new" with funds locked (seen at ~1-3 per 100-order burst).

Fix (backend/app):
* NEW app/services/post_commit.py — defer_after_commit(session, factory):
  runs Redis side effects strictly after the owning transaction commits
  (ordered sequential task; rollback = zero side effects).
* order_service: place/bulk place enqueue + index + HASH + publish deferred;
  cancel: book removal + cancel action + events + book snapshot publish
  deferred (snapshot read moved AFTER deferred removal → truthful book);
  modify: all book/queue/event ops deferred in original relative order.
* trade_service: trade/fill publishes deferred to post-commit (kills ghost
  fill events on rollback); SL/TP child registration deferred.
* stop_monitor_service: triggered child enqueue + HASH/index + stop removal
  + events deferred.
* matching/worker.py: defense-in-depth — TakerOrderInvalidError with
  status=MISSING now requeues the action (bounded, 5 attempts, 50ms×n
  backoff) instead of dropping it; exhausted → dead-letter with explicit
  stranded-order error. OrderActionPayload gained attempt + _raw fields.

Verification:
* test_postcommit.py: rollback → 0 events/0 queue push; commit → events
  fire post-commit and row is visible; attempt plumbing OK.
* test_sltp_smoke.py: full SL chain (fill → sl_tp_activated → stop armed in
  ZSET → trigger → child market order filled with real trade) works on the
  deferred paths.
* perf23/perf24 (back-to-back): 100/100 taker filled delivered, 200/200
  trade events, 50/50 order events per user, refused=0, requeued=0
  (safety net never fires now), worker errors=0, post-commit failures=0.
* P2 REST write improved as side effect: 86 → 103 req/s (p50 38ms).

Remaining known issues (unfixed, measured):
* WS burst tail latency p50 ~1.2s / p99 ~2s (single-task fan-out per user).
* BalanceVersionConflict → occasional 500 on concurrent same-account POSTs
  (1/400); should be 409 or per-account queue.
* Worker sleeps 1s per dead-lettered action (poison backlog stall).

## Clean rebuild verification (down + build --no-cache + up)
All 6 containers rebuilt from repo @ fa12041; fix presence verified inside
images (post_commit imports, worker requeue constant, frontend bundle).
* Cython microbench: mean 0.44us, p50 0.42us, p99 0.55us, GIL released.
* P1 REST read: 241 req/s, p50 32ms.
* P2 REST write: 89 req/s, p50 43ms, 1x500 (known BalanceVersionConflict).
* P3 E2E: 100/100 filled, 200/200 trades, 50/50 order events per user,
  refused=0, requeued=0, worker errors=0.
* WS burst tail (real, unfixed): e2e p50 1314ms, p99 2102ms.


## P5 order-type coverage (2026-11-09, run perf36)

Functional checks for every OrderType + lifecycle: 17/17 PASS.
limit-GTC taker, IOC (partial→expired, no rest, locks released), FOK fill / no-liquidity
(row closed, funds released, zero trades), post_only crossing (rejected, funds released),
iceberg hidden backfill, stop_market (armed→triggered→child filled), stop_limit
(armed→triggered→child), trailing_stop armed state, market+SL/TP children,
modify (cancel/replace), bulk place. P3 regression stayed 100/100 filled, P1 264 r/s.

Server bugs found by P5 and fixed (all verified by re-run):

1. **Worker reject paths never closed the order row.** post_only-cross and FOK-fail
   published only an event: the row stayed `new` with `locked_amount` held and no book
   entry — funds stuck until manual cancel. `worker._finalize_not_executed()` now closes
   the row (REJECTED/EXPIRED), pessimistically unlocks the reservation and drops the
   open-orders index + order HASH. IOC remainder gets the same treatment (EXPIRED) and
   an `expired` event.
2. **Stop/trailing registration died under post-commit deferral.** `_register_stop_order`
   touched expired ORM instances after the request session closed. It now takes a plain
   value snapshot (`_stop_order_snapshot`); stop_monitor's triggered-effects closure
   likewise captures scalars instead of reading `order.*` after commit.
3. **Engine float dust poisoned settlement.** The matcher tracks remainders in float;
   fill quantities like 0.0025000000000000005 summed past the exact Decimal lock and
   `settle_pessimistic` raised `Insufficient locked balance ... need 0.005000000000000000204,
   have 0.005` — valid matches dead-lettered. `trade_service.persist_trade_batch` now
   clamps every fill leg to its own exact remaining, and the worker quantizes float
   remainders (`_quant`, 12 dp) before writing them back into Redis HASHes.

Known issues still open:
- `BalanceVersionConflict` → HTTP 500 under concurrent placements for one user
  (optimistic balance lock, 3 retries insufficient; harness retries around it).
- Stop monitor reaction to a print can lag several seconds right after a burst
  (P3-scale event backlog); trigger semantics itself verified correct.
- One-off `Insufficient locked` guard trip per run under full-suite load (a settle
  attempted against an already-zero per-order lock). The pessimistic guard rejected
  it, balances verified consistent afterwards (order locks sum to zero, DLQ empty).
  Root cause not yet pinned down.

Harness (perf_suite.py P5) notes: the order-type phase now starts with the same
clean-slate purge as phase0 (cancel resting + purge stops/trailing/ob keys), because
P2–P4 residual liquidity measurably changes trigger/precheck outcomes. The market-buy
precheck validates against visible ask volume only, so the iceberg hidden-reserve
check drives in via two consecutive buys (visible first, then backfilled hidden).


## Self-trade prevention (STP) — 2026-11-10

Policy: a taker never matches its own resting orders. The maker order stays
untouched in the book and the taker continues against other participants
(skip-and-continue, classic "deactivate maker" flavour). Implemented at the
matcher level so every entry point (market/limit/IOC/FOK/post-only, stop
children, SL/TP children, bulk) inherits it for free.

Engine (`engine.pxd/.pyx`): `user_id` added to `OrderNode`/`COrder`;
matching loop skips same-user makers; FOK precheck sums only foreign
volume (own liquidity never makes an FOK fillable); post-only resting
check treats a level composed entirely of own orders as non-crossing
(rests instead of rejecting). `user_id=0` = unknown owner (legacy/bench)
and is never skipped, so benchmarks and old data keep working.
Worker passes owner ids through `_bridge.build_corder`/`add_passive_order`
and has a belt-and-braces guard dropping any same-user trade.

Related fixes found while testing STP:
- MARKET BUY lock (`_compute_market_buy_lock`) now excludes the taker's own
  asks from its book walk. Without this, STP skipping an own (better) ask
  made the real fill more expensive than the lock → "Insufficient locked
  balance for settle" and a stranded order.
- Worker: a settle ValueError now closes the order (EXPIRED) and releases
  the reservation instead of leaving it in 'new' with funds locked.
- perf harness: `api()` retry lost the `creds` argument and re-attributed
  retried requests to the default user (harness bug, masked as "orders
  owned by wrong user" in P5 STP run).

Verification (perf38): P5 19/19 including
- "STP: market buy skips own best ask, fills foreign level" (own ask stays
  `new`, buy fills at foreign 99.0),
- "STP: own-only liquidity → expired, no trades, funds released";
engine unit tests 33/33 (6 new STP tests); 0 same-user trades across the
whole run in DB; P3 regression 100/100 fills unchanged.
