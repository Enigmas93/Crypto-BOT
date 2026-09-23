"""Phase 7b - Global Kill Switch (spec section 23): a sticky, persisted
circuit breaker that stays tripped until a human explicitly resets it -
distinct from RiskEngine's per-evaluation drawdown/loss-streak guards,
which recompute fresh every call and can silently un-halt on their own.

kill_switch_state is one row per account (current state, upserted in
place). kill_switch_events is an append-only audit log of every trigger
and reset - a plain table, not a hypertable, because a kill switch is
meant to trip rarely (unlike risk_events, which grows once per risk
evaluation); reads are always "history for this account", never a
time-range scan across the whole table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_state",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("is_triggered", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "kill_switch_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("action", sa.Text, nullable=False),  # TRIGGERED | RESET
        sa.Column("reasons", postgresql.ARRAY(sa.Text), nullable=True),  # populated for TRIGGERED
        sa.Column("note", sa.Text, nullable=True),  # operator note, populated for RESET
    )
    op.create_index("ix_kill_switch_events_account_time", "kill_switch_events", ["account_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("kill_switch_events")
    op.drop_table("kill_switch_state")
