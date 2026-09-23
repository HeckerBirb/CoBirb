"""Run one flock session for cobirb-bench and print what happened as JSON.

``cobirb flock`` refuses to run headless, rightly: the charter is the one place
a person sees what the Worker Birbs will be allowed to touch. The benchmark has
nobody to ask, so this driver answers "yes" — acceptable only because it runs
in a throwaway copy of a fixture, and never anywhere a person's work lives.
It is the only place anything approves a charter on its own.

Invoked by cobirb_bench.py with the frozen source on PYTHONPATH:
    python flock_driver.py <cwd> <objective>
"""
from __future__ import annotations

import json
import sys

_WRITES = {"write_file", "edit_file", "apply_patch", "delete_file"}


def main() -> int:
    cwd, objective = sys.argv[1], sys.argv[2]
    from cobirb.flock.run import Asker, run_flock_session
    from cobirb.runtime import wiring
    from cobirb.runtime.headless import HeadlessIO

    orchestrator = wiring.build_orchestrator(cwd, {}, io_factory=HeadlessIO)
    try:
        run = run_flock_session(
            orchestrator, objective, cwd,
            ask=Asker(confirm=lambda prompt, detail="": True), probe=False,
        )
    finally:
        orchestrator.close()
    reports = run.outcome.reports if run.outcome else []
    # Every worker's calls, pooled, so the runner classifies a failed round the
    # way it classifies a single agent: wrote nothing → no_change, wrote
    # something the checker rejected → wrong_result. This used to send an empty
    # list, which made every failed round that ran read as no_change.
    calls = [call for r in reports for call in (r.tool_calls or [])]
    print(json.dumps({
        "ok": run.ran and all(r.ok for r in reports),
        "stop_reason": run.stopped_at or "answered",
        "summary": run.report[:2000],
        "workers": len(reports),
        "worker_ok": sum(1 for r in reports if r.ok),
        # Per worker, because the round report is Brainy Birb's account and in
        # the first measurement it called rounds successes that left stubs
        # unimplemented — the workers' own reports are what say which ticket failed.
        "worker_reports": [
            {"id": r.worker_id, "ok": r.ok, "accepted": r.accepted,
             "accepted_when_finished": getattr(r, "accepted_when_finished", r.accepted),
             "turns": r.turns,
             "wrote": any(c.get("ok") and c.get("name") in _WRITES for c in r.tool_calls),
             # What each refusal was aimed at: the evidence for or against
             # "workers fail because they cannot run what they need".
             "denied": [
                 {"tool": c["name"], "target": c.get("target", "")}
                 for c in r.tool_calls if c.get("denied")
             ],
             "summary": (r.summary or "")[:600], "error": (r.error or "")[:400]}
            for r in reports
        ],
        "charter": (
            {"seams": [s.at for s in run.charter.seams],
             "workers": {w.id: list(w.writes) for w in run.charter.workers}}
            if getattr(run, "charter", None) else None
        ),
        "turns": sum(r.turns for r in reports),
        "tool_calls": calls,
        "denied": [c.get("target") or c["name"] for c in calls if c.get("denied")],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
