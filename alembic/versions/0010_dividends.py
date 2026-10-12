"""broker cash dividends (T212 history/dividends + SnapTrade/Schwab dividend activities)

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dividends",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("paid_on", sa.DateTime(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("amount_account", sa.Float(), nullable=True),
        sa.Column("account_currency", sa.String(length=3), nullable=True),
        sa.Column("fx_to_account", sa.Float(), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("gross_per_share", sa.Float(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("ticker", sa.String(length=64), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.UniqueConstraint("account_id", "source", "external_id", name="uq_div_source_ext"),
    )
    op.create_index("ix_div_account_symbol", "dividends", ["account_id", "symbol"])
    op.create_index("ix_div_account_paid", "dividends", ["account_id", "paid_on"])
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_div_instrument", "dividends", "instruments", ["instrument_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("fk_div_instrument", "dividends", type_="foreignkey")
    op.drop_index("ix_div_account_paid", table_name="dividends")
    op.drop_index("ix_div_account_symbol", table_name="dividends")
    op.drop_table("dividends")
