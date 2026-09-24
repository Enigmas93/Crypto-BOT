"""Persistence for the single switchable BingX credential + demo/live mode
(Fase 17 - account switching from the dashboard).

BingX uses ONE api_key/api_secret pair for both its VST (demo) balance and
its real balance - there is nothing to store per mode except which mode is
currently selected. What DOES stay separate per mode is trading history:
"shadow_bingx_demo"/"shadow_bingx_live" (and the momentum equivalents) are
different account_id rows everywhere else in the schema - this repository
only owns the credential/mode singleton itself.

The secret is encrypted at rest (aegis.security.credential_crypto) -
`get_decrypted_credentials` is the only method that ever returns it in
plaintext, and only the BingX Shadow/Momentum trading scripts (via
`aegis.execution.bingx_session`) and the dashboard's live-enrichment
endpoint should ever call it.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from aegis.security.credential_crypto import decrypt, encrypt

_SINGLETON_ID = "singleton"
VALID_MODES = ("demo", "live")


@dataclass(slots=True)
class BingxAccountSettings:
    mode: str
    credentials_configured: bool
    updated_at: object  # datetime | None - kept loose to avoid importing datetime just for the hint


class BingxAccountRepository:
    def __init__(self, pool: asyncpg.Pool, encryption_key: str) -> None:
        self._pool = pool
        self._encryption_key = encryption_key

    async def get_settings(self) -> BingxAccountSettings:
        """Never returns None - a fresh install with no row yet (migration
        not run, or run before this repository existed) is simply "demo
        mode, no credentials", same as an explicit row would say."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT mode, api_key_encrypted, api_secret_encrypted, updated_at "
                "FROM bingx_account_settings WHERE id = $1",
                _SINGLETON_ID,
            )
        if row is None:
            return BingxAccountSettings(mode="demo", credentials_configured=False, updated_at=None)
        return BingxAccountSettings(
            mode=row["mode"],
            credentials_configured=bool(row["api_key_encrypted"] and row["api_secret_encrypted"]),
            updated_at=row["updated_at"],
        )

    async def get_decrypted_credentials(self) -> tuple[str, str] | None:
        """For the BingX trading scripts and dashboard live-enrichment ONLY.
        None if no credentials have ever been saved."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT api_key_encrypted, api_secret_encrypted FROM bingx_account_settings WHERE id = $1",
                _SINGLETON_ID,
            )
        if row is None or not row["api_key_encrypted"] or not row["api_secret_encrypted"]:
            return None
        return (
            decrypt(row["api_key_encrypted"], self._encryption_key),
            decrypt(row["api_secret_encrypted"], self._encryption_key),
        )

    async def save_credentials(self, api_key: str, api_secret: str) -> None:
        """Replaces the stored key/secret. Never touches `mode` - re-saving
        a key (e.g. after BingX key rotation) must not silently kick a live
        account back to demo."""
        api_key_encrypted = encrypt(api_key, self._encryption_key)
        api_secret_encrypted = encrypt(api_secret, self._encryption_key)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bingx_account_settings (id, api_key_encrypted, api_secret_encrypted, mode, updated_at)
                VALUES ($1, $2, $3, 'demo', now())
                ON CONFLICT (id) DO UPDATE SET
                    api_key_encrypted = EXCLUDED.api_key_encrypted,
                    api_secret_encrypted = EXCLUDED.api_secret_encrypted,
                    updated_at = now()
                """,
                _SINGLETON_ID, api_key_encrypted, api_secret_encrypted,
            )

    async def set_mode(self, mode: str) -> None:
        """Business rules (credentials configured, explicit confirmation,
        no open positions in the mode being left) are the API layer's job -
        this just persists the switch."""
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bingx_account_settings (id, mode, updated_at) VALUES ($1, $2, now())
                ON CONFLICT (id) DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
                """,
                _SINGLETON_ID, mode,
            )
