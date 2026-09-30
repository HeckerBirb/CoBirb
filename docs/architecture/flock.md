# The Flock

`flock/`, with its front-end in `tui/panes.py`, `tui/flock_bridge.py` and `tui/screens.py`.

One **Brainy Birb** plans the work and designs where the pieces meet. Under staged planning (the
default) it restates the design, and an **Architect Birb** writes the skeleton (interfaces, typed
stubs, semantic docstrings), each ticket's failing tests and each ticket's brief from that
restatement alone. The user approves the **charter** — the single decision point that grants
capability. **Worker Birbs** then run inside charter-derived scopes, their checks are re-run, their
work is reviewed, and the round is evaluated.

**The round report is not evidence — the checker is.** When judging a flock, start from the
benchmark's record of each worker's own report (`worker_reports`, with what each refused call was
aimed at) and the charter's file assignment. On the small flock tasks a single agent still beats
every flock, and the manual says so; on the golden task staged planning beat both the one-prompt
planner and a single agent on both models tried (91/95 each).

## The charter (`flock/charter.py`)

`objective`, `concurrency` (default 2, max 16), `[[seams]]` (`kind` ∈ `formal|loose`), and
`[[workers]]` with `writes`/`reads`/`tests`/`accept`/`brief`/`needs`. Held in the session, never
written to the repo. However a charter arrived — staged planning, the one-prompt planner, a
`propose_charter` outside a flock, or TOML recovered from a reply (`recover_charter`) — the user's
approval inside the run is the only place it is approved.

- **Seams.** A seam is the signature, not the file, and it locks nothing: the stub file belongs to the
  ticket that implements it, others build against the signature concurrently, and the owner keeping
  it is the worker's rules plus review. The only partition conflict is **write/write**, reported
  once per file. (Seams were once writable by no worker and a file another ticket read was refused;
  together they made "one implements `Store`, one builds against it" illegal.) A finished shared file
  simply has no owner.
- **`needs` is the last resort** (it serialises a fan-out). Unknown ids, self-reference and cycles are
  refused at parse. A ticket's tests should pass with its own code and the skeleton alone (a fake, or
  `needs`) — `BRAINY_RULES` says so. `Charter.effective_concurrency` is the widest graph level.
- **The approval dialog** is `stages.CharterApproval` — a `str` carrying the charter, the charters
  approved before it, the `NameMap` and what is not installed — drawn by `render.build_charter` in
  `tui.screens.CharterModal`; every other front-end prints its plain text.

## Staged planning, in rounds (`flock/stages.py`, `flock/tickets.py`, `run._StagedRounds`)

The default (`run.DEFAULT_PLANNING`). `stages` runs the stages and holds the prompts; `tickets` reads
and checks the ticket blocks they write, and runs requirement checks — parsing with no stage in it.
`run._StagedRounds` drives them: prepare (overview, restatement), then per round plan, approve, fan
out and plan the next, then report.

**Stages, each a fresh `Orchestrator`** built on the main one's model, policy, grants, front-end and
checkpoints, with only that stage's tools and a `_GatedHooks` gate that refuses a write outside the
stage's files through `before_tool` — **before** the policy, so it is refused without asking. The
design documents (`stages.Design`) are what a stage carries; nothing else survives between stages.
Every planning prompt carries `stages.machine_block()` — OS, distribution, CPU, WSL — and never a
list of toolchains, so the tools are Brainy Birb's choice. Planning runs with the project's
`verify_command` **off** (`_without_project_verification`): its job is to write failing tests, which
verification would tell it to "fix". A stage whose model call fails is sent once more before the
failure stands.

### 0. Overview (Brainy Birb, read tools only)

Section by section in one context: `SECTIONS`, fixed headings with a checklist each, each asked up
to `SECTION_ATTEMPTS = 3` times with every rejected attempt kept in the trace.

- **Decisions** are put to the user in `ask` autonomy (`Stager._put_decisions`, `Asker.decide`).
  Its checklist also treats a vague word in the request as a decision (one testable meaning, with
  a number), and "Architecture" asks for the rules to sit apart from I/O so tests can reach them.
