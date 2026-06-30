"""Balance model — the financial source-of-truth.

INVARIANT
---------
``total_balance == locked_balance + available_balance``

`available_balance` is **physically stored** (not a computed property) so that
O(1) reads are possible without arithmetic on every API call. Every mutation
MUST go through `app.services.balance_service.BalanceService` which:
  1. Loads the row with `SELECT ... FOR UPDATE` (or uses `version` for
     optimistic locking) inside a single DB transaction.
  2. Updates all three fields atomically.
  3. Bumps `version` on every UPDATE to detect lost updates from concurrent
     matching workers / API requests.

Precision
---------
`Numeric(36, 18)` gives 36 total digits with 18 after the decimal point —
enough for crypto-scale values (up to 10^18 base units of precision 1e-18).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class Balance(Base):
    """Per-user, per-asset balance with explicit locked/available split."""

    __tablename__ = "balances"
    __table_args__ = (
        UniqueConstraint("user_id", "asset", name="uq_balances_user_asset"),
        Index("ix_balances_user_asset", "user_id", "asset"),
    )

    id:                Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id:           Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    asset:             Mapped[str]      = mapped_column(String(20), nullable=False)
    total_balance:     Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    locked_balance:    Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    available_balance: Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    version:           Mapped[int]      = mapped_column(BigInteger, nullable=False, default=1)
    updated_at:        Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="balances")

    # ─── Invariant helpers (used by tests + service layer assertions) ────────

    def check_invariant(self) -> None:
        """Assert that total == locked + available.

        Called from service-layer post-conditions and from tests; never
        bypassed in production code.
        """
        expected_total = self.locked_balance + self.available_balance
        if self.total_balance != expected_total:
            raise RuntimeError(
                f"Balance invariant violated for (user={self.user_id}, asset={self.asset}): "
                f"total={self.total_balance} != locked+available={expected_total}"
            )

    def __repr__(self) -> str:
        return (
            f"<Balance user_id={self.user_id} asset={self.asset!r} "
            f"total={self.total_balance} locked={self.locked_balance} avail={self.available_balance}>"
        )
