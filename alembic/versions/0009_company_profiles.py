"""company name, sector and industry cache (one row per Yahoo symbol)

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_profiles",
        sa.Column("yahoo_symbol", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("industry", sa.String(length=120), nullable=True),
        sa.Column("quote_type", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_company_profiles_sector", "company_profiles", ["sector"])
    op.create_index("ix_company_profiles_industry", "company_profiles", ["industry"])


def downgrade() -> None:
    op.drop_index("ix_company_profiles_industry", table_name="company_profiles")
    op.drop_index("ix_company_profiles_sector", table_name="company_profiles")
    op.drop_table("company_profiles")
