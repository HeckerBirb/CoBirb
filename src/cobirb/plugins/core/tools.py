"""Built-in tools and the tool registry.

Tools are the "hands" of the agent. The core ships a registry of built-in tools;
third-party plugins extend it. The registry enforces a default-deny policy:
every tool must be explicitly allowed before it runs.
"""
from __future__ import annotations

import os
import re
from typing import Any, ClassVar, Iterator

from ... import patches
from ...policy import patch_target
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
# large file can be read in full, in pieces the window can hold. Truncating
# alone would leave a large file readable from the top and never finishable.
_MAX_READ_BYTES = 256 * 1024
_MAX_GREP_MATCHES = 500

# Entries returned by one list_dir or glob call. A directory with a hundred
# thousand files is not rare — node_modules, a build output, a mail spool —
# and dumping all of them was the same unbounded-result bug read_file had,
# without even a truncation message.
_MAX_LIST_ENTRIES = 1000

# One matching line, clipped. A grep hit in a minified bundle is a single line
# of megabytes; the useful part is that it matched and where.
_MAX_MATCH_CHARS = 300

# Bytes of combined stdout and stderr one shell call may return. Both ends are
# kept when it overflows: the head is what the command set out to say and the
# tail is usually where it went wrong, and losing either makes the other much
# harder to act on.
_MAX_SHELL_OUTPUT = 64 * 1024
# A preview is for a human to read before saying yes; past a point a longer
# diff makes the decision harder rather than better informed.
_MAX_PREVIEW_BYTES = 8 * 1024


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
            import json

            json.loads(content)
        elif ext == ".toml":
            import tomllib

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
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed)",
        n=3,
    )
    return _truncated("".join(diff), _MAX_PREVIEW_BYTES, "this diff")


def _page(items: list[str], offset: int, limit: int, what: str, path: str) -> ToolResult:
    """One page of a long list, with the offset that continues it.

    Shared by ``list_dir`` and ``glob`` so paging feels the same wherever the
    agent meets it — and the same as ``read_file``'s, which is the one it will
    meet first.
    """
    total = len(items)
    start = max(0, offset - 1)
    end = total if limit <= 0 else min(total, start + limit)
    end = min(end, start + _MAX_LIST_ENTRIES)
    page = items[start:end]

    if not page:
        return ToolResult(
            ok=True,
            content=f"(no {what} at offset {offset}; {path} has {total})",
            meta={"total": total, "at_end": True},
        )
    body = "\n".join(page)
    if end >= total:
        if start == 0:
            return ToolResult(ok=True, content=body, meta={"total": total, "at_end": True})
        return ToolResult(
            ok=True,
            content=f"{body}\n[{what} {offset}-{end} of {total}; end of list]",
            meta={"total": total, "next_offset": end + 1, "at_end": True},
        )
    return ToolResult(
        ok=True,
        content=(
            f"{body}\n[{what} {offset}-{end} of {total}; more follow — "
            f"call again with offset={end + 1}]"
        ),
        meta={"total": total, "next_offset": end + 1, "at_end": False},
    )


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


def _clip(line: str, limit: int = _MAX_MATCH_CHARS) -> str:
    """One matching line, cut to something readable.

    A match in a minified bundle is a single line of megabytes. Nobody — model
    or person — reads that, and storing it whole to truncate later is how a
    grep over a large tree turns into gigabytes of memory.
    """
    return line if len(line) <= limit else line[:limit] + f"…[+{len(line) - limit} chars]"


def _walk_files(root: str, rules: "IgnoreRules | None") -> "Iterator[str]":
    """Every file under ``root``, skipping ignored directories entirely.

    ``glob("**/*")`` builds a list of every path first and filters afterwards,
    so a tree with a large `node_modules` is fully enumerated before any of it
    is discarded. Pruning during the walk means those directories are never
    descended into at all, and nothing holds the whole tree in memory.
    """
    if os.path.isfile(root):
        yield root
        return
    for directory, subdirectories, filenames in os.walk(root):
        if rules is not None:
            subdirectories[:] = [
                name for name in subdirectories
                if not rules.is_ignored(os.path.join(directory, name), is_dir=True)
            ]
        for filename in sorted(filenames):
            path = os.path.join(directory, filename)
            if rules is None or not rules.is_ignored(path, is_dir=False):
                yield path


