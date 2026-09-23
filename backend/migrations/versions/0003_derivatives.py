"""Phase 4 - Derivatives Engine raw storage: open_interest and
long_short_ratios. Both hypertables, fed by periodic REST polling (these
endpoints have no WebSocket stream on Binance Futures - see
constants.POLL_ONLY_ENDPOINTS). Derived features (z-scores, PRICE×OI
patterns) are computed from these and merged into `market_features`
(Phase 3's table) rather than stored again here.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "open_interest",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("period", sa.Text, nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("sum_open_interest", sa.Float, nullable=False),
        sa.Column("sum_open_interest_value", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "period", "time"),
    )
    op.execute("SELECT create_hypertable('open_interest', 'time')")
    op.create_index("ix_open_interest_symbol_period_time", "open_interest", ["symbol", "period", "time"])

    op.create_table(
        "long_short_ratios",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("ratio_type", sa.Text, nullable=False),  # GLOBAL_ACCOUNT | TOP_ACCOUNT | TOP_POSITION
        sa.Column("period", sa.Text, nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("long_short_ratio", sa.Float, nullable=False),
        sa.Column("long_account", sa.Float, nullable=False),
        sa.Column("short_account", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "ratio_type", "period", "time"),
    )
    op.execute("SELECT create_hypertable('long_short_ratios', 'time')")
    op.create_index(
        "ix_long_short_ratios_symbol_type_period_time",
        "long_short_ratios", ["symbol", "ratio_type", "period", "time"],
    )


def downgrade() -> None:
    op.drop_table("long_short_ratios")
    op.drop_table("open_interest")
