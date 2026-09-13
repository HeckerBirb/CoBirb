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

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field

from . import paths

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
