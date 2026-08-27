"""add orders.locked_amount for per-order balance reservations

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27 10:00:00.000000

Why
---
`locked_amount` tracks the exact balance reservation owned by each active
order. Before this column, cancel/modify recomputed the unlock amount as
``remaining * price`` which:

* unlocked ZERO for MARKET BUY (no price on the row → funds stuck in
  ``locked_balance`` forever — worst leak);
* over/under-unlocked LIMIT BUYs partially filled at better prices.

Backfill strategy
-----------------
For pre-existing rows we approximate from available data:

* stop-type or terminal orders (filled/canceled/rejected/expired) → 0:
  they own no reservation anymore;
* SELL active orders      → remaining base quantity (exact);
* BUY active limit-family → remaining * price (best-effort approximation,
  slightly off when fills happened at better prices — acceptable because
  these historical rows are sandbox data; NEW orders store exact values);
* BUY active market       → cannot be reconstructed (worst-case estimate was
  never persisted) → 0 + a warning is expected during backfill. Historical
  stuck locks, if any, must be repaired manually via admin balance adjust.

The migration is intentionally conservative: it never produces negative or
over-reported locks.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ─── Revision identifiers ────────────────────────────────────────────────────
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


NUMERIC_36_18 = sa.Numeric(36, 18)

# Order type groupings used by the backfill predicate
STOP_TYPES = ("stop_market", "stop_limit", "trailing_stop")
LIMIT_FAMILY = ("limit", "post_only", "ioc", "fok", "iceberg")
TERMINAL_STATUSES = ("filled", "canceled", "rejected", "expired")


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Render a whitelisted Python constant tuple as a SQL IN-list.

    Safe: every value comes from the hard-coded constants above (never from
    user input), and each is single-quoted with embedded quotes escaped.
    `op.execute()` does not accept bound parameters as a second argument, so
    static inlining of code-owned literals is the correct pattern here.
    """
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def upgrade() -> None:
    # 1. Add the column with a non-breaking default of zero.
    op.add_column(
        "orders",
        sa.Column("locked_amount", NUMERIC_36_18, nullable=False, server_default="0"),
    )

    # 2. Backfill best-effort values for existing rows (see module docstring).
    op.execute(
        sa.text(
            f"""
            UPDATE orders SET locked_amount = CASE
                WHEN type IN ({_sql_in_list(STOP_TYPES)})
                     OR status IN ({_sql_in_list(TERMINAL_STATUSES)}) THEN 0
                WHEN side = 'sell'
                    THEN GREATEST(quantity - filled_quantity, 0)
                WHEN side = 'buy' AND type IN ({_sql_in_list(LIMIT_FAMILY)})
                     AND price IS NOT NULL
                    THEN GREATEST(price * (quantity - filled_quantity), 0)
                ELSE 0
            END
            """
        )
    )


def downgrade() -> None:
    op.drop_column("orders", "locked_amount")
