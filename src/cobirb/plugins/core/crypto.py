"""Core crypto backend: an AES-256-GCM + scrypt session cipher.

CoBirb ships **no hard crypto dependency** and keeps the core free of any crypto
implementation. This backend is the *default* ``SessionCrypto``, but it lives in
the plugin registry so a stronger/available scheme can be dropped in later.

The scheme:

    kdf   : scrypt (RFC 7914 interactive parameters: N=2**14, r=8, p=1), with a
            fresh random salt per encryption, stretching the password into a
            32-byte key.
    bulk  : AES-256-GCM (authenticated encryption) over that key.

The password is used exactly once to derive the session key; it is never stored.
The salt and nonce are not secret and travel with the ciphertext.

Note on post-quantum crypto: earlier design notes called for wrapping the key
in an ML-KEM-768 (Kyber) seal. That's a key *encapsulation* mechanism for two
parties exchanging a shared secret over a public key — it defends against a
future quantum computer breaking today's RSA/ECC key exchange ("harvest now,
decrypt later"). A session file has no such exchange: it's one user
encrypting to themselves with a password, no counterparty, no public key.
AES-256 is already considered quantum-resistant for that case (Grover's
algorithm only halves its effective strength, leaving 128 bits), and a
KEM derived from the same password wouldn't raise the cost of a password-
guessing attack — it would just add ceremony around the same weak point.
So there's no KEM here; this is a vetted, standard AES-256-GCM + scrypt
implementation, deliberately not the PQ seal the original design imagined.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ...typing.spi import SessionCrypto

_SALT_LEN = 16
_NONCE_LEN = 12
# RFC 7914 "interactive" parameters: costs roughly tens of milliseconds and
# ~16 MiB of memory per derivation, deliberately slow to brute-force offline
# without being noticeable for a CLI unlocking a session.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AesGcmScryptSessionCrypto(SessionCrypto):
    """Default session crypto. AES-256-GCM bulk cipher over a scrypt-derived key.

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
        return "aes256gcm-scrypt"

    def encrypt(self, plaintext_json: str, password: str) -> bytes:
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library "
                "before encrypting a session."
            )
        salt = os.urandom(_SALT_LEN)
        key = backend.scrypt_derive(password.encode(), salt)
        nonce, ciphertext = backend.aes256_gcm_encrypt(plaintext_json.encode(), key)
        # Serialize: base64 of salt || nonce || ciphertext (tag prepended to
        # ciphertext). Salt and nonce are not secret; they must travel with
        # the blob so decrypt() can reproduce the same key and cipher state.
        return base64.b64encode(salt + nonce + ciphertext)

    def decrypt(self, blob: bytes, password: str) -> str:
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library "
                "before decrypting a session."
            )
        raw = base64.b64decode(blob)
        salt, nonce, ciphertext = raw[:_SALT_LEN], raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN], raw[_SALT_LEN + _NONCE_LEN :]
        key = backend.scrypt_derive(password.encode(), salt)
        return backend.aes256_gcm_decrypt(ciphertext, key, nonce).decode()


class _Backend:
    """Concrete crypto backend built on the vetted ``cryptography`` library.

    Flow: scrypt(password, salt) -> 32-byte session key;
    AES-256-GCM(session_key, plaintext, nonce) -> ciphertext with tag prepended.
    A fresh random salt and nonce are generated per encryption.
    """

    def scrypt_derive(self, password: bytes, salt: bytes) -> bytes:
        """Stretch a password into a 32-byte key via scrypt (RFC 7914)."""
        kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return kdf.derive(password)

    def aes256_gcm_encrypt(self, plaintext: bytes, key: bytes) -> tuple[bytes, bytes]:
        """Encrypt with AES-256-GCM. ``key`` is the ephemeral KDF-derived key.

        Returns ``(nonce, ciphertext)`` where ``ciphertext`` has the tag
        prepended (the form ``cryptography`` 50.0+ returns).
        """
        nonce = os.urandom(_NONCE_LEN)
        return nonce, AESGCM(key).encrypt(nonce, plaintext, b"cobirb")

    def aes256_gcm_decrypt(self, ciphertext: bytes, key: bytes, nonce: bytes) -> bytes:
        """Decrypt with AES-256-GCM, verifying the tag."""
        return AESGCM(key).decrypt(nonce, ciphertext, b"cobirb")
