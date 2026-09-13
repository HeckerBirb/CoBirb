"""Running a whole flock: fan out, join, review, report.

This is the piece that turns an approved charter into work. It is deliberately
thin — the workers are ordinary agent runs (``worker.run_worker``) and the
review is ordinary file handling (``review``) — so what lives here is the
ordering, the concurrency, and the honest accounting at the end.

Three ordering decisions, each with a reason:

**Workers run concurrently; reviews run afterwards, one at a time.** Workers
own disjoint files, so their writes cannot collide — that is what exclusive
ownership buys. Review cannot join them, because reviewing means putting a
worker's implementation back to its stub for a moment, and a colleague whose
acceptance check imports that file would fail for a reason that has nothing to
do with its own work. So: fan out, join, *then* review.

**A worker that fails does not stop the others.** Brainy Birb needs the whole
picture to plan the next round, and losing four finished tickets because the
fifth blew up would be a poor trade.

**Stopping is checked between workers, not inside one.** A model call in
flight cannot be interrupted, which is already true of a single-agent turn.
Asking to stop means no further workers start and the run reports what it has.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, field

from ..config import Config
from .charter import Charter, WorkerBrief, describe_conflicts, find_conflicts
from .review import Baseline, Review, review_worker
from .worker import WorkerReport, run_worker

logger = logging.getLogger("cobirb")


@dataclass
class FlockOutcome:
    """Everything one round came to, for Brainy Birb and for the user.

    Holds reports and reviews separately because they answer different
    questions. A report says what a worker *did* and what it could not do; a
    review says whether to believe it.
    """

    charter: Charter
    reports: list[WorkerReport] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    stopped: bool = False
    elapsed: float = 0.0

    @property
    def complete(self) -> list[WorkerReport]:
        """Workers that finished their ticket and whose work survived review."""
        clean = {review.worker_id for review in self.reviews if review.clean}
        return [r for r in self.reports if r.complete and r.worker_id in clean]

    @property
    def outstanding(self) -> list[WorkerReport]:
        """Everything the next round would have to pick up."""
        done = {r.worker_id for r in self.complete}
        return [r for r in self.reports if r.worker_id not in done]

    @property
    def all_done(self) -> bool:
        return bool(self.reports) and not self.outstanding and not self.stopped

    def review_for(self, worker_id: str) -> Review | None:
        for review in self.reviews:
            if review.worker_id == worker_id:
                return review
        return None

    def describe(self) -> str:
        """The round's account of itself, for a person to read.

        Leads with what is *not* done. A report that opens with successes reads
        as progress even when the interesting half is the remainder, and the
        remainder is what the next decision is about.
        """
        lines = [
            f"Flock round finished in {self.elapsed:.0f}s — "
            f"{len(self.complete)} of {len(self.reports)} ticket(s) complete."
        ]
        if self.stopped:
            lines.append("Stopped early at your request; some workers never ran.")
        if self.outstanding:
            lines.append("")
            lines.append("Still outstanding:")
            for report in self.outstanding:
                lines.append(f"  {report.describe()}")
                review = self.review_for(report.worker_id)
                if review is not None and not review.clean:
                    lines.append("    " + review.describe().replace("\n", "\n    "))
        if self.complete:
            lines.append("")
            lines.append("Complete: " + ", ".join(r.worker_id for r in self.complete))
        return "\n".join(lines)


class FlockStopped(Exception):
    """Raised by nothing; reserved so callers can tell a stop from a failure."""


def run_flock(
    charter: Charter,
    cwd: str,
    *,
    config: Config | None = None,
    concurrency: int | None = None,
    stop: threading.Event | None = None,
    on_event=None,
    io_for=None,
) -> FlockOutcome:
    """Run one round of a charter and report on it.

    ``concurrency`` overrides the charter's own — used when the endpoint probe
    found a server that serialises and the user chose to carry on anyway, where
    running two at a time would queue rather than parallelise and make the
    transcript harder to read for no gain.

    ``on_event`` is an optional callback taking ``(kind, payload)``, so a
    front-end can show progress without this module knowing what a pane is.
    Kinds: ``"started"``, ``"finished"``, ``"reviewed"``.

    ``io_for(worker)`` optionally supplies each Worker Birb with its own I/O
    adapter — how the TUI gives each one a pane to draw into. Coarse events say
    *that* a worker started; this is what makes its work visible while it
    happens.
    """
    config = config or Config()
    stop = stop or threading.Event()
    workers = list(charter.workers)
    limit = max(1, concurrency or charter.concurrency)
    started_at = time.monotonic()

    def announce(kind: str, payload) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:  # noqa: BLE001 - a broken display must not fail the run
            logger.debug("a flock event handler raised", exc_info=True)

    # Captured before anything runs: this *is* the skeleton, and without it
    # there is nothing to diff against and no stub to put back.
    baseline = Baseline.capture(charter, cwd)
    outcome = FlockOutcome(charter=charter)

    def run_one(worker: WorkerBrief) -> WorkerReport:
        if stop.is_set():
            return WorkerReport(worker_id=worker.id, ok=False, error="stopped before it started")
        announce("started", worker)
        report = run_worker(
            worker, cwd, config=config, io=io_for(worker) if io_for else None
        )
        announce("finished", report)
        return report

    logger.info("flock: %d worker(s), %d at a time", len(workers), limit)
    with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as pool:
        outcome.reports = list(pool.map(run_one, workers))

    outcome.stopped = stop.is_set()

    # After the join, never during it. Review puts an implementation back to
    # its stub for a moment, and a colleague still running could import it.
    for report in outcome.reports:
        worker = charter.worker(report.worker_id)
        if worker is None:  # pragma: no cover - reports are built from workers
            continue
        review = review_worker(worker, baseline, cwd)
        outcome.reviews.append(review)
        announce("reviewed", review)

    outcome.elapsed = time.monotonic() - started_at
    return outcome


def check_partition(charter: Charter) -> tuple[bool, str]:
    """Whether this charter's scopes hold together, and what to say if not.

    Separate from ``run_flock`` because the answer belongs to the user before
    approval, not to the machinery afterwards. Overlaps are reported rather
    than refused: merging two workers, hoisting the shared file into a seam
    Brainy Birb owns, splitting it, or letting git reconcile them are all
    reasonable, and which one is right depends on things CoBirb cannot see.
    """
    conflicts = find_conflicts(charter)
    return not conflicts, describe_conflicts(conflicts)
