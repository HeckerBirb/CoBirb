# AGENTS.md — CoBirb

> **Single source of truth for this repo.** Absorbed the former `DESIGN.md` and `PLUGIN_SPEC.md`.
> If code and this file disagree, that's a bug in one of them — say which. Keep it current.

---

## 1. What CoBirb is

A **privacy-first, Copilot-like agentic CLI**: Copilot CLI behavior, minus every default
network/telemetry behavior, plus a hard boundary around the rest. **No data leaves the process
unless the user opts a capability in.** Noah the African Grey is the mascot and an *opt-in* persona,
not the default voice. Status: **v0.5.1.** Loop, tools, permissions, encrypted sessions, Ollama provider and
the interactive app, plus context compaction, project instructions, `.gitignore` awareness,
diff-before-write, `/undo`, `/diff`, `/export`, a headless CI mode, the `repo_map` tool,
secret redaction, an opt-in verification loop, per-role models, hooks, custom commands and an MCP
client over stdio, and the Flock (§4m).

**CoBirb is a client, not a model runtime.** It speaks to an OpenAI-compatible endpoint and does
not run weights — see §12.1.

## 2. Ironclad constraints (never violate)

1. **Zero telemetry.** No analytics, crash reports, or pings.
2. **No outbound network by default, and no shipped remote provider — ever.** Only the model
   layer may touch the network, only when the user configures a provider, and CoBirb will never
   package or bless one. Decided September 2026: local models only, forever. The SPI still lets a
   third party write a remote provider; that is their choice to make and ours to not make for
   them.
3. **Never echo the password.** `-w` with no value reads stdin without echo; never logged, stored,
   or retained past the call that needs it.
4. **Sessions encrypted at rest.** Plaintext never hits disk. No PQ-KEM — §7.
5. **Default-deny permissions.** Nothing is pre-approved; every capability comes from the user.
   The one breadth granted on a single "yes" is a *read* directory (§8).
6. **Everything local.** No cloud sessions, remote control, or background agents.

Feature ideas conflicting with these are out of scope. Park them.

## 2b. The hardware this targets

**Assume at least 16 GB of VRAM and that a 128k context window is fine.** CoBirb's users have
capable machines or rent high-end GPUs; it is explicitly not built for low- or mid-end consumer
hardware. Running two models at once, or several subagents concurrently, is a reasonable thing to
expect of it.

This is written down because getting it wrong is expensive and has happened. A context-compaction
feature was built around a 4,096-token window — Ollama's own default, mistaken for what a local
model gets — and that single assumption then sized the project-instructions budget, the repo map,
and the decision not to inject a map at all. All four were wrong, and none of them looked wrong
from inside.

The standing rule: **performance, memory and model-size figures are observations, never
arguments.** Noting that something is slow or large today is useful; letting it quietly pick a
default, narrow a feature or rule an approach out is not. Raise it with the user as a decision.

## 3. Working here

```bash
pip install -e ".[dev]"                                  # Python 3.11+, .venv/
cobirb                                                   # interactive (Textual app)
cobirb -p "task" --allow-tool=list_dir                   # one-shot, pipeable
cobirb help [session|persona|plan|model|plugins|tools|config]
pytest                                                   # keep green; see §11 on coverage
COBIRB_TEST_MODEL=llama3.1 pytest -m integration         # needs a real local Ollama
```

CI runs `pytest` on 3.11 and 3.12. No linter/formatter/type-checker configured — match surrounding
style by hand.

| Path | Responsibility |
|---|---|
| `cli.py` | Argparse, help text, one-shot mode. Nothing else. |
| `runtime/` | The application layer both front-ends compose a run out of: `personas`, `commands`, `plugins`, `wiring`, `sessions`. |
| `help_text.py` | The prose `cobirb help` prints. |
| `orchestrator.py` | The agent loop; model ↔ tools, policy-gated. |
| `policy.py` | Permissions, shell-command scanning, audit log. |
| `session.py` | `Turn`/`Session`, encrypted `SessionManager`, session discovery. |
| `config.py` | The config reader. One file, `~/.cobirb/config.json`; no repo layer (§4l). |
| `typing/spi.py` | **The plugin contract.** All SPI interfaces and shared dataclasses. |
| `checkpoints.py` | Pre-edit snapshots behind `/undo`. |
| `context.py` | Fitting the history into the model's window. |
| `paths.py` | Every path under `~/.cobirb`. Derived in one place, on purpose. |
| `plugins/loader.py` | Discovery (entry points + local dirs), fail-closed. |
| `plugins/core/` | Built-ins: `tools`, `model`, `io`, `crypto`, `persona`, `render`. |
| `personas/*.json` | `professional`, `neighbor`, `kawaii`. Noah is built in code. |
| `flock/` | The Flock (§4m): charter, worker, review, supervisor, brainy, probe, branch, run. |
| `tui/` | `app`, `widgets`, `screens`, `panes`, `io_bridge`, `app.tcss`. |
| `tests/` | One file per module; `conftest.py` isolates `COBIRB_HOME` everywhere. |

**Conventions**

- **Docstrings explain *why*, not what** — this is the house style and the reason the code is
  navigable. When you fix a subtle bug, the reason it was a bug goes in the docstring there.
- **Source code never cites a documentation file.** Not this one either. Docstrings used to end in
  `See DESIGN.md §7.`; those documents are gone and the pointers went with them. If the rationale
  matters, state it at the point of decision — which is the house style anyway.
- `from __future__ import annotations` in every module.
- **Fail-closed, never crash.** Broken plugin → reported and skipped. Tool raises → failed
  `ToolResult` the model can correct from. Adapter can't ask → deny. The broad `except Exception`
  blocks are deliberate and each is justified in a comment; read it before "cleaning up".
- **Keep the core thin.** Adding "core" logic for a feature means that feature is probably a
  plugin. This is the #1 architectural decision — don't erode it.
- **Persona data never touches behavior or permissions** (§6).

## 4. Architecture & the loop

Thin core (`Orchestrator`) wires five pluggable pieces and contains **no feature business logic**:
model provider → tool framework → I/O adapter → policy layer → session manager. All of it bounded
by permissions/audit/local-only policy. That separation is what lets speech, vision and MCP arrive
as plugins without touching core.

```
Prompt → understand → inspect → plan → act → observe → reason → iterate → validate → report
```

By default this happens **implicitly in one continuous loop** (`Orchestrator._loop`) — the model
plans, acts and checks its own work in one pass. It ends when the model replies with no tool calls
(that reply becomes `Session.summary`) or after `max_turns` (8).

**Plan mode** (`plan_mode` config, `--plan-mode`, `/plan on|off`; off by default) splits that into
three phases, each recorded as a `Turn` tagged with `Turn.phase`:

1. **plan** — one call with `tools=[]` so it *cannot* act; shown to the user immediately.
2. **act** — the normal loop, following the plan.
3. **validate** — bounded loop (`max_turns=4`) that re-reads files and re-runs tests, reporting
   with concrete references. Stored as `Session.validation`.

`phase` is descriptive — it never changes context replay, but it *is* covered by the turn hash
(§7), so a "plan" turn can't be relabeled "validate". In plan mode `run()` renders the act answer
itself, so the user reads the answer before the validation of it.

## 4a. Every result is bounded

A tool result becomes a session turn *and* a message in the next request, so any tool that can
produce an arbitrarily large answer has to bound it. Each does it in the way that suits what it
returns, and every cap announces itself rather than silently cutting:

- `read_file` **pages** — one call is capped, the file is not. A short read names the offset to
  continue from, so any size is readable in full. A file with no newlines can't be paged by line,
  so it is cut with an explanation.
- `list_dir` and `glob` **page** the same way, over entries and matches.
- `grep` prunes ignored directories *during* the walk rather than enumerating the tree and
  filtering after, clips each matching line as it is stored (a hit in a minified bundle is a match
  of megabytes; keeping 500 of those to truncate later is gigabytes held to produce kilobytes),
  and stops at 500 matches.
- `shell` keeps **both ends** of overlong output. The head is what the command set out to say and
  the tail is usually where it went wrong; either alone makes the other much harder to act on.
- `repo_map` is budgeted, and names the files it could not outline.

Measured against a 10 MB single-line bundle, a 10 MB log where every line matches, and a
50,000-entry directory: peak RSS stayed at 57 MB and the largest result was 65 KB.

## 4b. Context management

`Orchestrator._build_context` serialises the turn history to JSON for the provider — and trims it
to fit first (`cobirb/context.py`). Without this, a session outgrew the window, the server silently
truncated from the front, and the model lost its own objective; that reads to a user as "it got
dumb halfway through".

Passes run in order of how much they cost to lose: **elide old tool results** (a file read eight
turns ago is the cheapest thing to forget, and the call announcing it stays so the model doesn't
re-read it) → **drop the oldest turns** as a contiguous prefix, never orphaning a tool result from
the assistant turn that requested it → **trim inside the working set**, which only fires when one
enormous read is bigger than the whole budget. A short session takes a fast path and is returned
byte-for-byte unchanged. No model call is involved; summarisation would be better and can be added
on top, but a deterministic rule that never makes things worse is the right first version.

