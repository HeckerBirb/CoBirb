"""Staged planning: the overview, the ticket stages, the gate, and the rounds.

Driven by a scripted Brainy Birb that answers each stage by what the stage
asks for, so these tests are about what the harness does with the answers —
which stages run, what is carried between them, when a round is approved,
and when the rounds stop.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from cobirb.flock import stages
from cobirb.flock.run import Asker, FlockSettings, run_flock_session
from cobirb import sandbox
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
- builds: doubles the number
- done when: test_a passes

### ticket: b
- writes: `b.py`, `test_b.py`
- tests: test_b.py
- accept: "{py}" -m pytest test_b.py -q
- needs: none
- builds: doubles the number too
- done when: test_b passes
""".replace("{py}", sys.executable)

@pytest.fixture(autouse=True)
def _no_preflight(monkeypatch):
    """The pre-flight model check asks the endpoint; nothing here is about it."""
    monkeypatch.setattr("cobirb.flock.run.missing_models", lambda *a, **k: "")


# Long enough for the restatement's shortening check to apply to it.
_DETAIL = " It has detail." * 20

_PLAN = "## Job\nImplement it.\n" + "## Procedure\n1. Return n * 2.\n" * 20


def _restatement(tickets=_BLOCKS, names="- none"):
    """A restated design: every section reworded, the tickets as given."""
    parts = [f"## Names\n{names}", '## The request\nDouble two numbers, restated; print "OK".']
    for heading, _ in stages.SECTIONS:
        parts.append(f"## {heading}\n" + (tickets if heading == "Tickets" else f"The {heading} section, restated."
                                          + (_DETAIL if heading == "Architecture" else "")))
    return "\n\n".join(parts)


class _StagedBrainy:
    """Answers each stage by the marker in the prompt it was given."""

    def __init__(self, tickets=_BLOCKS, evaluations=None, restatements=None):
        self.tickets = tickets
        self.evaluations = list(evaluations or [])
        # Replies to "restate the design", in order; the last one repeats.
        self.restatements = list(restatements or [_restatement(tickets)])
        self.last_evaluation = ""
        self.prompts = []
        self.systems = []

    def name(self):
        return "staged-brainy"

    def chat(self, system, context, tools=None, *, stream=False):
        text = str(context)
        self.prompts.append(text)
        self.systems.append(str(system))
        last = max(("Write the next section:", "Stage: the skeleton", "Stage: the plan for ticket",
                    "Stage: after round", "Stage: restate the design", "Stage: restate the next round"),
                   key=text.rfind)
        if last == "Write the next section:":
            heading = text[text.rfind(last) + len(last):].split("---")[0].strip()
            if heading == "Architecture":
                return f"The {heading} section.{_DETAIL}"
            return self.tickets if heading == "Tickets" else f"The {heading} section."
        if last == "Stage: the plan for ticket":
            return _PLAN
        if last == "Stage: restate the design":
            return self.restatements.pop(0) if len(self.restatements) > 1 else self.restatements[0]
        if last == "Stage: restate the next round":
            return f"## Names\n- none\n\n## Tickets\n{self.last_evaluation}"
        if last == "Stage: after round":
            self.last_evaluation = self.evaluations.pop(0) if self.evaluations else "NO TICKETS nothing left."
            return self.last_evaluation
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


def _run(orchestrator, tmp_path, ask=None, request='double two numbers, printing "OK"'):
    return run_flock_session(
        orchestrator, request, str(tmp_path),
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
    ("### ticket: a\n- writes: a.py\n- accept: x", "has no test files"),
    ("### ticket: a\n- writes: a.py\n- tests: t.py", "no `accept`"),
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: x\n"
     "### ticket: b\n- writes: a.py\n- tests: tb.py\n- accept: x", "listed by more than one ticket"),
    # Another ticket's test file under `tests` — "the tests I must pass".
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: x\n"
     "### ticket: b\n- writes: b.py\n- tests: tb.py, ta.py\n- accept: x", "never another ticket's"),
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true\n- needs: ghost", "ghost"),
    # The language's name where the program goes — a real flock's `accept`.
    ("### ticket: a\n- writes: a.c\n- tests: ta.c\n- accept: c a.c ta.c", "`c`, which is not a program"),
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true && no-such-program-here", "no-such-program-here"),
    ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true $(x)", "cannot be read"),
])
def test_ticket_blocks_that_cannot_become_a_charter_say_why(blocks, problem):
    assert problem in check_tickets(parse_tickets(blocks))


