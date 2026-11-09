
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
