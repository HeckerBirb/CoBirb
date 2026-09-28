"""What every built-in tool shares: the base class, and the limits and
helpers more than one tool family uses."""

from __future__ import annotations

import difflib
import json
import os
import tomllib
from typing import Any, ClassVar

from ....typing.spi import Tool

# A tool result is not just shown to the user: it becomes a turn in the
# encrypted session *and* a message in the next model request. So one *call*
# is bounded — a single read taking half the context window is rarely what
# anyone wanted, whatever the hardware allows.
#
# The *file* is not bounded. read_file pages: a call that stops early says
# which lines it returned and what offset continues from, so an arbitrarily
# large file can be read in full, in pieces the window can hold. Truncating
# alone would leave a large file readable from the top and never finishable.
_MAX_READ_BYTES = 256 * 1024

# A preview is for a human to read before saying yes; past a point a longer
# diff makes the decision harder rather than better informed.
_MAX_PREVIEW_BYTES = 8 * 1024


class CobirbTool(Tool):
    """Concrete base class for built-in tools.

    Subclasses declare their machine name as the ``NAME`` class attribute and
    inherit ``name()`` from here. The SPI declares ``name`` as a *method*, and
    overriding it with a plain string would be a different type than the
    interface promises, forcing every consumer to branch on
    ``callable(tool.name)`` — and the one that forgets serializes a bound
    method into a request payload. Declaring the value and the accessor
    separately keeps the terse per-tool declaration without breaking the
    contract a plugin author reads.
    """

    NAME: ClassVar[str] = ""

    def __init__(self, cwd: str | None = None) -> None:
        self._cwd = cwd

    def name(self) -> str:
        return self.NAME

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        """Paths this call may change, for the undo snapshot to save first.

        Optional and duck-typed. An empty list means "nothing, or nothing
        knowable" — which for ``shell`` is the honest answer and the reason
        undo cannot cover what a command does.
        """
        return []

    def preview(self, arguments: dict[str, Any]) -> str:
        """What this call would do, shown before it is approved.

        Optional and duck-typed, like the I/O adapter's rendering hooks. The
        tools that change files override it; for the rest the arguments
        already say everything there is to say, and an empty string means the
        approval prompt shows nothing extra.

        Best-effort by contract: this runs *before* approval, so it must never
        raise and must never change anything.
        """
        return ""

    def _resolve(self, path: str) -> str:
        """Resolve ``path`` against this tool's configured working
        directory when it's relative. Without this, a relative path (which
        is what a model normally produces — "src/foo.py", not an absolute
        path) resolves against the *process's* actual working directory
        instead of whatever --cwd/session cwd the tool was configured
        with, silently reading/writing the wrong location whenever the two
        differ.

        ``~`` is expanded first, because it is neither absolute nor relative
        to anything: joined as-is it becomes a *directory literally named*
        ``~`` under the working directory, so "write it to ~/notes/x.md"
        quietly produced ``<cwd>/~/notes/x.md`` and reported success. The
        expansion has to happen before the ``isabs`` test, since ``~/x`` only
        becomes absolute once expanded.

        **``Policy._resolve`` resolves the same way and must keep doing so.**
        It exists to answer "where will this call actually land?" before the
        call is approved; the two drifting apart means a prompt naming one
        path and a write hitting another.
        """
        path = os.path.expanduser(path)
        if os.path.isabs(path):
            return path
        return os.path.join(self._cwd or ".", path)


def _syntax_note(path: str, content: str) -> str:
    """A warning when a write left a file that no longer parses, else ``""``.

    Checked in-process for the formats that can be checked without running
    anything — Python, JSON, TOML. The alternative is finding out several turns
    later, from a test run that fails for a reason the model has to rediscover,
    or not at all when nothing imports the file. It is a note, not a refusal:
    a half-written file mid-way through a multi-step change is legitimate.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".py":
            compile(content, path, "exec", dont_inherit=True)
        elif ext == ".json":
            json.loads(content)
        elif ext == ".toml":
            tomllib.loads(content)
        else:
            return ""
    except SyntaxError as exc:
        where = f"line {exc.lineno}" if exc.lineno else "somewhere"
        return f"\n\nWarning: {os.path.basename(path)} no longer parses as Python ({where}: {exc.msg})."
    except ValueError as exc:  # json.JSONDecodeError and tomllib.TOMLDecodeError both subclass it
        kind = "JSON" if ext == ".json" else "TOML"
        return f"\n\nWarning: {os.path.basename(path)} is no longer valid {kind} ({exc})."
    return ""


def _unified(path: str, old: str, new: str) -> str:
    """A unified diff of a pending change, for the approval prompt."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed)",
        n=3,
    )
    return _truncated("".join(diff), _MAX_PREVIEW_BYTES, "this diff")


def _as_int(value: Any, default: int) -> int:
    """A model-supplied number, or the default if it isn't one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truncated(text: str, limit: int, what: str) -> str:
    """``text`` cut to ``limit`` bytes, with a note saying so if it was."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    kept = encoded[:limit].decode("utf-8", errors="ignore")
    return (
        f"{kept}\n\n[truncated: {what} is {len(encoded)} bytes, showing the first "
        f"{len(kept.encode('utf-8'))}]"
    )
