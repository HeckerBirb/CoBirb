"""Tests for a whole flock session, end to end.

The approval gate is the important one. It is the only place a person sees
what a flock is about to be allowed to touch, so every path that reaches a
Worker Birb has to go through it.
"""
from __future__ import annotations

import sys
import textwrap


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
        ask=Asker(confirm=lambda q, detail='': False), probe=False,
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
        ask=Asker(confirm=lambda q, detail='': True), probe=False,
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
        ask=Asker(confirm=lambda q, detail='': shown.append(detail) or True, show=shown.append), probe=False,
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
        ask=Asker(confirm=lambda q, detail='': True), probe=False,
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
        ask=Asker(confirm=lambda q, detail='': False), probe=False,
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
        ask=Asker(confirm=lambda q, detail='': True), probe=False,
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


# --------------------------------------------------------------------------- #
# The review phase and the charter tool have to agree about what exists
# --------------------------------------------------------------------------- #
def test_the_charter_tool_outlives_the_planning_turn(monkeypatch, tmp_path):
    """It used to be popped off the orchestrator as soon as planning ended.

    But the planning transcript stays in context for the rest of the session,
    BRAINY_RULES — "call propose_charter" — included. So "redo the plan" asked
    afterwards produced a call to a tool that had been taken away, and an
    `Unknown tool` result the model could only conclude something was wrong
    with. It stays registered now.
    """
    _skeleton(tmp_path)
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_charter_toml(tmp_path)))

    run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": False), probe=False,
    )

    assert PROPOSE_CHARTER in orchestrator.tools
    assert orchestrator.policy.is_allowed(PROPOSE_CHARTER, {})


def test_a_second_flock_does_not_inherit_the_first_charter(monkeypatch, tmp_path):
    """The tool lives as long as the session now, so planning has to start from
    a clean slate — otherwise a fresh flock would be handed the last one's
    charter as though it had just been proposed."""
    from cobirb.flock.run import install_charter_tool

    _skeleton(tmp_path)
    orchestrator = _orchestrator(monkeypatch, tmp_path, _ScriptedBrainy(_charter_toml(tmp_path)))
    tool = install_charter_tool(orchestrator, str(tmp_path))
    tool.execute({"toml": _charter_toml(tmp_path)})
    assert tool.charter is not None

    tool.reset()

    assert tool.charter is None
    assert tool.attempts == 0
    assert tool.last_error == ""


def test_the_review_prompt_steers_the_verdict_into_prose():
    """An overnight flock died here. The review runs on the same session as
    planning, so BRAINY_RULES — "call propose_charter" — is still in context
    along with Brainy Birb's own successful call. The prompt then asked for
    "an amended charter", so a round that went badly produced the obedient
    thing: a call to a deregistered tool, an "Unknown tool" result no amount
    of re-reading could argue with, and the rest of the review turns spent
    failing to recover.
    """
    from cobirb.flock.brainy import round_summary
    from cobirb.flock.charter import Charter
    from cobirb.flock.supervisor import FlockOutcome

    text = round_summary(FlockOutcome(charter=Charter(objective="x", workers=[])))

    assert PROPOSE_CHARTER in text
    assert "DO NOT CALL" in text
    # The tool is registered for the whole session now, so the prompt must not
    # claim otherwise — it steers on the grounds that the round is over.
    assert "no longer available" not in text


def test_the_review_still_asks_what_a_second_round_should_be():
    """Warning the tool off must not cost the question itself — what the next
    round should be is the most useful thing the verdict carries."""
    from cobirb.flock.brainy import round_summary
    from cobirb.flock.charter import Charter
    from cobirb.flock.supervisor import FlockOutcome

    text = round_summary(FlockOutcome(charter=Charter(objective="x", workers=[])))

    assert "second round" in text


