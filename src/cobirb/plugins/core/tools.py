"""Built-in tools and the tool registry.

Tools are the "hands" of the agent. The core ships a registry of built-in tools;
third-party plugins extend it. The registry enforces a default-deny policy:
every tool must be explicitly allowed before it runs. See DESIGN.md §8.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from ...typing.spi import Tool, ToolResult


# --------------------------------------------------------------------------- #
# Tool base
# --------------------------------------------------------------------------- #
class CobirbTool(Tool):
    """Concrete base class for built-in tools."""

    def __init__(self, cwd: str | None = None) -> None:
        self._cwd = cwd

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
class ReadFileTool(CobirbTool):
    name = "read_file"

    def description(self) -> str:
        return "Read the contents of a file at a path."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to read."},
            },
            "required": ["path"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments["path"])
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return ToolResult(ok=True, content=fh.read())
        except OSError as exc:
            return ToolResult(ok=False, content=f"Could not read {path}: {exc}", error=str(exc))


class WriteFileTool(CobirbTool):
    name = "write_file"

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
    name = "edit_file"

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
    name = "apply_patch"

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


_DEFAULT_IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _is_ignored_path(path: str) -> bool:
    """True if any path component is a vendor/build/cache directory glob and
    grep skip by default, so they don't crawl e.g. .venv/ or .git/ on every
    call. Not full .gitignore parsing — just the common, expensive offenders.
    """
    parts = os.path.normpath(path).split(os.sep)
    return any(part in _DEFAULT_IGNORED_DIR_NAMES or part.endswith(".egg-info") for part in parts)


class GlobTool(CobirbTool):
    name = "glob"

    def description(self) -> str:
        return "Find files matching a glob pattern (searches from the working directory)."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. 'src/**/*.py'."},
                "include_ignored": {
                    "type": "boolean",
                    "description": "Include files normally ignored by .gitignore.",
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
                results = [r for r in results if not _is_ignored_path(r)]
            return ToolResult(ok=True, content="\n".join(sorted(results)) if results else "(no matches)")
        except OSError as exc:
            return ToolResult(ok=False, content=f"glob failed: {exc}", error=str(exc))


class GrepTool(CobirbTool):
    name = "grep"

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
                    "description": "Include files normally ignored.",
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
                paths = [p for p in paths if not _is_ignored_path(p)]
            matches = []
            for p in paths:
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if re.search(pattern, line):
                                matches.append(f"{p}:{lineno}: {line.strip()}")
                except (OSError, UnicodeDecodeError):
                    continue
            return ToolResult(ok=True, content="\n".join(matches) if matches else "(no matches)")
        except OSError as exc:
            return ToolResult(ok=False, content=f"grep failed: {exc}", error=str(exc))


class ListDirTool(CobirbTool):
    name = "list_dir"

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


class ShellTool(CobirbTool):
    """Highest-privilege tool. Gated behind the permission layer.

    The permission layer can match on the first word after splitting on shell
    separators (`; | && &`), so `bash -n` can be allowed without allowing `bash`.
    See DESIGN.md §8 and todo-list.md for the motivation.
    """

    name = "shell"

    def description(self) -> str:
        return "Execute a shell command. Requires explicit approval."

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
            },
            "required": ["command"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        import subprocess

        command = arguments["command"]
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=arguments.get("timeout", 300),
            )
            content = f"exit={result.returncode}\n{result.stdout}{result.stderr}"
            return ToolResult(ok=result.returncode == 0, content=content, meta={"returncode": result.returncode})
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, content="Command timed out.", error="timeout")
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(ok=False, content=f"Command failed: {exc}", error=str(exc))


# Registry of built-in tools.
BUILTIN_TOOLS: list[type[Tool]] = [
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ApplyPatchTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ShellTool,
]


def _first_word(command: str) -> str:
    """Return the first word of a shell command, split on common separators.

    Used by the permission layer to allow narrow scopes like ``bash -n`` without
    allowing ``bash``. See todo-list.md.
    """
    parts = re.split(r";|\||&&|&|\n", command.strip())
    return parts[0].split()[0] if parts else ""


class ToolRegistry:
    """Holds the built-in tools and supports plugin extension."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self._tools: dict[str, Tool] = {}
        for tool_cls in BUILTIN_TOOLS:
            self.register(tool_cls(self.cwd))

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def values(self) -> list[Tool]:
        """Return the registered tools in registration order."""
        return list(self._tools.values())

    def is_known(self, name: str) -> bool:
        return name in self._tools

    def first_word(self, command: str) -> str:
        """Convenience accessor for the permission layer."""
        return _first_word(command)
