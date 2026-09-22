"""Running one Worker Birb.

Almost nothing happens here, and that is the point. A Worker Birb is an
ordinary CoBirb run whose prompt came from Brainy Birb instead of from a
person, so this module builds a policy out of the brief, hands it to
``wiring.build_subagent``, runs one turn, and writes down what came back.
There is no worker runtime, no sandbox, no special execution mode — those
would all be machinery that drifts away from the real agent within a month.

What this *does* own is the ground rules a worker is told (see
``WORKER_RULES``) and the report it produces. The rules are not project
knowledge — they are the definition of done that comes attached to a ticket,
and they carry the one invariant the whole design rests on: **a Worker Birb
never changes its contract.** Everything it cannot do inside the contract comes
back as a written shortcoming for the next round.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import Config
from ..runtime.wiring import build_subagent
from .charter import WorkerBrief, policy_for

logger = logging.getLogger("cobirb")

# How many model turns one brief gets. Generous compared to the verify loop's
# budget because this is the whole of a worker's work rather than a follow-up
# fix, and bounded because a worker that has not converged by now is reporting
# a shortcoming rather than one turn from finishing.
DEFAULT_MAX_TURNS = 12

# How many times a worker may be *started*. Not a retry of the work — see
# `run_worker` for the condition, which is that nothing happened at all.
#
# The failure this exists for: several workers against one endpoint, the first
# one generating, and the others' opening request timing out while it waited its
# turn. That raised out of the provider, `except Exception` turned it into a
# report, and the ticket was gone — with the model never having seen the brief.
# A whole ticket lost to a fault that cost nothing to retry.
START_ATTEMPTS = 3

# A pause between those attempts. Short: the interesting case is an endpoint
# that was restarting or briefly wedged, where a couple of seconds is the whole
# difference, and a request that timed out has already waited
# `request_timeout` seconds by definition. Deliberately not a backoff ladder —
# a retry against a busy endpoint queues *behind* the work that made it busy, so
# more attempts spaced further apart buy depth in a queue rather than patience.
# Patience is `model.DEFAULT_REQUEST_TIMEOUT`'s job.
START_RETRY_SECONDS = 2.0

# Prepended to every brief. Deliberately short: everything here is either an
# invariant of the design or something a worker cannot discover for itself,
# and anything longer starts to be project knowledge, which a worker does not
# get.
WORKER_RULES = """\
You are a Worker Birb: one engineer on one ticket, working inside a larger \
piece of work you have deliberately not been told about. Another agent \
(Brainy Birb) designed the interfaces, wrote the skeleton and the failing \
tests, and split the work. Your part is below.

How this works:

1. The signatures, types and docstrings you were given are a contract other \
people are building against right now. DO NOT CHANGE THEM. If the contract \
cannot express what you need, implement everything it does allow, leave the \
rest explicitly unimplemented, and say so in your final answer — changing it \
yourself would silently break work you cannot see.
2. The failing tests are your acceptance criteria. Make them pass without \
weakening what they assert. You may and should ADD tests for anything you \
find while implementing — edge cases, error paths, whatever the skeleton did \
not anticipate.
3. You may READ any file in this project to understand it, and you have tools \
for it: `list_dir`, `glob`, `grep`, `read_file`, `repo_map`. USE THOSE, not the \
shell. You have no `find`, no `ls`, no `cat` and will not be given them — \
asking costs you a turn and the user's attention to be told to use the tool you \
already have. You may only CHANGE the files listed below.
4. RUN YOUR OWN ACCEPTANCE CHECK, AS OFTEN AS YOU LIKE. The command below is \
the one thing you may run with `shell`, and it is the definition of done for \
this ticket. Implement, run it, read the failure, fix, run it again. Do not \
write the whole thing and hope — the check is there so you do not have to \
guess whether you are finished. It is also run once more after your turn ends, \
so leaving it failing is not something you can talk your way past.
5. If you need something else you have not been given — another command, a \
tool nobody granted you — ASK for it by using it. The user is shown the \
request and answers it. You pause while they decide; your colleagues keep \
working, so asking costs you time and costs the round nothing. A refusal may \
come back with an instruction telling you what to do instead: that instruction \
is from the user, and it is what to do next.
6. The ONE thing you may never have is a write into a file another Worker \
Birb owns. That is refused outright and is not worth asking for — exclusive \
ownership of files is what lets all of you work at the same time. If your \
ticket seems to need it, that is a finding about the plan: report it (see 7).
7. If you cannot finish something, do not improvise around it. Say plainly: \
what you could not do, why, where it breaks, and either a proposed change to \
the design or a question for Brainy Birb. Being stuck is a normal outcome and \
an honest report is worth more than a guess.

