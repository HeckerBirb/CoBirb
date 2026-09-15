"""Memory catalogues: named, optionally-encrypted lists of facts.

A catalogue is one file under ``paths.memories_dir()`` — ``<name>.md`` if it
is plaintext, ``<name>.md.enc`` if it is password-protected — holding a flat
list of short, model-readable facts. There is no metadata, no timestamps, no
source: this text is read straight into a system prompt, so every byte in it
is a byte the model sees, and a catalogue should read like a list a person
would actually write, the same house style ``AGENTS.md`` already commits to.

Unlike a session, a catalogue is never auto-created except for the one
default: ``public.md``, the always-present, unencrypted catalogue every
install starts with. Everything else is created explicitly through
``/memories`` in the TUI — this module has no opinion about that surface,
only about the files themselves.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import paths

PUBLIC_NAME = "public"

_PLAIN_SUFFIX = ".md"
_ENCRYPTED_SUFFIX = ".md.enc"


class CatalogueError(ValueError):
    """A catalogue operation failed for a reason worth showing the user
    verbatim — wrong password, a name already taken, a missing file."""


def _validate_name(name: str) -> str:
    """Return ``name`` if it is a catalogue name, or raise.

    A name becomes a filename directly, so it has to *be* a name and not a
    path: ``../../escaped`` otherwise wrote a catalogue outside
    ``~/.cobirb/memories`` entirely, and a name with a separator in it wrote
    into a subdirectory that ``discover_catalogues`` would never list again.
    Typed by a person into a dialog rather than produced by a model, so this
    is a mistake to catch rather than an attack to repel — but the check
    costs nothing and the failure was silent.
    """
    name = name.strip()
    if not name:
        raise CatalogueError("a catalogue needs a name")
    if name != os.path.basename(name) or name in (os.curdir, os.pardir) or os.sep in name:
        raise CatalogueError(f"'{name}' is not a usable name — a catalogue name is not a path")
    if name.startswith("."):
        raise CatalogueError("a catalogue name cannot start with a dot")
    return name


@dataclass
class CatalogueFile:
    """One catalogue found on disk, without opening it — just what the
    filesystem can tell us (name, path, whether it needs a password)."""

    path: str
    name: str
    encrypted: bool


@dataclass
class MemoryCatalogue:
    """A catalogue with its facts loaded into memory.

    ``password`` is kept only because the whole point of "loading" a
    catalogue is that CoBirb can write back to it without asking again —
    the same reasoning session passwords are held for a session's lifetime.
    It is never serialized.
    """

    name: str
    path: str
    encrypted: bool
    facts: list[str] = field(default_factory=list)
    password: str | None = None

    def render(self) -> str:
        """This catalogue's facts as a system-prompt block, or ``""`` if
        there are none — an empty catalogue contributes nothing rather than
        an empty heading."""
        if not self.facts:
            return ""
        # The same bullet/indent shape the file uses, so a multi-line fact
        # reads as one item in the prompt rather than running into the next.
        return f"From memory catalogue '{self.name}':\n{_render_facts(self.facts)}"


def _plain_path(directory: str, name: str) -> str:
    return os.path.join(directory, f"{name}{_PLAIN_SUFFIX}")


def _encrypted_path(directory: str, name: str) -> str:
    return os.path.join(directory, f"{name}{_ENCRYPTED_SUFFIX}")


def default_catalogue_path() -> str:
    """Where the always-present Public catalogue lives."""
    return _plain_path(paths.memories_dir(), PUBLIC_NAME)


def ensure_public_exists() -> str:
    """Create an empty Public catalogue if none exists yet. Returns its path.

    Called lazily (on first ``/memories`` or first successful ``/remember``)
    rather than at every startup — a directory nobody has used yet should
    stay empty, not accumulate an unused file on every install.
    """
    directory = paths.memories_dir()
    os.makedirs(directory, exist_ok=True)
    path = default_catalogue_path()
    if not os.path.isfile(path):
        _write_plain(path, [])
    return path


def _render_facts(facts: list[str]) -> str:
    """Facts as the catalogue's markdown body.

    A fact spanning several lines is written as one bullet with its
    continuation lines indented, because the prompt box is a multi-line
    editor and ``/remember`` accepts exactly what was typed into it. Written
    flat, the second line was not a bullet, so reading the file back silently
    dropped everything after the first line — the file looked fine and the
    memory was half gone.
    """
    lines = []
    for fact in facts:
        head, *rest = fact.split("\n")
        lines.append(f"- {head}")
        lines.extend(f"  {line}" for line in rest)
    return "\n".join(lines)


def _write(path: str, data: bytes) -> None:
    """Write a catalogue, readable only by its owner, with no window in which
    it isn't.

    ``os.open`` with the mode rather than ``open()`` + ``os.chmod``: the
    latter leaves the file at the umask's mode until the chmod lands, which
    on a permissive umask is 0666 — world *writable*. That matters more here
    than for a session blob: a public catalogue is plaintext by design, and
    its contents are read straight into the system prompt, so a window in
    which another local account can write to it is a window in which they can
    put words in the model's mouth. Same reasoning as ``session._write_blob``.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _write_plain(path: str, facts: list[str]) -> None:
    body = _render_facts(facts)
    _write(path, (body + ("\n" if body else "")).encode("utf-8"))


