"""Trend engine paper book (Fase 24): BTC+ETH daily trend simulated against
real BingX prices and funding - no exchange orders.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "trend_account",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("starting_equity", sa.Float, nullable=False),
        sa.Column("cash", sa.Float, nullable=False),
        sa.Column("equity", sa.Float, nullable=False),
        sa.Column("last_rebalance_bar", TS),            # open time of the daily bar last acted on
        sa.Column("last_funding_ms", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_signal", postgresql.JSONB),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "trend_positions",
        sa.Column("account_id", sa.Text, nullable=False),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("qty", sa.Float, nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("mark_price", sa.Float),
        sa.Column("opened_at", TS),
        sa.PrimaryKeyConstraint("account_id", "symbol"),
    )
    op.create_table(
        "trend_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Text, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),     # TRADE | FUNDING
        sa.Column("qty", sa.Float, nullable=False, server_default="0"),
        sa.Column("price", sa.Float, nullable=False, server_default="0"),
        sa.Column("fee", sa.Float, nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Float, nullable=False, server_default="0"),
        sa.Column("funding", sa.Float, nullable=False, server_default="0"),
        sa.Column("side_before", sa.Text),
        sa.Column("side_after", sa.Text),
    )
    op.create_index("ix_trend_ledger_account_ts", "trend_ledger", ["account_id", "ts"])
    op.create_table(
        "trend_equity",
        sa.Column("account_id", sa.Text, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("equity", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("account_id", "ts"),
    )


def downgrade() -> None:
    op.drop_table("trend_equity")
    op.drop_index("ix_trend_ledger_account_ts", table_name="trend_ledger")
    op.drop_table("trend_ledger")
    op.drop_table("trend_positions")
    op.drop_table("trend_account")
