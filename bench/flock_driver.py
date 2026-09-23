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
    print(json.dumps({
        "ok": run.ran and all(r.ok for r in reports),
        "stop_reason": run.stopped_at or "answered",
        "summary": run.report[:2000],
        "workers": len(reports),
        "worker_ok": sum(1 for r in reports if r.ok),
        "turns": 0,
        "tool_calls": [],
        "denied": [],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
