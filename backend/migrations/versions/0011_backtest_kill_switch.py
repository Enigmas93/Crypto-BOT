"""Phase 7b/8 follow-up: records whether a backtest run's in-memory kill
switch simulation tripped (spec section 23) - a run that stopped opening
new positions partway through is a materially different result from one
that ran the whole window clean, and belongs on the row, not just in a log
line that's gone the next time the script runs.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("kill_switch_triggered", sa.Boolean, nullable=False,
                                               server_default="false"))
    op.add_column("backtest_runs", sa.Column("kill_switch_reasons", postgresql.ARRAY(sa.Text), nullable=True))
    op.add_column("backtest_runs", sa.Column("kill_switch_tripped_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_runs", "kill_switch_tripped_at")
    op.drop_column("backtest_runs", "kill_switch_reasons")
    op.drop_column("backtest_runs", "kill_switch_triggered")
