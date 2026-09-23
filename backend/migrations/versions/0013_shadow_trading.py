"""Phase 10 - Shadow Trading (spec section 9, 120): the same decision
pipeline as Paper Trading, but real orders on Binance Futures (testnet by
default) instead of an internally simulated fill.

shadow_positions holds at most one open position per (account, symbol),
same model as paper_positions - plus the two real order IDs
(stop_order_id, take_profit_order_id) needed to reconcile which leg
eventually fills. shadow_trades is the append-only closed-trade log, its
own table for the same reason paper_trades is separate from
backtest_trades: these rows represent a genuinely different kind of
process (real exchange orders, real elapsed time) and are never comparable
to a backtest or paper run. shadow_trading_cursor mirrors
paper_trading_cursor exactly - same idempotency need, same shape.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_positions",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("entry_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("stop_order_id", sa.BigInteger, nullable=False),
        sa.Column("take_profit_order_id", sa.BigInteger, nullable=False),
        sa.Column("stop_price", sa.Float, nullable=False),
        sa.Column("take_profit_price", sa.Float, nullable=False),
        sa.Column("risk_amount", sa.Float, nullable=False),
        sa.Column("confluence_score", sa.Float, nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "shadow_trades",
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
    op.create_index("ix_shadow_trades_account_symbol_time", "shadow_trades", ["account_id", "symbol", "closed_at"])

    op.create_table(
        "shadow_trading_cursor",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("interval", sa.Text, primary_key=True),
        sa.Column("last_processed_close_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("shadow_trading_cursor")
    op.drop_table("shadow_trades")
    op.drop_table("shadow_positions")
