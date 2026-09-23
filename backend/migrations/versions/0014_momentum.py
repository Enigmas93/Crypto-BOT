"""Fase 14 - Momentum Engine ("moonshot" scanner, liquid pairs only): same
shape as shadow_positions/shadow_trades/shadow_trading_cursor, its own
tables rather than reused ones - the symbol universe here is chosen
dynamically by the Scanner each cycle (not a fixed collector_symbols list),
and the exit mechanics differ (trailing_order_id instead of
take_profit_order_id, momentum_score carried through for later analysis).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "momentum_positions",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("entry_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("stop_order_id", sa.BigInteger, nullable=False),
        sa.Column("trailing_order_id", sa.BigInteger, nullable=False),
        sa.Column("stop_price", sa.Float, nullable=False),
        sa.Column("risk_amount", sa.Float, nullable=False),
        sa.Column("confluence_score", sa.Float, nullable=False),
        sa.Column("momentum_score", sa.Float, nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "momentum_trades",
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
        sa.Column("momentum_score", sa.Float, nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_momentum_trades_account_symbol_time", "momentum_trades", ["account_id", "symbol", "closed_at"])

    op.create_table(
        "momentum_trading_cursor",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("interval", sa.Text, primary_key=True),
        sa.Column("last_processed_close_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("momentum_trading_cursor")
    op.drop_table("momentum_trades")
    op.drop_table("momentum_positions")
