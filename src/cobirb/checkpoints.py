"""Snapshots taken before the agent changes a file, so a turn can be undone.

An agent that edits real files will sometimes edit them wrongly, and "keep
your work in git" is advice, not a safety net — plenty of edits happen between
commits. Undo is the cheapest confidence there is: it turns "the agent broke
my file" from a bad afternoon into a keystroke.

**Only what is touched.** Nothing copies the working tree. A tool about to
change a file says which paths it may write (`CobirbTool.writes`), and only
those are copied aside, only the first time in a turn. Snapshotting a project
would be slow and mostly pointless; snapshotting three files is instant.

**Shell is the honest gap.** `shell` cannot say what it will change, so
anything a command does is outside this. That limitation is real, is
documented in the README, and is why `/undo` reports which turn it restored
rather than claiming the workspace is as it was.

**Snapshots are plaintext.** Unlike sessions, which are encrypted because they
hold the conversation. A copy of `src/app.py` exposes nothing that
`src/app.py` did not already expose in the working directory it came from, so
encrypting it would be ceremony. The directory is created 0700 all the same,
and old checkpoints are pruned.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field

from . import paths

logger = logging.getLogger("cobirb")

# How many turns can be stepped back through. Deep history here is not worth
# much — an undo you reach for is nearly always the last thing that happened —
# and every kept turn is disk that never gets reclaimed otherwise.
DEFAULT_KEEP_TURNS = 20


@dataclass
class Checkpoint:
    """One turn's worth of "what these files looked like before"."""

    index: int
    created_at: float
    directory: str
    # Original path -> the name it was saved under, or None when the file did
    # not exist yet and undoing means deleting it.
    files: dict[str, str | None] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.files


@dataclass
class UndoReport:
    """What an undo actually did, so the user is told rather than reassured."""

    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    nothing_to_undo: bool = False

    def describe(self) -> str:
        if self.nothing_to_undo:
            return "Nothing to undo — no file changes have been recorded this session."
        parts = []
        if self.restored:
            parts.append(f"restored {len(self.restored)}: {', '.join(sorted(self.restored))}")
        if self.deleted:
            parts.append(f"removed {len(self.deleted)}: {', '.join(sorted(self.deleted))}")
        if self.failed:
            parts.append(
                "could not undo " + "; ".join(f"{p} ({why})" for p, why in sorted(self.failed.items()))
            )
        if not parts:
            return "That turn changed no files."
        return "Undo: " + "; ".join(parts) + "."


def _workspace_key(cwd: str) -> str:
    """A stable directory name for one workspace.

    Hashed rather than derived from the path so that a checkpoint store can't
    be steered somewhere unexpected by an odd working directory, and so the
    name stays a fixed length.
    """
    absolute = os.path.abspath(cwd)
    digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]
    return f"{os.path.basename(absolute) or 'root'}-{digest}"


