"""Tests for running a whole flock.

The ordering is what matters here: workers concurrently, reviews afterwards,
and an honest account at the end of what is still outstanding.
"""
from __future__ import annotations

import sys
import textwrap
import threading
import time

from cobirb.flock.charter import parse_charter
import pytest

from cobirb.flock import supervisor
from cobirb.flock.review import Review
from cobirb.flock.supervisor import Canceller, Slots, check_partition, run_flock
from cobirb.flock.worker import WorkerReport


def _charter(body):
    return parse_charter(textwrap.dedent(body))


# A real two-ticket project rather than a sketch: each worker owns a module
# and its tests, and the acceptance checks genuinely depend on the work being
# done. A charter with no `accept` can never report a complete worker — there
# is no definition of done and nothing for review to put back — so testing the
# supervisor against one would only ever exercise the unhappy path.
_STUB = "def {name}(n):\n    raise NotImplementedError\n"
_REAL = "def {name}(n):\n    return n * 2\n"
_TEST = "from {name} import {name}\n\ndef test_it_doubles():\n    assert {name}(2) == 4\n"


def _two_ticket_project(tmp_path):
    """Write the skeleton two workers would be fanned out against."""
    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text(_STUB.format(name=name))
        (tmp_path / f"test_{name}.py").write_text(_TEST.format(name=name))
    return textwrap.dedent(f"""
        objective = "two independent things"
        concurrency = 2

        [[workers]]
        id     = "a"
        writes = ["a.py", "test_a.py"]
        tests  = ["test_a.py"]
        accept = '"{sys.executable}" -m pytest test_a.py -q'
        brief  = "Implement a."

        [[workers]]
        id     = "b"
        writes = ["b.py", "test_b.py"]
        tests  = ["test_b.py"]
        accept = '"{sys.executable}" -m pytest test_b.py -q'
        brief  = "Implement b."
    """)


def _fake_workers(monkeypatch, behaviour):
    """Replace the worker runner, so these tests are about the supervisor."""
    from cobirb.flock import supervisor

    monkeypatch.setattr(supervisor, "run_worker", behaviour)


def _honest_worker(delay=0.0):
    """A worker that actually implements its stub, as a good one would."""
    def run(worker, cwd, **kwargs):
        if delay:
            time.sleep(delay)
        import os

        with open(os.path.join(cwd, f"{worker.id}.py"), "w", encoding="utf-8") as fh:
            fh.write(_REAL.format(name=worker.id))
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True, summary="did it")

    return run


# --------------------------------------------------------------------------- #
# Fanning out
# --------------------------------------------------------------------------- #
def test_every_worker_in_the_charter_runs(monkeypatch, tmp_path):
    _fake_workers(monkeypatch, _honest_worker())

    outcome = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path))

    assert {r.worker_id for r in outcome.reports} == {"a", "b"}
    assert "return n * 2" in (tmp_path / "a.py").read_text()
    assert "return n * 2" in (tmp_path / "b.py").read_text()


def test_workers_run_at_the_same_time(monkeypatch, tmp_path):
    """Two at a time is the default, and it has to be real — the whole reason
    the concurrency was built in from the first round rather than promised."""
    live = []
    peak = []
    lock = threading.Lock()

    def run(worker, cwd, **kwargs):
        with lock:
            live.append(worker.id)
            peak.append(len(live))
        time.sleep(0.15)
        with lock:
            live.remove(worker.id)
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path))

    assert max(peak) == 2


def test_concurrency_can_be_held_down_to_one(monkeypatch, tmp_path):
    """Used when the endpoint probe found a server that serialises: running
    two would queue rather than parallelise, and make the transcript harder to
    read for no gain."""
    live = []
    peak = []
    lock = threading.Lock()

    def run(worker, cwd, **kwargs):
        with lock:
            live.append(worker.id)
            peak.append(len(live))
        time.sleep(0.1)
        with lock:
            live.remove(worker.id)
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path), concurrency=1)

    assert max(peak) == 1


