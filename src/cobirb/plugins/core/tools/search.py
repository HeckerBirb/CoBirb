"""Finding things without reading them whole: ``glob``, ``grep``,
``list_dir`` and ``repo_map``."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

from ....typing.spi import ToolResult
from ..ignores import IgnoreRules
from ..repomap import DEFAULT_BUDGET_CHARS, render_map
from .base import _MAX_READ_BYTES, CobirbTool, _as_int, _truncated

# Matches one grep call returns before it stops and says so.
_MAX_GREP_MATCHES = 500

# Entries returned by one list_dir or glob call. A directory with a hundred
# thousand files is not rare — node_modules, a build output, a mail spool —
# and dumping all of them was the same unbounded-result bug read_file had,
# without even a truncation message.
_MAX_LIST_ENTRIES = 1000

# One matching line, clipped. A grep hit in a minified bundle is a single line
# of megabytes; the useful part is that it matched and where.
_MAX_MATCH_CHARS = 300


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
            f"{body}\n[{what} {offset}-{end} of {total}; more follow — call again with offset={end + 1}]"
        ),
        meta={"total": total, "next_offset": end + 1, "at_end": False},
    )


def _clip(line: str, limit: int = _MAX_MATCH_CHARS) -> str:
    """One matching line, cut to something readable.

    A match in a minified bundle is a single line of megabytes. Nobody — model
    or person — reads that, and storing it whole to truncate later is how a
    grep over a large tree turns into gigabytes of memory.
    """
    return line if len(line) <= limit else line[:limit] + f"…[+{len(line) - limit} chars]"


def _walk_files(root: str, rules: IgnoreRules | None) -> Iterator[str]:
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
                name
                for name in subdirectories
                if not rules.is_ignored(os.path.join(directory, name), is_dir=True)
            ]
        for filename in sorted(filenames):
            path = os.path.join(directory, filename)
            if rules is None or not rules.is_ignored(path, is_dir=False):
                yield path


def _ignore_rules(tool: CobirbTool) -> IgnoreRules:
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
                sorted(results),
                max(1, _as_int(arguments.get("offset"), 1)),
                _as_int(arguments.get("limit"), 0),
                "matches",
                pattern,
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
                    with open(path, encoding="utf-8", errors="ignore") as fh:
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
                    entry.name + ("/" if entry.is_dir(follow_symlinks=False) else "") for entry in entries
                )
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not list {path}: {exc}", error=str(exc))
        if not names:
            return ToolResult(ok=True, content=f"({path} is empty)", meta={"total": 0, "at_end": True})
        return _page(
            names,
            max(1, _as_int(arguments.get("offset"), 1)),
            _as_int(arguments.get("limit"), 0),
            "entries",
            path,
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
