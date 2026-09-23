"""Phase 9 - Paper Trading (spec section 9, 120): simulated live execution
against real, continuously-updating market data - no real money, no real
orders, but real persisted account state so it behaves like a genuine
system that could be pointed at a real broker later, not a toy.

paper_positions holds at most one open position per (account, symbol) -
the same single-position-at-a-time model BacktestEngine already uses,
carried over deliberately for consistency (see PaperTradingEngine's
docstring). paper_trades is the append-only closed-trade log, same shape
as backtest_trades but its own table - these rows represent real elapsed
time, not a reproducible replay, so they're never comparable to a backtest
run and don't belong in the same table. paper_trading_cursor is the only
genuinely new concept: it remembers the last candle close_time this engine
already acted on, per (account, symbol, interval), so a poller calling
run_once() every N seconds never double-processes the same closed candle.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paper_positions",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("entry_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("stop_price", sa.Float, nullable=False),
        sa.Column("take_profit_price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("risk_amount", sa.Float, nullable=False),
        sa.Column("confluence_score", sa.Float, nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.Text, nullable=False),
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
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_paper_trades_account_symbol_time", "paper_trades", ["account_id", "symbol", "closed_at"])

    op.create_table(
        "paper_trading_cursor",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("interval", sa.Text, primary_key=True),
        sa.Column("last_processed_close_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("paper_trading_cursor")
    op.drop_table("paper_trades")
    op.drop_table("paper_positions")
