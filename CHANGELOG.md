# Changelog

All notable changes to CoBirb are recorded here, newest first. See
[`AGENTS.md`](./AGENTS.md) for the architecture and design reasoning behind these changes,
and its §17 decisions record for the ones that were designed and then deliberately *not* built.

## [0.40.1]

- **Your git history cannot be changed from inside the sandbox.** The project's `.git` is read-only
  there, so a command that runs without asking can still `git status`, `diff` and `log`, but cannot
  commit, reset or switch branches — `/undo` restores files, not history, so history is kept out of
  reach. A command that needs to write it is sent unsandboxed, which always asks.
- The sandbox hides CoBirb's own home wherever `COBIRB_HOME` puts it, not only at `~/.cobirb`.

## [0.40.0]

- **A `delete_file` tool.** Removing a file used to need a shell command, and "…and delete the old
  module" was the step models most often reported done without doing. It is a write like the others:
  asked about (or allowed by a write grant for that directory), previewed, and undone by `/undo`.
  Directories are refused.
- Switching session in the app clears the AUTOPILOT and checklist markers, which belonged to the
  session being left.

## [0.39.0]

- **Reasoning models keep their train of thought between tool calls.** A model like gpt-oss thinks
  before each call, and its format expects that reasoning back alongside the call on the next step.
  CoBirb dropped it, so the model lost track of what it had already done — in testing, reading the
  same file over and over until CoBirb stopped it. The reasoning is now kept for the length of the
  session and sent back with the calls it led to.

## [0.38.0]

- **Long sessions keep their thread.** When a conversation outgrows the model's window and the
  oldest turns have to go, the model now writes a short summary of them first — what was asked,
  what mattered, what changed, what is left — instead of leaving only a note that something was
  dropped. It is asked once each time more has to be dropped, not on every message, and if it
  fails the plain note is used as before. `/context` says when turns were summarised.

## [0.37.0]

- **Plan mode looks before it plans.** The planning step used to be a single reply with no tools, so
  the model planned changes to code it had not been allowed to read. It is now a short read-only
  pass: the model can read, search and outline the project, but cannot change anything — a call
  that tries to is refused — and then writes its plan. The separate validation phase after the work
  is gone; checking your work is part of doing it.
- **A checklist for long tasks.** The new `todo` tool lets the model keep a list of steps and tick
  them off; its progress shows on the status bar (`checklist 2/5`). It changes nothing on disk, so it
  never asks for permission.

## [0.36.0]

- **Auto-pilot.** `/autopilot on` (or `--autopilot` with `-p`) lets the agent work unattended: it
  reads and changes files in your project and runs commands in the sandbox without asking, and
  anything else — writing outside the project, a command outside the sandbox, an MCP or plugin
  tool — is refused instead of asked about, so a long job never stalls on a dialog nobody is
  watching. It will not start unless the sandbox and git-backed undo are both active, since those
  are what make it safe. The status bar says AUTOPILOT while it is on.
- **Contained commands no longer ask by default.** With the sandbox active and git installed, a
  shell command that cannot reach the network or anything outside your project runs without a
  prompt, and `/undo` can take back whatever it changed. Set `"sandbox": "ask"` to be asked anyway.
  Without git, it still asks.
- **One question to read your project.** Answering "Always" to a read inside the project now covers
  the whole project for the session, instead of only that file's directory. Writes still ask per
  directory. A project that is your home directory or `/` keeps the narrow scope.

## [0.35.0]

- **`/undo` and `/diff` cover what shell commands did.** Every turn is now snapshotted as a whole
  tree, so a file a command deleted, created or rewrote comes back like any other. Before, undo only
  knew about files changed through the file tools. It works whether or not your project is a git
  repository, and if it is, your repository is never touched: the snapshots live in a separate,
  private store (respecting your `.gitignore`) that is deleted when the session ends.
- **Undo never takes back your own edits.** It restores only files the last turn changed and that
  are still as that turn left them; anything you have changed since is left alone and listed.
- Needs `git` installed; without it, CoBirb keeps the per-file snapshots it had.

## [0.34.0]

- **Shell commands run in a sandbox.** With bubblewrap installed, every command the agent runs is
  contained: no network at all, the filesystem read-only except your project and a private `/tmp`,
  its own process space, and credential directories — `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`,
  `~/.docker`, CoBirb's own `~/.cobirb` and others — hidden. An approved `npm test` used to be able
  to read your SSH keys and open a socket; now it cannot. `cobirb doctor` says whether it is active.
- **`"sandbox": "auto"` runs contained commands without asking**, since nothing inside can reach past
  the project; the default, `"ask"`, keeps asking. A command that genuinely needs the network is sent
  with `unsandboxed` and is always put to you first. `"off"` restores the old behaviour.
- Worker Birbs' commands are contained too; what they may run is still exactly what the charter grants.

## [0.33.0]

- **One key names your default model: `models.default.name`.** The older top-level `model` and
  `default_model` still work when it is unset, but no longer outrank it — "which setting is in
  force" had three answers. `cobirb doctor` now calls them deprecated and says where to move them;
  it used to call a config using them broken, including the bundled example, which now uses the
  current key.

## [0.32.0]

- **`apply_patch` understands the `*** Begin Patch` format** that gpt-oss and other OpenAI-trained
  models write, and diffs whose `@@` lines carry no line numbers. Both used to fail as "no valid
  hunks", so the edit never landed. Hunks are placed by their surrounding lines; one that matches in
  two places is refused rather than guessed. A patch names its own file, and that is the file the
  permission check looks at; one that touches several files, or moves or deletes one, is refused —
  send one file per call.

## [0.31.0]

- **`cobirb setup`** gets you from install to a working model without editing JSON: it asks where
  your model server is and which protocol it speaks, lists the models that server actually has,
  and saves your pick — keeping everything else in your config, and refusing to touch one that
  doesn't parse. It contacts only the address you give it.
- **The interactive app remembers a model you pick** when none is configured, if you say so,
  instead of asking again every session. When no server answers and no model is set, it points you
  at `cobirb setup`, and so does `cobirb doctor`.

## [0.30.0]

- **llama.cpp, LM Studio and vLLM work.** Set `"api": "openai"` on a model role and CoBirb talks
  `/v1/chat/completions` to it — streaming, tool calls, images and cancellation included. These
  servers could list their models in CoBirb before, and then failed every single message, because
  chat always went to Ollama's own API. They fix their context window at start-up, so CoBirb reads
  it from what the server reports instead of asking for one; `max_num_ctx` still caps it.
- Connection errors name the server you are actually using instead of always asking whether
  Ollama is running.

## [0.29.0]

- **`--system-prompt harness` now tells the model how to work,** not only how permissions work:
  look before changing anything, change files with the right tool, check your work, keep going
  until the task is done instead of describing the next step, and never claim success you have not
  checked. It is still off by default and still goes after your model's own `SYSTEM`; whether it
  becomes the default is being decided by measurement.

## [0.28.0]

- **A write that breaks a file says so straight away.** When `write_file`, `edit_file` or
  `apply_patch` leaves a Python, JSON or TOML file that no longer parses, the result tells the model
  where, instead of it finding out turns later from a failing test — or never, if nothing loads
  the file.

## [0.27.1]

- **The tools describe themselves properly.** Several descriptions were one line that said what a
  tool is, not when to use it or what it gives back — and a small local model leans on exactly
  that. Each now says when to reach for it and which neighbouring tool fits better (`edit_file`
  for part of a file, `write_file` for the whole of one; `grep` inside files, `glob` for names;
  the file tools rather than `shell` for looking around).

## [0.27.0]

