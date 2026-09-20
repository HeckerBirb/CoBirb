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
4. **Review**, one worker at a time: read the diff for weakened assertions, then restore the stub
   and check the tests actually go red. No model is involved, so it can't be argued with.
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

### Seams belong to nobody

A `[[seams]]` entry marked `kind = "formal"` is a declared artifact — an interface, an abstract
base class, a trait — that Brainy Birb wrote into the skeleton and everyone builds against. No
worker may list one in its `writes`, and a charter that does is refused when it's read, naming
the file and the worker.

That's the commonest way a partition falls apart. One worker claims the shared types file, every
other worker reads it, and you get one overlap reported per reader — four of them for five
workers, none really about the readers. The fix was always one line: that worker didn't have to
write the file at all.

A `kind = "loose"` seam is exempt, and so is one naming a symbol (`module.py::load`). A loose
seam is an agreement about behaviour with only a test behind it, and that behaviour is usually
somebody's to implement.

### How Brainy Birb builds it

A piece at a time, with each piece checked as it lands:

```
declare_seam   export/types.py, formal, "ExportSpec and Column"
add_worker     writer  writes export/csv.py, tests/test_csv.py   reads export/types.py
add_worker     cli     writes cli.py, tests/test_cli.py          reads export/types.py
seal_charter   "Add CSV export"
```

Every `add_worker` is checked against the plan so far, and answers immediately:

```
export/csv.py is already written by ticket 'writer', and two owners of one file is what
makes concurrent workers unsafe. Give this ticket a different file, or — if 'writer'
should not have claimed it — call drop_worker('writer') and add it back without that path.
```

So **a charter built this way cannot come out with an overlapping partition.** The refusal arrives
while Brainy Birb is still writing the ticket that caused it, naming one path and what to do about
it, and nothing is partially applied. `drop_worker` backs a ticket out when the file it claimed
turns out to belong to another one.

Tickets can be added in any order — both directions of every check run on every call, so nothing
depends on writers coming before readers. A `needs` may name a ticket that doesn't exist yet; those
and any circular waits are settled at `seal_charter`.

`propose_charter` still takes a whole charter as TOML in one call, which is less work for a small
plan. It's checked after the fact, so it *can* come back overlapping — which is what the next
section is about.

### When the partition overlaps

It's reported, not refused — merging two workers, hoisting the shared file into a seam, or
letting git reconcile them are all reasonable, and which one is right depends on things CoBirb
can't see. You're asked whether to run anyway, one worker at a time.

Overlaps are reported **once per file**, listing every worker involved, rather than once per
pair. And Brainy Birb is invited to correct an overlapping partition at most twice: the charter
is held either way, so a third attempt only delays the dialog you were always going to get.
If it re-proposes the same overlap, it's told so and told to stop.

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

A third case: a plan that was built and never sealed. Brainy Birb added every ticket and stopped
without calling `seal_charter`, so there's no charter to approve even though the plan is finished.
It gets one nudge with the plan quoted back; if it still doesn't seal, you're told how many tickets
were built rather than being told the work doesn't divide.

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

## Declare each ticket's `tests`

`tests` names which of a ticket's `writes` hold its acceptance tests. It looks optional and isn't,
in one specific way: review works by restoring the *implementation* and keeping the worker's tests,
and it can't tell them apart by filename. Undeclared, the test files count as implementation and get
restored too — so the check runs against the bare skeleton and proves nothing.

That case now reports **could not be checked** rather than passing, and Brainy Birb is warned when
it sets `accept` without `tests`. But the fix is to name them:

```toml
writes = ["export/csv.py", "tests/test_csv.py"]
tests  = ["tests/test_csv.py"]
```

A ticket that owns one file, whose acceptance tests live in a file the skeleton owns and nobody
writes, needs no `tests` — nothing of its scope gets restored over.

## `verify_command` and the skeleton

Your `verify_command` is **not** run against Brainy Birb's planning turn. It writes failing tests on
purpose, so your check fails by design at that point, and running it there told Brainy Birb to go and
fix the skeleton it had just built. Each worker's check is its own ticket's `accept`, and the review
passes run as usual.

## A worker runs its own acceptance check

The `accept` command from its ticket is the one thing a worker may run. It implements, runs the
check, reads the failure, fixes, runs it again — converging on green rather than writing blind.

What it gets is the **programs** your `accept` command names, with any arguments — so an `accept` of
`pytest tests/test_csv.py -q` lets it run `pytest` on one file at a time, add `-x`, add `-k`, and
converge. `pytest -q && ruff check src` grants both `pytest` and `ruff`.

What it does **not** get is anything the check never named, and that's deliberate:

- **A pipe is a second program.** `pytest -q | head -50` needs `head`, so it comes to you as a
  request. Shell grants carry no path scoping at all, so admitting `head` or `cat` would let a
  worker read outside its scope entirely — something its file tools cannot do.
- **A ticket with no `accept` gets no shell.** So does one whose `accept` uses a construct CoBirb
  can't read through — `$(…)`, a subshell, `find -exec` — since a grant over something unreadable is
  a grant over whatever it contains.

### Your config does not apply to workers

This surprises people, so: `allow_tools`, `allow_read_dirs` and `allow_write_dirs` in
`~/.cobirb/config.json` are **ignored for Worker Birbs**. `"shell(pytest)"` there grants a worker
nothing. The charter is what you approved, and the charter is the only thing that grants a worker
anything up front.

That's also the whole reason a worker asks about `find`, `pwd` or `ls` — one rule, not a special case
per command. There's no isolation rule about `find` in particular: reads are already open across the
working directory, so it would show a worker nothing `glob` doesn't. A worker reaching for `find`
when it has `glob`, `grep` and `list_dir` is just not using the tools it already has.

It used to be run *for* the worker, once, after its turn — which made the ticket's definition of
done the one thing it couldn't see. A worker wrote an implementation blind, learned once whether
the check passed, got a single fix attempt, and was finished, with its report saying "acceptance
check FAILED" about work it never had a chance to iterate on.

The check still runs once more after the turn ends, so leaving it failing isn't something a worker
can talk its way past.

## When a worker needs something it wasn't given

A charter grants files. Sooner or later a worker needs something else — to run the project's
formatter, to reach a tool nobody mentioned — and being silently refused used to cost the ticket:
it would spend its remaining turns retrying or writing up why it couldn't finish.

So it asks — **inside its own pane**, not in a dialog over the whole screen:

```
╭─ [exporter] held — waiting for you ─────────╮   ╭─ [cli] running ──────────────╮
│ writes export/csv.py                        │   │ writes cli.py                │
│ ╭─────────────────────────────────────────╮ │   │                              │
│ │ wants to use shell                      │ │   │  edit_file cli.py            │
│ │ ruff format export/csv.py               │ │   │  …                           │
│ │                                         │ │   │                              │
│ │ Not in its charter scope. Paused until  │ │   │                              │
│ │ you answer.                             │ │   │                              │
│ │ [ Deny; do this instead…              ] │ │   │                              │
│ │     Once    Session    Deny             │ │   │                              │
│ ╰─────────────────────────────────────────╯ │   │                              │
╰─────────────────────────────────────────────╯   ╰──────────────────────────────╯
```

**Each request lives in the column of the worker that asked**, which is the point. When these were
full-screen dialogs they stacked in one position, so dismissing one dropped the next under a cursor
already committed to clicking — and you'd approve a command you never read. Columns have distinct
positions, so that can't happen.

Nothing is focused by default, deliberately: a default target would put the same race on the
keyboard. Click the button in the pane you mean, or tab to it.

Its own `accept` command isn't one of these — see below.

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
