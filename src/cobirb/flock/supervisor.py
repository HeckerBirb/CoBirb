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
fifth blew up would be a poor trade. The exception is a worker that declared
it ``needs`` the failed one: it is skipped and says so, because building
against a seam nobody built produces work that cannot be reviewed and a report
nobody can act on.

**Most workers depend on nothing, and the scheduler is built for that.** A
charter's default shape is independent tickets, so ``needs`` is usually empty
and every worker is admissible at once. Where it is declared, a worker waits
for its dependencies *before* taking a concurrency slot — waiting while
holding one would deadlock any chain longer than the limit.

**Stopping is checked between workers, not inside one.** A model call in
flight cannot be interrupted, which is already true of a single-agent turn.
Asking to stop means no further workers start and the run reports what it has.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Config
from ..runtime.verify import DEFAULT_TIMEOUT_SECONDS, run_verification
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
            # Both halves, because stopping can land in either: before a worker
            # started, or after it finished but before it was reviewed. Saying
            # only "never ran" would misreport a worker whose work is on disk
            # and simply unchecked.
            lines.append(
                "Stopped early at your request; some workers never ran, and some may have "
                "finished without being reviewed."
            )
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
            # Complete, but not on their own: their check failed when they
            # finished and passed on the final tree. Worth saying, because it
            # means those tickets were not as independent as the charter said.
            late = [
                r.worker_id for r in self.complete if r.rechecked and r.accepted_when_finished is not True
            ]
            if late:
                lines.append(
                    f"Passed only once every worker had finished: {', '.join(late)} — "
                    "each one's check depended on another ticket's work."
                )
        return "\n".join(lines)


class Canceller:
    """A handle for force-stopping workers that are stuck mid-model-call.

    ``stop`` (the graceful ``Event`` the supervisor already respects) only
    keeps *new* workers from starting — it cannot touch one that is blocked
    waiting on the model, because that thread is not checking anything. This is
    the harder stop: it holds the live workers' orchestrators and calls
    ``cancel()`` on each, which drops their model connections and, with Ollama,
    makes the server abort the generation.

    Held by the front-end (so Ctrl+C can reach it) and passed into the run (so
    the run can register each worker as it starts). Every method is safe to
    call from another thread, which is the entire reason it exists.
    """

    def __init__(self) -> None:
        self._live: set = set()
        self._lock = threading.Lock()
        self._forced = False

    def register(self, orchestrator) -> None:
        with self._lock:
            self._live.add(orchestrator)
            already = self._forced
        # Forced before this worker even opened its connection: cancel it now,
        # so a worker starting during a force-stop does not slip through.
        if already:
            self._cancel(orchestrator)

    def unregister(self, orchestrator) -> None:
        with self._lock:
            self._live.discard(orchestrator)

    def force(self) -> None:
        """Cancel every worker in flight, now."""
        with self._lock:
            self._forced = True
            live = list(self._live)
        for orchestrator in live:
            self._cancel(orchestrator)

    @property
    def forced(self) -> bool:
        return self._forced

    @staticmethod
    def _cancel(orchestrator) -> None:
        try:
            orchestrator.cancel()
        except Exception:  # one worker that will not die is not worth the rest
            logger.debug("force-cancelling a worker raised", exc_info=True)


class Slots:
    """The flock's real concurrency limit, as something a worker can put down.

    Concurrency used to be the thread pool's size, which conflated two
    different things: how many workers *exist* and how many are allowed to be
    working at once. They came apart the moment a Worker Birb could stop to
    ask the user something. A worker waiting on a person is not using the
    model endpoint, not holding the GPU and not making progress — but under a
    sized pool it was still occupying the slot, so a flock with
    ``concurrency = 2`` and two workers waiting on dialogs ran nothing at all.

    So the pool is sized to the number of workers and *this* is the limit. A
    worker holds a slot while it works and gives it back while it is parked:

        with slots:                 # working
            ...
            with slots.released():  # parked on a question
                answer = ask()

    Deliberately a plain semaphore and not a priority queue. A worker whose
    question was just answered rejoins the queue rather than jumping it, which
    means it can wait behind a worker that has not started yet. That was a
    considered trade: the ordering is only visible when more workers are ready
    than there are slots, and the machinery to fix it is a custom waiter queue
    — worth building if the wait ever proves noticeable, not before.
    """

    def __init__(self, limit: int) -> None:
        self._semaphore = threading.Semaphore(max(1, limit))

    def __enter__(self) -> Slots:
        self._semaphore.acquire()
        return self

    def __exit__(self, *exc_info) -> bool:
        self._semaphore.release()
        return False

    @contextlib.contextmanager
    def released(self):
        """Give the slot up for the duration, then wait for one again.

        The re-acquire is in a ``finally`` so a worker that is force-stopped
        while parked still restores the count. A slot leaked here would not
        fail loudly — it would quietly lower the flock's concurrency for the
        rest of the round, which is the kind of bug that gets diagnosed as
        "the endpoint felt slow today".
        """
        self._semaphore.release()
        try:
            yield
        finally:
            self._semaphore.acquire()


