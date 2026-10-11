"""per-account auto stop: on for Schwab-family accounts, off for everyone else

Existing automatic stops on accounts that are off are cleared. Manual stops (stop_auto not true) are left alone.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _lit(on: bool) -> str:
    pg = op.get_bind().dialect.name == "postgresql"
    return ("true" if on else "false") if pg else ("1" if on else "0")


def upgrade() -> None:
    op.add_column("accounts", sa.Column("auto_stop", sa.Boolean(), nullable=False, server_default=sa.false()))
    on, off = _lit(True), _lit(False)
    op.execute(sa.text(
        "UPDATE accounts SET auto_stop = " + on + " "
        "WHERE lower(broker) IN ('schwab', 'snaptrade', 'thinkorswim', 'tos') "
        "OR lower(coalesce(source_key, '')) IN "
        "('snaptrade', 'schwab', 'schwab_api', 'schwab_csv', 'tos_email', 'tos_statement')"
    ))
    # A known non-Schwab broker stays off even if a source key also matched the line above.
    op.execute(sa.text(
        "UPDATE accounts SET auto_stop = " + off + " "
        "WHERE lower(broker) IN ('trading212', 't212', 'ibkr', 'ibkr_flex', 'demo', 'interactivebrokers') "
        "OR lower(coalesce(source_key, '')) IN ('t212', 'trading212', 'ibkr', 'ibkr_flex')"
    ))
    op.execute(sa.text(
        "UPDATE trades SET initial_stop = NULL, stop_auto = NULL, stop_src = NULL, "
        "stop_raw = NULL, stop_bar = NULL, stop_at = NULL "
        "WHERE stop_auto = " + on + " "
        "AND account_id IN (SELECT id FROM accounts WHERE auto_stop = " + off + ")"
    ))


def downgrade() -> None:
    with op.batch_alter_table("accounts", recreate="never") as b:
        b.drop_column("auto_stop")
