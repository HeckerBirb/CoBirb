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
3. You can only open the files listed below. Everything else in this project \
is invisible to you, on purpose. You do not need it, and asking for it will \
be refused.
4. If you cannot finish something, do not improvise around it. Say plainly: \
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


def compose_brief(worker: WorkerBrief) -> str:
    """The whole prompt a Worker Birb is given: the rules, then its ticket.

    The file lists are stated here as well as enforced by the policy, and both
    are deliberate. The policy is what *makes* the isolation true; telling the
    worker its boundaries is what stops it wasting turns discovering them by
    being refused.
    """
    parts = [WORKER_RULES, "", "--- Your ticket ---", "", worker.brief.strip(), ""]
    parts.append(f"Files you may change: {', '.join(worker.writes)}")
    if worker.reads:
        parts.append(
            f"Files you may read but must not change: {', '.join(worker.reads)}"
        )
    if worker.accept:
        parts.append(
            f"Your work is done when this passes: {worker.accept}\n"
            "(It is run for you after your turn; you cannot run commands yourself.)"
        )
    return "\n".join(parts)


def run_worker(
    worker: WorkerBrief,
    cwd: str,
    *,
    config: Config | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> WorkerReport:
    """Run one brief to completion and report on it.

    Never raises. A worker that blows up is a fact Brainy Birb needs in order
    to plan the next round, and taking down the whole flock because one ticket
    failed would be the wrong trade — the same fail-closed reasoning the rest
    of the codebase uses, applied one level up.
    """
    config = config or Config()
    policy = policy_for(worker, cwd, audit_log_enabled=bool(config.get("audit_log")))
    orchestrator = build_subagent(cwd, policy, accept=worker.accept, config=config)

    logger.info("worker %s starting; writes=%s", worker.id, ", ".join(worker.writes))
    try:
        session = orchestrator.run(
            compose_brief(worker),
            system="",
            cwd=cwd,
            persona=f"worker-{worker.id}",
            max_turns=max_turns,
        )
    except Exception as exc:  # noqa: BLE001 - one failed ticket is not a failed run
        logger.warning("worker %s failed: %s", worker.id, exc)
        return WorkerReport(worker_id=worker.id, ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
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
