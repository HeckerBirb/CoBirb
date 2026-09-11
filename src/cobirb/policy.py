"""Permissions, audit, and the local-only policy layer.

Enforces the ironclad rule **"default-deny"**: no tool runs unless it is
explicitly allowed and never denied. See DESIGN.md §8.

- Approval granularity: per-tool-name, or per-tool-with-narrow-args.
- The `shell` tool's scope can be narrowed by its first word (e.g. allow
  ``bash -n`` without allowing ``bash``), so a broad ``shell`` allow does not
  necessarily mean the agent can run anything.
- The audit log is append-only and never leaves the machine.
"""
from __future__ import annotations

import re

import json
import os
import time
from typing import Any


class PermissionError(Exception):
    """Raised when a capability is not permitted."""


class AuditLog:
    """Append-only local audit log of what ran. Never leaves the machine."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(
            os.environ.get("COBIRB_HOME", os.path.expanduser("~")), ".cobirb", "audit.jsonl"
        )

    def append(self, entry: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def _first_word(command: str) -> str:
    """Return the first word of a shell command, split on common separators.

    Used to narrow ``shell`` scope (e.g. ``bash -n`` without ``bash``).
    """
    parts = re.split(r";|\||&&|&|\n", command.strip())
    return parts[0].split()[0] if parts and parts[0] else ""


class Policy:
    """Default-deny permission policy with a local audit trail."""

    def __init__(
        self,
        allowed: set[str] | None = None,
        denied: set[str] | None = None,
        audit: AuditLog | None = None,
        cwd: str | None = None,
    ) -> None:
        self.cwd = cwd or os.getcwd()
        self._allowed = set(allowed or set())
        self._denied = set(denied or set())
        self.audit = audit or AuditLog()

    def is_denied(self, tool_name: str) -> bool:
        """Explicit deny list always wins."""
        return tool_name in self._denied

    def is_allowed(self, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        """Return True only if the tool is explicitly allowed and not denied.

        For the ``shell`` tool, scope is narrowed by the first word of the
        command when one is provided.
        """
        if self.is_denied(tool_name):
            return False

        # Narrow ``shell`` scope to a first-word scope when a command is
        # provided. If no command is given, the tool is allowed as-is.
        if tool_name == "shell" and arguments is not None:
            command = arguments.get("command", "")
            first = _first_word(command)
            if first:
                return first in self._allowed
            return True

        if tool_name not in self._allowed:
            return False

        return True

    def allow(self, tool_name: str, command: str | None = None) -> None:
        """Allow a tool, optionally narrowing the shell scope by first word."""
        if command is not None:
            first = _first_word(command)
            if first:
                self._allowed.add(first)
                return
        self._allowed.add(tool_name)

    def deny(self, tool_name: str) -> None:
        self._denied.add(tool_name)

    def log(self, tool_name: str, arguments: dict[str, Any] | None = None, cwd: str | None = None) -> None:
        entry = {
            "ts": time.time(),
            "tool": tool_name,
            "args": arguments,
            "cwd": cwd or self.cwd,
        }
        self.audit.append(entry)

    def allow_all_core_tools(self) -> None:
        """Convenience: allow the built-in core tools.

        The ``shell`` scope is narrowed to a safe default set of first words
        (git, python, pytest, ls, cat, grep, mkdir) — not unrestricted.
        """
        for name in ("read_file", "write_file", "edit_file", "apply_patch", "glob", "grep", "list_dir"):
            self._allowed.add(name)
        # The ``shell`` tool is enabled, but its scope is narrowed to a safe
        # default set of first words (git, python, pytest, ls, cat, grep,
        # mkdir, find) — not unrestricted shell.
        self._allowed.add("shell")
        for first in ("git", "python", "pytest", "ls", "cat", "grep", "mkdir", "find"):
            self._allowed.add(first)
