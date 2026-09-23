"""Phase 5 - Macro Engine: series registry, raw observations, and computed
snapshots. Deliberately plain PostgreSQL tables, not hypertables - macro
series update daily/weekly/monthly at most, total volume across every
series tracked will be a few thousand rows for years, nowhere near what
TimescaleDB's chunking exists to help with (unlike candles/trades/etc.).

`macro_snapshots` uses plain columns, not JSONB like `market_features` -
there is exactly one producer (MacroEngine) and a small fixed schema, so
the "multiple engines merge into one row" flexibility `market_features`
needed does not apply here.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "macro_series",
        sa.Column("series_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("frequency", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("importance", sa.Text, nullable=False),
        sa.Column("transformation", sa.Text, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "macro_observations",
        sa.Column("series_id", sa.Text, sa.ForeignKey("macro_series.series_id"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="fred"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("series_id", "date"),
    )
    op.create_index("ix_macro_observations_series_date", "macro_observations", ["series_id", "date"])

    op.create_table(
        "macro_snapshots",
        sa.Column("series_id", sa.Text, sa.ForeignKey("macro_series.series_id"), primary_key=True),
        sa.Column("as_of", sa.Date, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("change_pct", sa.Float, nullable=True),
        sa.Column("yoy_pct_change", sa.Float, nullable=True),
        sa.Column("zscore_vs_trailing", sa.Float, nullable=True),
        sa.Column("quality", sa.Text, nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("macro_snapshots")
    op.drop_table("macro_observations")
    op.drop_table("macro_series")
