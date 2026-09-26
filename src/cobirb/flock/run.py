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

import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..orchestrator import STOP_NO_PROGRESS, Orchestrator
from . import branch
from .brainy import (
    PROPOSE_CHARTER,
    AddWorkerTool,
    DeclareSeamTool,
    DropWorkerTool,
    ProposeCharterTool,
    SealCharterTool,
    next_move_prompt,
    plan_prompt,
    round_summary,
)
from .charter import Charter, recover_charter
from .preflight import missing_models
from .probe import ProbeResult, probe_concurrency
from .stages import (
    AUTONOMY_ASK,
    AUTONOMY_AUTO,
    DEFAULT_MAX_ROUNDS,
    LIMITS_HEADING,
    CharterApproval,
    Stager,
    approval_changes,
    check_tickets,
    left_to_do,
)
from .supervisor import Canceller, FlockOutcome, check_partition, run_flock

logger = logging.getLogger("cobirb")

# The turn budget for planning. Larger than a worker's, because building a
# skeleton means writing every interface, stub and failing test in the project
# before anything is proposed.
DEFAULT_PLAN_TURNS = 30

# How many times planning may be asked for the next move before it gives up.
# The first pass is the model's own; the rest are nudges computed from the
# draft. Four is enough for the sequence a stalled plan actually needs —
# declare, add, add, seal — and few enough that a model going nowhere does not
# spend five full turn budgets doing it.
MAX_PLAN_STEPS = 5

