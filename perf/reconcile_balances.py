"""Post-load-test balance reconciliation.

For every trade: buyer pays quote_quantity and receives quantity; seller
receives quote_quantity and delivers quantity. Fees are recorded as 0
(known TODO in trade_service). So for each user/asset:

    expected_end = baseline + sum(trade deltas since baseline)

Baseline for the 5 bench users is the deterministic phase0 reset
(USD 5_000_000 / BTC 2_000, locked 0). For any other user (e.g. the web
UI admin) the baseline is the pre-run snapshot passed in.

Also checks: available == total - locked on every row, and asset
conservation (sum of deltas across all users == 0 — no fees, no
deposits in the window).
"""
import asyncio
import json
import os
import sys
from decimal import Decimal

import asyncpg

BENCH_RESET = {"USD": Decimal("5000000"), "BTC": Decimal("2000")}


async def main():
    base_trade_id = int(sys.argv[1])
    other_baseline = json.loads(sys.argv[2])  # {"user_id:asset": "amount"}
    dsn = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgres://")
    db = await asyncpg.connect(dsn)

    bench_ids = {r["id"] for r in await db.fetch(
        "select id from users where email like 'bench%'")}

    trades = await db.fetch(
        "select id,symbol,price,quantity,quote_quantity,side,"
        "taker_user_id,maker_user_id,taker_fee,maker_fee "
        f"from trades where id > {base_trade_id} order by id")
    print(f"trades in window: {len(trades)} "
          f"volume_quote={sum(t['quote_quantity'] for t in trades):,.2f} "
          f"volume_base={sum(t['quantity'] for t in trades):,.4f}")
    nonzero_fee = [t for t in trades if t["taker_fee"] or t["maker_fee"]]
    print(f"trades with nonzero fees: {len(nonzero_fee)}")

    deltas: dict[tuple[int, str], Decimal] = {}
    bad_fees = 0

    def add(uid, asset, amt):
        k = (uid, asset)
        deltas[k] = deltas.get(k, Decimal(0)) + amt

    base, quote = "BTC", "USD"
    for t in trades:
        q, qq = t["quantity"], t["quote_quantity"]
        if t["taker_fee"] or t["maker_fee"]:
            bad_fees += 1
        taker_buy = t["side"] == "buy"
        # taker
        if taker_buy:
            add(t["taker_user_id"], base, q)
            add(t["taker_user_id"], quote, -qq)
        else:
            add(t["taker_user_id"], base, -q)
            add(t["taker_user_id"], quote, qq)
        # maker is always the other side
        if taker_buy:
            add(t["maker_user_id"], base, -q)
            add(t["maker_user_id"], quote, qq)
        else:
            add(t["maker_user_id"], base, q)
            add(t["maker_user_id"], quote, -qq)

    rows = await db.fetch("select user_id,asset,total_balance,locked_balance,available_balance from balances order by user_id,asset")
    mismatches = 0
    asset_sum: dict[str, Decimal] = {}
    for r in rows:
        uid, asset = r["user_id"], r["asset"]
        actual = Decimal(str(r["available_balance"]))
        locked = Decimal(str(r["locked_balance"]))
        total = Decimal(str(r["total_balance"]))
        if abs(total - locked - actual) > Decimal("1e-12"):
            print(f"!! INVARIANT user={uid} {asset}: total-locked={total - locked} != available={actual}")
            mismatches += 1
        if uid in bench_ids:
            baseline = BENCH_RESET[asset] if asset in BENCH_RESET else Decimal(0)
        else:
            baseline = Decimal(other_baseline.get(f"{uid}:{asset}", "0"))
        d = deltas.get((uid, asset), Decimal(0))
        expected = baseline + d
        asset_sum[asset] = asset_sum.get(asset, Decimal(0)) + d
        if abs(expected - actual) > Decimal("1e-12"):
            print(f"!! MISMATCH user={uid} {asset}: expected {expected} got {actual} "
                  f"diff={actual - expected}")
            mismatches += 1
        else:
            print(f"OK user={uid:<3} {asset:<4} baseline={baseline:>14,} "
                  f"delta={d:>+15,.6f} end={actual:>16,} locked={locked}")

    print("conservation (sum of deltas, fees=0):")
    for asset, s in sorted(asset_sum.items()):
        status = "OK" if abs(s) <= Decimal("1e-12") else "!! LEAK"
        print(f"  {status} {asset}: {s:+.18f}")
        if s != 0:
            mismatches += 1

    # trades vs orders filled consistency
    print(f"RESULT: mismatches={mismatches} trades={len(trades)} bad_fees={bad_fees}")
    await db.close()


asyncio.run(main())
