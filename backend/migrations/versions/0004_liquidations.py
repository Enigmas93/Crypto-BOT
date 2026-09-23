"""Phase 4b - Liquidation Engine: raw storage for force-liquidation orders
from the market-wide `!forceOrder@arstream` stream. Derived features
(clusters, acceleration, SqueezeScore) are computed from this and merged
into `market_features`, same pattern as open_interest/long_short_ratios.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "liquidations",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=False),  # SELL = long liquidated, BUY = short liquidated
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("average_price", sa.Float, nullable=False),
        sa.Column("order_status", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "time", "side", "quantity", "price"),
    )
    op.execute("SELECT create_hypertable('liquidations', 'time')")
    op.create_index("ix_liquidations_symbol_time", "liquidations", ["symbol", "time"])


def downgrade() -> None:
    op.drop_table("liquidations")
