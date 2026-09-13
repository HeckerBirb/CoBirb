"""Encrypted session manager.

Persists conversations to disk only as an encrypted blob. The plaintext session
exists in RAM only, never on disk. Each turn carries a content hash so tampering
with the session file is detectable on reload.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from . import paths

# Which stage of a plan-mode run produced a turn (Turn.phase). Compared in
# three modules; a typo here renders wrong rather than failing.
PHASE_PLAN = "plan"
PHASE_ACT = "act"
PHASE_VALIDATE = "validate"


def default_sessions_dir() -> str:
    """Where interactive mode looks for and offers to save session files.

    Kept as a name here because the Sessions tab and the CLI both ask for it
    by this name; the path itself is derived in one place (``paths``).
    """
    return paths.sessions_dir()


@dataclass
class SessionFile:
    """One ``.json`` file found in a sessions directory, without decrypting
    it — just what the filesystem can tell us."""

    path: str
    name: str
    size: int
    modified: float


def discover_sessions(directory: str) -> list[SessionFile]:
    """List the session files in ``directory``, most recently modified first.

    Returns ``[]`` for a directory that doesn't exist yet (e.g. no session
    has ever been saved there) rather than raising — an empty list reads
    naturally as "nothing here yet."
    """
    if not os.path.isdir(directory):
        return []
    files = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        files.append(SessionFile(path=path, name=name, size=stat.st_size, modified=stat.st_mtime))
    files.sort(key=lambda f: f.modified, reverse=True)
    return files


@dataclass
class Turn:
    """A single user or assistant message."""

    role: str
    content: str
    tool_use: list[dict[str, Any]] | None = None
    # Which phase of a plan-mode run produced this turn ("plan", "act", or
    # "validate" — see Orchestrator.run()), or None for a normal turn/run
    # with plan mode off. Purely descriptive: never affects how a turn is
    # replayed into context (see Orchestrator._build_context).
    phase: str | None = None
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
            "phase": self.phase,
            "ts": self.ts,
            "hash": self.digest(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        return cls(
            role=data["role"],
            content=data["content"],
            tool_use=data.get("tool_use"),
            phase=data.get("phase"),
            ts=data.get("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            _stored_hash=data.get("hash"),
        )

    def digest(self) -> str:
        """Content hash for tamper detection.

        Covers ``tool_use`` and ``phase`` as well as the text: ``tool_use``
        records which tool ran with which arguments, so a hash over the
        prose alone would happily accept a session whose recorded
        ``read_file`` call had been rewritten into a ``shell`` one; ``phase``
        records which stage of a plan-mode run a turn came from, so the
        same rewrite risk applies to relabeling a "plan" turn as "validate"
        after the fact. Serialized with sorted keys so the digest is stable
        across runs.
        """
        payload = json.dumps(
            {"role": self.role, "content": self.content, "tool_use": self.tool_use, "phase": self.phase},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Session:
    """An encrypted, persisted conversation."""

    schema: int = 1
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    working_dir: str = "."
    # "none" rather than "noah": personas are opt-in, so a session that
    # doesn't record one must reload without one. This defaulted to "noah"
    # from before that changed, which meant a persona-less session file came
    # back wearing a costume nobody had asked for.
    persona: str = "none"
    turns: list[Turn] = field(default_factory=list)
    summary: str | None = None
    # Set only when a run used plan mode (Orchestrator.run(plan_mode=True)):
    # the model's own validate-phase report on whether/how the request was
    # actually fulfilled, with references. None for a normal run.
    validation: str | None = None
    # The flock this session *is*, when it is a flock session rather than a
    # main one. Pairs with a `flock_engaged` turn in the main session carrying
    # the same token, which is what makes the branch and the rejoin both
    # findable months later — you can see where the conversation handed off,
    # follow the token to read what the workers actually did, and see where it
    # came back. One engagement is one flock session; a second round continues
    # it rather than starting another.
    flock: str | None = None

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
            "validation": self.validation,
            "flock": self.flock,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            schema=data.get("schema", 1),
            created_at=data.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            working_dir=data.get("working_dir", "."),
            persona=data.get("persona", "none"),
            turns=[Turn.from_dict(t) for t in data.get("turns", [])],
            summary=data.get("summary"),
            validation=data.get("validation"),
            flock=data.get("flock"),
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
        persona: str = "none",
        password: str | None = None,
    ) -> None:
        # `password` is accepted for call-compatibility but deliberately not
        # retained: save()/load() take it per call, so keeping a copy would
        # hold the unlock secret in memory for the manager's whole lifetime
        # to no purpose.
        self.path = path
        self.crypto = crypto
        self.working_dir = working_dir
        self.persona = persona
        self.session: Session | None = None

    @classmethod
    def create(
        cls,
        path: str,
        crypto: Any,
        working_dir: str = ".",
        persona: str = "none",
        password: str | None = None,
    ) -> "SessionManager":
        manager = cls(path, crypto, working_dir, persona, password)
        manager.session = Session(
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            working_dir=working_dir,
            persona=persona,
        )
        return manager

    @classmethod
    def load(
        cls, path: str, crypto: Any, password: str | None, working_dir: str = ".", persona: str = "none"
    ) -> "SessionManager":
        """Load an existing session, verifying turn hashes against the file.

        The ``password`` decrypts the blob and is not retained; ``save()``
        takes it again per call.
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
    """Write the encrypted session, readable only by its owner.

    The blob is encrypted, so the mode is defence in depth rather than the
    thing keeping it private — but a session file landing world-readable at
    the default umask on a shared machine is a needless invitation, and
    creating it with the mode is free.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # os.open rather than open() + chmod: the latter leaves a window in which
    # the file exists at the umask's mode.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