- **Fixed: `edit_file` could change the wrong place and say it had succeeded.** When the text to
  replace appeared more than once, it edited the first occurrence — in a file of similar functions,
  a different function from the one meant — and the model then reported the job done. An ambiguous
  edit is now refused with the line of every match, so the model can add context; `replace_all`
  changes every occurrence on purpose.
- **`edit_file` forgives small copying slips.** Trailing whitespace, Windows line endings and a
  missing level of indentation no longer make an edit fail, as long as the match is unique. When
  the text really isn't there, the error quotes the closest lines so the model can copy them exactly.
  A successful edit shows the lines it produced.

## [0.26.0]

- **Tool calls a model writes as text are now made.** Many local models put the call in their reply
  instead of the structured field — Qwen3-coder switches to its XML format once it has more than a
  handful of tools, others write `<tool_call>` JSON or a bare JSON object — and CoBirb took that
  for the model's final answer, so the task ended on a call that never ran. These are now read and
  run through exactly the same permission check as any other call. Only tools CoBirb actually
  offered count, and plain JSON only when it is the whole reply, so a JSON example in an explanation
  is never executed.
- **A call that can't be read is sent back instead of ending the task** — a misspelled tool, broken
  JSON, or the model server's own tool-call parser failing (which Ollama can return as if it were
  the answer). The model is told what went wrong and gets to send it again.

## [0.25.0]

- **A task is no longer cut off after 8 turns.** That cap ended an ordinary task — read a few files,
  edit, run the tests, fix — before it was half done, and it was the commonest way a real task
  ended. A run now stops when it stops making progress: the same call with the same arguments made
  twice in a row gets a note telling the model so, four in a row ends the run, and so do six failed
  or refused calls in a row. `max_turns` (default 40) remains as a backstop; the reason a run stopped
  is reported, and in `--output json` it is `"stop_reason": "no_progress"` or `"turn_limit"`.
- Worker Birbs get up to 30 turns (was 12) now that they run their own acceptance check and fix what
  it reports; the same no-progress brakes apply to them.
- **Fixed:** `cobirb doctor` reported `connect_timeout` and `request_timeout` as settings CoBirb does
  not read, calling a correct config broken, since they were added in 0.21.0.

## [0.24.0]

- **Sampling options per model role.** `models.<role>.options` is sent to the server with every
  request — `temperature`, `top_p`, `seed` and the like — and a role's options are merged over the
  default's key by key. `num_ctx` is ignored there, since `max_num_ctx` already owns the window.

## [0.23.1]

- **CoBirb reports its real version.** `cobirb.__version__` was a literal nobody bumped: it said
  0.13.1 for eight releases, and the MCP client announces it to every server it starts. It is now
  read from the installed package.
- Documentation that described a different program is corrected: the version in AGENTS.md, a line
  claiming vision support did not exist, the worker approval dialog 0.21.0 replaced, and the
  README's description of which API CoBirb speaks.
- The test suite now fails any unit test that reaches a model endpoint or the network, instead of
  letting it pass slowly against a running Ollama or hang against a stopped one.

## [0.23.0]

- **Running out of turns is reported as what it is.** CoBirb used to make up a reply for the model —
  "Stopped after 8 turns without a final answer." — and store it as the answer. It was shown as
  though the model had said it, sat in the session as the conclusion, and a headless run exited 0 on
  it, so a pipeline read an unfinished task as a finished one. Now nothing is invented: CoBirb says
  it stopped, as a note of its own, without a "Completed." underneath, and `--output json` carries
  `"stop_reason"` (`"answered"` or `"turn_limit"`) and exits 1 when the run did not finish.

## [0.22.0]

- **Personas are gone.** `--persona`, `/persona` and its picker, the `persona` config key, the
  bundled `professional`, `neighbor` and `kawaii` files and `~/.cobirb/personas/` are all removed.
  They had nothing to do with what CoBirb is for, and in testing they made models behave worse — a
  voice layered over the instructions that make an agent work is a voice competing with them. Replies
  are simply labelled "CoBirb" (Brainy Birb and the Worker Birbs keep their own names), and the
  status bar now starts with the model.
- **Nothing you already have breaks.** A config that still sets `"persona"` starts normally;
  `cobirb doctor` says the key was removed and can be deleted, rather than calling it a typo.
  Sessions saved with a persona open as before, and the field is dropped on the next save. The
  plugin interface's `Persona` class stays, unused, so a plugin that imports it still loads.
  `--persona` on the command line is now an error, since a flag that silently does nothing is worse.
- Personas are listed in the new [`ROADMAP.md`](./ROADMAP.md) as an idea that may return one day
  as an optional plugin.

## [0.21.1]

- **Planning runs until the plan is finished.** A model's turn ends when it stops calling tools,
  which is the right rule for a conversation and the wrong one for a phase with an objective
  completion test: is there a sealed charter? A Brainy Birb that worked out its next move and
  stopped to *say* it — "I will proceed by correcting the first worker's ticket" — ended the phase
  on that sentence without making the move, and the run was reported as "did not propose a charter",
  the opposite of what it had concluded. Planning is now a loop: after each pass, if there is no
  charter, CoBirb reads the draft and asks for exactly the move that is missing — declare the seams,
  add the tickets, seal, or correct the rejection it quotes back. Bounded by attempts and the turn
  budget, and a model that called no tool at all and built nothing still gets the answer it always
  had, since "do not fan this out" is a legitimate one.
- **A refusal ends with the call to make.** "That file is already written by ticket 'a'" now closes
  with *call `add_worker` again* — a diagnosis on its own invites a weak model to narrate the
  correction instead of sending it, and the narration ends the phase. Dropped once the same refusal
  has repeated, where "send it again" is the one thing proven not to work.
- **A test file missing from `writes` is adopted rather than refused.** A worker's acceptance tests
  are its own files, so a path under `tests` is a path that ticket writes; refusing it asked for a
  whole ticket to be re-sent to move one string between two lists. The ownership check still runs
  over the adopted paths, so a ticket cannot claim a neighbour's file by calling it a test.
- **A stalled plan is reported as a stall.** Two nudges running answered with prose and no tool call
  stops planning and says so, rather than replacing a halt with a loop.
- **A finished flock no longer prints its report twice or steals the tab.** The report is frequently
  the reply the transcript already holds — for a stopped planning phase it *is* the narration, for a
  finished round it is the verdict — and it was written again on the way out. The prompt box is also
  focused only when you are already on the Current tab, instead of yanking you off the Flock tab at
  the moment it has the most worth looking at.
- **Brainy Birb is asked to cut the partition fine enough to hold the need-to-know boundary up** —
  as many Worker Birbs as the work supports, each ticket isolated well enough that its holder cannot
  reconstruct the feature from it. A worker count named in your objective overrides it.

## [0.21.0]

- **Two model timeouts instead of one, and both configurable.** `connect_timeout` (10s) is how long
  to wait for your endpoint to accept a connection — short, because either something is listening or
  it isn't, and it's the only question a timeout can answer with "Is Ollama running?".
  `request_timeout` (600s) is how long to wait for it to *say* something once connected. One 120s
  number used to do both jobs and was wrong for both: a socket timeout measures **silence, not work**,
  and a request your endpoint has queued behind another generation sends nothing at all until it
  starts producing tokens. A flock is exactly that shape, so Worker Birbs died at 120 seconds having
  never sent a prompt — and were told the server might not be running, while it was busy answering
  their colleague.
- **A provider fault no longer costs a whole ticket.** Any exception from the model turned into a
  dead worker; a transient timeout lost work the model had never even started. A worker that couldn't
  start is now started again, up to three times. Only when nothing happened yet: one that had already
  edited files isn't restarted, since re-sending its brief against a tree that has moved under it is
  worse than the half-finished ticket it reports instead. Not retried during a force-stop, and
  deliberately not a backoff ladder — a retry against a busy endpoint queues *behind* what made it
  busy, so spacing attempts further apart buys queue depth rather than patience.
