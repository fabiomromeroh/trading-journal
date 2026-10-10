"""multi-broker: account currency/timezone/market, execution currency + FX, instruments, FX rate cache

All new columns are nullable or have server defaults (USD / America/New_York / US), so existing rows keep their meaning.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(length=30), nullable=False),
        sa.Column("broker_symbol", sa.String(length=64), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("exchange", sa.String(length=12), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("asset_type", sa.String(length=16), nullable=False, server_default="STOCK"),
        sa.Column("price_symbol", sa.String(length=32), nullable=True),
        sa.Column("price_scale", sa.Float(), nullable=False, server_default="1"),
        sa.Column("user_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("source_key", "broker_symbol", name="uq_instrument_broker"),
    )
    op.create_index("ix_instruments_isin", "instruments", ["isin"])
    op.create_table(
        "fx_rates",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("quote", sa.String(length=3), primary_key=True),
        sa.Column("rate", sa.Float(), nullable=False),
    )
    # plain ADD COLUMN (no table rebuild: a SQLite rebuild of accounts/trades would cascade-delete child rows)
    op.add_column("accounts", sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"))
    op.add_column("accounts", sa.Column("timezone", sa.String(length=40), nullable=False, server_default="America/New_York"))
    op.add_column("accounts", sa.Column("market", sa.String(length=8), nullable=False, server_default="US"))
    op.add_column("accounts", sa.Column("kind", sa.String(length=16), nullable=True))
    op.add_column("accounts", sa.Column("source_key", sa.String(length=30), nullable=True))
    op.add_column("executions", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column("executions", sa.Column("fx_to_account", sa.Float(), nullable=True))
    op.add_column("executions", sa.Column("instrument_id", sa.Integer(), nullable=True))
    op.add_column("executions", sa.Column("settle_date", sa.Date(), nullable=True))
    op.add_column("trades", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column("trades", sa.Column("instrument_id", sa.Integer(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":  # SQLite cannot ALTER in a constraint (dev only; the app doesn't need it)
        op.create_foreign_key("fk_exec_instrument", "executions", "instruments", ["instrument_id"], ["id"], ondelete="SET NULL")
        op.create_foreign_key("fk_trade_instrument", "trades", "instruments", ["instrument_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("fk_trade_instrument", "trades", type_="foreignkey")
        op.drop_constraint("fk_exec_instrument", "executions", type_="foreignkey")
    # native DROP COLUMN (recreate="never"): keeps rows, no cascade through a table rebuild
    for table, cols in (("trades", ("instrument_id", "currency")),
                        ("executions", ("settle_date", "instrument_id", "fx_to_account", "currency")),
                        ("accounts", ("source_key", "kind", "market", "timezone", "currency"))):
        with op.batch_alter_table(table, recreate="never") as b:
            for c in cols:
                b.drop_column(c)
    op.drop_table("fx_rates")
    op.drop_index("ix_instruments_isin", table_name="instruments")
    op.drop_table("instruments")
