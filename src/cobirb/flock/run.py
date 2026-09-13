"""``cobirb flock`` — the whole thing, end to end.

Five stages, exactly one of which is the user's:

1. Brainy Birb plans, designs the seams and builds the skeleton, then proposes
   a charter.
2. CoBirb checks the partition holds together.
3. **The user approves the charter.** The single decision point. Approving it
   is what authorises every Worker Birb to run unattended inside scopes the
   user has now seen, which is why there are no per-tool prompts afterwards.
4. The flock fans out and is reviewed.
5. Brainy Birb reports, and proposes what a second round should be.

One round, then back to the user — a second round is a new decision made with
the results in front of them, and what shape it takes is part of what Brainy
Birb proposes rather than a rule fixed here.

The approval and the two questions (an overlapping partition, an endpoint that
serialises) are asked through a small protocol rather than ``input()``, so the
same flow works from the terminal and from the full-screen app.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config
from ..orchestrator import Orchestrator
from . import branch
from .brainy import PROPOSE_CHARTER, ProposeCharterTool, plan_prompt, round_summary
from .charter import Charter
from .probe import ProbeResult, probe_concurrency
from .supervisor import FlockOutcome, check_partition, run_flock

logger = logging.getLogger("cobirb")

# The turn budget for planning. Larger than a worker's, because building a
# skeleton means writing every interface, stub and failing test in the project
# before anything is proposed.
DEFAULT_PLAN_TURNS = 30


@dataclass
class Asker:
    """How this run puts a question to the user.

    A protocol rather than ``input()``, because a full-screen app cannot use
    ``input()`` and a pipeline has nobody to answer. ``confirm`` returns a
    decision; ``show`` reports progress. Both default to something safe: a run
    with no way to ask must not approve its own charter.
    """

    confirm: Callable[[str], bool] = lambda prompt: False
    show: Callable[[str], None] = lambda text: None


@dataclass
class FlockRun:
    """Everything that happened, including the ways it can end early."""

    charter: Charter | None = None
    outcome: FlockOutcome | None = None
    probe: ProbeResult | None = None
    report: str = ""
    stopped_at: str = ""
    # The token pairing this engagement's own session file with the point in
    # the main session where the conversation branched.
    token: str = ""

    @property
    def ran(self) -> bool:
        return self.outcome is not None


def _plan(
    orchestrator: Orchestrator, objective: str, cwd: str, turns: int
) -> tuple[Charter | None, str]:
    """Let Brainy Birb plan and scaffold, and take the charter it proposes.

    The charter tool is registered for this turn only, and removed afterwards:
    it is a way of answering one question, not a capability the agent keeps for
    the rest of the session.
    """
    tool = ProposeCharterTool(cwd)
    orchestrator.tools[PROPOSE_CHARTER] = tool
    orchestrator.policy.allow(PROPOSE_CHARTER, "")
    try:
        session = orchestrator.run(
            plan_prompt(objective), system="", cwd=cwd, persona="Brainy Birb", max_turns=turns
        )
    finally:
        orchestrator.tools.pop(PROPOSE_CHARTER, None)
    return tool.charter, session.summary or ""


def run_flock_session(
    orchestrator: Orchestrator,
    objective: str,
    cwd: str,
    *,
    ask: Asker | None = None,
    config: Config | None = None,
    stop: threading.Event | None = None,
    on_event: Callable[[str, Any], None] | None = None,
    plan_turns: int = DEFAULT_PLAN_TURNS,
    probe: bool = True,
    password: str | None = None,
) -> FlockRun:
    """Engage flock mode for one objective, and come back with a report.

    The branch is opened before anything runs and closed however this ends,
    including the three ways it stops early. A history that only records the
    flocks that succeeded is a history missing the interesting parts — "we
    tried this and the partition did not hold" is exactly the thing someone
    wants to find later.
    """
    ask = ask or Asker()
    config = config or Config()
    run = FlockRun()

    main = getattr(orchestrator, "session", None)
    run.token = branch.mint()
    flock_session = None
    if main is not None:
        branch.engage(main.session, objective, run.token)
        flock_session = branch.open_flock_session(main, run.token, objective, password)

    try:
        return _drive(run, orchestrator, objective, cwd, ask, config, stop, on_event,
                      plan_turns, probe)
    finally:
        _close_branch(main, flock_session, run, password)


def _drive(
    run: FlockRun,
    orchestrator: Orchestrator,
    objective: str,
    cwd: str,
    ask: Asker,
    config: Config,
    stop: threading.Event | None,
    on_event: Callable[[str, Any], None] | None,
    plan_turns: int,
    probe: bool,
) -> FlockRun:
    """The five stages. Split out so the branch above closes on every path."""
    # ---- 1. Plan and scaffold ------------------------------------------- #
    ask.show("Brainy Birb is planning and building the skeleton…")
    charter, narration = _plan(orchestrator, objective, cwd, plan_turns)
    if charter is None:
        run.report = narration or "Brainy Birb did not propose a charter."
        run.stopped_at = "planning"
        # Not a failure. "This is a single person's job, do not fan it out" is
        # a correct answer, and the narration is where it says so.
        return run
    run.charter = charter

    # ---- 2. Does the partition hold together? --------------------------- #
    disjoint, partition = check_partition(charter)
    if not disjoint:
        ask.show(partition)
        if not ask.confirm(
            "The partition overlaps. Workers writing the same file cannot run safely "
            "in parallel, and 'which worker broke this' stops having an answer.\n"
            "Run anyway, one worker at a time?"
        ):
            run.stopped_at = "partition"
            run.report = f"Stopped: the partition overlaps.\n{partition}"
            return run
        # Overlapping scopes make concurrency unsafe, so honour the user's
        # choice to continue by removing the thing that makes it unsafe.
        concurrency = 1
    else:
        concurrency = charter.concurrency

    # ---- 3. The one human decision -------------------------------------- #
    ask.show(charter.describe())
    if not ask.confirm(
        f"Approve this charter? {len(charter.workers)} Worker Birb(s) will run "
        "unattended inside exactly these scopes, with no further prompts."
    ):
        run.stopped_at = "approval"
        run.report = "Charter not approved; nothing ran."
        return run

    # ---- 4. Can the endpoint actually do two at once? ------------------- #
    if probe and concurrency > 1:
        if ask.confirm(
            "Check whether your model endpoint serves two requests at once? "
            "It takes a few seconds, and a server that queues them would make a "
            "concurrent flock quietly sequential."
        ):
            run.probe = probe_concurrency(orchestrator.model)
            ask.show(run.probe.describe())
            if run.probe.concurrent is False and not ask.confirm(
                "Run the Worker Birbs one at a time instead?"
            ):
                pass  # they chose to carry on as configured
            elif run.probe.concurrent is False:
                concurrency = 1

    # ---- 5. Fan out, review, report ------------------------------------- #
    ask.show(f"Fanning out {len(charter.workers)} ticket(s), {concurrency} at a time…")
    outcome = run_flock(
        charter, cwd, config=config, concurrency=concurrency, stop=stop, on_event=on_event
    )
    run.outcome = outcome
    ask.show(outcome.describe())

    ask.show("Brainy Birb is reviewing the round…")
    verdict = orchestrator.run(
        round_summary(outcome), system="", cwd=cwd, persona="Brainy Birb", max_turns=6
    )
    run.report = verdict.summary or outcome.describe()
    return run


def _close_branch(main, flock_session, run: FlockRun, password: str | None) -> None:
    """Record where the conversation rejoined, and save the flock's own file.

    Guarded, because traceability is worth less than the work it describes: a
    session file that cannot be written should cost a record, never the report
    the user is waiting for.
    """
    if main is None:
        return
    branch.rejoin(main.session, run.token, run.report or "The flock returned with no report.")
    if flock_session is None:
        return
    try:
        for report in (run.outcome.reports if run.outcome else []):
            flock_session.session.add_text(report.worker_id, report.describe())
        for review in (run.outcome.reviews if run.outcome else []):
            flock_session.session.add_text(f"review:{review.worker_id}", review.describe())
        flock_session.session.summary = run.report
        flock_session.save(password)
    except Exception:  # noqa: BLE001 - a lost record must not cost the round
        logger.warning("could not write the flock session file", exc_info=True)