class _BadCharterBrainy:
    """Proposes an invalid charter, then announces that it worked.

    Not a strawman: this is what the model actually did in the run that
    prompted the fix — one `propose_charter` call rejected for a malformed
    worker entry, then "The charter has been finalized and submitted
    successfully" and a stop.
    """

    def __init__(self, attempts_before_giving_up=99):
        self._calls = 0
        self._limit = attempts_before_giving_up

    def name(self):
        return "bad-charter-brainy"

    def chat(self, system, context, tools=None, *, stream=False):
        return "The charter has been finalized and submitted successfully."

    def parse_tool_calls(self, reply):
        if self._calls >= self._limit:
            return []
        self._calls += 1
        return [ToolCall(name=PROPOSE_CHARTER, arguments={"toml": 'objective = "x"'})]

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def test_a_rejected_charter_is_reported_as_a_failure_not_a_decision(monkeypatch, tmp_path):
    """The bug this was written for: a charter rejected by validation left
    `charter is None`, which read identically to Brainy Birb deciding the work
    should not be divided. The run then reported the model's own account of it
    — "finalized and submitted successfully" — and no approval dialog ever
    appeared, because the run had already returned.
    """
    _skeleton(tmp_path)
    orchestrator = _orchestrator(monkeypatch, tmp_path, _BadCharterBrainy(attempts_before_giving_up=1))

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": True), probe=False,
    )

    assert run.stopped_at == "charter"
    assert "rejected" in run.report
    assert "successfully" not in run.report  # not the model's version of events
    assert not run.ran


def test_a_charter_that_was_never_attempted_is_still_a_legitimate_answer(monkeypatch, tmp_path):
    """The other half of the distinction. Proposing nothing at all means
    "this does not divide", which is a conclusion Brainy Birb is meant to be
    able to reach — and must not be reported as a failure."""
    _skeleton(tmp_path)
    model = _ScriptedBrainy(_charter_toml(tmp_path), propose=False)
    orchestrator = _orchestrator(monkeypatch, tmp_path, model)

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": True), probe=False,
    )

    assert run.stopped_at == "planning"
    assert "rejected" not in run.report


def test_a_rejected_charter_gets_one_more_try_with_the_reason_quoted(monkeypatch, tmp_path):
    """A model that has just been told its charter is invalid may stop by
    declaring success. Asked once more, with the reason, it usually corrects
    it — and that turns a dead run into a working one."""
    _skeleton(tmp_path)
    prompts = []
    model = _BadCharterBrainy(attempts_before_giving_up=1)
    orchestrator = _orchestrator(monkeypatch, tmp_path, model)
    original = orchestrator.run

    def record(prompt, *args, **kwargs):
        prompts.append(prompt)
        return original(prompt, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "run", record)

    run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": False), probe=False,
    )

    assert any("was NOT accepted" in p for p in prompts)


def test_the_charter_tool_stops_asking_after_enough_rejections(tmp_path):
    """A tool that only ever says "no, try again" is a loop with the turn
    limit for a brake — and the user watching it cannot tell whether anything
    is happening."""
    from cobirb.flock.brainy import MAX_CHARTER_ATTEMPTS, ProposeCharterTool

    tool = ProposeCharterTool(str(tmp_path))
    for _ in range(MAX_CHARTER_ATTEMPTS):
        result = tool.execute({"toml": "objective = not quoted"})

    assert tool.exhausted
    assert "STOP calling propose_charter" in result.content


def test_the_charter_template_is_sent_once_not_after_every_failure(tmp_path):
    """Twenty-five identical lines after every rejection is the strongest
    possible hint to a model that the thing to send next is the same again."""
    from cobirb.flock.brainy import ProposeCharterTool

    tool = ProposeCharterTool(str(tmp_path))
    first = tool.execute({"toml": "objective = not quoted"})
    second = tool.execute({"toml": "objective = not quoted"})

    assert "path/to/module.py" in first.content
    assert "path/to/module.py" not in second.content