- **A worker is told its working directory**, so it stops spending an approval dialog on `pwd`. The
  cwd line the orchestrator adds is only attached when there's a system prompt or project context,
  and a worker has neither by design.
- **A worker is told to use its file tools rather than the shell.** It has `list_dir`, `glob`, `grep`,
  `read_file` and `repo_map`, and was reaching past them for `find` — which costs an approval dialog
  to be told to use what it already had.
- **A Worker Birb's request is now asked in that worker's own pane.** It used to be a full-screen
  dialog, and several workers asking at once stacked them in the same position — so dismissing one
  dropped the next under a cursor already committed to clicking, and you could approve a command you
  never read. Columns have distinct positions, so the race has nowhere to happen. Nothing is focused
  by default either, or the same problem reappears on the keyboard: click the button in the pane you
  mean. With no panes on screen a request is denied rather than falling back to a dialog.
- **Worker panes are twice as wide** (90 columns, was 44). At 44 a worker's tool calls, shortcoming
  report and review all wrapped to shreds, and a pane now also holds requests that need reading
  rather than skimming.
- **A worker may run its acceptance check the way it actually needs to.** The grant was the exact
  invocation from the charter, which meant a worker iterating — one test file at a time, `-x`, `-k` —
  missed it on every variation and asked you about a command you had already approved. It now grants
  the *programs* the check names, with any arguments, plus every program a chained check runs. A pipe
  is still a second program, so `pytest -q | head -50` still asks: shell grants carry no path scoping
  at all, so admitting `head` or `cat` would let a worker read outside its scope entirely.
- **Documented, because it surprises people:** your `allow_tools`, `allow_read_dirs` and
  `allow_write_dirs` do **not** apply to Worker Birbs and never have. The charter you approve is the
  only thing that grants one anything up front, which is the single reason a worker asks about
  `find`, `pwd` or `ls` — there is no isolation rule about `find` in particular.
- **The flock review's third pass — mutation testing — has been removed.** It mutated each behaviour
  a docstring claimed and required the tests to catch every one, which made it a review of the
  *contract* as much as of the work. It was also wired to nothing: the code existed, the reviewer
  accepted the mutants as an argument, and nothing ever passed any — so it ran zero times in every
  flock while the docs described it as part of how review works. Writing the mutants needs a model
  round-trip per stated behaviour, and review otherwise costs no tokens at all, so it is gone rather
  than connected. Review is now two passes, as it always was in practice, and the hole this one
  covered is closed from the other side: pass 2 reports "could not be checked" where it cannot
  judge. `cobirb help flock` and the manual no longer claim otherwise.

## [0.20.2]

Four defects found by a static read of flock mode. The first two had been there for many releases
and are the reason a complex flock did not survive its own planning phase.

- **Fixed: Brainy Birb was told to fix the failing tests it had just deliberately written.** Its job
  is to write a skeleton of failing tests for the workers to make pass, so the planning turn ends
  with your project's check failing *by design* — and `verify_command` then ran, failed, and handed
  Brainy Birb "VERIFICATION FAILED. Fix the cause." with four turns and its file tools. The obedient
  answer is to implement its own stubs or weaken its own tests, destroying the skeleton the workers
  were about to build against. Planning now runs with that check off, as each worker's verification
  has always been scoped to its own ticket. It also stops burning a second full test run per flock,
  and stops masking the "ran out of planning turns" report.
- **Fixed: the strongest review check silently passed whenever a ticket declared no `tests`.** It
  restores the implementation and keeps the worker's tests, then requires the acceptance check to
  fail — but with `tests` undeclared, every file the worker owns counts as implementation, so the
  restore put the whole scope back to the skeleton and the check failed for the skeleton's own
  reasons. It reported "caught" no matter what the worker did. Two cases are now reported as **could
  not be checked** rather than as a pass: a worker that changed nothing, and one whose whole scope
  would be restored. A ticket owning a single file whose tests live elsewhere is unaffected. Brainy
  Birb is also warned at charter time when it sets `accept` and forgets `tests`.
- **Fixed: Ctrl+C didn't stop the review passes.** A review rewrites the worker's files, runs the
  acceptance command with its own timeout, and puts them back — so a stopped round went on touching
  your tree and spawning test runs for minutes after you said stop, and quitting during that window
  could leave a file mid-revert. Stopping is now checked between reviews as it is between workers. A
  review already under way still finishes, because putting the files back is part of it.
- **Fixed: a plan holding only seams was nudged to seal itself.** `seal_charter` refuses a plan with
  no tickets, so the nudge spent a whole planning turn budget reaching a refusal and then reported a
  rejected charter for a model that never proposed one.

## [0.20.1]

Both of these are defects in 0.20.0, found by testing the paths its own tests did not cover.

- **Fixed: a multi-segment `accept` command denied the worker its own check.** `accept = "pytest -q
  && ruff check src"` is an ordinary definition of done, and the grant covered only the first
  segment while the check requires every one — so the worker was refused the command the charter
  had approved, escalated to you for it, and did so again on every attempt. Every segment is now
  granted. A command using substitution or a subshell still grants nothing, since a grant over
  something unreadable is a grant over whatever it contains; the post-turn verification runs the
  check either way, so no ticket is lost to this.
- **Fixed: a plan built but never sealed was reported as "this work does not divide".** A model
  that added every ticket and stopped without calling `seal_charter` produced no charter and no
  recorded attempt, which read as Brainy Birb declining to fan the work out — while a finished plan
  sat in the session, unrun. It now gets one nudge with the plan quoted back, and if it still does
  not seal, you are told plainly that N tickets were built and never sealed. Deciding not to divide
  the work is still reported as the legitimate answer it is.
- The Flock tab's planning strip names which ticket or seam each planning call was about, instead of
  four identical `add_worker` lines.

## [0.20.0]

- **Brainy Birb builds a charter a piece at a time, and an overlapping partition is now
  impossible to build.** `declare_seam`, `add_worker`, `drop_worker` and `seal_charter` replace
  "write the whole document and hope": each call is checked against the plan so far and answers
  immediately, so a file another ticket already writes — or one that is a formal seam — is refused
  at the move that causes it, naming the path, the ticket that has it, and what to do. Previously a
  charter had to be right about eight things at once, was refused all together when it was wrong
  about one, and every stage of the round was gated on that one artifact. Tickets can be added in
  any order; `needs` and circular waits are settled when the charter is sealed. `drop_worker` backs
  a ticket out, so one wrong move no longer means abandoning the plan.
- **`propose_charter` is still there** for a plan small enough to say in one document, and it is
  still where a charter written into a reply is read from. Brainy Birb is steered to the pieces for
  anything larger.
- **A Worker Birb runs its own acceptance check.** It could not run anything at all, and its check
  was run *for* it after its turn — so the ticket's definition of done was the one thing it could
  not observe: it wrote an implementation blind, learned once whether the check passed, got one fix
  attempt, and was finished, with its report saying "acceptance check FAILED" about work it never
  had a chance to iterate on. It now implements, runs the check, reads the failure and fixes, until
  it passes. That command is the only one it gets: the grant is a prefix rule over the exact
  invocation you approved in the charter, applied to every segment of anything it runs, so a chained
  command with something else in it is refused whole. A ticket with no `accept` still gets no shell.
  The check is also still run once after the turn, so a worker cannot leave it failing.

## [0.19.1]

- **A charter whose partition overlaps no longer loops.** The tool answered a valid but
  overlapping charter with "Charter accepted… propose a corrected charter" — two states at once,
  and an instruction to act on the second. Nothing counted the attempts (the existing brake only
  ever caught charters that would not *parse*), and the text was identical every time, which for a
  model is the strongest possible signal to send the same thing again. A run then spent its whole
  planning budget re-proposing and reported at the end that no charter had been proposed at all.
  The charter is now **held**, a better partition is invited twice, and a resubmission that
  overlaps in the same places is told so and told to stop.