@pytest.mark.parametrize("accept", [
    "true",
    "cd sub && FLAG=1 true",
    # A program given as a path may be built by the command itself.
    "./build.sh && /tmp/test_a",
])
def test_an_accept_command_naming_real_programs_passes_the_check(accept):
    assert check_tickets(parse_tickets(f"### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: {accept}")) == ""


@pytest.mark.parametrize("line", [
    "- writes: a.py",
    "- **writes**: a.py",
    "- **writes:** a.py",
    "* __writes__ : a.py",
])
def test_a_field_is_read_whether_or_not_its_name_is_bold(line):
    assert parse_tickets(f"### ticket: a\n{line}\n")[0].writes == ("a.py",)


@pytest.mark.parametrize("block, tests", [
    ("### ticket: a\n- writes: a.py, tests/test_a.py\n- tests: tests/test_a.py", ("tests/test_a.py",)),
    # No `tests` line: the test files it writes are its tests.
    ("### ticket: a\n- writes: a.py, tests/test_a.py", ("tests/test_a.py",)),
    ("### ticket: a\n- writes: a.py, a_test.py", ("a_test.py",)),
    ("### ticket: a\n- writes: a.py", ()),
])
def test_a_tickets_tests_are_read_from_its_block(block, tests):
    assert parse_tickets(block)[0].tests == tests


@pytest.mark.parametrize("line, expected", [
    ("- requires: zlib headers — check: pkg-config --exists zlib", ("zlib headers", "pkg-config --exists zlib")),
    ("- **requires**: requests, check: `python3 -c \"import requests\"`",
     ("requests", 'python3 -c "import requests"')),
    ("- requires: zlib", ("zlib", "")),
])
def test_a_requirement_is_read_with_its_check(line, expected):
    ticket = parse_tickets(f"### ticket: a\n- writes: a.c\n{line}\n")[0]

    assert ticket.requires == (expected,)
    assert parse_tickets(ticket.block())[0].requires == (expected,)


@pytest.mark.parametrize("line, problem", [
    ("- requires: zlib", "has no check"),
    ("- requires: zlib — check: no-such-probe --exists zlib", "`no-such-probe`, which is not a program"),
])
def test_a_requirement_needs_a_check_that_can_run(line, problem):
    blocks = f"### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true\n{line}"

    assert problem in check_tickets(parse_tickets(blocks))


def _requiring(*checks):
    return [TicketSpec(id="a", writes=("a.c",), tests=("ta.c",), accept="true",
                       requires=tuple((f"dep{i}", c) for i, c in enumerate(checks)))]


def test_requirements_are_not_run_where_commands_would_be_asked_about(tmp_path):
    """Brainy Birb's checks run only where contained commands already run unasked."""
    main = SimpleNamespace(tools={}, policy=SimpleNamespace(sandbox_auto=False))

    results = stages.check_requirements(main, _requiring("true"), str(tmp_path))

    assert results == [("a", "dep0", stages.UNCHECKED)]
    assert "not checked" in stages.describe_requirements(results)


@pytest.mark.skipif(sandbox.find_bwrap() is None, reason="bubblewrap not usable here")
def test_requirements_are_checked_inside_the_sandbox(tmp_path):
    box = sandbox.from_config("auto", str(tmp_path))
    main = SimpleNamespace(tools={"shell": SimpleNamespace(sandbox=box)},
                           policy=SimpleNamespace(sandbox_auto=True))

    results = stages.check_requirements(main, _requiring("true", "false"), str(tmp_path))

    assert [status for _, _, status in results] == [stages.INSTALLED, stages.MISSING]
    assert "dep1 — MISSING" in stages.describe_requirements(results)
    assert "dep0" not in stages.describe_requirements(results)


