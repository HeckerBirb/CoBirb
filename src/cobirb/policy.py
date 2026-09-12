"""Permissions, audit, and the local-only policy layer.

Enforces the ironclad rule **"default-deny"**: no tool runs unless it is
explicitly allowed and never denied. See DESIGN.md §8.

- Approval granularity: per-tool-name, or per-tool-with-narrow-args.
- The ``shell`` tool's scope is narrowed per *command segment*: a shell
  command may chain several invocations (``git status; rm -rf /``), and the
  shell runs all of them, so **every** segment must be permitted — not just
  the first. See ``_segments`` and todo-list.md for the motivation.
- Redirection (``>``, ``>>``, ``<``) stays attached to the command it
  belongs to rather than starting a new one — ``git log > out.txt`` is one
  command, not two. Anything the policy still cannot verify (command
  substitution, a subshell, an unbalanced quote) is refused rather than
  waved through: the shell would execute the whole string, so what can't
  be checked can't run.
- The audit log is append-only and never leaves the machine.
"""
from __future__ import annotations

import json
import os
import shlex
import time
from typing import Any

# Tokens made only of these characters are shell operators rather than words.
_PUNCTUATION = set("();<>|&")

# Operators that merely separate one command from the next. Each side is a
# command in its own right and is checked independently.
_SEPARATOR_CHARS = set(";|&")

# Redirection operators (`>`, `>>`, `<`, `<<`, `>|`, ...) stay part of the
# command they redirect, rather than splitting it or being refused outright:
# `git log > out.txt` is a single command, and the target file is not
# something a shell-level scan can usefully allow/deny on its own.
_REDIRECT_CHARS = set("<>")


class PermissionError(Exception):
    """Raised when a capability is not permitted."""


