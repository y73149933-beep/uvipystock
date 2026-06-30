"""initial schema: users, api_keys, balances, trading_pairs, orders, trades

Revision ID: 0001
Revises:
Create Date: 2026-06-27 10:00:00.000000

This migration creates the full schema for the crypto-exchange-sandbox paper
trading platform. All enums are created as native Postgres ENUM types first,
then referenced in CREATE TABLE statements.

Idempotent notes
----------------
* `if_exists=True` is NOT used for tables — Alembic guarantees we run on a
  clean DB or after the previous revision.
* ENUM types use `checkfirst=True` so re-running the migration in dev does
  not fail on partially-applied state.
"""
from __future__ import annotations

from alembic import op, context
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql import ARRAY


# ─── Revision identifiers ────────────────────────────────────────────────────
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# ─── Enum type names ─────────────────────────────────────────────────────────
# Use postgresql.ENUM explicitly so `create_type=False` is honored (the generic
# sa.Enum doesn't always forward this kwarg correctly).
# Two variants per enum:
#   *_CREATE    — owns the CREATE TYPE / DROP TYPE calls
#   inline vars — used inside create_table columns; never emit CREATE TYPE
ORDERSIDE_CREATE   = pg.ENUM("buy", "sell", name="orderside")
ORDERTYPE_CREATE   = pg.ENUM(
    "market", "limit", "stop_market", "stop_limit",
    "post_only", "ioc", "fok", "trailing_stop", "iceberg",
    name="ordertype",
)
ORDERSTATUS_CREATE = pg.ENUM(
    "pending", "new", "partially_filled", "filled",
    "canceled", "rejected", "expired",
    name="orderstatus",
)

ORDERSIDE   = pg.ENUM("buy", "sell", name="orderside", create_type=False)
ORDERTYPE   = pg.ENUM(
    "market", "limit", "stop_market", "stop_limit",
    "post_only", "ioc", "fok", "trailing_stop", "iceberg",
    name="ordertype", create_type=False,
)
ORDERSTATUS = pg.ENUM(
    "pending", "new", "partially_filled", "filled",
    "canceled", "rejected", "expired",
    name="orderstatus", create_type=False,
)


