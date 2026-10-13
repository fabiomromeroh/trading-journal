"""broker cash flows (deposits / withdrawals) and the stored daily balance series

Revision ID: 0012
Revises: 0011
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cash_flows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.UniqueConstraint("account_id", "source", "external_id", name="uq_cashflow_source_ext"),
    )
    op.create_index("ix_cashflow_account_at", "cash_flows", ["account_id", "occurred_at"])
    op.create_table(
        "balance_points",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("net_deposits", sa.Float(), nullable=False),
        sa.Column("realized_cum", sa.Float(), nullable=False),
        sa.Column("dividends_cum", sa.Float(), nullable=False),
        sa.Column("other_cum", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized", sa.Float(), nullable=True),
        sa.Column("balance", sa.Float(), nullable=True),
        sa.Column("missing", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("account_id", "currency", "day", name="uq_balance_point"),
    )


def downgrade() -> None:
    op.drop_table("balance_points")
    op.drop_index("ix_cashflow_account_at", table_name="cash_flows")
    op.drop_table("cash_flows")
