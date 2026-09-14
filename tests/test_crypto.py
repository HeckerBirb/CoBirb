"""Tests for the core AES-256-GCM + scrypt session crypto backend."""
from __future__ import annotations

import pytest

from cobirb.plugins.core import crypto as crypto_module
from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto


# --------------------------------------------------------------------------- #
# Encryption round-trip (genuine AES-256-GCM via the cryptography library)
# --------------------------------------------------------------------------- #
def test_encrypt_decrypt_round_trip():
    crypto = AesGcmScryptSessionCrypto()
    plaintext = '{"role": "user", "content": "hello"}'
    blob = crypto.encrypt(plaintext, "s3cret")
    assert isinstance(blob, bytes)
    assert blob.decode() != plaintext  # on-disk blob must not be plaintext

    decrypted = crypto.decrypt(blob, "s3cret")
    assert decrypted == plaintext


def test_wrong_password_rejected():
    crypto = AesGcmScryptSessionCrypto()
    blob = crypto.encrypt('{"content": "data"}', "correct")
    # A wrong password must fail GCM authentication (not silently corrupt).
    with pytest.raises(Exception):
        crypto.decrypt(blob, "wrong")


def test_different_blob_per_encrypt_random_nonce():
    crypto = AesGcmScryptSessionCrypto()
    plaintext = '{"content": "same"}'
    blob1 = crypto.encrypt(plaintext, "pw")
    blob2 = crypto.encrypt(plaintext, "pw")
    # Each encrypt uses a fresh random nonce, so the blobs differ.
    assert blob1 != blob2


def test_name_is_aes256gcm_scrypt():
    crypto = AesGcmScryptSessionCrypto()
    assert crypto.name() == "aes256gcm-scrypt"


def test_same_password_derives_different_keys_per_encryption():
    """Each encryption must use a fresh random salt, so brute-forcing one
    session's password doesn't help against another with the same password."""
    crypto = AesGcmScryptSessionCrypto()
    blob1 = crypto.encrypt('{"content": "same"}', "shared-password")
    blob2 = crypto.encrypt('{"content": "same"}', "shared-password")

    # Through the format's own parser rather than re-slicing the bytes here.
    # This test used to base64-decode the whole blob and take the first 16
    # bytes, which encoded the wire layout a second time — so adding the
    # parameter header broke the test without anything about salts changing.
    def salt_of(blob):
        _, body = crypto_module._split(blob)
        return crypto_module.base64.b64decode(body)[: crypto_module._SALT_LEN]

    assert salt_of(blob1) != salt_of(blob2)


# --------------------------------------------------------------------------- #
# Backend-unavailable path (core must not hard-depend on cryptography)
# --------------------------------------------------------------------------- #
def test_backend_unavailable_raises_runtime_error():
    """Simulates the cryptography library being unavailable by passing
    backend=None through the public constructor — not by reaching into
    self._backend after construction, which would break the moment that
    attribute's name or type changed even though the actual behavior being
    tested (fail loudly, don't hang or corrupt) hadn't."""
    crypto = AesGcmScryptSessionCrypto(backend=None)
    with pytest.raises(RuntimeError):
        crypto.encrypt('{"content": "x"}', "pw")
    with pytest.raises(RuntimeError):
        crypto.decrypt(b"anything", "pw")


# --------------------------------------------------------------------------- #
# Versioned blob format (v0.7.0 hardening). The original format was bare
# base64 with nowhere to record its own KDF parameters, which meant the cost
# could never be raised without silently orphaning every file already written.
# --------------------------------------------------------------------------- #
def _legacy_blob(plaintext: str, password: str) -> bytes:
    """A blob in exactly the shape v0.1.0–v0.6.0 wrote: no marker, no header,
    and derived at the old cost. Built here rather than captured as a fixture
    so it stays readable and obviously equivalent to the old code path."""
    import base64
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt, nonce = os.urandom(16), os.urandom(12)
    key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), b"cobirb")
    return base64.b64encode(salt + nonce + ciphertext)


def test_sessions_written_before_the_format_existed_still_open():
    """The whole point of keeping a legacy path: raising the KDF cost must not
    cost anyone the sessions they already have."""
    crypto = AesGcmScryptSessionCrypto()
    blob = _legacy_blob('{"legacy": true}', "pw")

    assert crypto.decrypt(blob, "pw") == '{"legacy": true}'


def test_a_new_blob_records_the_parameters_it_was_written_with():
    """Not asserting the exact header bytes — what matters is that the cost is
    recoverable from the blob rather than assumed from whatever the current
    constants happen to be."""
    import base64
    import json

    crypto = AesGcmScryptSessionCrypto()
    blob = crypto.encrypt('{"x": 1}', "pw")

    assert blob.startswith(crypto_module._MAGIC)
    header = json.loads(base64.b64decode(blob[len(crypto_module._MAGIC):].split(b"\n")[0]))
    assert header["kdf"] == "scrypt"
    assert header["n"] == crypto_module._SCRYPT_N


def test_a_blob_is_read_at_the_cost_it_was_written_with_not_the_current_one(monkeypatch):
    """The property that makes raising the cost safe at all: an old file is
    derived with the old parameters even after the constants move on."""
    crypto = AesGcmScryptSessionCrypto()
    blob = crypto.encrypt('{"x": 1}', "pw")

    # A later CoBirb, with a higher cost, reading a blob this one wrote.
    monkeypatch.setattr(crypto_module, "_SCRYPT_N", 2**18)

    assert crypto.decrypt(blob, "pw") == '{"x": 1}'


def test_a_blob_from_a_newer_format_is_refused_rather_than_misread(monkeypatch):
    crypto = AesGcmScryptSessionCrypto()
    monkeypatch.setattr(crypto_module, "_FORMAT_VERSION", 99)
    blob = crypto.encrypt('{"x": 1}', "pw")
    monkeypatch.setattr(crypto_module, "_FORMAT_VERSION", 1)

    with pytest.raises(ValueError, match="newer CoBirb"):
        crypto.decrypt(blob, "pw")


@pytest.mark.parametrize(
    "blob, expected",
    [
        (b"garbage", "does not look like"),
        (b"", "too short"),
        (b"c2hvcnQ=", "too short"),
        (b"cobirb1!!!\nzzzz", "header is unreadable"),
    ],
)
def test_a_file_that_is_not_a_session_says_so_rather_than_looking_like_a_bad_password(blob, expected):
    """These all used to surface as binascii errors or failures from inside the
    cipher — which read as "wrong password" and send someone off to re-type a
    password that was never the problem."""
    crypto = AesGcmScryptSessionCrypto()

    with pytest.raises(ValueError, match=expected):
        crypto.decrypt(blob, "pw")
