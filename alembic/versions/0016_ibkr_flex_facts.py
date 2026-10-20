"""IBKR Flex facts and a transfer-pair link on executions.

Revision ID: 0016
Revises: 0015

Additive. ``ibkr_flex_facts`` stores parsed Cash Report, NAV, and the other
optional Flex sections (numbers only, not a second copy of the XML).
``executions.transfer_pair_id`` links an internal transfer out to its in.
Both are nullable or empty-string keyed so existing rows stay valid.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("transfer_pair_id", sa.String(length=64), nullable=True))
    op.create_index("ix_exec_transfer_pair", "executions", ["transfer_pair_id"])
    op.create_table(
        "ibkr_flex_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section", sa.String(length=24), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("conid", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("line_key", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("numbers", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "account_id", "section", "day", "currency", "symbol", "conid", "line_key",
            name="uq_ibkr_flex_fact"),
    )
    op.create_index("ix_ibkr_flex_fact_lookup", "ibkr_flex_facts", ["account_id", "section", "day"])


def downgrade() -> None:
    op.drop_index("ix_ibkr_flex_fact_lookup", table_name="ibkr_flex_facts")
    op.drop_table("ibkr_flex_facts")
    op.drop_index("ix_exec_transfer_pair", table_name="executions")
    with op.batch_alter_table("executions", recreate="never") as batch:
        batch.drop_column("transfer_pair_id")