def run_flock(
    charter: Charter,
    cwd: str,
    *,
    config: Config | None = None,
    concurrency: int | None = None,
    stop: threading.Event | None = None,
    on_event=None,
    io_for=None,
    canceller: Canceller | None = None,
    grants=None,
    refuse: bool | Callable[[], bool] = False,
    remotes=None,
) -> FlockOutcome:
    """Run one round of a charter and report on it.

    ``concurrency`` overrides the charter's own — used when the endpoint probe
    found a server that serialises and the user chose to carry on anyway, where
    running two at a time would queue rather than parallelise and make the
    transcript harder to read for no gain.

    ``on_event`` is an optional callback taking ``(kind, payload)``, so a
    front-end can show progress without this module knowing what a pane is.
    Kinds: ``"started"``, ``"finished"``, ``"reviewed"``.

    ``io_for(worker, slots)`` optionally supplies each Worker Birb with its own
    I/O adapter — how the TUI gives each one a pane to draw into. Coarse events
    say *that* a worker started; this is what makes its work visible while it
    happens. It receives ``slots`` as well as the worker because an adapter
    that can stop to ask the user something has to be able to put its
    concurrency slot down first (see ``Slots``); one that only draws can
    ignore it.

    ``grants`` is the session's approval store, passed to each worker so an
    answer of "allow for the session" reaches the workers that have not
    started yet as well as the one that asked.

    ``remotes`` (a ``cobirb.remote.pool.RemotePool``) runs each ticket with a
    ``runs_on`` as a Remote Worker Birb: one task per remote, outside the
    concurrency limit, on the first idle remote of its OS. Such a ticket's
    re-check and review run on its remote too — a Windows test is judged on
    Windows. Kind ``"waiting_remote"`` says a ticket is waiting for one.
    """
    return _Round(
        charter,
        cwd,
        config=config or Config(),
        limit=max(1, concurrency or charter.concurrency),
        stop=stop or threading.Event(),
        on_event=on_event,
        io_for=io_for,
        canceller=canceller,
        grants=grants,
        refuse=refuse,
        remotes=remotes,
    ).run()