- **A worker can no longer be given a formal seam to write.** That was the cause of the loop above:
  one worker claims the shared interface file it never had to write, every other worker reads it,
  and you get one overlap reported per reader. It is refused when the charter is read, naming the
  file and the worker. Loose seams and seams naming a symbol (`module.py::load`) are unaffected —
  those are agreements about behaviour somebody has to implement.
- **Overlaps are reported once per file, not once per pair.** Five workers around one shared types
  file reported four identical overlaps; the count is the first thing you read, and "4 overlaps in
  the partition" describes a partition in ruins rather than one path in one `writes` list.
- **A charter proposed after planning ran out of turns says so.** The approval prompt looked the
  same whether Brainy Birb finished or was cut off mid-skeleton, and you are about to authorise
  workers to build against whatever is on disk.

## [0.19.0]

- **A charter can say that one worker waits for another.** `needs = ["exporter"]` on a
  `[[workers]]` entry holds it back until that worker has finished. It is deliberately the last
  resort and Brainy Birb is told so: independent tickets all start at once, which is the entire
  reason for fanning out, and every `needs` takes one away. It exists for the one shape
  independence cannot express — a seam that must be *built* before anything can be built against
  it, where hoisting it into the skeleton was not possible. Previously that needed two rounds and
  a second trip through you.
- **Reading a file a worker you depend on writes is no longer reported as an overlap.** It stopped
  changing when that worker finished, which is the condition the check existed to catch. Reading
  one you do *not* depend on still is. Two workers writing the same file is still a conflict
  whatever the ordering — "which worker broke this" has to keep having an answer.
- **The charter says what concurrency you will really get.** A chain runs one at a time however
  large `concurrency` is, so the approval now reads `4 at a time — but 1 in practice, because some
  wait for others` rather than letting you approve a number the round was never going to reach.
- **A worker whose dependency never ran is skipped and says which one.** A dependency whose
  acceptance check merely failed still lets the next one run: that is common and usually unrelated
  to what the dependent needs, and one flaky check should not kill a whole subtree. Unknown ids,
  a worker needing itself, and circular waits are refused when the charter is read, with the cycle
  named.

## [0.18.0]

- **A Worker Birb can ask for something its charter scope did not give it.** A worker that needed
  to run a command or reach a tool nobody granted it was silently refused, and then spent its
  remaining turns retrying or writing up why it could not finish — the permission question
  answered correctly and the ticket lost anyway. It now asks, and you answer: once, for the whole
  CoBirb session, or no — optionally with a line saying what to do instead, which reaches the
  worker as part of the refusal rather than leaving it with nothing but a retry.
- **Asking costs the asker, not the round.** A worker waiting on you gives up its concurrency slot,
  so the rest of the flock keeps running at full speed and the next worker starts immediately. Its
  column shows `held — waiting for you`. Previously the limit was the thread pool's size, which
  conflated how many workers exist with how many may be working — so two open dialogs at
  `concurrency = 2` would have stalled everything.
- **A write into a file another worker owns is refused outright, never asked about.** Exclusive
  file ownership is what makes workers safe to run at the same time, so it is not something to
  grant away at a dialog — and the person answering should not have to hold the whole partition in
  their head to spot it. CoBirb has the charter and checks it.
- **New: "allow for the session".** Wider than "always", which only widens the policy it was asked
  about — a Worker Birb's policy is built per ticket and thrown away with it, so "always" in a
  flock was re-asked on the next ticket and the next round. A session grant reaches every agent in
  the session, including workers that have not started. In memory only; never written to config.
- **`max_num_ctx` can be written the way people say it.** `"64k"` (or `"64K"`) means 65536 — a `k`
  is 1024, because what anyone means by "64k" here is the window, and windows are powers of two.
  The plain number still works. A value that isn't a size costs the cap rather than the run, and
  `cobirb doctor` reports it instead of leaving you uncapped in silence.

## [0.17.0]

- **New: `max_num_ctx`, a ceiling on the context window CoBirb asks for.** CoBirb states `num_ctx`
  on every request rather than letting Ollama serve its own 4096, and when a model's Modelfile
  names no window of its own it asks for the architecture's advertised maximum — 262144 for a
  Qwen2-family model. Sized as KV cache that can exceed a card's VRAM on its own, well before the
  weights and whatever else shares the card are counted, and the server quietly offloads the
  remainder to CPU. Set `"max_num_ctx": 32768` and a model asking for more is clamped to it, while
  one asking for less is left alone. It also caps what history is packed against, so CoBirb stops
  filling a window the server was never asked for.
- **Fixed: `context_tokens` claimed to do that and never did.** Its help text said it was "only
  needed to cap a window your VRAM would rather not hold". It is the history budget and never
  reached the `num_ctx` sent to the server. The text now says what it is and points at
  `max_num_ctx`.

## [0.16.1]

- **Fixed: approving a charter asked twice.** `/charter`, and a charter proposed mid-conversation,
  put up an approval dialog and then handed the charter to the flock — which asks the identical
  question, word for word, at its own stage 3. One decision, two dialogs, the second repeating the
  first. Only the flock's own prompt asks now: it is the design's single decision point, and a
  second dialog saying the same thing teaches people to dismiss both without reading either.
- **Fixed: `/charter` offered to re-run a flock that had already finished.** `propose_charter`
  keeps the last charter it accepted so a dismissed dialog stays recoverable, but nothing cleared
  it once a round had actually run — so `/charter` afterwards silently offered to do the whole
  thing again.

## [0.16.0]

- **The Flock tab shows Brainy Birb's working-out while it plans.** `/flock` moves you to that
  tab, but its worker panes only exist once a charter does — the end of the longest phase of the
  run — so until then it sat blank while everything happened on the tab you had just left. It now
  carries the last eight things Brainy Birb did, and `[ Waiting for LLM... ]` while it is blocked
  on a reply. The section disappears as soon as a charter is proposed and the panes take over.

## [0.15.5]

- **Fixed: a charter Brainy Birb wrote into its reply left the run dead.** CoBirb reads tool calls
  only from Ollama's native `tool_calls` field, so a model that writes the charter into its
  *answer* instead — fenced, or as plain TOML — produces no tool call at all, and the orchestrator
  reads the reply as a final answer. The flock then reported that no charter was proposed while
  the charter sat in the transcript in front of you. It is now read from there when the tool was
  not called, and the run says that is what happened. This is the "I see the proposed charter and
  nothing happens" case, and why nudging sometimes worked: native tool-calling is not reliable
  per-call on local models, and gets less so as the context fills with a skeleton.
- **Brainy Birb is told the mechanism, not just the instruction.** `BRAINY_RULES` said to call
  `propose_charter`; it never said that writing the charter into a reply *does nothing*. It does
  now, and it is told that every file it writes costs a planning turn.
- **Running out of planning turns is its own outcome.** A skeleton with more files in it than the
  budget allows used to end with `Stopped after 30 turns without a final answer.` reported
  verbatim — a synthetic string that reads exactly like a considered answer. It now says the turns
  ran out, that the skeleton is still there, and what to do next.
- **The Flock tab shows progress while Brainy Birb is planning.** `/flock` moves you to that tab,
  whose panes are only built once a charter exists — the end of the longest phase — so until then
  you watched one unchanging line while all the work rendered into the tab you had just left.
- **Fixed: streamed tool calls split across chunks lost all but the last.** Dormant with Ollama,
  which sends them together, but wrong.

## [0.15.4]

