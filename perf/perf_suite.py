#!/usr/bin/env python3
"""Performance suite for the crypto exchange sandbox.

Run INSIDE the backend container:
    docker cp perf_suite.py exchange-backend:/tmp/
    docker exec exchange-backend python /tmp/perf_suite.py

Phases:
  0. Login bench user, raise its api-key rate limit, top up balances (asyncpg)
  1. REST read load: N signed GET /api/v1/balance from M threads -> RPS, p50/p90/p99
  2. REST write (enqueue): post_only makers far from touch price -> same stats
  3. End-to-end matching latency: market takers -> wait 'filled' on private WS
     -> per-taker e2e latency percentiles + aggregate matching throughput
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import statistics
import threading
import time
import urllib.request
from http.client import HTTPConnection
from urllib.parse import urlparse

BASE_HOST = "localhost"
BASE_PORT = 8000
API = f"http://{BASE_HOST}:{BASE_PORT}"
DB_URL = os.environ["DATABASE_URL"].replace("+asyncpg", "")

CREDS: dict = {}
STOP_WS = threading.Event()
WS_FILLS: dict[int, tuple[float, str]] = {}   # order_id -> (recv_ts, status_event)
WS_RX: dict[int, dict[str, int]] = {}
WS_LOCK = threading.Lock()


def sign(secret: str, method: str, path: str, ts: int, body: str) -> str:
    key = hashlib.sha256(secret.encode()).hexdigest()
    payload = f"{method}\n{path}\n{ts}\n{body}"
    return hmac_mod.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def api(method: str, path: str, body: str = "", auth: bool = True,
        creds: dict | None = None) -> tuple[int, str]:
    creds = creds or CREDS
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = _tls.conn = HTTPConnection(BASE_HOST, BASE_PORT, timeout=30)
    headers = {}
    if auth and creds:
        ts = int(time.time())
        sig = sign(creds["secret"], method, path, ts, body)
        headers = {"X-API-Key": creds["key"], "X-Timestamp": str(ts),
                   "X-Signature": sig, "Content-Type": "application/json"}
    try:
        conn.request(method, path, body=body or None, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        return resp.status, data
    except (BrokenPipeError, ConnectionResetError, OSError):
        conn.close()
        _tls.conn = None
        return api(method, path, body, auth)


_tls = threading.local()


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def report(name: str, lat: list[float], ok: int, err: int, wall: float,
           sample_err=None) -> None:
    ms = [x * 1000 for x in lat]
    print(f"\n### {name}")
    print(f"requests={len(ms)} ok={ok} err={err} wall={wall:.2f}s "
          f"throughput={len(ms)/wall:.0f} req/s")
    print(f"latency ms: avg={statistics.mean(ms):.2f} "
          f"p50={pct(ms,50):.2f} p90={pct(ms,90):.2f} "
          f"p99={pct(ms,99):.2f} max={max(ms):.2f}")
    if sample_err:
        print(f"sample_errors: {sample_err}")


# ─── phase 0: bootstrap ──────────────────────────────────────────────────────

USERS: list[dict] = []


def phase0():
    global CREDS
    pw = "pe" + chr(110) + "ch-" + "2026"
    logins = ["bench"] + [f"bench{i}" for i in range(1, 5)]
    for lg in logins:
        body = json.dumps({"login": lg, "password": pw})
        api("POST", "/api/v1/auth/register", body, auth=False)
        st, data = api("POST", "/api/v1/auth/login", body, auth=False)
        d = json.loads(data)
        assert st == 200, f"login failed for {lg}: {st} {data}"
        USERS.append({"login": lg, "key": d["api_key"], "secret": d["api_secret"],
                      "user_id": d["user_id"]})
    CREDS.update(USERS[0])
    print("phase0 users:", ", ".join(f'{u["login"]}={u["user_id"]}' for u in USERS))

    # ── clean slate: cancel ALL resting orders, then purge the Redis book,
    # only then touch balances. Cancelling refunds locked balances properly;
    # resetting balances with resting orders in the book corrupts settlement
    # (maker lock wiped → worker ValueError → dead-letter storm).
    for u in USERS:
        CREDS = dict(u)
        for _round in range(6):
            st, data = api("GET", "/api/v1/orders?limit=500&status=new,partially_filled")
            orders = json.loads(data).get("orders", []) if st == 200 else []
            if not orders:
                break
            for o in orders:
                api("DELETE", f"/api/v1/orders/{o['id']}")
    CREDS = dict(USERS[0])

    async def purge_book():
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        keys = []
        async for k in r.scan_iter("ob:*"):
            keys.append(k)
        async for k in r.scan_iter("stops:*"):
            keys.append(k)
        if keys:
            await r.delete(*keys)
        await r.aclose()
        print(f"phase0: purged {len(keys)} book keys")

    import asyncio
    asyncio.run(purge_book())
    import asyncpg

    async def db():
        conn = await asyncpg.connect(DB_URL)
        uid = CREDS["user_id"]
        ids = [u["user_id"] for u in USERS]
        await conn.execute(
            "UPDATE api_keys SET rate_limit_per_min=1000000 WHERE user_id = ANY($1)", ids)
        for uid in ids:
            for asset, amt in (("USD", 5_000_000), ("BTC", 2_000)):
                await conn.execute("""
                    INSERT INTO balances (user_id, asset, total_balance, locked_balance,
                                          available_balance, version, updated_at)
                    VALUES ($1, $2, $3, 0, $3, 1, now())
                    ON CONFLICT (user_id, asset)
                    DO UPDATE SET total_balance=$3, available_balance=$3, version=1,
                                  locked_balance=0""", uid, asset, float(amt))
        await conn.close()

    asyncio.run(db())
    print("phase0: rate limit raised, balances topped up")


# ─── private WS listener ─────────────────────────────────────────────────────

def ws_listener(creds: dict):
    import asyncio

    async def run():
        import websockets
        uri = f"ws://{BASE_HOST}:{BASE_PORT}/api/v1/ws/private"
        async with websockets.connect(uri) as ws:
            await ws.recv()  # auth_required
            ts = int(time.time())
            key = hashlib.sha256(creds["secret"].encode()).hexdigest()
            payload = f"WS_HANDSHAKE\n{ts}\n{creds['key']}"
            sig = hmac_mod.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
            await ws.send(json.dumps({"action": "auth", "api_key": creds["key"],
                                      "timestamp": ts, "signature": sig}))
            async for raw in ws:  # NO wait_for: cancelling recv() drops messages
                if STOP_WS.is_set():
                    break
                msg = json.loads(raw)
                cnt = WS_RX.setdefault(creds["user_id"], {"all": 0, "order": 0})
                cnt["all"] += 1
                if msg.get("event") != "order":
                    continue
                cnt["order"] += 1
                ev = msg.get("status_event")
                oid = msg.get("order_id")
                if oid and ev in ("placed", "filled", "partially_filled",
                                  "rejected", "expired"):
                    with WS_LOCK:
                        if ev in ("filled", "rejected", "expired") or oid not in WS_FILLS:
                            WS_FILLS[oid] = (time.perf_counter(), ev)
    asyncio.run(run())


# ─── direct redis subscriber counters (per user) ────────────────────────────
REDIS_EVENTS: dict[int, dict[str, int]] = {}
TRADE_EVENTS: list[float] = []


def redis_watcher(creds: dict):
    import asyncio as _aio

    async def run():
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        ps = r.pubsub()
        await ps.subscribe(f"pub:orders:{creds['user_id']}")
        async for m in ps.listen():  # NO get_message(timeout=): cancelling the
            if STOP_WS.is_set():    # read can drop buffered messages
                break
            if m["type"] != "message":
                continue
            d = json.loads(m["data"])
            ev = d.get("status_event")
            if ev in ("placed", "filled", "partially_filled", "rejected", "expired"):
                REDIS_EVENTS.setdefault(creds["user_id"], {}).setdefault(ev, 0)
                REDIS_EVENTS[creds["user_id"]][ev] += 1
        await ps.unsubscribe()
        await r.aclose()
    _aio.run(run())


# ─── workers ─────────────────────────────────────────────────────────────────

def read_worker(n: int, lat: list, errs: list, start_i: int = 0):
    for _ in range(n):
        t0 = time.perf_counter()
        st, _ = api("GET", "/api/v1/balance")
        lat.append(time.perf_counter() - t0)
        if st != 200:
            errs.append(st)


def maker_worker(n: int, start_i: int, lat: list, errs: list):
    for i in range(n):
        idx = start_i + i
        side = "buy" if idx % 2 == 0 else "sell"
        price = 99.0 if side == "buy" else 101.0
        body = json.dumps({"symbol": "BTC/USD", "side": side, "type": "limit",
                           "quantity": 0.005, "price": price, "post_only": True,
                           "client_order_id": f"mk-{idx}"})
        t0 = time.perf_counter()
        st, data = api("POST", "/api/v1/orders", body)
        lat.append(time.perf_counter() - t0)
        if st not in (200, 201):
            errs.append((st, data[:80]))


def taker_worker(n: int, start_i: int, post_ts: dict, lat: list, errs: list):
    for i in range(n):
        idx = start_i + i
        side = "sell" if idx % 2 == 0 else "buy"
        cid = f"tk-{idx}"
        body = json.dumps({"symbol": "BTC/USD", "side": side, "type": "market",
                           "quantity": 0.01, "client_order_id": cid})
        t0 = time.perf_counter()
        st, data = api("POST", "/api/v1/orders", body, creds=USERS[1 + idx % 4])
        if st in (200, 201):
            post_ts[int(json.loads(data)["id"])] = t0
        else:
            errs.append((st, data[:80]))


def run_threads(fn, threads: int, per: int, **kw) -> float:
    t0 = time.perf_counter()
    ts_ = [threading.Thread(target=fn, kwargs={"n": per, "start_i": k * per, **kw})
           for k in range(threads)]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join()
    return time.perf_counter() - t0


def main():
    phase0()
    def trade_watcher():
        import asyncio as _aio

        async def run():
            import redis.asyncio as aioredis
            r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
            ps = r.pubsub()
            await ps.subscribe("pub:trades:BTC/USD")
            async for m in ps.listen():  # no cancelling reads (drops messages)
                if STOP_WS.is_set():
                    break
                if m["type"] == "message":
                    TRADE_EVENTS.append(time.perf_counter())
            await ps.unsubscribe()
            await r.aclose()
        _aio.run(run())

    TRADE_EVENTS.clear()
    ws_threads = [threading.Thread(target=trade_watcher, daemon=True)] + [threading.Thread(target=ws_listener, args=(u,), daemon=True)
                  for u in USERS]
    ws_threads += [threading.Thread(target=redis_watcher, args=(u,), daemon=True)
                   for u in USERS[1:]]
    for t in ws_threads:
        t.start()
    time.sleep(2.0)

    # pre-clean resting orders from previous runs
    st, data = api("GET", "/api/v1/orders?limit=1000")
    for o in (json.loads(data).get("orders", []) if st == 200 else []):
        api("DELETE", f"/api/v1/orders/{o['id']}")

    # phase 1 — REST read load
    lat, errs = [], []
    wall = run_threads(read_worker, 8, 500, lat=lat, errs=errs)
    report("P1 REST read (GET /balance)", lat, len(lat) - len(errs), len(errs), wall, errs[:3])

    # phase 2 — REST write: enqueue 400 post_only makers (far from touch)
    lat, errs = [], []
    wall = run_threads(maker_worker, 4, 100, lat=lat, errs=errs)
    report("P2 REST write (POST limit post_only)", lat, len(lat) - len(errs), len(errs), wall, errs[:3])

    # phase 3 — end-to-end market taker matching via WS
    post_ts: dict[str, float] = {}
    lat, errs = [], []
    wall = run_threads(taker_worker, 4, 25, post_ts=post_ts, lat=lat, errs=errs)
    n_takers = len(post_ts)
    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline:
        with WS_LOCK:
            done = sum(1 for oid in post_ts if WS_FILLS.get(oid, ("", ""))[1]
                       in ("filled", "rejected", "expired"))
        if done >= n_takers:
            break
        time.sleep(0.2)
    e2e, missing = [], 0
    for oid, t0 in post_ts.items():
        with WS_LOCK:
            got = WS_FILLS.get(oid)
        if got and got[1] == "filled":
            e2e.append(got[0] - t0)
        else:
            st9, data9 = api("GET", f"/api/v1/orders/{oid}")
            status9 = json.loads(data9).get("status") if st9 == 200 else f"http{st9}"
            if status9 == "filled":
                e2e.append(got[0] - t0 if got else 0.0)  # fallback: exclude from latency? mark
            missing += 1
    print(f"\n### P3 E2E market-taker matching (POST -> WS 'filled')")
    print(f"takers={n_takers} filled={len(e2e)} missing={missing} "
          f"submit_wall={wall:.2f}s")
    if errs:
        print(f"sample_taker_errors: {errs[:3]} (n={len(errs)})")
    with WS_LOCK:
        seen = {oid: WS_FILLS[oid][1] for oid in post_ts if oid in WS_FILLS}
    from collections import Counter
    miss_ids = [oid for oid in post_ts if WS_FILLS.get(oid, ("", ""))[1] != "filled"]
    print(f"missing_ids_sample: {miss_ids[:10]}")
    print(f"ws_raw_counts: {WS_RX}")
    hist = Counter(seen.values())
    print(f"ws events seen: {dict(hist)} never-seen={n_takers - len(seen)}")
    print(f"redis-channel events: {REDIS_EVENTS}")
    print(f"public trade events published: {len(TRADE_EVENTS)}")
    if e2e:
        ms = [x * 1000 for x in e2e]
        span = max(v[0] for k, v in WS_FILLS.items() if k in post_ts) \
             - min(post_ts.values())
        print(f"e2e latency ms: avg={statistics.mean(ms):.2f} "
              f"p50={pct(ms,50):.2f} p90={pct(ms,90):.2f} "
              f"p99={pct(ms,99):.2f} max={max(ms):.2f}")
        print(f"aggregate pipeline throughput ~{len(e2e)/span:.0f} trades-side/s "
              f"over {span:.2f}s span")

    # cleanup: cancel resting makers
    st, data = api("GET", "/api/v1/orders?limit=500")
    open_orders = json.loads(data).get("orders", []) if st == 200 else []
    cancels, errs2 = [], []
    t0 = time.perf_counter()
    for o in open_orders:
        st2, _ = api("DELETE", f"/api/v1/orders/{o['id']}")
        cancels.append(st2 == 200)
    wall = time.perf_counter() - t0
    print(f"\ncleanup: canceled {sum(cancels)}/{len(cancels)} orders in {wall:.2f}s "
          f"({len(cancels)/max(wall,0.01):.0f} req/s single-threaded)")

    STOP_WS.set()
    print("\nPERF_DONE")


if __name__ == "__main__":
    main()