def test_the_charter_approval_says_what_is_not_installed(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    blocks = _BLOCKS.replace("- needs: none", "- requires: zlib — check: false\n- needs: none", 1)
    details = []

    _run(_orchestrator(monkeypatch, tmp_path, _StagedBrainy(tickets=blocks)), tmp_path,
         ask=Asker(confirm=lambda q, detail="": details.append(detail) or False))

    # Missing, or not checked where no sandbox runs commands unasked — named either way.
    assert "zlib" in details[0] and "Needed on this machine" in details[0]


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
    ({}, ("staged", "ask", 5)),
    ({"planning": "staged", "autonomy": "auto", "max_rounds": 3}, ("staged", "auto", 3)),
    ({"planning": "stagd", "autonomy": "yolo", "max_rounds": "lots"}, ("staged", "ask", 5)),
    ({"max_rounds": 0}, ("staged", "ask", 5)),
    ({"planning": "single"}, ("single", "ask", 5)),
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
    assert steps[:len(stages.SECTIONS) + 1] == [f"overview: {h}" for h, _ in stages.SECTIONS] + ["restate the design"]
    assert "skeleton" in steps and "ticket: a" in steps and "ticket: b" in steps
    # Architect Birb's ticket plan is the brief the Worker Birb gets, as written.
    assert run.charter.worker("a").brief == _PLAN.strip()


@pytest.mark.parametrize("marker", ["Stage: the skeleton", "Stage: the plan for ticket 'a'"])
def test_architect_birb_sees_only_the_restated_design(monkeypatch, tmp_path, marker):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()
    orchestrator = _orchestrator(monkeypatch, tmp_path, brainy)
    orchestrator.project_context = "PROJECT NOTES in the planner's own terms"

    _run(orchestrator, tmp_path)

    index = next(i for i, p in enumerate(brainy.prompts) if marker in p)
    prompt, system = brainy.prompts[index], brainy.systems[index]
    assert "Double two numbers, restated;" in prompt and "The Architecture section, restated." in prompt
    assert "double two numbers" not in prompt and "The Architecture section." not in prompt
    assert "PROJECT NOTES" not in system + prompt


def test_every_ticket_stage_carries_its_own_ticket_only(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    a_stage = next(p for p in brainy.prompts if "Stage: the plan for ticket 'a'" in p)
    assert "Stage: the plan for ticket 'b'" not in a_stage  # a fresh context


def test_the_front_end_is_told_which_agent_each_planning_stage_is(monkeypatch, tmp_path):
    """Architect Birb's stages share Brainy Birb's front-end; it is told whose they are."""
    _staged(tmp_path)
    speakers = []

    _run(_orchestrator(monkeypatch, tmp_path, _StagedBrainy()), tmp_path,
         ask=Asker(confirm=lambda q, detail="": False, speaking=speakers.append))

    first_architect = speakers.index("Architect Birb")
    assert set(speakers[:first_architect]) == {"Brainy Birb"}  # overview and restatement
    assert set(speakers[first_architect:]) == {"Architect Birb"}  # skeleton and ticket stages


def test_the_restatement_renames_what_the_workers_see_and_the_user_is_shown_the_names(monkeypatch, tmp_path):
    _staged(tmp_path)
    for name in ("double_a", "b"):
        (tmp_path / f"{name}.py").write_text(f"def {name}(n):\n    raise NotImplementedError\n")
        (tmp_path / f"test_{name}.py").write_text(f"from {name} import {name}\n\ndef test_it():\n    assert {name}(2) == 4\n")
    renamed = _BLOCKS.replace("ticket: a", "ticket: double_a").replace("a.py", "double_a.py")
    brainy = _StagedBrainy(restatements=[_restatement(renamed, "- renamed: a -> double_a\n- kept: b")])
    details = []

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path,
               ask=Asker(confirm=lambda q, detail="": details.append(detail) or False))

    assert [w.id for w in run.charter.workers] == ["double_a", "b"]
    assert run.charter.worker("double_a").writes == ("double_a.py", "test_double_a.py")
    assert "a → double_a" in details[0] and "b  (yours, kept)" in details[0]
    assert details[0].names.renamed == {"a": "double_a"}


_SURVIVOR = _BLOCKS.replace("ticket: a", "ticket: double_a")  # renamed, but a.py is still there


@pytest.mark.parametrize("reply, problem", [
    ("## Names\n- none", "sections are missing"),
    (_restatement(_SURVIVOR, "- renamed: a.py -> double_a.py"), "still appear"),
    # Renamed without saying so: the tickets no longer match Brainy Birb's.
    (_restatement(_BLOCKS.replace("ticket: a", "ticket: double_a")), "same tickets under their new ids"),
    (_restatement("no tickets at all"), "restated tickets cannot be used"),
    # A section summarised rather than restated: the overview's is far longer.
    (_restatement().replace("The Architecture section, restated." + _DETAIL, "Parts."), "much shorter than the original"),
    # A value the request quotes ("OK"), not copied exactly.
    (_restatement().replace('print "OK"', "print OK"), "exact values"),
    # A literal name pytest never collects.
    (_restatement(_BLOCKS.replace("test_a.py", "check_a.py"), "- renamed: test_a.py -> check_a.py"),
     "lost the `test_` prefix"),
])
def test_a_restatement_that_cannot_be_used_is_asked_for_again_then_stops_the_flock(
        monkeypatch, tmp_path, reply, problem):
    _staged(tmp_path)
    brainy = _StagedBrainy(restatements=[reply])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    failed = [t for t in run.trace if t["step"] == "restate the design" and t.get("problem")]
    assert run.stopped_at == "restatement" and not run.ran
    assert len(failed) == stages.SECTION_ATTEMPTS and problem in failed[0]["problem"]
    assert problem in run.report


def test_a_restatement_that_loses_the_ticket_blocks_is_shown_the_blocks_expected(monkeypatch, tmp_path):
    _staged(tmp_path)
    broken = _restatement("the tickets, in prose", "- renamed: a -> double_a")
    brainy = _StagedBrainy(restatements=[broken, _restatement()])

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path, ask=Asker(confirm=lambda q, detail="": False))

    retry = [p for p in brainy.prompts if "Stage: restate the design" in p][-1]
    assert "### ticket: double_a" in retry and "- writes: a.py, test_a.py" in retry