- **Fixed: a charter Brainy Birb could not get right became a loop.** Every rejection came back
  with the same twenty-five-line template, which is the strongest available hint to a model that
  the thing to send next is what it just sent. The template is now shown once; after that the
  rejection names the error and says not to resend. After five attempts the tool stops asking
  altogether and tells Brainy Birb to explain what it is stuck on instead, and planning no longer
  spends a second turn budget retrying a model that has already exhausted them. **0.15.3's retry
  made this worse before it made it better** — it doubled the turns available for failing.
- **A TOML error now names a likely cause.** `Invalid value (at line 1, column 13)` is the
  identical message for a curly quote and for an unquoted string, which tells a model correcting
  its own output nothing it can act on. Both are now identified by name, with the offending line
  quoted.

## [0.15.3]

- **Fixed: a rejected charter ended a flock as though nothing had been wrong.** When validation
  refused a charter, `charter` came back as `None` — which is also what Brainy Birb deciding the
  work should not be divided looks like. So the run reported the model's own account of it, which
  in one case was *"The charter has been finalized and submitted successfully"*, and returned
  before ever reaching the approval dialog. A charter that was attempted and rejected is now its
  own outcome, reported with the number of attempts and the reason the last one failed. Brainy
  Birb also gets one more try, with the rejection quoted back, because a model told its charter is
  invalid will otherwise often end the turn by announcing success.
- **Fixed: a charter validation message stopped mid-sentence.** `worker 'w3' lists … under tests
  but does not write them — a worker's acceptance tests are its own files, so that they can be
  added to` — and that was the whole message. It now says what to do about it. This was the error
  the model was given in the run above, which is some of why it could not correct itself.
- **`propose_charter` is available for the whole session.** It used to be registered for the
  planning turn and taken away afterwards, while the planning transcript — which tells Brainy Birb
  to deliver a charter by calling it — stayed in context for the rest of the session. So "redo the
  plan" produced a call to a tool that was no longer there, an `Unknown tool` result, and a model
  reasonably concluding something had broken. It stays registered and permitted now.
- **A charter proposed outside a flock run now reaches you.** Brainy Birb can propose one at any
  point, not only while planning; the approval dialog appears as soon as the current turn finishes.
  Approving it runs the flock from that charter without planning again.
- **`/charter`** — review the last proposed charter and run it if you approve, for one that was
  dismissed or proposed while another flock was still running. `/flock` with no objective does the
  same thing.

## [0.15.2]

- **`scripts/release.sh` cuts a release.** Bump level in, version bump and CHANGELOG heading out,
  then a commit and a tag; `--push` to ship it, `--test` to run the suite first, neither by
  default. Nothing changes for anyone using CoBirb — this is a maintainer script, and it exists
  because the sequence had been run by hand five times and had grown a different set of
  unnecessary checks around it each time.

## [0.15.1]

- **Fixed: a flock round that went badly took the review down with it.** Brainy Birb's review runs
  on the same session as its planning, so `BRAINY_RULES` — which says to deliver a charter by
  calling `propose_charter` — was still in its context, as was its own successful call from
  earlier. The review prompt then asked it for "an amended charter", while `_plan` deregisters that
  tool the moment planning ends. So a round with anything to fix produced the obedient thing: a
  call to a tool that was no longer there, an `Unknown tool` result no amount of re-reading could
  argue with, and the rest of the review turns spent failing to recover. The review now asks for
  the second round in prose and says plainly that the tool is gone. Re-registering it would have
  been worse: a round ends here by design, so nothing reads a second charter and the call would
  have succeeded and been discarded in silence.

## [0.15.0]

- **A demo GIF replaces the static screenshot in the README.** `docs/images/cobirb-demo.gif`
  shows the interactive app actually being used rather than one frame of it.
- **Typing `/` lists the commands.** Five rows under the prompt box, the same shape the `@` file
  picker already has: `↑`/`↓` to move, `tab` or `enter` to pick, `escape` to dismiss, and keep
  typing to narrow. Each row carries the command's own one-line description, and a `5 of 15`
  counter says how many more matched than fit.
  - **Your own commands are in the list**, tagged `user` or `project`. They were previously
    invisible unless you already knew their names or ran `/commands`.
  - A slash only opens it as the **first word** of a message, so "remind me to /clear later" stays
    prose and a path like `/usr/bin` closes it again at the second slash. That matches what
    dispatch has always done — a command mid-sentence has never run.
  - Descriptions come from the handlers' own docstrings rather than a list kept beside them; seven
    commands that had no summary line, or led with their signature, now read properly in `/help`
    too.

## [0.14.2]

- **Fixed: `~` in a path given to a tool was treated as a directory called `~`.** "Write it to
  `~/git/c2/x.py`" produced `<cwd>/~/git/c2/x.py` and reported success. `CobirbTool._resolve` now
  expands it — and so does `Policy._resolve`, in the same change and for the same reason: the
  policy exists to say where a call will land *before* it is approved, so the two resolving
  differently would mean approving one path and writing another. A `~` path is now gated as the
  file in your home directory it will really become, which also means approving your working
  directory does not quietly carry it.
- **Fixed: a `cd` in `shell` looked like it worked.** Each call is its own process, so `cd
  somewhere` moved a shell that exited immediately after, and the next call started where the last
  one did — reported as a bare `exit=0`, indistinguishable from having stuck, with the mistake
  only surfacing when something later ran in the wrong place. A command that is *only* a `cd` now
  says so in its result, and `shell` takes an optional `cwd` argument for running one call
  somewhere else without a `cd` at all.
- **`/clear`.** Starts the conversation over from here: clears the screen and the model's context
  together. **Nothing is deleted** — it records a marker turn, a point in the history the way
  committing an emptied file is a new commit rather than a rewrite of the ones before it. The
  earlier turns stay in the session file, so it remains a complete record for auditing, and
  `/export` still writes all of it out. Reopening a cleared session picks up from the marker
  rather than replaying a conversation you had already put behind you.

## [0.14.1]

- **Fixed: `install.sh` could not work out the latest release.** It read the tag off GitHub's
  `/releases/latest` HTML redirect, which turns out to depend on a "latest" flag that is not set
  as reliably as it looks — a repository's only published, non-draft, non-prerelease release can
  still redirect to the releases *index*, and `releases` is not a version. The one-liner failed
  with `'releases' is not a release version`. It now asks the API, which answers directly, and
  keeps the redirect as a fallback for a caller that has spent its unauthenticated 60 requests an
  hour. Naming a release explicitly (`--version v0.14.0`) was unaffected throughout.

## [0.14.0]

- **A one-line install.** `curl -fsSL .../install.sh | bash` builds a virtualenv in
  `~/.local/share/cobirb`, installs a checksum-verified release into it, and puts `cobirb` on
  your `PATH` at `~/.local/bin/cobirb`. No `sudo`, no `pipx`, no venv to activate, and nothing
  written outside your home directory. Python 3.11+ is the only prerequisite. Previously the only
  way in was a git clone plus knowing which of `pip install -e .` or `pipx` you wanted.
  - The script is wrapped in a single function it calls on its last line, so a download that dies
    halfway through executes nothing rather than the first half of an installer.
  - The wheel is verified against the release's `SHA256SUMS` before anything is installed. A
    mismatch aborts rather than retries.
  - `--uninstall` removes the virtualenv and the symlink and **leaves `~/.cobirb/` alone** —
    config, sessions and memory outlive any install.
- **`cobirb --upgrade` now knows what kind of install it is in.** Managed (put there by
  `install.sh`), a git checkout, or neither. A managed upgrade re-runs the installer that put it
  there with a different `--version`, so **upgrading and downgrading are one operation**:
  `cobirb --upgrade v0.13.0 --force` goes back the same way `cobirb --upgrade` goes forward. The
  script stays the only implementation of "move to version X", because it is also what a
  first-time user runs. An install that is neither shape is told so, and told what would work,
  instead of failing with "could not find a git checkout".