Finish with a short account of what you implemented, what you added tests \
for, and anything you could not do."""


@dataclass
class WorkerReport:
    """What one Worker Birb's run came to, for Brainy Birb to read.

    ``denied`` is the interesting field and the one worth watching. A worker
    that tried to open something outside its scope is not usually misbehaving —
    it is telling you the brief was incomplete or the partition was drawn in
    the wrong place, and that is a finding about the charter rather than about
    the worker.
    """

    worker_id: str
    ok: bool
    summary: str = ""
    accepted: bool | None = None
    accept_output: str = ""
    denied: tuple[str, ...] = ()
    error: str = ""
    turns: int = 0
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether this worker finished its part outright.

        ``accepted is None`` means the charter named no check for it, which is
        not the same as passing — it means nobody said what done looked like,
        so this cannot claim the work is finished.
        """
        return self.ok and self.accepted is True

    def describe(self) -> str:
        """One block a person, or Brainy Birb, can read."""
        if not self.ok:
            return f"[{self.worker_id}] did not run — {self.error}"
        if self.accepted is None:
            verdict = "no acceptance check was configured"
        elif self.accepted:
            verdict = "acceptance check passed"
        else:
            verdict = "acceptance check FAILED"
        lines = [f"[{self.worker_id}] {verdict}, {self.turns} turn(s)"]
        if self.denied:
            lines.append(
                f"    tried to reach outside its scope: {', '.join(sorted(set(self.denied)))}"
            )
        if self.summary:
            lines.append(f"    {self.summary.strip()}")
        return "\n".join(lines)


def compose_brief(worker: WorkerBrief, cwd: str = "") -> str:
    """The whole prompt a Worker Birb is given: the rules, then its ticket.

    The file lists are stated here as well as enforced by the policy, and both
    are deliberate. The policy is what *makes* the isolation true; telling the
    worker its boundaries is what stops it wasting turns discovering them by
    being refused.

    **The working directory is stated here because nothing else tells it.**
    ``Orchestrator.run`` appends a "Working directory:" line to the system
    prompt only ``if (system or self.project_context)``, and a worker has
    neither — ``build_subagent`` passes ``project_context=""`` and the flock
    passes ``system=""``, both on purpose. So the line was dropped and workers
    asked for ``pwd``, which is a shell command nobody granted them: a whole
    approval dialog to learn a fact that costs one line to state.
    """
    parts = [WORKER_RULES, "", "--- Your ticket ---", "", worker.brief.strip(), ""]
    if cwd:
        parts.append(f"You are working in: {cwd}")
        parts.append("Every path below is relative to it.")
    parts.append(f"Files you may change: {', '.join(worker.writes)}")
    parts.append(
        "You may read anything else in the project to understand it, but change "
        "only the files above."
    )
    if worker.reads:
        parts.append(
            "Pay particular attention to these — they are the interfaces your work "
            f"has to fit: {', '.join(worker.reads)}"
        )
    if worker.accept:
        parts.append(
            f"Your work is done when this passes: {worker.accept}\n"
            "Run it yourself with `shell`, as many times as you need — it is the one "
            "command you are permitted. It is also run once more after your turn ends."
        )
    return "\n".join(parts)


