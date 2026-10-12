"""corporate-action adjustments and raw sync payloads

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "corporate_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ratio", sa.Float(), nullable=True),
        sa.Column("new_symbol", sa.String(length=64), nullable=True),
        sa.Column("effective_on", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("external_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("account_id", "external_key", name="uq_cadj_key"),
    )
    op.create_index("ix_cadj_account_symbol", "corporate_adjustments", ["account_id", "symbol"])
    op.create_table(
        "sync_payloads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("filename", sa.String(length=120), nullable=False),
        sa.Column("content_type", sa.String(length=40), nullable=False),
        sa.Column("gzipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("nbytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_sync_payload_account", "sync_payloads", ["account_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_sync_payload_account", table_name="sync_payloads")
    op.drop_table("sync_payloads")
    op.drop_index("ix_cadj_account_symbol", table_name="corporate_adjustments")
    op.drop_table("corporate_adjustments")