def test_one_worker_failing_does_not_take_down_the_others(monkeypatch, tmp_path):
    """Brainy Birb needs the whole picture to plan the next round; losing the
    finished tickets because one blew up would be a poor trade."""
    honest = _honest_worker()

    def run(worker, cwd, **kwargs):
        if worker.id == "a":
            return WorkerReport(worker_id="a", ok=False, error="it fell over")
        # 'b' implements its stub, as a real worker does: a worker that changed
        # nothing cannot be reviewed, so it would never reach `complete` and the
        # surviving ticket would look like a casualty of a's failure.
        honest(worker, cwd, **kwargs)
        return WorkerReport(worker_id="b", ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    outcome = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path))

    assert len(outcome.reports) == 2
    assert [r.worker_id for r in outcome.outstanding] == ["a"]


def test_asking_to_stop_means_no_further_workers_start(monkeypatch, tmp_path):
    """A model call in flight cannot be interrupted — already true of a
    single-agent turn. Stopping means nothing further begins."""
    stop = threading.Event()
    stop.set()
    _fake_workers(monkeypatch, _honest_worker())

    outcome = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path), stop=stop)

    assert outcome.stopped
    assert all(not r.ok for r in outcome.reports)
    assert "NotImplementedError" in (tmp_path / "a.py").read_text()  # untouched


def test_progress_is_announced_without_the_supervisor_knowing_what_a_pane_is(
    monkeypatch, tmp_path
):
    _fake_workers(monkeypatch, _honest_worker())
    seen = []

    run_flock(
        _charter(_two_ticket_project(tmp_path)), str(tmp_path),
        on_event=lambda kind, payload: seen.append(kind),
    )

    assert seen.count("started") == 2
    assert seen.count("finished") == 2
    assert seen.count("reviewed") == 2


def test_a_broken_event_handler_does_not_fail_the_run(monkeypatch, tmp_path):
    _fake_workers(monkeypatch, _honest_worker())

    def boom(kind, payload):
        raise RuntimeError("the pane exploded")

    outcome = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path), on_event=boom)

    assert len(outcome.reports) == 2


# --------------------------------------------------------------------------- #
# Review comes after the join
# --------------------------------------------------------------------------- #
def test_reviews_run_after_every_worker_has_finished(monkeypatch, tmp_path):
    """Reviewing means putting an implementation back to its stub for a
    moment. A colleague still running whose check imports that file would fail
    for a reason that has nothing to do with its own work."""
    order = []

    def run(worker, cwd, **kwargs):
        time.sleep(0.05)
        order.append(f"work:{worker.id}")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    run_flock(
        _charter(_two_ticket_project(tmp_path)), str(tmp_path),
        on_event=lambda kind, payload: order.append(kind) if kind == "reviewed" else None,
    )

    assert order.index("work:a") < order.index("reviewed")
    assert order.index("work:b") < order.index("reviewed")


def test_work_that_fails_review_is_not_counted_complete(monkeypatch, tmp_path):
    """The acceptance check passing is not enough on its own — that is the
    whole point of reviewing. A suite that passes against an unimplemented
    function passes for the wrong reason."""
    stub = "def double(n):\n    raise NotImplementedError\n"
    vacuous = "import work\n\ndef test_it_exists():\n    assert work is not None\n"
    (tmp_path / "work.py").write_text(stub)
    (tmp_path / "test_work.py").write_text(vacuous)

    charter = _charter(f"""
        objective = "x"
        [[workers]]
        id     = "a"
        writes = ["work.py", "test_work.py"]
        tests  = ["test_work.py"]
        accept = '"{sys.executable}" -m pytest test_work.py -q'
        brief  = "go"
    """)

    def run(worker, cwd, **kwargs):
        (tmp_path / "work.py").write_text("def double(n):\n    return n * 2\n")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    outcome = run_flock(charter, str(tmp_path))

    assert outcome.reports[0].complete  # its own check passed
    assert not outcome.complete  # but review says otherwise
    assert not outcome.all_done


