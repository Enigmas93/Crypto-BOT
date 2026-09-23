"""Fase 15b - momentum_scan_results: the Momentum Engine's current top-N
scan (aegis.scanner.ranking.rank_by_momentum), persisted so the dashboard
can show it.

Current-state table, same shape as news_asset_status (0007) - one row per
symbol currently in the top-N, fully replaced every scan cycle
(momentum_scan_interval_seconds, 900s by default). Not a history table:
nothing about "what was scanned an hour ago" is useful here the way
point-in-time news status was for backtesting (spec's lookahead concern) -
the scanner itself has no lookahead risk (it always looks at the current
24h ticker window), so there's nothing to protect against replaying.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "momentum_scan_results",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("momentum_score", sa.Float, nullable=False),
        sa.Column("price_change_pct", sa.Float, nullable=False),
        sa.Column("quote_volume", sa.Float, nullable=False),
        sa.Column("scanned_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("momentum_scan_results")
