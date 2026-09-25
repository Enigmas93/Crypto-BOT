"""Fase 17 - walk_forward_runs: persists WalkForwardEngine results (the
blueprint's "LIVE só é liberado depois que... passou por... WALK-FORWARD
aprovado" validation step, previously entirely missing).

One row per walk-forward run (a run = one symbol/interval/config replayed
across N sequential folds). Per-fold detail (window boundaries, net PnL,
win rate, kill switch flag) is stored as JSONB rather than a child table -
proportionate for a research/validation tool that's read as a whole run,
never queried fold-by-fold across runs the way backtest_trades genuinely
needs row-level SQL access for.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "walk_forward_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("interval", sa.Text, nullable=False),
        sa.Column("strategy_ids", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("window_bars", sa.Integer, nullable=False),
        sa.Column("step_bars", sa.Integer, nullable=False),
        sa.Column("fold_count", sa.Integer, nullable=False),
        sa.Column("profitable_fold_pct", sa.Float, nullable=False),
        sa.Column("average_net_pnl", sa.Float, nullable=False),
        sa.Column("worst_fold_net_pnl", sa.Float, nullable=False),
        sa.Column("any_fold_kill_switch_triggered", sa.Boolean, nullable=False),
        sa.Column("folds", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_walk_forward_runs_created_at", "walk_forward_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_walk_forward_runs_created_at", table_name="walk_forward_runs")
    op.drop_table("walk_forward_runs")