class _Round:
    """One round of a charter: fan out, join, re-check, review.

    The workers run on a pool's threads and meet here — each finished worker's
    report, the events its dependents wait on, the remote runs kept for review
    — so that shared state lives on this object, behind ``_lock``.
    """

    def __init__(
        self,
        charter: Charter,
        cwd: str,
        *,
        config: Config,
        limit: int,
        stop: threading.Event,
        on_event,
        io_for,
        canceller: Canceller | None,
        grants,
        refuse: bool | Callable[[], bool],
        remotes,
    ) -> None:
        self.charter = charter
        self.cwd = cwd
        self.config = config
        self.limit = limit
        self.stop = stop
        self.on_event = on_event
        self.io_for = io_for
        self.canceller = canceller
        self.grants = grants
        self.refuse = refuse
        self.refusing = refuse if callable(refuse) else (lambda: bool(refuse))
        self.remotes = remotes
        self.slots = Slots(limit)
        # One per worker, set when it stops running however it ended. Dependents
        # wait on these rather than polling, and the `finally` that sets them is
        # what keeps a failed or skipped worker from hanging everything behind it.
        self.finished = {worker.id: threading.Event() for worker in charter.workers}
        self.reports: dict[str, WorkerReport] = {}
        # Remote tickets' runs, kept until after review: their workspaces are
        # where their checks run.
        self.remote_runs: dict = {}
        self._lock = threading.Lock()

    def run(self) -> FlockOutcome:
        started_at = time.monotonic()
        workers = list(self.charter.workers)
        # Captured before anything runs: this *is* the skeleton, and without it
        # there is nothing to diff against and no stub to put back.
        baseline = Baseline.capture(self.charter, self.cwd)
        outcome = FlockOutcome(charter=self.charter)

        logger.info("flock: %d worker(s), %d at a time", len(workers), self.limit)
        # Sized to the workers, with `slots` holding the real limit — a worker
        # parked on a question needs its thread kept alive while costing nothing
        # against the concurrency budget. `pool.map` still does the dispatching,
        # so `reports` stays in charter order, which `complete`, `outstanding`
        # and the review loop all rely on.
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(workers))) as pool:
            outcome.reports = list(pool.map(self._record, workers))

        outcome.stopped = self.stop.is_set()
        if not outcome.stopped:
            recheck(
                self.charter,
                outcome.reports,
                self.cwd,
                stop=self.stop,
                config=self.config,
                run_check_for=self._remote_check,
            )
        self._review(outcome, baseline)

        for run in self.remote_runs.values():
            run.end()
            self.remotes.release(run.client)
        outcome.elapsed = time.monotonic() - started_at
        return outcome

    def _announce(self, kind: str, payload) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # a broken display must not fail the run
            logger.debug("a flock event handler raised", exc_info=True)

    def _wait_for_dependencies(self, worker: WorkerBrief) -> str:
        """Block until this worker may start; return why it may not, if so.

        Waited out **before** taking a slot, never while holding one — a
        worker sitting on a slot waiting for a colleague that needs that slot
        to finish is a deadlock, and at ``concurrency = 1`` it would be every
        chain. The pool is sized to the worker count, so a waiting worker
        costs a parked thread and nothing else.
        """
        for need in worker.needs:
            event = self.finished.get(need)
            if event is None:  # pragma: no cover - parse_charter refuses these
                continue
            while not event.wait(timeout=0.2):
                if self.stop.is_set():
                    return "stopped while waiting for " + need
            with self._lock:
                upstream = self.reports.get(need)
            # Gated on whether it *ran*, not on whether its acceptance check
            # passed. A failing check is common and often unrelated to what
            # the dependent needs; a worker that never ran leaves nothing to
            # build against at all.
            if upstream is None or not upstream.ok:
                return f"did not run — its dependency '{need}' failed"
        return ""

    def _run_remote(self, worker: WorkerBrief) -> WorkerReport:
        from ..remote.runner import RemoteRun

        client = self.remotes.acquire(
            worker.runs_on, self.stop, on_wait=lambda: self._announce("waiting_remote", worker)
        )
        if client is None:
            return WorkerReport(
                worker_id=worker.id,
                ok=False,
                error=(
                    "stopped while waiting for a remote"
                    if self.stop.is_set()
                    else f"no {worker.runs_on} remote is available"
                ),
            )
        self._announce("started", worker)
        run = RemoteRun(
            client,
            worker,
            self.cwd,
            config=self.config,
            io=self.io_for(worker, None) if self.io_for else None,
            grants=self.grants,
            refuse=self.refusing,
        )
        with self._lock:
            self.remote_runs[worker.id] = run
        report = run.execute(self.stop)
        self._announce("finished", report)
        return report

    def _run_one(self, worker: WorkerBrief) -> WorkerReport:
        # Checked before waiting and before taking a slot: a stopped flock
        # should not queue up behind the workers still finishing just to
        # decline to run.
        if self.stop.is_set():
            return WorkerReport(worker_id=worker.id, ok=False, error="stopped before it started")
        blocked = self._wait_for_dependencies(worker)
        if blocked:
            return WorkerReport(worker_id=worker.id, ok=False, error=blocked)
        if worker.runs_on and not worker.static and self.remotes is not None:
            return self._run_remote(worker)
        with self.slots:
            if self.stop.is_set():
                return WorkerReport(worker_id=worker.id, ok=False, error="stopped before it started")
            self._announce("started", worker)
            report = run_worker(
                worker,
                self.cwd,
                config=self.config,
                io=self.io_for(worker, self.slots) if self.io_for else None,
                canceller=self.canceller,
                grants=self.grants,
                refuse=self.refuse,
            )
        self._announce("finished", report)
        return report

    def _record(self, worker: WorkerBrief) -> WorkerReport:
        """``_run_one``, published to this worker's dependents when it ends.

        **The order of the two statements in the ``finally`` is the whole
        point.** The report is stored and only then is the event set, because
        a dependent released first would look up a result that is not there
        yet and read a perfectly good worker as a failed one. The placeholder
        covers the path where ``_run_one`` raises — it never does today, since
        ``run_worker`` reports rather than throws, but a dependent left
        waiting forever is not the way to find out that changed.
        """
        report = WorkerReport(worker_id=worker.id, ok=False, error="did not finish and said nothing")
        try:
            report = self._run_one(worker)
            report.static = worker.static
            return report
        finally:
            with self._lock:
                self.reports[worker.id] = report
                running_remotes = [
                    r for wid, r in self.remote_runs.items() if wid != worker.id and wid not in self.reports
                ]
            self.finished[worker.id].set()
            # A remote worker reading this one's files gets them now, as a
            # local one would on its next read of the shared tree.
            if report.ok:
                for run in running_remotes:
                    shared = [p for p in run.worker.reads if p in set(worker.writes)]
                    if shared:
                        run.push(shared)

    def _remote_check(self, worker_id: str):
        """How to run a check for this worker on its remote, or ``None`` for here."""
        run = self.remote_runs.get(worker_id)
        if run is None:
            return None
        return lambda command, seconds, overrides=None: run.check(command, seconds, overrides)

    def _review(self, outcome: FlockOutcome, baseline: Baseline) -> None:
        """Review every worker, after the join and never during it.

        Review puts an implementation back to its stub for a moment, and a
        colleague still running could import it.

        **Checked between reviews, for the same reason it is checked between
        workers.** A review is not cheap and it is not read-only: each one
        writes over the worker's files, runs the acceptance command with its
        own timeout, and puts the files back in a `finally`. So a stopped round
        went on rewriting the user's tree and spawning test runs for minutes
        after they asked it not to — and `Canceller` cannot reach any of it,
        because reviews run subprocesses rather than orchestrators. Worse, the
        window between mutating and restoring is one `review.py` documents as
        survivable only for an outright kill; a stop followed by quitting the
        app landed squarely in it.

        Never mid-review: the restore is part of the operation, so a review
        that has started finishes and puts its files back.
        """
        for report in outcome.reports:
            if self.stop.is_set():
                outcome.stopped = True
                logger.info("flock: stopping before reviewing %s", report.worker_id)
                break
            worker = self.charter.worker(report.worker_id)
            if worker is None:  # pragma: no cover - reports are built from workers
                continue
            review = review_worker(worker, baseline, self.cwd, run_check=self._remote_check(worker.id))
            outcome.reviews.append(review)
            self._announce("reviewed", review)