- **`LIMITS_HEADING` ("Limits on this machine")** says what cannot be built or checked here and what
  the user must do elsewhere; it is quoted in the report (`run._left_elsewhere`).
- **Tickets** are fixed-form blocks (`### ticket: <id>` + `- key: value`, a key may be bold), parsed
  by `parse_tickets`:
  - `writes` lists files; `tests` names the ticket's test files — a line holding the command that
    runs them keeps only the files it names, preferring test-named project files (`_test_paths`);
    with no `tests` line, the test files among `writes` (`_named_as_tests`: whole words of the file
    name in any language's convention, else a `test`/`tests`/`spec` directory);
  - `needs` keeps each entry's leading id (`_need_ids`);
  - `requires` lines (`- requires: <what> — check: <command> — install: <command>`, any number) name
    what must be installed.
- **`check_tickets`** refuses, in overview terms: a command word or remark on a `writes` line; a file
  in two tickets; a ticket without tests or `accept`; an `accept` or a requirement check that names a
  program not on `PATH` or that `policy.command_segments` cannot read (`_accept_problem` — a worker may
  run only what its check names; a missing program's refusal lists which `TOOLCHAINS` that machine
  has); then whatever a scratch `PlanDraft` refuses.
- `NO TICKETS` is a legitimate decline.

### 1. Restatement (Brainy Birb, no tools)

`Stager.clear`: the whole design, request included, restated under `CLEAR_RULES` — the *cleared*
design (`Design.cleared`). Names too: one the user's request states is a requirement, kept
(`- kept:`); one Brainy Birb invented is renamed to say literally what it does
(`- renamed: old -> new`), and the code behind a kept name gets an invented internal name. The
mapping is `stages.NameMap` (`Design.names`), Brainy Birb's and the user's only. `parse_names` cuts
each renamed name at its end — a backticked span, else the text before a space, comma or bracket — and
reads `a -> b, c -> d` as two renames and a comma list after `kept:` as several names, because models
explain renames on the same line. **A name the project already uses is never renamed**
(`keep_existing_names`): an identifier in the project's code (prose files aside) or an existing path
is moved to `kept` and the restated text given the original back — code and tests depend on it, and
renaming `to_roman` left the workers implementing a function nothing imported. A restatement is
refused, and asked again up to `SECTION_ATTEMPTS` times (then `stopped_at="restatement"`), for:

- a missing section, unusable tickets, or tickets that are not the overview's under their mapped ids;
- **a renamed name that survives anywhere in the text** (`NameMap.survivors`, whole-identifier match,
  kept and new names blanked first);
- a section under 75 % of its original's length (restating only adds; one model cut the golden
  request to 18 % and lost the whole API spec);
- a double-quoted value from the request no longer there as a quoted unit (`request_literals`);
- a test file that lost pytest's `test_` prefix.

A ticket refusal shows the blocks expected with the model's own renames applied
(`_expected_blocks`), leaving only `builds`/`done` to restate. `requires` lines the restatement drops
are copied back from the overview's tickets (`_keep_requirements`). The cleared tickets replace the
overview's: the charter, rounds and reports are keyed by cleared ids.

### 2. Skeleton and 3. one stage per ticket (Architect Birb)

