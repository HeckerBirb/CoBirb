"""Built-in tools and the tool registry.

Tools are the "hands" of the agent. The core ships a registry of built-in tools;
third-party plugins extend it. The registry enforces a default-deny policy:
every tool must be explicitly allowed before it runs.
"""
from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from ...typing.spi import Tool, ToolResult
from .ignores import IgnoreRules
from .repomap import DEFAULT_BUDGET_CHARS, render_map


# --------------------------------------------------------------------------- #
# Tool base
# --------------------------------------------------------------------------- #
class CobirbTool(Tool):
    """Concrete base class for built-in tools.

    Subclasses declare their machine name as the ``NAME`` class attribute and
    inherit ``name()`` from here. The SPI declares ``name`` as a *method*, and
    the built-ins used to override it with a plain string instead — a
    different type than the interface promised, which meant every consumer had
    to branch on ``callable(tool.name)`` and the one that forgot serialized a
    bound method into a request payload. Declaring the value and the accessor
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
        """
        if os.path.isabs(path):
            return path
        return os.path.join(self._cwd or ".", path)


# --------------------------------------------------------------------------- #
# Built-in tools
# --------------------------------------------------------------------------- #
# A tool result is not just shown to the user: it becomes a turn in the
# encrypted session *and* a message in the next model request. So one *call*
# is bounded — a single read taking half the context window is rarely what
# anyone wanted, whatever the hardware allows.
#
# The *file* is not bounded. read_file pages: a call that stops early says
# which lines it returned and what offset continues from, so an arbitrarily
# large file can be read in full, in pieces the window can hold. An earlier
# version only truncated, which meant a large file could be read from the top
# and never finished.
_MAX_READ_BYTES = 256 * 1024
_MAX_GREP_MATCHES = 500
# A preview is for a human to read before saying yes; past a point a longer
# diff makes the decision harder rather than better informed.
_MAX_PREVIEW_BYTES = 8 * 1024


def _unified(path: str, old: str, new: str) -> str:
    """A unified diff of a pending change, for the approval prompt."""
    import difflib

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


def _read_range(path: str, offset: int, limit: int) -> tuple[list[str], int, bool]:
    """Read from line ``offset``, stopping at ``limit`` lines or the byte cap.

    Returns ``(lines, next_offset, at_end)``. Streamed rather than read whole
    and sliced, so asking for line 40,000 of a very large file costs the
    memory of the lines returned rather than of the file.

    Paginating rather than simply truncating is the point. A cap on its own
    meant a large file could only ever be read from the top and never
    finished — the agent got the first 256 KB and had no way to ask for the
    rest, which is not a "large file" limitation but a missing feature.
    """
    lines: list[str] = []
    collected_bytes = 0
    line_number = 0
    at_end = True
    with open(path, "r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            if line_number < offset:
                continue
            if limit > 0 and len(lines) >= limit:
                at_end = False
                break
            if collected_bytes + len(line) > _MAX_READ_BYTES:
                if lines:
                    at_end = False
                    break
                # One line, on its own, larger than the whole budget: a
                # minified bundle or single-line JSON. Line offsets cannot
                # page through that, so it is cut and said so — reading such
                # a file whole into a model is rarely the useful thing
                # anyway, and grep or shell are better tools for it.
                lines.append(
                    line[:_MAX_READ_BYTES]
                    + f"\n[line {line_number} is {len(line)} characters and was cut here; "
                    "line offsets cannot page within a single line]"
                )
                at_end = False
                break
            lines.append(line)
            collected_bytes += len(line)
    next_offset = offset + len(lines)
    return lines, next_offset, at_end


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


class ReadFileTool(CobirbTool):
    NAME = "read_file"

    def description(self) -> str:
        return (
            "Read a file. Returns the whole file unless it is large, in which case it "
            "returns a range of lines and says how to ask for the next one."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to read."},
                "offset": {
                    "type": "number",
                    "description": "First line to return, 1-indexed. Use this to continue a "
                    "read that reported more lines follow.",
                    "default": 1,
                },
                "limit": {
                    "type": "number",
                    "description": "Maximum number of lines to return. Omit for as many as fit.",
                },
            },
            "required": ["path"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments["path"])
        offset = max(1, _as_int(arguments.get("offset"), 1))
        limit = _as_int(arguments.get("limit"), 0)

        try:
            lines, next_offset, at_end = _read_range(path, offset, limit)
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not read {path}: {exc}", error=str(exc))

        body = "".join(lines)
        if offset == 1 and at_end:
            return ToolResult(ok=True, content=body)  # the whole file, unadorned

        last = next_offset - 1
        note = f"[lines {offset}-{last} of {path}"
        if at_end:
            note += "; end of file]"
        else:
            note += f"; more follow — call read_file with offset={next_offset} to continue]"
        return ToolResult(
            ok=True,
            content=f"{body}\n{note}",
            meta={"offset": offset, "next_offset": next_offset, "at_end": at_end},
        )


class WriteFileTool(CobirbTool):
    NAME = "write_file"

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        path = arguments.get("path")
        return [self._resolve(str(path))] if path else []

    def preview(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("path", "")))
        new = str(arguments.get("content", ""))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = fh.read()
        except OSError:
            lines = new.count("\n") + 1
            return f"new file: {path} ({lines} line{'s' if lines != 1 else ''})"
        return _unified(path, old, new) or f"no change to {path}"

    def description(self) -> str:
        return "Create or overwrite a file at a path with the given content."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target path."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path, content = self._resolve(arguments["path"]), arguments["content"]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return ToolResult(ok=True, content=f"Wrote {len(content)} bytes to {path}")
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not write {path}: {exc}", error=str(exc))