# Consecutive nudges answered with prose and no tool call at all before
# planning stops. A driven loop that cannot tell "working" from "talking"
# replaces a halt with a loop, which is not an improvement: two is enough to
# rule out a single stray narration and short enough that nobody watches a
# model describe the same plan five times.
MAX_SILENT_STEPS = 2


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
    # Staged planning in ``ask`` mode puts Brainy Birb's design decisions to
    # the user: given the numbered list, returns the user's answers, or None
    # to leave every decision to Brainy Birb. The default leaves them to it —
    # a decision about the design is not a capability, so "nobody answered"
    # safely means "use your judgement", unlike a charter approval.
    decide: Callable[[str], "str | None"] = lambda decisions: None
    # Which agent is working now — "Brainy Birb" or "Architect Birb" — told at
    # the start of every planning stage, so a front-end can name the one it is
    # showing. Nothing to do by default.
    speaking: Callable[[str], None] = lambda label: None


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
    # Staged planning: every round's outcome (``outcome`` is the last), the
    # design documents, and a trace of which tools each planning step called
    # — the evidence for where a failed round went wrong.
    rounds: list = field(default_factory=list)
    design: str = ""
    trace: list = field(default_factory=list)

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
    # Tickets sitting in an unsealed draft. The incremental route's own version
    # of "tried and produced nothing": a model that called `add_worker` five
    # times and never called `seal_charter` has done all the work and missed
    # the last step, which is a different outcome from deciding the work does
    # not divide — and indistinguishable from it without this, because
    # `attempts` only counts attempts to *seal*.
    unsealed: int = 0
    # True when the loop ran out of moves rather than reaching a conclusion:
    # the plan was incomplete, the next move was named, and the model answered
    # with prose and no tool call. Distinct from every field above, because it
    # is the one case where CoBirb knows the model meant to divide the work and
    # knows it never finished saying how.
    stalled: bool = False

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

    **All five planning tools go on together, over one shared ``CharterDesk``.**
    Brainy Birb can build a charter up a move at a time (``declare_seam``,
    ``add_worker``, ``drop_worker``, ``seal_charter``) or send one whole TOML
    document (``propose_charter``), and both routes write to the same desk — so
    whichever one produced a charter, the front-end finds it in the same place.
    Registering a subset would be worse than registering none: the rules in
    context name all of them, and a model calling the one that is missing gets
    an ``Unknown tool`` it cannot argue its way past.

    Idempotent, and returns whichever tool is now installed, so the flock and
    the front-end can both call it without racing to own the instance.
    """
    existing = orchestrator.tools.get(PROPOSE_CHARTER)
    if isinstance(existing, ProposeCharterTool):
        if on_proposed is not None:
            existing.on_proposed = on_proposed
        return existing
    tool = ProposeCharterTool(cwd, on_proposed=on_proposed)
    for planner in (
        tool,
        DeclareSeamTool(tool.desk),
        AddWorkerTool(tool.desk),
        DropWorkerTool(tool.desk),
        SealCharterTool(tool.desk),
    ):
        orchestrator.tools[planner.name()] = planner
        # No second argument: that one scopes a grant to a *command*, and
        # reaches "allow the tool outright" only by falling through an
        # empty-word check. These tools take no command, so it says what it
        # means. Permitting them is the same considered exception to
        # default-deny as `propose_charter` was: they reach no filesystem, no
        # network and no subprocess — they build a plan in memory.
        orchestrator.policy.allow(planner.name())
    return tool


@contextlib.contextmanager
def _without_project_verification(orchestrator: Orchestrator):
    """Take the user's ``verify_command`` off Brainy Birb for the planning turn.

    **Brainy Birb's job is to write failing tests.** ``BRAINY_RULES`` step 3
    says so in as many words — "failing tests that pin EVERY behaviour the
    docstring claims" — and they are what the Worker Birbs are briefed to make
    pass. The planning turn therefore ends with a project whose check fails *by
    design*.

    That went straight into ``_verify_and_fix``, which saw a turn that had
    changed files, ran the user's ``verify_command``, watched it fail, and
    handed Brainy Birb "VERIFICATION FAILED. Fix the cause." with four turns
    and its file tools still attached. The obedient response is to implement its
    own stubs or weaken its own tests — destroying the one artifact the entire
    design rests on, and the one the workers were about to build against. It
    also spent a second full run of the suite and clobbered
    ``turns_exhausted``, which is set at the top of every ``_loop``.

    ``build_subagent`` already scopes each worker's verification to its own
    ``accept`` so a worker "never meets somebody else's failing test to
    helpfully fix". This is the same rule applied to the agent that *authors*
    the failing tests, which had simply been missed.

    Restored in a ``finally``, and safe to mutate because a flock holds the
    session: ``cmd_flock`` refuses to start while a turn is running and
    ``/flock`` refuses while a flock is. The workers' own scoped checks and the
    review passes are untouched — they are the verification a flock actually
    wants.
    """
    original = orchestrator.verify
    orchestrator.verify = None
    try:
        yield
    finally:
        orchestrator.verify = original


PLANNING_SINGLE = "single"
PLANNING_STAGED = "staged"
# Staged by default. On the golden task — work big enough to divide — it came
# first on both models measured (91/95 each, against 88 and 83 for the
# one-prompt planner and 83 and 75 for a single agent). On small tasks the
# one-prompt planner does better, and a single agent better still, which is
# why the docs say to use a single agent for small jobs rather than a flock.
DEFAULT_PLANNING = PLANNING_STAGED


@dataclass
class FlockSettings:
    """The ``flock`` config block, read once, with every value checked.

    A typo costs the setting, never the run: an unknown planner or autonomy
    falls back to the default, and a round cap that is not a positive number
    becomes the default cap.
    """

    planning: str = DEFAULT_PLANNING
    autonomy: str = AUTONOMY_ASK
    max_rounds: int = DEFAULT_MAX_ROUNDS

    @classmethod
    def from_config(cls, config: Config) -> "FlockSettings":
        block = config.get("flock") or {}
        if not isinstance(block, dict):
            block = {}
        planning = block.get("planning", DEFAULT_PLANNING)
        autonomy = block.get("autonomy", AUTONOMY_ASK)
        try:
            rounds = int(block.get("max_rounds", DEFAULT_MAX_ROUNDS))
        except (TypeError, ValueError):
            rounds = DEFAULT_MAX_ROUNDS
        return cls(
            planning=planning if planning in (PLANNING_SINGLE, PLANNING_STAGED) else DEFAULT_PLANNING,
            autonomy=autonomy if autonomy in (AUTONOMY_ASK, AUTONOMY_AUTO) else AUTONOMY_ASK,
            max_rounds=rounds if rounds >= 1 else DEFAULT_MAX_ROUNDS,
        )


# **/autopilot means nothing asks.** The flock runs in auto autonomy (its design
# decisions and later rounds are not put to the user) and every Worker Birb
# refuses what its charter does not cover instead of asking. The first charter
# approval stays: it is the one question that grants capability. Auto
# autonomy's own requirement, the sandbox, is one /autopilot already has.
#
# Read at each decision rather than once at the start, because auto-pilot can be
# switched on or off (F3) while a flock runs, and a flock that took its answer at
# the start would keep asking — or keep not asking — after the user changed it.
def _autopilot(orchestrator: Orchestrator) -> bool:
    return bool(getattr(orchestrator, "autopilot", False))


def _autonomy(settings: FlockSettings, orchestrator: Orchestrator) -> str:
    return AUTONOMY_AUTO if _autopilot(orchestrator) else settings.autonomy


def _unless_autopilot(decide, orchestrator: Orchestrator):
    """``decide``, answered by nobody while auto-pilot is on (Brainy Birb decides)."""
    return lambda text: "" if _autopilot(orchestrator) else decide(text)


def _plan(orchestrator: Orchestrator, objective: str, cwd: str, turns: int,
          trace: "list | None" = None) -> PlanResult:
    """Let Brainy Birb plan and scaffold, and take the charter it proposes.

    **A driven loop with a completion predicate, rather than one turn plus a
    couple of ad-hoc retries.** A model's turn ends when it stops calling
    tools, which is the right rule for a conversation and the wrong one here,
    because planning has an objective completion test CoBirb can apply itself:
    is there a sealed charter? A model that worked out its next move correctly
    and then stopped to *say* it — "I will proceed by correcting the first
    worker's ticket" — ended the phase on that sentence and never got to make
    the move. The two recovery branches this replaces could not catch it: one
    needed a seal attempt, which had not happened, and the other needed tickets
    in the draft, which the one refused ``add_worker`` had kept out.

    So each pass asks the completion predicate, and where the plan is
    incomplete it computes the next move from the draft and asks for exactly
    that (``next_move_prompt``). It converts "the model must sustain a long
    agentic chain unaided" into "the model must make one correct move,
    repeatedly, while CoBirb holds the state" — which is what a weaker model
    can actually do.

    Four ways out, all bounded:

    - a sealed charter, which is the point;
    - a model that called no tool at all and has no plan — prose *is* the
      answer, and "this is a single person's job" is a legitimate one;
    - a limit the desk itself enforces (attempts spent), or the turn budget;
    - two nudges running answered with prose and no tool call, which is a
      stall and is reported as one rather than looped over.
    """
    tool = install_charter_tool(orchestrator, cwd)
    tool.reset()
    # Before a single skeleton file exists, so that at seal the desk can tell
    # what planning wrote from what was already there.
    tool.desk.watch_skeleton(cwd)
    narration = ""
    stalled = False
    silent = 0
    touched = False
    with _without_project_verification(orchestrator):
        prompt = plan_prompt(objective)
        for _ in range(MAX_PLAN_STEPS):
            session = orchestrator.run(
                prompt, system="", cwd=cwd, label="Brainy Birb", max_turns=turns
            )
            if trace is not None:
                trace.append({"step": f"plan pass {len(trace) + 1}", "calls": [
                    c["name"] for c in getattr(orchestrator, "last_run_tool_calls", [])]})
            narration = session.summary or narration
            called = bool(getattr(orchestrator, "last_run_tool_calls", None))
            touched = touched or called

            if tool.charter is not None:
                break
            if not touched and tool.desk.draft.empty and not tool.attempts:
                # Nothing was called, nothing was built, nothing was tried.
                # This is a model that read the work and answered in prose, and
                # the answer it is *supposed* to be able to give — "do not fan
                # this out" — looks exactly like this. Nudging it would be
                # arguing with a decision it was asked to make.
                break
            stop = getattr(orchestrator, "last_stop", None)
            if (
                tool.exhausted
                or bool(getattr(orchestrator, "turns_exhausted", False))
                or getattr(stop, "reason", None) == STOP_NO_PROGRESS
            ):
                # The desk has stopped inviting corrections, the turn budget is
                # gone, or the loop saw the model repeating itself. Each has its
                # own outcome at the caller; another pass would buy a second
                # budget's worth of the same failure.
                break

            silent = silent + 1 if not called else 0
            if silent >= MAX_SILENT_STEPS:
                stalled = True
                break
            prompt = next_move_prompt(tool.desk)
        else:
            # Out of moves with the plan still incomplete. Bounded by design:
            # the point of the loop is to hold the state while the model makes
            # one move at a time, not to keep asking forever.
            stalled = tool.charter is None

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
        unsealed=0 if charter is not None else len(tool.desk.draft.workers),
        stalled=stalled and charter is None,
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

    settings = FlockSettings.from_config(config)
    if charter is None and settings.planning == PLANNING_STAGED:
        return _drive_staged(run, orchestrator, objective, cwd, ask, config, stop, on_event,
                             plan_turns, probe, io_for, on_charter, canceller, settings)

    # ---- 1. Plan and scaffold ------------------------------------------- #
    if charter is not None:
        # Already written, by a `propose_charter` call outside a planning turn.
        # Planning again would discard it and ask for a second one.
        plan = PlanResult(charter=charter, narration="")
    else:
        ask.show("Brainy Birb is planning and building the skeleton…")
        try:
            plan = _plan(orchestrator, objective, cwd, plan_turns, trace=run.trace)
        except RuntimeError as exc:
            # The model server failed mid-plan. A fault in something else is
            # reported and the run ends, rather than escaping as a crash that
            # takes the session with it (AGENTS.md invariant 6).
            run.stopped_at = "error"
            run.report = f"No flock ran: planning stopped because the model server failed.\n\n{exc}"
            return run
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
    if plan.charter is None and plan.unsealed:
        # Distinct from both branches below. A draft with tickets in it is not
        # "this work does not divide" — it is the opposite, said by a model
        # that did the dividing and did not seal it. Reported as the other
        # thing, the user is told their objective was declined while a finished
        # plan sits in the session unrun.
        run.stopped_at = "unsealed"
        run.report = (
            f"No flock ran. Brainy Birb built a plan of {plan.unsealed} ticket(s) but never "
            "called `seal_charter`, so no charter was proposed and nothing was approved.\n\n"
            "Whatever skeleton it wrote is still there. Run /flock again, or ask it to seal "
            "the plan it has already built."
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
    if plan.charter is None and plan.stalled:
        # It meant to divide the work and never finished saying how. Reported
        # as its own thing because the alternative is the narration — which at
        # this point is a model describing the move it was about to make — sent
        # to the user under a heading that reads as "decided not to fan out".
        # That is the opposite of what happened, and it is the reason this
        # whole loop exists.
        run.stopped_at = "stalled"
        run.report = (
            "No flock ran. Brainy Birb stopped mid-plan: it was asked for the next move "
            "and answered in prose without calling anything, so no charter was proposed.\n\n"
            "Whatever skeleton it wrote is still there. Run /flock again — a smaller slice "
            "of the work usually gets further — or ask it to finish the plan it started."
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
    if plan.exhausted_turns:
        # A charter *and* an exhausted budget: planning was cut off rather than
        # finished, so the skeleton behind this charter may be half-written.
        # Worth a sentence, because the approval prompt below looks identical
        # either way and the user is about to authorise workers to build
        # against whatever is actually on disk.
        ask.show(
            f"Brainy Birb used all {plan_turns} planning turns. The charter below is the "
            "one it proposed, but it stopped rather than finished — check the skeleton is "
            "complete before approving."
        )
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
        # The charter's number, capped by what its dependency graph actually
        # permits — a chain of four runs one at a time whatever `concurrency`
        # says, and the probe below would otherwise go looking for parallelism
        # this round was never going to use.
        concurrency = charter.effective_concurrency

    # ---- 3. The one human decision -------------------------------------- #
    # The charter travels *with* the question rather than being shown before
    # it. Shown before, a centred dialog covers the very thing it is asking
    # about — which makes "approve this charter" a question nobody can
    # actually answer.
    ask.show(charter.describe())
    if not ask.confirm(
        f"Approve this charter? {len(charter.workers)} Worker Birb(s) will run "
        "unattended inside exactly these scopes, with no further prompts.",
        CharterApproval(charter),
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
        refuse=lambda: _autopilot(orchestrator),
    )
    run.outcome = outcome
    ask.show(outcome.describe())

    ask.show("Brainy Birb is reviewing the round…")
    try:
        verdict = orchestrator.run(
            round_summary(outcome), system="", cwd=cwd, label="Brainy Birb", max_turns=6
        )
        run.report = verdict.summary or outcome.describe()
    except RuntimeError as exc:
        # The round ran; only its account failed. The computed one stands.
        run.report = f"{outcome.describe()}\n\n(Brainy Birb's account of the round failed: {exc})"
    return run


def _round_verdict(outcome: FlockOutcome) -> str:
    """Everything the evaluation stage is given about a round: CoBirb's own
    account first (the checks re-run on the finished tree), then review, then
    each worker's report — structured where it gave one."""
    lines = [outcome.describe(), "", "--- Review ---"]
    lines += [review.describe() for review in outcome.reviews]
    lines += ["", "--- What each Worker Birb reported ---"]
    for report in outcome.reports:
        lines.append(f"[{report.worker_id}] " + (report.report_text() or "(no report)"))
        if report.accepted is False and report.accept_output:
            lines.append("    check output (end): " + report.accept_output[-800:].replace("\n", "\n    "))
    return "\n".join(lines)