# --------------------------------------------------------------------------- #
# The account of itself
# --------------------------------------------------------------------------- #
def test_the_report_leads_with_what_is_not_done(monkeypatch, tmp_path):
    """A report that opens with successes reads as progress even when the
    remainder is the interesting half — and the remainder is what the next
    decision is about."""
    honest = _honest_worker()

    def run(worker, cwd, **kwargs):
        # Both implement their stub, as a real worker does — a worker that
        # changed nothing is not reviewable, and 'b' has to reach `complete`
        # for this test to be about the ordering of the report.
        honest(worker, cwd, **kwargs)
        if worker.id == "a":
            return WorkerReport(worker_id="a", ok=True, accepted=False, summary="could not finish")
        return WorkerReport(worker_id="b", ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    text = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path)).describe()

    assert "Still outstanding" in text
    assert text.index("Still outstanding") < text.index("Complete:")


def test_a_fully_finished_round_says_so(monkeypatch, tmp_path):
    _fake_workers(monkeypatch, _honest_worker())

    outcome = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path))

    assert outcome.all_done
    assert "2 of 2" in outcome.describe()


# --------------------------------------------------------------------------- #
# The partition check, which belongs to the user before approval
# --------------------------------------------------------------------------- #
def test_a_disjoint_partition_passes_the_check(tmp_path):
    ok, message = check_partition(_charter(_two_ticket_project(tmp_path)))

    assert ok
    assert "disjoint" in message


