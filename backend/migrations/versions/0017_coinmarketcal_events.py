"""Fase 15b - coinmarketcal_events: crypto-specific event calendar
(listings, mainnet launches, hard forks, etc.) from CoinMarketCal's free
tier - see aegis/providers/coinmarketcal/client.py for how the real API
was verified live (the docs site was Cloudflare-blocked; the actual API
subdomain works fine).

Upserted by CoinMarketCal's own event id, not append-only - an event's
`date`/`displayedDate`/`isEstimated` genuinely change over time as
estimated dates firm up (confirmed live: `updatedAt` differed from
`createdAt` on more than one real event on first fetch), so this table
always reflects each event's latest known state, same reasoning as
`news_asset_status` (current state) rather than `news` (append-only, each
article is a one-time fact that never changes after publication).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "coinmarketcal_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("date_end", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("date_type", sa.Text, nullable=False),
        sa.Column("is_estimated", sa.Boolean, nullable=False),
        sa.Column("displayed_date", sa.Text, nullable=False),
        sa.Column("coins", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("impact", sa.Float, nullable=True),
        sa.Column("impact_summary", sa.Text, nullable=True),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("snapshot_url", sa.Text, nullable=True),
        sa.Column("last_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_coinmarketcal_events_date", "coinmarketcal_events", ["date"])


def downgrade() -> None:
    op.drop_index("ix_coinmarketcal_events_date", table_name="coinmarketcal_events")
    op.drop_table("coinmarketcal_events")