def _with_contradicted_tests(outcome: FlockOutcome, known: list, tickets: list,
                             whys: dict[str, str]) -> "tuple[list, dict[str, str]]":
    """Add every ticket whose worker reported a test contradicting the contract.

    **A reported contradiction always leads to that test being rewritten.**
    On the golden task a worker named five of the planner's tests with a
    precise, correct reason each ("expects 0 ticks when elapsed = 0.14 ≥
    tick_seconds = 0.1"), and the evaluation stopped the flock instead of
    planning a round to fix them. The ticket's own stage rewrites its tests
    in the next round, so the ticket goes back in — even past an evaluation
    that said there was nothing left — with the tests named in its reason.
    The round cap and the no-progress stop still bound it.
    """
    tickets, whys = list(tickets), dict(whys)
    present = {t.id for t in tickets}
    by_id = {t.id: t for t in known}
    for report in outcome.reports:
        contradicted = (report.structured or {}).get("test_contradicts") or []
        if not contradicted or report.worker_id not in by_id:
            continue
        if report.worker_id not in present:
            tickets.append(by_id[report.worker_id])
            present.add(report.worker_id)
        whys[report.worker_id] = " ".join(filter(None, [
            whys.get(report.worker_id, ""),
            "REWRITE these tests, which the worker reported as contradicting the contract "
            "(check each against the contract; if the worker is right, fix the test): "
            + "; ".join(contradicted),
        ]))
    return tickets, whys