def test_planning_does_not_retry_once_the_attempts_are_spent(monkeypatch, tmp_path):
    """The retry is for a turn that ended early, not for a model that has
    already failed the tool's own limit — that just buys a second turn budget
    of the identical failure."""
    from cobirb.flock.brainy import MAX_CHARTER_ATTEMPTS

    _skeleton(tmp_path)
    prompts = []
    orchestrator = _orchestrator(
        monkeypatch, tmp_path, _BadCharterBrainy(attempts_before_giving_up=MAX_CHARTER_ATTEMPTS)
    )
    original = orchestrator.run
    monkeypatch.setattr(
        orchestrator, "run",
        lambda prompt, *a, **k: (prompts.append(prompt), original(prompt, *a, **k))[1],
    )

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": False), probe=False,
    )

    assert not any("was NOT accepted" in p for p in prompts)
    assert run.stopped_at == "charter"


def test_a_toml_error_names_a_likely_cause(tmp_path):
    """tomllib says where it gave up, not what was done wrong — and "Invalid
    value (at line 1, column 13)" is the same message for a curly quote as for
    an unquoted string. A model correcting its own output cannot tell those
    apart, so it sends the same thing again."""
    from cobirb.flock.charter import CharterError, parse_charter

    for toml, expected in [
        ("objective = “x”", "typographic quotes"),
        ("objective = add CSV export", "no quotes around it"),
    ]:
        try:
            parse_charter(toml)
        except CharterError as exc:
            assert expected in str(exc), f"{toml!r} -> {exc}"
        else:
            raise AssertionError(f"{toml!r} should not have parsed")


class _ProseCharterBrainy:
    """Writes the charter into its reply instead of calling the tool.

    The commonest way a flock dies: Ollama's native tool_calls field is empty,
    so the orchestrator reads the reply as a final answer, and the charter sits
    in the transcript while the run reports that nothing happened.
    """

    def __init__(self, toml, fenced=True):
        self._reply = (
            f"Here is the charter:\n\n```toml\n{toml}```\n\nReady for the workers."
            if fenced else f"Here is the charter:\n\n{toml}"
        )

    def name(self):
        return "prose-brainy"

    def chat(self, system, context, tools=None, *, stream=False):
        return self._reply

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def test_a_charter_written_into_the_reply_is_read_from_there(monkeypatch, tmp_path):
    """Refusing to read it means telling the user their flock produced nothing
    while the charter sits on screen in front of them."""
    _skeleton(tmp_path)
    _honest_workers(monkeypatch, tmp_path)
    orchestrator = _orchestrator(
        monkeypatch, tmp_path, _ProseCharterBrainy(_charter_toml(tmp_path))
    )

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": True), probe=False,
    )

    assert run.charter is not None
    assert run.ran


def test_a_reply_with_no_charter_in_it_recovers_nothing(monkeypatch, tmp_path):
    """Recovery must not invent one. Most replies are just prose."""
    from cobirb.flock.charter import recover_charter

    assert recover_charter("The workers are ready to commence work.") is None
    assert recover_charter("") is None


def test_running_out_of_planning_turns_is_its_own_outcome(monkeypatch, tmp_path):
    """The synthetic "Stopped after N turns" string reads like an answer. A
    user told that has no way to know the run needed more room, not less work.
    """
    _skeleton(tmp_path)

    class _NeverFinishes:
        def name(self): return "busy"
        def chat(self, system, context, tools=None, *, stream=False): return "working"
        def parse_tool_calls(self, reply):
            return [ToolCall(name="read_file", arguments={"path": "a.py"})]
        def supports_tool_calling(self): return True
        def supports_streaming(self): return False

    orchestrator = _orchestrator(monkeypatch, tmp_path, _NeverFinishes())

    run = run_flock_session(
        orchestrator, "do the thing", str(tmp_path),
        ask=Asker(confirm=lambda q, detail="": True), probe=False, plan_turns=3,
    )

    assert run.stopped_at == "turns"
    assert "planning turns" in run.report
    assert "Stopped after" not in run.report  # not the synthetic string verbatim