- **Releases carry artifacts.** A `vX.Y.Z` tag now builds a wheel and an sdist and attaches them,
  `SHA256SUMS` and `install.sh` to the GitHub release. The tag is checked against the packaged
  version first, so a release can't ship a wheel nobody can ask for by name.
- **`cobirb doctor` reports the install shape**, and no longer warns that a perfectly good
  managed install is "not an editable clone". It deliberately does not check whether a newer
  release exists on a managed install: that means asking GitHub, and `doctor` talks to the
  endpoint you configured and nothing else.

## [0.13.1]

- **Fixed: `cobirb doctor` reported a working model as missing when the tag was omitted.** Ollama
  treats `gemma4` and `gemma4:latest` as the same model and serves either, but `list_models` only
  ever reports the qualified form — so comparing them as raw strings told people to pull a model
  they already had. An absent tag now means `:latest` on both sides of the comparison. A
  *different* tag (`thing:70b` against `thing:9b`) is still a failure, and a genuinely absent
  model still is too.

## [0.13.0] — At hand

- **`@path` mentions.** Type `@` in the prompt box and a five-row picker appears; ↑/↓ to move,
  tab or enter to pick, escape to dismiss. The named file is sent with your message, so putting a
  file in front of the model no longer costs a `read_file` call, an approval prompt and a second
  round trip.
  - **Fuzzy matching** — subsequence, the fzf/"Goto Anything" behaviour: letters must appear in
    order but needn't be adjacent, so `gba` finds `global.py`, `general_batch.py` and `gba.py`.
    Ranking is what makes it usable: an exact name wins outright, then a prefix, then letters
    landing on word boundaries (`general_batch` above `global` for `gb`), then the rest.
  - Expanded on the way to the model, never into the transcript — the transcript shows
    `@src/main.py` as typed. Mentioned files are capped like any other read and go through the
    same secret redaction; the candidate list honours the ignore rules `glob` and `grep` use.
  - With the picker open, enter chooses rather than sends. Sending a half-typed mention is never
    what was meant.
- **`cobirb --continue`.** Reopens the session you were last in, and implies `--password` —
  continuing a session means unlocking one, so asking for both flags would be asking twice. An
  explicit `-w <password>` is still honoured, so it can be scripted.
- **`cobirb doctor`** (also `cobirb --doctor`). One command for "am I ready to go", replacing five
  failures that each surfaced somewhere different.
  - **Config**: parses, every key is one CoBirb actually reads, values are the right type, and
    what it references exists. The key check is the one that earns its keep — `Config.get` is a
    plain lookup, so `redact_secret` for `redact_secrets` currently reads as redaction *off* while
    it stays *on*, in total silence.
  - **Environment**: endpoint reachable, every configured role's model actually pulled, and
    whether it can see images. These are the checks that otherwise fail mid-turn.
  - **Install**: version against the latest release, whether `--upgrade` can work here, and
    whether the checkout is on a branch — a detached `HEAD` swallows the next commit made in it.
  - Exits non-zero only on a real failure. A warning is worth knowing, not a broken install.

## [0.12.7]

- **Status badges in the README.** The tests badge is live from the Actions workflow; the Python
  row matches `requires-python` and the CI matrix, the licence matches `LICENSE`, and the two
  privacy badges restate invariants the codebase holds. No version badge until a release badge
  would be telling the truth.

## [0.12.6]

- **Fixed: the upgrade tests failed in CI.** They created throwaway git repositories and then
  named the `main` branch by hand, which only works on a machine whose git config sets
  `init.defaultBranch`. `git init` otherwise produces `master`, so the tests passed locally and
  failed on the runner. Each test repository now names its initial branch explicitly, so the
  suite no longer depends on whoever is running it.

## [0.12.5]

- **Fixed: `cobirb --upgrade` left the checkout on a detached `HEAD`.** It checked the tag out
  directly, which is harmless for someone only running CoBirb and a trap for anyone who also
  commits to it — the next commit belongs to no branch, so `git push` silently has nothing to
  send and the work is easy to lose. The current branch is now fast-forwarded onto the tagged
  commit instead, leaving you where you were.
  - Detaching remains the fallback where there is nothing else honest to do: already detached,
    or a branch carrying commits the tag doesn't have. A branch with its own work is never moved.
  - The result says which happened. `Upgraded v0.7.0 → v0.8.0 (v0.8.0). Still on main.` or, when
    detached, what to run to get back on a branch.
  - A clone that has never fetched upgrades fine: `upgrade()` fetches before it resolves
    anything, and fetching a tag brings the commit it points to, so the fast-forward has a local
    ref to move onto. Pinned by a test, since narrowing or reordering that fetch would break it
    silently — the branch would simply stay behind and the checkout detach.
- `AGENTS.md` §15 now states that a behaviour change is not finished until the matching page in
  `docs/` is updated in the same change.

## [0.12.4]

- **Fixed: closing an unresponsive MCP server blocked for as long as that server lived.**
  `StdioClient.close()` closed the pipes before ending the process, and a reader thread sitting
  in `for line in process.stdout` holds that stream's lock — so the close waited for a read that
  was never going to return. Terminating first makes the blocked read hit EOF immediately.
  A 30-second shutdown is now 2.
- **Docs.** `docs/` gains eleven short how-to pages — install, first run, commands, CLI, config,
  permissions, sessions, memory, images, the Flock, plugins & MCP — linked from the README. The
  screenshot moved to `docs/images/`.
- **Tests run in half the time** (207s to 98s, ~5/s to ~10/s), with no coverage removed:
  - Session keys derive at scrypt's interactive cost across the suite. `test_crypto.py` restores
    the shipped cost, since there the KDF is the subject. The cipher, blob format and round trip
    are still exercised for real everywhere.
  - The probe's give-up path is asserted in 1.2s instead of 60 — the fake endpoint stalls for
    longer than the probe waits, which is the whole condition, and needs no more than that.
    `probe_concurrency(timeout=...)` is typed `float` now, since it is a duration.
- **Restored seven `/image` argument-parsing tests** that were removed by accident along with a
  debug test in v0.12.1. The behaviour shipped correctly; its tests did not.
- Comments and docstrings across `src/` and `tests/` now describe what the code does rather than
  what it once did.

## [0.12.3]

`tui/app.py` was 1648 lines and the home of everything the interactive app could do. It is now
1225, and three of the things it was doing have somewhere of their own to live.

- **`tui/slash_commands.py`** — what each `/command` does. Fourteen `_cmd_*` methods made the app
  the home of exporting markdown, toggling plan mode and starting a flock, on top of being the
  Textual application. They were already plain functions of `(app, argument)` — the dispatch table
  held *unbound* methods and called them `handler(self, argument)` — so this changed how they are
  stored, not how they work. The app keeps `_dispatch_command`, which is a routing decision about
  input rather than the behaviour of any one command.
- **`tui/transcript.py`** — `TranscriptView`: everything written to the transcript, and the
  ordering rule they all depend on (flush the streamed reply before writing anything else, or a
  tool-call panel lands above the reasoning that led to it). Takes the persona name and a message's
  attachments as arguments rather than reading them off the app, so it changes when the transcript
  changes and not when the application's fields do.
- **`tui/attachments.py`** — `PendingAttachments`: the images `/image` has queued for the next
  message, with the reading, format sniffing and payload shaping that were spread across two app
  methods. `AttachmentError` carries a sentence, because every failure it can have is one a person
  should read.
- **Fixed: the flock's activity roll-up could name a worker that wasn't there.** `WorkerPane`
  rendered its state without remembering it, so the app kept a parallel `_flock_states` dict to
  build the roll-up from — the same fact in two places, and only one of them noticed when a pane
  was gone. The pane that shows a state now has it, `FlockPane.activity_summary()` reads off the
  panes, and the dict is deleted.

