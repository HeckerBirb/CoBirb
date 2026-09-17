"""Encrypted session manager.

Persists conversations to disk only as an encrypted blob. The plaintext session
exists in RAM only, never on disk. Each turn carries a content hash so tampering
with the session file is detectable on reload.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import paths

# Which stage of a plan-mode run produced a turn (Turn.phase). Compared in
# three modules; a typo here renders wrong rather than failing.
PHASE_PLAN = "plan"
PHASE_ACT = "act"
PHASE_VALIDATE = "validate"

# The session-file schema this CoBirb writes. See `migrate` for what a bump
# obliges you to do, and for why this is still 1 after eight releases.
SCHEMA_VERSION = 1


class UnsupportedSessionSchema(ValueError):
    """A session file's schema is one this CoBirb cannot read.

    A ``ValueError`` so the existing "could not open that session" paths in
    the CLI and the Sessions tab report it like any other unreadable file,
    with its own message passed through intact (see
    ``runtime.sessions.session_open_error``).
    """


# Upgrades, keyed by the version each one reads. `_MIGRATIONS[n]` takes a
# schema-n payload and returns a schema-(n+1) one; `migrate` chains them.
#
# **Empty, and not a placeholder.** Every field added since schema 1 —
# `phase`, `validation`, `flock`, `forked_from` — was added as optional with a
# default, so an old file loads correctly with no conversion at all. That is
# the cheap path and the one to keep taking: a new optional field needs no
# migration and no bump. A bump is for changes that make an old payload
# genuinely *wrong* rather than merely sparse — a renamed field, a changed
# unit, a restructured turn — and then the function that fixes it goes here,
# `SCHEMA_VERSION` goes up, and a round-trip test covers the upgrade.
_MIGRATIONS: "dict[int, Callable[[dict[str, Any]], dict[str, Any]]]" = {}


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring a session payload up to ``SCHEMA_VERSION``.

    Refuses a file from the *future* rather than reading it optimistically.
    This is the whole reason the version is written down: a newer CoBirb may
    have changed what a field means, and a best-effort read of it would not
    fail — it would succeed, quietly, with the wrong content, and then save
    that back over the original. Declining to open a file is recoverable;
    rewriting someone's history with a misreading of it is not.

    Missing version means schema 1: files written before the field existed.
    """
    version = data.get("schema", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise UnsupportedSessionSchema(f"session schema {version!r} is not a version number")
    if version > SCHEMA_VERSION:
        raise UnsupportedSessionSchema(
            f"this session is schema v{version}, and this CoBirb understands up to "
            f"v{SCHEMA_VERSION} — it was written by a newer CoBirb, so upgrade rather "
            "than opening it here, which would misread it and then save the misreading"
        )
    while version < SCHEMA_VERSION:
        upgrade = _MIGRATIONS.get(version)
        if upgrade is None:  # pragma: no cover - guarded by test_every_schema_step_has_a_migration
            raise UnsupportedSessionSchema(
                f"no migration from session schema v{version} to v{version + 1}"
            )
        data = upgrade(data)
        version += 1
        data["schema"] = version
    return data


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


# The role of the marker `/clear` appends. A role rather than a new field on
# `Turn`, and that is load-bearing: `Turn.digest()` is compared against a hash
# written by whichever CoBirb saved the file, so adding a field to the formula
# would fail every session written before it existed (see that docstring).
# `role` is already part of the digest, so a marker costs no schema bump and
# no migration — an older CoBirb reading one of these sees a turn with an
# unfamiliar role and shows it, rather than failing to load the session.
CLEAR_ROLE = "clear"


def turns_since_clear(turns: "list[Any]") -> "list[Any]":
    """The turns after the most recent ``/clear``, or all of them.

    The single definition of what a clear marker means, shared by the two
    places that must agree about it: the context the model is sent
    (``Orchestrator._build_context``) and the history drawn on resume
    (``tui/transcript.render_history``). If those disagreed, a resumed
    session would show a conversation the model cannot see, or hide one it
    can — and either is worse than not having the feature.
    """
    for index in range(len(turns) - 1, -1, -1):
        if getattr(turns[index], "role", "") == CLEAR_ROLE:
            return list(turns[index + 1:])
    return list(turns)


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
    # Images attached to this turn: [{"id": <content hash>, "filename": <original
    # basename>}, ...]. The bytes live in ``Session.images``, keyed by that id,
    # inside the same encrypted blob as everything else — this is the reference
    # into it. No alt-text/description field: nobody types a caption for their
    # own screenshot, and a model-written one would need a model call this class
    # has no business making. Deliberately *not* covered by digest() below —
    # see that method's docstring for why.
    images: "list[dict[str, str]] | None" = None
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
            "images": self.images,
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
            images=data.get("images"),
            ts=data.get("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            _stored_hash=data.get("hash"),
        )

    def digest(self) -> str:
        """Content hash for tamper detection.

        Covers ``tool_use`` and ``phase`` as well as the text: ``tool_use``
        records which tool ran with which arguments, so a hash over the
        prose alone would happily accept a session whose recorded
        ``read_file`` call rewritten into a ``shell`` one; ``phase``
        records which stage of a plan-mode run a turn came from, so the
        same rewrite risk applies to relabeling a "plan" turn as "validate"
        after the fact. Serialized with sorted keys so the digest is stable
        across runs.

        Deliberately does **not** cover ``images``. This formula is compared
        against a hash stored by whichever CoBirb wrote the file, so adding a
        field to it fails every session written by a CoBirb that hashed
        without it. Images reference supplementary content rather than record
        what the agent did, so leaving them out is a deliberate gap rather
        than a hole in the guarantee.
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

    schema: int = SCHEMA_VERSION
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    working_dir: str = "."
    # "none" rather than "noah": personas are opt-in, so a session that
    # doesn't record one reloads without one. Defaulting to a named persona
    # here would bring a persona-less session back wearing a costume nobody
    # asked for.
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
    # Set only on a session created by fork_session(): "<source path>@turn<N>",
    # this is session branching's equivalent of `flock`'s pairing token — a
    # branch found months from now still says plainly where it came from and
    # at which turn.
    forked_from: str | None = None
    # Every image attached anywhere in this conversation: {id: base64 bytes},
    # keyed by content hash so the same screenshot attached twice is stored
    # once. Turns hold only the id (see Turn.images).
    #
    # **Inside the session, not beside it**, and that placement is the whole
    # design. Separately-encrypted files in a sibling directory would be
    # unreadable to the layer that assembles what the model sees:
    # SessionManager deliberately does not retain a password (see its
    # __init__), and neither does Orchestrator, so a resumed session could
    # only ever show a "[image: x.png]" marker where the image belongs. Living
    # in the session payload, these bytes are already decrypted by the time
    # `load()` returns, under the same AES-256-GCM and the same password as
    # every other field here — same protection, one mechanism instead of two,
    # and an image that survives a resume.
    images: dict[str, str] = field(default_factory=dict)

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)

    def add_text(self, role: str, content: str) -> None:
        self.add(Turn(role=role, content=content))

    def add_clear(self) -> None:
        """Mark "start over from here" — what ``/clear`` records.

        A turn like any other, deliberately: clearing is a point in the
        conversation, not a rewrite of it. Nothing before this is deleted,
        so the session file remains a complete record of what happened for
        anyone auditing or debugging it later; it is only what gets replayed
        — to the model, and onto the screen — that starts here.
        """
        self.add(Turn(role=CLEAR_ROLE, content=""))

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
            "forked_from": self.forked_from,
            "images": self.images,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        # Every path that reads a session goes through here — SessionManager.load,
        # fork_session, the tests — so this is the one place the version has to
        # be honoured, and putting it anywhere else would leave a way in that
        # skips it.
        data = migrate(data)
        return cls(
            schema=data.get("schema", SCHEMA_VERSION),
            created_at=data.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            working_dir=data.get("working_dir", "."),
            persona=data.get("persona", "none"),
            turns=[Turn.from_dict(t) for t in data.get("turns", [])],
            summary=data.get("summary"),
            validation=data.get("validation"),
            flock=data.get("flock"),
            forked_from=data.get("forked_from"),
            images=data.get("images") or {},
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


def _branch_path_for(source_path: str) -> str:
    """A fresh sibling filename for a branch of ``source_path``.

    Alongside the source rather than off in some other directory — a branch
    found later in a file listing should sit next to the conversation it
    came from, the same way the Sessions tab expects everything relevant
    under one directory. A short random suffix rather than a turn count or
    timestamp: branching the same session twice, or branching a branch, must
    never collide with an existing file, and nothing about *when* or *from
    where* needs to be legible from the filename — that's what
    ``Session.forked_from`` is for.
    """
    directory = os.path.dirname(os.path.abspath(source_path)) or "."
    stem, ext = os.path.splitext(os.path.basename(source_path))
    ext = ext or ".json"
    for _ in range(10):
        candidate = os.path.join(directory, f"{stem}.branch-{secrets.token_hex(4)}{ext}")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"could not find a free filename to branch {source_path} into")


def fork_session(
    path: str,
    crypto: Any,
    password: str | None,
    *,
    up_to_turn: int | None = None,
    out_path: str | None = None,
) -> SessionManager:
    """Branch a saved session into a new, independent file.

    Mid-turn steering (``Orchestrator.steer()``) redirects a turn that is
    still running; this is the equivalent for a conversation that has
    already stopped — trying a different direction from a point already on
    disk, without disturbing what got you there. The source at ``path`` is
    only ever *read*: everything is written to a new file, so the original
    stays exactly as it was and is still resumable at its original length.

    ``up_to_turn`` keeps turns ``0..up_to_turn`` inclusive — "branch from
    here" rather than "branch the whole thing" (the default, ``None``,
    which keeps every turn). Out of range raises rather than silently
    clamping: clamping could quietly turn "branch from turn 3" into a full
    copy, or drop turns a caller meant to keep, and either is a worse
    surprise than an error.

    ``out_path`` names the new file explicitly (``cobirb --branch PATH``);
    left unset, a fresh sibling of ``path`` is generated (the Sessions tab's
    one-click "Branch"). An explicit ``out_path`` that already exists is
    refused — a branch is a new thing, never a silent overwrite of whatever
    was already there.
    """
    manager = SessionManager.load(path, crypto, password)
    source = manager.session
    total = len(source.turns)
    if up_to_turn is not None:
        if not (0 <= up_to_turn < total):
            raise ValueError(
                f"turn index {up_to_turn} is out of range for {path} (0..{total - 1})"
            )
        kept = source.turns[: up_to_turn + 1]
        lineage_turn = up_to_turn
    else:
        kept = list(source.turns)
        lineage_turn = total - 1

    if out_path is not None:
        if os.path.exists(out_path):
            raise ValueError(f"{out_path} already exists — choose a different destination")
        branch_path = out_path
    else:
        branch_path = _branch_path_for(path)

    # A truncated branch keeps no summary or validation: both describe the
    # *end* of the conversation, and the turns they describe are the ones
    # being cut off — carrying them over would caption a branch with the
    # conclusion of a conversation it deliberately doesn't contain. A full
    # branch is a faithful copy and keeps them. `flock` is carried either
    # way: it identifies which engagement this conversation belongs to, not
    # any particular turn, so a branch of a flock session is still part of
    # that flock (and `forked_from` says which branch it is).
    truncated = len(kept) < total
    branch = Session(
        working_dir=source.working_dir,
        persona=source.persona,
        # Deep-copied via to_dict/from_dict rather than kept as the same Turn
        # objects: a fresh Turn.from_dict recomputes nothing but re-parses
        # cleanly independent of the source, so mutating the branch later can
        # never reach back into the session it came from.
        turns=[Turn.from_dict(t.to_dict()) for t in kept],
        summary=None if truncated else source.summary,
        validation=None if truncated else source.validation,
        flock=source.flock,
        forked_from=f"{path}@turn{lineage_turn}",
        # Only the images the kept turns actually reference. A branch taken
        # from before an attachment shouldn't carry its bytes around — and a
        # branch taken from after it must, or the turn that shows the model a
        # screenshot would come back as a note saying one belongs there.
        images={
            image["id"]: source.images[image["id"]]
            for turn in kept
            for image in (turn.images or [])
            if image.get("id") in source.images
        },
    )
    branch_manager = SessionManager(branch_path, crypto, source.working_dir, source.persona)
    branch_manager.session = branch
    branch_manager.save(password)
    return branch_manager
