"""indexes for the dashboard / trades / FX reads

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_trades_account_status", "trades", ["account_id", "status"])
    op.create_index("ix_trade_fills_trade_pos", "trade_fills", ["trade_id", "position"])
    op.create_index("ix_fx_quote_day", "fx_rates", ["quote", "day"])


def downgrade() -> None:
    op.drop_index("ix_fx_quote_day", table_name="fx_rates")
    op.drop_index("ix_trade_fills_trade_pos", table_name="trade_fills")
    op.drop_index("ix_trades_account_status", table_name="trades")