class AuditLog:
    """Append-only local record of what ran (tool name, arguments, cwd,
    timestamp) — off by default.

    Opt-in, not opt-out: the arguments logged are whatever a tool call
    actually carried, unfiltered — for ``write_file`` that's the full file
    content, for ``edit_file`` the full old/new text, for ``apply_patch``
    the full diff, for ``shell`` the full command. That makes this a
    genuinely useful "what did the agent do" trail once turned on, but it
    also means an *always-on* audit log would silently keep a second,
    plaintext, unencrypted, never-rotated copy of everything written or
    run — directly at odds with sessions being encrypted at rest. So
    ``enabled`` defaults to ``False`` here and stays false unless
    ``"audit_log": true`` is set in config (see ``cobirb help config``);
    nothing is ever written, and no file is even created, until then.
    """

    def __init__(self, path: str | None = None, enabled: bool = False) -> None:
        self.path = path or os.path.join(
            os.environ.get("COBIRB_HOME", os.path.expanduser("~")), ".cobirb", "audit.jsonl"
        )
        self.enabled = enabled

    def append(self, entry: dict[str, Any]) -> None:
        if not self.enabled:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def _tokenize(command: str) -> list[str] | None:
    """Tokenize a shell command, honouring quotes and returning operators
    (``;``, ``&&``, ``|``, ``>`` ...) as tokens of their own.

    Returns ``None`` when the command cannot be parsed (an unbalanced quote,
    say). Callers must treat that as "not verifiable", and therefore deny:
    guessing at a command the policy can't read is how allow-lists get
    bypassed.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(command: str) -> list[list[str]] | None:
    """Split a shell command into its separately-executed segments.

    ``"git status; rm -rf /"`` becomes ``[["git", "status"], ["rm", "-rf", "/"]]``
    — two commands, both of which the shell will run, so both must be
    permitted. Quoting is respected, so a separator inside a quoted argument
    (``git commit -m "fix: a; b"``) does *not* split the command.

    Returns ``None`` if the command can't be parsed, or if it uses a
    construct whose contents this scan cannot see and therefore cannot
    verify: command substitution (``$(...)``, backticks) or a subshell.
    """
    if "`" in command:
        return None  # backtick substitution hides an arbitrary command
    tokens = _tokenize(command)
    if tokens is None:
        return None

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= _PUNCTUATION:
            if set(token) <= _SEPARATOR_CHARS:
                if current:
                    segments.append(current)
                    current = []
                continue
            if set(token) <= _REDIRECT_CHARS:
                # Part of the current command, not a new one and not denied
                # outright — appended as a literal token so the following
                # target filename rides along in the same segment.
                current.append(token)
                continue
            # ( ) and anything mixing redirect/separator characters: a
            # subshell or a construct this scan can't confidently read.
            return None
        current.append(token)
    if current:
        segments.append(current)
    return segments


class Policy:
    """Default-deny permission policy with a local audit trail.

    Two kinds of ``shell`` allow rules exist:

    - **first-word** (``_allowed``): the bare binary is trusted with *any*
      arguments, e.g. allowing ``git`` also allows ``git push --force``.
      Only use this for tools that are safe regardless of arguments.
    - **prefix** (``_allowed_prefixes``): a specific multi-word invocation is
      trusted, e.g. allowing ``python -m pytest`` does **not** allow
      ``python -c '...'``. This is what ``allow(tool, command)`` produces
      when ``command`` has more than one word — narrowing to just the first
      word (e.g. bare ``python``) would defeat the point of narrowing at all,
      since ``python`` alone can run arbitrary code via ``-c``.

    Both are applied to *every* segment of a chained command, so an allowed
    binary can't be used to smuggle a denied one in after a separator.
    """

    def __init__(
        self,
        allowed: set[str] | None = None,
        denied: set[str] | None = None,
        audit: AuditLog | None = None,
        cwd: str | None = None,
    ) -> None:
        self.cwd = cwd or os.getcwd()
        self._allowed = set(allowed or set())
        self._allowed_prefixes: set[tuple[str, ...]] = set()
        self._denied = set(denied or set())
        self.audit = audit or AuditLog()

    def is_denied(self, tool_name: str) -> bool:
        """Explicit deny list always wins."""
        return tool_name in self._denied

    def is_allowed(self, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        """Return True only if the tool is explicitly allowed and not denied.

        For the ``shell`` tool with a command, every segment of that command
        must be permitted (see ``_shell_allowed``).
        """
        if self.is_denied(tool_name):
            return False

        if tool_name == "shell" and arguments is not None:
            return self._shell_allowed(arguments)

        return tool_name in self._allowed

    def _shell_allowed(self, arguments: dict[str, Any]) -> bool:
        """Whether every command in a ``shell`` invocation is permitted.

        Fails closed. A missing or blank command, an unparseable one, and one
        using command substitution or a subshell are all denied: the shell
        executes the entire string, so anything this can't verify must not
        run. A denial is not a dead end — the orchestrator then asks the user
        (see ``I_OAdapter.confirm``), so being strict here costs a prompt,
        not a capability.
        """
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return False

        segments = _segments(command)
        if not segments:  # unparseable/unverifiable (None) or nothing to run ([])
            return False
        return all(self._segment_allowed(words) for words in segments)

    def _segment_allowed(self, words: list[str]) -> bool:
        """Whether one command segment is permitted: its binary is trusted
        outright, or the segment matches an allowed narrow prefix."""
        if words[0] in self._allowed:
            return True
        return any(words[: len(prefix)] == list(prefix) for prefix in self._allowed_prefixes)

    def allow(self, tool_name: str, command: str | None = None) -> None:
        """Allow a tool.

        With no ``command``, allows the tool outright. With a single-word
        ``command`` (just a binary name), that binary is trusted with any
        arguments. With a multi-word ``command``, only that exact invocation
        prefix is trusted — narrower than allowing the binary outright.
        """
        if command is not None:
            words = _first_segment_words(command)
            if len(words) > 1:
                self._allowed_prefixes.add(tuple(words))
                return
            if words:
                self._allowed.add(words[0])
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

        The ``shell`` scope is narrowed to a safe default: binaries that are
        safe regardless of arguments (git, ls, cat, grep, mkdir, find,
        pytest) are allowed outright. ``python`` is deliberately *not*
        allowed outright — ``python -c '...'`` runs arbitrary code — so only
        specific narrow invocations are allowed instead.
        """
        for name in ("read_file", "write_file", "edit_file", "apply_patch", "glob", "grep", "list_dir"):
            self._allowed.add(name)
        self._allowed.add("shell")
        for first in ("git", "ls", "cat", "grep", "mkdir", "find", "pytest"):
            self._allowed.add(first)
        for prefix in ("python --version", "python -m pytest", "python -m cobirb"):
            self.allow("shell", prefix)


def _first_segment_words(command: str) -> list[str]:
    """Words of the first segment of ``command``, for building allow rules.

    Unlike ``_segments`` this never refuses: it describes a rule the user is
    writing, not a command about to run, and falls back to a naive split so
    an odd rule can't crash rule construction.
    """
    segments = _segments(command)
    if segments:
        return segments[0]
    return command.split()
