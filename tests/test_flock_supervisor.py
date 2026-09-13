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
from cobirb.flock.supervisor import check_partition, run_flock
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
    def run(worker, cwd, **kwargs):
        if worker.id == "a":
            return WorkerReport(worker_id="a", ok=False, error="it fell over")
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
    def run(worker, cwd, **kwargs):
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
