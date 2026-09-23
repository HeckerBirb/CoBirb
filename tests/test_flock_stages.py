"""Staged planning: the overview, the ticket stages, the gate, and the rounds.

Driven by a scripted Brainy Birb that answers each stage by what the stage
asks for, so these tests are about what the harness does with the answers —
which stages run, what is carried between them, when a round is approved,
and when the rounds stop.
"""
from __future__ import annotations

import os
import sys

import pytest

from cobirb.flock import stages
from cobirb.flock.run import Asker, FlockSettings, run_flock_session
from cobirb.flock.stages import TicketSpec, _GatedHooks, check_tickets, parse_tickets
from cobirb.flock.worker import WorkerReport
from cobirb.runtime.hooks import EVENT_BEFORE_TOOL, HookOutcome
from conftest import write_config

_BLOCKS = """
### ticket: a
- writes: a.py, test_a.py
- tests: test_a.py
- accept: "{py}" -m pytest test_a.py -q
- needs: none
- builds: doubles a number
- done when: test_a passes

### ticket: b
- writes: `b.py`, `test_b.py`
- tests: test_b.py
- accept: "{py}" -m pytest test_b.py -q
- needs: none
- builds: doubles a number too
- done when: test_b passes
""".replace("{py}", sys.executable)

@pytest.fixture(autouse=True)
def _no_preflight(monkeypatch):
    """The pre-flight model check asks the endpoint; nothing here is about it."""
    monkeypatch.setattr("cobirb.flock.run.missing_models", lambda *a, **k: "")


_PLAN = "## Job\nImplement it.\n" + "## Procedure\n1. Return n * 2.\n" * 20


class _StagedBrainy:
    """Answers each stage by the marker in the prompt it was given."""

    def __init__(self, tickets=_BLOCKS, evaluations=None):
        self.tickets = tickets
        self.evaluations = list(evaluations or [])
        self.prompts = []

    def name(self):
        return "staged-brainy"

    def chat(self, system, context, tools=None, *, stream=False):
        text = str(context)
        self.prompts.append(text)
        last = max(("Write the next section:", "Stage: the skeleton", "Stage: the plan for ticket",
                    "Stage: after round"), key=text.rfind)
        if last == "Write the next section:":
            heading = text[text.rfind(last) + len(last):].split("---")[0].strip()
            return self.tickets if heading == "Tickets" else f"The {heading} section."
        if last == "Stage: the plan for ticket":
            return _PLAN
        if last == "Stage: after round":
            return self.evaluations.pop(0) if self.evaluations else "NO TICKETS nothing left."
        return "Skeleton written."

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def _project(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text(f"def {name}(n):\n    raise NotImplementedError\n")
        (tmp_path / f"test_{name}.py").write_text(
            f"from {name} import {name}\n\ndef test_it():\n    assert {name}(2) == 4\n")


def _workers(monkeypatch, tmp_path, fail_first=()):
    """Workers that implement their stub — except, the first time each is
    run, the ones in ``fail_first``, which leave it."""
    from cobirb.flock import supervisor

    seen = set()

    def run(worker, cwd, **kwargs):
        if worker.id in fail_first and worker.id not in seen:
            seen.add(worker.id)
            return WorkerReport(worker_id=worker.id, ok=True, accepted=False,
                                structured={"tests_pass": False, "contract_kept": True,
                                            "missing": ["the body — ran out of ideas"],
                                            "test_contradicts": []})
        (tmp_path / f"{worker.id}.py").write_text(f"def {worker.id}(n):\n    return n * 2\n")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True, summary="did it")

    monkeypatch.setattr(supervisor, "run_worker", run)


def _orchestrator(monkeypatch, tmp_path, model):
    from cobirb.runtime import wiring

    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)
    return wiring.build_orchestrator(str(tmp_path), {})


def _staged(tmp_path, **flock):
    write_config(tmp_path, {"flock": {"planning": "staged", **flock}})


def _run(orchestrator, tmp_path, ask=None):
    return run_flock_session(
        orchestrator, "double two numbers", str(tmp_path),
        ask=ask or Asker(confirm=lambda q, detail="": True), probe=False,
    )


# --------------------------------------------------------------------------- #
# The ticket blocks
# --------------------------------------------------------------------------- #
def test_ticket_blocks_are_read_whatever_their_formatting():
    tickets = parse_tickets(_BLOCKS)

    assert [t.id for t in tickets] == ["a", "b"]
    assert tickets[1].writes == ("b.py", "test_b.py")
    assert tickets[0].tests == ("test_a.py",) and tickets[0].needs == ()


