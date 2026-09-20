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
from .brainy import (
    PROPOSE_CHARTER,
    ProposeCharterTool,
    charter_retry_prompt,
    plan_prompt,
    round_summary,
)
from .charter import Charter, recover_charter
from .preflight import missing_models
from .probe import ProbeResult, probe_concurrency
from .supervisor import Canceller, FlockOutcome, check_partition, run_flock

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

    # ``detail`` is what the question is *about* — the charter, the list of
    # overlaps. It goes to the asker rather than being printed beforehand
    # because a centred dialog drawn over the thing it is asking about leaves
    # the user approving something they cannot read.
    confirm: Callable[..., bool] = lambda prompt, detail="": False
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


@dataclass
class PlanResult:
    """What planning produced, and — when it produced nothing — why not."""

    charter: Charter | None
    narration: str
    attempts: int = 0
    last_error: str = ""
    # True when the charter was read out of Brainy Birb's reply rather than
    # proposed through the tool — see `charter.recover_charter`.
    recovered: bool = False
    # True when planning ran out of turns instead of reaching a conclusion.
    exhausted_turns: bool = False

    @property
    def failed(self) -> bool:
        """Whether a charter was attempted and none of the attempts held up.

        The distinction the caller cannot do without. **No attempts at all is
        a legitimate answer** — "this is a single person's job, do not fan it
        out" is a conclusion Brainy Birb is supposed to be able to reach.
        Attempts with nothing to show for them is a failure. Reported as the
        first, the second hands the user the model's own account of a round
        that never happened, and a model that has just had a charter rejected
        is quite capable of announcing that it succeeded.
        """
        return self.charter is None and self.attempts > 0


def install_charter_tool(
    orchestrator: Orchestrator, cwd: str, on_proposed: "Callable[[Charter], None] | None" = None
) -> ProposeCharterTool:
    """Put the charter tool on ``orchestrator`` and leave it there.

    **Registered for the session rather than for the planning turn**, which is
    the opposite of what this used to do. The tool was previously added before
    planning and popped in a ``finally`` afterwards, on the reasoning that it
    answers one question rather than being a capability worth keeping. That
    reasoning ignored where the conversation goes next: the planning
    transcript — ``BRAINY_RULES`` included, which says to deliver a charter by
    calling this — stays in context for the rest of the session, so "redo the
    plan" produces a call to a tool that has been taken away, and an ``Unknown
    tool`` result the model cannot argue its way past.

    It is granted permission too, and that is a considered exception to the
    default-deny rule rather than an oversight. The rule exists to gate
    *capability*: reaching the filesystem, the network, a subprocess. This
    reaches none of them — it parses text and keeps the result in memory — so
    an approval prompt for it would be asking the user to authorise CoBirb to
    talk to itself.

    Idempotent, and returns whichever tool is now installed, so the flock and
    the front-end can both call it without racing to own the instance.
    """
    existing = orchestrator.tools.get(PROPOSE_CHARTER)
    if isinstance(existing, ProposeCharterTool):
        if on_proposed is not None:
            existing.on_proposed = on_proposed
        return existing
    tool = ProposeCharterTool(cwd, on_proposed=on_proposed)
    orchestrator.tools[PROPOSE_CHARTER] = tool
    # No second argument: that one scopes a grant to a *command*, and reaches
    # "allow the tool outright" only by falling through an empty-word check.
    # This tool takes no command, so it says what it means.
    orchestrator.policy.allow(PROPOSE_CHARTER)
    return tool