## [0.12.2]

Housekeeping pass: two real bugs, and the catalogue bookkeeping moved out of the app.

- **Fixed: the always-present public catalogue was never created.** `memory.ensure_public_exists`
  had no callers outside the tests, so a fresh install opened `/memories` on an empty list and
  `/remember` offered nowhere to put the fact. Both commands now create it on first use, which is
  what "lazily, on first use" claimed all along.
- **Fixed: a catalogue name was used as a filename unchecked.** `../../escaped` wrote a catalogue
  outside `~/.cobirb/memories` entirely, and a name containing a separator wrote into a
  subdirectory `discover_catalogues` would never list again. A name must now be a name.
- **`CatalogueStore` (`runtime/catalogues.py`), extracted from `CoBirbApp`.** Ten methods of
  catalogue bookkeeping were living among tabs, modals and worker threads without touching a single
  widget. They have one reason to change and it is not "the screen changed", so they now have their
  own home; the app keeps thin forwarders for the modals, and `tui/app.py` loses 145 lines.
- **`MemoryCataloguesModal` and `RememberModal` share a base class.** Reading the catalogues,
  rendering them loaded-first, showing an inline error and the unlock-then-continue dance were the
  same code twice.
- `policy._read_target` is now `_target_path` — it has always served the write tools too, so the
  name claimed a narrower job than it does, in the one module where being exact matters most.
- Removed seven genuinely dead imports; the two that survive are marked as the re-exports they are.

## [0.12.1]

- **`/image <path> <message>` now works.** Typing the path and the question on one line is what
  people reach for, and reading the whole line as a filename failed with
  `No such file or directory: 'docs/cobirb.png What is this image?'` — which blames the file for a
  parsing rule. The first word is the path and the rest is the message, sent with the image in one
  go. A quoted path wins outright, and an unquoted path that *does* name an existing file is still
  taken whole, so paths containing spaces keep working.
- **`/image` resolves a relative path against the session's working directory**, not the directory
  CoBirb happened to be launched from — `--cwd` exists precisely so those can differ.
- **Switching model with `/model` no longer keeps the previous model's context window.**
  `_context_budget` asks the provider once and remembers, which is true within a run and wrong
  across a session: `/model` swaps the provider under it. Switching away from a small-window model
  left every later turn compacting against the old budget — enough on its own to elide an attached
  image, since one is priced at 1500 tokens against a stale 2048-token budget.

## [0.12.0]

- **An attached image now survives a resume.** Attach one, quit, reopen the session, and the model
  can still see it — which is what v0.11.0 was supposed to do and didn't. Its images lived as
  separately-encrypted files in a `<session>.images/` sibling directory, and that could never
  work: neither `SessionManager` nor `Orchestrator` retains a password, so the layer that builds
  the model's context had no way to decrypt them. A resumed session showed a `[image: x.png]`
  marker where the picture had been, and nothing ever read those files back at all.
  - The bytes now live in `Session.images` (`{id: base64}`, deduplicated by content hash) **inside
    the session's own encrypted blob**. Same AES-256-GCM, same password, one mechanism instead of
    two — and already decrypted by the time `load()` returns. `crypto.py`'s `derive_key`/
    `encrypt_bytes`/`decrypt_bytes`, `SessionManager.attach_image`/`read_image`/`images_dir` and
    `Session.image_key_salt` are all deleted; the SPI was never touched, so no plugin breaks.
  - Every image-bearing turn is resolved against that table, not just the newest, so an image stays
    visible for as long as its turn does. Old ones fall back to a `[image: filename]` marker in
    compaction pass 1; recent ones are never elided. Compaction prices an image at a flat 1500
    tokens rather than its base64 length (a 1 MB screenshot would otherwise look like 350k tokens).
  - `fork_session` carries exactly the images the branch's kept turns reference.
- **Fixed: `/image` crashed the turn in the default (no `--session`) configuration.** From the
  second turn onward it raised `'NoneType' object has no attribute 'encrypt'` — the orchestrator
  manufactures its own `SessionManager` after turn one, and that one has no crypto backend. The
  error was swallowed by the TUI's catch-all and the user's message never reached the model.
- **Fixed: `/image` created a stray `..images/` directory in the working tree** in that same
  configuration, writing into the user's repository.
- **Fixed: a multi-line `/remember` silently lost every line after the first.** The prompt box is a
  multi-line editor; continuation lines are now indented in the `.md` and read back correctly.
- **Fixed: memory catalogues existed world-writable (`0666`) for a window** between creation and
  `chmod`. Created with `os.open(..., 0o600)` now, matching `session._write_blob`.

## [0.11.0]

- **Vision: attach an image with `/image <path>`.** No caption argument — nobody types a
  description of their own screenshot; the message typed and sent next is the caption, when there
  is one, the same as attaching a file anywhere else.
  - Persisted reference only (`{id, filename}`); the encrypted bytes live in a sibling
    `<session>.images/` directory, keyed by a session-password-derived key that's derived once and
    reused (`crypto.py`'s new `derive_key`/`encrypt_bytes`/`decrypt_bytes` — duck-typed extras, not
    added to the frozen `SessionCrypto` ABC, so a third-party crypto plugin without them still
    works, just slower).
  - **Only the newest turn's image is ever sent as bytes.** Every earlier image-bearing turn is
    rewritten to a plain `[image: filename]` marker the moment a later turn exists — nothing is
    ever resent, and `context.py`'s compaction needs no changes to know images exist.
  - `supports_vision()` is real now (was a hardcoded `False`): reads `capabilities` off the same
    cached `/api/show` payload the context-window lookup already uses. Most local models can't see
    images at all, so CoBirb asks rather than assuming.
  - `/export` shows a `📎 filename` marker, never the bytes — an export is plaintext by design, and
    an attached image riding along unasked in it isn't the same explicit act.

## [0.10.0]

- **Memory catalogues.** Named lists of facts fed into the system prompt, each its own file under
  `~/.cobirb/memories/`: `<name>.md` (plaintext) or `<name>.md.enc` (password-protected, reusing
  the same AES-256-GCM + scrypt backend sessions already use). A `public.md` catalogue always
  exists; anything else is created explicitly.
  - `/memories` — load, unload, rename, delete, or create a catalogue, from a TUI dialog. Loaded
    catalogues are listed first, separated from the rest.
  - `/remember <fact>` — save a fact into a catalogue you pick on the spot, prompting for a
    password there if the chosen one is locked. **Deliberately a plain slash command, not a model
    tool**: a tool would either be unreachable for a model without tool-calling support, or get
    called on every turn with no memory of already having asked. This fires exactly once, exactly
    when typed, regardless of what the model can do.
  - Loaded catalogues are composed into the system prompt fresh on every turn (never baked into
    the orchestrator at construction time, which is what made the old header panel go stale) and
    are never sent to a Worker Birb — same "nothing but its brief" boundary the Flock already
    enforces for `AGENTS.md` and the repo map.

## [0.9.2]

- **`--cwd` (and its default) now resolves to a real, absolute path** instead of passing the
  literal string `.` around when it wasn't given — which used to show up verbatim in the header
  panel and status bar (`CoBirb · model · .`). Resolved once in `cli.main()` rather than at every
  call site.
- **Removed the interactive app's header panel.** It named the model configured at startup, not
  necessarily the one actually running — if the configured model wasn't available and the startup
  picker changed it, the panel kept naming the original for the rest of the session, because the
  transcript it lived in is append-only and can't be corrected in place. A same-day fix attempt
  (writing a second, corrected panel) produced a visibly duplicated banner instead, which is worse
  than the original bug. Removed rather than patched further: persona/model/plan/cwd/session are
  already all on the live status line, which self-corrects because it isn't an append-only log.