def test_a_restatement_asked_again_can_succeed(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy(restatements=["## Names\n- none", _restatement()])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert run.ran and run.outcome.all_done
    retry = [p for p in brainy.prompts if "Stage: restate the design" in p][-1]
    assert "could not be used" in retry


@pytest.mark.parametrize("text, expected", [
    ("## Names\n- none\n## Tickets\nx", {"Names": "- none", "Tickets": "x"}),
    # A sub-heading of Brainy Birb's own is content, even as a section's first line.
    ("## Names\n- none\n## Tickets\n## Parts\nx", {"Names": "- none", "Tickets": "## Parts\nx"}),
    ("## names\n- none\n## TICKETS\nx", {"Names": "- none", "Tickets": "x"}),
    ("## Names\na\n## Names\nb", {"Names": "a\n## Names\nb"}),   # a repeat is content
    ("## Tickets\nx", {"Tickets": "x"}),                             # a missing one is absent
])
def test_a_restatement_splits_only_at_the_headings_asked_for(text, expected):
    assert stages.split_sections(text, ("Names", "Tickets")) == expected


def test_sub_headings_in_the_design_survive_the_restatement(monkeypatch, tmp_path):
    """The overview is free Markdown; its own sub-headings reach the
    restatement, which must not read them as sections of its own."""
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    sub_headed = _restatement().replace("## Architecture\n", "## Architecture\n## Components\n")
    brainy = _StagedBrainy(restatements=[sub_headed])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert run.ran and run.outcome.all_done
    skeleton = next(p for p in brainy.prompts if "Stage: the skeleton" in p)
    assert "## Components" in skeleton and "The Architecture section, restated." in skeleton


# --------------------------------------------------------------------------- #
# The name mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("section, renamed, kept", [
    ("- none", {}, []),
    ("- renamed: `kill` -> `send_sigterm`", {"kill": "send_sigterm"}, []),
    ("- renamed: kill → send_sigterm\n- kept: /kill", {"kill": "send_sigterm"}, ["/kill"]),
    ("* Renamed: a => b\n- kept: none", {"a": "b"}, []),
    ("- renamed: same -> same\n- renamed: broken", {}, []),
    ("- kept: `Store` class\n- renamed: `tick()` method -> `advance()` method", {"tick()": "advance()"}, ["Store"]),
    ("- renamed: a -> `ticket: a`\n- renamed: b -> ### ticket: c", {"b": "c"}, []),
])
def test_the_names_section_is_read(section, renamed, kept):
    names = stages.parse_names(section)

    assert (names.renamed, names.kept) == (renamed, kept)


