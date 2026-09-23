"""Render docs/manual/models.md from a cobirb-bench results directory.

The compatibility table is generated, not written: a hand-maintained "works
well with" list is a claim nobody re-checks, and it drifts the first time a
release changes how CoBirb talks to a model. Regenerate it from a fresh run:

    python bench/compat_table.py bench/results/<run> > docs/manual/models.md
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


def render(results_dir: Path) -> str:
    meta = json.loads((results_dir / "meta.json").read_text())
    rows = [json.loads(line) for line in (results_dir / "results.jsonl").read_text().splitlines() if line]
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)

    lines = [
        "# Models",
        "",
        "How well CoBirb works with the models it has been measured against, from its own offline",
        "benchmark (`bench/`): small real tasks — fixing bugs, implementing from a spec, refactoring",
        "across files, writing tests — each checked by tests the model never sees.",
        "",
        f"Measured at commit `{meta['sha'][:10]}`, {meta['tasks']} tasks × {meta['reps']} runs each, "
        "on one local Ollama.",
        "",
        "| Model | Tasks passed | Harder tasks | Median time per task |",
        "|---|---|---|---|",
    ]
    for model, results in sorted(by_model.items(), key=lambda kv: -sum(r["passed"] for r in kv[1])):
        hard = [r for r in results if r["task"].startswith("hard-")]
        passed = sum(r["passed"] for r in results)
        hard_cell = f"{sum(r['passed'] for r in hard)}/{len(hard)}" if hard else "—"
        lines.append(
            f"| `{model}` | {passed}/{len(results)} ({100 * passed / len(results):.0f}%) | {hard_cell} | "
            f"{statistics.median(r['elapsed'] for r in results):.0f}s |"
        )
    lines += [
        "",
        "What the failures were, where there were any:",
        "",
    ]
    for model, results in sorted(by_model.items()):
        failures = Counter(r["outcome"] for r in results if not r["passed"])
        if failures:
            lines.append(f"- `{model}`: " + ", ".join(f"{n}× {kind.replace('_', ' ')}" for kind, n in failures.most_common()))
    lines += [
        "",
        "These are small tasks on one machine; treat the numbers as a floor for what works, not a",
        "ranking of the models. Any model that can call tools can be used — see",
        "[config](config.md) for pointing CoBirb at yours.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(Path(sys.argv[1])), end="")
