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
``salt || nonce || ciphertext``. That header is the v0.7.0 hardening change,
and it exists for one reason: the original format was bare base64 with nowhere
to record its parameters, which meant the cost constants could never be raised
— doing so would have silently orphaned every session file already written.
Blobs without the marker are pre-v0.7.0 and are read with the parameters that
era used (N=2**14); they keep opening, and are written back in the new format
the next time the session is saved.

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
# Raising this was only possible because the blob now records which parameters
# produced it (see the format note below). Before that, these constants were
# effectively permanent: change them and every session file ever written stops
# decrypting, with no way to tell which ones were which.
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1

# What v0.1.0–v0.6.0 wrote: RFC 7914 "interactive" parameters, ~16 MiB. Kept
# so those files still open. Never used for new writes.
_LEGACY_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

# Blob format marker. Versioned blobs are `b"cobirb1"` + one JSON header line
# + the raw bytes; anything without the marker is a pre-v0.7.0 blob, which was
# bare base64 with no room to say anything about itself. That omission is the
# finding this addresses: a crypto format that cannot describe its own
# parameters can never change them, which means it can never be strengthened.
_MAGIC = b"cobirb1"
_FORMAT_VERSION = 1


def _split(blob: bytes) -> tuple[dict[str, int], bytes]:
    """Separate a blob's KDF parameters from its ciphertext.

    Two formats, and both have to keep working: a pre-v0.7.0 blob is bare
    base64 with nothing to say about how it was derived, so it is taken as
    the legacy parameters — which is exactly what it was. A versioned blob
    states them, and is read back at whatever cost it was written with, not
    whatever cost is current.

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

    ``cryptography`` is a required dependency and is imported at module scope;
    this docstring used to claim it was imported lazily so the core had no
    hard dependency on it, which was never true of the shipped code. The
    backend is still resolved at construction time and may come back ``None``,
    so an install where the library is present but unusable fails when a
    session is actually encrypted rather than at import.
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

    # ------------------------------------------------------------------ #
    # Attachment encryption: derive once, reuse the key.
    #
    # These are *not* part of the ``SessionCrypto`` ABC (see typing/spi.py's
    # "additive only" rule for a frozen SPI) — a new abstract method would
    # break every third-party crypto plugin the moment core called it. They
    # exist only here, called via ``getattr(crypto, "derive_key", None)``,
    # the same duck-typed-optional-hook pattern ``cancel()`` already uses.
    # A plugin that hasn't heard of them still works, through encrypt()/
    # decrypt() re-deriving the key each time — correct, just slower.
    #
    # The reason to have them at all: encrypt()/decrypt() each pay a full
    # scrypt derivation (deliberately ~a few hundred ms). That is correct
    # once, to unlock a session — it is wrong per attached image. A session
    # with a dozen screenshots would cost several seconds of pure KDF work
    # just to open, which is a property of scrypt being intentionally slow,
    # not something more CPU fixes. Deriving once and reusing the key for
    # every attachment is the actual fix.
    # ------------------------------------------------------------------ #
    def derive_key(self, password: str, salt: bytes) -> bytes:
        """Derive this backend's current-cost scrypt key. Used to encrypt
        session attachments (images) without re-deriving per attachment."""
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library."
            )
        return backend.scrypt_derive(password.encode(), salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)

    def encrypt_bytes(self, plaintext: bytes, key: bytes) -> bytes:
        """AES-256-GCM over an already-derived key. No KDF header: unlike a
        session or catalogue file, an attachment blob trusts the session's
        own header for how the key it was handed was derived — it is
        ``nonce || ciphertext`` and nothing else. A fresh nonce every call,
        since a key is being reused across many of these."""
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library."
            )
        nonce, ciphertext = backend.aes256_gcm_encrypt(plaintext, key)
        return nonce + ciphertext

    def decrypt_bytes(self, blob: bytes, key: bytes) -> bytes:
        backend = self._backend
        if backend is None:
            raise RuntimeError(
                "Crypto backend unavailable. Install the 'cryptography' library."
            )
        if len(blob) <= _NONCE_LEN:
            raise ValueError("this attachment is too short to be genuine")
        nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
        return backend.aes256_gcm_decrypt(ciphertext, key, nonce)

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
        # never the problem. Undecodable base64 used to surface as binascii's
        # "Incorrect padding"; a truncated blob used to slice into an empty
        # salt and nonce and fail inside the cipher instead.
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