class EditFileTool(CobirbTool):
    NAME = "edit_file"

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        path = arguments.get("path")
        return [self._resolve(str(path))] if path else []

    def preview(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("path", "")))
        old_str, new_str = str(arguments.get("old_str", "")), str(arguments.get("new_str", ""))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = fh.read()
        except OSError as exc:
            return f"cannot read {path}: {exc}"
        if old_str not in old:
            return f"old_str does not appear in {path} — this edit will fail"
        return _unified(path, old, old.replace(old_str, new_str, 1)) or f"no change to {path}"

    def description(self) -> str:
        return "Replace a region of a file: old_str must match exactly, new_str is the replacement."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target path."},
                "old_str": {"type": "string", "description": "Exact literal text to replace."},
                "new_str": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_str", "new_str"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments["path"])
        old_str, new_str = arguments["old_str"], arguments["new_str"]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            if old_str not in content:
                return ToolResult(
                    ok=False,
                    content=f"'{old_str!r}' not found in {path}.",
                    error="no_match",
                )
            content = content.replace(old_str, new_str, 1)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return ToolResult(ok=True, content=f"Edited {path}")
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not edit {path}: {exc}", error=str(exc))


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def _parse_patch_hunks(patch_text: str) -> list[tuple[int, list[tuple[str, str]]]]:
    """Parse unified-diff hunks into ``(old_start, [(marker, line), ...])``.

    ``old_start`` is the 1-indexed starting line from the hunk header
    (``@@ -old_start,old_len +new_start,new_len @@``). ``marker`` is one of
    ``' '`` (context), ``'-'`` (removed), or ``'+'`` (added). File header
    lines (``---``/``+++``) and "\\ No newline at end of file" markers are
    skipped.
    """
    hunks: list[tuple[int, list[tuple[str, str]]]] = []
    lines = patch_text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith(("---", "+++")):
            i += 1
            continue
        match = _HUNK_HEADER_RE.match(lines[i])
        if not match:
            i += 1
            continue
        old_start = int(match.group(1))
        i += 1
        body: list[tuple[str, str]] = []
        while i < len(lines) and not lines[i].startswith("@@"):
            hunk_line = lines[i]
            if hunk_line.startswith(("+", "-", " ")):
                body.append((hunk_line[0], hunk_line[1:]))
            elif hunk_line != "\\ No newline at end of file":
                # Some diff generators emit a bare empty line for a blank
                # context line, dropping the leading space marker.
                body.append((" ", hunk_line))
            i += 1
        hunks.append((old_start, body))
    return hunks


