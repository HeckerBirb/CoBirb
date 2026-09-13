"""Tests for running one Worker Birb.

These go through the real wiring — a real ``ToolRegistry`` writing real files
under a real ``Policy``, with only the model scripted. The claim being tested
is that a Worker Birb is an ordinary agent run held in place by its policy, so
a test that stubbed the policy would be testing nothing.
"""
from __future__ import annotations

import json

from conftest import write_config

from cobirb.flock.charter import parse_charter
from cobirb.flock.worker import WorkerReport, compose_brief, run_worker
from cobirb.typing.spi import ToolCall


class _ScriptedWorker:
    """A model that makes a fixed sequence of tool calls, then answers.

    Records the system prompt and context it was given, which is how a test
    can check that a worker really was told nothing about the wider project.
    """

    def __init__(self, calls, answer="Done."):
        self._calls = list(calls)
        self._answer = answer
        self.seen_context = []
        self.seen_system = []

    def name(self):
        return "scripted-worker"

    def chat(self, system, context, tools=None, *, stream=False):
        self.seen_system.append(system)
        self.seen_context.append(context)
        return "Working." if self._calls else self._answer

    def parse_tool_calls(self, reply):
        return [self._calls.pop(0)] if self._calls else []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def _brief(body):
    return parse_charter(body).workers[0]


def _scripted(monkeypatch, model):
    from cobirb.runtime import wiring

    monkeypatch.setattr(wiring, "build_for_role", lambda *a, **k: model)
    return model


