"""Reading, writing and deleting whole files."""

from __future__ import annotations

import os
from typing import Any

from ....typing.spi import ToolResult
from .base import _MAX_READ_BYTES, CobirbTool, _as_int, _syntax_note, _unified


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
    with open(path, encoding="utf-8") as fh:
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
            with open(path, encoding="utf-8") as fh:
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
            with open(path, encoding="utf-8", errors="replace") as fh:
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
            return ToolResult(
                ok=False,
                error="is_directory",
                content=f"{path} is a directory; delete_file removes files only.",
            )
        try:
            os.remove(path)
        except FileNotFoundError:
            return ToolResult(ok=False, error="not_found", content=f"{path} does not exist.")
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not delete {path}: {exc}", error=str(exc))
        return ToolResult(ok=True, content=f"Deleted {path}")
