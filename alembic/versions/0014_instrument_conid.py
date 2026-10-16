"""IBKR conid on instruments so BERI/BERIL and ticker changes share one security.

Revision ID: 0014
Revises: 0013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("conid", sa.String(length=32), nullable=True))
    op.create_index("ix_instruments_conid", "instruments", ["conid"])


def downgrade() -> None:
    op.drop_index("ix_instruments_conid", table_name="instruments")
    with op.batch_alter_table("instruments", recreate="never") as b:
        b.drop_column("conid")