class Checkpoints:
    """The undo history for one workspace."""

    def __init__(self, cwd: str, store: str | None = None, keep: int = DEFAULT_KEEP_TURNS) -> None:
        self.cwd = os.path.abspath(cwd)
        self.store = store or os.path.join(paths.cobirb_dir(), "checkpoints", _workspace_key(cwd))
        self.keep = keep
        self._checkpoints: list[Checkpoint] = []
        self._next_index = 0

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def begin_turn(self) -> None:
        """Open a checkpoint for the turn about to run.

        Opened lazily on disk: a turn that changes nothing — most of them,
        since asking a question is not editing — leaves no directory behind.
        """
        self._checkpoints.append(
            Checkpoint(
                index=self._next_index,
                created_at=time.time(),
                directory=os.path.join(self.store, str(self._next_index)),
            )
        )
        self._next_index += 1
        self._prune()

    def record(self, path: str) -> None:
        """Save ``path`` as it is now, before something changes it.

        Only the first record of a file in a turn matters: undo restores the
        state at the start of the turn, so a second edit to the same file must
        not overwrite the copy taken before the first.
        """
        if not self._checkpoints:
            return
        checkpoint = self._checkpoints[-1]
        absolute = os.path.abspath(path)
        if absolute in checkpoint.files:
            return

        if not os.path.isfile(absolute):
            # Undoing a file that did not exist means removing it again.
            checkpoint.files[absolute] = None
            return

        try:
            os.makedirs(checkpoint.directory, mode=0o700, exist_ok=True)
            saved = f"{len(checkpoint.files)}-{os.path.basename(absolute)}"
            shutil.copy2(absolute, os.path.join(checkpoint.directory, saved))
        except OSError:
            # A snapshot that cannot be taken must not stop the edit. The user
            # loses the ability to undo this one file, which is worth saying
            # nothing about mid-turn and is visible in /undo's report later.
            return
        checkpoint.files[absolute] = saved
        self._write_manifest(checkpoint)

    # ------------------------------------------------------------------ #
    # Undoing
    # ------------------------------------------------------------------ #
    def undo_last(self) -> UndoReport:
        """Put back the files the most recent changing turn altered."""
        while self._checkpoints and self._checkpoints[-1].is_empty:
            self._checkpoints.pop()
        if not self._checkpoints:
            return UndoReport(nothing_to_undo=True)

        checkpoint = self._checkpoints.pop()
        report = UndoReport()
        for original, saved in checkpoint.files.items():
            try:
                if saved is None:
                    if os.path.isfile(original):
                        os.remove(original)
                        report.deleted.append(os.path.relpath(original, self.cwd))
                    continue
                source = os.path.join(checkpoint.directory, saved)
                os.makedirs(os.path.dirname(original), exist_ok=True)
                shutil.copy2(source, original)
                report.restored.append(os.path.relpath(original, self.cwd))
            except OSError as exc:
                report.failed[os.path.relpath(original, self.cwd)] = str(exc)

        shutil.rmtree(checkpoint.directory, ignore_errors=True)
        return report

    # ------------------------------------------------------------------ #
    # Reviewing
    # ------------------------------------------------------------------ #
    def originals(self) -> dict[str, str | None]:
        """Every file the agent has changed, and where its *earliest* saved
        copy lives — or ``None`` for one it created.

        Earliest, walking oldest checkpoint first: the question "what has this
        session done to my code" is asked against how the files looked when it
        started, not against the state one turn ago.
        """
        first: dict[str, str | None] = {}
        for checkpoint in self._checkpoints:
            for original, saved in checkpoint.files.items():
                if original in first:
                    continue
                first[original] = (
                    os.path.join(checkpoint.directory, saved) if saved is not None else None
                )
        return first

    def session_diff(self) -> str:
        """A unified diff of everything the agent has changed so far.

        Built from the snapshots rather than from git, for two reasons: it
        works in a directory that isn't a repository, and it shows *the
        agent's* changes specifically rather than conflating them with
        whatever the user had already edited.
        """
        chunks: list[str] = []
        for current, original in sorted(self.originals().items()):
            shown = os.path.relpath(current, self.cwd)
            before = ""
            if original is not None:
                try:
                    with open(original, "r", encoding="utf-8", errors="replace") as fh:
                        before = fh.read()
                except OSError:
                    continue
            try:
                with open(current, "r", encoding="utf-8", errors="replace") as fh:
                    after = fh.read()
            except OSError:
                after = ""  # deleted since, or never created
            if before == after:
                continue
            chunks.append(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=f"{shown} (before this session)",
                        tofile=f"{shown} (now)",
                    )
                )
            )
        return "\n".join(chunk for chunk in chunks if chunk)

    @property
    def undoable_turns(self) -> int:
        return sum(1 for checkpoint in self._checkpoints if not checkpoint.is_empty)

    # ------------------------------------------------------------------ #
    # Housekeeping
    # ------------------------------------------------------------------ #
    def _write_manifest(self, checkpoint: Checkpoint) -> None:
        """Record what was saved, so a half-written checkpoint is readable
        after a crash rather than being an unlabelled pile of copies."""
        try:
            with open(os.path.join(checkpoint.directory, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"created_at": checkpoint.created_at, "files": checkpoint.files}, fh)
        except OSError:
            pass

    def _prune(self) -> None:
        """Drop the oldest checkpoints past ``keep``, from disk as well."""
        while len(self._checkpoints) > self.keep:
            oldest = self._checkpoints.pop(0)
            shutil.rmtree(oldest.directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Whole-tree checkpoints
# --------------------------------------------------------------------------- #
class TreeCheckpoints:
    """Every turn snapshotted as a whole tree, in a git store CoBirb owns.

    The per-file ``Checkpoints`` above can only undo what a tool *declared* it
    would write, which is why ``shell`` was the honest gap: whatever a command
    did was outside undo and outside ``/diff``. This takes the tree itself —
    before a turn and after it — so a file a command deleted or created comes
    back like any other.

    **The project need not be a git repository, and if it is, its repository is
    never touched.** The store is a separate git directory with the project as
    its work tree: a repository project's ``.gitignore`` is respected, a plain
    directory gets CoBirb's built-in ignore list, and the project's own
    ``.git`` is never read from, written to, or snapshotted. git here is
    CoBirb's bookkeeping, not a requirement of the project.

    **It lives as long as the session.** The store is created for the session
    and deleted by ``close()``, so no plaintext history of the project outlives
    it — less residue than the per-file snapshots, which stayed on disk.
    A store left by a process that died is cleaned up by the next one.

    **Undo never takes back your own edits.** It restores only the files the
    last turn changed, and only those still exactly as that turn left them; a
    file changed since is left alone and reported.
    """

    def __init__(self, cwd: str, git: str, store: str | None = None) -> None:
        self.cwd = os.path.realpath(cwd)
        self.git = git
        parent = os.path.join(paths.cobirb_dir(), "checkpoints")
        self.store = store or os.path.join(parent, f"{_workspace_key(cwd)}-tree-{os.getpid()}")
        self._turns: list[tuple[str, str | None]] = []  # (before, after) commit ids
        self._first: str | None = None
        self._ready = False
        _sweep_dead_stores(parent)

    # -- plumbing ----------------------------------------------------------- #
    def _run(self, *args: str, check: bool = True) -> str:
        import subprocess

        command = [
            self.git, f"--git-dir={self.store}", f"--work-tree={self.cwd}",
            # The user's own git configuration must not reach this: no hooks,
            # no signing, no pager, a fixed identity.
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=CoBirb", "-c", "user.email=cobirb@localhost",
            "-c", "core.autocrlf=false", "-c", "core.quotepath=false",
            *args,
        ]
        done = subprocess.run(command, cwd=self.cwd, capture_output=True, text=True,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
        if check and done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or f"git {args[0]} failed")
        return done.stdout

    def _ensure(self) -> None:
        if self._ready:
            return
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        os.makedirs(self.store, mode=0o700, exist_ok=True)
        os.chmod(self.store, 0o700)
        self._run("init", "-q")
        from .plugins.core.ignores import ALWAYS_IGNORED

        exclude = os.path.join(self.store, "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(ALWAYS_IGNORED | {".git"})) + "\n")
        self._ready = True

    def _snapshot(self, label: str) -> str:
        self._ensure()
        self._run("add", "-A", "--", ".")
        self._run("commit", "-q", "--allow-empty", "--no-verify", "-m", label)
        return self._run("rev-parse", "HEAD").strip()

    def _changed(self, before: str, after: str) -> list[tuple[str, str]]:
        out = self._run("diff", "--no-renames", "--name-status", "-z", before, after)
        fields = [f for f in out.split("\0") if f]
        return list(zip(fields[0::2], fields[1::2]))

    def _blob(self, commit: str, path: str) -> str | None:
        out = self._run("rev-parse", "--verify", "-q", f"{commit}:{path}", check=False).strip()
        return out or None

    def _current(self, path: str) -> str | None:
        full = os.path.join(self.cwd, path)
        if not os.path.lexists(full):
            return None
        return self._run("hash-object", "--", path, check=False).strip() or None

    # -- the Checkpoints interface ------------------------------------------ #
    def begin_turn(self) -> None:
        try:
            before = self._snapshot(f"before turn {len(self._turns) + 1}")
        except Exception:  # noqa: BLE001 - undo is a convenience; never cost a turn
            logger.debug("could not snapshot the tree", exc_info=True)
            return
        self._first = self._first or before
        self._turns.append((before, None))

    def end_turn(self) -> None:
        if not self._turns or self._turns[-1][1] is not None:
            return
        try:
            after = self._snapshot(f"after turn {len(self._turns)}")
        except Exception:  # noqa: BLE001
            logger.debug("could not snapshot the tree", exc_info=True)
            return
        self._turns[-1] = (self._turns[-1][0], after)

    def record(self, path: str) -> None:
        """Nothing to do: the whole tree is snapshotted anyway."""

    @property
    def undoable_turns(self) -> int:
        return sum(1 for before, after in self._turns if after and self._changed(before, after))

    def undo_last(self) -> UndoReport:
        self.end_turn()  # a turn still open counts as finished for undo
        while self._turns and (self._turns[-1][1] is None or not self._changed(*self._turns[-1])):
            self._turns.pop()
        if not self._turns:
            return UndoReport(nothing_to_undo=True)
        before, after = self._turns.pop()
        report = UndoReport()
        for status, path in self._changed(before, after):
            if self._current(path) != self._blob(after, path):
                report.failed[path] = "changed since that turn; left as it is"
                continue
            full = os.path.join(self.cwd, path)
            try:
                if status == "A":
                    os.remove(full)
                    report.deleted.append(path)
                else:
                    self._run("checkout", before, "--", path)
                    report.restored.append(path)
            except (OSError, RuntimeError) as exc:
                report.failed[path] = str(exc)
        return report

    def session_diff(self) -> str:
        if self._first is None:
            return ""
        try:
            self._run("add", "-A", "--", ".")
            return self._run("diff", "--cached", "--no-color", "--no-renames", self._first)
        except Exception:  # noqa: BLE001
            logger.debug("could not diff the tree", exc_info=True)
            return ""

    def close(self) -> None:
        shutil.rmtree(self.store, ignore_errors=True)


def _sweep_dead_stores(parent: str) -> None:
    """Remove tree stores whose process is gone (a crash skips ``close()``)."""
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        head, sep, pid = name.rpartition("-tree-")
        if not sep or not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
        except PermissionError:
            pass


def for_workspace(cwd: str) -> "Checkpoints | TreeCheckpoints":
    """Whole-tree checkpoints when git is installed, else per-file ones."""
    git = shutil.which("git")
    return TreeCheckpoints(cwd, git) if git else Checkpoints(cwd)