def upgrade() -> None:
    # ─── 1. Create enum types ───────────────────────────────────────────────
    # NOTE: We check context.is_offline_mode() to avoid duplicate CREATE TYPE
    # statements when rendering SQL via `alembic upgrade head --sql`. In online
    # mode, checkfirst=True prevents the duplicate; in offline mode it does not.
    bind = op.get_bind()
    ORDERSIDE_CREATE.create(bind, checkfirst=not context.is_offline_mode())
    ORDERTYPE_CREATE.create(bind, checkfirst=not context.is_offline_mode())
    ORDERSTATUS_CREATE.create(bind, checkfirst=not context.is_offline_mode())

    # ─── 2. users ───────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id",            sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email",         sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active",     sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_admin",      sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at",    sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",    sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    # ─── 3. api_keys ────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id",                 sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id",            sa.BigInteger(), nullable=False),
        sa.Column("api_key",            sa.String(length=64), nullable=False),
        sa.Column("secret_hash",        sa.String(length=255), nullable=False),
        sa.Column("label",              sa.String(length=100), nullable=True),
        sa.Column("permissions",        ARRAY(sa.String()), nullable=False,
                  server_default=sa.text("ARRAY['trade', 'read', 'ws']::text[]")),
        sa.Column("rate_limit_per_min", sa.Integer(), nullable=False, server_default=sa.text("120")),
        sa.Column("is_revoked",         sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at",         sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at",         sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_api_keys_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("api_key", name=op.f("uq_api_keys_api_key")),
    )
    op.create_index(op.f("ix_api_keys_api_key"), "api_keys", ["api_key"], unique=True)
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"])

    # ─── 4. balances ────────────────────────────────────────────────────────
    op.create_table(
        "balances",
        sa.Column("id",                sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id",           sa.BigInteger(), nullable=False),
        sa.Column("asset",             sa.String(length=20), nullable=False),
        sa.Column("total_balance",     sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("locked_balance",    sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("available_balance", sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("version",           sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at",        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_balances_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balances")),
        sa.UniqueConstraint("user_id", "asset", name="uq_balances_user_asset"),
    )
    op.create_index("ix_balances_user_asset", "balances", ["user_id", "asset"], unique=False)

    # ─── 5. trading_pairs ───────────────────────────────────────────────────
    op.create_table(
        "trading_pairs",
        sa.Column("id",                 sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol",             sa.String(length=20), nullable=False),
        sa.Column("base_asset",         sa.String(length=20), nullable=False),
        sa.Column("quote_asset",        sa.String(length=20), nullable=False),
        sa.Column("price_precision",    sa.Integer(), nullable=False),
        sa.Column("quantity_precision", sa.Integer(), nullable=False),
        sa.Column("min_lot_size",       sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("max_lot_size",       sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("tick_size",          sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("maker_fee_bps",      sa.Numeric(precision=10, scale=6), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("taker_fee_bps",      sa.Numeric(precision=10, scale=6), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("is_active",          sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at",         sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trading_pairs")),
        sa.UniqueConstraint("symbol", name=op.f("uq_trading_pairs_symbol")),
    )
    op.create_index(op.f("ix_trading_pairs_symbol"), "trading_pairs", ["symbol"], unique=True)

    # ─── 6. orders (self-referential FKs added after creation) ──────────────
    op.create_table(
        "orders",
        sa.Column("id",              sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id",         sa.BigInteger(), nullable=False),
        sa.Column("symbol",          sa.String(length=20), nullable=False),
        sa.Column("side",            ORDERSIDE, nullable=False),
        sa.Column("type",            ORDERTYPE, nullable=False),
        sa.Column("status",          ORDERSTATUS, nullable=False, server_default=sa.text("'new'::orderstatus")),
        sa.Column("price",           sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column("stop_price",      sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column("trailing_delta",  sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column("quantity",        sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("filled_quote_qty", sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("visible_quantity", sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column("hidden_quantity",  sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column("replace_count",   sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("replaces_id",     sa.BigInteger(), nullable=True),
        sa.Column("replaced_by_id",  sa.BigInteger(), nullable=True),
        sa.Column("parent_order_id", sa.BigInteger(), nullable=True),
        sa.Column("sl_order_id",     sa.BigInteger(), nullable=True),
        sa.Column("tp_order_id",     sa.BigInteger(), nullable=True),
        sa.Column("bulk_id",         sa.String(length=36), nullable=True),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version",         sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        # FKs: cross-table
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_orders_user_id_users"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["symbol"], ["trading_pairs.symbol"], name=op.f("fk_orders_symbol_trading_pairs")),
        # FKs: self-referential
        sa.ForeignKeyConstraint(["replaces_id"],    ["orders.id"], name=op.f("fk_orders_replaces_id_orders")),
        sa.ForeignKeyConstraint(["replaced_by_id"], ["orders.id"], name=op.f("fk_orders_replaced_by_id_orders")),
        sa.ForeignKeyConstraint(["parent_order_id"], ["orders.id"], name=op.f("fk_orders_parent_order_id_orders")),
        sa.ForeignKeyConstraint(["sl_order_id"],    ["orders.id"], name=op.f("fk_orders_sl_order_id_orders")),
        sa.ForeignKeyConstraint(["tp_order_id"],    ["orders.id"], name=op.f("fk_orders_tp_order_id_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index("ix_orders_user_symbol_status", "orders", ["user_id", "symbol", "status"], unique=False)
    op.create_index("ix_orders_status_symbol",      "orders", ["status",  "symbol"],           unique=False)
    op.create_index("ix_orders_parent_id",          "orders", ["parent_order_id"],              unique=False)
    op.create_index("ix_orders_bulk_id",            "orders", ["bulk_id"],                      unique=False)
    op.create_index("ix_orders_replaces_id",        "orders", ["replaces_id"],                  unique=False)

    # ─── 7. trades ──────────────────────────────────────────────────────────
    op.create_table(
        "trades",
        sa.Column("id",             sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("taker_order_id", sa.BigInteger(), nullable=False),
        sa.Column("maker_order_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol",         sa.String(length=20), nullable=False),
        sa.Column("price",          sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("quantity",       sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("quote_quantity", sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column("side",           ORDERSIDE, nullable=False,
                  comment="Taker side (buy/sell)"),
        sa.Column("taker_user_id",  sa.BigInteger(), nullable=False),
        sa.Column("maker_user_id",  sa.BigInteger(), nullable=False),
        sa.Column("taker_fee",      sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("maker_fee",      sa.Numeric(precision=36, scale=18), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("executed_at",    sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["taker_order_id"], ["orders.id"], name=op.f("fk_trades_taker_order_id_orders"),
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["maker_order_id"], ["orders.id"], name=op.f("fk_trades_maker_order_id_orders"),
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["symbol"], ["trading_pairs.symbol"], name=op.f("fk_trades_symbol_trading_pairs")),
        sa.ForeignKeyConstraint(["taker_user_id"], ["users.id"], name=op.f("fk_trades_taker_user_id_users")),
        sa.ForeignKeyConstraint(["maker_user_id"], ["users.id"], name=op.f("fk_trades_maker_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trades")),
    )
    op.create_index("ix_trades_symbol_executed_at", "trades", ["symbol", "executed_at"], unique=False)
    op.create_index("ix_trades_taker_user_id",      "trades", ["taker_user_id"],         unique=False)
    op.create_index("ix_trades_maker_user_id",      "trades", ["maker_user_id"],         unique=False)
    op.create_index("ix_trades_taker_order_id",     "trades", ["taker_order_id"],        unique=False)


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_index("ix_trades_taker_order_id",     table_name="trades")
    op.drop_index("ix_trades_maker_user_id",      table_name="trades")
    op.drop_index("ix_trades_taker_user_id",      table_name="trades")
    op.drop_index("ix_trades_symbol_executed_at", table_name="trades")
    op.drop_table("trades")

    op.drop_index("ix_orders_replaces_id",        table_name="orders")
    op.drop_index("ix_orders_bulk_id",            table_name="orders")
    op.drop_index("ix_orders_parent_id",          table_name="orders")
    op.drop_index("ix_orders_status_symbol",      table_name="orders")
    op.drop_index("ix_orders_user_symbol_status", table_name="orders")
    op.drop_table("orders")

    op.drop_index("ix_trading_pairs_symbol", table_name="trading_pairs")
    op.drop_table("trading_pairs")

    op.drop_index("ix_balances_user_asset", table_name="balances")
    op.drop_table("balances")

    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_api_key", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    # Drop enum types
    ORDERSTATUS_CREATE.drop(op.get_bind(), checkfirst=not context.is_offline_mode())
    ORDERTYPE_CREATE.drop(op.get_bind(), checkfirst=not context.is_offline_mode())
    ORDERSIDE_CREATE.drop(op.get_bind(), checkfirst=not context.is_offline_mode())