**The `num_ctx` trap.** A model advertising a 131072-token `context_length` will still be served
Ollama's own default (4096) unless its Modelfile sets `num_ctx`. So `LocalModelProvider.context_window()`
trusts *only* `num_ctx`, returns `None` otherwise, and the caller falls back to a conservative
4096. Guessing high recreates the exact bug; guessing low only costs some avoidable compaction.
`"context_tokens"` in config is how a user who runs a bigger window says so. `/context` shows it.

## 4c. Undo

`checkpoints.py` copies a file aside before the agent changes it, and `/undo` puts the last
changing turn's files back. Only paths a tool declares via the optional `CobirbTool.writes()` are
saved, and only the first time each is touched in a turn — undo restores the state at the *start*
of the turn, so a second edit must not overwrite the copy taken before the first. Turns that
changed nothing leave no directory behind, and `/undo` reaches past them.

Snapshots are taken **after** approval and **before** execution: a denied call litters nothing, an
approved one always has something to restore. Both the snapshot and the tool's `writes()` are
guarded — failing to snapshot must never block an edit the user just approved.

**`shell` is the honest gap.** It cannot declare what it will change, so anything a command does
is outside undo. That is why `UndoReport.describe()` names the files it actually restored instead
of reporting success: a user told "undone" who then finds otherwise is worse off than one told
exactly what came back.

`/diff` renders the whole session's changes from the same store: for each file ever touched, its
*earliest* snapshot against the file now. Deliberately not `git diff` — this works in a directory
that was never a repository, and it shows the agent's changes rather than conflating them with
whatever the user had already edited. A file the agent edited and then edited back does not
appear, which is correct: nothing changed.

Snapshots are plaintext, unlike sessions. A copy of `src/app.py` exposes nothing that
`src/app.py` did not already expose in the directory it came from, so encrypting it would be
ceremony; the store is 0700 and pruned to the last 20 changing turns.

## 4d. Export

`runtime/export.py` renders a decrypted session as markdown: `--export PATH` alongside `--session`,
or `/export [path]` in the app. Tool results are fenced rather than inlined — they are output, not
prose, and the fence grows to contain any backticks in them.

Deliberately explicit and deliberately plaintext: a "shareable" file that stayed encrypted would
be neither, so this is the one place CoBirb writes a conversation unprotected. It says so when it
does, never picks the destination itself, and still writes 0600 — sharing it is the user's next
decision, not the file mode's.

## 4e. The repo map

`plugins/core/repomap.py` outlines a codebase: which files matter, what is defined in them,
most-referenced first. Python symbols come from `ast` (exact, stdlib); other languages get a small
set of regexes, which is crude next to tree-sitter but avoids a compiled dependency for a job that
is orientation rather than analysis.

**It is a tool, not something injected into every request.** Aider-style tools put their map in
every system prompt, which works at 128k and would be ruinous at the 4096 a local model is usually
served — a permanent map would consume the conversation it exists to help, and fight the
compaction in §4b. As a tool it costs nothing until asked for, and the model can ask about a
subtree. If that turns out to be the wrong call and it should be injected at session start
instead, that is a deliberate decision to take, not a default to drift into.

Ranking: imports carry the most weight (the closest thing to an objective measure of what a
codebase considers central), entry points are boosted because they are where a reader starts
regardless, and tests are pushed down because "where does this behave" is rarely the first
question. Output is budgeted; files that don't fit are still named, because knowing a file exists
is most of what orientation is.

## 4f. Secret redaction

`redaction.py` strips credentials from every tool result, at one chokepoint in the orchestrator so
a plugin tool gets the same treatment as a built-in, and from the audit log's recorded arguments.
A tool result becomes a session turn, a message in the next request and possibly a plaintext log
line, so one `read_file` on a `.env` used to put a live key in three places at once.

**Only high-confidence patterns**: formats with a distinctive prefix or structure (`AKIA…`,
`ghp_…`, `sk-…`, PEM blocks, JWTs). Nothing matches on a *name* — no `password=`, no `SECRET=`, no
entropy heuristics — because those fire on documentation, fixtures, declarations and prose, and a
redactor that eats real content is worse than none. The trade is deliberate and stated: this will
miss a bespoke format, and it will very rarely destroy something that wasn't a credential. The
false-positive tests are load-bearing; don't loosen a rule without adding to them.

Redaction is **visible** (`[redacted: github token]`) so a model can say a key was there rather
than working from a value it never received, and it can be turned off (`"redact_secrets": false`)
because an agent asked to *edit* a credentials file cannot do it through a redacted read.

## 4g. Verification

`runtime/verify.py` runs the project's own check (`"verify_command"`, e.g. `pytest -q`) after any
turn that changed files, and hands a failure back to the model as a user turn — it is a fact about
the world that arrived after its last answer, which is what a user turn is for. Plan mode's
validate phase asks the *model* whether the work is right; this reads an exit code.

Off unless a command is named: no guessing from project layout, because guessing wrong means
running an arbitrary command the user never asked for after every turn. Only after a turn that
actually changed something (`_CHANGING_TOOLS`) — running a suite because someone asked a question
would be absurd, and on a slow suite hostile. The fix loop is bounded to one attempt by default: a
model that can't fix a failing suite in one focused try usually isn't one more turn away.

**It runs outside the permission layer, deliberately.** The command comes from the user's own
config — a more explicit authorization than `allow_tools`, which is a pattern rather than a literal
command — and CoBirb runs it, not the model. The model can neither choose it nor change it
mid-session. Documented rather than quietly assumed.

## 4h. One model per role

`runtime/models.py` resolves a **role** — a job — rather than a model name. `default` is what
everything falls back to, `orchestrator` is the agent you talk to and the only role with a caller
today, `worker` is a subagent given one bounded piece of work and is reserved for the flock
(§12). Roles inherit from `default` field by field, so a role may name a model without repeating
the endpoint it is served from; `--model` outranks all of them. `cobirb models` prints how each
one resolves *and where the answer came from*, including roles nobody recognises, so a typo is
visible rather than a silent fallback.

`worker` resolving before anything calls it is deliberate: a config written today can be checked
today, instead of being discovered wrong on the first fan-out. The three older keys (`model`,
`models.default.name`, `default_model`) all still name the `default` role, in that order —
backward compatibility, not three behaviours.

## 4i. Hooks

`runtime/hooks.py`. A hook is the user's own command at one of four lifecycle points:
`before_tool`, `after_tool`, `before_turn`, `after_turn`. The event arrives as JSON on stdin; exit
0 proceeds.

**`before_tool` is the only one that changes what happens.** A non-zero exit blocks the call and
the hook's own output becomes the reason handed to the *model*, so a hook can say "infra/ is
generated, edit the module instead" and be adapted to rather than retried. It fires **before the
approval prompt**, because a prompt whose answer has already been overruled is a nonsense question.
A hook can only refuse; anything it lets past still goes to the permission layer. The other three
events are observations — a failing one is reported to the user and never blocks, because a
formatter hook that has been quietly exiting 1 for a week is worse than no hook at all.

A hook that **cannot be run, or times out, counts as a refusal**. Failing open there would mean a
guard stops guarding at exactly the moment it breaks.

## 4j. Custom commands

`runtime/custom_commands.py`. A markdown file in `~/.cobirb/commands/` or
`<project>/.cobirb/commands/` becomes `/<filename>`; `$ARGUMENTS` and `$1`…`$9` are filled from
what followed it, and a body with no placeholder gets them appended rather than losing them.
Optional `---`/`description:`/`---` frontmatter, read by a dozen lines rather than a YAML
dependency, since the whole vocabulary is one key.

Built-ins are resolved first, so a custom `/undo` cannot quietly change what `/undo` does. Both
front-ends expand *before* sending, so the transcript shows what was actually asked. Discovery is
per-submission, not cached at startup: editing a command and using it in the same session is how
these get written.

## 4k. MCP

`mcp/` — a hand-written JSON-RPC 2.0 client over stdio (`client.py`) and a `Tool` wrapper
(`tools.py`). Each remote tool becomes `mcp__<server>__<tool>` in the same registry as the
built-ins, so the model schema, the approval prompt, the policy, redaction and the audit log all
treat it identically. That is the point: MCP is a way of *acquiring* tools, not a second set of
rules about what tools may do.

Written by hand rather than depending on the official SDK: the wire format is JSON-RPC over a pipe
and the handshake is three messages, while the SDK brings asyncio, pydantic and transports this
client will never speak.

**stdio only.** An MCP server over stdio is a subprocess on this machine talking over a pipe.
HTTP/SSE transports point at a URL, and a URL is a network call CoBirb did not make and cannot see
inside.

Four things bound the damage, and all four are worth keeping:

- **The environment is not inherited.** A server gets `PATH`, `HOME`, `LANG` plus whatever the user
  listed under `env`. This is the one place a CoBirb-spawned subprocess could read cloud
  credentials and tokens for unrelated services and send them anywhere. `"inherit_env": true` opts
  out, per server.
- **MCP tools are not in `READ_TOOLS`.** CoBirb cannot know whether a remote `fetch_issue` reads,
  writes or bills someone, so the directory-scoped read grant must not generalise to it.
- **A server that fails to start is an issue, never an exception** — reported on stderr and
  skipped. A silent absence would present as the model inexplicably lacking a configured tool.
- **The client owns the process.** `Orchestrator.close()` stops them; one-shot mode calls it in a
  `finally`, and the TUI on every orchestrator discard and on unmount.

What is *not* bounded, and is documented rather than pretended away: a configured server can open
its own network connections and send the arguments of every call it receives anywhere it likes.
CoBirb's promises are about CoBirb. `cobirb help mcp` says this plainly, and carries a worked
example of writing an offline proxy — the case worth building for.

## 4l. There is one config file, and a repository cannot write it

**`~/.cobirb/config.json` is the only configuration CoBirb reads.** It does not read a
`cobirb.json` from the working directory, does not merge one over the user's, and does not look
for one. `Config` takes a `user_path` and nothing else — no `cwd`, deliberately, because a
parameter that no longer selects anything is an invitation to assume it still does.

This replaced a two-layer merge with repo-overrides-user precedence. That is the conventional
shape and it was the wrong one here: configuration in this tool is not preference, it decides what
is pre-approved, which directories may be read or written, what runs at lifecycle points and which
subprocesses start. A repository able to contribute any of that means cloning it and running
CoBirb inside it lets its author influence the permission model — before the model is asked
anything, and with no prompt to intervene, because the prompt is something CoBirb decides to show
and this would be config deciding whether to show it. Not hypothetical: a committed `cobirb.json`
naming `allow_tools` pre-approved those tools silently, verified by running it.

`hooks` and `mcp_servers` were built against a narrower version of this rule (a `user_get` that
read past the repo layer). That helper is gone — with no repo layer there is nothing for it to
read past, and one accessor is better than two where the difference used to be load-bearing.

The narrower fixes were considered and rejected: exempting the dangerous keys, or prompting once
to trust a directory. Both keep the shape and rely on the list of dangerous keys staying correct
forever — a standing obligation on every future setting. Removing the layer has none.

**What a repository may still do is describe itself.** `AGENTS.md` instructions, the repo map and
prompt files under `<project>/.cobirb/commands/` are all still read. Those are *content for the
model*, not capability granted to it, and a custom command only expands to a prompt when the user
types its name — every tool call it leads to still goes through the permission layer. The line is:
a project may tell CoBirb about itself, never tell CoBirb what it is allowed to do.

The cost is per-project settings of any kind, including a reviewed `cobirb.json` in a CI checkout.
Accepted: `--allow-tool` covers that case on the command line, where it is visible in the job
definition rather than in a file that travels with the code.

## 4m. The Flock

`flock/`. One **Brainy Birb** (the lead) divides work between several **Worker Birbs** that cannot
see each other. Shipped in v0.5.0; `cobirb flock -p "..."`.

**The skeleton is the communication channel.** Brainy Birb plans, designs the seams, and writes the
interfaces, typed stubs, semantic docstrings and failing tests into the project *before* fanning
out. Workers never coordinate because everything they would have had to agree on is already written
down where all of them can see it. Each gets one brief and nothing else — no `AGENTS.md`, no repo
map; conventions reach a worker through the stub it is filling in, which doubles as the style guide.

