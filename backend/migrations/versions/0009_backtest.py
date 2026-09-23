"""Phase 8 - Backtest persistence (spec section 86): one row per backtest
run (config snapshot + final metrics) plus one row per trade it produced.

Both are plain tables, not hypertables - unlike risk_events (one row per
live risk evaluation, growing continuously in real time), a backtest run
is a batch write: the whole config, every trade, and the final metrics all
land at once when `run()` finishes, and reads are always "give me this one
run" or "list recent runs", never a time-range scan across the whole
table's history the way hypertables are built for.

Metrics are flattened into real columns on backtest_runs, not a JSONB
blob - BacktestMetrics is a single, fixed, known-in-advance structure
written by exactly one producer (this repository), so there's no need for
the merge-on-conflict flexibility JSONB buys in market_features, where
several independent engines write into the same row.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("interval", sa.Text, nullable=False),
        # config snapshot - exactly what was run, so a result can always be
        # explained without guessing what parameters produced it
        sa.Column("initial_equity", sa.Float, nullable=False),
        sa.Column("fees_pct", sa.Float, nullable=False),
        sa.Column("slippage_pct", sa.Float, nullable=False),
        sa.Column("strategy_ids", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("strategy_weights", postgresql.JSONB, nullable=True),
        sa.Column("confluence_threshold", sa.Float, nullable=False),
        sa.Column("stop_atr_multiple", sa.Float, nullable=False),
        sa.Column("take_profit_r_multiple", sa.Float, nullable=False),
        sa.Column("leverage", sa.Integer, nullable=False),
        sa.Column("warmup_bars", sa.Integer, nullable=False),
        sa.Column("risk_account_id", sa.Text, nullable=False),
        # the actual candle range consumed, including warmup bars
        sa.Column("data_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("data_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("total_bars", sa.Integer, nullable=False),
        # final metrics snapshot (BacktestMetrics, flattened)
        sa.Column("total_trades", sa.Integer, nullable=False),
        sa.Column("wins", sa.Integer, nullable=False),
        sa.Column("losses", sa.Integer, nullable=False),
        sa.Column("win_rate", sa.Float, nullable=False),
        sa.Column("loss_rate", sa.Float, nullable=False),
        sa.Column("gross_profit", sa.Float, nullable=False),
        sa.Column("gross_loss", sa.Float, nullable=False),
        sa.Column("net_pnl", sa.Float, nullable=False),
        sa.Column("profit_factor", sa.Float, nullable=True),
        sa.Column("average_win", sa.Float, nullable=False),
        sa.Column("average_loss", sa.Float, nullable=False),
        sa.Column("expectancy", sa.Float, nullable=False),
        sa.Column("total_fees", sa.Float, nullable=False),
        sa.Column("max_drawdown_pct", sa.Float, nullable=False),
        sa.Column("recovery_factor", sa.Float, nullable=True),
        sa.Column("average_r", sa.Float, nullable=True),
        sa.Column("median_r", sa.Float, nullable=True),
        sa.Column("sharpe_r", sa.Float, nullable=True),
        sa.Column("sortino_r", sa.Float, nullable=True),
        sa.Column("calmar_r", sa.Float, nullable=True),
        sa.Column("max_consecutive_losses", sa.Integer, nullable=False),
        sa.Column("total_return_pct", sa.Float, nullable=False),
        sa.Column("final_equity", sa.Float, nullable=False),
    )
    op.create_index("ix_backtest_runs_symbol_created", "backtest_runs", ["symbol", "created_at"])

    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("entry_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("exit_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("exit_price", sa.Float, nullable=False),
        sa.Column("exit_reason", sa.Text, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("gross_pnl", sa.Float, nullable=False),
        sa.Column("fees_paid", sa.Float, nullable=False),
        sa.Column("net_pnl", sa.Float, nullable=False),
        sa.Column("r_multiple", sa.Float, nullable=False),
        sa.Column("confluence_score", sa.Float, nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=False),
    )
    op.create_index("ix_backtest_trades_run_id", "backtest_trades", ["run_id"])


def downgrade() -> None:
    op.drop_table("backtest_trades")
    op.drop_table("backtest_runs")