def _apply_patch_hunks(original_lines: list[str], hunks: list[tuple[int, list[tuple[str, str]]]]) -> list[str]:
    """Apply parsed hunks (in order) to ``original_lines`` and return the
    patched lines. Raises ``ValueError`` if a hunk's context/removed lines
    don't match the file at the position its header claims, rather than
    guessing and silently corrupting the file.
    """
    result: list[str] = []
    cursor = 0
    for old_start, body in hunks:
        start_idx = old_start - 1
        if start_idx < cursor:
            raise ValueError(f"hunk at line {old_start} overlaps a previous hunk")
        result.extend(original_lines[cursor:start_idx])
        cursor = start_idx
        for marker, text in body:
            if marker in (" ", "-"):
                if cursor >= len(original_lines) or original_lines[cursor] != text:
                    found = original_lines[cursor] if cursor < len(original_lines) else "<end of file>"
                    raise ValueError(f"patch does not apply at line {cursor + 1}: expected {text!r}, found {found!r}")
                if marker == " ":
                    result.append(text)
                cursor += 1
            else:  # "+"
                result.append(text)
    result.extend(original_lines[cursor:])
    return result


class ApplyPatchTool(CobirbTool):
    NAME = "apply_patch"

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        path = arguments.get("path")
        return [self._resolve(str(path))] if path else []

    def preview(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("patch", ""))

    def description(self) -> str:
        return "Apply a structured diff patch to a file (unified diff format)."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target path."},
                "patch": {"type": "string", "description": "Unified diff text."},
            },
            "required": ["path", "patch"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path, patch = self._resolve(arguments["path"]), arguments["patch"]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read()
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not patch {path}: {exc}", error=str(exc))

        hunks = _parse_patch_hunks(patch)
        if not hunks:
            return ToolResult(ok=False, content="No valid hunks found in patch.", error="no_hunks")

        original_lines = original.splitlines()
        try:
            new_lines = _apply_patch_hunks(original_lines, hunks)
        except ValueError as exc:
            return ToolResult(ok=False, content=f"Could not apply patch to {path}: {exc}", error=str(exc))

        content = "\n".join(new_lines)
        if new_lines and original.endswith("\n"):
            content += "\n"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not patch {path}: {exc}", error=str(exc))
        return ToolResult(ok=True, content=f"Applied patch to {path}")


def _ignore_rules(tool: "CobirbTool") -> IgnoreRules:
    """The ignore rules for this tool's working directory.

    Built per call rather than cached: it is one small file read, and a
    long-running session should notice a `.gitignore` the agent itself just
    edited rather than working from a stale copy of it.
    """
    return IgnoreRules.for_directory(tool._cwd or ".")


class GlobTool(CobirbTool):
    NAME = "glob"

    def description(self) -> str:
        return "Find files matching a glob pattern (searches from the working directory)."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. 'src/**/*.py'."},
                "include_ignored": {
                    "type": "boolean",
                    "description": (
                        "Include files that .gitignore, or CoBirb's built-in list of "
                        "vendor and cache directories, would otherwise skip."
                    ),
                    "default": False,
                },
            },
            "required": ["pattern"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        import glob as glob_module

        pattern = self._resolve(arguments["pattern"])
        include_ignored = arguments.get("include_ignored", False)
        try:
            results = glob_module.glob(pattern, recursive=True)
            if not include_ignored:
                rules = _ignore_rules(self)
                results = [r for r in results if not rules.is_ignored(r)]
            return ToolResult(ok=True, content="\n".join(sorted(results)) if results else "(no matches)")
        except OSError as exc:
            return ToolResult(ok=False, content=f"glob failed: {exc}", error=str(exc))


class GrepTool(CobirbTool):
    NAME = "grep"

    def description(self) -> str:
        return "Search file contents for a regex pattern."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {"type": "string", "description": "Optional path/directory to restrict search."},
                "include_ignored": {
                    "type": "boolean",
                    "description": (
                        "Include files that .gitignore, or CoBirb's built-in list of "
                        "vendor and cache directories, would otherwise skip."
                    ),
                    "default": False,
                },
            },
            "required": ["pattern"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        import glob as glob_module
        import os

        pattern = arguments["pattern"]
        include_ignored = arguments.get("include_ignored", False)
        try:
            root = self._resolve(arguments.get("path", "."))
            paths = [p for p in glob_module.glob(root + "/**/*", recursive=True)]
            if not include_ignored:
                rules = _ignore_rules(self)
                paths = [p for p in paths if not rules.is_ignored(p)]
            # Compiled once, up front, so an invalid pattern is reported as
            # what it is. Left to re.search it would raise re.error on the
            # first line of the first file, escape this handler (which only
            # catches OSError), and reach the model as a generic tool failure
            # with nothing actionable in it.
            try:
                expression = re.compile(pattern)
            except re.error as exc:
                return ToolResult(
                    ok=False, content=f"Not a valid regex: {pattern!r} — {exc}", error="bad_pattern"
                )
            matches: list[str] = []
            capped = False
            for p in paths:
                if capped:
                    break
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if expression.search(line):
                                matches.append(f"{p}:{lineno}: {line.strip()}")
                                if len(matches) >= _MAX_GREP_MATCHES:
                                    capped = True
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
            if not matches:
                return ToolResult(ok=True, content="(no matches)")
            content = "\n".join(matches)
            if capped:
                content += f"\n\n[stopped at {_MAX_GREP_MATCHES} matches; narrow the pattern or path]"
            return ToolResult(ok=True, content=_truncated(content, _MAX_READ_BYTES, "this result"))
        except OSError as exc:
            return ToolResult(ok=False, content=f"grep failed: {exc}", error=str(exc))


class ListDirTool(CobirbTool):
    NAME = "list_dir"

    def description(self) -> str:
        return "List the contents of a directory."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path."},
            },
            "required": ["path"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments["path"])
        try:
            entries = sorted(os.listdir(path))
            return ToolResult(ok=True, content="\n".join(entries))
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not list {path}: {exc}", error=str(exc))


