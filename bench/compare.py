"""Compare cobirb-bench runs: what changed, and whether it is more than noise.

    python bench/compare.py bench/results/<before> bench/results/<after> [more ...]

The first run is the baseline; every other run is compared with it. Prints
Markdown, so the output can go straight into a note or a pull request.

Two things this refuses to leave implicit, because both were easy to get wrong
by hand:

- **Whether the runs measured the same thing.** Different tasks, models, reps,
  window or extra config are printed as warnings above the numbers. A different
  commit is not a warning — that is usually the point of comparing — but it is
  always shown.
- **Whether a difference is evidence.** A task at 3 reps moves in steps of 33
  points, and a model's total wanders by a pass or two between identical runs.
  Each change in pass count carries a two-sided Fisher exact p-value, so "6/6 →
  2/6" (p ≈ 0.06) and "2/6 → 3/6" (p = 1.0) do not read alike.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

# The keys in meta.json that must match for two runs to be comparable.
_SETTINGS = ("tasks", "reps", "models", "max_num_ctx", "extra", "flock")


def load(path: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((path / "meta.json").read_text())
    rows = [json.loads(line) for line in (path / "results.jsonl").read_text().splitlines() if line]
    return meta, rows


def fisher(a: int, n: int, b: int, m: int) -> float:
    """Two-sided Fisher exact p for a/n passes against b/m passes."""
    total, passes = n + m, a + b
    denominator = math.comb(total, passes)
    if denominator == 0:
        return 1.0

    def p(x: int) -> float:
        return math.comb(n, x) * math.comb(m, passes - x) / denominator

    observed = p(a)
    low, high = max(0, passes - m), min(n, passes)
    return min(1.0, sum(p(x) for x in range(low, high + 1) if p(x) <= observed * (1 + 1e-9)))


def _cell(rows: list[dict]) -> tuple[int, int]:
    return sum(r["passed"] for r in rows), len(rows)


def _pct(passed: int, total: int) -> str:
    return f"{passed}/{total} ({100 * passed / total:.0f}%)" if total else "—"


def _delta(base: tuple[int, int], other: tuple[int, int]) -> str:
    (a, n), (b, m) = base, other
    if not n or not m:
        return "—"
    change = 100 * b / m - 100 * a / n
    return f"{change:+.0f} pts, p={fisher(a, n, b, m):.2f}"


def _refused_program(target: str, tool: str) -> str:
    """What a refused call wanted, in a few words: the first program a shell
    command runs, marked when a `cd` came before it — the record does not say
    which segment was refused, and before 0.43 it was usually the `cd` —
    otherwise the tool."""
    if tool != "shell" or not target:
        return tool
    segments = target.replace("&&", ";").replace("||", ";").replace("|", ";").split(";")
    words = [segment.split() for segment in segments if segment.split()]
    after_cd = bool(words) and words[0][0] == "cd"
    rest = [w for w in words if w[0] != "cd"]
    if not rest:
        return "shell cd"
    return f"shell {rest[0][0]}" + (" (after cd)" if after_cd else "")


def compare(paths: list[Path]) -> str:
    runs = [(path.name, *load(path)) for path in paths]
    base_name, base_meta, _ = runs[0]
    out = ["# cobirb-bench comparison", ""]

    out += ["| Run | Commit | Tasks × reps | Window | Extra config | Flock |", "|---|---|---|---|---|---|"]
    for name, meta, _ in runs:
        out.append(
            f"| `{name}` | `{meta['sha'][:10]}` | {meta['tasks']} × {meta['reps']} | "
            f"{meta.get('max_num_ctx', '')} | `{json.dumps(meta.get('extra', {}))}` | "
            f"{'yes' if meta.get('flock') else 'no'} |"
        )
    warnings = [
        f"`{name}` differs from `{base_name}` in {key}: "
        f"{json.dumps(base_meta.get(key))} → {json.dumps(meta.get(key))}"
        for name, meta, _ in runs[1:]
        for key in _SETTINGS
        if meta.get(key) != base_meta.get(key)
    ]
    if warnings:
        out += ["", "**Not like for like:**", ""] + [f"- {w}" for w in warnings]

    models = sorted({r["model"] for _, _, rows in runs for r in rows})
    tasks = sorted({r["task"] for _, _, rows in runs for r in rows})

    def by(rows: list[dict], **match) -> list[dict]:
        return [r for r in rows if all(r[k] == v for k, v in match.items())]

    header = "| Model | " + " | ".join(f"`{name}`" for name, _, _ in runs) + " |"
    rule = "|---|" + "---|" * len(runs)
    out += ["", "## Pass rate", "", header, rule]
    for model in models + ["**all**"]:
        cells = []
        base = _cell(by(runs[0][2], model=model) if model != "**all**" else runs[0][2])
        for index, (_, _, rows) in enumerate(runs):
            cell = _cell(by(rows, model=model) if model != "**all**" else rows)
            text = _pct(*cell)
            if index:
                text += f" · {_delta(base, cell)}"
            cells.append(text)
        out.append(f"| {model} | " + " | ".join(cells) + " |")

    out += ["", "## Per task and model", "", "| Task | Model | " +
            " | ".join(f"`{name}`" for name, _, _ in runs) + " |", "|---|---|" + "---|" * len(runs)]
    for task in tasks:
        for model in models:
            cells = [by(rows, task=task, model=model) for _, _, rows in runs]
            if not any(cells):
                continue
            texts = []
            for rows in cells:
                passed, total = _cell(rows)
                failures = Counter(r["outcome"] for r in rows if not r["passed"])
                why = ", ".join(f"{n}× {k}" for k, n in failures.most_common())
                texts.append(f"{passed}/{total}" + (f" ({why})" if why else "") if total else "—")
            if len({t.split(' ')[0] for t in texts}) > 1:  # only rows that changed
                out.append(f"| {task} | {model} | " + " | ".join(texts) + " |")

    out += ["", "## Failures by cause", "", "| Cause | " +
            " | ".join(f"`{name}`" for name, _, _ in runs) + " |", rule]
    causes = sorted({r["outcome"] for _, _, rows in runs for r in rows if not r["passed"]})
    for cause in causes:
        out.append(f"| {cause} | " + " | ".join(
            str(sum(1 for r in rows if not r["passed"] and r["outcome"] == cause)) for _, _, rows in runs
        ) + " |")

    out += ["", "## Time", "", "| | " + " | ".join(f"`{name}`" for name, _, _ in runs) + " |", rule]
    out.append("| median seconds per run | " + " | ".join(
        f"{statistics.median(r['elapsed'] for r in rows):.0f}" if rows else "—" for _, _, rows in runs
    ) + " |")

    if any(meta.get("flock") for _, meta, _ in runs):
        out += ["", "## Flock", "", "| | " + " | ".join(f"`{name}`" for name, _, _ in runs) + " |", rule]

        def row(label: str, value) -> None:
            out.append(f"| {label} | " + " | ".join(str(value(rows)) for _, _, rows in runs) + " |")

        workers = lambda rows: [w for r in rows for w in r.get("workers") or []]  # noqa: E731
        row("rounds with no charter", lambda rows: sum(1 for r in rows if not r.get("charter")
                                                       and r.get("stop_reason") != "answered"))
        row("workers that did not run", lambda rows: sum(1 for w in workers(rows) if not w.get("ok")))
        row("workers whose own check failed",
            lambda rows: sum(1 for w in workers(rows) if w.get("ok") and w.get("accepted") is False))
        row("refused calls", lambda rows: sum(len(w.get("denied") or []) for w in workers(rows)))

        refusals = [Counter(_refused_program(d.get("target", ""), d.get("tool", ""))
                            for w in workers(rows) for d in w.get("denied") or [])
                    for _, _, rows in runs]
        kinds = sorted(set().union(*refusals), key=lambda k: -sum(c[k] for c in refusals))
        if kinds:
            out += ["", "Refused calls, by what they wanted:", "", "| Wanted | " +
                    " | ".join(f"`{name}`" for name, _, _ in runs) + " |", rule]
            for kind in kinds[:12]:
                out.append(f"| {kind} | " + " | ".join(str(c[kind]) for c in refusals) + " |")

    out += ["", "p is a two-sided Fisher exact test on the pass counts; below 0.05 is a difference "
            "worth believing, and anything near 1 is the same result twice.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__.splitlines()[2].strip())
    print(compare([Path(p) for p in sys.argv[1:]]), end="")
