"""Unit tests for HMAC signature computation and BalanceTracker.

These verify that our client-side signature matches the format the
server expects (see `backend/app/core/security.py`). If the server
ever changes the payload format, these tests will break — which is
exactly what we want.

Run with::

    cd /path/to/uvipystock
    python -m pytest bots/test_auth_and_balances.py -v
"""
from __future__ import annotations

import hashlib
import hmac
import sys
from decimal import Decimal
from pathlib import Path

# Make the repo root importable when running pytest from there
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.orderbook_viewer.hmac_rest_client import compute_signature, compute_ws_signature  # noqa: E402
from bots.orderbook_viewer.balances import Balance, BalanceTracker  # noqa: E402


# ─── HMAC REST signature ────────────────────────────────────────────────────

def _reference_signing_key(raw_secret: str) -> str:
    """Reference implementation of the SHA-256 derivation step.

    Mirrors `app.core.security.hash_api_secret` on the server and
    `sha256Hex()` in `frontend/src/lib/hmac.ts`.
    """
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


def test_rest_signature_matches_server_format():
    """The signature must match what the server produces.

    Server flow (see `app.api.deps.authenticate_request`):
      signing_key = api_key_row.secret_hash = sha256(raw_secret)
      signature   = hmac_sha256(signing_key, "{METHOD}\\n{PATH}\\n{TS}\\n{BODY}")

    Our `compute_signature` takes the raw_secret and derives the signing
    key internally — so callers just pass the raw secret.
    """
    raw_secret = "my-test-secret"
    method = "POST"
    path = "/api/v1/orders"
    timestamp = 1716543200
    body = '{"symbol":"BTC/USDT","side":"buy","type":"limit","price":"100.00","quantity":"0.5"}'

    # Reference: derive signing key, then HMAC
    signing_key = _reference_signing_key(raw_secret)
    payload = f"{method}\n{path}\n{timestamp}\n{body}"
    expected = hmac.new(
        signing_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    actual = compute_signature(
        secret=raw_secret, method=method, path=path,
        timestamp=timestamp, body=body,
    )
    assert actual == expected
    assert len(actual) == 64  # SHA-256 hex digest


def test_rest_signature_uses_sha256_of_secret_not_raw_secret():
    """Critical: the signing key is SHA-256(raw_secret), NOT raw_secret itself.

    If we ever regress to using raw_secret directly, the server will
    reject every request with 401 Unauthorized (because it uses the
    stored SHA-256 hash as the key, not the raw secret).
    """
    raw_secret = "abc123"
    args = dict(secret=raw_secret, method="GET", path="/api/v1/balance",
                timestamp=100, body="")

    correct_sig = compute_signature(**args)
    # Wrong sig: using raw_secret directly as HMAC key
    wrong_payload = f"GET\n/api/v1/balance\n100\n"
    wrong_sig = hmac.new(
        raw_secret.encode("utf-8"),
        wrong_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    assert correct_sig != wrong_sig, (
        "Signature using SHA-256(secret) must differ from signature using "
        "raw secret directly — otherwise the server will reject with 401."
    )


def test_rest_signature_with_query_string():
    """Signature must use path INCLUDING the query string."""
    raw_secret = "s3cr3t"
    method = "GET"
    path = "/api/v1/balance?asset=BTC"
    timestamp = 1716543200
    body = ""

    actual = compute_signature(
        secret=raw_secret, method=method, path=path,
        timestamp=timestamp, body=body,
    )

    # Manually recompute
    signing_key = _reference_signing_key(raw_secret)
    payload = f"GET\n/api/v1/balance?asset=BTC\n{timestamp}\n"
    expected = hmac.new(
        signing_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert actual == expected


def test_rest_signature_method_case_insensitive_input():
    """Method is uppercased internally — 'get' and 'GET' produce the same sig."""
    raw_secret = "x"
    args = dict(secret=raw_secret, path="/api/v1/balance", timestamp=1, body="")
    sig_upper = compute_signature(method="GET", **args)
    sig_lower = compute_signature(method="get", **args)
    assert sig_upper == sig_lower


def test_rest_signature_different_bodies_differ():
    """Different bodies must produce different signatures (sanity check)."""
    raw_secret = "x"
    sig_a = compute_signature(secret=raw_secret, method="POST", path="/o",
                              timestamp=1, body='{"a":1}')
    sig_b = compute_signature(secret=raw_secret, method="POST", path="/o",
                              timestamp=1, body='{"a":2}')
    assert sig_a != sig_b


# ─── HMAC WS signature ──────────────────────────────────────────────────────

def test_ws_signature_matches_server_format():
    """WS handshake signature must match what the server produces."""
    raw_secret = "ws-secret"
    api_key = "pubkey123"
    timestamp = 1716543200

    actual = compute_ws_signature(
        secret=raw_secret, api_key=api_key, timestamp=timestamp,
    )

    # Reference: server uses sha256(raw_secret) as the HMAC key
    signing_key = _reference_signing_key(raw_secret)
    payload = f"WS_HANDSHAKE\n{timestamp}\n{api_key}"
    expected = hmac.new(
        signing_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert actual == expected


def test_ws_signature_uses_sha256_of_secret():
    """WS handshake must also derive the signing key via SHA-256.

    Same regression guard as the REST test — if we ever use raw_secret
    directly, the private WS handshake will fail with auth_failed.
    """
    raw_secret = "ws-abc"
    api_key = "k1"
    timestamp = 100

    correct = compute_ws_signature(secret=raw_secret, api_key=api_key,
                                   timestamp=timestamp)
    # Wrong: raw_secret directly
    wrong_payload = f"WS_HANDSHAKE\n{timestamp}\n{api_key}"
    wrong = hmac.new(
        raw_secret.encode("utf-8"),
        wrong_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert correct != wrong


# ─── BalanceTracker ─────────────────────────────────────────────────────────

def test_balance_tracker_rest_snapshot_replaces_state():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([
        {"asset": "BTC", "total": "1.5", "locked": "0.5", "available": "1.0"},
        {"asset": "USDT", "total": "1000", "locked": "0", "available": "1000"},
    ])
    assert bt.snapshot_count == 1
    assert bt.update_count == 0
    b_btc = bt.get("BTC")
    assert b_btc is not None
    assert b_btc.total == Decimal("1.5")
    assert b_btc.locked == Decimal("0.5")
    assert b_btc.available == Decimal("1.0")
    b_usdt = bt.get("USDT")
    assert b_usdt is not None
    assert b_usdt.total == Decimal("1000")
    assert not b_usdt.is_zero


def test_balance_tracker_ws_update_overwrites():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([
        {"asset": "BTC", "total": "1.0", "locked": "0", "available": "1.0"},
    ])
    # Simulate an order placed → 0.5 BTC now locked
    bt.apply_ws_update(
        asset="BTC", total="1.0", locked="0.5", available="0.5",
    )
    assert bt.update_count == 1
    b = bt.get("BTC")
    assert b.locked == Decimal("0.5")
    assert b.available == Decimal("0.5")
    assert b.total == Decimal("1.0")  # total unchanged


def test_balance_tracker_ws_update_adds_new_asset():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([])  # start with nothing
    # User gets credited ETH (admin adjustment, deposit, etc.)
    bt.apply_ws_update(
        asset="ETH", total="10.0", locked="0", available="10.0",
    )
    assert bt.get("ETH") is not None
    assert bt.get("ETH").total == Decimal("10.0")


def test_balance_tracker_rest_snapshot_removes_dropped_assets():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([
        {"asset": "BTC", "total": "1", "locked": "0", "available": "1"},
        {"asset": "ETH", "total": "5", "locked": "0", "available": "5"},
    ])
    # Second REST snapshot omits ETH — should be removed from tracker
    bt.apply_rest_snapshot([
        {"asset": "BTC", "total": "1", "locked": "0", "available": "1"},
    ])
    assert bt.get("BTC") is not None
    assert bt.get("ETH") is None


def test_balance_tracker_non_zero_filter():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([
        {"asset": "BTC", "total": "1", "locked": "0", "available": "1"},
        {"asset": "ETH", "total": "0", "locked": "0", "available": "0"},
        {"asset": "USDT", "total": "500", "locked": "100", "available": "400"},
    ])
    non_zero = bt.non_zero_balances()
    assets = {b.asset for b in non_zero}
    assert assets == {"BTC", "USDT"}
    assert "ETH" not in assets  # zero-balance asset filtered out


def test_balance_tracker_all_balances_sorted():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([
        {"asset": "USDT", "total": "1", "locked": "0", "available": "1"},
        {"asset": "BTC", "total": "1", "locked": "0", "available": "1"},
        {"asset": "ETH", "total": "1", "locked": "0", "available": "1"},
    ])
    all_b = bt.all_balances()
    assert [b.asset for b in all_b] == ["BTC", "ETH", "USDT"]


def test_balance_is_zero_property():
    zero = Balance(asset="X", total=Decimal("0"), locked=Decimal("0"),
                   available=Decimal("0"))
    non_zero = Balance(asset="X", total=Decimal("1"), locked=Decimal("0"),
                       available=Decimal("1"))
    assert zero.is_zero
    assert not non_zero.is_zero


def test_balance_tracker_snapshot_is_a_copy():
    bt = BalanceTracker()
    bt.apply_rest_snapshot([
        {"asset": "BTC", "total": "1", "locked": "0", "available": "1"},
    ])
    snap = bt.snapshot()
    # Mutate the copy
    snap["BTC"] = Balance(asset="BTC", total=Decimal("999"),
                          locked=Decimal("0"), available=Decimal("999"))
    # Original tracker is unaffected
    assert bt.get("BTC").total == Decimal("1")


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
    sys.exit(0 if passed == len(tests) else 1)
