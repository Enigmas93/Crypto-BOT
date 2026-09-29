"""System watchdog status (Fase 22): single row with the latest health check,
written every minute by scripts/run_watchdog.py and read by the dashboard.
A stale `checked_at` itself means the watchdog is not running.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_health_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("checked_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_health_state")
