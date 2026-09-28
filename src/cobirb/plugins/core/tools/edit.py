"""``edit_file``: replace one exact region of a file."""

from __future__ import annotations

import difflib
import re
from typing import Any

from ....typing.spi import ToolResult
from .base import CobirbTool, _syntax_note, _unified


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
            with open(path, encoding="utf-8") as fh:
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
            with open(path, encoding="utf-8") as fh:
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

    def __init__(self, content: str | None, message: str, error: str = "") -> None:
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
    region = updated[begin : begin + len(new)].splitlines() or [""]
    note = " (matched ignoring whitespace differences)" if loose else ""
    return _EditPlan(
        updated, f"{note}. Lines {first}–{first + len(region) - 1} now read:\n" + _numbered(region, first)
    )


def _loose_match(content: str, old: str, new: str) -> tuple[int, int, str] | None:
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
        prefix = _common_extra_indent(lines[i : i + n], old_lines)
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


def _common_extra_indent(have_lines: list[str], want_lines: list[str]) -> str | None:
    """The indent every ``have`` line carries beyond its ``want`` line, when
    the text is otherwise equal and that extra indent is the same throughout."""
    prefix: str | None = None
    for have, want in zip(have_lines, want_lines, strict=False):
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
        extra = have_indent[len(want_indent) :]
        if prefix is None:
            prefix = extra
        elif extra != prefix:
            return None
    return prefix


def _miss_message(content: str, old: str) -> str:
    """ "Not found", plus the closest region so the model can copy it exactly."""
    lines = content.splitlines()
    size = max(1, len(old.strip("\n").splitlines()))
    best, best_ratio = -1, 0.0
    target = old.strip()
    for i in range(max(1, len(lines) - size + 1)):
        window = "\n".join(lines[i : i + size])
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
            + _numbered(lines[best : best + size], best + 1)
        )
    else:
        message += " Read the file again and copy the region exactly."
    return message
