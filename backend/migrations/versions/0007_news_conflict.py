"""Phase 6b - news_asset_status: one row per tracked asset holding the
current NEWS_CONFLICT / WAIT_FOR_CONFIRMATION / CONFIRMED verdict (spec
section 41). Plain table, single producer (NewsEngine), same reasoning as
macro_snapshots in 0005 - no JSONB needed.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_asset_status",
        sa.Column("asset", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("distinct_sources", sa.Integer, nullable=False),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("dominant_sentiment", sa.Text, nullable=True),
        sa.Column("window_hours", sa.Integer, nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("news_asset_status")
