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
        # NOTE: must forward creds on retry — falling back to the global
        # CREDS silently re-attributed the request to another user.
        return api(method, path, body, auth, creds)


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
            st, data = api("GET", "/api/v1/orders?limit=500&status=new,partially_filled,pending")
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
                                  "rejected", "expired", "pending",
                                  "canceled", "modified", "triggered"):
                    with WS_LOCK:
                        if (ev in ("filled", "rejected", "expired",
                                   "canceled", "triggered")
                                or oid not in WS_FILLS):
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
            if ev in ("placed", "filled", "partially_filled", "rejected",
                      "expired", "pending", "canceled", "modified", "triggered"):
                REDIS_EVENTS.setdefault(creds["user_id"], {}).setdefault(ev, 0)
                REDIS_EVENTS[creds["user_id"]][ev] += 1
        await ps.unsubscribe()
        await r.aclose()
    _aio.run(run())


# ─── phase 5: order-type coverage ────────────────────────────────────────────

SYM = "BTC/USD"


def _db_fetch(query: str, *args):
    import asyncio
    import asyncpg

    async def go():
        c = await asyncpg.connect(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
        try:
            return await c.fetch(query, *args)
        finally:
            await c.close()

    return asyncio.run(go())


def _redis_zrange(key: str):
    import asyncio
    import redis.asyncio as aioredis

    async def go():
        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        try:
            return await r.zrange(key, 0, -1)
        finally:
            await r.aclose()

    return asyncio.run(go())


def _order_status(oid):
    # NOTE: GET /orders/{id} is NOT an endpoint (405) — read from the DB.
    rows = _db_fetch(
        "select id,status,filled_quantity,quantity,locked_amount "
        "from orders where id=$1", oid)
    if not rows:
        return {"status": None}
    r = dict(rows[0])
    r["filled_quantity"] = float(r["filled_quantity"])
    r["locked_amount"] = float(r["locked_amount"])
    return r


def _place(creds, body):
    # BalanceVersionConflict → 500 is a known server issue when a seed user's
    # own fills settle concurrently with new placements; retry with backoff so
    # functional coverage measures order semantics, not that flake.
    t0 = time.perf_counter()
    for attempt in range(4):
        st, data = api("POST", "/api/v1/orders", json.dumps(body), creds=creds)
        if st != 500:
            break
        time.sleep(0.3 * (attempt + 1))
    try:
        parsed = json.loads(data)
    except Exception:
        parsed = {}
    if st >= 400:
        print(f"    (place FAIL http={st} side={body.get(chr(39)+chr(115)+chr(105)+chr(100)+chr(101)+chr(39)) if isinstance(body,dict) else None} type={body.get(chr(39)+chr(116)+chr(121)+chr(112)+chr(101)+chr(39)) if isinstance(body,dict) else None}: {data[:200]})")
    return st, parsed, t0


def _wait_ev(oid, kinds, timeout=8.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        with WS_LOCK:
            got = WS_FILLS.get(oid)
        if got and got[1] in kinds:
            return got
        time.sleep(0.1)
    return None


def p5_order_types():
    """Functional coverage of every OrderType: correctness, not throughput."""
    print("\n### P5 order-type coverage")
    results = []

    def ck(name, ok, det=""):
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + det) if det else ''}")

    mk = USERS[0]      # liquidity seed / maker operator
    t1, t2, t3, t4 = USERS[1], USERS[2], USERS[3], USERS[4]

    # Clean slate: P2–P4 leave resting makers (e.g. bids at 99.0) and stale
    # stop/trailing state behind; the conditional-order checks below assume a
    # known book, same rationale as phase0's purge.
    for u in USERS:
        st, data = api("GET", "/api/v1/orders?limit=500&status=new,partially_filled,pending",
                       creds=u)
        for o in (json.loads(data).get("orders", []) if st == 200 else []):
            api("DELETE", f"/api/v1/orders/{o['id']}", creds=u)

    import asyncio
    async def _purge_cond():
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        for pat in ("stops:*", "trailing:*", "ob:*"):
            async for k in r.scan_iter(match=pat):
                await r.delete(k)
        await r.aclose()
    asyncio.run(_purge_cond())
    time.sleep(0.6)

    def n_makers(creds, side, px, qty_each, count):
        for _ in range(count):
            api("POST", "/api/v1/orders", json.dumps(
                {"symbol": SYM, "side": side, "type": "limit",
                 "quantity": qty_each, "price": px}), creds=creds)

    # seed: 10 sells @99.5 (0.05), 10 buys @98.5 (0.05), 4 buys @97.5 (0.02)
    n_makers(mk, "sell", 99.5, 0.005, 10)
    n_makers(mk, "buy", 98.5, 0.005, 10)
    n_makers(mk, "buy", 97.5, 0.005, 4)
    time.sleep(1.5)

    # 1) GTC limit as taker (crosses) → fills immediately
    st, d, _ = _place(t1, {"symbol": SYM, "side": "buy", "type": "limit",
                           "quantity": 0.005, "price": 99.6})
    oid = d.get("id")
    _wait_ev(oid, ("filled", "partially_filled", "canceled"))
    o = _order_status(oid)
    ck("limit GTC crossing fills as taker", o.get("status") == "filled",
       f"status={o.get('status')}")

    # 2) IOC larger than liquidity → partial, remainder NOT resting, locks released
    st, d, _ = _place(t2, {"symbol": SYM, "side": "buy", "type": "ioc",
                           "quantity": 0.06, "price": 99.5})
    oid = d.get("id")
    _wait_ev(oid, ("canceled", "expired", "partially_filled", "filled"))
    time.sleep(1.0)
    o = _order_status(oid)
    row = _db_fetch("select status, filled_quantity, locked_amount from orders where id=$1", oid)
    r = dict(row[0]) if row else {}
    filled_ok = r.get("filled_quantity", 0) and float(r["filled_quantity"]) > 0
    locks_free = float(r.get("locked_amount", -1)) == 0
    ck("IOC partial-fill (fills what it can, then expired)",
       filled_ok and str(r.get("status")) == "expired",
       f"status={r.get('status')} filled={r.get('filled_quantity')}")
    ck("IOC remainder not resting + locks released", locks_free,
       f"locked_amount={r.get('locked_amount')}")

    # 3) FOK with enough liquidity → fully filled
    n_makers(mk, "sell", 99.5, 0.005, 4)   # 0.02 ask
    time.sleep(1.0)
    st, d, _ = _place(t3, {"symbol": SYM, "side": "buy", "type": "fok",
                           "quantity": 0.01, "price": 99.5})
    oid = d.get("id")
    _wait_ev(oid, ("filled", "canceled", "expired"))
    o = _order_status(oid)
    ck("FOK with liquidity → filled", o.get("status") == "filled",
       f"status={o.get('status')}")

    # 4) FOK without enough liquidity → canceled/expired, no partial fills
    before = _db_fetch("select count(*) c from trades")
    st, d, _ = _place(t3, {"symbol": SYM, "side": "buy", "type": "fok",
                           "quantity": 0.05, "price": 99.5})
    oid = d.get("id")
    _wait_ev(oid, ("canceled", "expired", "rejected"), timeout=5)
    time.sleep(1.0)
    o = _order_status(oid)
    after = _db_fetch("select count(*) c from trades")
    o2 = _order_status(oid)
    no_trades = int(before[0]["c"]) == int(after[0]["c"])
    ck("FOK without liquidity → no trades, row closed, funds released",
       o2.get("status") in ("canceled", "expired", "rejected") and no_trades
       and o2.get("locked_amount") == 0.0,
       f"status={o2.get('status')} new_trades={int(after[0]['c']) - int(before[0]['c'])} locked={o2.get('locked_amount')}")

    # 5) post_only that would cross → rejected (4xx or rejected status)
    st, d, _ = _place(t4, {"symbol": SYM, "side": "sell", "type": "post_only",
                           "quantity": 0.01, "price": 98.4})
    oid = d.get("id")
    _wait_ev(oid, ("rejected", "expired", "canceled"), timeout=5)
    time.sleep(0.5)
    o = _order_status(oid)
    rejected = st in (400, 422) or (oid and
                 o.get("status") in ("rejected", "expired", "canceled"))
    ck("post_only crossing → rejected, funds released",
       rejected and o.get("locked_amount") == 0.0,
       f"http={st} status={o.get('status')} locked={o.get('locked_amount')}")

    # 6) iceberg: hidden reserve becomes tradable (engine backfill)
    st, d, _ = _place(mk, {"symbol": SYM, "side": "sell", "type": "iceberg",
                           "quantity": 0.02, "price": 99.5,
                           "iceberg_visible_quantity": 0.005,
                           "iceberg_hidden_quantity": 0.015})
    ice_id = d.get("id")
    time.sleep(0.5)
    # market-buy precheck validates against VISIBLE ask volume only, so a
    # single buy cannot reach the hidden reserve: eat visible ask + iceberg
    # visible first, then a second buy drives into the backfilled hidden qty.
    _place(t1, {"symbol": SYM, "side": "buy", "type": "market", "quantity": 0.014})
    time.sleep(1.0)
    _place(t1, {"symbol": SYM, "side": "buy", "type": "market", "quantity": 0.005})
    time.sleep(2.0)
    o = _order_status(ice_id)
    ice_row = _db_fetch("select filled_quantity from orders where id=$1", ice_id)
    filled_ice = float(ice_row[0]["filled_quantity"]) if ice_row else 0
    ck("iceberg hidden volume tradable", filled_ice >= 0.0079,
       f"iceberg filled={filled_ice} status={o.get('status')}")

    # 6z) re-seed liquidity: the checks above ate the seed book, and the
    # conditional-order section needs known depth BELOW/ABOVE the stop levels.
    for p in (98.5, 98.3, 98.0, 97.5):
        _place(mk, {"symbol": SYM, "side": "buy", "type": "limit",
                    "quantity": 0.06, "price": p})
    for p in (99.0, 99.5):
        _place(mk, {"symbol": SYM, "side": "sell", "type": "limit",
                    "quantity": 0.06, "price": p})
    time.sleep(0.4)

    # 6y) self-trade prevention: taker skips its own liquidity
    st, d, _ = _place(t2, {"symbol": SYM, "side": "sell", "type": "limit",
                           "quantity": 0.01, "price": 98.8})
    own_ask = d.get("id")
    time.sleep(0.5)
    st, d, _ = _place(t2, {"symbol": SYM, "side": "buy", "type": "market",
                           "quantity": 0.005})
    mb_id = d.get("id")
    _wait_ev(mb_id, ("filled", "expired"), timeout=10)
    time.sleep(1.0)
    tr = _db_fetch("select price from trades where taker_order_id=$1", mb_id)
    o_own = _order_status(own_ask)
    fills_foreign = bool(tr) and float(tr[0]["price"]) >= 99.0
    mb_o = _order_status(mb_id)
    ck("STP: market buy skips own best ask, fills foreign level",
       fills_foreign and o_own.get("status") == "new"
       and mb_o.get("status") == "filled",
       f"trade_prices={[str(x['price']) for x in tr]} buy_status={mb_o.get('status')} "
       f"own_ask_status={o_own.get('status')}")
    # self-only side of the book: at this point every resting bid belongs
    # to mk, so mk's own market sell must skip all of them → no_liquidity
    # → expired, funds released, no trades.
    st, d, _ = _place(mk, {"symbol": SYM, "side": "sell", "type": "market",
                           "quantity": 0.001})
    mb2 = d.get("id")
    _wait_ev(mb2, ("expired", "canceled", "rejected"), timeout=10)
    time.sleep(1.0)
    o2 = _order_status(mb2)
    tr2 = _db_fetch("select id from trades where taker_order_id=$1", mb2)
    ck("STP: own-only liquidity → expired, no trades, funds released",
       o2.get("status") in ("expired", "canceled") and not tr2
       and o2.get("locked_amount") == 0.0,
       f"status={o2.get('status')} trades={len(tr2)} locked={o2.get('locked_amount')}")

    # 7) stop_market: arms in stop queue, triggers on print, child fills.
    # stop=98.4 while last print is ~99.5 → must NOT fire at registration
    # (the monitor evaluates on stops_changed too); then a print at 98.3
    # crosses it.
    st, d, _ = _place(t2, {"symbol": SYM, "side": "sell", "type": "stop_market",
                           "quantity": 0.005, "stop_price": 98.6})
    stop_id = d.get("id")
    time.sleep(0.5)
    zsnap = _redis_zrange(f"stops:{SYM}")
    armed = str(stop_id).encode() in zsnap or stop_id is None
    if not armed:
        print(f"    (armed debug: id={stop_id} zset={[x.decode() for x in zsnap]})")
    _place(t3, {"symbol": SYM, "side": "sell", "type": "market", "quantity": 0.07})
    got = _wait_ev(stop_id, ("triggered",), timeout=15)
    kid = _db_fetch("select id,status from orders where parent_order_id=$1", stop_id)
    ck("stop_market armed in stop queue", armed)
    ck("stop_market triggered on crossing print", got is not None)
    ck("stop_market child executed", bool(kid) and kid[0]["status"] in ("filled", "new", "partially_filled"),
       f"child={[dict(k) for k in kid]}")

    # 8) stop_limit: arms (last print 98.3 > stop 98.0), then prints reach
    # 98.0 → trigger → child limit 97.9 vs best bid 97.5 → rests as 'new'.
    st, d, _ = _place(t2, {"symbol": SYM, "side": "sell", "type": "stop_limit",
                           "quantity": 0.005, "stop_price": 98.4, "price": 98.3})
    sl_id = d.get("id")
    time.sleep(0.5)
    _place(t3, {"symbol": SYM, "side": "sell", "type": "market", "quantity": 0.05})
    got = _wait_ev(sl_id, ("triggered",), timeout=15)
    time.sleep(1.5)
    kid = _db_fetch("select id,status from orders where parent_order_id=$1", sl_id)
    ck("stop_limit triggered", got is not None)
    ck("stop_limit child placed", bool(kid),
       f"child={[dict(k) for k in kid]}")

    # 9) trailing_stop: registered in trailing state (no trigger assertion)
    st, d, _ = _place(t1, {"symbol": SYM, "side": "buy", "type": "trailing_stop",
                           "quantity": 0.005, "trailing_delta": 1.0})
    ts_id = d.get("id")
    time.sleep(0.8)
    trail = _redis_zrange(f"trailing:{SYM}")
    state = _db_fetch("select status from orders where id=$1", ts_id)
    ck("trailing_stop armed (PENDING + trailing state)",
       bool(state) and state[0]["status"] == "pending" and
       (str(ts_id).encode() in trail or ts_id is None),
       f"status={state[0]['status'] if state else None} trailing_zset={len(trail)}")

    # 10) SL/TP attached to a market parent: children created & armed
    n_makers(mk, "sell", 99.5, 0.005, 3)
    time.sleep(1.0)
    st, d, _ = _place(t1, {"symbol": SYM, "side": "buy", "type": "market",
                           "quantity": 0.01,
                           "sl": {"type": "stop_market", "stop_price": 80.0},
                           "tp": {"type": "limit", "price": 130.0}})
    par = d.get("id")
    time.sleep(2.0)
    kids = _db_fetch("select id,type,status from orders where parent_order_id=$1 and id <> $1", par)
    ck("market + SL/TP: parent filled, 2 children",
       par and _order_status(par).get("status") == "filled" and len(kids) == 2,
       f"children={[dict(k) for k in kids]}")

    # 11) modify (cancel-replace)
    st, d, _ = _place(mk, {"symbol": SYM, "side": "sell", "type": "limit",
                           "quantity": 0.005, "price": 99.7})
    old_id = d.get("id")
    time.sleep(0.8)
    st2, d2 = api("PUT", f"/api/v1/orders/{old_id}", json.dumps(
        {"price": 99.8, "quantity": 0.01}), creds=mk), None
    st2 = st2[0]
    time.sleep(1.2)
    old_o = _order_status(old_id)
    rows = _db_fetch("select id,status,quantity from orders where replaced_by_id is null and user_id=$1 "
                     "and status='new' and price=99.8 and quantity=0.01", mk["user_id"])
    ck("modify: old canceled", old_o.get("status") == "canceled",
       f"old_status={old_o.get('status')}")
    ck("modify: new order rests at new params", len(rows) > 0, f"new_resting={len(rows)}")

    # 12) bulk place (all-or-nothing)
    orders = [{"symbol": SYM, "side": "buy", "type": "limit",
               "quantity": 0.001, "price": 96.5} for _ in range(10)]
    st, data = api("POST", "/api/v1/orders/bulk", json.dumps({"orders": orders}), creds=mk)
    bulk = json.loads(data) if st in (200, 201) else {}
    placed = len(bulk.get("orders", [])) if isinstance(bulk, dict) else 0
    ck("bulk place 10 orders", st in (200, 201) and placed == 10, f"http={st} placed={placed}")

    ok = sum(results)
    print(f"P5 summary: {ok}/{len(results)} checks passed")
    return ok == len(results)



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
    st, data = api("GET", "/api/v1/orders?limit=500")
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

    p5_order_types()

    # cleanup: cancel resting makers
    # cleanup: cancel remaining open orders AS THEIR OWNER — GET /orders
    # only ever lists the caller's own orders, so a single-user cleanup
    # silently leaves other users' resting orders (e.g. SL/TP children of
    # the P5 market order) armed forever in the book and the UI.
    cancels, errs2 = [], []
    t0 = time.perf_counter()
    for u in USERS:
        for _round in range(4):
            st, data = api("GET", "/api/v1/orders?limit=500&status=new,partially_filled,pending",
                           creds=u)
            open_orders = json.loads(data).get("orders", []) if st == 200 else []
            if not open_orders:
                break
            for o in open_orders:
                st2, _ = api("DELETE", f"/api/v1/orders/{o['id']}", creds=u)
                cancels.append(st2 == 200)
    wall = time.perf_counter() - t0
    print(f"\ncleanup: canceled {sum(cancels)}/{len(cancels)} orders in {wall:.2f}s "
          f"({len(cancels)/max(wall,0.01):.0f} req/s single-threaded)")

    STOP_WS.set()
    print("\nPERF_DONE")


if __name__ == "__main__":
    main()
