"""cobirb-bench: does CoBirb finish real tasks on real local models?

Each task is a small fixture repository, an instruction, and a checker the
agent never sees. For every (model, task, repetition) the runner copies the
fixture into a scratch directory, runs CoBirb headless against it exactly as a
user would (``cobirb -p ... --headless --output json``), then runs the checker
over whatever the agent left behind. A run passes only if the checker does.

Three decisions shape the numbers, and each is deliberate:

- **CoBirb runs from a frozen copy of one commit** (``git worktree``), not the
  working tree. A benchmark takes hours; code changed during a run must not
  leak into its results, and every result records the commit it measured.
- **Sampling is pinned by seed, not by temperature.** ``temperature: 0`` makes
  small models loop on themselves, which would measure the setting rather than
  the harness. A fixed seed per repetition keeps a run repeatable while each
  repetition still samples the way a user's model does.
- **Every failure is classified by cause.** A pass rate moves by 100/N points
  per task and wanders between runs; "unparsed tool call went from 9 to 0" is
  a fact about a fix even when the total barely moves.

Nothing here talks to anything but the endpoint named on the command line
(default: the local Ollama). Results are files on disk under ``bench/results/``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TASKS_DIR = HERE / "tasks"
RESULTS_DIR = HERE / "results"

# The file tools every task may use inside its own scratch directory — the
# grant a user makes by trusting a project directory. Shell is per task.
_READ_TOOLS = ("read_file", "list_dir", "glob", "grep", "repo_map")
_WRITE_TOOLS = ("write_file", "edit_file", "apply_patch")

# What a tool call looks like when a model writes it as text instead of making
# it: Hermes/Qwen <tool_call> JSON, Qwen3-coder's XML, gpt-oss channel markers,
# or a bare JSON object naming one of our tools.
_TOOL_NAMES = "|".join((*_READ_TOOLS, *_WRITE_TOOLS, "shell"))
_TEXT_TOOL_CALL = re.compile(
    r"<tool_call>|<function=|<\|call\|>|<\|channel\|>"
    rf"|\"name\"\s*:\s*\"(?:{_TOOL_NAMES})\""
    rf"|\b(?:{_TOOL_NAMES})\s*\(\s*\{{",
)

FAILURE_CLASSES = (
    "pass", "turn_limit", "no_progress", "unparsed_tool_call", "edit_miss", "no_change",
    "wrong_result", "error", "timeout", "crash",
)


@dataclass
class Task:
    id: str
    path: Path
    prompt: str
    category: str
    changes_files: bool
    shell: list[str] = field(default_factory=list)
    timeout: int = 900

    @classmethod
    def load(cls, path: Path) -> "Task":
        data = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
        return cls(
            id=path.name,
            path=path,
            prompt=data["prompt"].strip(),
            category=data.get("category", "edit"),
            changes_files=bool(data.get("changes_files", True)),
            shell=list(data.get("shell", [])),
            timeout=int(data.get("timeout", 900)),
        )


@dataclass
class Result:
    model: str
    task: str
    category: str
    rep: int
    seed: int
    outcome: str
    passed: bool
    turns: int = 0
    tool_calls: int = 0
    failed_calls: int = 0
    writes: int = 0
    denied: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    elapsed: float = 0.0
    summary: str = ""
    error: str = ""
    check_output: str = ""


# --------------------------------------------------------------------------- #
# Running one task
# --------------------------------------------------------------------------- #
def _config(task: Task, work: Path, model: str, base_url: str, seed: int,
            max_num_ctx: str, extra: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "models": {"default": {"name": model, "base_url": base_url, "options": {"seed": seed}}},
        "max_num_ctx": max_num_ctx,
        "allow_read_dirs": [str(work)],
        "allow_write_dirs": [str(work)],
        "allow_tools": [f"shell({command})" for command in task.shell],
        # Generation on a busy or offloaded model can be slow to start.
        "request_timeout": 1800,
    }
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = {**config[key], **value}
        else:
            config[key] = value
    return config


def _environment(home: Path, src: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["COBIRB_HOME"] = str(home)
    # The frozen source wins over the editable install in the venv.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH", "")]))
    # `python` and `pytest` in a task's shell resolve to this interpreter's
    # environment, which has pytest installed.
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    for key in ("COBIRB_MODEL_NAME", "COBIRB_OLLAMA_URL"):
        env.pop(key, None)
    return env


def _classify(task: Task, report: dict[str, Any] | None, passed: bool) -> str:
    if passed:
        return "pass"
    if report is None:
        return "crash"
    if report.get("stop_reason") in ("turn_limit", "no_progress"):
        return report["stop_reason"]
    summary = report.get("summary") or ""
    if _TEXT_TOOL_CALL.search(summary):
        return "unparsed_tool_call"
    if not report.get("ok"):
        return "error"
    calls = report.get("tool_calls") or []
    wrote = [c for c in calls if c["name"] in _WRITE_TOOLS and c.get("ok")]
    missed = [c for c in calls if c["name"] in ("edit_file", "apply_patch") and not c.get("ok")]
    if task.changes_files and not wrote:
        return "edit_miss" if missed else "no_change"
    return "wrong_result"


def _check(task: Task, work: Path, scratch: Path, summary: str) -> tuple[bool, str]:
    answer = scratch / "answer.txt"
    answer.write_text(summary, encoding="utf-8")
    hidden = scratch / "hidden"
    if (task.path / "hidden").is_dir():
        shutil.copytree(task.path / "hidden", hidden)
        (hidden / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    env = dict(os.environ, BENCH_ANSWER=str(answer), BENCH_HIDDEN=str(hidden),
               BENCH_TASK=str(task.path), PYTHONPATH=str(work))
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    checker = task.path / "check.py"
    if checker.is_file():
        command = [sys.executable, str(checker)]
    else:
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "-c", str(hidden / "pytest.ini"), "--rootdir", str(hidden), str(hidden)]
    try:
        done = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "checker timed out"
    output = (done.stdout + done.stderr)[-2000:]
    return done.returncode == 0, output


def run_one(task: Task, model: str, rep: int, seed: int, *, base_url: str, src: Path,
            max_num_ctx: str, extra: dict[str, Any], flock: bool = False) -> Result:
    scratch = Path(tempfile.mkdtemp(prefix=f"bench-{task.id}-"))
    try:
        work = scratch / "work"
        shutil.copytree(task.path / "repo", work)
        home = scratch / "home"
        (home / ".cobirb").mkdir(parents=True)
        (home / ".cobirb" / "config.json").write_text(
            json.dumps(_config(task, work, model, base_url, seed, max_num_ctx, extra), indent=2),
            encoding="utf-8",
        )
        command = (
            [sys.executable, str(HERE / "flock_driver.py"), str(work), task.prompt] if flock
            else [sys.executable, "-m", "cobirb.cli", "-p", task.prompt,
                  "--headless", "--output", "json", "--cwd", str(work)]
        )
        started = time.monotonic()
        report: dict[str, Any] | None = None
        error = ""
        try:
            done = subprocess.run(command, env=_environment(home, src), capture_output=True,
                                  text=True, timeout=task.timeout)
            try:
                report = json.loads(done.stdout)
            except json.JSONDecodeError:
                error = (done.stderr or done.stdout)[-1500:]
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            return Result(model, task.id, task.category, rep, seed, "timeout", False,
                          elapsed=elapsed, error=f"no result within {task.timeout}s")
        elapsed = time.monotonic() - started

        summary = (report or {}).get("summary") or ""
        passed, check_output = _check(task, work, scratch, summary)
        calls = (report or {}).get("tool_calls") or []
        return Result(
            model=model, task=task.id, category=task.category, rep=rep, seed=seed,
            outcome=_classify(task, report, passed), passed=passed,
            turns=(report or {}).get("turns", 0),
            tool_calls=len(calls),
            failed_calls=sum(1 for c in calls if not c.get("ok")),
            writes=sum(1 for c in calls if c["name"] in _WRITE_TOOLS and c.get("ok")),
            denied=(report or {}).get("denied", []),
            stop_reason=(report or {}).get("stop_reason"),
            elapsed=round(elapsed, 1),
            summary=summary[:600],
            error=error or ((report or {}).get("error") or ""),
            check_output=check_output,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The frozen source tree
# --------------------------------------------------------------------------- #
def freeze_source(revision: str, into: Path) -> tuple[Path, str]:
    """Check ``revision`` out into its own worktree; return its ``src`` and full sha."""
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", revision],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(into), sha],
                   capture_output=True, text=True, check=True)
    return into / "src", sha


def release_source(path: Path) -> None:
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(path)],
                   capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarise(results: list[Result], meta: dict[str, Any]) -> str:
    by_model: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        by_model[result.model].append(result)
    lines = [f"# cobirb-bench — {meta['label']}", "",
             f"- commit `{meta['sha'][:10]}`, {meta['tasks']} task(s) × {meta['reps']} rep(s)",
             f"- max_num_ctx {meta['max_num_ctx']}; extra config {json.dumps(meta['extra'])}", "",
             "| Model | Pass rate | Spread across reps | Median turns | Median time |",
             "|---|---|---|---|---|"]
    for model, rows in by_model.items():
        per_rep = defaultdict(list)
        for row in rows:
            per_rep[row.rep].append(row.passed)
        rates = [100 * sum(v) / len(v) for v in per_rep.values()]
        spread = f"{min(rates):.0f}–{max(rates):.0f}%" if len(rates) > 1 else "—"
        lines.append(
            f"| {model} | {statistics.mean(rates):.0f}% | {spread} | "
            f"{statistics.median(r.turns for r in rows):.0f} | "
            f"{statistics.median(r.elapsed for r in rows):.0f}s |"
        )
    lines += ["", "## Outcomes", "", "| Model | " + " | ".join(FAILURE_CLASSES) + " |",
              "|---|" + "---|" * len(FAILURE_CLASSES)]
    for model, rows in by_model.items():
        counts = Counter(r.outcome for r in rows)
        lines.append(f"| {model} | " + " | ".join(str(counts.get(c, 0)) for c in FAILURE_CLASSES) + " |")
    lines += ["", "## Per task", "", "| Task | " + " | ".join(by_model) + " |",
              "|---|" + "---|" * len(by_model)]
    for task in sorted({r.task for r in results}):
        cells = []
        for model in by_model:
            rows = [r for r in by_model[model] if r.task == task]
            passed = sum(r.passed for r in rows)
            outcomes = ",".join(sorted({r.outcome for r in rows if not r.passed}))
            cells.append(f"{passed}/{len(rows)}" + (f" ({outcomes})" if outcomes else ""))
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", required=True, help="comma-separated model names")
    parser.add_argument("--tasks", default="*", help="glob over task ids (default: all)")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1000, help="seed of rep 1; rep n uses seed+n-1")
    parser.add_argument("--revision", default="HEAD", help="commit to measure (default: HEAD)")
    parser.add_argument("--base-url", default=os.environ.get("COBIRB_OLLAMA_URL", "http://localhost:11434"))
    parser.add_argument("--max-num-ctx", default="32k",
                        help="context ceiling for the run (default 32k; the fixtures are small)")
    parser.add_argument("--config", default="{}", help="extra config JSON merged over the run's own")
    parser.add_argument("--label", default="run")
    parser.add_argument("--flock", action="store_true",
                        help="run each task as a flock session (charter auto-approved) instead of one agent")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    tasks = sorted((Task.load(p) for p in TASKS_DIR.glob(args.tasks) if (p / "task.toml").is_file()),
                   key=lambda t: t.id)
    if not tasks:
        print(f"no tasks match {args.tasks!r}", file=sys.stderr)
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    extra = json.loads(args.config)
    out = args.out or RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.label}"
    out.mkdir(parents=True, exist_ok=True)

    tree = Path(tempfile.mkdtemp(prefix="bench-src-")) / "tree"
    src, sha = freeze_source(args.revision, tree)
    meta = {"label": args.label, "sha": sha, "tasks": len(tasks), "reps": args.reps, "flock": args.flock,
            "models": models, "max_num_ctx": args.max_num_ctx, "extra": extra}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    results: list[Result] = []
    try:
        with (out / "results.jsonl").open("a", encoding="utf-8") as sink:
            for model in models:  # model-major: loading a model is the slow part
                for rep in range(1, args.reps + 1):
                    for task in tasks:
                        result = run_one(task, model, rep, args.seed + rep - 1, base_url=args.base_url,
                                         src=src, max_num_ctx=args.max_num_ctx, extra=extra,
                                         flock=args.flock)
                        results.append(result)
                        sink.write(json.dumps(asdict(result)) + "\n")
                        sink.flush()
                        print(f"[{model} r{rep}] {task.id:<28} {result.outcome:<18} "
                              f"{result.turns:>3} turns {result.elapsed:>6.0f}s", flush=True)
    finally:
        release_source(tree)
    report = summarise(results, meta)
    (out / "summary.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
