"""Running the project's own tests after the agent changes something.

Plan mode's validate phase asks the *model* whether the work is right, which
is worth something and is not evidence. This runs the command the user
nominated and reads the exit code, which is.

**Off unless configured.** `"verify_command": "pytest -q"` turns it on. There
is no guessing at a project's test command from its layout: guessing wrong
means running an arbitrary command the user never asked for, after every turn.

**It runs outside the permission layer, deliberately.** The command comes from
the user's own config file — a more explicit authorization than `allow_tools`,
which is a pattern rather than a literal command — and it is CoBirb running
it, not the model choosing to. The model can neither pick the command nor
change it mid-session. This is documented rather than quietly assumed.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

# A verification that outlives the attention span it was meant to serve is
# worse than none — the user is sitting there watching a turn not finish.
DEFAULT_TIMEOUT_SECONDS = 120

# How many times the model gets to react to a failure before CoBirb stops and
# reports. One is deliberate: a model that cannot fix a failing suite in a
# single focused attempt is not usually one turn away, and every extra round
# is model time the user did not ask for.
DEFAULT_MAX_FIX_ATTEMPTS = 1

# Test output is verbose and mostly repetition. The tail carries the summary
# and the last failure, which is what a fix actually needs.
_MAX_OUTPUT_CHARS = 4000


@dataclass
class VerifySettings:
    """What to run, where, and how patient to be about it."""

    command: str
    cwd: str = "."
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS
    # Whether to skip the check when the turn changed nothing. True for the
    # user's own `verify_command`, which is a regression guard: running a test
    # suite because somebody asked a question would be absurd, and on a slow
    # suite hostile.
    #
    # False for a subagent's acceptance check, which is a different thing
    # wearing the same clothes — it is the *definition of done* for that
    # ticket, so a worker that changed nothing has definitively not finished
    # and must be told so. Skipping it there would report "nobody said what
    # done looks like" for a worker that simply did nothing.
    only_after_changes: bool = True
    # The turn budget for one fix attempt — smaller than a normal run's,
    # because fixing a named failure is a narrower job than the original task.
    max_turns: int = 4


@dataclass
class VerifyResult:
    """The outcome of running the project's verification command."""

    command: str
    ok: bool
    output: str
    timed_out: bool = False
    error: str | None = None

    def describe(self) -> str:
        if self.error:
            return f"Could not run `{self.command}`: {self.error}"
        if self.timed_out:
            return f"`{self.command}` timed out."
        return f"`{self.command}` {'passed' if self.ok else 'failed'}."

    def as_turn(self) -> str:
        """What the model is told when verification fails."""
        return (
            f"VERIFICATION FAILED. The project's own check, `{self.command}`, does not pass "
            f"after your changes:\n\n{self.output}\n\n"
            "Fix the cause. If the failure is unrelated to what you changed, say so plainly "
            "rather than changing unrelated code to make it go away."
        )


def _tail(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    """The end of the output, which is where the summary lives."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"[…{len(text) - limit} characters of earlier output omitted]\n" + text[-limit:]


def run_verification(
    command: str, cwd: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> VerifyResult:
    """Run ``command`` in ``cwd`` and report whether it passed."""
    if not command.strip():
        # `subprocess.run("", shell=True)` exits 0, which would report a check
        # that never ran as a passing one — the worst possible answer.
        return VerifyResult(command, ok=False, output="", error="no command configured")
    try:
        process = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            start_new_session=(os.name == "posix"),
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(command, ok=False, output="", timed_out=True)
    except Exception as exc:  # noqa: BLE001 - a broken command is not a crash
        return VerifyResult(command, ok=False, output="", error=str(exc))

    combined = _tail(f"{process.stdout}{process.stderr}")
    return VerifyResult(command, ok=process.returncode == 0, output=combined)
