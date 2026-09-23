"""Phase 3 - market_features: the shared feature-store table.

One row per (symbol, interval, as_of). `features` is JSONB so later phases
(Derivatives, Macro, News, Sentiment engines) can merge in their own keys
without a schema migration each time - see feature_repository.py for the
merge-on-conflict logic that makes that safe.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_features",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("interval", sa.Text, nullable=False),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("quality", sa.Text, nullable=False),
        sa.Column("features", postgresql.JSONB, nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "interval", "as_of"),
    )
    op.execute("SELECT create_hypertable('market_features', 'as_of')")
    op.create_index("ix_market_features_symbol_interval_time", "market_features", ["symbol", "interval", "as_of"])
    # fast existence/JSON-key lookups (e.g. "which bars have a breakout key")
    op.execute("CREATE INDEX ix_market_features_gin ON market_features USING GIN (features)")


def downgrade() -> None:
    op.drop_table("market_features")
