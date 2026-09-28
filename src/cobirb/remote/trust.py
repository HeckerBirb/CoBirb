"""Who trusts whom, and for how long.

**On the main machine** (``paths.remotes_path()``): per remote URL, the
fingerprint of the certificate the user chose to trust, and the token the remote
issued when the user typed its pairing code.

**On the remote** (``paths.remote_worker_dir()``): the hashes of the tokens it
has issued. Only hashes — a copy of this file lets nobody connect.

A pairing lasts ``PAIRING_DAYS`` from its last use on each side, so a remote in
regular use never asks again and one left idle for a month has to be paired
anew. Both files are written ``0600`` via ``os.open`` and replaced atomically:
they are credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from typing import Any

from .. import paths

PAIRING_DAYS = 30
_TTL = PAIRING_DAYS * 24 * 3600


def _read(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TrustStore:
    """The main machine's pairings, keyed by remote URL."""

    def __init__(self, path: str | None = None, *, now=time.time) -> None:
        self._path = path or paths.remotes_path()
        self._now = now
        self._lock = threading.Lock()

    def fingerprint(self, url: str) -> str:
        """The certificate fingerprint trusted for ``url``, or ``""``."""
        return str(_read(self._path).get(url, {}).get("fingerprint", ""))

    def token(self, url: str) -> str:
        """A token for ``url`` that has not lapsed, or ``""``."""
        entry = _read(self._path).get(url, {})
        if not entry.get("token") or self._now() - float(entry.get("last_used", 0)) > _TTL:
            return ""
        return str(entry["token"])

    def days_left(self, url: str) -> int | None:
        """Whole days before the pairing lapses; ``None`` if not paired."""
        entry = _read(self._path).get(url, {})
        if not entry.get("token"):
            return None
        return max(0, int((float(entry.get("last_used", 0)) + _TTL - self._now()) // 86400))

    def trust(self, url: str, fingerprint: str) -> None:
        """Pin ``fingerprint`` for ``url``. A new certificate drops the old token."""
        with self._lock:
            data = _read(self._path)
            entry = data.get(url, {})
            if entry.get("fingerprint") != fingerprint:
                entry = {}
            entry["fingerprint"] = fingerprint
            data[url] = entry
            _write(self._path, data)

    def paired(self, url: str, token: str) -> None:
        with self._lock:
            data = _read(self._path)
            entry = data.get(url, {})
            entry.update(token=token, last_used=self._now())
            data[url] = entry
            _write(self._path, data)

    def used(self, url: str) -> None:
        """Restart the 30 days: every connection counts as use."""
        with self._lock:
            data = _read(self._path)
            if url in data and data[url].get("token"):
                data[url]["last_used"] = self._now()
                _write(self._path, data)

    def forget(self, url: str) -> None:
        with self._lock:
            data = _read(self._path)
            if data.pop(url, None) is not None:
                _write(self._path, data)


class IssuedTokens:
    """The remote's record of the tokens it issued: hashes and last use."""

    def __init__(self, path: str | None = None, *, now=time.time) -> None:
        self._path = path or os.path.join(paths.remote_worker_dir(), "tokens.json")
        self._now = now
        self._lock = threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            data = _read(self._path)
            data[_hash(token)] = self._now()
            _write(self._path, data)
        return token

    def valid(self, token: object) -> bool:
        """Whether ``token`` was issued here and used within the last 30 days.
        A valid one is renewed, and lapsed ones are dropped on the way."""
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            data = _read(self._path)
            now = self._now()
            fresh = {h: t for h, t in data.items() if now - float(t) <= _TTL}
            key = _hash(token)
            ok = key in fresh
            if ok:
                fresh[key] = now
            if ok or fresh != data:
                _write(self._path, fresh)
            return ok
