"""Fase 17f - strategy_settings: per-strategy enable/disable toggle from
the dashboard, requested by the user alongside activating
LIQUIDATION_SQUEEZE. Absence of a row means enabled (the default for every
strategy in ALL_STRATEGY_IDS) - this table only ever holds explicit
overrides, so a strategy added to the codebase later starts enabled
without needing a data migration to seed it.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_settings",
        sa.Column("strategy_id", sa.Text, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("strategy_settings")