def _both_ends(text: str, limit: int) -> str:
    """Keep the start and the end of an overlong output, dropping the middle.

    Neither end alone is enough for command output: the head is what the
    command set out to say and the tail is usually where it went wrong, and a
    build log truncated to its first half hides the error that matters.
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - (half * 2)
    return f"{text[:half]}\n[…{dropped} characters of output omitted…]\n{text[-half:]}"


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
        return (
            "Create a file, or replace a file's entire content. Missing parent directories are "
            "created. Use this for new files and for rewriting a short file completely; to "
            "change part of an existing file, use edit_file."
        )

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
            return ToolResult(
                ok=True, content=f"Wrote {len(content)} bytes to {path}{_syntax_note(path, content)}"
            )
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not write {path}: {exc}", error=str(exc))


class EditFileTool(CobirbTool):
    """Replace one exact region of a file.

    The contract is *one* region. ``old_str`` matching several places used to
    edit the first of them and report success — which, in a file of similar
    functions, is a different function from the one the model meant, with the
    model then telling the user it had done what was asked. An ambiguous match
    is now refused with the line of every candidate, and ``replace_all`` says
    out loud when every one is meant.

    A near miss is the other common failure: the model reproduces the region
    with different trailing whitespace, line endings or indentation. A unique
    whitespace-tolerant match is applied (re-indenting ``new_str`` by the same
    offset); a true miss quotes the closest region with line numbers, because
    "not found" on its own leaves a small model guessing.
    """

    NAME = "edit_file"

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        path = arguments.get("path")
        return [self._resolve(str(path))] if path else []

    def preview(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("path", "")))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = fh.read()
        except OSError as exc:
            return f"cannot read {path}: {exc}"
        edit = _plan_edit(old, arguments)
        if edit.content is None:
            return f"this edit will fail: {edit.message}"
        return _unified(path, old, edit.content) or f"no change to {path}"

    def description(self) -> str:
        return (
            "Replace one region of a file. old_str must match exactly one place: copy it from "
            "read_file output, with a few lines of context if the same text appears more than "
            "once. Set replace_all to change every occurrence. To create a file or rewrite a "
            "small one completely, use write_file instead."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old_str": {"type": "string", "description": "The exact text to replace."},
                "new_str": {"type": "string", "description": "What to put in its place."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring exactly one.",
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments["path"])
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not edit {path}: {exc}", error=str(exc))
        edit = _plan_edit(content, arguments)
        if edit.content is None:
            return ToolResult(ok=False, content=f"{path}: {edit.message}", error=edit.error)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(edit.content)
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not edit {path}: {exc}", error=str(exc))
        return ToolResult(ok=True, content=f"Edited {path}{edit.message}{_syntax_note(path, edit.content)}")


class _EditPlan:
    """What an edit would do: the new content (``None`` if refused) and a message."""

    def __init__(self, content: "str | None", message: str, error: str = "") -> None:
        self.content, self.message, self.error = content, message, error


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _numbered(lines: list[str], first: int, limit: int = 12) -> str:
    shown = lines[:limit]
    body = "\n".join(f"{first + i:>5}  {line.rstrip()}" for i, line in enumerate(shown))
    more = f"\n       … {len(lines) - limit} more line(s)" if len(lines) > limit else ""
    return body + more


def _plan_edit(content: str, arguments: dict[str, Any]) -> _EditPlan:
    old, new = str(arguments.get("old_str", "")), str(arguments.get("new_str", ""))
    if not old:
        return _EditPlan(None, "old_str is empty; say which text to replace.", "empty")
    starts = [m.start() for m in re.finditer(re.escape(old), content)]
    loose = False
    if not starts:
        found = _loose_match(content, old, new)
        if found is None:
            return _EditPlan(None, _miss_message(content, old), "no_match")
        (begin, end, new), loose = found, True
        starts = [begin]
    else:
        end = starts[0] + len(old)

    if len(starts) > 1 and not arguments.get("replace_all"):
        where = ", ".join(str(_line_of(content, s)) for s in starts[:12])
        return _EditPlan(
            None,
            f"old_str matches {len(starts)} places (lines {where}). Include enough surrounding "
            "lines to match exactly one, or set replace_all to change every one.",
            "ambiguous",
        )
    if len(starts) > 1:
        updated = content.replace(old, new)
        return _EditPlan(updated, f": replaced all {len(starts)} occurrences.")

    begin = starts[0]
    updated = content[:begin] + new + content[end:]
    first = _line_of(updated, begin)
    region = updated[begin:begin + len(new)].splitlines() or [""]
    note = " (matched ignoring whitespace differences)" if loose else ""
    return _EditPlan(updated, f"{note}. Lines {first}–{first + len(region) - 1} now read:\n"
                     + _numbered(region, first))


def _loose_match(content: str, old: str, new: str) -> "tuple[int, int, str] | None":
    """A unique match ignoring trailing whitespace, CRLF and a missing indent.

    Returns ``(start, end, new_str re-indented)`` or ``None``. Whole lines
    only — a fragment that matches loosely is too easily the wrong fragment —
    and the indent may only be *missing* from ``old_str`` by the same amount on
    every line, which is the shape of a model that dropped a level of nesting
    when it copied the region. That same amount is added back to ``new_str``.
    """
    old_lines = [line.rstrip() for line in old.strip("\n").splitlines()]
    if not any(line.strip() for line in old_lines):
        return None
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    n = len(old_lines)
    matches: list[tuple[int, str]] = []
    for i in range(len(lines) - n + 1):
        prefix = _common_extra_indent(lines[i:i + n], old_lines)
        if prefix is not None:
            matches.append((i, prefix))
    if len(matches) != 1:
        return None
    i, prefix = matches[0]
    last = lines[i + n - 1]
    end = offsets[i + n] - (len(last) - len(last.rstrip("\r\n")))
    new_lines = new.strip("\n").split("\n")
    reindented = "\n".join(prefix + line if line.strip() else line for line in new_lines)
    return offsets[i], end, reindented


def _common_extra_indent(have_lines: list[str], want_lines: list[str]) -> "str | None":
    """The indent every ``have`` line carries beyond its ``want`` line, when
    the text is otherwise equal and that extra indent is the same throughout."""
    prefix: "str | None" = None
    for have, want in zip(have_lines, want_lines):
        have = have.rstrip()
        if not want.strip():
            if have.strip():
                return None
            continue
        if have.lstrip() != want.lstrip():
            return None
        have_indent = have[: len(have) - len(have.lstrip())]
        want_indent = want[: len(want) - len(want.lstrip())]
        if not have_indent.startswith(want_indent):
            return None
        extra = have_indent[len(want_indent):]
        if prefix is None:
            prefix = extra
        elif extra != prefix:
            return None
    return prefix


def _miss_message(content: str, old: str) -> str:
    """"Not found", plus the closest region so the model can copy it exactly."""
    import difflib

    lines = content.splitlines()
    size = max(1, len(old.strip("\n").splitlines()))
    best, best_ratio = -1, 0.0
    target = old.strip()
    for i in range(0, max(1, len(lines) - size + 1)):
        window = "\n".join(lines[i:i + size])
        matcher = difflib.SequenceMatcher(None, window, target, autojunk=False)
        if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = i, ratio
    message = "old_str was not found."
    if best >= 0 and best_ratio >= 0.5:
        message += (
            f" The closest text is at line {best + 1} — copy it exactly, whitespace included:\n"
            + _numbered(lines[best:best + size], best + 1)
        )
    else:
        message += " Read the file again and copy the region exactly."
    return message


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


def _apply_context_hunks(lines: list[str], hunks: list[tuple[str, list[tuple[str, str]]]]) -> list[str]:
    """Apply hunks located by their context rather than by line numbers.

    Each hunk's old lines (context and removals) must appear at or after the
    previous hunk; an ``@@`` anchor narrows the search to after the first line
    containing it. Without an anchor, a block that matches more than once is
    refused, for the same reason ``edit_file`` refuses an ambiguous edit.
    Trailing whitespace is forgiven when the exact text is not found.
    """
    result = list(lines)
    cursor = 0
    for anchor, body in hunks:
        old = [text for marker, text in body if marker in (" ", "-")]
        new = [text for marker, text in body if marker in (" ", "+")]
        start = cursor
        if anchor:
            hits = [i for i in range(cursor, len(result)) if anchor in result[i]]
            if not hits:
                raise ValueError(f"the @@ anchor {anchor!r} was not found")
            start = hits[0]
        if not old:
            raise ValueError("a hunk with no context or removed lines cannot be placed; include the line it follows")

        def matches(loose: bool) -> list[int]:
            same = (lambda a, b: a.rstrip() == b.rstrip()) if loose else (lambda a, b: a == b)
            return [i for i in range(start, len(result) - len(old) + 1)
                    if all(same(result[i + k], old[k]) for k in range(len(old)))]

        found = matches(False) or matches(True)
        if not found:
            raise ValueError(f"these lines were not found in the file: {old[0]!r}…")
        if len(found) > 1 and not anchor:
            where = ", ".join(str(i + 1) for i in found[:8])
            raise ValueError(f"the hunk starting {old[0]!r} matches {len(found)} places (lines {where}); "
                             "add context or an @@ anchor naming the enclosing function")
        at = found[0]
        result[at:at + len(old)] = new
        cursor = at + len(new)
    return result


class ApplyPatchTool(CobirbTool):
    """Unified diffs, and the ``*** Begin Patch`` format (``cobirb.patches``).

    Where a Begin-Patch writes is read from the patch itself, through the same
    ``policy.patch_target`` the permission check uses — so the file that was
    approved is the file that is written.
    """

    NAME = "apply_patch"

    def _target(self, arguments: dict[str, Any]) -> str | None:
        if patches.is_begin_patch(arguments.get("patch")):
            return patch_target(arguments)
        path = arguments.get("path")
        return str(path) if path else None

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        target = self._target(arguments)
        return [self._resolve(target)] if target else []

    def preview(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("patch", ""))

    def description(self) -> str:
        return (
            "Apply a patch to one file: a unified diff (with @@ hunk headers) plus path, or the "
            "'*** Begin Patch' / '*** Update File: <path>' format, which names the file itself. "
            "Useful for several separate changes to the same file at once; for a single change, "
            "edit_file is simpler. One file per call."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to patch (optional when the patch names it)."},
                "patch": {"type": "string", "description": "Unified diff, or '*** Begin Patch' text."},
            },
            "required": ["patch"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        patch = str(arguments["patch"])
        target = self._target(arguments)
        if target is None:
            if patches.is_begin_patch(patch):
                return ToolResult(
                    ok=False, error="patch_scope",
                    content="This patch touches more than one file, moves or deletes one, or names a "
                            "different file than 'path'. Send one *** Update File (or *** Add File) "
                            "section per call.",
                )
            return ToolResult(ok=False, content="apply_patch needs a path for a unified diff.", error="no_path")
        path = self._resolve(target)

        if patches.is_begin_patch(patch):
            section = patches.parse(patch)[0]
            if section.op == "add":
                if os.path.exists(path):
                    return ToolResult(ok=False, error="exists",
                                      content=f"{path} already exists; use *** Update File to change it.")
                content = "\n".join(section.lines) + "\n"
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(content)
                except OSError as exc:
                    return ToolResult(ok=False, content=f"Could not create {path}: {exc}", error=str(exc))
                return ToolResult(ok=True, content=f"Created {path}{_syntax_note(path, content)}")

        try:
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read()
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not patch {path}: {exc}", error=str(exc))

        original_lines = original.splitlines()
        try:
            if patches.is_begin_patch(patch):
                new_lines = _apply_context_hunks(original_lines, patches.parse(patch)[0].hunks)
            else:
                hunks = _parse_patch_hunks(patch)
                if hunks:
                    new_lines = _apply_patch_hunks(original_lines, hunks)
                else:
                    # A diff with bare "@@" headers and no line numbers — the
                    # shape a model writes when it knows the change but not
                    # the numbers. Placed by context, like a Begin-Patch.
                    bare = patches.parse(f"*** Update File: {target}\n{patch}")
                    if not bare or not bare[0].hunks:
                        return ToolResult(ok=False, content="No valid hunks found in patch.", error="no_hunks")
                    new_lines = _apply_context_hunks(original_lines, bare[0].hunks)
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
        return ToolResult(ok=True, content=f"Applied patch to {path}{_syntax_note(path, content)}")


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
        return (
            "Find files by name pattern, e.g. '**/*.py' or 'tests/test_*.py', relative to the "
            "working directory. Returns matching paths. Use grep to search inside files."
        )

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
                "offset": {
                    "type": "number",
                    "description": "First match to return, 1-indexed, for continuing a long list.",
                    "default": 1,
                },
                "limit": {"type": "number", "description": "Maximum matches to return."},
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
            if not results:
                return ToolResult(ok=True, content="(no matches)", meta={"total": 0, "at_end": True})
            return _page(
                sorted(results), max(1, _as_int(arguments.get("offset"), 1)),
                _as_int(arguments.get("limit"), 0), "matches", pattern,
            )
        except OSError as exc:
            return ToolResult(ok=False, content=f"glob failed: {exc}", error=str(exc))


class GrepTool(CobirbTool):
    NAME = "grep"

    def description(self) -> str:
        return (
            "Search inside files for a regular expression. Returns path:line: text for each "
            "matching line. Use it to find where something is defined or used before reading "
            "or editing — e.g. every caller of a function you are about to rename."
        )

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
                "offset": {
                    "type": "number",
                    "description": "First match to return, 1-indexed, for continuing a long list.",
                    "default": 1,
                },
                "limit": {"type": "number", "description": "Maximum matches to return."},
            },
            "required": ["pattern"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        pattern = arguments["pattern"]
        include_ignored = arguments.get("include_ignored", False)
        root = self._resolve(arguments.get("path", "."))

        # Compiled once, up front, so an invalid pattern is reported as what
        # it is. Left to re.search it would raise re.error on the first line
        # of the first file, escape the handler below (which only catches
        # OSError), and reach the model as a generic tool failure with
        # nothing actionable in it.
        try:
            expression = re.compile(pattern)
        except re.error as exc:
            return ToolResult(
                ok=False, content=f"Not a valid regex: {pattern!r} — {exc}", error="bad_pattern"
            )

        rules = None if include_ignored else _ignore_rules(self)
        matches: list[str] = []
        capped = False
        try:
            for path in _walk_files(root, rules):
                if capped:
                    break
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if not expression.search(line):
                                continue
                            # Clipped as it is stored, not at the end. A
                            # minified file is one line of several megabytes;
                            # keeping five hundred of those and truncating
                            # afterwards would mean holding gigabytes to
                            # produce a few kilobytes of answer.
                            matches.append(f"{path}:{lineno}: {_clip(line.strip())}")
                            if len(matches) >= _MAX_GREP_MATCHES:
                                capped = True
                                break
                except (OSError, UnicodeDecodeError):
                    continue
        except OSError as exc:
            return ToolResult(ok=False, content=f"grep failed: {exc}", error=str(exc))

        if not matches:
            return ToolResult(ok=True, content="(no matches)")
        content = "\n".join(matches)
        if capped:
            content += f"\n\n[stopped at {_MAX_GREP_MATCHES} matches; narrow the pattern or path]"
        return ToolResult(ok=True, content=_truncated(content, _MAX_READ_BYTES, "this result"))


class ListDirTool(CobirbTool):
    NAME = "list_dir"

    def description(self) -> str:
        return (
            "List what is in one directory (not recursive). Directories end with '/'. Use glob "
            "to find files by pattern across the tree."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path. Defaults to the working directory.",
                },
                "offset": {
                    "type": "number",
                    "description": "First entry to return, 1-indexed, for continuing a long listing.",
                    "default": 1,
                },
                "limit": {"type": "number", "description": "Maximum entries to return."},
            },
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(str(arguments.get("path") or "."))
        try:
            # scandir rather than listdir: it reports whether each entry is a
            # directory without a stat() per name, which on a directory of a
            # hundred thousand files is the difference between instant and not.
            with os.scandir(path) as entries:
                names = sorted(
                    entry.name + ("/" if entry.is_dir(follow_symlinks=False) else "")
                    for entry in entries
                )
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not list {path}: {exc}", error=str(exc))
        if not names:
            return ToolResult(ok=True, content=f"({path} is empty)", meta={"total": 0, "at_end": True})
        return _page(
            names, max(1, _as_int(arguments.get("offset"), 1)),
            _as_int(arguments.get("limit"), 0), "entries", path,
        )


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


_DEFAULT_SHELL_TIMEOUT = 300
# A ceiling as well as a default. Read straight out of the arguments with no
# declared parameter and no bound, a timeout is invisible to the model that
# might set it and unbounded if one guesses at it — a large enough value makes
# a hung command effectively unkillable except by cancelling the turn.
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


# Tokens the shell treats as the end of one command and the start of another.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})


def _changes_directory_only(command: str) -> bool:
    """Whether every command on this line is a bare ``cd``.

    Such a line does nothing that outlives it. Each ``shell`` call is its own
    process (see ``ShellTool.execute``), so ``cd somewhere`` moves a shell
    that exits a moment later, and the *next* call starts where this one did.
    Reported as ``exit=0`` with no output, that is indistinguishable from
    having worked, and the mistake only surfaces later when something reads
    the wrong directory.

    Deliberately fail-open and advisory: this decides whether a result
    carries an explanatory note, never whether anything may run, so a line
    this cannot read confidently returns ``False`` and simply says nothing.
    That is the opposite of ``policy._segments``, which must fail *closed*
    because it is deciding what is permitted — which is why the two do not
    share an implementation.
    """
    import shlex

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return bool(segments) and all(segment and segment[0] == "cd" for segment in segments)


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
        # Set by the wiring (see cobirb.sandbox). None, or an inactive one,
        # runs commands exactly as before.
        self.sandbox: Any = None
        self._current_process: Any = None
        self._cancel_requested = False

    def description(self) -> str:
        return (
            "Run a shell command, e.g. the test suite or a build, and return its output and exit "
            "code. Requires approval. To look at files, use read_file, list_dir, glob and grep "
            "instead — they need no shell. Each call runs in its own process, so a directory "
            "change does not carry over to the next one: chain it ('cd build && make') or pass "
            "'cwd' instead. Commands may run in a sandbox with no network and with writes "
            "allowed only inside the project and /tmp; set unsandboxed only when a command "
            "genuinely needs more, and expect to be asked."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "cwd": {
                    "type": "string",
                    "description": (
                        "Directory to run the command in, relative to the working "
                        "directory unless absolute. Use this instead of a separate "
                        "'cd', which does not persist between calls."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Seconds to wait before killing the command "
                        f"(default {_DEFAULT_SHELL_TIMEOUT}, maximum {_MAX_SHELL_TIMEOUT})."
                    ),
                    "default": _DEFAULT_SHELL_TIMEOUT,
                },
                "unsandboxed": {
                    "type": "boolean",
                    "description": (
                        "Run outside the sandbox — only for a command that needs the network or "
                        "to write outside the project. Always asks the user."
                    ),
                },
            },
            "required": ["command"],
        }

    def _contained(self, arguments: dict[str, Any]) -> bool:
        return bool(self.sandbox is not None and self.sandbox.active and not arguments.get("unsandboxed"))

    def preview(self, arguments: dict[str, Any]) -> str:
        """Where the command will run — the command itself is already in the
        prompt. Nothing at all without a sandbox, as before."""
        if self._contained(arguments):
            return self.sandbox.note()
        if self.sandbox is not None and self.sandbox.active:
            return "(outside the sandbox: full network and filesystem access)"
        return ""

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        import subprocess

        command = arguments["command"]
        timeout = _shell_timeout(arguments.get("timeout"))

        requested_cwd = arguments.get("cwd")
        cwd = self._resolve(str(requested_cwd)) if requested_cwd else self._cwd
        if cwd is not None and not os.path.isdir(cwd):
            return ToolResult(
                ok=False,
                content=f"No such directory: {cwd}",
                error="cwd not found",
            )

        self._cancel_requested = False
        try:
            contained = self._contained(arguments)
            process = subprocess.Popen(
                self.sandbox.argv(command, cwd or os.getcwd()) if contained else command,
                shell=not contained,
                cwd=cwd,
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
        where = f" {self.sandbox.note()}" if contained else ""
        content = f"exit={process.returncode}{where}\n{_both_ends(f'{stdout}{stderr}', _MAX_SHELL_OUTPUT)}"
        if process.returncode == 0 and _changes_directory_only(command):
            # Said out loud because the alternative is a bare, successful
            # `exit=0` that reads exactly like a directory change that stuck —
            # and the mistake is then only discovered by whatever runs in the
            # wrong place next.
            content += (
                f"\n[note: this changed the directory of the shell that has now exited. "
                f"The next call starts in {cwd or os.getcwd()} again. Chain it into one "
                f"command ('cd somewhere && ...') or pass 'cwd' to run it elsewhere.]"
            )
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
class DeleteFileTool(CobirbTool):
    """Delete one file.

    There was no way to remove a file except ``shell`` — so a refactor that
    ends with "and delete the old module" was, in the benchmark, the step
    models most often reported done and had not done. A file tool is scoped,
    previewed and undoable like the other writes; a directory is refused,
    because removing a tree is not something to do by one call.
    """

    NAME = "delete_file"

    def writes(self, arguments: dict[str, Any]) -> list[str]:
        path = arguments.get("path")
        return [self._resolve(str(path))] if path else []

    def preview(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("path", "")))
        if not os.path.isfile(path):
            return f"{path} is not a file — this will fail"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().count("\n")
        except OSError:
            lines = 0
        return f"delete {path} ({lines} line(s))"

    def description(self) -> str:
        return "Delete one file (not a directory). Undoable with the other file changes."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to delete."}},
            "required": ["path"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments["path"])
        if os.path.isdir(path):
            return ToolResult(ok=False, error="is_directory",
                              content=f"{path} is a directory; delete_file removes files only.")
        try:
            os.remove(path)
        except FileNotFoundError:
            return ToolResult(ok=False, error="not_found", content=f"{path} does not exist.")
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not delete {path}: {exc}", error=str(exc))
        return ToolResult(ok=True, content=f"Deleted {path}")


class TodoTool(CobirbTool):
    """A checklist the model keeps for a multi-step task.

    The whole list is sent on every call rather than edited item by item: one
    shape to get right, which matters for small models, and no way for the
    list to drift out of step with what the model thinks it says. It changes
    nothing on disk and reaches nothing, which is why it is permitted outright
    (``wiring``) — the same considered exception the charter tools are.
    """

    NAME = "todo"
    _STATUSES = ("pending", "in_progress", "done")
    _MARKS = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}

    def __init__(self, cwd: str | None = None) -> None:
        super().__init__(cwd)
        self.items: list[dict[str, str]] = []

    def description(self) -> str:
        return (
            "Keep a checklist for a task with several steps, and keep it current: send the whole "
            "list every time, each item marked pending, in_progress or done. The user sees it. "
            "Changes nothing on disk."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "The full checklist, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": list(self._STATUSES)},
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["items"],
        }

    def progress(self) -> tuple[int, int]:
        """``(done, total)``."""
        return sum(1 for item in self.items if item["status"] == "done"), len(self.items)

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw = arguments.get("items")
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, list):
            return ToolResult(ok=False, error="bad_items",
                              content="items must be a list of {text, status} entries.")
        items = []
        for entry in raw:
            if isinstance(entry, str):
                entry = {"text": entry}
            if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
                continue
            status = str(entry.get("status", "pending")).strip().lower().replace(" ", "_")
            items.append({"text": str(entry["text"]).strip(),
                          "status": status if status in self._STATUSES else "pending"})
        self.items = items[:50]
        done, total = self.progress()
        lines = [f"{self._MARKS[item['status']]} {item['text']}" for item in self.items]
        return ToolResult(ok=True, content=f"Checklist ({done}/{total} done):\n" + "\n".join(lines))


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
    TodoTool,
    DeleteFileTool,
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