## [0.9.1]

- Removed every named mention of other AI coding tools from docstrings, `AGENTS.md`, `README.md`
  and package metadata (`pyproject.toml`), rephrased to keep the same meaning. Prompted by a local
  model echoing one of those names back unprompted — CoBirb's own description of itself is
  something a model can read (project instructions, package metadata) and repeat, so it
  shouldn't name a product it isn't.
- `cobirb.__version__` (used in the MCP client handshake) had drifted out of sync with
  `pyproject.toml`'s version since v0.8.0; corrected and kept in this release.

## [0.9.0] — "Noah", the default theme

The parrot's own colours, replacing the stock teal/orange/navy scheme app-wide: one accent
(tail red — was two colours in two places), a canvas, structural chrome, and two tiers of text.
Defined once in `plugins.core.render` so the plain one-shot CLI and the full-screen app render
identically, same as everything else that module builds. An error now gets bold text on a
tinted background strip rather than plain coloured text, so it doesn't read as the same accent
used for the active-tab underline everywhere else.

## [0.8.0] — Installable

A stranger can get a working `cobirb` binary onto their `PATH`, and keep it current.

- **Global install via [pipx](https://pipx.pypa.io).** `pipx install --editable .` puts a
  `cobirb` binary on `PATH` with its three dependencies isolated in their own environment — no
  venv to activate. `--editable` keeps it pointed at the git clone, so a source change still
  needs nothing further; see README "Installing" for the plain-venv alternative.
- **`cobirb --upgrade [tag] [--force]`.** Fetches tags, checks out the latest release by default
  or a named one, and reinstalls to refresh metadata. Refuses to move to an older release than
  the one currently running unless `--force` says so, and refuses outright on a checkout with
  uncommitted changes rather than guessing what to do with them. The one CoBirb command that
  talks to a network by default — because it's what was just typed, the same justification
  `cobirb plugin install` already relies on.

## [0.7.0] — Hardened

The last release before the plugin and session-file formats were frozen.

- **Plugin SPI frozen and versioned.** A plugin declares `COBIRB_SPI = 1` (absent means 1, so
  nothing existing breaks); changes within a version are additive-only. A plugin written against
  a newer CoBirb is refused at the loader boundary with a message naming which side to upgrade,
  and one bad plugin no longer stops the others from loading.
- **Session schema migrations.** `migrate()` runs inside `from_dict`, on every read path. A
  session file written by a *newer* CoBirb is declined rather than read optimistically and saved
  back with the wrong content.
- **Security review** (see AGENTS.md §12.3): the encrypted session header now carries its own KDF
  parameters, which let the scrypt cost move from N=2¹⁴ to 2¹⁷ (OWASP's current recommendation)
  without orphaning files already on disk — old sessions still open, at the old cost, from a
  headerless blob. Corrupt session files used to report as "Incorrect padding" or fail inside the
  cipher, both of which read as *wrong password*; they're now distinguished. The plugin installer
  put its directory at the front of `sys.path`, ahead of the standard library; it's now appended.
- **Cold-start README pass.**

## [0.6.0] — Interactive

A running session stops being a one-shot commitment.

- **Plugin distribution.** `cobirb plugin install/list/remove` turns a plugin's source directory
  on disk into something the loader discovers, via a real `pip install -e` round trip. Local
  only — no registry, no fetching.
- **Mid-turn steering.** A message sent while a turn is running redirects it instead of queuing
  behind it, cutting the model off mid-stream where it supports that.
- **Session branching.** `session.fork_session()` forks a saved conversation into a new,
  independent file; the source is only ever read. `--branch` / `--branch-at`, or one click from
  the Sessions tab.

## [0.5.1] — Field notes

Everything the first real Flock run turned up.

- Worker reads opened to the whole project (writes stayed exactly as strict) — reads scoped to
  exact brief files had left workers unable to orient.
- Force-stop for a worker stuck on the model — streaming moved onto a tracked `http.client`
  connection so a hung request can actually be interrupted.
- A 404 no longer misreported as "is Ollama running?" — `HTTPError` is a subclass of `URLError`,
  and the two were being conflated.
- Pre-flight, tag-insensitive model check, to catch the `:latest` trap before a Flock plans
  around a model that will 404.
- Charter dialog colour, the dialog showing the charter it's asking about, multi-line prompt
  input, a scrolling Flock tab, selectable pane text, a starter config on first run, an activity
  line.

## [0.5.0] — The Flock

Multi-agent subtasking. `cobirb flock -p "..."` divides a piece of work between several agents
that cannot see each other.

- **Charter.** A TOML document *Brainy Birb* proposes and you approve: objective, seams,
  per-worker read/write scope, and an acceptance check per worker.
- **Skeleton.** Brainy Birb writes the interfaces and failing tests into the tree before anyone
  fans out — ordinary tool use, no code generation in core.
- **Fan-out.** *Worker Birbs* run under a charter-derived policy, two at a time by default, each
  knowing only its own part of the work.
- **Three-pass review.** Baseline diff, stub reversion, behaviour mutation, cheapest first.
- **Flock tab.** A pane per worker, live status, force-stop.
- **Session pairing.** A GUID pairs the main session with its own flock session file.

See the "The Flock" design record for the full decision history.

## [0.4.0] — Extensible

- **MCP client (stdio).** Tools from a local MCP server become CoBirb tools under the same
  permission layer as everything else — no outbound socket, since stdio servers are local
  subprocesses.
- **Hooks.** Four lifecycle points; a `before_tool` hook can refuse a call before you're even
  asked about it.
- **Custom commands.** Markdown files under a project become slash commands.
- **Per-role model selection.** `models.default/orchestrator/worker`, each inheriting from
  `default` field by field.
- **Documented the shell privilege gap** rather than building a sandbox — an approved shell
  command still runs with your full privileges.
- An embedded GGUF runtime was designed for this release and then deliberately cut; see
  AGENTS.md §12.1.

## [0.3.0] — Grounded

- **`repo_map` tool** — a ranked outline of the codebase (Python symbols via `ast`, regexes
  elsewhere), so the agent can answer "where is X" without grepping blind.
- **`/diff`** — everything the agent changed this session, built on the undo snapshots rather
  than git, so it works in a directory that was never a repository. (Auto-commit deferred; see
  AGENTS.md §12.1.)
- **Self-verification loop** — `verify_command` runs after a turn that changed files; a failure
  goes back to the model for one bounded attempt.
- **Write-scope grants**, mirroring the existing read grant.
- **Secret redaction** — high-confidence formats only (private keys, `AKIA…`, `ghp_…`, `sk-…`),
  nothing matching on a variable name.
- **Session export** — `--export` / `/export` to markdown, on explicit request only.

## [0.2.0] — Trustworthy

- **Context compaction** against the model's real context window (via `/api/show`) — the fix
  for the single most damaging failure mode an agentic tool has: a session that quietly gets
  dumber the longer it runs. Old tool results are elided first, then the oldest turns, then the
  working set is trimmed; a short session takes an unchanged fast path. `/context` shows the
  budget.
- **Project instructions** — `AGENTS.md` (and `CoBirb.md`) read from the working directory into
  the system prompt.
- **`.gitignore` awareness** in `glob` / `grep`.
- **Diff preview before write** — the approval dialog shows the actual diff before it applies,
  not after.
- **Checkpoint & `/undo`** — touched files are snapshotted before each turn.
- **Headless mode** — `--headless`, a policy file, `--output json`, real exit codes.
- **Malformed tool-call repair** — a bad call gets one corrective round trip instead of burning
  a turn.

## [0.1.0] — Initial

Core agent loop, built-in tools, the permission model, a local Ollama provider, and encrypted
sessions (AES-256-GCM, keyed via scrypt).
