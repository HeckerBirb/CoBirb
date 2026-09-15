"""Core crypto backend: an AES-256-GCM + scrypt session cipher.

CoBirb ships **no hard crypto dependency** and keeps the core free of any crypto
implementation. This backend is the *default* ``SessionCrypto``, but it lives in
the plugin registry so a stronger/available scheme can be dropped in later.

The scheme:

    kdf   : scrypt (N=2**17, r=8, p=1 — OWASP's current recommendation, ~128
            MiB per derivation), with a fresh random salt per encryption,
            stretching the password into a 32-byte key.
    bulk  : AES-256-GCM (authenticated encryption) over that key.

The password is used exactly once to derive the session key; it is never stored.
The salt and nonce are not secret and travel with the ciphertext.

**The blob says how it was made.** A versioned blob is ``b"cobirb1"`` + a
base64 JSON header naming the KDF and its cost + a newline + the base64 of
``salt || nonce || ciphertext``. The header is what makes the cost constants
changeable at all: a format that cannot describe its own parameters can never
raise them, because doing so silently orphans every file already written. A
blob with no marker is read at the interactive parameters (N=2**14) and
written back in the versioned format the next time the session is saved.

No post-quantum KEM here, and that is deliberate rather than an omission. An
ML-KEM-768 (Kyber) seal is a key *encapsulation* mechanism for two parties
exchanging a shared secret over a public key, defending against a future
quantum computer breaking today's RSA/ECC key exchange ("harvest now, decrypt
later"). A session file has no such exchange: one user encrypting to
themselves with a password, no counterparty, no public key. AES-256 is already
considered quantum-resistant for that case (Grover's algorithm only halves its
effective strength, leaving 128 bits), and a KEM derived from the same password
would not raise the cost of a password-guessing attack — only add ceremony
around the same weak point.
"""
from __future__ import annotations

import base64
import binascii
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ...typing.spi import SessionCrypto

_SALT_LEN = 16
_NONCE_LEN = 12

# Current scrypt cost. 2**17 is ~128 MiB and a few hundred milliseconds per
# derivation — OWASP's present recommendation, and the right end of the range
# for a file that sits on disk indefinitely rather than a login that happens
# constantly. The cost is paid once, when a session is unlocked.
#
# Changeable only because the blob records which parameters produced it (see
# the format note above). Without that record these constants are effectively
# permanent: change them and every session file already written stops
# decrypting, with no way to tell which ones were written under which cost.
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1

# RFC 7914 "interactive" parameters, ~16 MiB — what a blob carrying no header
# is read at. Never used for new writes.
_LEGACY_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

# Blob format marker. Versioned blobs are `b"cobirb1"` + one JSON header line
# + the raw bytes; anything without the marker is bare base64, with no room to
# say anything about itself. A crypto format that cannot describe its own
# parameters can never change them, which means it can never be strengthened.
_MAGIC = b"cobirb1"
_FORMAT_VERSION = 1


def _split(blob: bytes) -> tuple[dict[str, int], bytes]:
    """Separate a blob's KDF parameters from its ciphertext.

    Two formats, and both have to keep working. An unmarked blob is bare
    base64 with nothing to say about how it was derived, so it is read at the
    interactive parameters. A versioned blob states them, and is read back at
    whatever cost it was written with, not whatever cost is current.

    A header from a *newer* format version is refused rather than guessed at,
    on the same reasoning as the session schema: failing to open a file is
    recoverable, and a wrong password is what a mis-derived key looks like,
    which would send someone chasing the wrong problem entirely.
    """
    if not blob.startswith(_MAGIC):
        return dict(_LEGACY_SCRYPT), blob
    header_b64, _, body = blob[len(_MAGIC) :].partition(b"\n")
    try:
        header = json.loads(base64.b64decode(header_b64))
        params = {"n": int(header["n"]), "r": int(header["r"]), "p": int(header["p"])}
        version = int(header["v"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"this session's header is unreadable: {exc}") from exc
    if version > _FORMAT_VERSION:
        raise ValueError(
            f"this session uses crypto format v{version}, and this CoBirb reads up to "
            f"v{_FORMAT_VERSION} — it was written by a newer CoBirb"
        )
    return params, body


class AesGcmScryptSessionCrypto(SessionCrypto):
    """Default session crypto. AES-256-GCM bulk cipher over a scrypt-derived key.

    ``cryptography`` is a required dependency, imported at module scope. The
    backend is nevertheless resolved at construction time and may come back
    ``None``, so an install where the library is present but unusable fails
    when a session is actually encrypted rather than at import.
    """

    _AUTO = object()  # sentinel: "detect the backend" vs. an explicit (possibly None) override

    def __init__(self, backend: "_Backend | None | object" = _AUTO) -> None:
        # `backend` is normally left to auto-detect; tests can pass `backend=None`
        # to exercise the "cryptography isn't available" path through the public
        # constructor instead of reaching into a private attribute after the fact.
        self._backend = self._try_load_backend() if backend is self._AUTO else backend

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
        key = backend.scrypt_derive(password.encode(), salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        nonce, ciphertext = backend.aes256_gcm_encrypt(plaintext_json.encode(), key)
        # Salt and nonce are not secret; they must travel with the blob so
        # decrypt() can reproduce the same key and cipher state. The KDF
        # parameters travel with it now too, for the same reason and one
        # more: without them a future CoBirb cannot raise the cost without
        # orphaning every file this one wrote.
        header = json.dumps(
            {"v": _FORMAT_VERSION, "kdf": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P},
            sort_keys=True,
        ).encode()
        return _MAGIC + base64.b64encode(header) + b"\n" + base64.b64encode(salt + nonce + ciphertext)

    def decrypt(self, blob: bytes, password: str) -> str:
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library "
                "before decrypting a session."
            )
        params, body = _split(blob)
        # Both of these say the same thing — this is not a session file — and
        # both exist because the alternative is a failure that *reads* like a
        # wrong password, sending whoever hit it to re-type a password that was
        # never the problem. Undecodable base64 surfaces as binascii's
        # "Incorrect padding" otherwise, and a truncated blob slices into an
        # empty salt and nonce and fails inside the cipher instead.
        try:
            raw = base64.b64decode(body, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"this does not look like a CoBirb session file: {exc}") from exc
        if len(raw) <= _SALT_LEN + _NONCE_LEN:
            raise ValueError("this file is too short to be a CoBirb session")
        salt = raw[:_SALT_LEN]
        nonce = raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
        ciphertext = raw[_SALT_LEN + _NONCE_LEN :]
        key = backend.scrypt_derive(password.encode(), salt, params["n"], params["r"], params["p"])
        return backend.aes256_gcm_decrypt(ciphertext, key, nonce).decode()


class _Backend:
    """Concrete crypto backend built on the vetted ``cryptography`` library.

    Flow: scrypt(password, salt) -> 32-byte session key;
    AES-256-GCM(session_key, plaintext, nonce) -> ciphertext with tag prepended.
    A fresh random salt and nonce are generated per encryption.
    """

    def scrypt_derive(self, password: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
        """Stretch a password into a 32-byte key via scrypt (RFC 7914).

        The cost parameters are arguments rather than constants read from the
        module: decryption has to reproduce whatever cost the blob was
        *written* with, which is not necessarily the current one.
        """
        kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
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