# --------------------------------------------------------------------------- #
# The brief
# --------------------------------------------------------------------------- #
def test_the_brief_carries_the_one_rule_the_design_rests_on():
    """A worker that quietly widens a signature breaks colleagues it cannot
    see. That instruction has to be in front of it every time."""
    text = compose_brief(_brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["a.py"]
        brief = "Implement parse()."
    """))

    assert "DO NOT CHANGE THEM" in text
    assert "Implement parse()." in text


def test_the_brief_states_the_boundaries_the_policy_enforces():
    """Both, deliberately: the policy makes the isolation true, and saying so
    stops the worker spending turns discovering it by being refused."""
    text = compose_brief(_brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["a.py"]
        reads = ["types.py"]
        accept = "pytest -q"
        brief = "go"
    """))

    assert "may change: a.py" in text
    assert "must not change: types.py" in text
    assert "pytest -q" in text


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #
def test_a_worker_writes_the_file_it_owns(monkeypatch, tmp_path):
    model = _scripted(monkeypatch, _ScriptedWorker([
        ToolCall(name="write_file", arguments={"path": "mine.py", "content": "def parse(): ...\n"}),
    ]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "Implement parse()."
    """)

    report = run_worker(worker, str(tmp_path))

    assert report.ok
    assert (tmp_path / "mine.py").read_text() == "def parse(): ...\n"
    assert report.denied == ()
    assert model.seen_context  # it really went through a model


def test_a_worker_is_refused_a_file_outside_its_brief(monkeypatch, tmp_path):
    """The isolation is the policy. Nothing about this depends on the model
    choosing to respect the boundary it was told about."""
    _scripted(monkeypatch, _ScriptedWorker([
        ToolCall(name="write_file", arguments={"path": "theirs.py", "content": "sneaky"}),
    ]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "go"
    """)

    report = run_worker(worker, str(tmp_path))

    assert not (tmp_path / "theirs.py").exists()
    assert report.denied == ("write_file",)


def test_reaching_outside_scope_is_reported_as_a_finding_about_the_charter(
    monkeypatch, tmp_path
):
    """A worker that tried to open something it could not is usually telling
    you the brief was incomplete, not misbehaving."""
    _scripted(monkeypatch, _ScriptedWorker([
        ToolCall(name="read_file", arguments={"path": "somewhere_else.py"}),
    ]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "go"
    """)

    report = run_worker(worker, str(tmp_path))

    assert "outside its scope" in report.describe()


def test_a_worker_is_told_nothing_about_the_project(monkeypatch, tmp_path):
    """"Nothing but its brief" has to be true of what actually reaches the
    model, not only of what the design says. AGENTS.md and the repo map are
    both on by default for a normal run."""
    (tmp_path / "AGENTS.md").write_text("SECRET HOUSE CONVENTIONS")
    (tmp_path / "elsewhere.py").write_text("class SomebodyElsesClass: pass\n")
    model = _scripted(monkeypatch, _ScriptedWorker([]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "Implement parse()."
    """)

    run_worker(worker, str(tmp_path))

    everything = " ".join(model.seen_system) + " ".join(model.seen_context)
    assert "SECRET HOUSE CONVENTIONS" not in everything
    assert "SomebodyElsesClass" not in everything
    assert "Implement parse()." in everything


def test_the_acceptance_check_decides_whether_the_work_is_done(monkeypatch, tmp_path):
    _scripted(monkeypatch, _ScriptedWorker([
        ToolCall(name="write_file", arguments={"path": "mine.py", "content": "ok\n"}),
    ]))
    worker = _brief(f"""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        accept = "test -f {tmp_path / 'mine.py'}"
        brief = "go"
    """)

    report = run_worker(worker, str(tmp_path))

    assert report.accepted is True
    assert report.complete


def test_a_failing_acceptance_check_is_not_complete(monkeypatch, tmp_path):
    _scripted(monkeypatch, _ScriptedWorker([]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        accept = "false"
        brief = "go"
    """)

    report = run_worker(worker, str(tmp_path))

    assert report.accepted is False
    assert not report.complete
    assert "FAILED" in report.describe()


def test_no_acceptance_check_is_not_the_same_as_passing(monkeypatch, tmp_path):
    """Nobody said what done looks like, so this cannot claim the work is
    finished — an easy place to accidentally default to True."""
    _scripted(monkeypatch, _ScriptedWorker([]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "go"
    """)

    report = run_worker(worker, str(tmp_path))

    assert report.accepted is None
    assert not report.complete


def test_a_worker_that_blows_up_is_a_report_not_an_exception(monkeypatch, tmp_path):
    """One failed ticket must not take down the flock — Brainy Birb needs the
    fact in order to plan the next round."""
    class _Exploding(_ScriptedWorker):
        def chat(self, *args, **kwargs):
            raise RuntimeError("the endpoint went away")

    _scripted(monkeypatch, _Exploding([]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "go"
    """)

    report = run_worker(worker, str(tmp_path))

    assert isinstance(report, WorkerReport)
    assert not report.ok
    assert "the endpoint went away" in report.error
    assert "did not run" in report.describe()


def test_a_worker_uses_the_worker_model_role(monkeypatch, tmp_path):
    """Per-role models shipped in 0.4 precisely so the flock had somewhere to
    resolve from. This is the test that they are actually wired together."""
    write_config(tmp_path, {"models": {"default": {"name": "big"}, "worker": {"name": "small"}}})
    chosen = []

    from cobirb.runtime import wiring

    real = wiring.build_for_role
    monkeypatch.setattr(
        wiring, "build_for_role",
        lambda role, *a, **k: (chosen.append(role), real(role, *a, **k))[1],
    )
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "go"
    """)

    run_worker(worker, str(tmp_path))

    assert chosen == ["worker"]


def test_a_worker_obeys_the_users_own_hooks(monkeypatch, tmp_path):
    """It is an ordinary run, so the user's rules apply to it. Their hooks
    should govern every agent working in their tree, not only the ones they
    prompted themselves."""
    guard = tmp_path / "guard.sh"
    guard.write_text("#!/bin/sh\necho 'not that file'\nexit 1\n")
    guard.chmod(0o755)
    write_config(tmp_path, {"hooks": {"before_tool": [{"command": str(guard)}]}})
    _scripted(monkeypatch, _ScriptedWorker([
        ToolCall(name="write_file", arguments={"path": "mine.py", "content": "x"}),
    ]))
    worker = _brief("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["mine.py"]
        brief = "go"
    """)

    run_worker(worker, str(tmp_path))

    assert not (tmp_path / "mine.py").exists()
