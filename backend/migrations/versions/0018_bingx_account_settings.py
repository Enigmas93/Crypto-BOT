"""Fase 17 - bingx_account_settings: the single switchable BingX credential
+ demo/live mode, managed from the dashboard (Configurações > BingX)
instead of BINGX_API_KEY/SECRET/TESTNET in .env.

BingX uses ONE api_key/api_secret pair for both its VST (demo) balance and
its real balance - confirmed directly by the user, not assumed - so unlike
Binance's genuinely separate testnet, there is nothing to store per mode
except which mode ("demo" or "live") is currently selected. The secret is
encrypted at rest (aegis.security.credential_crypto) - this migration seeds
the singleton row from the pre-Fase-17 BINGX_API_KEY/SECRET/TESTNET .env
values if they're present, purely so upgrading doesn't stop the already-
running shadow_bingx/momentum_bingx engines cold - the user can still
manage everything from the dashboard afterward.

What DOES need to stay separate per mode is trading history and risk
state: an account's demo performance must never influence a live kill
switch/drawdown decision, or vice versa. Every table that already scoped
rows by account_id "shadow_bingx"/"momentum_bingx" gets those renamed to
"shadow_bingx_demo"/"momentum_bingx_demo" here - all pre-existing history
is demo/VST data (BINGX_TESTNET defaulted true, and no live-mode code
existed before this), so "demo" is the correct, lossless label for every
row that already exists.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SINGLETON_ID = "singleton"

# Every table with an account_id column that used the old, unsuffixed BingX
# account ids - see ShadowRepository/MomentumRepository/RiskRepository/
# KillSwitchRepository for why each one exists.
_RENAMED_TABLES = (
    "shadow_positions", "shadow_trades", "shadow_trading_cursor",
    "momentum_positions", "momentum_trades", "momentum_trading_cursor",
    "risk_account_state", "risk_events", "kill_switch_state", "kill_switch_events",
)


def upgrade() -> None:
    op.create_table(
        "bingx_account_settings",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("api_key_encrypted", sa.Text, nullable=True),
        sa.Column("api_secret_encrypted", sa.Text, nullable=True),
        sa.Column("mode", sa.Text, nullable=False, server_default="demo"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("mode IN ('demo', 'live')", name="bingx_account_settings_mode_check"),
    )

    for table in _RENAMED_TABLES:
        op.execute(sa.text(f"UPDATE {table} SET account_id = 'shadow_bingx_demo' WHERE account_id = 'shadow_bingx'"))
        op.execute(
            sa.text(f"UPDATE {table} SET account_id = 'momentum_bingx_demo' WHERE account_id = 'momentum_bingx'")
        )

    # One-time seed from the pre-Fase-17 .env values, if any, so upgrading
    # doesn't require re-entering the key from the dashboard before the
    # already-running BingX engines can trade again. Best-effort: any
    # failure here (no key configured, bad encryption key) just leaves the
    # row credential-less - the dashboard's Configurações screen is always
    # the fallback path.
    try:
        from aegis.config import get_settings
        from aegis.security.credential_crypto import CredentialEncryptionNotConfigured, encrypt

        settings = get_settings()
        if settings.bingx_api_key and settings.bingx_api_secret and settings.credential_encryption_key:
            api_key_encrypted = encrypt(settings.bingx_api_key, settings.credential_encryption_key)
            api_secret_encrypted = encrypt(settings.bingx_api_secret, settings.credential_encryption_key)
            op.execute(
                sa.text(
                    "INSERT INTO bingx_account_settings (id, api_key_encrypted, api_secret_encrypted, mode) "
                    "VALUES (:id, :api_key, :api_secret, 'demo')"
                ).bindparams(id=_SINGLETON_ID, api_key=api_key_encrypted, api_secret=api_secret_encrypted)
            )
        else:
            op.execute(
                sa.text("INSERT INTO bingx_account_settings (id, mode) VALUES (:id, 'demo')").bindparams(
                    id=_SINGLETON_ID
                )
            )
    except (CredentialEncryptionNotConfigured, ImportError):
        op.execute(
            sa.text("INSERT INTO bingx_account_settings (id, mode) VALUES (:id, 'demo')").bindparams(id=_SINGLETON_ID)
        )


def downgrade() -> None:
    for table in _RENAMED_TABLES:
        op.execute(sa.text(f"UPDATE {table} SET account_id = 'shadow_bingx' WHERE account_id = 'shadow_bingx_demo'"))
        op.execute(
            sa.text(f"UPDATE {table} SET account_id = 'momentum_bingx' WHERE account_id = 'momentum_bingx_demo'")
        )
        # Any "_live" rows created after upgrading have no pre-Fase-17
        # equivalent account_id - downgrading collapses them back into the
        # single unsuffixed id too, since the old schema only had one.
        op.execute(sa.text(f"UPDATE {table} SET account_id = 'shadow_bingx' WHERE account_id = 'shadow_bingx_live'"))
        op.execute(
            sa.text(f"UPDATE {table} SET account_id = 'momentum_bingx' WHERE account_id = 'momentum_bingx_live'")
        )
    op.drop_table("bingx_account_settings")
