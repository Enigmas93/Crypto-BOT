"""Phase 6 - News Engine: source registry, classified news items, and
asset mentions. Plain PostgreSQL tables, not hypertables - a handful of
official press-release feeds produce a few items a day, nowhere near the
volume hypertable chunking exists for (same reasoning as macro_* in 0005).

`news` allows re-classification in place (`ON CONFLICT ... DO UPDATE`) -
the raw item for a given (source_id, guid) doesn't change, but the
classifier improving later is a legitimate reason to overwrite it, the
same way FRED/BLS/BEA observations get revised.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_sources",
        sa.Column("source_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("feed_url", sa.Text, nullable=False),
        sa.Column("format", sa.Text, nullable=False),
        sa.Column("source_quality", sa.Text, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "news",
        sa.Column("source_id", sa.Text, sa.ForeignKey("news_sources.source_id"), nullable=False),
        sa.Column("guid", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("link", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sentiment", sa.Text, nullable=False),
        sa.Column("sentiment_score", sa.Float, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("magnitude", sa.Text, nullable=False),
        sa.Column("source_quality_score", sa.Float, nullable=False),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("source_id", "guid"),
    )
    op.create_index("ix_news_published_at", "news", ["published_at"])

    op.create_table(
        "news_entities",
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("guid", sa.Text, nullable=False),
        sa.Column("asset", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["source_id", "guid"], ["news.source_id", "news.guid"]),
        sa.PrimaryKeyConstraint("source_id", "guid", "asset"),
    )
    op.create_index("ix_news_entities_asset", "news_entities", ["asset"])


def downgrade() -> None:
    op.drop_table("news_entities")
    op.drop_table("news")
    op.drop_table("news_sources")