def _plan(orchestrator: Orchestrator, objective: str, cwd: str, turns: int) -> PlanResult:
    """Let Brainy Birb plan and scaffold, and take the charter it proposes.

    One retry when the charter came back rejected. The planning turn ends
    whenever the model stops calling tools, and a model that has just been
    told its charter is invalid may well stop by declaring success instead of
    correcting it — which is how a rejected charter used to become a finished
    flock that never ran. Being asked once more, with the reason quoted back,
    costs one turn and recovers the common case.
    """
    tool = install_charter_tool(orchestrator, cwd)
    tool.reset()
    session = orchestrator.run(
        plan_prompt(objective), system="", cwd=cwd, persona="Brainy Birb", max_turns=turns
    )

    # Not when the attempts are already spent. The retry is for a planning turn
    # that ended early — a model that declared success over a rejected charter
    # — and asking again after it has failed the tool's own limit buys another
    # turn budget's worth of the identical failure.
    if tool.charter is None and tool.attempts and not tool.exhausted:
        session = orchestrator.run(
            charter_retry_prompt(tool.last_error),
            system="", cwd=cwd, persona="Brainy Birb", max_turns=turns,
        )

    narration = session.summary or ""
    charter, recovered = tool.charter, False
    if charter is None:
        # Last resort: the model wrote the charter into its reply instead of
        # calling the tool. A reply does not propose anything — but one that
        # contains a complete, valid charter has done all the work and missed
        # only the mechanism, and refusing to read it means telling the user
        # their flock produced nothing while the charter sits on screen in
        # front of them.
        charter = recover_charter(narration)
        recovered = charter is not None

    return PlanResult(
        charter=charter,
        narration=narration,
        attempts=tool.attempts,
        last_error=tool.last_error,
        recovered=recovered,
        exhausted_turns=bool(getattr(orchestrator, "turns_exhausted", False)),
    )


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
    io_for: Callable[[Any, Any], Any] | None = None,
    on_charter: Callable[[Charter], None] | None = None,
    canceller: Canceller | None = None,
    charter: Charter | None = None,
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
                      plan_turns, probe, io_for, on_charter, canceller, charter)
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
    io_for: Callable[[Any, Any], Any] | None = None,
    on_charter: Callable[[Charter], None] | None = None,
    canceller: Canceller | None = None,
    charter: Charter | None = None,
) -> FlockRun:
    """The five stages. Split out so the branch above closes on every path.

    ``charter`` skips the first of them. A charter can now be proposed outside
    a planning turn — Brainy Birb keeps the tool for the whole session — and
    one that arrives that way has already been written; planning again would
    throw it away and ask the model to invent a second one.
    """
    # ---- 0. Can this run at all? ---------------------------------------- #
    # Before the planning work, not after: a missing worker model costs
    # nothing to find now and costs a whole skeleton to find later.
    warning = missing_models(config)
    if warning and not ask.confirm("Start the flock anyway?", warning):
        run.stopped_at = "preflight"
        run.report = warning
        return run

    # ---- 1. Plan and scaffold ------------------------------------------- #
    if charter is not None:
        # Already written, by a `propose_charter` call outside a planning turn.
        # Planning again would discard it and ask for a second one.
        plan = PlanResult(charter=charter, narration="")
    else:
        ask.show("Brainy Birb is planning and building the skeleton…")
        plan = _plan(orchestrator, objective, cwd, plan_turns)
    if plan.charter is not None and plan.recovered:
        # Say so. A charter that arrived this way is one the model got right
        # apart from how it delivered it, and the user approving it should
        # know it was read out of a reply rather than proposed.
        ask.show(
            "Brainy Birb wrote the charter into its reply instead of proposing it. "
            "Reading it from there — it is the same charter, and you still approve it below."
        )
    if plan.failed:
        # A charter was attempted and every attempt was rejected. Reported as
        # its own outcome rather than through `narration`, which at this point
        # is whatever the model chose to say about a round that did not happen
        # — in the case this was written for, that it had "finalized and
        # submitted the charter successfully".
        run.stopped_at = "charter"
        run.report = (
            f"No flock ran. Brainy Birb proposed a charter {plan.attempts} time(s) and the "
            f"last was rejected:\n\n    {plan.last_error}\n\n"
            "Nothing was started and nothing was changed beyond whatever skeleton it wrote. "
            "Run /flock again, or say what to correct."
        )
        return run
    if plan.charter is None and plan.exhausted_turns:
        # Ran out of turns rather than reaching a conclusion — almost always a
        # skeleton bigger than the budget, since every file written costs a
        # turn. Reported as its own thing because the synthetic "Stopped after
        # N turns" string reads like an answer, and a user told that has no way
        # to know the run needed more room rather than less work.
        run.stopped_at = "turns"
        run.report = (
            f"No flock ran. Brainy Birb used all {plan_turns} planning turns without "
            "proposing a charter — usually a skeleton with more files in it than the "
            "budget allows, since every file written costs a turn.\n\n"
            "Whatever it wrote is still there. Run /flock again with a smaller slice of "
            "the work, or ask it to propose a charter for the skeleton it has already built."
        )
        return run
    if plan.charter is None:
        run.report = plan.narration or "Brainy Birb did not propose a charter."
        run.stopped_at = "planning"
        # Not a failure, and distinct from the branch above: no charter was
        # attempted at all. "This is a single person's job, do not fan it out"
        # is a correct answer, and the narration is where it says so.
        return run
    charter = plan.charter
    run.charter = charter
    if on_charter is not None:
        # Before the partition check and before approval, so a front-end can
        # lay out its display while the user is still reading the charter —
        # panes that appear one at a time as workers start would never show
        # what is waiting to run.
        try:
            on_charter(charter)
        except Exception:  # noqa: BLE001 - a display is not worth the run
            logger.debug("a charter handler raised", exc_info=True)

    # ---- 2. Does the partition hold together? --------------------------- #
    disjoint, partition = check_partition(charter)
    if not disjoint:
        if not ask.confirm(
            "The partition overlaps. Run anyway, one worker at a time?",
            f"{partition}\n\nWorkers writing the same file cannot run safely in parallel, "
            "and 'which worker broke this' stops having an answer.",
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
    # The charter travels *with* the question rather than being shown before
    # it. Shown before, a centred dialog covers the very thing it is asking
    # about — which makes "approve this charter" a question nobody can
    # actually answer.
    ask.show(charter.describe())
    if not ask.confirm(
        f"Approve this charter? {len(charter.workers)} Worker Birb(s) will run "
        "unattended inside exactly these scopes, with no further prompts.",
        charter.describe(),
    ):
        run.stopped_at = "approval"
        run.report = "Charter not approved; nothing ran."
        return run

    # ---- 4. Can the endpoint actually do two at once? ------------------- #
    if probe and concurrency > 1:
        if ask.confirm(
            "Check whether your model endpoint serves two requests at once?",
            "It takes a few seconds. A server that queues them would make a concurrent "
            "flock quietly sequential — two panes, one of them stalled, for reasons "
            "nothing tells you.",
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
        charter, cwd, config=config, concurrency=concurrency, stop=stop,
        on_event=on_event, io_for=io_for, canceller=canceller,
        # Read off the orchestrator rather than passed in: Brainy Birb's
        # agent already holds the session's approvals, and threading a second
        # copy through every caller would be the same object in two places,
        # free to be the wrong one.
        grants=getattr(orchestrator, "grants", None),
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