_DEFAULT_SHELL_TIMEOUT = 300
# A ceiling as well as a default. The timeout used to be read straight out of
# the arguments with no declared parameter and no bound, so it was invisible
# to the model that might set it and unbounded if one guessed at it — a large
# enough value would have made a hung command effectively unkillable except
# by cancelling the turn.
_MAX_SHELL_TIMEOUT = 600


def _shell_timeout(value: Any) -> float:
    """Clamp a caller-supplied timeout into something survivable, falling
    back to the default for anything that isn't a usable number."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(_DEFAULT_SHELL_TIMEOUT)
    if seconds != seconds or seconds <= 0:  # NaN or nonsense
        return float(_DEFAULT_SHELL_TIMEOUT)
    return min(seconds, float(_MAX_SHELL_TIMEOUT))


class RepoMapTool(CobirbTool):
    """A ranked outline of the codebase, for orientation.

    One is already in the system prompt at session start; this is for asking
    about a subtree, or for refreshing after the layout has changed under the
    agent's own hands.
    """

    NAME = "repo_map"

    def description(self) -> str:
        return (
            "Outline the codebase: which files matter and what is defined in them, "
            "most-referenced first. Use this to find your way around before searching."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to map. Defaults to the working directory.",
                    "default": ".",
                },
                "budget_chars": {
                    "type": "number",
                    "description": (
                        f"Roughly how much outline to return "
                        f"(default {DEFAULT_BUDGET_CHARS}). Raise it for a large repository."
                    ),
                    "default": DEFAULT_BUDGET_CHARS,
                },
            },
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        root = self._resolve(str(arguments.get("path") or "."))
        try:
            budget = int(arguments.get("budget_chars") or DEFAULT_BUDGET_CHARS)
        except (TypeError, ValueError):
            budget = DEFAULT_BUDGET_CHARS
        budget = max(500, min(budget, _MAX_READ_BYTES))
        if not os.path.isdir(root):
            return ToolResult(ok=False, content=f"Not a directory: {root}", error="not_a_directory")
        try:
            return ToolResult(ok=True, content=render_map(root, budget))
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not map {root}: {exc}", error=str(exc))


class ShellTool(CobirbTool):
    """Highest-privilege tool. Gated behind the permission layer.

    The permission layer can match on the first word after splitting on shell
    separators (`; | && &`), so `bash -n` can be allowed without allowing
    `bash` — the motivation being that a bare binary is often too broad to
    trust when a specific invocation of it is perfectly safe.

    Runs the command in its own process group (POSIX; a plain child on other
    platforms) rather than sharing the caller's, so a command that
    backgrounds or forks something long-running (``python game.py &``, a
    server, anything that doesn't exit on its own) can be torn down as a
    whole — by ``timeout`` below, or by ``cancel_running()`` — instead of
    leaving that descendant running as an orphan once the immediate shell
    process is gone.
    """

    NAME = "shell"

    def __init__(self, cwd: str | None = None) -> None:
        super().__init__(cwd)
        # Set only while a call is actually in flight; read from another
        # thread by cancel_running() (the TUI's Ctrl+C / quit-while-running
        # handling — see tui/app.py). Tool calls run one at a time on the
        # orchestrator's own thread, so there is never more than one to track.
        self._current_process: Any = None
        self._cancel_requested = False

    def description(self) -> str:
        return "Execute a shell command. Requires explicit approval."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Seconds to wait before killing the command "
                        f"(default {_DEFAULT_SHELL_TIMEOUT}, maximum {_MAX_SHELL_TIMEOUT})."
                    ),
                    "default": _DEFAULT_SHELL_TIMEOUT,
                },
            },
            "required": ["command"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        import subprocess

        command = arguments["command"]
        timeout = _shell_timeout(arguments.get("timeout"))
        self._cancel_requested = False
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=self._cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except Exception as exc:  # noqa: BLE001 - defensive
            return ToolResult(ok=False, content=f"Command failed: {exc}", error=str(exc))

        self._current_process = process
        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill(process)
                stdout, stderr = process.communicate()
        finally:
            self._current_process = None

        # Cancellation takes priority even if it happened to race with the
        # timeout firing at the same moment — the user's explicit action is
        # the more informative thing to report.
        if self._cancel_requested:
            return ToolResult(ok=False, content="Command cancelled.", error="cancelled")
        if timed_out:
            return ToolResult(ok=False, content="Command timed out.", error="timeout")
        content = f"exit={process.returncode}\n{stdout}{stderr}"
        return ToolResult(ok=process.returncode == 0, content=content, meta={"returncode": process.returncode})

    @staticmethod
    def _kill(process: Any) -> None:
        """Kill ``process`` and, on POSIX, everything in its process group —
        see the class docstring for why a plain ``process.kill()`` isn't
        enough for a command that backgrounds or forks."""
        import signal

        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass  # already gone — nothing to do

    def cancel_running(self) -> bool:
        """Stop the in-flight command, if any. Returns whether there was
        anything to cancel.

        This is what lets the TUI's Ctrl+C (or quitting while a turn is
        running) actually interrupt a stuck or merely slow shell call —
        without it, the ``timeout`` above is the only way out, and the
        whole app (including quitting it) blocks until either the command
        finishes or that timeout elapses.
        """
        process = self._current_process
        if process is None or process.poll() is not None:
            return False
        self._cancel_requested = True
        self._kill(process)
        return True


# Registry of built-in tools.
BUILTIN_TOOLS: list[type[Tool]] = [
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ApplyPatchTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    RepoMapTool,
    ShellTool,
]


class ToolRegistry:
    """Holds the built-in tools and supports plugin extension."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self._tools: dict[str, Tool] = {}
        for tool_cls in BUILTIN_TOOLS:
            self.register(tool_cls(self.cwd))

    def register(self, tool: Tool) -> None:
        """Register ``tool`` under the name it reports.

        Validates here rather than tolerating a non-conformant tool, because
        tolerance is what let a bound method reach a JSON payload once
        already: a tool whose ``name`` isn't the method the SPI documents is
        rejected at the boundary with a message that says so, and the caller
        (see ``cli._merge_tool_plugins``) reports and skips it rather than
        letting it break a turn much later.
        """
        if not callable(tool.name):
            raise TypeError(
                f"{type(tool).__name__}.name must be a method returning a string, "
                "as the Tool interface declares — not a plain attribute"
            )
        self._tools[tool.name()] = tool

    @property
    def tools(self) -> dict[str, Tool]:
        """The live name -> tool mapping the orchestrator dispatches against.

        Public because the orchestrator is constructed with it; callers had
        been reaching into ``registry._tools`` to get the same object.
        """
        return self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def values(self) -> list[Tool]:
        """Return the registered tools in registration order."""
        return list(self._tools.values())

    def is_known(self, name: str) -> bool:
        return name in self._tools
