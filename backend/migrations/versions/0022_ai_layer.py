"""AI layer (NVIDIA NIM), listing radar, research results, and per-trade
excursion tracking.

- ai_news_analysis: LLM read of each news item, kept next to (never
  overwriting) the deterministic keyword classification in `news`.
- ai_insights: market briefs / trade reviews / other AI outputs, append-only.
- listing_events: exchange new-listing announcements (Binance/Upbit/OKX),
  deduplicated, for the radar and its alerts.
- strategy_research_runs: backtest evidence the dashboard shows next to
  every active configuration.
- *_positions.best_price/worst_price and *_trades.mfe_r/mae_r: how far a
  trade went in our favor/against before closing, measured from the mark
  price on every poll - the data behind "losers that were green first".

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_news_analysis",
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("guid", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("sentiment", sa.Text, nullable=False),
        sa.Column("magnitude", sa.Integer, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("assets", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("summary_pt", sa.Text, nullable=False),
        sa.Column("analyzed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_id", "guid"], ["news.source_id", "news.guid"]),
        sa.PrimaryKeyConstraint("source_id", "guid"),
    )

    op.create_table(
        "ai_insights",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_ai_insights_kind_subject_time", "ai_insights", ["kind", "subject", "created_at"])

    op.create_table(
        "listing_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("announced_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("bingx_symbol", sa.Text, nullable=True),
        sa.Column("price_at_detection", sa.Float, nullable=True),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("source", "ticker", "announced_at", name="uq_listing_events"),
    )
    op.create_index("ix_listing_events_announced_at", "listing_events", ["announced_at"])

    op.create_table(
        "strategy_research_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("data_source", sa.Text, nullable=False),
        sa.Column("interval", sa.Text, nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False),
        sa.Column("metrics", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    for table in ("shadow_positions", "momentum_positions", "paper_positions"):
        op.add_column(table, sa.Column("best_price", sa.Float, nullable=True))
        op.add_column(table, sa.Column("worst_price", sa.Float, nullable=True))
    for table in ("shadow_trades", "momentum_trades", "paper_trades"):
        op.add_column(table, sa.Column("mfe_r", sa.Float, nullable=True))
        op.add_column(table, sa.Column("mae_r", sa.Float, nullable=True))


def downgrade() -> None:
    for table in ("shadow_trades", "momentum_trades", "paper_trades"):
        op.drop_column(table, "mae_r")
        op.drop_column(table, "mfe_r")
    for table in ("shadow_positions", "momentum_positions", "paper_positions"):
        op.drop_column(table, "worst_price")
        op.drop_column(table, "best_price")
    op.drop_table("strategy_research_runs")
    op.drop_table("listing_events")
    op.drop_table("ai_insights")
    op.drop_table("ai_news_analysis")