def recheck(
    charter: Charter,
    reports: list[WorkerReport],
    cwd: str,
    *,
    stop: threading.Event | None = None,
    config: Config | None = None,
    run_check_for: Callable[[str], Callable | None] | None = None,
) -> None:
    """Run every finished worker's acceptance check again, now that all are done.

    **A worker's own verdict is from the moment it finished**, and in a flock
    that is often the wrong moment. A CLI worker whose tests construct a real
    ``Store`` finishes while the store worker is still writing it, runs its
    check against stubs that raise, and reports "acceptance check FAILED" —
    about a round whose final tree passes. The reverse happens too: a check
    that passed can be broken by a colleague who finished later. The round's
    account is about the tree the user is left with, so it is checked here, on
    that tree, with no model involved.

    Updates each report in place and keeps the earlier verdict in
    ``accepted_when_finished``, because a disagreement between the two is
    itself worth reading: it says which tickets were not as independent as the
    charter claimed.
    """
    timeout = int((config or Config()).get("verify_timeout", default=DEFAULT_TIMEOUT_SECONDS))
    for report in reports:
        if stop is not None and stop.is_set():
            return
        worker = charter.worker(report.worker_id)
        if worker is None or not report.ok or not worker.accept.strip():
            continue
        remote = run_check_for(worker.id) if run_check_for is not None else None
        result = (
            remote(worker.accept, timeout)
            if remote is not None
            else run_verification(worker.accept, cwd, timeout)
        )
        if result.error:
            # The check could not run at all; the worker's own verdict is the
            # better evidence than none.
            continue
        report.accepted_when_finished = report.accepted
        report.accepted = result.ok
        report.accept_output = result.output
        report.rechecked = True


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
