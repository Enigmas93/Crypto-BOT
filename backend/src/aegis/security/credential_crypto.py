"""Symmetric encryption for credentials stored at rest (Fase 17 - BingX
demo/live account switching from the dashboard).

Uses Fernet (AES-128-CBC + HMAC, from the `cryptography` package), keyed by
CREDENTIAL_ENCRYPTION_KEY - a real, user-provisioned secret, not a fallback
default, because this is the only thing standing between "database access"
and "can place real orders on the exchange" once an account's mode is
switched to 'live'. There is no silent no-encryption fallback: a missing or
invalid key fails loudly instead of ever writing a plaintext secret to the
database.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionNotConfigured(RuntimeError):
    """CREDENTIAL_ENCRYPTION_KEY is missing or not a valid Fernet key."""


class CredentialDecryptionFailed(RuntimeError):
    """Stored ciphertext could not be decrypted with the configured key."""


def _fernet(encryption_key: str) -> Fernet:
    if not encryption_key:
        raise CredentialEncryptionNotConfigured(
            "CREDENTIAL_ENCRYPTION_KEY is not set - generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and add it to .env."
        )
    try:
        return Fernet(encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionNotConfigured(f"CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str, encryption_key: str) -> str:
    return _fernet(encryption_key).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, encryption_key: str) -> str:
    try:
        return _fernet(encryption_key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionFailed(
            "could not decrypt stored credentials - CREDENTIAL_ENCRYPTION_KEY may have changed since they were saved"
        ) from exc
