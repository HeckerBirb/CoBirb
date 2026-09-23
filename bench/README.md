# cobirb-bench

Does CoBirb finish real tasks on real local models? This is how we find out, and how a claim that
something "improves reliability" is checked before it is believed. It is for people working on
CoBirb and is not shipped.

## What a run is

Each **task** is a small fixture repository, an instruction, and a checker the agent never sees.
For every model × task × repetition the runner copies the fixture into a scratch directory, runs
CoBirb against it exactly as a user would (`cobirb -p … --headless --output json`), then runs the
checker over whatever the agent left behind. **A run passes only if the checker does**, whatever the
agent said about its own work.

Three rules make the numbers mean something:

- **A frozen commit.** CoBirb runs from a `git worktree` of `--revision` (default `HEAD`), never
  the working tree. A run takes hours, and code changed meanwhile must not leak into it. Every
  result records the commit it measured.
- **A seed, not temperature 0.** Rep *n* uses seed `--seed + n − 1`. Temperature 0 makes small
  models loop, which would measure the setting instead of CoBirb.
- **Every failure has a cause.** `turn_limit`, `no_progress`, `unparsed_tool_call`, `edit_miss`,
  `no_change`, `wrong_result`, `error`, `timeout` or `crash`. "Unparsed tool calls went from 9 to 0"
  is a fact about a fix, even when the pass rate barely moves.

Only the endpoint given with `--base-url` (default the local Ollama) is contacted. Each run gets its
own `COBIRB_HOME` and a config that grants read and write on the fixture and the task's `shell`
commands, and nothing else.

## Running it

```bash
python bench/selftest.py                                # every checker is honest (below)
python bench/cobirb_bench.py --models qwen3-coder:30b,gemma4:latest --reps 3 --label my-change
python bench/cobirb_bench.py --models … --tasks 'hard-*' --skip 'hard-api-*'
python bench/cobirb_bench.py --models … --tasks 'flock-*' --flock --reps 3
python bench/cobirb_bench.py --models … --config '{"flock_planning": "staged"}'
```

| Flag | Meaning |
|---|---|
| `--models` | Comma-separated model names, as the endpoint knows them. |
| `--tasks`, `--skip` | Globs over task ids. |
| `--reps` | Repetitions per model × task. **Use 3 or more for anything you will quote.** |
| `--revision` | The commit to measure. |
| `--max-num-ctx` | The context ceiling (default `32k`). Keep it the same across runs you compare. |
| `--config` | Extra config JSON merged over the run's own — how a setting is A/B-tested. |
| `--flock` | Run each task as a flock session instead of one agent (below). |
| `--label`, `--out` | Name and place the results directory. |

## Results

A run writes `bench/results/<timestamp>-<label>/` (or `--out`):

- `meta.json` — commit, models, reps, window, extra config, flock or not.
- `results.jsonl` — one line per model × task × rep: outcome, pass, turns, calls, refused calls,
  elapsed seconds, the agent's summary and the checker's output. Flock runs add each worker's report
  (ran, check passed, turns, what each refused call was aimed at), the charter's file assignment,
  and — for a failed round only — the skeleton and briefs as planning left them.
- `summary.md` — the pass-rate and failure-cause tables.

**Finished runs are committed**, all three files. They are small (tens of KB), and a number in
AGENTS.md or the CHANGELOG is only worth quoting if the data behind it can be found. The `.log`
files and unfinished runs are not.

## Comparing runs

```bash
python bench/compare.py bench/results/<before> bench/results/<after> [more …]
```

Prints Markdown: pass rates per model with the change against the first run, the task × model cells
that changed, failures by cause, time, and for flock runs the charters, workers and refused calls. It
warns when two runs did not measure the same thing (tasks, models, reps, window, config).

**How to read a difference.** Each change carries a two-sided Fisher exact p-value on the pass
counts. At 3 reps a task moves in steps of 33 points, and a model's total wanders by a pass or two
between identical runs:

- p below 0.05 is a difference worth believing;
- p around 0.1–0.2 is a lead — rerun with more reps before building on it;
- p near 1 is the same result twice.

A consistent **mechanism** can outweigh a weak p: three identical failures with the same wrong
value, or a failure class dropping to zero, say more than the pass count does. Say which kind of
evidence a claim rests on.

## Adding a task

```
bench/tasks/<id>/
  task.toml        the instruction and settings
  repo/            the fixture the agent starts from
  hidden/          pytest tests the checker runs, with the work directory on PYTHONPATH …
  check.py         … or a script instead: exit 0 for a pass
  solution/        a reference answer, overlaid on repo/ by the self-test
```

`task.toml`:

```toml
category = "fix"            # fix, implement, refactor, tests, docs, config, answer, flock
prompt = """What the agent is told, as a user would say it."""
shell = ["python -m pytest"]  # commands the agent may run without asking
changes_files = true        # false for a question: the answer is judged, not the tree
timeout = 900               # seconds for the whole run
```

A `check.py` reads `BENCH_ANSWER` (a file holding the agent's final answer), `BENCH_HIDDEN` and
`BENCH_TASK` from the environment. In `solution/`, an `ANSWER` file is the reference answer and a
`DELETE` file lists paths the reference solution removes.

**Then run `python bench/selftest.py`.** Every checker must fail the untouched fixture and pass the
reference solution — a checker that passes the fixture measures nothing, and one that fails the
solution measures the checker. A task is not finished until the self-test says `ok`.

## Flock mode

`--flock` runs each task through a whole flock session (`bench/flock_driver.py`): Brainy Birb plans,
the charter is approved **automatically** — the only place anything approves a charter, acceptable
only because it happens in a throwaway copy — the Worker Birbs run, and the checker judges the tree
they leave. The round report is Brainy Birb's account and is not evidence; the checker and the
per-worker records are.

## The models page

`docs/manual/models.md` is generated from a run, never written by hand:

```bash
python bench/compat_table.py bench/results/<run> > docs/manual/models.md
```
