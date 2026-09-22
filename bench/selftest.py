"""Prove every checker is honest before trusting a single benchmark number.

For each task: the untouched fixture (with a useless answer) must FAIL its
checker, and the fixture overlaid with ``solution/`` must PASS it. A checker
that passes the untouched fixture measures nothing; one that fails the
reference solution measures the checker.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cobirb_bench import TASKS_DIR, Task, _check  # noqa: E402


def attempt(task: Task, solved: bool) -> bool:
    scratch = Path(tempfile.mkdtemp(prefix="selftest-"))
    try:
        work = scratch / "work"
        shutil.copytree(task.path / "repo", work)
        answer = "I could not work it out."
        solution = task.path / "solution"
        if solved and solution.is_dir():
            for path in solution.rglob("*"):
                if path.is_file() and path.name != "ANSWER":
                    target = work / path.relative_to(solution)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(path, target)
            if (solution / "ANSWER").is_file():
                answer = (solution / "ANSWER").read_text()
        passed, output = _check(task, work, scratch, answer)
        if passed != solved:
            print(f"  {'solution' if solved else 'fixture'}: {output[-400:]}")
        return passed
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    bad = 0
    for path in sorted(TASKS_DIR.iterdir()):
        if not (path / "task.toml").is_file():
            continue
        task = Task.load(path)
        untouched, solved = attempt(task, False), attempt(task, True)
        ok = not untouched and solved
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {task.id:<30} fixture {'passes' if untouched else 'fails'}, "
              f"solution {'passes' if solved else 'fails'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
