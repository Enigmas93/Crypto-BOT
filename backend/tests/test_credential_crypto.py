"""Unit tests for aegis.security.credential_crypto (Fase 17)."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from aegis.security.credential_crypto import (
    CredentialDecryptionFailed,
    CredentialEncryptionNotConfigured,
    decrypt,
    encrypt,
)

_KEY = Fernet.generate_key().decode()


def test_encrypt_then_decrypt_round_trips():
    ciphertext = encrypt("my-secret-api-key", _KEY)
    assert decrypt(ciphertext, _KEY) == "my-secret-api-key"


def test_ciphertext_does_not_contain_the_plaintext():
    ciphertext = encrypt("super-secret-value", _KEY)
    assert "super-secret-value" not in ciphertext


def test_encrypt_without_a_key_raises_not_silently_stores_plaintext():
    with pytest.raises(CredentialEncryptionNotConfigured):
        encrypt("anything", "")


def test_decrypt_without_a_key_raises():
    with pytest.raises(CredentialEncryptionNotConfigured):
        decrypt("anything", "")


def test_invalid_key_format_raises_not_configured_not_a_generic_error():
    with pytest.raises(CredentialEncryptionNotConfigured):
        encrypt("value", "not-a-valid-fernet-key")


def test_decrypting_with_the_wrong_key_fails_loudly():
    ciphertext = encrypt("value", _KEY)
    other_key = Fernet.generate_key().decode()
    with pytest.raises(CredentialDecryptionFailed):
        decrypt(ciphertext, other_key)
