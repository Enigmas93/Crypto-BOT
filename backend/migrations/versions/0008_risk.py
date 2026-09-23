"""Phase 7 - Risk Engine: account state (equity/drawdown/loss-streak
tracking, one row per account) and risk_events (append-only audit log of
every risk decision - spec section 85, "registrar absolutamente tudo").

risk_account_state is a plain table (one row per account, tiny). risk_events
is a hypertable - once a real signal/execution flow exists, this table sees
one row per evaluated trade proposal, which is exactly the kind of growing
time-series the rest of the schema already reserves hypertables for.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "risk_account_state",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("equity", sa.Float, nullable=False),
        sa.Column("peak_equity", sa.Float, nullable=False),
        sa.Column("daily_starting_equity", sa.Float, nullable=False),
        sa.Column("daily_realized_pnl", sa.Float, nullable=False, server_default="0"),
        sa.Column("consecutive_losses", sa.Integer, nullable=False, server_default="0"),
        sa.Column("open_positions_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("correlated_exposure_pct", sa.Float, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("account_id", sa.Text, nullable=False),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("strategy_id", sa.Text, nullable=True),
        sa.Column("decision", sa.Text, nullable=False),  # PASS | BLOCK
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("drawdown_state", sa.Text, nullable=False),
        sa.Column("effective_risk_per_trade", sa.Float, nullable=False),
        sa.Column("position_quantity", sa.Float, nullable=True),
        sa.Column("position_notional", sa.Float, nullable=True),
        sa.Column("net_r_multiple", sa.Float, nullable=True),
        sa.PrimaryKeyConstraint("id", "occurred_at"),
    )
    op.execute("SELECT create_hypertable('risk_events', 'occurred_at')")
    op.create_index("ix_risk_events_account_symbol_time", "risk_events", ["account_id", "symbol", "occurred_at"])


def downgrade() -> None:
    op.drop_table("risk_events")
    op.drop_table("risk_account_state")
