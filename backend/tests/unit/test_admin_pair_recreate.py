"""Regression tests: re-creating a soft-deleted trading pair must work.

Bug being covered
-----------------
The admin UI "removes" pairs via PATCH /pairs/{id}/active?is_active=false
(soft delete), while GET /pairs listed only active rows — so a deactivated
pair looked "deleted". Re-creating it then hit:

    ValueError("Trading pair 'X/Y' already exists") -> unhandled -> HTTP 500

Fix contract:
1. create on an INACTIVE symbol → reactivates that same row (same id),
   overwriting parameters with the request body;
2. create on an ACTIVE symbol → DuplicateTradingPairHTTPError (409);
3. list_trading_pairs(include_inactive=True) exposes deactivated rows.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import app.models  # noqa: E402,F401  — registers every ORM mapper
from app.core.exceptions import DuplicateTradingPairHTTPError  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models.trading_pair import TradingPair  # noqa: E402
from app.services.admin_service import AdminService  # noqa: E402


BASE_KWARGS = dict(
    base_asset="RUR",
    quote_asset="ORION",
    price_precision=4,
    quantity_precision=2,
    min_lot_size=Decimal("0.01"),
    max_lot_size=Decimal("1000"),
    tick_size=Decimal("0.0001"),
    maker_fee_bps=Decimal("10"),
    taker_fee_bps=Decimal("20"),
)


@pytest_asyncio.fixture
async def svc():
    """AdminService over a fresh in-memory SQLite DB (trading_pairs only)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=[TradingPair.__table__])
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield AdminService(session)
    await engine.dispose()


class TestTradingPairRecreate:
    async def test_create_inserts_active_pair(self, svc):
        pair = await svc.create_trading_pair(symbol="RUR/ORION", **BASE_KWARGS)
        assert pair.id is not None
        assert pair.is_active is True
        assert str(pair.tick_size) == "0.0001"

    async def test_recreate_after_deactivate_reactivates_same_row(self, svc):
        original = await svc.create_trading_pair(symbol="RUR/ORION", **BASE_KWARGS)

        # "Delete" in the admin UI == soft delete
        await svc.toggle_pair_active(original.id, False)
        assert (await svc.pair_repo.get_by_symbol("RUR/ORION")).is_active is False

        # Re-create with DIFFERENT parameters — must reactivate, not duplicate,
        # and apply the new settings.
        new_kwargs = dict(BASE_KWARGS, tick_size=Decimal("0.01"), quantity_precision=6)
        restored = await svc.create_trading_pair(symbol="RUR/ORION", **new_kwargs)

        assert restored.id == original.id
        assert restored.is_active is True
        assert str(restored.tick_size) == "0.01"
        assert restored.quantity_precision == 6

    async def test_duplicate_of_active_pair_raises_409(self, svc):
        await svc.create_trading_pair(symbol="RUR/ORION", **BASE_KWARGS)
        with pytest.raises(DuplicateTradingPairHTTPError) as exc_info:
            await svc.create_trading_pair(symbol="RUR/ORION", **BASE_KWARGS)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "duplicate_trading_pair"

    async def test_list_filters_and_include_inactive(self, svc):
        pair = await svc.create_trading_pair(symbol="RUR/ORION", **BASE_KWARGS)
        await svc.toggle_pair_active(pair.id, False)

        assert await svc.list_trading_pairs() == []
        inactive = await svc.list_trading_pairs(include_inactive=True)
        assert [p.symbol for p in inactive] == ["RUR/ORION"]