def run_worker(
    worker: WorkerBrief,
    cwd: str,
    *,
    config: Config | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    io=None,
    canceller=None,
    grants=None,
) -> WorkerReport:
    """Run one brief to completion and report on it.

    ``io`` lets a front-end watch the work happen — the TUI gives each worker
    an adapter bound to its own pane, so tool calls appear as they are made
    rather than arriving in one lump at the end. Whatever is passed must still
    refuse rather than prompt: the single approval happened at the charter, and
    two workers racing for the same modal is exactly what that decision avoids.
    Default is ``HeadlessIO``, which does refuse.

    Never raises. A worker that blows up is a fact Brainy Birb needs in order
    to plan the next round, and taking down the whole flock because one ticket
    failed would be the wrong trade — the same fail-closed reasoning the rest
    of the codebase uses, applied one level up.
    """
    config = config or Config()
    policy = policy_for(worker, cwd, audit_log_enabled=bool(config.get("audit_log")))
    orchestrator = build_subagent(
        cwd, policy, accept=worker.accept, config=config, io=io,
        # Joins this worker to the session's approvals, so one granted before
        # it started applies to it without asking again — and so one it asks
        # for itself reaches the workers after it.
        grants=grants,
        # Named on its own approval requests: with several workers running,
        # "may I run this?" is only answerable if you know which of them is
        # asking.
        agent_id=worker.id,
    )
    # Registered so a force-stop can reach this worker while it is blocked on
    # the model; a plain graceful stop never gets a chance to, because the
    # thread is not checking anything.
    if canceller is not None:
        canceller.register(orchestrator)

    logger.info("worker %s starting; writes=%s", worker.id, ", ".join(worker.writes))
    brief = compose_brief(worker, cwd)
    try:
        session = None
        for attempt in range(1, START_ATTEMPTS + 1):
            try:
                session = orchestrator.run(
                    brief,
                    system="",
                    cwd=cwd,
                    label=f"worker-{worker.id}",
                    max_turns=max_turns,
                )
                break
            except Exception as exc:  # noqa: BLE001 - one failed ticket is not a failed run
                # **Retried only when nothing happened.** A run that made no
                # tool call did not touch the workspace, so starting it again is
                # clean. One that had already edited files is not retried: the
                # brief would be re-sent against a tree that has moved under it,
                # and a worker half-way through its ticket reporting a
                # shortcoming is a better outcome than one that starts over
                # against its own half-finished work.
                started_work = bool(getattr(orchestrator, "last_run_tool_calls", None))
                stopping = canceller is not None and canceller.forced
                if started_work or stopping or attempt == START_ATTEMPTS:
                    logger.warning("worker %s failed: %s", worker.id, exc)
                    return WorkerReport(
                        worker_id=worker.id,
                        ok=False,
                        error=(
                            f"{type(exc).__name__}: {exc}"
                            + ("" if attempt == 1 else f" (after {attempt} attempts to start)")
                        ),
                    )
                logger.warning(
                    "worker %s could not start (attempt %d/%d): %s",
                    worker.id, attempt, START_ATTEMPTS, exc,
                )
                time.sleep(START_RETRY_SECONDS)
    finally:
        if canceller is not None:
            canceller.unregister(orchestrator)
        orchestrator.close()

    calls = list(orchestrator.last_run_tool_calls)
    verification = orchestrator.last_verification
    return WorkerReport(
        worker_id=worker.id,
        ok=True,
        summary=session.summary or "",
        # None rather than True when nothing was configured: "nobody said what
        # done looks like" must not read as "done".
        accepted=None if verification is None else bool(verification.ok),
        accept_output=getattr(verification, "output", "") or "",
        denied=tuple(call["name"] for call in calls if call.get("denied")),
        turns=len(session.turns),
        tool_calls=calls,
    )