def test_an_overlapping_partition_is_explained_rather_than_refused():
    ok, message = check_partition(_charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["shared.py"]
        brief = "go"
        [[workers]]
        id = "b"
        writes = ["shared.py"]
        brief = "go"
    """))

    assert not ok
    assert "shared.py" in message
    assert "both write it" in message


# --------------------------------------------------------------------------- #
# Canceller — the force-stop handle for workers blocked mid-model-call.
#
# The graceful `stop` Event only keeps new workers from starting; a worker
# already blocked waiting on the model never checks anything, so the only way
# to reach it is to hold its live orchestrator and call cancel() on it
# directly. The interesting case is the race: a worker that starts *after*
# force() was called must not slip through un-cancelled.
# --------------------------------------------------------------------------- #
class _FakeOrchestrator:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def test_force_cancels_every_registered_worker():
    canceller = Canceller()
    a, b = _FakeOrchestrator(), _FakeOrchestrator()
    canceller.register(a)
    canceller.register(b)

    canceller.force()

    assert a.cancelled == 1
    assert b.cancelled == 1


def test_a_worker_that_never_registered_is_unaffected():
    canceller = Canceller()
    canceller.force()  # nothing registered yet; must not raise

    assert canceller.forced


def test_a_worker_registered_after_force_is_cancelled_immediately():
    """The race this exists to close: a worker starting during a force-stop
    must not slip through un-cancelled just because it was not live yet when
    force() ran."""
    canceller = Canceller()
    canceller.force()

    late = _FakeOrchestrator()
    canceller.register(late)

    assert late.cancelled == 1


def test_unregistering_stops_further_force_calls_reaching_it():
    canceller = Canceller()
    worker = _FakeOrchestrator()
    canceller.register(worker)
    canceller.unregister(worker)

    canceller.force()

    assert worker.cancelled == 0


def test_a_worker_whose_cancel_raises_does_not_block_the_others():
    """One worker that will not die is not worth losing the rest."""
    class _Exploding(_FakeOrchestrator):
        def cancel(self):
            raise RuntimeError("boom")

    canceller = Canceller()
    exploding = _Exploding()
    fine = _FakeOrchestrator()
    canceller.register(exploding)
    canceller.register(fine)

    canceller.force()  # must not raise

    assert fine.cancelled == 1


def test_force_is_reflected_in_a_worker_started_and_reported_from_run_flock(monkeypatch, tmp_path):
    """Wired all the way through run_flock: force() reaches a live worker's
    real orchestrator, not just a Canceller used in isolation."""
    from cobirb.flock import supervisor

    seen_cancellers = []

    def fake_run_worker(worker, cwd, *, canceller=None, **_):
        seen_cancellers.append(canceller)
        if canceller is not None:
            canceller.register(_FakeOrchestrator())
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    monkeypatch.setattr(supervisor, "run_worker", fake_run_worker)
    canceller = Canceller()

    run_flock(
        _charter(_two_ticket_project(tmp_path)), str(tmp_path), canceller=canceller
    )

    assert all(c is canceller for c in seen_cancellers)


# --------------------------------------------------------------------------- #
# Asking the user costs the asker, not the round.
# --------------------------------------------------------------------------- #
def test_a_worker_waiting_on_the_user_lets_another_one_run(monkeypatch, tmp_path):
    """The whole feature, at the one concurrency where it is unambiguous. With
    a single slot, a worker parked on a question must let its colleague past —
    otherwise a flock with everyone waiting on dialogs runs nothing at all."""
    order = []
    parked = threading.Event()

    def run(worker, cwd, *, io=None, **kwargs):
        if worker.id == "a":
            order.append("a-asks")
            with io.released():        # parked on a question
                parked.set()
                time.sleep(0.2)
            order.append("a-resumes")
        else:
            parked.wait(timeout=2)
            order.append("b-runs-anyway")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    run_flock(
        _charter(_two_ticket_project(tmp_path)), str(tmp_path), concurrency=1,
        io_for=lambda worker, slots: slots,
    )

    assert order.index("b-runs-anyway") < order.index("a-resumes")


def test_a_parked_worker_gives_the_slot_back_even_if_it_blows_up(monkeypatch, tmp_path):
    """A leaked slot would not fail loudly — it would quietly lower the
    flock's concurrency for the rest of the round."""
    slots = Slots(1)

    with pytest.raises(RuntimeError):
        with slots.released():
            raise RuntimeError("cancelled while parked")

    # The count is intact: something can still take the only slot.
    with slots:
        pass


def test_the_concurrency_limit_still_holds_when_nobody_asks(monkeypatch, tmp_path):
    """The pool is sized to the workers now, so the limit lives in the slots
    rather than in the pool — and it has to still be a limit."""
    live, peak, lock = [], [], threading.Lock()

    def run(worker, cwd, **kwargs):
        with lock:
            live.append(worker.id)
            peak.append(len(live))
        time.sleep(0.1)
        with lock:
            live.remove(worker.id)
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path), concurrency=1)

    assert max(peak) == 1


# --------------------------------------------------------------------------- #
# Dependencies between workers.
# --------------------------------------------------------------------------- #
def _dependent_project(tmp_path):
    """A seam one worker builds and another builds against."""
    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text(_STUB.format(name=name))
    return textwrap.dedent("""
        objective = "a seam, then work against it"
        concurrency = 4

        [[workers]]
        id     = "a"
        writes = ["a.py"]
        brief  = "Build the seam."

        [[workers]]
        id     = "b"
        writes = ["b.py"]
        needs  = ["a"]
        brief  = "Build against it."
    """)


def test_a_dependent_worker_does_not_start_until_its_dependency_finishes(
    monkeypatch, tmp_path
):
    """The one thing independence cannot express: b's whole job is to build on
    something a has to exist first."""
    order = []

    def run(worker, cwd, **kwargs):
        order.append(f"start:{worker.id}")
        time.sleep(0.1)
        order.append(f"end:{worker.id}")
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    run_flock(_charter(_dependent_project(tmp_path)), str(tmp_path))

    assert order == ["start:a", "end:a", "start:b", "end:b"]


