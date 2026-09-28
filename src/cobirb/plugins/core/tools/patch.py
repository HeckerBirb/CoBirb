"""``apply_patch``: unified diffs, and the ``*** Begin Patch`` format."""

from __future__ import annotations

import os
import re
from typing import Any

from .... import patches
from ....policy import patch_target
from ....typing.spi import ToolResult
from .base import CobirbTool, _syntax_note

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


def _apply_patch_hunks(
    original_lines: list[str], hunks: list[tuple[int, list[tuple[str, str]]]]
) -> list[str]:
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
                    raise ValueError(
                        f"patch does not apply at line {cursor + 1}: expected {text!r}, found {found!r}"
                    )
                if marker == " ":
                    result.append(text)
                cursor += 1
            else:  # "+"
                result.append(text)
    result.extend(original_lines[cursor:])
    return result


def _block_positions(lines: list[str], block: list[str], start: int, *, loose: bool = False) -> list[int]:
    """Every index at or after ``start`` where ``block`` appears in ``lines``;
    ``loose`` ignores trailing whitespace."""

    def same(a: str, b: str) -> bool:
        return a.rstrip() == b.rstrip() if loose else a == b

    return [
        i
        for i in range(start, len(lines) - len(block) + 1)
        if all(same(lines[i + k], line) for k, line in enumerate(block))
    ]


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
            raise ValueError(
                "a hunk with no context or removed lines cannot be placed; include the line it follows"
            )

        found = _block_positions(result, old, start) or _block_positions(result, old, start, loose=True)
        if not found:
            raise ValueError(f"these lines were not found in the file: {old[0]!r}…")
        if len(found) > 1 and not anchor:
            where = ", ".join(str(i + 1) for i in found[:8])
            raise ValueError(
                f"the hunk starting {old[0]!r} matches {len(found)} places (lines {where}); "
                "add context or an @@ anchor naming the enclosing function"
            )
        at = found[0]
        result[at : at + len(old)] = new
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
                "path": {
                    "type": "string",
                    "description": "File to patch (optional when the patch names it).",
                },
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
                    ok=False,
                    error="patch_scope",
                    content="This patch touches more than one file, moves or deletes one, or names a "
                    "different file than 'path'. Send one *** Update File (or *** Add File) "
                    "section per call.",
                )
            return ToolResult(
                ok=False, content="apply_patch needs a path for a unified diff.", error="no_path"
            )
        path = self._resolve(target)

        if patches.is_begin_patch(patch):
            section = patches.parse(patch)[0]
            if section.op == "add":
                if os.path.exists(path):
                    return ToolResult(
                        ok=False,
                        error="exists",
                        content=f"{path} already exists; use *** Update File to change it.",
                    )
                content = "\n".join(section.lines) + "\n"
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(content)
                except OSError as exc:
                    return ToolResult(ok=False, content=f"Could not create {path}: {exc}", error=str(exc))
                return ToolResult(ok=True, content=f"Created {path}{_syntax_note(path, content)}")

        try:
            with open(path, encoding="utf-8") as fh:
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
                        return ToolResult(
                            ok=False, content="No valid hunks found in patch.", error="no_hunks"
                        )
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
