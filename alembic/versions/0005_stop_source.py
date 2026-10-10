"""trades.stop_src: how an auto stop was computed ('5m' | 'daily')

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.add_column(sa.Column("stop_src", sa.String(length=8), nullable=True))
        b.add_column(sa.Column("stop_raw", sa.Float(), nullable=True))
        b.add_column(sa.Column("stop_bar", sa.Integer(), nullable=True))
        b.add_column(sa.Column("stop_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("stop_prev", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as b:
        for c in ("stop_prev", "stop_at", "stop_bar", "stop_raw", "stop_src"):
            b.drop_column(c)
