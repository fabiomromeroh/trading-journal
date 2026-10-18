"""Daily closes for the balance curve, and updated_at stamps on fills and executions.

Revision ID: 0015
Revises: 0014

``updated_at`` is nullable and has no server default, so existing rows are left in place.
New ORM writes fill it. ``daily_closes`` is derived and can be rebuilt from prices.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("trade_fills", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.create_table(
        "daily_closes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", "day", name="uq_daily_close"),
    )
    op.create_index("ix_daily_close_symbol", "daily_closes", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_daily_close_symbol", table_name="daily_closes")
    op.drop_table("daily_closes")
    with op.batch_alter_table("trade_fills", recreate="never") as b:
        b.drop_column("updated_at")
    with op.batch_alter_table("executions", recreate="never") as b:
        b.drop_column("updated_at")
