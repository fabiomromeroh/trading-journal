"""planned risk journal fields on trades (initial stop, risk $, profit target)

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.add_column(sa.Column("initial_stop", sa.Float(), nullable=True))
        b.add_column(sa.Column("risk_amount", sa.Float(), nullable=True))
        b.add_column(sa.Column("profit_target", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.drop_column("profit_target")
        b.drop_column("risk_amount")
        b.drop_column("initial_stop")
