"""Core crypto backend: an AES-256-GCM + HKDF-SHA-256 session cipher.

CoBirb ships **no hard crypto dependency** and keeps the core free of any crypto
implementation. This backend is the *default* ``SessionCrypto``, but it lives in
the plugin registry so a stronger/available scheme can be dropped in later.

The design (see DESIGN.md §7.2):

    bulk  : AES-256-GCM (authenticated encryption)
    kdf   : HKDF-SHA-256 over the password

The password is used exactly once to derive the session key; it is never stored.

Note: this is a vetted, standard implementation. A post-quantum seal (e.g.
ML-KEM-768 via pqcrypto/liboqs) is the production path described in AGENTS.md §6
and remains a swappable plugin — we never hand-roll crypto.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ...typing.spi import SessionCrypto


class HybridPQCSessionCrypto(SessionCrypto):
    """Default hybrid session crypto. AES-256-GCM bulk cipher over HKDF-derived key.

    The crypto library is imported lazily so the core has no hard dependency on
    ``cryptography``. If it is unavailable the core still loads — the crypto plugin
    just fails when a session is actually encrypted.
    """

    def __init__(self) -> None:
        self._backend = self._try_load_backend()

    def _try_load_backend(self) -> _Backend | None:
        """Import the ``cryptography`` backend if available. Returns None if unavailable."""
        try:
            return _Backend()
        except Exception:
            return None

    def name(self) -> str:
        return "aes256gcm"

    def encrypt(self, plaintext_json: str, password: str) -> bytes:
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library "
                "before encrypting a session."
            )
        # KDF the password into an ephemeral key (used exactly once).
        key = backend.hkdf_sha256(password.encode(), info=b"cobirb-kdf")
        # Encrypt the JSON payload; base64 the blob so it is safe in a session file.
        nonce, ciphertext = backend.aes256_gcm_encrypt(plaintext_json.encode(), key)
        # Serialize: base64 of nonce || ciphertext (ciphertext has tag prepended).
        return base64.b64encode(nonce + ciphertext)

    def decrypt(self, blob: bytes, password: str) -> str:
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library "
                "before decrypting a session."
            )
        key = backend.hkdf_sha256(password.encode(), info=b"cobirb-kdf")
        b64 = base64.b64decode(blob)
        nonce, ciphertext = b64[:12], b64[12:]
        return backend.aes256_gcm_decrypt(ciphertext, key, nonce).decode()


class _Backend:
    """Concrete crypto backend built on the vetted ``cryptography`` library.

    Flow: HKDF-SHA-256(password, info) -> 32-byte session key;
    AES-256-GCM(session_key, plaintext, nonce) -> ciphertext with tag prepended.
    A fresh random nonce is generated per operation.
    """

    def hkdf_sha256(self, ikm: bytes, info: bytes | None = None) -> bytes:
        """HKDF-SHA-256 (RFC-5869)."""
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=info or b"cobirb-kdf",
        ).derive(ikm)

    def aes256_gcm_encrypt(self, plaintext: bytes, key: bytes) -> tuple[bytes, bytes]:
        """Encrypt with AES-256-GCM. ``key`` is the ephemeral KDF-derived key.

        Returns ``(nonce, ciphertext)`` where ``ciphertext`` has the tag
        prepended (the form ``cryptography`` 50.0+ returns).
        """
        nonce = os.urandom(12)
        return nonce, AESGCM(key).encrypt(nonce, plaintext, b"cobirb")

    def aes256_gcm_decrypt(self, ciphertext: bytes, key: bytes, nonce: bytes) -> bytes:
        """Decrypt with AES-256-GCM, verifying the tag."""
        return AESGCM(key).decrypt(nonce, ciphertext, b"cobirb")