def _failing(outcome: FlockOutcome) -> tuple:
    """What was still wrong after a round, for noticing a round that changed nothing."""
    return tuple(sorted((r.worker_id, bool(r.ok), r.accepted) for r in outcome.outstanding))


def _drive_staged(
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
    io_for: Callable[[Any, Any], Any] | None,
    on_charter: Callable[[Charter], None] | None,
    canceller: Canceller | None,
    settings: "FlockSettings",
) -> FlockRun:
    """Staged planning, in rounds — see ``flock.stages``.

    **Approval.** The first charter is approved by the user in both modes. A
    later round's charter is put to the user again in ``ask`` mode, showing
    only what it adds; in ``auto`` mode it is approved without asking, which is
    why ``auto`` refuses to start outside a working sandbox — a mode that runs
    rounds unattended is only as safe as what contains it.
    """
    if _autonomy(settings, orchestrator) == AUTONOMY_AUTO:
        box = getattr(orchestrator.tools.get("shell"), "sandbox", None)
        if box is None or not box.active:
            run.stopped_at = "autonomy"
            run.report = (
                "No flock ran. Auto mode approves later rounds without asking, so it only runs "
                "inside the shell sandbox, and the sandbox is not active here (bubblewrap is "
                "needed — see 'cobirb doctor'). Use \"autonomy\": \"ask\", or install bubblewrap."
            )
            return run

    stager = Stager(
        orchestrator, cwd, objective, turns=plan_turns, trace=run.trace,
        decide=_unless_autopilot(ask.decide, orchestrator) if settings.autonomy == AUTONOMY_ASK else None,
        show=ask.show, speaking=getattr(ask, "speaking", None),
    )
    ask.show("Brainy Birb is writing the overview…")
    try:
        with _without_project_verification(orchestrator):
            problem = stager.overview()
    except RuntimeError as exc:
        run.stopped_at = "error"
        run.report = f"No flock ran: planning stopped because the model server failed.\n\n{exc}"
        return run
    run.design = stager.design.document()
    if problem.startswith("declined:"):
        run.stopped_at = "planning"
        run.report = "Brainy Birb decided this work should not be divided: " + problem[len("declined:"):].strip()
        return run
    if problem:
        run.stopped_at = "charter"
        run.report = f"No flock ran: {problem}."
        return run
    ask.show(run.design)

    # ---- Restatement: everything after this works from the cleared design - #
    ask.show("Brainy Birb is restating the design for Architect Birb…")
    try:
        problem = stager.clear()
    except RuntimeError as exc:
        run.stopped_at = "error"
        run.report = f"No flock ran: planning stopped because the model server failed.\n\n{exc}"
        return run
    run.design = stager.design.record()
    if problem:
        run.stopped_at = "restatement"
        run.report = f"No flock ran: {problem}."
        return run
    if stager.design.names:
        ask.show("Names restated for Architect Birb and the Worker Birbs:\n" + stager.design.names.describe())

    tickets = list(stager.design.tickets)
    previous: dict[str, str] = {}
    approved: list[Charter] = []
    last_failing: tuple | None = None
    # What an evaluation said cannot be done on this machine, for the report.
    left: list[str] = []
    for round_number in range(1, settings.max_rounds + 1):
        # ---- Skeleton, then one stage per ticket -------------------------- #
        try:
            with _without_project_verification(orchestrator):
                known = {p for c in approved for w in c.workers for p in w.writes}
                fresh = [t for t in tickets if any(p not in known for p in t.writes)]
                if fresh:
                    ask.show(f"Round {round_number}: Architect Birb is writing the skeleton…")
                    stager.skeleton(fresh)
                briefs = {}
                for ticket in tickets:
                    ask.show(f"Round {round_number}: Architect Birb is planning ticket '{ticket.id}'…")
                    # The plan is written from the cleared design, so it is the
                    # brief as it stands — restating it again would only drift.
                    briefs[ticket.id] = stager.ticket_plan(ticket, previous.get(ticket.id, ""))
            charter = stager.charter(tickets, briefs)
        except RuntimeError as exc:
            run.stopped_at = "error"
            run.report = (f"Stopped in round {round_number}: planning failed because the model "
                          f"server failed.\n\n{exc}")
            return run
        run.charter = charter
        run.design = stager.design.record()
        if on_charter is not None:
            try:
                on_charter(charter)
            except Exception:  # noqa: BLE001 - a display is not worth the run
                logger.debug("a charter handler raised", exc_info=True)

        # ---- Approval ----------------------------------------------------- #
        if not approved or _autonomy(settings, orchestrator) == AUTONOMY_ASK:
            detail = CharterApproval(charter, approved, stager.design.names)
            question = (
                f"Approve this charter? {len(charter.workers)} Worker Birb(s) will run "
                "unattended inside exactly these scopes, with no further prompts."
                if not approved else
                f"Approve round {round_number}? {len(charter.workers)} ticket(s) to try again or add."
            )
            ask.show(detail)
            if not ask.confirm(question, detail):
                run.stopped_at = "approval"
                run.report = ("Charter not approved; nothing ran." if not approved else
                              f"Round {round_number} not approved; stopped after round {round_number - 1}.")
                break
        else:
            ask.show(f"Round {round_number} approved automatically (auto mode, inside the sandbox).\n"
                     + approval_changes(charter, approved))
        approved.append(charter)

        # ---- Fan out ------------------------------------------------------ #
        concurrency = charter.effective_concurrency
        if probe and concurrency > 1 and round_number == 1 and ask.confirm(
            "Check whether your model endpoint serves two requests at once?",
            "It takes a few seconds. A server that queues them would make a concurrent "
            "flock quietly sequential.",
        ):
            run.probe = probe_concurrency(orchestrator.model)
            ask.show(run.probe.describe())
            if run.probe.concurrent is False:
                concurrency = 1
        ask.show(f"Round {round_number}: fanning out {len(charter.workers)} ticket(s), "
                 f"{concurrency} at a time…")
        outcome = run_flock(
            charter, cwd, config=config, concurrency=concurrency, stop=stop,
            on_event=on_event, io_for=io_for, canceller=canceller,
            grants=getattr(orchestrator, "grants", None),
            refuse=lambda: _autopilot(orchestrator),
        )
        run.outcome = outcome
        run.rounds.append(outcome)
        ask.show(outcome.describe())

        # ---- Stop, or plan the next round --------------------------------- #
        if outcome.all_done or outcome.stopped or (stop is not None and stop.is_set()):
            break
        failing = _failing(outcome)
        if failing == last_failing:
            run.stopped_at = "no_progress"
            ask.show("Stopping: this round ended with the same tickets failing the same way as the last.")
            break
        last_failing = failing
        if round_number >= settings.max_rounds:
            run.stopped_at = "rounds"
            break
        try:
            next_tickets, whys, evaluation = stager.evaluate(
                round_number, _round_verdict(outcome),
                outstanding=[r.worker_id for r in outcome.outstanding])
            left += [line for line in left_to_do(evaluation) if line not in left]
        except RuntimeError as exc:
            ask.show(f"Stopping: the evaluation failed because the model server failed ({exc}).")
            break
        next_tickets, whys = _with_contradicted_tests(outcome, stager.design.tickets, next_tickets, whys)
        if not next_tickets:
            break
        problem = check_tickets(next_tickets)
        if problem:
            ask.show(f"Stopping: the next round's tickets could not be used — {problem}.")
            break
        reports = {r.worker_id: r for r in outcome.reports}
        previous = {
            t.id: "\n".join(filter(None, [
                f"What must change: {whys.get(t.id, '')}",
                f"Last plan:\n{stager.design.plans.get(t.id, '')}",
                f"Report: {reports[t.id].report_text()}" if t.id in reports else "",
            ]))
            for t in next_tickets
        }
        by_id = {t.id: t for t in stager.design.tickets}
        by_id.update({t.id: t for t in next_tickets})
        stager.design.tickets = list(by_id.values())
        tickets = next_tickets

    run.design = stager.design.record()
    if run.rounds:
        lines = [f"Flock finished after {len(run.rounds)} round(s)."]
        for index, outcome in enumerate(run.rounds, 1):
            lines.append(f"\n--- Round {index} ---\n{outcome.describe()}")
        if run.stopped_at == "no_progress":
            lines.append("\nStopped: the last round changed nothing.")
        elif run.stopped_at == "rounds":
            lines.append(f"\nStopped at the round cap ({settings.max_rounds}).")
        lines += _left_elsewhere(stager.design.sections.get(LIMITS_HEADING, ""), left)
        run.report = "\n".join(lines)
    return run