`ARCHITECT_INTRO`, `_stage(architect=True)`: given only `Design.cleared_document()` and the machine
facts, **no project context** (the project's instructions and repo map are uncleared); it has the
read tools. Skeleton: any file but the tickets' tests. Ticket stage: only that ticket's tests; **its
reply is the brief as it stands** (`TICKET_PROMPT`: job, files, done when, contract, state and rules,
procedure, example, how to work, out of scope — checked against the tests it just wrote). Architect
Birb is the one agent that sees the whole shape, in cleared form — accepted by the user for it alone.
Test rules (the user's): contracts only, `parametrize` over input → output, K.I.S.S., no design
knowledge, no nudging toward an implementation.

**The contract lives once, in the code.** The skeleton carries it: stub docstrings state what each
function returns (boundaries included), raises and changes, and every exact value is a named
constant. The ticket plan's Contract section only points at those files (`file::symbol`) and quotes
code only where that is clearer, because a second copy can drift from the first — a hand-copied
block once lost its `@dataclass` line. The plan adds what code cannot say: order of work, examples,
edge cases. Architect Birb cannot run anything (read and write tools only), so the tests' imports are
checked by reading. A stub the design outgrows is flagged in the plan as `Stub lacks:`, because the
ticket stage may write only its own tests.

**The harness seals** (`Stager.charter`); no stage has a seal tool. (A seal tool, when offered, was
used in step 1 on every seed traced, so no skeleton step ever ran.) The Flock tab shows each stage's
tool calls (`FlockPane.planning_note`, last `PLANNING_TAIL` lines) under the agent running it
(`Stager._run` → `Asker.speaking` → `FlockPane.planning_agent`); streamed tokens deliberately do not
feed it.

### Before approval: what the tickets need installed

`check_requirements` runs each requirement's check — inside the sandbox, and only where the main
agent's policy has `sandbox_auto`; elsewhere each is "not checked". Only a pass is cached, so a
missing one is looked for again next round. When something is not found, `run._settle_requirements`
puts it to the user with Brainy Birb's `install:` command (`Asker.choose` → `tui.screens.ChoiceModal`,
nothing highlighted; falls back to `confirm`): continue without / installed now (checked again,
asked again) / stop (`stopped_at="requirements"`). CoBirb never runs the install command.

### Rounds

After a round: `supervisor.recheck`, review, and each worker's structured report go to an evaluation
stage (Brainy Birb, on its raw design plus the `NameMap`, since the reports use cleared names), which
returns ticket blocks for only what is open, each with a `- why:`. Its blocks are restated
(`Stager.clear_round`, same checks, the mapping extended) before Architect Birb sees them; a fallback
retry's `why` is the harness's own words, never the unrestated evaluation. A ticket that cannot pass
on this machine is left out with a `- left to do:` line, which goes to the report. The next round
re-runs the skeleton for new files and a stage per ticket, carrying the last plan, the `why` and the
report.

It stops on all green, `flock.max_rounds` (default 5), a round whose failing set equals the last
one's (`no_progress`), or an explicit `NO TICKETS` — an evaluation that cannot be read retries the
tickets still failing rather than ending the flock. **A reported test contradiction always sends its
ticket back** (`run._with_contradicted_tests`), even past `NO TICKETS`, with the tests named so the
ticket's stage rewrites them; the cap and the no-progress stop still bound it.

### Autonomy and auto-pilot

- `flock.autonomy`: the first charter is approved by the user in both modes. `ask` (default) puts the
  Decisions to the user (empty leaves them to Brainy Birb — safe, a design decision grants nothing)
  and asks before every later round, showing only what it adds (`approval_changes`). `auto` decides
  itself and approves later rounds without asking, so **it refuses to start unless the shell sandbox
  is active** (`stopped_at="autonomy"`).
- **`/autopilot` reaches the whole flock, and is read at each decision** (`run._autonomy`,
  `_unless_autopilot`), because `f3` can switch it mid-flock: it forces `auto` autonomy; planning
  stages follow it through the policy they share with the main agent; workers get `refuse` as a
  callable through `run_flock`/`run_worker` into `worker.RefusingIO(when=...)`, plus `AUTOPILOT_NOTE`
  in the brief when it is on at the start. Switching it on answers open `WorkerRequest`s "deny"
  (`FlockPane.refuse_pending`). Only the first charter approval remains — the one question that grants
  capability.
- The benchmark's driver (`bench/flock_driver.py`) approves charters itself; it is the only thing that
  does, and runs only in throwaway copies.

## The one-prompt planner (`flock.planning = "single"`)

- **Built a validated move at a time** (`plan.PlanDraft`; `declare_seam`, `add_worker`,
  `drop_worker`, `seal_charter`): an overlapping partition is unbuildable rather than reported, a
  refusal names one path and costs one move, and order does not matter (`needs` is settled at seal).
  `drop_worker` refuses to drop a ticket others `need`. `propose_charter` stays for small plans.
- **All five tools are moves on one `CharterDesk`** (owner of the draft, the charter and every
  counter), registered and permitted together for the whole session by `install_charter_tool` — the
  planning rules stay in context, so a missing tool is an `Unknown tool` the model cannot argue past.
- **A driven loop with a completion predicate** (`run._plan`, `MAX_PLAN_STEPS = 5`): after each pass,
  no sealed charter → `brainy.next_move_prompt` asks for exactly the missing move. Exits: a charter;
  no tool called and nothing built (a legitimate "do not divide"); `tool.exhausted`, the turn budget,
  or `STOP_NO_PROGRESS`; `MAX_SILENT_STEPS = 2` unanswered nudges.
- **How many run at once is not the model's.** `CharterDesk.accept` sets `DEFAULT_CONCURRENCY` on
  every charter from these tools; `seal_charter` takes no `concurrency`. A model once sealed "4 at a
  time" where the user expected two.
- A list entry of `None`, `null` and the like is no path (`charter.is_path`) — a pane once read
  "reads  None".
- **A refusal ends with the call to make** (`CharterDesk.refuse(retry=...)`), dropped at
  `MAX_REPEATED_REFUSALS = 3`. `tests ⊄ writes` is adopted, not refused, on the move route.
- **`seal` asks once about skeleton files nobody owns** (`CharterDesk.ownership_question`, a project
  snapshot taken before planning, CoBirb's own directory excluded): a file nobody owns stays as the
  skeleton left it. Sealing again unchanged means "finished"; not counted as an attempt. An
  overlapping held charter gets `MAX_OVERLAP_ATTEMPTS = 2` invitations; a failing one stops being
  asked for at `MAX_CHARTER_ATTEMPTS = 5` (template sent with the first rejection only; `_toml_hint`
  names the cause). A charter recovered from a reply is `PlanResult.recovered`.
- The run then checks the partition, asks for the one approval, and may probe the endpoint's real
  concurrency before fanning out (`run._drive_single`, steps 1–5; `_plan_failure` names each way
  planning can end without a charter). Both routes share `run._Flight`, which carries the engagement's
  inputs and does what every route does the same way.

## Workers (`flock/worker.py`, `runtime/wiring.build_subagent`)

- **An ordinary CoBirb agent with its prompt from Brainy Birb.** `build_subagent()` differs from a
  normal run in these ways: its policy is handed in (config's `allow_*` keys do not apply); **no
  project context** (need-to-know); the `worker` model role; its I/O is the caller's (`HeadlessIO` by
  default, its pane in the TUI); verification is its own `accept`; no plugin discovery and no MCP
  servers. Sandbox, per-file checkpoints, redaction and hooks still apply; the sandbox never
  auto-approves a worker's shell.
- **The prompt** is `compose_brief()`: `WORKER_RULES`, then the brief, the working directory, the
  files it may change, the interfaces to read, and its check. The run uses `system=""`.
- `policy_for()`: **writes file-strict, reads open across `cwd`** (read isolation was tried and left
  workers unable to orient), **shell = the programs the worker's own `accept` names, any arguments**
  (`allow_command` covers every segment); a `cd` inside `cwd` is never what refuses a command
  (`Policy._harmless_cd`). Nothing the check never names — a pipe to `head` is a second program, and
  shell grants carry no path scoping. An unreadable `accept` grants nothing.
- A write into a file another worker owns is refused without asking (`writes_owner`): exclusive
  ownership is what makes concurrency safe.
- A worker ends by calling `report` (`worker.ReportTool`, permitted outright): tests pass, contract
  kept, what is missing and why, any test that contradicts the contract. Kept as
  `WorkerReport.structured`; `report_text()` marks a worker that never called it "unstructured".
- **Asking.** Under auto-pilot a worker never asks (`RefusingIO`); the refusal tells it which programs
  it may run, alone (`refusal_note`). Otherwise it may ask for what its
  scope lacks (`WorkerPaneIO.confirm_request`) — **in its own pane, never a modal** (distinct
  positions, nothing focused by default, fail closed with no pane; buttons docked, body scrolls, so a
  tall request cannot push them off screen) — and releases its concurrency slot while it waits.
  Answers: once / session / deny-with-instruction.
- `START_ATTEMPTS = 3`, only when nothing happened yet (no tool calls) and not during a force-stop;
  `START_RETRY_SECONDS = 2`, no backoff (a retry queues behind the work that made the endpoint busy).
  `DEFAULT_MAX_TURNS = 30`.
- Workers wait **before** taking a slot; `record()` stores the report, then sets the event. A
  dependent is skipped only when its dependency did not run.

**Remote Worker Birbs.** A ticket with `runs on` another OS runs on a remote (see
[remote](remote.md)): its re-check and review run there, and a ticket for an OS with no remote is
made static by the user's choice or stops the flock (`stopped_at="remote"`).

## Re-check and review (`flock/supervisor.py`, `flock/review.py`)

- **One round is a `supervisor._Round`**: fan out on a pool sized to the workers, with `Slots` as
  the real concurrency limit and each worker's dependents waiting on its `finished` event; then
  re-check and review.
- **Re-check.** After the join, `supervisor.recheck` runs every finished ticket's `accept` again on
  the final tree (no model) and updates `accepted`, keeping `accepted_when_finished`: a worker's own
  verdict is from the moment it finished, often before a colleague's code landed. A ticket that passed
  only on the final tree is named in the round's account.
- **Review.** Workers run concurrently, join, **then** are reviewed one at a time (review reverts a
  stub temporarily). Two passes, no model, no tokens: (1) read the diff for suspicious changes,
  including a changed declaration; (2) restore the stub and require the acceptance check to **fail**
  (`expect_red`) — worded "PASS — … it is this worker's code that makes them pass", because "stub
  reversion: caught" was read by two of three models as the worker having reverted. It reports "could
  not be checked" when the worker changed nothing, or when `tests` is undeclared, several files are
  owned and every changed file would be restored. Stopping is checked between workers and between
  reviews; a review under way finishes.

## How a run ends (`FlockRun.stopped_at`)

Empty when the flock finished on its own (all green, or `NO TICKETS`). Otherwise:

| Value | Meaning |
|---|---|
| `preflight` | A configured model is missing and the user chose not to go on (`preflight.missing_models`) |
| `error` | The model server failed during planning |
| `planning` | Brainy Birb decided the work should not be divided, or proposed no charter |
| `charter` | The ticket list or charter could not be used; the reason is quoted back |
| `unsealed` | A charter was built but never sealed, after one `seal_reminder_prompt` (one-prompt planner) |
| `turns` | Planning ran out of turns (one-prompt planner) |
| `stalled` | `MAX_SILENT_STEPS` nudges went unanswered (one-prompt planner) |
| `partition` | The partition overlaps and the user chose not to run anyway (one-prompt planner) |
| `restatement` | The design could not be restated (staged) |
| `remote` | Tickets need an OS no Remote Worker Birb offers and the user chose not to go on |
| `autonomy` | `auto` autonomy without an active sandbox (staged) |
| `requirements` | The user stopped over what is not installed (staged) |
| `approval` | A charter or a later round was not approved |
| `no_progress` | A round failed the same tickets the same way as the last |
| `rounds` | `flock.max_rounds` reached |

The report ends with what is left to do elsewhere: the design's limits section, any evaluation's
`left to do` lines, and what was not installed.

## Around it

- `preflight.missing_models()` warns before planning; `probe` measures real concurrency.
- `branch.py` pairs the main session with one flock session per engagement. The design and every
  round's reports go to the flock's encrypted session (`_close_branch`), never the repo;
  `FlockRun.trace` records each planning step's tool calls, for either planner.
- `FlockSettings.from_config` reads the `flock` block (`planning`, `autonomy`, `max_rounds`) and falls
  back per value; `doctor` names a value it could not read.
- A finished flock does not steal the tab or reprint a report the transcript already holds. A charter
  proposed outside a flock run is held and offered when the turn ends (`on_proposed`,
  `offer_pending_charter`, `/charter`).
- Worker panes are fed by a queue the app drains every 0.1 s (`drain_flock_writes`): per-panel
  blocking trips to the UI thread made a flock lag.
