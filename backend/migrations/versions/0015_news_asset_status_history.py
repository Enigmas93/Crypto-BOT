"""Fase 15b - news_asset_status_history: append-only log of every
NEWS_CONFLICT/WAIT_FOR_CONFIRMATION/CONFIRMED verdict computed for each
asset over time.

`news_asset_status` (0007) is deliberately a current-state-only table (one
upserted row per asset) - correct for a live engine asking "what does the
news say right now", but useless for a historical replay: the Backtest
Engine needs to know what the status WAS at some point in the past,
without leaking today's status into that decision (spec's lookahead rule,
enforced everywhere else in this codebase). This table is purely additive
- every `upsert_asset_status` call keeps updating the current-state table
exactly as before, and now also appends one row here, cheaply (a handful
of tracked assets, one insert per News Engine poll cycle).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_asset_status_history",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("asset", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("distinct_sources", sa.Integer, nullable=False),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("dominant_sentiment", sa.Text, nullable=True),
        sa.Column("window_hours", sa.Integer, nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_news_asset_status_history_asset_computed_at",
        "news_asset_status_history", ["asset", "computed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_news_asset_status_history_asset_computed_at", table_name="news_asset_status_history")
    op.drop_table("news_asset_status_history")