def test_a_worker_whose_dependency_failed_is_skipped_and_says_why(monkeypatch, tmp_path):
    """Building against a seam nobody built produces work that cannot be
    reviewed and a report nobody can act on."""
    def run(worker, cwd, **kwargs):
        if worker.id == "a":
            return WorkerReport(worker_id="a", ok=False, error="it fell over")
        return WorkerReport(worker_id="b", ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    outcome = run_flock(_charter(_dependent_project(tmp_path)), str(tmp_path))

    skipped = next(r for r in outcome.reports if r.worker_id == "b")
    assert not skipped.ok
    assert "'a'" in skipped.error
    assert {r.worker_id for r in outcome.outstanding} == {"a", "b"}


def test_a_dependency_whose_acceptance_check_failed_still_lets_the_next_one_run(
    monkeypatch, tmp_path
):
    """Gated on whether it ran, not on whether its check passed: a failing
    check is common and often has nothing to do with what the dependent
    needs, and one flaky test should not kill a whole subtree."""
    def run(worker, cwd, **kwargs):
        if worker.id == "a":
            return WorkerReport(worker_id="a", ok=True, accepted=False)
        return WorkerReport(worker_id="b", ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    outcome = run_flock(_charter(_dependent_project(tmp_path)), str(tmp_path))

    ran = next(r for r in outcome.reports if r.worker_id == "b")
    assert ran.ok and ran.accepted


def test_waiting_for_a_dependency_does_not_hold_a_concurrency_slot(monkeypatch, tmp_path):
    """A worker sitting on a slot while waiting for a colleague that needs
    that slot to finish is a deadlock — and at concurrency 1 it would be every
    chain. The proof is simply that this run terminates."""
    _fake_workers(monkeypatch, _honest_worker())

    outcome = run_flock(
        _charter(_dependent_project(tmp_path)), str(tmp_path), concurrency=1
    )

    assert len(outcome.reports) == 2
    assert all(r.ok for r in outcome.reports)


def test_stopping_releases_workers_waiting_on_a_dependency(monkeypatch, tmp_path):
    """Otherwise ctrl+c on a chain leaves the dependents parked forever."""
    stop = threading.Event()

    def run(worker, cwd, **kwargs):
        stop.set()      # a finishes, and the flock is stopped while b waits
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)

    outcome = run_flock(_charter(_dependent_project(tmp_path)), str(tmp_path), stop=stop)

    assert outcome.stopped
    assert len(outcome.reports) == 2


def test_reports_stay_in_charter_order_however_they_were_scheduled(monkeypatch, tmp_path):
    """`complete`, `outstanding` and the review loop all read this order."""
    _fake_workers(monkeypatch, _honest_worker())

    outcome = run_flock(_charter(_dependent_project(tmp_path)), str(tmp_path))

    assert [r.worker_id for r in outcome.reports] == ["a", "b"]


def test_asking_to_stop_also_stops_the_review_passes(monkeypatch, tmp_path):
    """A review is not read-only: it rewrites the worker's files, runs the
    acceptance command, and puts them back. A stopped round that kept doing
    that went on touching the user's tree for minutes after they said stop."""
    reviewed = []
    stop = threading.Event()
    honest = _honest_worker()

    def run(worker, cwd, **kwargs):
        honest(worker, cwd, **kwargs)
        stop.set()  # the user asks to stop once the first worker is done
        return WorkerReport(worker_id=worker.id, ok=True, accepted=True)

    _fake_workers(monkeypatch, run)
    monkeypatch.setattr(
        supervisor, "review_worker",
        lambda worker, baseline, cwd, **k: reviewed.append(worker.id) or Review(worker.id),
    )

    outcome = run_flock(_charter(_two_ticket_project(tmp_path)), str(tmp_path), stop=stop)

    assert reviewed == []
    assert outcome.stopped
    assert "without being reviewed" in outcome.describe()
