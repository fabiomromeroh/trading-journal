"""journal: option lists, mistakes, question answers, execution grade, auto-stop flag

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.add_column(sa.Column("stop_auto", sa.Boolean(), nullable=True))
        b.add_column(sa.Column("exec_grade", sa.String(length=2), nullable=True))
        b.add_column(sa.Column("journal", sa.Text(), nullable=True))
    op.create_table(
        "journal_options",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("kind", "name", name="uq_journal_option"),
    )
    op.create_index("ix_journal_options_kind", "journal_options", ["kind"])
    op.create_table(
        "trade_mistakes",
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("name", sa.String(length=80), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("trade_mistakes")
    op.drop_index("ix_journal_options_kind", table_name="journal_options")
    op.drop_table("journal_options")
    with op.batch_alter_table("trades") as b:
        b.drop_column("journal")
        b.drop_column("exec_grade")
        b.drop_column("stop_auto")
