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
   Nothing runs until you say yes, and it is the only place capability is granted up front — a
   worker can still ask for something later (see below), which pauses only itself.
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

### When one worker has to go first

Usually none of them do — that's what the skeleton is for. Brainy Birb builds the seam the
workers meet at, so their tickets are independent and all start at once.

For the case it can't hoist into the skeleton, a worker can name what it waits for:

```toml
[[workers]]
id     = "consumer"
writes = ["src/report.py"]
reads  = ["src/export/csv.py"]
needs  = ["exporter"]     # runs only once `exporter` has finished
```

`needs` also settles the partition check: reading a file a worker you depend on writes isn't an
overlap, because it has stopped changing by the time you start. Reading one you *don't* depend
on still is.

Two things it costs, both shown before you approve:

- **Parallelism.** A chain runs one at a time whatever `concurrency` says, so the charter tells
  you what you'll actually get: `3 Worker Birb(s), 4 at a time — but 1 in practice, because some
  wait for others`.
- **The dependents, if it fails.** A worker whose dependency never ran is skipped and says so,
  rather than building against a seam that isn't there. A dependency whose *acceptance check*
  failed still lets the next one run — that's common and usually unrelated.

An unknown id, a worker needing itself, or a circle are refused when the charter is read, with
the cycle named.

### While it plans

`/flock` moves you to the Flock tab, and the worker panes only exist once there is a charter — so
until then the tab shows what Brainy Birb is doing: the last eight things it did, and
`[ Waiting for LLM... ]` while it is blocked on a reply. It disappears the moment a charter is
proposed and the panes take over.

Building a skeleton for a large partition takes a while, and this is how you tell a run that is
working from one that has stopped.

### When one doesn't take

If a charter fails validation, Brainy Birb is told exactly what was wrong and asked to correct
it. If it still doesn't produce a usable one, the run stops and **says so** — naming how many
attempts there were and why the last was rejected. Nothing started, nothing changed beyond
whatever skeleton got written.

That is a different outcome from Brainy Birb deciding the work shouldn't be divided at all,
which is a legitimate answer and is reported as one.

### Asking for it again

Brainy Birb can propose a charter at any point, not only while planning — so "redo the plan, the
partition didn't hold" works after a round has finished. A charter it proposes puts itself in
front of you for approval as soon as the current turn ends.

```
/charter        # review the last proposed charter and run it if you approve
/flock          # with no objective, the same thing
```

Useful if you dismissed the dialog, or if a charter was proposed while another flock was still
running.

## What a worker knows

Its brief, and nothing else. No plan, no project instructions, no repo map, no memory. That
need-to-know boundary is the whole design: a worker that can see everyone else's work starts
making decisions that aren't its own.

Checkpoints, secret redaction and your hooks still apply — those belong to every agent working
in your tree.

## When a worker needs something it wasn't given

A charter grants files. Sooner or later a worker needs something else — to run the project's
formatter, to reach a tool nobody mentioned — and being silently refused used to cost the ticket:
it would spend its remaining turns retrying or writing up why it couldn't finish.

So it asks, and you get three answers:

```
[exporter] wants to use shell
pytest tests/test_csv_export.py -q

  Once (y)     Session (s)     Deny (n)
  [ Disallow; do this instead…                    ]
```

- **Once** — this call only.
- **Session** — every agent for the rest of this CoBirb session: this conversation and every
  Worker Birb, including ones that haven't started and later rounds. It lives in memory and is
  never written to your config.
- **Deny** — optionally with a line saying what to do instead, which reaches the worker as part
  of the refusal. That's usually the answer worth giving: "no" alone leaves it with nothing but
  a retry.

**Asking costs the asker, not the round.** The worker pauses and gives up its concurrency slot,
so the rest of the flock keeps going at full speed and the next one starts immediately. Its
column shows `held — waiting for you`.

**One thing is never asked.** A write into a file another worker owns is refused outright, and
the worker is told why. Exclusive ownership of files is what makes workers safe to run at the
same time; granting it away mid-round would leave two agents editing one file, and "which worker
broke this" would stop having an answer.

Without a screen — `cobirb flock -p "…"` — there's nobody to ask, so everything outside the
charter is refused, exactly as before.

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