@pytest.mark.parametrize("text, survivors", [
    ("call send_sigterm on the child", []),
    ("call kill on the child", ["kill"]),
    ("the user's /kill command stays", []),           # kept names do not count
    ("SIGKILL and kill_all and skill", []),           # a name only as a whole word
    ("see kill.py", ["kill"]),
])
def test_a_renamed_name_is_found_wherever_it_survives(text, survivors):
    names = stages.NameMap({"kill": "send_sigterm"}, ["/kill"])

    assert names.survivors(text) == survivors


@pytest.mark.parametrize("first, second, problem", [
    ({"a": "b"}, {"a": "c"}, "already renamed"),
    ({"a": "c"}, {"b": "c"}, "both renamed"),
    ({"a": "b"}, {"c": "d"}, ""),
])
def test_names_merge_only_when_they_agree(first, second, problem):
    _, found = stages.NameMap(first).merged(stages.NameMap(second))

    assert (problem in found) if problem else found == ""


def test_declining_to_divide_ends_planning(monkeypatch, tmp_path):
    _staged(tmp_path)
    brainy = _StagedBrainy(tickets="NO TICKETS — one function, one person.")

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert not run.ran and run.stopped_at == "planning"
    assert "one function" in run.report


def test_staged_planning_is_the_default(monkeypatch, tmp_path):
    brainy = _StagedBrainy()

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path,
         ask=Asker(confirm=lambda q, detail="": False))

    assert any("Write the next section:" in p for p in brainy.prompts)


def test_the_one_prompt_planner_is_one_setting_away(monkeypatch, tmp_path):
    write_config(tmp_path, {"flock": {"planning": "single"}})
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
    # The answer is part of the design Brainy Birb restates for Architect Birb.
    assert any("1: use curses" in p for p in brainy.prompts if "Stage: restate the design" in p)


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


def test_every_planning_stage_is_told_the_machine_it_plans_for(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()

    _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    for marker in ("Write the next section: Tickets", "Stage: the skeleton", "Stage: the plan for ticket"):
        prompt = next(p for p in brainy.prompts if marker in p)
        assert "--- This machine ---" in prompt and "Operating system:" in prompt, marker


def test_what_cannot_be_done_on_this_machine_reaches_the_report(monkeypatch, tmp_path):
    """The design's limits, and an evaluation's `left to do`, in Brainy Birb's words."""
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})

    class _Limited(_StagedBrainy):
        def chat(self, system, context, tools=None, *, stream=False):
            text = str(context)
            marker = "Write the next section:"
            if text[text.rfind(marker) + len(marker):].split("---")[0].strip() == stages.LIMITS_HEADING:
                self.prompts.append(text)
                return "- The screen capture calls the Windows API: build-only here; run it on Windows."
            return super().chat(system, context, tools, stream=stream)

    brainy = _Limited(evaluations=["NO TICKETS b cannot pass here.\n- left to do: b: run its tests on Windows"])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert "calls the Windows API" in run.report
    assert "b: run its tests on Windows" in run.report


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


def test_an_unreadable_evaluation_tries_the_failing_tickets_again(monkeypatch, tmp_path):
    """Stopping takes an explicit NO TICKETS. Four overnight runs stopped after
    round 1 with tickets still failing because the reply could not be read."""
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})
    brainy = _StagedBrainy(evaluations=["I think b needs another go, it was close."])

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert len(run.rounds) == 2 and run.outcome.all_done
    assert [w.id for w in run.rounds[1].charter.workers] == ["b"]
    # Brainy Birb's unrestated words never reach Architect Birb.
    second = [p for p in brainy.prompts if "Stage: the plan for ticket 'b'" in p][-1]
    assert "it was close" not in second


