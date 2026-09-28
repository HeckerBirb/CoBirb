"""``shell``: run a command, contained by the sandbox when there is one."""

from __future__ import annotations

import os
from typing import Any

from ....typing.spi import ToolResult
from .base import CobirbTool

# Bytes of combined stdout and stderr one shell call may return. Both ends are
# kept when it overflows: the head is what the command set out to say and the
# tail is usually where it went wrong, and losing either makes the other much
# harder to act on.
_MAX_SHELL_OUTPUT = 64 * 1024


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
    That is the opposite of ``policy.command_segments``, which must fail *closed*
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
                # Never CoBirb's own stdin. A Remote Worker Birb's job reads its
                # orders from a pipe on stdin, and on Windows a child holding
                # that pipe blocks at start-up while the job waits on it: every
                # `python -c "print(1)"` hung until its timeout. A command has
                # no one to type to here anyway.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
                creationflags=0 if os.name == "posix" else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
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
        return ToolResult(
            ok=process.returncode == 0, content=content, meta={"returncode": process.returncode}
        )

    @staticmethod
    def _kill(process: Any) -> None:
        """Kill ``process`` and everything it started — see the class docstring
        for why a plain ``process.kill()`` isn't enough for a command that
        backgrounds or forks. On Windows there is no process group to signal,
        and killing ``cmd.exe`` alone leaves a compiler or test binary running
        under a Remote Worker Birb, so ``taskkill /T`` takes the whole tree."""
        import signal
        import subprocess

        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )
                process.kill()
        except (ProcessLookupError, OSError):
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