def _read_plain(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return _parse_facts(text)


def _parse_facts(text: str) -> list[str]:
    """Read a catalogue body back into facts.

    A line starting with ``-`` opens a fact; an indented line continues the
    one before it (see ``_render_facts``). Anything else — a stray heading, a
    note someone typed into the file by hand — is ignored rather than turned
    into a fact nobody wrote.
    """
    facts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            facts.append(stripped[1:].strip())
        elif facts and line.startswith("  ") and stripped:
            facts[-1] = f"{facts[-1]}\n{stripped}"
    return facts


def discover_catalogues(directory: "str | None" = None) -> list[CatalogueFile]:
    """List every catalogue in ``directory`` (default: ``paths.memories_dir()``),
    without opening any of them. Returns ``[]`` for a directory that doesn't
    exist yet, the same convention ``session.discover_sessions`` uses."""
    directory = directory or paths.memories_dir()
    if not os.path.isdir(directory):
        return []
    rows = []
    for fname in os.listdir(directory):
        path = os.path.join(directory, fname)
        if not os.path.isfile(path):
            continue
        if fname.endswith(_ENCRYPTED_SUFFIX):
            rows.append(CatalogueFile(path=path, name=fname[: -len(_ENCRYPTED_SUFFIX)], encrypted=True))
        elif fname.endswith(_PLAIN_SUFFIX):
            rows.append(CatalogueFile(path=path, name=fname[: -len(_PLAIN_SUFFIX)], encrypted=False))
    return sorted(rows, key=lambda row: row.name)


def _existing_path(directory: str, name: str) -> "str | None":
    for suffix, encrypted in ((_PLAIN_SUFFIX, False), (_ENCRYPTED_SUFFIX, True)):
        candidate = os.path.join(directory, f"{name}{suffix}")
        if os.path.isfile(candidate):
            return candidate
    return None


def create(directory: str, name: str, crypto: Any, password: "str | None") -> MemoryCatalogue:
    """Create a new, empty catalogue named ``name``.

    ``password`` empty or ``None`` makes it plaintext; anything else
    encrypts it. Refuses if a catalogue by this name already exists under
    either extension — a create is never a silent overwrite.
    """
    name = _validate_name(name)
    os.makedirs(directory, exist_ok=True)
    if _existing_path(directory, name):
        raise CatalogueError(f"a catalogue named '{name}' already exists")
    if password:
        path = _encrypted_path(directory, name)
        _write(path, crypto.encrypt("", password))
        return MemoryCatalogue(name=name, path=path, encrypted=True, facts=[], password=password)
    path = _plain_path(directory, name)
    _write_plain(path, [])
    return MemoryCatalogue(name=name, path=path, encrypted=False, facts=[])


def load(path: str, crypto: Any, password: "str | None" = None) -> MemoryCatalogue:
    """Open an existing catalogue. ``password`` is required iff the file is
    encrypted; raises ``CatalogueError`` on a wrong password or a corrupt
    file, the same way a session does."""
    if not os.path.isfile(path):
        raise CatalogueError(f"no catalogue at {path}")
    name = os.path.basename(path)
    if name.endswith(_ENCRYPTED_SUFFIX):
        catalogue_name = name[: -len(_ENCRYPTED_SUFFIX)]
        with open(path, "rb") as fh:
            blob = fh.read()
        try:
            text = crypto.decrypt(blob, password or "")
        except Exception as exc:  # noqa: BLE001 - wrong password/corruption, not fatal
            raise CatalogueError(f"could not open '{catalogue_name}' — {exc}") from exc
        return MemoryCatalogue(
            name=catalogue_name, path=path, encrypted=True, facts=_parse_facts(text), password=password
        )
    catalogue_name = name[: -len(_PLAIN_SUFFIX)] if name.endswith(_PLAIN_SUFFIX) else name
    return MemoryCatalogue(name=catalogue_name, path=path, encrypted=False, facts=_read_plain(path))


def save(catalogue: MemoryCatalogue, crypto: Any) -> None:
    """Write ``catalogue`` back to disk in full.

    For an encrypted catalogue this is a fresh encryption every time — a
    fresh salt and nonce, never an in-place append to old ciphertext — the
    same contract every other caller of ``crypto.encrypt`` already relies on.
    """
    if catalogue.encrypted:
        _write(catalogue.path, crypto.encrypt(_render_facts(catalogue.facts), catalogue.password or ""))
    else:
        _write_plain(catalogue.path, catalogue.facts)


def append_fact(catalogue: MemoryCatalogue, text: str) -> None:
    """Add one fact to ``catalogue`` in memory. Caller still has to
    ``save()`` — this never touches disk on its own."""
    text = text.strip()
    if text:
        catalogue.facts.append(text)


def delete(path: str) -> None:
    if os.path.isfile(path):
        os.remove(path)


def rename(path: str, new_name: str) -> str:
    """Rename the catalogue at ``path`` to ``new_name``, keeping its
    encryption suffix. Returns the new path."""
    new_name = _validate_name(new_name)
    directory = os.path.dirname(path)
    if _existing_path(directory, new_name):
        raise CatalogueError(f"a catalogue named '{new_name}' already exists")
    suffix = _ENCRYPTED_SUFFIX if path.endswith(_ENCRYPTED_SUFFIX) else _PLAIN_SUFFIX
    new_path = os.path.join(directory, f"{new_name}{suffix}")
    os.rename(path, new_path)
    return new_path
