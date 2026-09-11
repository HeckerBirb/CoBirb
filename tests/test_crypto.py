"""Tests for the core AES-256-GCM + HKDF-SHA-256 session crypto backend."""
from __future__ import annotations

import pytest

from cobirb.plugins.core import crypto as crypto_module
from cobirb.plugins.core.crypto import HybridPQCSessionCrypto


# --------------------------------------------------------------------------- #
# Encryption round-trip (genuine AES-256-GCM via the cryptography library)
# --------------------------------------------------------------------------- #
def test_encrypt_decrypt_round_trip():
    crypto = HybridPQCSessionCrypto()
    plaintext = '{"role": "user", "content": "hello"}'
    blob = crypto.encrypt(plaintext, "s3cret")
    assert isinstance(blob, bytes)
    assert blob.decode() != plaintext  # on-disk blob must not be plaintext

    decrypted = crypto.decrypt(blob, "s3cret")
    assert decrypted == plaintext


def test_wrong_password_rejected():
    crypto = HybridPQCSessionCrypto()
    blob = crypto.encrypt('{"content": "data"}', "correct")
    # A wrong password must fail GCM authentication (not silently corrupt).
    with pytest.raises(Exception):
        crypto.decrypt(blob, "wrong")


def test_different_blob_per_encrypt_random_nonce():
    crypto = HybridPQCSessionCrypto()
    plaintext = '{"content": "same"}'
    blob1 = crypto.encrypt(plaintext, "pw")
    blob2 = crypto.encrypt(plaintext, "pw")
    # Each encrypt uses a fresh random nonce, so the blobs differ.
    assert blob1 != blob2


def test_name_is_aes256gcm():
    crypto = HybridPQCSessionCrypto()
    assert crypto.name() == "aes256gcm"


def test_uses_cryptography_library():
    # The backend should have imported successfully.
    assert crypto_module._Backend() is not None


# --------------------------------------------------------------------------- #
# Backend-unavailable path (core must not hard-depend on cryptography)
# --------------------------------------------------------------------------- #
def test_backend_unavailable_raises_runtime_error(monkeypatch):
    crypto = HybridPQCSessionCrypto()
    # Simulate the cryptography library being unavailable.
    crypto._backend = None
    with pytest.raises(RuntimeError):
        crypto.encrypt('{"content": "x"}', "pw")
    with pytest.raises(RuntimeError):
        crypto.decrypt(b"anything", "pw")