| Module | Responsibility |
|---|---|
| `charter.py` | TOML in, scopes and seams out. `find_conflicts`, `policy_for`. |
| `worker.py` | One brief → one ordinary agent run → one report. |
| `review.py` | The three verification passes and the baseline. |
| `supervisor.py` | Fan out, join, review, account for the round. |
| `brainy.py` | The lead's guidance, and `propose_charter`. |
| `probe.py` | Does this endpoint answer two requests at once? |
| `branch.py` | The GUID pairing a flock session to its main one. |
| `run.py` | The five stages, including the one approval. |
| `tui/flock_bridge.py` | `TuiAsker` (modals answer the flock's questions) and `WorkerPaneIO`. |

**Writes are strict; reads are project-wide.** A worker may change only the files its charter
named — that is the isolation that matters, and what lets two workers run at once without
clobbering each other. Reads were scoped just as tightly in the first version, on the theory that a
worker "does not know the other files exist." The first real run showed what that actually bought:
workers denied on every `list_dir` and `glob`, unable to orient themselves, burning turns on
"permission denied" instead of the ticket. The knowledge isolation never depended on the read grant
— the charter is never written to disk and the brief omits it, so a worker reading a sibling sees
code, not the plan. Reads are now open to the whole working directory; only writes stay file-level,
out of the same directory-level `Policy` machinery (`_within(path, base)` matches a path that *is*
the granted one or sits beneath it, so granting a file matches that file and nothing else).
`shell` is granted to no worker; its acceptance check is run *for* it instead.

**A Worker Birb is not special** (§4n). `wiring.build_subagent` composes an ordinary run and
differs in four places: policy handed in rather than read from config, no project context,
`HeadlessIO`, and a scoped acceptance check. It inherits checkpoints, redaction and the user's hooks
because it *is* an ordinary run.

**Trust, then verify.** Workers may edit their own tests and should add more — locking them out
would ship under-tested code to prevent a cheat review catches anyway. Three passes, cheapest
first: read the diff for vanished assertions, disabled tests and changed declarations; put the stub
back and confirm the worker's tests go red; mutate each behaviour the docstring claims and confirm
each is caught. A surviving mutant is usually *Brainy Birb's* omission — a behaviour specified with
no acceptance test behind it.

⚠️ **Review runs after the join, never during it.** Reviewing puts an implementation back to its
stub for a moment; a colleague still running whose check imports that file would fail for a reason
unrelated to its own work.

**Dependency Inversion is hardcoded into `BRAINY_RULES` on purpose.** It is not a style preference
here: if worker A needs worker B's concrete implementation the work is serial however many workers
exist, and hoisting the abstraction into a Brainy-Birb-owned file turns one dependency edge into
two independent ones. The prompt carries it as a check to run over its own partition. Seams are
declared `formal` (the type system holds them up) or `loose` (nothing does, so a test must).

**Flock mode cannot run headless.** The charter approval is the only place a person sees what the
workers will be allowed to touch; a flock approving its own charter would be an agent granting
itself permissions. An overlapping partition is reported and the user asked — carrying on drops
concurrency to 1, since honouring the choice means removing what made it unsafe.

Undo is git's, deliberately (§12.1). Each engagement gets its own session file beside the main one,
paired by a GUID; a new *round* continues that session rather than starting another.

**A stuck Worker Birb can be force-stopped.** The graceful stop (an `Event` the supervisor already
checks) only keeps *new* workers from starting — it cannot reach one already blocked waiting on the
model, because that thread is not checking anything, which is exactly the state a user hit ("stuck
waiting for Ollama... Ctrl+C says it'll stop after the worker finishes"). `Canceller`
(`supervisor.py`) holds every live worker's orchestrator and calls `Orchestrator.cancel()` on each,
which reaches the model provider's `cancel()`. `LocalModelProvider._stream_chat` opens its
connection through `http.client` rather than `urllib` for exactly this: `urllib` hides the socket,
and closing an `HTTPResponse` from another thread does **not** wake a blocked `recv` on Linux — only
`socket.shutdown()` does, and that needs the real socket. Dropping the connection is also the signal
that tells Ollama to stop generating; there is no per-request abort endpoint. The TUI escalates:
first Ctrl+C is the graceful stop, a second once already stopping offers the force-stop, and quitting
goes straight to it (no dialog to answer on the way out).

⚠️ **The force-stop does not reach a hang inside `/api/show`.** `chat()` calls it before every
request — streaming or not — to resolve the context window, and `_post` (which serves it, and the
non-streaming `chat()`) deliberately stayed on plain `urllib` rather than moving to the same tracked
socket as `_stream_chat`. Doing so would close this gap completely, at the cost of rewriting roughly
30 existing tests in `test_model.py` that mock `urllib.request.urlopen` at that exact call — a large,
mostly-mechanical diff for a narrow case. In practice this rarely bites: every Worker Birb turn goes
through `_stream_chat` (streaming is on whenever an `io` adapter is attached and the provider
supports it, which `HeadlessIO` plus this provider always are), and `/api/show` is a fast local
metadata call that only hangs if the whole server is already wedged, not merely mid-generation. Left
here rather than fixed silently or fixed at the cost of that diff without asking.

## 4n. Subagents are ordinary runs

A Worker Birb "is not special — it just got its instructions from another agent instead of a
human". That is a design rule, not a description: it keeps rejecting machinery that would otherwise
seem reasonable. Mutation testing edits files in place with no sentinel and no crash-recovery,
because a kill mid-edit is the same situation as any agent killed mid-edit and is version
control's problem. There is no worker runtime, no sandbox, no special execution mode — those would
all drift from the real agent within a month.

The generalisation worth keeping: **do not reach for access control to solve what is a review
problem**, and do not build a parallel path for an agent that can use the existing one.

## 4o. Say what actually failed

`urllib.error.HTTPError` is a **subclass** of `URLError`, so a single
`except urllib.error.URLError` catches both "nothing is listening on that port" and "the server
answered, and the answer was no" — and, in `LocalModelProvider`, reported them identically as
*"Could not reach the model provider … Is Ollama running?"*.

That is how a model the endpoint did not have came back as a connectivity problem, with Ollama's
own explanation (`model "x" not found, try pulling it first`) sitting unread in the response body.
Found in the field, by a user whose three Worker Birbs all failed against an Ollama that was
plainly running. See `_unreachable` and `_error_body`.

The rule this leaves behind: **a transport failure and a rejected request are different questions
and must not share an error message.** "Is it running?" is the right thing to ask only when nothing
answered. When something answered, it has already said what is wrong, and repeating its words beats
guessing at them.

`flock/preflight.py` applies the same idea one level earlier: a flock is the first thing that uses
two model roles at once, so a worker model named in config but never pulled produced one identical
failure per Worker Birb, minutes after approval, with the planning work already spent. One
`/v1/models` call before planning turns that into a sentence. A *check*, not a gate — an endpoint
that cannot answer it gets the benefit of the doubt, since being unable to list models is not
evidence that a model is absent.

## 5. Plugin SPI

Core imports only these interfaces; plugins import only the SPI and never each other; core never
relies on a plugin's module layout beyond its entry point.

### 5.1 Discovery & merging

Sources: **built-in core plugins** (imported directly, so they work even if discovery fails) →
`$COBIRB_PROJECT_DIR/cobirb/plugins/<name>/` → `$COBIRB_HOME/.cobirb/plugins/<name>/` → installed
entry points:

```toml
[project.entry-points."cobirb.plugins"]
my-tool = "my_plugin.tools:MyTool"      # module.path:ClassName
```

The slot is decided by **inspecting the class hierarchy** against the SPI bases, not the entry-point
name. Discovery is lazy, cached per run, and non-fatal — errors go to stderr (CLI) or the Plugins
tab (TUI) and the run continues.

- **Tools are additive.** Every discovered `Tool` is registered alongside the built-ins; the policy
  still gates whether it can *run*, so no extra trust decision is needed to make it visible. A name
  collision with a *different* implementation is skipped and reported; the same plugin found twice
  (entry point *and* local dir) is recognised as a harmless duplicate by comparing the class.
- **Model/IO/crypto are singleton slots.** Replacing a core default is opt-in via
  `plugins.model|io|crypto`. Unset — or `core-model`/`core-io`/`core-crypto` — keeps the core
  default, built with its *normal* constructor arguments rather than a bare no-arg construction.
  Naming an undiscovered plugin is reported and the default kept.

### 5.2 Interfaces (`typing/spi.py`, re-exported from `cobirb/__init__.py`)

```python
class ModelProvider(abc.ABC):
    def name(self) -> str: ...                                  # "ollama/llama3.1"
    def chat(self, system: str, context: str, tools: Optional[list[Tool]] = None,
             *, stream: bool = False) -> "Iterable[str] | str": ...
    def parse_tool_calls(self, raw: str) -> list[ToolCall]: ...
    def supports_tool_calling(self) -> bool: ...
    def supports_streaming(self) -> bool: ...
    def supports_vision(self) -> bool: ...

class Tool(abc.ABC):
    def name(self) -> str: ...              # machine name, used for allow/deny matching
    def description(self) -> str: ...
    def parameters(self) -> dict[str, Any]: ...        # JSON-schema-ish
    def execute(self, arguments: dict[str, Any]) -> ToolResult: ...

class I_OAdapter(abc.ABC):
    def name(self) -> str: ...
    def render(self, text: str) -> None: ...           # once per streamed token; no trailing newline
    def listen(self) -> Optional[str]: ...
    def view(self, data: bytes, mime: str | None = None) -> None: ...
    def confirm(self, tool_name: str, arguments: dict[str, Any]) -> str: ...
    # optional: confirm_scoped(ApprovalRequest) -> str, for adapters that can
    # show what "always" grants and what the call would change.

class SessionCrypto(abc.ABC):
    def name(self) -> str: ...                                  # "aes256gcm-scrypt"
    def encrypt(self, plaintext_json: str, password: str) -> bytes: ...
    def decrypt(self, blob: bytes, password: str) -> str: ...   # raises on wrong password/corruption

@dataclass class ToolCall:   name; arguments
@dataclass class ToolResult: ok; content; error; meta
@dataclass class Persona:    name; species; tone; greeting; phrasings; emoji_density; known_squawks
```

- `context` is the history as a compact string; the provider decides how to unpack it. Core encodes
  JSON (`_build_context`) and `LocalModelProvider._build_messages` parses it into a role-tagged
  `messages` array. **This matters:** flattening history into one opaque user message was the root
  cause of the tool-calling loop never converging — a `tool` result appeared with no assistant turn
  requesting it, so the model just repeated the call.
- `confirm()` returns exactly `"once"`, `"always"` or `"deny"`. An adapter that cannot ask **must**
  return `"deny"` — fail-closed, not fail-open.
- **`list_models()` is optional and duck-typed.** Core implements it via OpenAI-compatible
  `GET /v1/models` (not Ollama's `/api/tags`), so it works against llama.cpp/vLLM/LM Studio too.
  Only interactive mode calls it, and only via `cli._build_model(None, cwd)` — a `plugins.model`
  lacking it breaks nothing.
- **Duck-typed rendering hooks** beyond the contract, implemented by both shipped adapters:
  `spinner`, `begin_stream`, `render_header`, `render_answer`, `render_plan`, `render_validation`,
  `render_tool_call`, `write_error`. Reached via `getattr(io, ..., None)` with a plain-`render()`
  fallback, so a future speech/vision adapter isn't forced to implement chrome.
- **`Tool.name` is a method, and only a method.** Built-ins declare `NAME: ClassVar[str]` and
  inherit `name()` from `CobirbTool`; a tool that exposes `name` as a plain attribute is rejected
  by `ToolRegistry.register` with a message saying so, and `cli._merge_tool_plugins` reports and
  skips it. Tolerating both shapes is what once let a bound method reach a JSON payload and break
  every turn, so the boundary validates instead.

### 5.3 Built-in tools

| Name | Notes |
|---|---|
| `read_file` | Paged. One *call* is capped so a single read can't take half the window; the *file* is not — a short read reports the offset to continue from. |
| `write_file` | Creates parent directories. |
| `edit_file` | Exact `old_str` match, first occurrence only. |
| `apply_patch` | Unified diff; verifies context/removed lines and refuses rather than guessing. |
| `glob` | Paged like `list_dir`. Skips ignored paths unless `include_ignored`. |
| `grep` | Prunes ignored directories during the walk rather than filtering after; clips each match line and caps at 500. |
| `list_dir` | Paged; directories marked with a trailing slash. |
| `repo_map` | Ranked outline of the codebase — see §4e. |
| `shell` | **Highest privilege; gated.** Output keeps both ends when it overflows. Own process group on POSIX so forked children die with it; `cancel_running()` backs the TUI's Ctrl+C. |

All extend `CobirbTool`, whose `_resolve()` joins relative paths against the configured working
directory — otherwise a model's `"src/foo.py"` would resolve against the *process's* cwd, not
`--cwd`.

### 5.4 Packaging & testing

A local plugin is `cobirb/plugins/<name>/` with a `pyproject.toml` declaring entry points and
`src/<name>/__init__.py` exporting the classes. Any version loads; mismatches surface non-fatally.

Tests: per-tool units (`test_tools.py`), a stub-provider loop contract (`test_orchestrator.py`),
crypto round-trip incl. tamper and wrong-password (`test_crypto.py`), plugin load + broken-plugin
survival (`test_loader.py`), live Ollama behind `COBIRB_TEST_MODEL` (`test_integration_ollama.py`).

### 5.5 `cobirb plugin install/list/remove` (`runtime/plugin_install.py`)

A published entry-point package needs nothing from this — `pip install some-plugin` is already the
whole story. This exists only for the local-directory kind: `_load_local_plugin` resolves a plugin
under `~/.cobirb/plugins/<name>/` through real `importlib.metadata` package metadata, which means it
must genuinely be `pip install -e`'d under a name matching the directory, a step easy to get wrong
by hand. `cobirb plugin install <path>` automates exactly that sequence and refuses early — before
touching pip — if `pyproject.toml` is missing, has no `[project].name`, declares no
`[project.entry-points."cobirb.plugins"]` table, or the name doesn't start with
`cobirb_plugins_<name>` (the prefix the loader's directory-basename convention requires).

**Deliberately local-only, forever.** `install` takes a path already on disk, never a URL, a
package name to resolve, or a version to fetch. There is no index and nothing it reaches out for —
a registry, even a curated one, is a new trust and networking question this project has repeatedly
closed off. No checksum or signature check either: the source is already on the user's own disk
under their own control before this ever runs, so copying it crosses no new trust boundary. That
verification would only earn its cost once this module can *fetch* a plugin rather than merely
relocate one, which it deliberately cannot.

**A genuine Python gotcha, worth knowing if this file is ever touched again:** right after a fresh
`pip install -e`, `importlib.metadata.distribution()` finds the new package immediately (it just
reads the dist-info pip wrote), but the module itself is not yet importable in the *same* running
process — `site` only processes new `.pth` entries at interpreter startup. `install_plugin()`
verifies its own work by re-running discovery, so this bit a real end-to-end test the moment
`_run_pip` was no longer mocked. Fixed by `_make_importable_in_this_process()`
(`importlib.invalidate_caches()` + `sys.path.insert(0, target)`) — which also means a plugin
installed while CoBirb is already running works immediately, no restart needed.

`remove` only ever touches what this mechanism itself created (`~/.cobirb/plugins/<name>/`) — never
a project-scoped plugin or one installed by hand with plain `pip install`. Removing the directory
is what actually controls discovery going forward; the `pip uninstall` alongside it is best-effort
tidiness. Both `install` and `remove` roll back cleanly on failure rather than leaving a directory
that looks installed but was never discovered.

**`--replace` never costs you the plugin you already had.** The existing directory is *moved* to
`~/.cobirb/plugins.being-replaced/<name>` rather than deleted, and put back — with a best-effort
`pip install -e` to restore the editable dist the failed attempt took down with it — if the new
source turns out not to install or not to be discoverable. Parked beside `plugins/`, never inside
it: discovery walks everything under `plugins/`, so a copy stranded there by a process killed
mid-reinstall would show up for good as a second, wrongly-named plugin.

**SPI security:** a plugin cannot pre-approve its own tools — the policy is the sole authority; a
plugin-declared `shell`-grade tool inherits the same gate; plugin tool calls hit the audit log like
any other.

## 6. Personas

**Off by default.** A persona is a costume, and CoBirb sends it as a system message — which
*replaces* the model's own Modelfile `SYSTEM`. Wearing one by default would silently override a
configuration the user built deliberately. So an unconfigured run sends no voice instructions and
the model sounds like itself.

- `build_plain_persona()` — name `"CoBirb"`, every voice field empty. `persona_shapes_voice()`
  checks exactly that and is the signal to emit no persona block.
- **Bundled:** `noah` (code), `professional`, `neighbor`, `kawaii` (JSON). Fields: `name`,
  `species`, `tone`, `greeting`, `phrasings[]`, `emoji_density`, `known_squawks[]`.
- **Resolution:** bundled → `./<name>.json` → `~/.cobirb/personas/<name>.json`. An unknown name
  falls back to *no* persona, never Noah — a typo must not dress the model in a character nobody asked for.
- Sessions store `cli._persona_key()` (the name that *reloads* it), not the display name:
  `kawaii.json` calls itself something else, so the display name wouldn't resolve back to a file.
- Tone: friendly and playful, **not** saccharine. Light puns, no sparkle.
- **Hard rule:** persona data shapes *how* CoBirb speaks and nothing else. It can never grant
  permission to skip permissions, encryption or network controls.

**The model's own prompt wins.** Ollama takes one system message per request. Default
(`--system-prompt off`) sends none, so the model behaves as in `ollama run`. When CoBirb *does* have
something to add (persona, plan-mode instructions, `--system-prompt harness`), `compose_system()`
reads the model's own prompt via `/api/show` and puts it **first**. The `harness` block only states
that tool calls are human-gated — worth it because a model that doesn't know a denial is a decision
will retry a blocked tool until the loop gives up. No CoBirb guarantee depends on any of this;
they're enforced in `policy.py` and the crypto backend, not by asking the model to cooperate.

## 7. Sessions & encryption

**Threat model:** plaintext conversation never recoverable from disk; password never echoed or
logged; resist offline brute-force of a stolen file.

```
on disk = base64( salt ‖ nonce ‖ AES-256-GCM(plaintext, key, aad=b"cobirb") )
          key = scrypt(password, salt)   ← RAM only; N=2**14, r=8, p=1 (RFC 7914 interactive)
```

Fresh random salt and nonce per encryption; both travel with the ciphertext (not secret). Vetted
`cryptography` library, never hand-rolled; the backend is a swappable plugin. The password derives
the key once and is discarded — `SessionManager` deliberately doesn't retain it.

**No post-quantum KEM, deliberately.** A KEM protects a shared secret two parties establish over a
public key. A password-protected local file has no such exchange — one user, one password, no
counterparty — so there's nothing for a KEM to protect that scrypt doesn't cover. AES-256 is already
quantum-resistant here (Grover halves it to 128 bits), and a KEM re-derived from the same password
wouldn't raise the cost of password guessing. **Don't resurrect this** without a real asymmetric use
case (e.g. encrypting to a device's public key for multi-device sync).

Plaintext shape (RAM only; disk holds the blob above):

```jsonc
{ "format": "cobirb-session", "schema": 1, "created_at": "...", "working_dir": "/abs/path",
  "persona": "none", "summary": null, "validation": null,
  "turns": [ { "role": "user|assistant|tool", "content": "...", "tool_use": null,
               "phase": null, "ts": "...", "hash": "..." } ] }
```

Each turn carries a content hash, verified on reload (`_verify_hashes`). The digest covers `role`,
`content`, `tool_use` **and** `phase` — hashing the prose alone would accept a session whose
recorded `read_file` had been rewritten into a `shell` call. Sessions default to
`$COBIRB_HOME/.cobirb/sessions/`; `--session <path>` works anywhere. Saved every turn (interactive)
or once at the end (one-shot); on exit the reopen command is printed with a bare `-w`, so the
password never reaches scrollback.

## 8. Permissions & audit

**Default-deny, with nothing pre-approved.** `build_default_policy()` returns a policy that allows
*nothing*; a deny list always wins over anything later granted. Capabilities arrive three ways: the
user answers a prompt, they list rules in config's `allow_tools`, or they pass `--allow-tool`. Both
rule forms take the same syntax — `name` or `name(arg)`, e.g. `shell(python -m pytest)`.

**Reads and writes are each scoped by directory** (`allow_read_dir`/`READ_TOOLS`,
`allow_write_dir`/`WRITE_TOOLS`), in **two separate sets that never imply one another** — agreeing
CoBirb may read a project is a far smaller thing than agreeing it may rewrite one. Approving one
call grants the matching tool set that directory and everything beneath it, so a user who agreed
CoBirb may work somewhere isn't asked again per file. `shell` is in neither set: it can't say what
it touches. Paths are compared after `realpath`, so `..` and symlinks can't name a file outside an
approved tree, and the separator check stops a grant on `/x` covering `/x-secrets`.
`allow_read_dirs`/`allow_write_dirs` in config state standing scopes.

**The approval question carries a preview.** Tools that change files implement an optional
`preview(arguments)` (`CobirbTool.preview`) returning a unified diff of what the call *would* do;
the orchestrator puts it in the `ApprovalRequest` and both adapters show it before asking.
Approving a write you have not seen is approving the tool rather than the change, and the change
is the thing that matters. By contract a preview runs before approval, so it must never raise and
never alter anything — the orchestrator guards it anyway, because no diff is a much better outcome
than no question.

**`Policy.grant()` decides what an "always" answer widens to** — a read to its directory, a shell
call to its invocation, anything else to the tool name — so the orchestrator doesn't have to know.
`Policy.describe_grant()` renders that as prose for the prompt, reached through the optional
`confirm_scoped` adapter hook, because agreeing to "always" on a read means agreeing to a directory
and a prompt that can't say so is asking the user to agree blind.

**Two kinds of `shell` rule:** *first-word* trusts a binary with any arguments (`git` also allows
`git push`); *prefix* trusts one multi-word invocation (`python -m pytest` does not allow
`python -c`). `allow(tool, command)` produces a prefix rule for a multi-word command — narrowing to
the first word would defeat the point.

**Every segment is checked.** `git status; rm -rf /` is two commands and the shell runs both, so
`_segments` splits on `;`/`|`/`&` and requires all segments to pass. Quoting is respected;
redirection stays attached to its command. Anything unverifiable — command substitution, backticks,
subshells, unbalanced quotes, and **`find`'s `-exec`/`-execdir`/`-ok` family** — is **refused**,
because the shell would run the whole string. (`-exec` is refused specifically because the `;`
terminating its clause reads as a separator, so the exec'd program lands in an unchecked tail
segment while `find` itself looks innocuous.) Motivation for narrowing generally: `bash` outright is
unwise but `bash -n` is fine; `python -m pytest` is safe where bare `python` isn't.

**Interactive approval.** A call the static list doesn't cover isn't silently refused — `confirm()`
asks, giving `once` / `always` (extends the live policy for this run) / `deny`. That's what makes
default-deny mean *asks first* rather than *the model never learns it could have worked*. No
adapter, or one that can't ask, fails closed.

**What CoBirb does not enforce: anything past the model socket.** Every prompt, file and diff
goes to a long-lived server nobody here wrote, over an unauthenticated local port other processes
can also use, by a program that fetches from a registry and makes its own outbound requests. That
is trust, not enforcement, and it is the largest remaining gap between the pitch and the code.

**Decided September 2026: CoBirb does not close that gap itself.** An embedded GGUF runtime was
designed and then withdrawn mid-design — see §12.1. CoBirb is the *client* of an OpenAI-compatible
endpoint and does not run models; where inference happens is the user's business, and they are free
to point it at an endpoint they wrote. So this gap is documented honestly (README has a "What
CoBirb does not protect you from" section) and not treated as a defect awaiting a fix. Say so
plainly rather than implying the promise reaches further than it does.

**What the policy layer does not do: sandbox.** It decides *whether* a command runs, never what
it can reach once running. An approved `npm test` has your full user privileges and can read
`~/.ssh`. Decided: document this plainly (README has a "What CoBirb does not protect you from"
section) rather than build a sandbox. The SPI allows a third party to replace the built-in `shell`
tool with a sandboxing one, which is the right place for it. Don't quietly imply otherwise in docs.

**Audit log** — append-only, local, never leaves the machine, **off by default** (`"audit_log":
true`). Off because arguments are logged unredacted: `write_file`'s full content, `edit_file`'s
old/new text, `apply_patch`'s diff, `shell`'s command. Always-on would be a second plaintext,
unencrypted, never-rotated copy of everything written or run — contradicting encrypted sessions.
Nothing is written and no file created until enabled.

## 9. CLI & interactive surface

**Headless** (`--headless`, usually with `--output json`) never prompts: anything not permitted by
`allow_tools`/`--allow-tool` is refused outright, because in a pipeline the terminal prompt blocks
on a question nobody will answer. Exit codes are what CI consumes — 0 clean, 1 failed, 2 completed
but something was refused. **The 2 is headless-only**: a person who answered "no" themselves got
what they asked for, and reporting that as non-zero would make an ordinary decision look like a
broken script. There is deliberately **no flag that approves everything** — a policy file is a
decision someone made once, reviewably; a blanket flag is a decision nobody made. A test asserts
that flag's absence so the argument can't be lost by accident.

**One-shot** (`cobirb -p`) is plain stdout via `TerminalIO` so it pipes and scripts — deliberately
*not* the full-screen app, since that can't be piped. `--allow-tool` takes `name` or `name(arg)`,
repeatable.

**Interactive** is a full-screen Textual app. Textual is an accepted exception to the
few-dependencies stance (a tab bar, fixed regions and modals need a real framework, not more `rich`
polish) and is imported lazily, so one-shot never loads it. Three live tabs:

- **Current** — transcript, live status line (persona · model · plan · cwd · session), boxed input
  that stays usable through a turn rather than greying out (mid-turn steering, below) and returns
  when done (no `continue? [y/N]` gate), tool approval as a modal (`y` once / `a` always / `n`/escape
  deny).
- **Sessions** — lists `.json` files under `default_sessions_dir()` with size/mtime read **without
  decrypting**; resumes one (a `TextPromptModal` collects the password, since a full-screen app
  can't use `getpass`), branches one (below), or starts a new one. Resuming validates the password,
  reads persona/turn count, sets `self.orchestrator = None`, and lets the *next* message rebuild
  through the same load-or-create path `--session` uses — so **only one code path ever opens a
  session file**.
- **Flock** — one pane per Worker Birb (§4m), laid out from the approved charter and updated live
  from the supervisor's `started`/`finished`/`reviewed` events, plus a `WorkerPaneIO` per worker so
  its tool calls appear as it makes them. Each pane keeps its *scope* on screen rather than letting
  it scroll away with the log: what a worker may touch is the whole of its isolation, and a person
  watching should be able to see it at any moment. `ctrl+c` mid-flock asks first (`ConfirmModal`)
  — a flock is several agents deep in a working tree, and an accidental keypress that silently
  abandoned one would leave it in a state nobody chose.
- **Plugins** — `cli.describe_plugins()`'s live snapshot: registered tools with source, active
  implementation per slot, discovery problems. It exists because a full-screen app's stderr is
  invisible, and that's where CLI modes report the same issues.

Commands `/model`, `/persona [name]`, `/plan [on|off]`, `/context`, `/undo`, `/diff`, `/export`,
`/commands`, `/flock <objective>`, `?`/`/help [topic]`. Anything else beginning with `/` is looked up as a custom command
(§4j) and otherwise goes to the model unchanged. Non-interactive: `cobirb models` (§4h),
`cobirb commands`, and `cobirb plugin install <path> [--replace] | list | remove <name>` (§5.5) —
the `topic`/`target` positional is shared with `help` and validated by hand rather than through
argparse `choices=`, since which verbs are legal depends on the subcommand. Keys: `f1` help, `f2`
next tab, `ctrl+q` quit, `up`/`down` recall the last 100 prompts (memory only), `ctrl+c` copies a
selection if there is one else cancels the turn. `/model` hot-swaps `orchestrator.model` in place
rather than rebuilding, preserving this session's "always allow" approvals. `default_model` is
validated at startup *silently* — unavailable is ignored, not an error — and if nothing resolves the
picker opens unprompted.

**Mid-turn steering** (`Orchestrator.steer()`, v0.6.0). Submitting a message while a turn is
already running redirects it instead of queuing a second one: the prompt box is left enabled
through a turn (it used to disable outright), and `on_prompt_input_submitted` routes to
`_steer_current_turn` — no command dispatch, no custom-command expansion, just the raw text handed
to `steer()` — whenever `_turn_in_progress` is true. Two things happen, and either alone is enough:

- The message is queued and applied as a user turn at the next loop boundary (`_loop`'s
  `_drain_steer`, between tool calls or before the next model call), prefixed with a short preamble
  telling the model plainly that this arrived while it was still replying — a small local model has
  no other way to know its own output was just interrupted.
- If the model is *currently streaming* and implements the optional, duck-typed
  `interrupt_current_reply()` (`LocalModelProvider` does), that stream is cut off immediately by
  raising `SteeringInterrupted` out of `chat()` — the same tracked-socket `shutdown()` force-stop
  uses, but **resumable**: it sets a `_steer_signal` that `_as_control_exception` consumes once and
  clears, rather than `cancel()`'s one-way `_cancelled` latch, so the very next request opens a
  fresh connection and behaves normally. Getting this to actually raise (rather than let the chunked
  reader end the stream quietly on a clean EOF) needs *both* `shutdown()` and `close()` on the
  connection — that pair is `_drop()`, shared with `cancel()`, and both halves are load-bearing: a
  real, non-obvious finding from testing this against a real socket rather than a mock, the same
  discipline that found the force-stop bug.
  ⚠️ `_stream_chat` **clears `_steer_signal` before every request**, because an interrupt that lands
  in the gap between the last chunk and the generator returning sets the flag with no exception left
  to consume it. A signal surviving into the next request would report that request's first genuine
  failure — an unreachable endpoint, say — as a steer: the same conflation of "we did this" with
  "the server said no" that §12's 404 field note existed to stamp out.

The partial reply is kept as a genuine (if incomplete) assistant turn, not discarded or treated as
an error — `_loop` records it, shows a small "redirected by a new message" notice, and loops
straight back around to pick up the queued message, consuming one of `max_turns`. Rendered in the
transcript with a distinct `»` marker (`render.build_steer_message`, bold magenta, no closing
rule) so it reads as an interjection into something already in progress rather than a fresh
exchange. Not built for the Flock: a worker's own steering is a different, unbuilt question.

**Session branching** (`session.fork_session()`, v0.6.0). Trying a different direction from a point
already on disk, without disturbing what got you there — the Sessions tab's "Branch…" button forks
the *whole* of a selected session into a brand-new file and switches straight into it, the same way
"Resume" does (reusing `_apply_resumed_session`, with a `verb` parameter so the status line says
"Branched" rather than "Resumed"). The source file is only ever read: it comes out bit-for-bit
unchanged and stays independently resumable at its original length. `Session.forked_from` records
`"<source path>@turn<N>"` on the branch — the same lineage idea as a Flock session's `flock` pairing
token, so a branch found months later still says plainly where it came from.

A branch is a faithful copy, not just its turns: `persona`, `working_dir` and the `flock` token come
along (`flock` names the *engagement*, not a turn, so a branch of a flock session is still part of
that flock). `summary` and `validation` are the exception when `--branch-at` truncates — both
describe the end of a conversation, and a truncated branch is precisely the one that doesn't have
that end, so carrying them would caption it with a conclusion it no longer contains.

Branching from an *earlier* point in the conversation, rather than its current end, is CLI-only for
now: `cobirb --session PATH -w --branch NEW_PATH --branch-at N` keeps turns `0..N` inclusive. The
TUI button doesn't expose this — picking a cut point needs its own turn-list UI, a bigger piece than
this milestone's scope, so the one-click path only ever branches everything. An explicit
`--branch` destination that already exists is refused outright, matching `plugin install`'s
existing-target convention (§5.5) rather than silently overwriting it; the TUI's generated filename
(`<stem>.branch-<8 hex>.json`, alongside the source) sidesteps the question entirely.

**Threading.** `Orchestrator.run()` stays **synchronous and untouched**; the app runs it on a
Textual thread worker and `TuiIO` marshals every callback back via `App.call_from_thread`. No
callback may touch a widget directly. `confirm()` is the sharp edge: it must block the worker and
return a decision string synchronously into `_execute_tool`, which it does by having
`call_from_thread` schedule a coroutine awaiting `push_screen_wait`. It deliberately skips
`TuiIO._call`'s same-thread shortcut — `push_screen_wait` only works inside a worker, and calling it
on the UI thread would deadlock the loop that has to dismiss the modal.

⚠️ **Always `event.stop()` in modal handlers.** `TextPromptModal`'s unstopped `Input.Submitted`
bubbled to the App's own `on_input_submitted` (the chat box's), which can't tell one `Input` from
another — so typing a session password and pressing enter *also* ran it as a chat prompt.

**One render layer.** Both adapters build panels from the same pure builders in
`plugins/core/render.py` (they return a Rich renderable and never touch a console); `TerminalIO`
prints them, `TuiIO` writes the same objects into a `RichLog`. That's why the two modes look
identical and why chrome is defined once. A `plugins.io` selection replaces whichever adapter the
mode would have used — in interactive mode that means the *adapter*, not the app, decides where
output goes.

## 10. Configuration

`$COBIRB_HOME/.cobirb/config.json` — **the only config file there is** (§4l). No repo layer, no
merge, and no `cobirb.json` in the working directory is read or even looked for. Nothing defaults
to a networked provider. See `config.json.example`.

| Key | Meaning |
|---|---|
| `default_model` | Default model. Validated at interactive startup; unavailable is ignored, not an error. |
| `model`, `models.default.name` / `.base_url` | Equivalent older keys; endpoint URL. Any OpenAI-compatible server works. |
| `instructions` / `instructions_max_chars` | Read `AGENTS.md`/`CoBirb.md` from the working directory into the system prompt (on by default, capped at 2000 chars). |
| `allow_read_dirs` / `allow_write_dirs` | Directories CoBirb may read from / change files in without asking. Separate lists on purpose. |
| `allow_tools` | List of permission rules in `--allow-tool` syntax, e.g. `["read_file", "shell(git)"]`. The user's standing exemptions from the prompt. |
| `persona` | Default persona. Unset or `"none"` means none. |
| `system_prompt` | `"off"` (default) or `"harness"` — §6. |
| `plugins.model` / `.io` / `.crypto` | Select a discovered plugin for that slot. |
| `models.<role>.name` / `.base_url` | One model per role — `default`, `orchestrator`, `worker`. Roles inherit from `default` field by field. §4h. |
| `plan_mode`, `audit_log` | Both default `false`. Read §8 before enabling the latter. |
| `hooks` | Your own commands at four lifecycle points; a `before_tool` hook can refuse a call. §4i. |
| `mcp_servers` | Local MCP servers to start and take tools from. §4k. |

Model name resolution: `--model` → `models.<role>.name` → `model` → `models.default.name` →
`default_model`. The last three all name the `default` role; the multiplicity is backward
compatibility, not three behaviors. `cobirb models` prints the result.

Env: `COBIRB_HOME` (relocates the whole `.cobirb` tree — see `paths.py`; how tests isolate), `COBIRB_MODEL_NAME`,
`COBIRB_OLLAMA_URL`, `COBIRB_PROJECT_DIR`, `COBIRB_TEST_MODEL`.

## 11. Testing

`pytest` runs the suite. Coverage sits around 97%, which is **higher than the bar and not a target
to defend**: the aim is roughly 90%, and above all a suite that tests contracts rather than
internals.

The concrete test: if a change preserves a function's contract and behaviour, its tests should not
need to change. A suite that has to be hand-held through every refactor is the failure mode — and
this one has been there. Symptoms to watch for and fix in the test, not the source: asserting on
whole recorded-call dicts, on exact log strings, on private helpers, or on call sequences when the
observable result is what matters. Loosening or deleting an over-specified test is a legitimate
outcome, and a small coverage drop is a fine price for a suite that stops obstructing change.

Where the doubles live: `conftest.py` holds the three that were genuinely shared
(`DummyModel`, `StubSession`, `StubSessionManager`). The rest are local to the file whose questions
they are shaped around, deliberately.

## 12. Roadmap

- **v0.1.0 (now)** — core runtime, tools, permissions, encrypted sessions, personas, CLI + TUI.
- **v0.2.0 (done)** — context compaction, project instructions, `.gitignore` awareness,
  diff-before-write, checkpoint + `/undo`, headless/CI mode, actionable tool-failure messages.
  *Parallel read-only tools was dropped from the milestone — see §12.1.*
- **v0.3.0 (done)** — `repo_map`, `/diff`, self-verification loop, secret redaction. Write-scope
  grants and session export landed early in 0.2. *Git auto-commit deferred — see §12.1.*
- **v0.4.0 "Extensible" (done)** — per-role model selection (§4h), hooks (§4i), custom commands
  (§4j), MCP client over stdio (§4k). *The embedded GGUF runtime was cut from this milestone and
  from the project — see §12.1.*
- **v0.5.0 "The Flock" (done)** — subagent orchestration: charter → scaffold → fan-out →
  review → report, with a Flock tab showing a pane per Worker Birb. See §4m.
- **v0.5.1 "Field notes" (done)** — everything the first real Flock run turned up. Collected here
  rather than deferred: these were found in the way of the work, and a list of them aging in a
  later milestone is how a tool stays annoying.
  - ✅ **A 404 from the model endpoint is reported as "Is Ollama running?"** `urllib.error.HTTPError`
    is a *subclass* of `URLError`, so one `except URLError` catches both "nothing is listening" and
    "the server answered, and the answer was no", and reports them identically. Ollama's own
    explanation is read off the wire and discarded. *A transport failure and a rejected request are
    different questions and must not share a message.*
  - ✅ **An untagged model name silently means `:latest`.** Found in the field and worth writing down
    because it cost a whole run: a config naming `ornith-1.5` against an endpoint holding
    `ornith-1.5:9b` 404s, because Ollama resolves a bare name to `ornith-1.5:latest`. The
    orchestrator model happened to be `:latest` and worked, so only the Worker Birbs failed, which
    made it read as a Flock bug. CoBirb should say *"the endpoint has ornith-1.5:9b — did you mean
    that?"* rather than leaving someone to compare `ollama list` by eye.
  - ✅ **Check the models exist before planning, not after.** A flock is the first thing using two
    model roles at once, so a bad worker model produces one identical failure per Worker Birb,
    minutes after approval, with the skeleton already built. One `/v1/models` call up front turns
    that into a sentence. ⚠️ A tag-insensitive comparison here would *miss* the case above — the
    check has to know that a bare name means `:latest`.
  - ✅ **The charter approval dialog covers the charter.** It is centred over the transcript the
    charter was just printed into, so the one question in the whole run that requires reading
    something is asked with that something hidden. The charter belongs *inside* the dialog.
  - ✅ **Multi-line prompt input.** The box is one line and scrolls sideways for ever. It should wrap,
    grow to at most 8 lines, then scroll vertically. Not cosmetic — a flock objective is a
    paragraph, and composing one in a horizontally-scrolling single line is genuinely hard.
    Decided: **enter submits, shift+enter inserts a newline** — with a caveat, see §12.1.
  - ✅ **Flock tab scrolls horizontally.** Panes are `1fr` each, so three agents already
    squeeze the text past readability. They should keep a minimum readable width and the row should
    scroll left/right instead of shrinking. A pane too narrow to read is the same as no pane.
  - ✅ **Flock pane text is selectable.** The worker panes use a plain `RichLog`, which cannot be
    selected or copied out of at all; the main transcript uses `TranscriptLog` precisely because of
    that. The panes should use the same widget — a report you cannot copy out of is a report you
    have to retype.
  - ✅ **A starter config is written on first run.** `~/.cobirb/` and a `config.json` seeded from
    `config.json.example` should exist after installing, rather than the user finding an empty
    directory and having to know what goes in it. Must never overwrite an existing file.
  - ✅ **A rule closes each prompt.** Marker colour alone is not enough to find where
    one ends and the next begins when scrolling back. A rule at ~80% width between exchanges.
  - ✅ **An activity line shows what is running.** Brainy Birb builds a whole skeleton with no sign of life
    beyond Ollama's own terminal scrolling. The status bar's spinner is not enough and did not
    always appear; the bottom of the screen should carry a live line of what is currently running —
    which agent, which phase — for the Flock and for ordinary turns.
- **v0.6.0 "Interactive" (done)** — the three items that make a running session less of a
  one-shot commitment.
  - ✅ **Plugin distribution.** `cobirb plugin install/list/remove` (§5.5) — turns a plugin's source
    directory on disk into something the loader actually discovers, without CoBirb ever reaching
    out to fetch or resolve one itself.
  - ✅ **Mid-turn steering.** `Orchestrator.steer()` (§9) — a message sent while a turn is running
    redirects it instead of queuing a second one, cutting off an in-progress stream immediately
    where the model supports it.
  - ✅ **Session branching.** `session.fork_session()` (§9) — fork a conversation into a new,
    independent file and try a different direction from there, without disturbing the original.
    Distinct from the Flock's already-shipped GUID-based worker branching (§4m).
- **v0.7.0 "Hardened" (next)** — SPI freeze and versioning, a real session-schema migration path, a
  security review of the crypto and the plugin-install path, and documentation someone can start
  from cold. The last release before 1.0, and now the *only* one: what used to be v0.7.0 "Aware"
  no longer exists, so this moved up from 0.8–0.9 rather than leaving a gap in the numbering.
- **v1.0.0** — judged by whether someone other than the author adopts it.
- **v1.1.0 "Projects"** — a project is a password-encrypted batch of the sessions *and* memories
  belonging to one piece of work, with **no bleed-over between projects**. **Cross-session memory
  is part of this release, not an earlier one** (§12.2): it arrives inside the password boundary,
  designed once, rather than shipping loose and unencrypted first and being restructured here.
  Automatic distillation exists only within a project, alongside the explicit path.
- **~v2.0.0 — Vision input.** Deliberately late. See §12.2 for why, and for the storage shape it
  is expected to take once Projects exists.

**Time horizon.** CoBirb is built for the hardware of the next few years, not this one — local
models on ordinary machines will be considerably more capable in one to three years than they are
now. So **performance, memory and model-size figures are observations, never arguments.** Noting
that something is slow or large today is useful; letting that quietly pick a default, narrow a
feature, or rule an approach out is not. When a hardware consideration looks like it should change
the design, raise it with the user as a decision rather than resolving it in an estimate.
Designing to this year's ceiling is how a tool arrives obsolete.

### 12.1 Deferred, needing a decision

**Multi-line prompt input: what up/down do.** *(Settled and shipped — kept for the reasoning.)* `PromptInput` extends `Input`, and its
docstring says why that worked: *"``Input`` is single-line, so it binds neither arrow key itself and
both are free to mean 'walk the history'."* Going multi-line takes that back, and takes `enter`
with it.

`PromptInput` extended `Input`, and its docstring said why that worked: *"``Input`` is
single-line, so it binds neither arrow key itself and both are free to mean 'walk the history'."*
Going multi-line took that back. The shell convention resolved it without a new key: recall only
when the cursor is already on the first (or last) line, otherwise move. Enter submits;
shift+enter, alt+enter and ctrl+j all insert a newline, because many terminals send shift+enter
identically to enter and on those there would otherwise be no way to type a second line.

Also a rewrite rather than a tweak: `TextArea` has no `Submitted` event, so
`CoBirbApp.on_input_submitted` needs replacing with key handling, and Textual's `TextArea` does not
auto-grow, so the height has to be set from the wrapped line count on change and clamped at 8.

**Git auto-commit** — a commit per completed task, with a written message. Deferred because it
writes to someone's repository history, which is not a default to drift into: it needs decisions
about when to commit, what to do with pre-existing uncommitted work, and whether to touch the
user's branch at all. `/diff` covers the reviewing half without any of that.

**Parallel read-only tool calls** was on the 0.2 list and is not built. Executing several reads
concurrently is a performance change whose benefit here is unproven — the model, not local disk
I/O, is the bottleneck — while the cost is real: turn ordering, and `ShellTool` holding per-call
process state. Per §12's time-horizon rule that is a decision to take deliberately rather than one
for an estimate to make quietly, so it is parked rather than dropped.

**Settled decisions.** *Local models only, forever* — no shipped remote provider, ever (§2).
**CoBirb is a client and never a model runtime** — it speaks to an OpenAI-compatible endpoint and
does not run weights; see the GGUF entry below. The shell privilege gap is documented rather than
sandboxed (§8). Speech I/O is deliberately deferred past 1.0 — a large platform-specific
dependency for a workflow almost nobody uses on a coding agent. No embedding-based RAG: a repo map
plus grep beats it for code at a fraction of the machinery.

**Embedded GGUF runtime — designed, then cut. Do not reopen.** The motivation was real: CoBirb's
guarantees stop at the model socket (§8), and Ollama binds an unauthenticated local port, fetches
from a registry and makes its own outbound requests. Two shapes were costed. *In-process*
(`llama-cpp-python`) means a compiled dependency whose GPU support needs a CUDA-version-specific
wheel index or a compiler, plus reimplementing chat templating, GBNF grammar construction and
tool-call parsing inside CoBirb — and its own server component is semi-deprecated, with upstream
pointing at `llama-server`. *Supervised subprocess* was the better of the two and is worth writing
down, because the finding survives the decision: **`llama-server --host <path>.sock` binds a Unix
domain socket, not a TCP port**, and `--jinja` (on by default) makes it do templates, grammars and
tool-call parsing itself. A `0600` socket in a directory CoBirb owns is a stronger boundary than
`--api-key`, and a CoBirb-spawned child has none of the four daemon properties that made Ollama
uncomfortable.

It was cut anyway, and correctly: **CoBirb is the interface that speaks to an OpenAI-compatible
endpoint, and a user can point it at one they wrote themselves.** The trust problem belongs to the
endpoint, and solving it there — as a separate privacy-first Ollama replacement, perhaps a future
CoBirb-family project — fixes it once for everything that speaks the protocol instead of once for
one client. Running weights is not what this tool is for. When the flock arrives, workers get an
endpoint like everything else; there is no "just for subagents" exception.

**Omitted forever:** cloud sessions, remote control, background agents, telemetry. They contradict
the founding principle. (Subagents are not background agents — they are local, in-process, and
bounded by a turn you asked for.)

### 12.2 Memory, Projects and vision: decisions on record

Settled by the user against the design-proposals artifact. Written here rather than left in an
artifact because this is where the next person looks — and because all of it is some form of "do
not build this yet", which is exactly the kind of instruction that gets accidentally reopened. The
whole of what was once v0.7.0 "Aware" now lives at v1.1.0 or later.

**Cross-session memory is not built until Projects (v1.1.0). Do not build it before.** The
intermediate design — memory shipping loose, unencrypted, opt-in, under `~/.cobirb/memory/` — was
considered in full and rejected: it would be built once now and then restructured at 1.1 anyway,
when a project's password becomes the boundary it belongs inside. Memory arrives *with* Projects,
designed once, or not at all. What survives from that design and still holds when it is built:

- **Facts, not transcripts.** What was learned about the codebase, standalone and true independent
  of the conversation that produced it. Sessions already hold history; memory is for what outlives
  one of them.
- **Never in the repository.** The same argument as config (§4l): memory is read straight into the
  system prompt, so a repo-writable memory is a stored prompt injection with no approval step in
  front of it. A project's own store, under the user's control, is where it lives.
- **Explicit by default, automatic only inside a project.** `/remember <fact>` and a `remember`
  tool under the ordinary approval gate are the baseline. The model proposing entries of its own at
  the end of a run exists only within a project — containment: a confidently wrong invented fact is
  worse than no fact, and inside a project its blast radius is that project rather than everything
  CoBirb believes about every codebase.
- **Not injected into a Worker Birb's run.** "Nothing but its brief" is load-bearing for the
  Flock's knowledge-isolation argument (§4m). Memory reaches Brainy Birb, which already reads
  `AGENTS.md`, and stops there.

**Vision is deferred to ~v2.0.0 — do not build it.** No `images` parameter on `chat()`, no
`Turn.images`, no `/image`, and `supports_vision()` stays the constant `False` it is today. Not
taking that SPI change now is also what frees the v0.7.0 SPI freeze from having to be sequenced
around it. When vision does land, and assuming Projects exists by then, the storage shape is
already decided: **images are encrypted files under the project's own password, and the session
stores a short descriptive "alt-text" rather than the image itself** — so a run can work out which
image the user means from context and that alt-text, without decrypting every image in the project
just to start a turn.