def test_a_stage_survives_one_dropped_connection(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    _workers(monkeypatch, tmp_path)
    brainy = _StagedBrainy()
    real, dropped = brainy.chat, []

    def flaky(*args, **kwargs):
        if not dropped:
            dropped.append(1)
            raise RuntimeError("The connection to the model provider was lost part-way through the reply")
        return real(*args, **kwargs)

    brainy.chat = flaky

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert run.ran and run.outcome.all_done


def test_a_rejected_ticket_list_is_kept_in_the_trace(monkeypatch, tmp_path):
    _staged(tmp_path)
    bad = "### ticket: a\n- writes: a.py\n- accept: x"
    brainy = _StagedBrainy(tickets=bad)

    run = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    failed = [t for t in run.trace if t.get("problem")]
    assert run.stopped_at == "charter" and len(failed) == stages.SECTION_ATTEMPTS
    assert failed[0]["text"] == bad


def test_a_reported_contradiction_always_gets_its_test_rewritten(monkeypatch, tmp_path):
    """Even when the evaluation says there is nothing left: a worker that names
    a test contradicting the contract sends that ticket back, with the test
    named, so its stage rewrites it."""
    _staged(tmp_path)
    _project(tmp_path)
    from cobirb.flock import supervisor

    seen = set()

    def run(worker, cwd, **kwargs):
        if worker.id == "b" and "b" not in seen:
            seen.add("b")
            return WorkerReport(worker_id="b", ok=True, accepted=False, structured={
                "tests_pass": False, "contract_kept": True, "missing": [],
                "test_contradicts": ["test_it — expects 5 for 2, the contract says 4"]})
        (tmp_path / f"{worker.id}.py").write_text(f"def {worker.id}(n):\n    return n * 2\n")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    monkeypatch.setattr(supervisor, "run_worker", run)
    brainy = _StagedBrainy(evaluations=["NO TICKETS — looks finished to me."])

    run_ = _run(_orchestrator(monkeypatch, tmp_path, brainy), tmp_path)

    assert len(run_.rounds) == 2
    second = [p for p in brainy.prompts if "Stage: the plan for ticket 'b'" in p][-1]
    assert "REWRITE these tests" in second and "expects 5 for 2" in second


# --------------------------------------------------------------------------- #
# /autopilot: nothing asks, except the first charter approval
# --------------------------------------------------------------------------- #
def _autopilot(orchestrator, monkeypatch):
    """Auto-pilot's state without its preconditions: a sandbox that reports
    itself active, and the project granted on the main agent's policy."""
    monkeypatch.setattr(type(orchestrator.tools["shell"].sandbox), "active", property(lambda self: True))
    orchestrator.policy.autopilot_root = str(orchestrator.policy.cwd)


def test_a_planning_stage_follows_autopilot_switched_on_and_off_while_it_runs(monkeypatch, tmp_path):
    orchestrator = _orchestrator(monkeypatch, tmp_path, _StagedBrainy())
    stager = stages.Stager(orchestrator, str(tmp_path), "x", turns=3, trace=[])
    stage = stager._stage(set(), None, "")
    assert stage.autopilot is False

    _autopilot(orchestrator, monkeypatch)
    assert stage.autopilot is True

    orchestrator.disable_autopilot()
    assert stage.autopilot is False


def test_under_autopilot_the_flock_runs_in_auto_autonomy(monkeypatch, tmp_path):
    """Decisions are not put to the user and later rounds are not asked about;
    the first charter still is."""
    _staged(tmp_path, autonomy="ask")
    _project(tmp_path)
    _workers(monkeypatch, tmp_path, fail_first={"b"})
    orchestrator = _orchestrator(monkeypatch, tmp_path, _StagedBrainy(evaluations=[_REDO_B]))
    _autopilot(orchestrator, monkeypatch)
    questions, decided = [], []
    ask = Asker(confirm=lambda q, detail="": questions.append(q) or True,
                decide=lambda text: decided.append(text) or "")

    run = _run(orchestrator, tmp_path, ask=ask)

    assert len(run.rounds) == 2 and decided == []
    assert [q for q in questions if q.startswith("Approve")] == [questions[0]]


def test_under_autopilot_workers_refuse_instead_of_asking(monkeypatch, tmp_path):
    _staged(tmp_path)
    _project(tmp_path)
    from cobirb.flock import supervisor

    seen = []

    def run(worker, cwd, **kwargs):
        seen.append(kwargs.get("refuse"))
        (tmp_path / f"{worker.id}.py").write_text(f"def {worker.id}(n):\n    return n * 2\n")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    monkeypatch.setattr(supervisor, "run_worker", run)
    orchestrator = _orchestrator(monkeypatch, tmp_path, _StagedBrainy())
    _autopilot(orchestrator, monkeypatch)

    _run(orchestrator, tmp_path)

    assert seen and all(seen)