def _left_elsewhere(limits: str, left: list[str]) -> list[str]:
    """The report's account of what could not be done on this machine.

    Some work cannot pass here however well it is written — a call into the
    Windows API, on Linux — and a flock that simply reported those tickets as
    failed would leave the user to work out why, and what to do. Brainy Birb
    names these limits in the overview and after each round; they go to the
    user in its words.
    """
    lines = []
    if limits.strip() and not limits.strip().lower().rstrip(".").startswith("none"):
        lines += ["", f"--- {LIMITS_HEADING} (from the design) ---", limits.strip()]
    if left:
        lines += ["", "--- Left to do elsewhere (from the evaluation) ---"] + [f"- {item}" for item in left]
    return lines


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
        if run.design:
            flock_session.session.add_text("design", run.design)
        outcomes = run.rounds or ([run.outcome] if run.outcome else [])
        for number, outcome in enumerate(outcomes, 1):
            prefix = f"round {number}: " if len(outcomes) > 1 else ""
            for report in outcome.reports:
                flock_session.session.add_text(prefix + report.worker_id, report.describe())
            for review in outcome.reviews:
                flock_session.session.add_text(f"{prefix}review:{review.worker_id}", review.describe())
        flock_session.session.summary = run.report
        flock_session.save(password)
    except Exception:  # noqa: BLE001 - a lost record must not cost the round
        logger.warning("could not write the flock session file", exc_info=True)