@pytest.mark.parametrize("blocks, problem", [
    ("no blocks here", "no ticket blocks"),
    ("### ticket: a\n- writes: a.py\n- accept: x", "names no test files"),
    ("### ticket: a\n- writes: a.py\n- tests: t.py", "no `accept`"),
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: x\n"
     "### ticket: b\n- writes: a.py\n- tests: tb.py\n- accept: x", "already written by ticket 'a'"),
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: x\n- needs: ghost", "ghost"),
])
def test_ticket_blocks_that_cannot_become_a_charter_say_why(blocks, problem):
    assert problem in check_tickets(parse_tickets(blocks))


def test_good_ticket_blocks_pass_the_check():
    assert check_tickets(parse_tickets(_BLOCKS)) == ""


# --------------------------------------------------------------------------- #
# The gate: a write outside the stage's files is refused before anyone is asked
# --------------------------------------------------------------------------- #
class _Base:
    def fire(self, event, **kwargs):
        return HookOutcome()


@pytest.mark.parametrize("path, blocked", [
    ("test_a.py", False),
    ("a.py", True),
    ("../outside.py", True),
    ("", True),
])
def test_the_gate_refuses_writes_outside_the_stage(tmp_path, path, blocked):
    gate = _GatedHooks(_Base(), str(tmp_path), lambda p: p == "test_a.py", "tests only")

    outcome = gate.fire(EVENT_BEFORE_TOOL, tool_name="write_file", arguments={"path": path})

    assert outcome.blocked is blocked


def test_the_gate_leaves_reads_alone(tmp_path):
    gate = _GatedHooks(_Base(), str(tmp_path), lambda p: False, "nothing")

    assert not gate.fire(EVENT_BEFORE_TOOL, tool_name="read_file", arguments={"path": "a.py"}).blocked


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("block, expected", [
    ({}, ("single", "ask", 5)),
    ({"planning": "staged", "autonomy": "auto", "max_rounds": 3}, ("staged", "auto", 3)),
    ({"planning": "stagd", "autonomy": "yolo", "max_rounds": "lots"}, ("single", "ask", 5)),
    ({"max_rounds": 0}, ("single", "ask", 5)),
])
def test_flock_settings_fall_back_on_anything_unreadable(tmp_path, block, expected):
    from cobirb.config import Config

    write_config(tmp_path, {"flock": block})
    settings = FlockSettings.from_config(Config())

    assert (settings.planning, settings.autonomy, settings.max_rounds) == expected


# --------------------------------------------------------------------------- #
# A whole staged round
# --------------------------------------------------------------------------- #
def test_a_staged_flock_runs_every_stage_and_the_workers(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert run.ran and len(run.rounds) == 1 and run.outcome.all_done
    steps = [entry["step"] for entry in run.trace]
    assert steps[:6] == [f"overview: {h}" for h, _ in stages.SECTIONS]
    assert "skeleton" in steps and "ticket: a" in steps and "ticket: b" in steps
    # The ticket plan is the brief the Worker Birb gets.
    assert run.charter.worker("a").brief.startswith("## Job")


def test_every_ticket_stage_carries_the_design_and_its_own_ticket_only(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    a_stage = next(p for p in brainy.prompts if "Stage: the plan for ticket 'a'" in p)
    assert "The Architecture section." in a_stage          # the design documents
    assert "double two numbers" in a_stage                  # and the request
    assert "Stage: the plan for ticket 'b'" not in a_stage  # a fresh context


def test_declining_to_divide_ends_planning(monkeypatch, tmp_path):
    _staged(tmp_path)
    brainy = _StagedBrainy(tickets="NO TICKETS — one function, one person.")

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert not run.ran and run.stopped_at == "planning"
    assert "one function" in run.report


def test_the_one_prompt_planner_is_the_default(monkeypatch, tmp_path):
    brainy = _StagedBrainy()

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path,
         ask=Asker(confirm=lambda q, detail="": False))

    assert not any("Write the next section:" in p for p in brainy.prompts)


# --------------------------------------------------------------------------- #
# Decisions and autonomy
# --------------------------------------------------------------------------- #
def test_ask_mode_puts_the_decisions_to_the_user_and_carries_the_answer(monkeypatch, tmp_path):
    _staged(tmp_path, autonomy="ask")
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()
    asked = []
    ask = Asker(confirm=lambda q, detail="": True,
                decide=lambda decisions: asked.append(decisions) or "1: use curses")

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path, ask=ask)

    assert asked == ["The Decisions section."]
    assert any("1: use curses" in p for p in brainy.prompts if "Stage: the plan for ticket" in p)


