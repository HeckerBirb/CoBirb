"""Encrypted session manager.

Persists conversations to disk only as an encrypted blob. The plaintext session
exists in RAM only, never on disk. Each turn carries a content hash so tampering
with the session file is detectable on reload. See DESIGN.md §7.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    """A single user or assistant message."""

    role: str
    content: str
    tool_use: list[dict[str, Any]] | None = None
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _stored_hash: str | None = None

    @property
    def hash(self) -> str:
        """The hash stored on disk, compared against the live ``digest``.

        The persisted hash is separate from the current content's digest so
        that tampering with the content on reload is detected.
        """
        return self._stored_hash or self.digest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_use": self.tool_use,
            "ts": self.ts,
            "hash": self.digest(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        return cls(
            role=data["role"],
            content=data["content"],
            tool_use=data.get("tool_use"),
            ts=data.get("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            _stored_hash=data.get("hash"),
        )

    def digest(self) -> str:
        """Content hash for tamper detection."""
        return hashlib.sha256(f"{self.role}:{self.content}".encode("utf-8")).hexdigest()


@dataclass
class Session:
    """An encrypted, persisted conversation."""

    schema: int = 1
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    working_dir: str = "."
    persona: str = "noah"
    turns: list[Turn] = field(default_factory=list)
    summary: str | None = None

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)

    def add_text(self, role: str, content: str) -> None:
        self.add(Turn(role=role, content=content))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "cobirb-session",
            "schema": self.schema,
            "created_at": self.created_at,
            "working_dir": self.working_dir,
            "persona": self.persona,
            "turns": [t.to_dict() for t in self.turns],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            schema=data.get("schema", 1),
            created_at=data.get("created_at"),
            working_dir=data.get("working_dir", "."),
            persona=data.get("persona", "noah"),
            turns=[Turn.from_dict(t) for t in data.get("turns", [])],
            summary=data.get("summary"),
        )


class SessionManager:
    """Manages an encrypted session file.

    The plaintext JSON is serialized to memory and handed to the crypto backend.
    Only the encrypted blob is ever written to disk.
    """

    def __init__(
        self,
        path: str,
        crypto: Any,
        working_dir: str = ".",
        persona: str = "noah",
        password: str | None = None,
    ) -> None:
        self.path = path
        self.crypto = crypto
        self.working_dir = working_dir
        self.persona = persona
        self.password = password
        self.session: Session | None = None

    @classmethod
    def create(
        cls,
        path: str,
        crypto: Any,
        working_dir: str = ".",
        persona: str = "noah",
        password: str | None = None,
    ) -> "SessionManager":
        manager = cls(path, crypto, working_dir, persona, password)
        manager.session = Session(created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return manager

    @classmethod
    def load(
        cls, path: str, crypto: Any, password: str | None, working_dir: str = ".", persona: str = "noah"
    ) -> "SessionManager":
        """Load an existing session, verifying turn hashes against the file.

        The ``password`` is used to decrypt the blob and is stored for the
        lifetime of the manager so the session can be re-saved.
        """
        manager = cls(path, crypto, working_dir, persona, password)
        blob = _read_blob(path)
        plaintext = crypto.decrypt(blob, password)
        data = json.loads(plaintext)
        session = Session.from_dict(data)
        manager._verify_hashes(session)
        manager.session = session
        return manager

    def save(self, password: str | None) -> bytes | None:
        """Encrypt the in-memory session and write the blob to disk.

        Returns the encrypted blob (or ``None`` if the manager was not
        created/loaded, so callers can detect the failure cleanly).
        """
        if self.session is None:
            return None
        plaintext = json.dumps(self.session.to_dict(), indent=2, ensure_ascii=False)
        blob = self.crypto.encrypt(plaintext, password)
        _write_blob(self.path, blob)
        return blob

    def _verify_hashes(self, session: Session) -> None:
        """Raise if any stored turn's content no longer matches its hash."""
        for stored in session.turns:
            if stored.digest() != stored.hash:
                raise ValueError(f"tampered session detected in {self.path} (turn {stored.role} corrupted)")


def _read_blob(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_blob(path: str, blob: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)
