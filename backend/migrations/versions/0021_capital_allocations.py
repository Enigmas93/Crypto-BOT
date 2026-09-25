"""Fase 17g - capital_allocations: splits ONE real BingX balance between
Shadow BingX and Momentum BingX, requested by the user ("faça a repartição
do valor total da conta... para cada estratégia").

Real gap this closes: shadow_bingx_{demo,live} and momentum_bingx_{demo,live}
are two INDEPENDENT engines/processes both calling BingXExecutionProvider.
get_equity() against the SAME underlying account balance (Fase 17b's
sync_equity_from_exchange fix made each engine's local equity accurate
individually, but never accounted for the fact that they share one real
pool) - each engine's RiskEngine sizing was treating the FULL account
balance as if it alone owned it, so both could size a position assuming
100% of the same capital was available, double-counting it. Splitting the
real balance between the two engines before it ever reaches
sync_equity_from_exchange means each engine's risk-per-trade sizing
operates against its own honest slice.

Default split (0.60 / 0.40) - a documented HYPOTHESIS the user can change
from the dashboard, not a validated model (same standard as confluence
weights and every other un-fitted ratio in this codebase): Shadow BingX
runs three established, already-backtested-and-walk-forward-tested
strategies (TREND_PULLBACK/BREAKOUT/MEAN_REVERSION [+LIQUIDATION_SQUEEZE,
currently inert on BingX]); Momentum BingX is explicitly documented
elsewhere in this codebase (aegis/momentum/engine.py's own module
docstring) as a deliberately higher-risk "moonshot" style chasing large,
fast moves - a smaller allocation there is the conservative default, not a
claim that 60/40 is optimal.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capital_allocations",
        sa.Column("engine", sa.Text, primary_key=True),
        sa.Column("allocation_pct", sa.Float, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("allocation_pct > 0 AND allocation_pct <= 1", name="capital_allocations_pct_range"),
    )
    op.execute("INSERT INTO capital_allocations (engine, allocation_pct) VALUES ('shadow_bingx', 0.60)")
    op.execute("INSERT INTO capital_allocations (engine, allocation_pct) VALUES ('momentum_bingx', 0.40)")


def downgrade() -> None:
    op.drop_table("capital_allocations")