def test_auto_mode_refuses_to_start_without_a_sandbox(monkeypatch, tmp_path):
    _staged(tmp_path, autonomy="auto")
    orchestrator = _orchestrator(monkeypatch, tmp_path, _StagedBrainy())
    monkeypatch.setattr(type(orchestrator.tools["shell"].sandbox), "active",
                        property(lambda self: False))

    run = _run(orchestrator, tmp_path)

    assert run.stopped_at == "autonomy" and not run.ran


# --------------------------------------------------------------------------- #
# Rounds
# --------------------------------------------------------------------------- #
_REDO_B = """### ticket: b
- writes: b.py, test_b.py
- tests: test_b.py
- accept: "{py}" -m pytest test_b.py -q
- why: implement the body this time
""".replace("{py}", sys.executable)


def test_a_failed_ticket_is_planned_again_in_a_second_round(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})
    brainy = _StagedBrainy(evaluations=[_REDO_B])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert len(run.rounds) == 2 and run.outcome.all_done
    assert [w.id for w in run.rounds[1].charter.workers] == ["b"]
    second = [p for p in brainy.prompts if "Stage: the plan for ticket 'b'" in p][-1]
    assert "implement the body this time" in second and "ran out of ideas" in second


def test_ask_mode_asks_before_every_round(monkeypatch, tmp_path):
    _staged(tmp_path, autonomy="ask")
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})
    questions = []
    ask = Asker(confirm=lambda q, detail="": questions.append(q) or True)

    _run(_orchestrator(monkeypatch, tmp_path, _StagedBrainy(evaluations=[_REDO_B])), tmp_path, ask=ask)

    assert sum(q.startswith("Approve") for q in questions) == 2


def test_auto_mode_approves_later_rounds_without_asking(monkeypatch, tmp_path):
    _staged(tmp_path, autonomy="auto")
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})
    orchestrator = _orchestrator(monkeypatch, tmp_path, _StagedBrainy(evaluations=[_REDO_B]))
    monkeypatch.setattr(type(orchestrator.tools["shell"].sandbox), "active",
                        property(lambda self: True))
    questions = []
    ask = Asker(confirm=lambda q, detail="": questions.append(q) or True)

    run = _run(orchestrator, tmp_path, ask=ask)

    assert len(run.rounds) == 2
    assert sum(q.startswith("Approve") for q in questions) == 1  # the first charter only


def test_a_round_that_changes_nothing_stops_the_flock(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    from cobirb.flock import supervisor

    monkeypatch.setattr(supervisor, "run_worker", lambda worker, cwd, **k: WorkerReport(
        worker_id=worker.id, ok=True, accepted=False))
    brainy = _StagedBrainy(evaluations=[_REDO_B, _REDO_B, _REDO_B])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    # Round 2 retries only b, which fails exactly as it did; round 3 is the
    # first round to change nothing, so the flock stops after it.
    assert run.stopped_at == "no_progress" and len(run.rounds) == 3


def test_the_rounds_stop_at_the_cap(monkeypatch, tmp_path):
    _staged(tmp_path, max_rounds=1)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})

    run = _run(_orchestrator(monkeypatch, tmp_path, _StagedBrainy(evaluations=[_REDO_B])), tmp_path)

    assert len(run.rounds) == 1 and run.stopped_at == "rounds"


def test_approval_of_a_later_round_names_what_is_new():
    from cobirb.flock.charter import parse_charter

    first = parse_charter('objective = "x"\n[[workers]]\nid = "a"\nwrites = ["a.py"]\n'
                          'accept = "pytest"\nbrief = "go"')
    later = parse_charter('objective = "x"\n[[workers]]\nid = "a"\nwrites = ["a.py", "extra.py"]\n'
                          'accept = "pytest"\nbrief = "go"')

    text = stages.approval_changes(later, [first])

    assert "NEW: extra.py" in text and "NEW command" not in text


def test_ticket_blocks_round_trip_through_their_own_format():
    ticket = TicketSpec(id="a", writes=("a.py", "t.py"), tests=("t.py",), accept="pytest t.py",
                        needs=("b",), builds="x", done="y")

    assert parse_tickets(ticket.block())[0] == ticket


def test_a_ticket_stage_that_writes_no_tests_is_asked_again(monkeypatch, tmp_path):
    """The tests are the worker's acceptance criteria; a plan without them is
    not a finished stage."""
    _staged(tmp_path)
    (tmp_path / "a.py").write_text("def a(n):\n    raise NotImplementedError\n")
    (tmp_path / "b.py").write_text("def b(n):\n    raise NotImplementedError\n")
    brainy = _StagedBrainy()

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path,
         ask=Asker(confirm=lambda q, detail="": False))

    a_stages = [p for p in brainy.prompts if "Stage: the plan for ticket 'a'" in p]
    assert len(a_stages) == 2 and "test files were not written" in a_stages[-1]
    assert not os.path.exists(tmp_path / "test_a.py")
