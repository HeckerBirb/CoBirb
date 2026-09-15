# The Flock

Split one job across several agents. A **Brainy Birb** plans and writes the skeleton; **Worker
Birbs** fill it in, in parallel, each locked to its own files.

## Run one

```
/flock add CSV export to the reporting tool
```

Or without the app:

```bash
cobirb flock -p "add CSV export to the reporting tool"
```

## What happens

1. **Brainy Birb plans.** Reads the project, decides the seams, writes interfaces, typed stubs
   and failing tests.
2. **You approve the charter.** One dialog listing each worker and exactly what it may write.
   This is the only decision point — nothing runs until you say yes.
3. **Workers run**, concurrently, unattended, each inside its own scope.
4. **Review**, one worker at a time: read the diff, restore the stub and check the test
   actually fails, probe each stated behaviour.
5. **Brainy Birb reports.**

The **Flock** tab shows one column per worker while it happens.

## The charter

```toml
objective = "add CSV export"
concurrency = 2          # default 2, max 16

[[workers]]
id = "exporter"
writes = ["src/export/csv.py"]
reads  = ["src/reporting/"]
accept = "pytest tests/test_csv_export.py"
brief  = "Implement write_csv() against the stub's docstring."
```

Writes are file-strict. Reads are open across the working directory — a worker that can't
orient can't work.

The charter lives in the session. It is never written into your repository.

## What a worker knows

Its brief, and nothing else. No plan, no project instructions, no repo map, no memory. That
need-to-know boundary is the whole design: a worker that can see everyone else's work starts
making decisions that aren't its own.

Checkpoints, secret redaction and your hooks still apply — those belong to every agent working
in your tree.

## Two models

Give the planner more capacity than the workers:

```json
{
  "models": {
    "default":      { "name": "qwen2.5-coder:14b" },
    "orchestrator": { "name": "qwen2.5-coder:32b" },
    "worker":       { "name": "qwen2.5-coder:7b" }
  }
}
```

CoBirb warns before planning if a role's model is missing, and can measure whether your
endpoint really serves two requests at once.

## Stopping

`ctrl+c` stops the flock. A model call already in flight can't be interrupted, so the current
worker finishes its turn; no further workers start.
