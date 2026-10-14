"""stored per-trade results (native snapshot + USD / EUR conversions) for page loads

Revision ID: 0013
Revises: 0012
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_results",
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("currency", sa.String(length=6), primary_key=True),
        sa.Column("sig", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=True),
        sa.Column("setup", sa.String(length=120), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("net", sa.Float(), nullable=True),
        sa.Column("gross", sa.Float(), nullable=True),
        sa.Column("fees", sa.Float(), nullable=True),
        sa.Column("mfe", sa.Float(), nullable=True),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("risk", sa.Float(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_trade_results_account", "trade_results", ["account_id", "currency"])


def downgrade() -> None:
    op.drop_index("ix_trade_results_account", table_name="trade_results")
    op.drop_table("trade_results")
