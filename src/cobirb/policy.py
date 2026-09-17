"""Permissions, audit, and the local-only policy layer.

Enforces the ironclad rule **"default-deny"**: no tool runs unless it is
explicitly allowed and never denied. Nothing is pre-approved — a fresh policy
permits nothing at all, and every capability is granted either by the user
answering an approval prompt or by their own config/``--allow-tool``.

- Approval granularity: per-tool-name, or per-tool-with-narrow-args.
- Reading and writing are each scoped by *directory* rather than by file, in
  two separate sets that never imply one another. Approving a read grants the
  read-only tools that directory and everything under it; approving a write
  grants the file-changing tools the same, and neither grants the other —
  agreeing that CoBirb may read a project is a far smaller thing than
  agreeing it may rewrite one. Executing is never granted this way: ``shell``
  cannot say what it touches.
- The ``shell`` tool's scope is narrowed per *command segment*: a shell
  command may chain several invocations (``git status; rm -rf /``), and the
  shell runs all of them, so **every** segment must be permitted — not just
  the first. See ``_segments`` for the mechanics.
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
import sys
import time
from typing import Any

from . import paths
from .redaction import redact_arguments

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

# `find`'s -exec family terminates its command with a bare `;`, which the
# segment scanner below reads as a command separator — so the program being
# exec'd lands in a segment of its own (usually an empty tail) and is never
# checked, while `find` itself looks innocuous. `find . -exec rm -rf {} ;`
# is the whole allow-list defeated by one flag. A segment carrying one of
# these is refused outright, on the same principle as command substitution:
# what this scan cannot read, it cannot approve.
_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

# Tools that change files. A directory approval covers these the same way it
# covers reads — but they are a separate set, and a read grant never implies a
# write one. `shell` is in neither: it cannot say what it touches.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})

# Tools that only ever read. These are the ones a *directory* approval
# covers; everything else (write_file, edit_file, apply_patch, shell, and any
# plugin tool) is approved per call or by an explicit rule.
READ_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep", "repo_map"})

# Glob metacharacters — everything before the first component containing one
# is the fixed directory prefix a pattern searches under.
_GLOB_MAGIC = "*?["


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
        self.path = path or paths.audit_path()
        self.enabled = enabled

    def append(self, entry: dict[str, Any]) -> None:
        """Record one tool call, never at the cost of the call itself.

        An unwritable log path (a read-only volume, a full disk, a directory
        that can't be created) must never take down the tool call this is only
        observing. The trail is worth having, but not more than the work it is
        a trail of — so a failure is reported once and stepped over.
        """
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # 0o600, and created with it rather than chmod'ed after: unlike a
            # session this log is plaintext, and by its own docstring it holds
            # file contents, diffs and shell commands verbatim.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            print(f"cobirb: could not write the audit log at {self.path} — {exc}", file=sys.stderr)


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
    verify: command substitution (``$(...)``, backticks), a subshell, or
    ``find``'s ``-exec`` family (see ``_EXEC_FLAGS``).
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
    if any(token in _EXEC_FLAGS for segment in segments for token in segment):
        # The exec'd program is not in a segment this scan can attribute to
        # anything, so the command as a whole is unverifiable.
        return None
    return segments


def _glob_base(pattern: str) -> str:
    """The fixed directory prefix a glob pattern searches under.

    ``"src/**/*.py"`` -> ``"src"``; ``"*.py"`` -> ``"."``. Used to decide
    which directory a ``glob`` call is actually reading from.
    """
    base: list[str] = []
    for part in pattern.replace("\\", "/").split("/"):
        if any(char in part for char in _GLOB_MAGIC):
            break
        base.append(part)
    return "/".join(base) or "."


def _target_path(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """The path a path-scoped tool call is about to touch, as written.

    Serves reads *and* writes — it was called ``_read_target`` while both
    ``_scoped_allowed`` and ``path_scope`` already used it for the write
    tools too, so the name quietly claimed a narrower job than it does, in
    the one module where being exact about what is being permitted matters
    most.

    Each tool names its target differently — ``glob`` takes a ``pattern``,
    ``grep``'s ``path`` is optional and defaults to the working directory the
    way the tool itself defaults it — so the mapping lives here rather than
    being guessed at the call site. ``None`` means the call doesn't name a
    resolvable target at all, which is treated as "cannot verify", and
    therefore denied.
    """
    if tool_name == "glob":
        pattern = arguments.get("pattern")
        return _glob_base(pattern) if isinstance(pattern, str) and pattern else None
    target = arguments.get("path")
    if target is None and tool_name in ("grep", "repo_map"):
        target = "."  # matching those tools' own defaults
    return target if isinstance(target, str) and target else None


class Policy:
    """Default-deny permission policy with a local audit trail.

    A fresh policy allows **nothing**. Capabilities arrive one of three ways:
    the user answers an approval prompt, they list a rule in config's
    ``allow_tools``, or they pass ``--allow-tool``. There is no pre-approved
    set — the tool asks before it reads, writes, or runs anything.

    Reads and writes are each scoped by directory (``allow_read_dir``,
    ``allow_write_dir``): approving one grants the matching tool set that
    directory and everything beneath it, so a user who has agreed that CoBirb
    may work on a project is not asked again for every file in it. The two
    sets are deliberately separate and one never implies the other — agreeing
    that it may *read* a project is a far smaller thing than agreeing it may
    rewrite one. ``shell`` is in neither set, because it cannot say what it
    touches.

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
        self._allowed_read_dirs: set[str] = set()
        self._allowed_write_dirs: set[str] = set()
        self._denied = set(denied or set())
        self.audit = audit or AuditLog()

    def is_denied(self, tool_name: str) -> bool:
        """Explicit deny list always wins."""
        return tool_name in self._denied

    def is_allowed(self, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        """Return True only if the tool is explicitly allowed and not denied.

        For the ``shell`` tool with a command, every segment of that command
        must be permitted (see ``_shell_allowed``). For a read-only tool, an
        approved directory covering the path it names is enough (see
        ``_read_allowed``) — as is a blanket rule naming the tool itself,
        which is what ``--allow-tool=read_file`` produces and which
        deliberately outranks the directory scope.
        """
        if self.is_denied(tool_name):
            return False

        if tool_name == "shell" and arguments is not None:
            return self._shell_allowed(arguments)

        if tool_name in self._allowed:
            return True

        if tool_name in READ_TOOLS and arguments is not None:
            return self._scoped_allowed(tool_name, arguments, self._allowed_read_dirs)

        if tool_name in WRITE_TOOLS and arguments is not None:
            return self._scoped_allowed(tool_name, arguments, self._allowed_write_dirs)

        return False

    # ------------------------------------------------------------------ #
    # Directory-scoped reads
    # ------------------------------------------------------------------ #
    def _resolve(self, path: str) -> str:
        """A path as the tools will actually resolve it: ``~`` expanded,
        relative to this policy's working directory, then fully resolved.

        ``realpath`` rather than ``abspath`` so that ``..`` and symlinks
        cannot be used to name a file outside an approved directory while
        looking like one inside it.

        **This must mirror ``CobirbTool._resolve`` step for step.** The whole
        point of this method is to decide, before approving a call, which file
        that call will touch — so any difference between the two is a
        permission check performed on a path the tool is not going to use.
        ``~`` is expanded here for exactly that reason: the tool expands it,
        therefore `~/x` must be gated as the file in the home directory it
        will really become, not as a literal `~` directory under the cwd.
        """
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.realpath(path)

    def _scoped_allowed(
        self, tool_name: str, arguments: dict[str, Any], approved: set[str]
    ) -> bool:
        """Whether a path-scoped call falls inside one of ``approved``."""
        target = _target_path(tool_name, arguments)
        if target is None:
            return False
        resolved = self._resolve(target)
        return any(_within(resolved, directory) for directory in approved)

    def path_scope(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """The directory an "always" answer to this call would approve.

        For a call naming a file that is its parent directory; for one already
        naming a directory it is the target itself. ``None`` when the call
        names nothing resolvable, in which case there is no scope to offer and
        the answer can only apply to this one call.
        """
        target = _target_path(tool_name, arguments)
        if target is None:
            return None
        resolved = self._resolve(target)
        if tool_name in WRITE_TOOLS or tool_name == "read_file":
            return os.path.dirname(resolved) or os.sep
        return resolved

    def allow_read_dir(self, directory: str) -> None:
        """Approve reading ``directory`` and everything beneath it."""
        self._allowed_read_dirs.add(self._resolve(directory))

    def allow_write_dir(self, directory: str) -> None:
        """Approve changing files in ``directory`` and everything beneath it.

        Kept separate from the read set on purpose: agreeing that CoBirb may
        *read* a project is a much smaller thing than agreeing it may rewrite
        it, and one should never quietly imply the other.
        """
        self._allowed_write_dirs.add(self._resolve(directory))

    @property
    def allowed_read_dirs(self) -> set[str]:
        """The approved read directories — a copy, for display and tests."""
        return set(self._allowed_read_dirs)

    @property
    def allowed_write_dirs(self) -> set[str]:
        return set(self._allowed_write_dirs)

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
        """Record a tool call, with credentials stripped from its arguments.

        This log's whole problem is that it stores arguments verbatim —
        ``write_file``'s full content, ``shell``'s full command — in a
        plaintext file that outlives the session. Redacting here means turning
        it on does not mean accepting a second copy of every key the agent
        happened to handle.
        """
        entry = {
            "ts": time.time(),
            "tool": tool_name,
            "args": redact_arguments(arguments or {}),
            "cwd": cwd or self.cwd,
        }
        self.audit.append(entry)

    # ------------------------------------------------------------------ #
    # Granting, after the user says yes
    # ------------------------------------------------------------------ #
    def grant(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        """Widen the policy after an "always" approval.

        What "always" means depends on the tool, and deciding that here keeps
        the orchestrator from having to know: a read is granted over its
        directory, a shell call over the invocation it named (a bare binary,
        or an exact multi-word prefix), and anything else — writes, patches,
        plugin tools — over the tool name, since there is no narrower unit
        those calls have in common.
        """
        arguments = arguments or {}
        if tool_name == "shell":
            self.allow(tool_name, arguments.get("command"))
            return
        if tool_name in READ_TOOLS:
            directory = self.path_scope(tool_name, arguments)
            if directory is not None:
                self.allow_read_dir(directory)
                return
        if tool_name in WRITE_TOOLS:
            # Scoped to the directory, and to the write tools only. Granting
            # write_file everywhere would be a great deal more than the
            # question appeared to be asking.
            directory = self.path_scope(tool_name, arguments)
            if directory is not None:
                self.allow_write_dir(directory)
                return
        self.allow(tool_name)

    def describe_grant(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Plain-language description of what ``grant`` would permit, for the
        approval prompt.

        The user is agreeing to a scope, not to a single call, so the prompt
        has to be able to say what that scope is — "read files in /x and its
        subdirectories" is a different question from "allow read_file", and
        only one of them is what actually happens.
        """
        arguments = arguments or {}
        if tool_name == "shell":
            words = _first_segment_words(str(arguments.get("command") or ""))
            if len(words) > 1:
                return f"run '{' '.join(words)}' commands"
            if words:
                return f"run '{words[0]}' commands"
            return "run shell commands"
        if tool_name in READ_TOOLS:
            directory = self.path_scope(tool_name, arguments)
            if directory is not None:
                return f"read files in {directory} and its subdirectories"
        if tool_name in WRITE_TOOLS:
            directory = self.path_scope(tool_name, arguments)
            if directory is not None:
                return f"change files in {directory} and its subdirectories"
        return f"use '{tool_name}'"


def _within(path: str, directory: str) -> bool:
    """Whether ``path`` is ``directory`` itself or sits beneath it.

    Both are already fully resolved by ``Policy._resolve``. The separator on
    the prefix check is what stops ``/home/birb/project-secrets`` from
    matching an approval for ``/home/birb/project``.
    """
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


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
