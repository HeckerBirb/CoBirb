"""Tests for a whole flock session, end to end.

The approval gate is the important one. It is the only place a person sees
what a flock is about to be allowed to touch, so every path that reaches a
Worker Birb has to go through it.
"""
from __future__ import annotations

import sys
import textwrap

import pytest

from cobirb import cli
from cobirb.flock.brainy import PROPOSE_CHARTER
from cobirb.flock.run import Asker, run_flock_session
from cobirb.flock.worker import WorkerReport
from cobirb.typing.spi import ToolCall


def _charter_toml(tmp_path, workers=2):
    entries = "\n".join(
        textwrap.dedent(f"""
        [[workers]]
        id     = "{name}"
        writes = ["{name}.py", "test_{name}.py"]
        tests  = ["test_{name}.py"]
        accept = '"{sys.executable}" -m pytest test_{name}.py -q'
        brief  = "Implement {name}."
        """)
        for name in ("a", "b")[:workers]
    )
    return f'objective = "two things"\nconcurrency = 2\n{entries}'


class _ScriptedBrainy:
    """A Brainy Birb that proposes a fixed charter, then reports."""

    def __init__(self, toml, propose=True):
        self._toml = toml
        self._propose = propose
        self._done = False

    def name(self):
        return "scripted-brainy"

    def chat(self, system, context, tools=None, *, stream=False):
        return "Here is the plan." if not self._done else "All done."

    def parse_tool_calls(self, reply):
        if self._propose and not self._done:
            self._done = True
            return [ToolCall(name=PROPOSE_CHARTER, arguments={"toml": self._toml})]
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def _orchestrator(monkeypatch, tmp_path, model):
    from cobirb.runtime import wiring
    from cobirb.runtime.personas import load_persona

    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)
    return wiring.build_orchestrator(str(tmp_path), load_persona(None), {})


def _skeleton(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text(f"def {name}(n):\n    raise NotImplementedError\n")
        (tmp_path / f"test_{name}.py").write_text(
            f"from {name} import {name}\n\ndef test_it():\n    assert {name}(2) == 4\n"
        )


def _honest_workers(monkeypatch, tmp_path):
    from cobirb.flock import supervisor

    def run(worker, cwd, **kwargs):
        (tmp_path / f"{worker.id}.py").write_text(f"def {worker.id}(n):\n    return n * 2\n")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True, summary="did it")

    monkeypatch.setattr(supervisor, "run_worker", run)


# --------------------------------------------------------------------------- #
# The approval gate
# --------------------------------------------------------------------------- #
def test_nothing_runs_until_the_charter_is_approved(monkeypatch, tmp_path):
    """The single human decision point. A charter that ran without it would be
    an agent granting itself permissions."""
    _skeleton(tmp_path)
    ran = []
    from cobirb.flock import supervisor

    monkeypatch.setattr(
        supervisor, "run_worker",
        lambda worker, cwd, **k: ran.append(worker.id) or WorkerReport(worker.id, True),
    )
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_charter_toml(tmp_path)))

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q: False), probe=False,
    )

    assert ran == []
    assert run.stopped_at == "approval"
    assert not run.ran


def test_a_run_with_nobody_to_ask_approves_nothing(monkeypatch, tmp_path):
    """The default Asker refuses. A flock that could not find anyone to ask
    must not decide for itself."""
    _skeleton(tmp_path)
    ran = []
    from cobirb.flock import supervisor

    monkeypatch.setattr(
        supervisor, "run_worker",
        lambda worker, cwd, **k: ran.append(worker.id) or WorkerReport(worker.id, True),
    )
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_charter_toml(tmp_path)))

    run_flock_session(orchestrator, "do the thing", str(tmp_path), probe=False)

    assert ran == []


def test_approving_the_charter_fans_the_work_out(monkeypatch, tmp_path):
    _skeleton(tmp_path)
    _honest_workers(monkeypatch, tmp_path)
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_charter_toml(tmp_path)))

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q: True), probe=False,
    )

    assert run.ran
    assert {r.worker_id for r in run.outcome.reports} == {"a", "b"}
    assert "return n * 2" in (tmp_path / "a.py").read_text()


def test_the_user_is_shown_the_scopes_before_being_asked(monkeypatch, tmp_path):
    """Approving a partition you cannot see is not approval."""
    _skeleton(tmp_path)
    _honest_workers(monkeypatch, tmp_path)
    shown = []
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_charter_toml(tmp_path)))

    run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q: True, show=shown.append), probe=False,
    )

    everything = "\n".join(shown)
    assert "a.py" in everything and "b.py" in everything
    assert "Worker Birb(s)" in everything


# --------------------------------------------------------------------------- #
# When Brainy Birb declines to divide the work
# --------------------------------------------------------------------------- #
def test_no_charter_is_a_legitimate_answer_not_a_failure(monkeypatch, tmp_path):
    """"This is a single person's job, do not fan it out" is the right answer
    for plenty of work, and the narration is where it says so."""
    orchestrator = _orchestrator(
        monkeypatch, tmp_path, _ScriptedBrainy("", propose=False)
    )

    run = run_flock_session(
        orchestrator, "rename one variable", str(tmp_path),
        ask=Asker(confirm=lambda q: True), probe=False,
    )

    assert run.charter is None
    assert run.stopped_at == "planning"
    assert not run.ran


# --------------------------------------------------------------------------- #
# An overlapping partition
# --------------------------------------------------------------------------- #
_OVERLAPPING = """
objective = "x"
[[workers]]
id = "a"
writes = ["shared.py"]
brief = "go"
[[workers]]
id = "b"
writes = ["shared.py"]
brief = "go"
"""


def test_an_overlapping_partition_stops_unless_the_user_says_otherwise(monkeypatch, tmp_path):
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_OVERLAPPING))

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q: False), probe=False,
    )

    assert run.stopped_at == "partition"
    assert "both write it" in run.report


def test_carrying_on_past_an_overlap_drops_to_one_worker_at_a_time(monkeypatch, tmp_path):
    """Honouring the choice means removing the thing that made it unsafe.
    Overlapping scopes plus concurrency is the one combination with no
    defensible behaviour."""
    seen = {}
    from cobirb.flock import supervisor

    def fake_run_flock(charter, cwd, **kwargs):
        seen.update(kwargs)
        from cobirb.flock.supervisor import FlockOutcome

        return FlockOutcome(charter=charter)

    monkeypatch.setattr("cobirb.flock.run.run_flock", fake_run_flock)
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_OVERLAPPING))

    run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q: True), probe=False,
    )

    assert seen["concurrency"] == 1


# --------------------------------------------------------------------------- #
# The CLI surface
# --------------------------------------------------------------------------- #
def test_flock_refuses_to_run_headless(capsys, tmp_path):
    """A flock that approved its own charter would be an agent granting itself
    permissions — exactly what the permission layer exists to prevent."""
    status = cli.main(["flock", "-p", "do it", "--headless", "--cwd", str(tmp_path)])

    assert status != 0
    assert "cannot run headless" in capsys.readouterr().err


def test_flock_without_an_objective_says_what_it_needs(capsys, tmp_path):
    status = cli.main(["flock", "--cwd", str(tmp_path)])

    assert status != 0
    assert "needs an objective" in capsys.readouterr().err
