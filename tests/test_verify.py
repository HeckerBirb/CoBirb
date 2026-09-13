"""Tests for running the project's own check after the agent changes things."""
from __future__ import annotations

import json
import sys

from conftest import write_config

from cobirb.config import Config
from cobirb.orchestrator import Orchestrator
from cobirb.plugins.core.tools import ToolRegistry
from cobirb.policy import Policy
from cobirb.runtime.verify import VerifySettings, run_verification
from cobirb.runtime.wiring import _verify_settings
from cobirb.typing.spi import ToolCall


def test_a_passing_command_reports_success(tmp_path):
    result = run_verification(f'"{sys.executable}" -c "raise SystemExit(0)"', str(tmp_path))

    assert result.ok
    assert "passed" in result.describe()


def test_a_failing_command_carries_its_output_back(tmp_path):
    result = run_verification(
        f'"{sys.executable}" -c "print(\'THE FAILURE DETAIL\'); raise SystemExit(1)"',
        str(tmp_path),
    )

    assert not result.ok
    assert "THE FAILURE DETAIL" in result.output
    assert "THE FAILURE DETAIL" in result.as_turn()


def test_a_hanging_command_times_out_rather_than_holding_the_turn(tmp_path):
    """A verification that outlives the attention span it was meant to serve
    is worse than none — the user is watching a turn not finish."""
    result = run_verification(f'"{sys.executable}" -c "import time; time.sleep(30)"',
                              str(tmp_path), timeout=1)

    assert result.timed_out and not result.ok
    assert "timed out" in result.describe()


def test_a_command_that_cannot_run_is_reported_not_raised(tmp_path):
    result = run_verification("", str(tmp_path))

    assert not result.ok


def test_long_output_keeps_the_tail_where_the_summary_lives(tmp_path):
    script = 'for i in range(4000): print("noise", i)\nprint("SUMMARY LINE")\nraise SystemExit(1)'
    result = run_verification(f'"{sys.executable}" -c \'{script}\'', str(tmp_path))

    assert "SUMMARY LINE" in result.output
    assert "omitted" in result.output


# --------------------------------------------------------------------------- #
# Wiring into a turn.
# --------------------------------------------------------------------------- #
class _EditingModel:
    """Edits a file once, then answers."""

    def __init__(self, path):
        self.path = path
        self.done = False

    def chat(self, *args, **kwargs):
        return "" if not self.done else "I changed it."

    def parse_tool_calls(self, raw):
        if self.done:
            return []
        self.done = True
        return [ToolCall("write_file", {"path": self.path, "content": "changed\n"})]

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False

    def context_window(self):
        return 8192


class _AnsweringModel(_EditingModel):
    """Never calls a tool — the "asked a question" case."""

    def chat(self, *args, **kwargs):
        return "Nothing needed changing."

    def parse_tool_calls(self, raw):
        return []


def _orchestrator(tmp_path, model, command):
    policy = Policy()
    policy.allow("write_file")
    return Orchestrator(
        model=model,
        tools=ToolRegistry(str(tmp_path)).tools,
        policy=policy,
        verify=VerifySettings(command=command, cwd=str(tmp_path), max_fix_attempts=0),
    )


def test_verification_runs_after_a_turn_that_changed_a_file(tmp_path):
    orchestrator = _orchestrator(
        tmp_path, _EditingModel("a.txt"), f'"{sys.executable}" -c "raise SystemExit(0)"'
    )

    orchestrator.run("change it", "sys", cwd=str(tmp_path))

    assert orchestrator.last_verification is not None
    assert orchestrator.last_verification.ok


def test_verification_does_not_run_when_nothing_changed(tmp_path):
    """Running a test suite because someone asked a question would be
    absurd, and on a slow suite actively hostile."""
    orchestrator = _orchestrator(
        tmp_path, _AnsweringModel("a.txt"), f'"{sys.executable}" -c "raise SystemExit(1)"'
    )

    orchestrator.run("what does this do?", "sys", cwd=str(tmp_path))

    assert orchestrator.last_verification is None


def test_an_acceptance_check_runs_even_when_nothing_changed(tmp_path):
    """A subagent's check is the *definition of done* for its ticket, not a
    regression guard — so a worker that changed nothing has definitively not
    finished and has to be told, rather than reported as unverified.

    The user's own ``verify_command`` keeps the opposite default; this is the
    one setting that separates the two meanings.
    """
    orchestrator = Orchestrator(
        model=_AnsweringModel("a.txt"),
        tools=ToolRegistry(str(tmp_path)).tools,
        policy=Policy(),
        verify=VerifySettings(
            command=f'"{sys.executable}" -c "raise SystemExit(1)"',
            cwd=str(tmp_path),
            max_fix_attempts=0,
            only_after_changes=False,
        ),
    )

    orchestrator.run("do the ticket", "sys", cwd=str(tmp_path))

    assert orchestrator.last_verification is not None
    assert not orchestrator.last_verification.ok


def test_a_failure_is_handed_back_for_the_model_to_fix(tmp_path):
    """The point of the loop: the model gets told, in the same turn, that its
    change broke the project's own check."""
    seen: list[str] = []

    class _Reacting(_EditingModel):
        def chat(self, system, context, tools=None, *, stream=False):
            seen.append(context)
            return "" if not self.done else "fixed"

    policy = Policy()
    policy.allow("write_file")
    orchestrator = Orchestrator(
        model=_Reacting("a.txt"),
        tools=ToolRegistry(str(tmp_path)).tools,
        policy=policy,
        verify=VerifySettings(
            command=f'"{sys.executable}" -c "print(\'BROKEN\'); raise SystemExit(1)"',
            cwd=str(tmp_path),
            max_fix_attempts=1,
        ),
    )

    orchestrator.run("change it", "sys", cwd=str(tmp_path))

    assert any("VERIFICATION FAILED" in context for context in seen)
    assert any("BROKEN" in context for context in seen)


def test_the_fix_loop_is_bounded(tmp_path):
    """A model that cannot fix a failing suite in one focused attempt is not
    usually one more turn away, and every round is model time nobody asked
    for."""
    runs: list[int] = []

    class _NeverFixes(_EditingModel):
        def chat(self, *args, **kwargs):
            runs.append(1)
            return "" if not self.done else "I tried."

    policy = Policy()
    policy.allow("write_file")
    orchestrator = Orchestrator(
        model=_NeverFixes("a.txt"),
        tools=ToolRegistry(str(tmp_path)).tools,
        policy=policy,
        verify=VerifySettings(
            command=f'"{sys.executable}" -c "raise SystemExit(1)"',
            cwd=str(tmp_path),
            max_fix_attempts=1,
        ),
    )

    orchestrator.run("change it", "sys", cwd=str(tmp_path))

    assert len(runs) < 12  # bounded, not spinning


# --------------------------------------------------------------------------- #
# Configuration.
# --------------------------------------------------------------------------- #
def test_verification_is_off_unless_a_command_is_named(tmp_path):
    """No guessing from the project layout: guessing wrong means running an
    arbitrary command the user never asked for, after every turn."""
    assert _verify_settings(str(tmp_path), Config()) is None


def test_a_configured_command_turns_it_on(tmp_path):
    write_config(tmp_path, json.loads('{"verify_command": "pytest -q", "verify_timeout": 30}'))

    settings = _verify_settings(str(tmp_path), Config())

    assert settings is not None
    assert settings.command == "pytest -q"
    assert settings.timeout == 30


def test_a_blank_command_is_the_same_as_none(tmp_path):
    write_config(tmp_path, json.loads('{"verify_command": "   "}'))

    assert _verify_settings(str(tmp_path), Config()) is None
