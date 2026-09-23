# AGENTS.md — CoBirb

Working reference for agents editing this repository. Derived from the source and tests.

## 1. What this is

CoBirb is a **privacy-first agentic coding CLI**: the capabilities people get from cloud-hosted
coding assistants, against models running on the user's own machine, with no telemetry and no
outbound network unless the user explicitly asks for it.

- Version: `pyproject.toml` (the one place it is written; `cobirb.__version__` reads the installed
  metadata). Python ≥3.11, MIT. Entry point `cobirb = cobirb.cli:main`.
- Runtime deps: `rich` (rendering), `cryptography` (session cipher), `textual` (interactive app,
  imported lazily so one-shot never loads it). Dev extras: `pytest`, `pytest-cov`, `pytest-asyncio`.
- Ships **no models**. The core model provider speaks to a local Ollama-compatible endpoint
  (`http://localhost:11434` by default) and only once a model is named.

## 2. Invariants — do not violate

1. **Default-deny — with one contained exception.** A fresh `Policy` permits nothing. Capability
   comes from an approval prompt, `allow_tools`/`allow_read_dirs`/`allow_write_dirs` in config,
   `--allow-tool`, or auto-pilot (below). There is deliberately no "approve everything" flag. The
   exception (0.36.0, decision D2): a shell command **inside the sandbox** runs without asking by
   default, because it can reach no network and write nothing outside the project — and only where
   whole-tree checkpoints can undo what it changed in the project (§5). Anything the sandbox does
   not contain still asks.
2. **One config file: `~/.cobirb/config.json`.** No repo-local config is read, merged, or looked
   for. A repository may *describe* itself (`AGENTS.md`, repo map, `<project>/.cobirb/commands/`)
   but may never grant capability.
3. **No outbound network by default.** Only the model provider talks to a socket (a local endpoint
   the user configured), plus two explicitly-invoked exceptions: `cobirb --upgrade` and MCP servers
   the user configured (local subprocesses). What `--upgrade` reaches depends on the install shape
   it finds (§14, `runtime/upgrade.py`): a git remote for a checkout, or GitHub's release assets
   plus PyPI for the dependencies when it delegates to `install.sh` on a managed install. The
   install shape decides *which host*, never *whether* — it is still only ever a command somebody
   typed, which is the whole of the exception.
4. **Sessions are encrypted at rest.** Plaintext conversation exists in RAM only.
5. **Fail closed.** No I/O adapter, an adapter that cannot ask, an adapter that raises, an
   unparseable shell command, a plugin that will not load → deny/skip, never proceed.
6. **Never crash on a recoverable fault.** Broken plugin → reported and skipped. Tool raises →
   failed `ToolResult` the model can correct from. Broken config → reported, defaults used. The
   broad `except Exception` blocks are deliberate; each carries a comment saying why.
7. **The model's own system prompt wins.** With nothing to add, CoBirb sends *no* system message,
   leaving the Modelfile `SYSTEM` in force. When it must add something, the model's own goes first.
8. **Every version bump gets a matching `vX.Y.Z` git tag** — `cobirb --upgrade` resolves releases
   only from tags, so an untagged bump is unreachable.

## 2b. The hardware this targets

**Assume at least 16 GB of VRAM and that a 128k context window is fine.** Users have capable
machines or rent GPUs; this is explicitly not built for low- or mid-end consumer hardware. Running
two models at once, or several subagents concurrently, is reasonable to expect.

Written down because getting it wrong was expensive: compaction was once built around a
4096-token window (Ollama's *default*, mistaken for what a local model gets), and that one
assumption then sized the instructions budget, the repo map, and the decision not to inject a map
at all — four wrong calls, none of which looked wrong from inside. **Performance, memory and
model-size figures are observations, never arguments.** Note that something is slow or large; do
not let it quietly pick a default, narrow a feature or rule an approach out. Raise it as a decision.

## 3. Layout

| Path | Responsibility |
|---|---|
| `cli.py` | argparse, mode dispatch, one-shot run. No wiring logic. |
| `orchestrator.py` | The agent loop; model ↔ tools, policy-gated. No feature logic. |
| `policy.py` | Permissions, shell-command scanning, audit log. |
| `session.py` | `Turn`/`Session`, encrypted `SessionManager`, schema migration, `fork_session`. |
| `context.py` | Fitting history into the model's window. |
| `memory.py` | Memory-catalogue *files*: read, write, encrypt, rename, delete. |
| `checkpoints.py` | Whole-tree snapshots per turn (`TreeCheckpoints`, git) behind `/undo` and `/diff`; per-file snapshots (`Checkpoints`) without git and for Worker Birbs. |
| `redaction.py` | Credential stripping for tool output and audit args. |
| `config.py` / `paths.py` | The single config file; every `~/.cobirb` path derived in one place. |
| `typing/spi.py` | **The plugin contract.** All SPI interfaces + shared dataclasses. |
| `plugins/loader.py` | Discovery (entry points + local dirs), fail-closed. |
| `plugins/core/` | Built-ins: `tools`, `model` (Ollama), `openai` (OpenAI-compatible servers, §9), `toolcalls` (calls written as text), `io`, `crypto`, `render`, `repomap`, `ignores`. |
| `runtime/` | Composition layer both front-ends share: `wiring`, `plugins`, `models`, `system_prompt` (CoBirb's own system-prompt block, empty by default), `commands`, `command_index` (what the `/` picker lists and how it ranks), `sessions`, `instructions`, `hooks`, `verify`, `custom_commands`, `headless`, `export`, `bootstrap`, `plugin_install`, `upgrade`, `catalogues` (which catalogues a session has open — see §6b), `mentions` (`@path` ranking + expansion), `doctor` (the readiness checks). |
| `mcp/` | stdio MCP client (`client`) and its tool adapter (`tools`). |
| `flock/` | Multi-agent runs: `charter`, `plan` (the incremental builder), `brainy`, `worker`, `supervisor`, `review`, `run`, `branch`, `probe`, `preflight`. |
| `tui/` | Textual app: `app` (the application itself — mount, input, the turn, workers, actions), `slash_commands` (what each `/command` does, as `(app, argument)` functions + the `COMMANDS` table), `transcript` (everything written to the transcript, and the flush-before-write ordering rule), `attachments` (images queued by `/image` for the next message), `mention_picker` (the five-row `@path` list), `command_picker` (the five-row `/command` list), `widgets`, `screens`, `panes`, `io_bridge`, `flock_bridge`, `app.tcss`. |
| `help_text.py` | The prose `cobirb help [topic]` prints. |
| `install.sh` | The installer, shipped inside the package. Also the upgrader and downgrader — see §14. |
| `tests/` | One file per module; `conftest.py` isolates `COBIRB_HOME` for every test. |

## 4. The loop

`Orchestrator` wires five pluggable pieces — model provider, tool registry, policy, I/O adapter,
session manager — and contains no feature business logic.

```
prompt → model call → tool calls (policy-gated) → results into history → repeat
       → plain reply with no tool calls == final answer (becomes session.summary)
```

- **Stopped by lack of progress, not by a turn count.** `DEFAULT_MAX_TURNS = 40` (config `max_turns`)
  is a backstop; what ends an unproductive run is `_loop`'s brakes: the same call with the same
  arguments back to back gets a note appended to its result (`_REPEAT_NOTE_AT = 2`) and ends the run
  at `_REPEAT_STOP_AT = 4`, and `_FAILURE_STOP_AT = 6` failed-or-denied calls in a row end it too —
  both as `STOP_NO_PROGRESS`. The cap was a flat 8, which an ordinary read-edit-test-fix task spends
  before it is half done. The Flock's planning loop treats `STOP_NO_PROGRESS` like an exhausted
  budget, since another pass would buy more of the same repetition. Worker Birbs get 30 (was 12).
  Validate phase 4. **How a run ended is `Orchestrator.last_stop`**
  (`RunStop`: `STOP_ANSWERED` or `STOP_TURN_LIMIT`), never text in the summary. Running out of turns
  used to return a made-up reply — "Stopped after N turns without a final answer." — which was
  stored as the model's answer and read by every consumer as a conclusion (headless exited 0 on it).
  Now the summary stays empty, the orchestrator reports the stop through the I/O adapter's
  `render_notice`, neither front-end prints "Completed." under it, and headless reports
  `stop_reason` and exits 1.
- Each iteration: drain steering queue → build context → model call → either record tool calls and
  execute, or return the final answer.
- **Plan mode** (`--plan-mode on`, `/plan on`, `plan_mode` config; off by default) is a planning
  pass, then the work (0.37.0, decision D9). The pass is a bounded `_loop` (`_PLAN_MAX_TURNS = 12`)
  offered only `READ_TOOLS` and `todo`, so the plan is made *after looking at the code* — it was a
  single reply with `tools=[]`, a plan for code the model had not been allowed to see — and a call
  naming a tool the phase did not offer is refused (`_execute_tool_calls(offered=...)`), because a
  native call can name anything. The third "validate" phase was removed: the working-method prompt
  and `verify_command` cover it. `Turn.phase` is `plan|act` for new turns; `validate` turns and
  `Session.validation` in old sessions still load. `phase` is descriptive only — it never changes
  context replay, but it *is* covered by the turn hash.
- **Verification** (`_verify_and_fix`) runs the user's `verify_command` after a turn that changed
  files, feeds a failure back as a user turn, and allows one bounded fix attempt.
- **Mid-turn steering** (`steer()`, thread-safe): queues a message applied as a user turn at the
  next loop boundary, and — if the provider implements `interrupt_current_reply()` — cuts the
  in-flight stream via `SteeringInterrupted`. The partial reply is kept as a real turn. Distinct
  from `cancel()`, which is the one-way stop.
- **Optional, duck-typed hooks the loop probes for** (never part of the ABC): on the model,
  `context_window()`, `cancel()`, `interrupt_current_reply()`, `malformed_tool_call()` (§9); on tools, `writes()`, `preview()`,
  `cancel_running()`; on the I/O adapter, `spinner`, `begin_stream`, `confirm_scoped`,
  `render_answer`, `render_plan`, `render_validation`, `render_tool_call`, `render_notice`,
  `write_error`. `render_through()` is the shared probe-with-fallback helper.

## 5. Permissions (`policy.py`)

- `READ_TOOLS = {read_file, list_dir, glob, grep, repo_map}` and
  `WRITE_TOOLS = {write_file, edit_file, apply_patch}` are each scoped **by directory**, in two
  separate sets that never imply one another. `shell` is in neither: it cannot say what it touches.
- Paths resolve with `realpath` against the policy's `cwd`, so `..`/symlinks cannot escape a grant;
  `_within` requires a separator so `/x/project-secrets` never matches a grant for `/x/project`.
- **`Policy._resolve` and `CobirbTool._resolve` must mirror each other step for step** — `~`
  expansion included. The policy's job is to say where a call will land *before* it is approved,
  so any difference between the two is a permission check performed on a path the tool will not
  use. They were once out of step on `~`: neither expanded it, so `~/notes/x.md` was gated, and
  then written, as a directory literally named `~` under the cwd.
- `shell` is checked **per command segment** (`git status; rm -rf /` is two commands, both must
  pass). Allow rules are either a bare binary (any arguments) or an exact multi-word prefix.
- Refused outright as unverifiable: backticks, `$(...)`, subshells, unbalanced quotes, and
  `find -exec/-execdir/-ok/-okdir`.
- `grant()` decides what "always" widens to: a read → its directory, a write → its directory, a
  shell call → that invocation, anything else (plugin/MCP tools) → the tool name.
  `describe_grant()` produces the sentence the prompt shows before the user agrees.
- `SessionGrants` is the same widening applied to **every agent in the session** rather than to the
  one policy that was asked about — the `"session"` decision. It exists because a Worker Birb's
  policy is built per ticket and dies with it, so `"always"` answered in a flock is re-asked on the
  next ticket and the next round. Every policy built in the session registers and receives the
  backlog, so a worker that starts *after* an approval is already widened by it. **A session grant
  is a session grant:** it records nothing about who asked, because a permission that means
  different things depending on its origin is one nobody can reason about — so a grant made in the
  main conversation reaches workers, and one made by a worker reaches the main conversation.
  Memory only, never written to config (a permission surviving a restart is the user's decision to
  make in their own file), and policies are held weakly so finished workers do not accumulate.
- **"Always" on a read inside the project grants the whole project** (`path_scope`, decision D5),
  not the file's directory: one question per project rather than one per subdirectory, which had
  taught people to stop reading the prompt. `_project_root` refuses a working directory that is `/`
  or the home directory, where "the project" would mean everything. Writes keep the directory scope.
- **Auto-pilot** (`Orchestrator.enable_autopilot`, `/autopilot`, `--autopilot` with `-p`): reads and
  writes inside the project (`Policy.autopilot_root`) and contained shell commands run unasked;
  **everything else is refused without asking** (`_autopilot_refusal`, a reason the model can act
  on) — unsandboxed commands, writes outside the project, MCP and plugin tools. It refuses to start
  without an active sandbox *and* whole-tree checkpoints, or in a too-broad project, because it is
  exactly as safe as what contains it. Combined with the progress brakes (§4) it is the recommended
  way to run long work unattended.
- **Shell commands run in a sandbox** (`cobirb/sandbox.py`, bubblewrap): the filesystem read-only
  except the project and a private `/tmp`, no network namespace, own PID/IPC/UTS namespaces, and
  credential paths hidden (`DEFAULT_HIDDEN`, resolved to real paths because on WSL `~/.aws` is a
  symlink into the Windows drive and bubblewrap resolves links inside the new root). Modes:
  `"auto"` (default since 0.36.0 — contained and not asked, via `Policy.sandbox_auto`, set by the
  wiring for the main agent only, so a Worker Birb's shell stays the charter's to grant; a
  *default* auto only takes effect where whole-tree checkpoints exist, `Sandbox.explicit`
  distinguishing the user's own choice), `"ask"` (contained and still asked) and `"off"`. In `auto` even a command the segment scan
  cannot read is allowed, because containment rather than the scan is the guarantee; `unsandboxed:
  true` runs outside and always goes through the normal policy. `find_bwrap` *probes* bubblewrap
  once with the real isolation flags, since installed-but-no-user-namespaces would fail every
  command. Without it everything behaves as before and `doctor` says so. This reverses §17's old
  "no sandbox" entry: a network-less sandbox is what makes "an approved command cannot exfiltrate"
  enforced rather than hoped for.
- `AuditLog` is **off** unless `"audit_log": true`. It stores arguments verbatim (file contents,
  diffs, commands), so it is written 0600, credential-redacted, and never created until enabled.

## 6. Sessions, crypto, undo

- `SCHEMA_VERSION = 1`. `Session.from_dict` is the only read path and always calls `migrate()`.
  A **future** schema is refused, not best-effort read. Every field added since schema 1 was
  optional, which is why `_MIGRATIONS` is empty — add optional fields, not migrations, unless a
  change makes an old payload *wrong*. `tests/test_session.py::test_every_schema_step_has_a_migration`
  fails the build if `SCHEMA_VERSION` rises without one.
- `Turn.digest()` hashes role + content + `tool_use` + `phase`; `SessionManager.load` verifies every
  stored hash and raises on mismatch.
- Crypto: AES-256-GCM over a key from scrypt `N=2**17, r=8, p=1`. Blob = `b"cobirb1"` + base64 JSON
  header naming the KDF parameters + `\n` + base64(`salt||nonce||ciphertext`). Headerless blobs are
  pre-header files, read at `N=2**14`, rewritten in the new format on next save. No PQ KEM: a
  password-encrypted local file has no key exchange to protect.
- Session files and the audit log are created `0600` via `os.open` (no chmod window).
- `fork_session()` only ever reads the source; a truncated branch drops `summary`/`validation`,
  keeps `flock`, and records `forked_from = "<path>@turn<N>"`.
- **`TreeCheckpoints` snapshots the whole tree before and after every turn** (decision D6), so `/undo`
  and `/diff` cover what a *shell command* did — the old honest gap. The store is a separate git
  directory with the project as work tree: the project need not be a repository, and if it is, its
  `.git` is never read or written (a test checks `git status` and `HEAD` are unchanged) and its
  `.gitignore` holds. `ALWAYS_IGNORED` plus `.git` go in the store's `info/exclude`. The user's git
  configuration is kept out (`core.hooksPath=/dev/null`, no signing, fixed identity). The store is
  `0700`, lives for the session (`Orchestrator.close` → `close()`), and a store whose process died
  is swept (`_sweep_dead_stores`). **Undo restores only files the last turn changed and only if
  still as that turn left them** — your own later edits are left and reported. `for_workspace` picks
  it when `git` is installed. Worker Birbs keep the per-file `Checkpoints` (`writes()`-declared
  paths, `DEFAULT_KEEP_TURNS = 20`): several run concurrently in one tree, and a whole-tree
  snapshot by one would capture its colleagues' half-finished work.

## 6b. Memory catalogues (`memory.py`)

Named lists of facts fed into the system prompt, each one file under `paths.memories_dir()`:
`<name>.md` (plaintext) or `<name>.md.enc` (password-protected, the same crypto backend
sessions use — see §6). `public.md` always exists and is created lazily on first use; anything
else is created explicitly through `/memories`. A catalogue's `.md` body is a flat `- fact` bullet
list, continuation lines indented two spaces — no metadata, no timestamps, no source, because this
text is read straight into a system prompt and should read like a list a person would actually
write. Both kinds are created `0600` via `os.open`, never `open()` + `chmod`: a public catalogue is
plaintext *and* feeds the system prompt, so a umask-width window in which another local account can
write to it is a window in which they can put words in the model's mouth.

`/memories` (TUI) loads, unloads, renames, deletes, or creates catalogues; `/remember <fact>` saves
a fact into one, prompting for its password there if it's locked. Both are **plain slash commands,
not a model tool** — a tool would either be unreachable for a model without tool-calling support, or
get called on every turn with no memory of already having asked, so this fires exactly once, exactly
when typed, regardless of what the model can do.

Which catalogues are open is `runtime.catalogues.CatalogueStore` (`app.catalogues`), not the app:
none of that bookkeeping touches a widget. Session-lifetime only, never auto-loaded just because
they exist on disk. Its `system_prompt()` is composed in fresh on every `Orchestrator.run()` call
rather than baked into the orchestrator at construction —
the same staleness that killed the old header panel would hit a memory block computed once and
never revisited. **Never reaches a Worker Birb**: `build_subagent` passes `project_context=""` for
the same "nothing but its brief" reason it withholds `AGENTS.md` and the repo map.

## 6c. Vision — attached images (`session.py`, `orchestrator.py`, `plugins/core/model.py`)

`/image <path>` (TUI) queues a file onto the *next* submitted prompt — the message typed and sent
next is the caption, when there is one, the same as attaching a file anywhere else.
`/image <path> <message>` does both at once and sends immediately: path and question on one line is
what people reach for, and taking the whole argument as a filename failed with "no such file",
blaming the file for a parsing rule. A quoted path wins; an unquoted argument that names an
existing file is taken whole, so paths with spaces still work (`_split_image_argument`). Relative
paths resolve against the session's `cwd`, not the process's.

**The bytes live in `Session.images` (`{id: base64}`, keyed by content hash), inside the same
encrypted blob as everything else**; `Turn.images` holds only `[{"id", "filename"}]` referencing it.
That placement is the design, not an implementation detail. They were briefly separate
AES-GCM files in a `<session>.images/` sibling directory, and that could not work: neither
`SessionManager` nor `Orchestrator` retains a password (both by deliberate design), so the layer
that assembles the model's context could never decrypt them — a resumed session could only ever
show a `[image: x.png]` marker where the image had been, and nothing ever called `read_image` at
all. Living in the session payload, images are already decrypted when `load()` returns, under the
same password and cipher as every other field. **Do not move them back out.**

`Orchestrator._build_context` resolves every image-bearing turn against that table, not just the
newest — an attachment is part of the conversation the way its text is, so a resumed session shows
the model the image again. An id with no bytes behind it (hand-edited session, a branch taken
before the attachment) degrades to a `[image: filename]` marker rather than failing the turn.
`fork_session` carries exactly the images its kept turns reference. `context.py` prices an image at
a flat `_IMAGE_TOKENS = 1500` (**never** `estimate_tokens(base64)` — a 1 MB screenshot would price
at ~350 000 tokens and stampede compaction) and `_elide_image` drops old ones to a marker in
compaction pass 1, so recent images — the one you are actually discussing — always survive.

`_build_messages` (`plugins/core/model.py`) attaches data to Ollama's native `images` field only
when `supports_vision()` is true — read off the same cached `/api/show` payload `context_window()`
already uses, checking `capabilities` for `"vision"`, since most local models cannot see images at
all and CoBirb has to ask rather than assume. `/export` shows a `📎 filename` marker, never bytes.

## 7. Context management (`context.py`)

`DEFAULT_CONTEXT_TOKENS = 32768` is the floor used only when the provider cannot say; the provider
states `num_ctx` on every request, so the window is asked for rather than guessed. `history_budget`
reserves 20 % clamped to 2048–16384 tokens. Estimation is `len(text) // 4`.

`LocalModelProvider.context_window()` (`plugins/core/model.py`) resolves what to ask for — the
Modelfile's own `num_ctx` if set, otherwise the architecture's advertised max — and then clamps it
against `max_num_ctx` (config key, wired through `runtime/models.build_for_role` to every role) if
one is set. `models.parse_context_size` reads it as either a number or the way people say them —
`"64k"` is 65536, a `k` being 1024 — and returns `None` for anything unparseable, so a typo costs
the cap rather than the run; `cobirb doctor` reports that case rather than leaving it silent. That ceiling only ever lowers the request: a model asking for less than the ceiling is
untouched, and an endpoint with no `max_num_ctx` configured behaves exactly as before.
`Orchestrator._context_budget` reads the same hook, so capping the window also caps what history is
packed against it rather than leaving CoBirb filling a window the server was never asked for. It exists
because an advertised architecture max (Qwen2's is 262144) sized as KV cache can exceed a card's
VRAM on its own, well before weights and everything else sharing the card are counted — see
`context_tokens` above, which is a different knob (history budget, not the wire `num_ctx`) and does
not cap this.

`/clear` (`session.turns_since_clear`) sets where the conversation is read *from*. It appends a
turn with `role == "clear"` and nothing else: no deletion, no schema bump, no migration — a role
rather than a new `Turn` field because `Turn.digest()` is compared against a hash written by
whichever CoBirb saved the file, so extending that formula would fail every older session. Two
callers share the one helper and must not drift: `Orchestrator._build_context` (what the model is
sent) and `tui/transcript.render_history` (what a resumed session redraws). A resumed session
showing turns the model cannot see, or hiding ones it can, is worse than not having the feature.
Everything before the marker stays in the file, so the session remains a complete record and
`/export` still writes all of it.

`compact()` runs four passes, in increasing order of loss, and short sessions return unchanged:
1. Elide old tool results outside the last `_KEEP_RECENT = 6` turns (≥400 chars; keeps the call, drops the body).
2. Drop a contiguous run of oldest turns after turn 0, never starting the remainder on an orphaned tool result, leaving a note — **with a model-written summary of what was dropped** when the caller offers a `summarise` callback (`Orchestrator._summarise_dropped`: no tools offered, material trimmed to fit, cached by how many turns it covers so a later compaction summarises "summary so far + the newly dropped turns" — one call per growth of the prefix — and the plain note if it fails or returns nothing; capped at `_SUMMARY_MAX_CHARS`).
3. Elide inside the working set if it is itself over budget.
4. Trim the largest bodies repeatedly (bounded at 64 iterations) — the pass that guarantees it fits.

## 8. Tools (`plugins/core/tools.py`)

Built-ins: `read_file`, `write_file`, `edit_file`, `apply_patch`, `glob`, `grep`, `list_dir`,
`repo_map`, `shell`, `todo`. **`todo` is a checklist the model keeps** — the whole list each call
(one shape, easy for small models), progress on the TUI status bar — reaching nothing, so permitted
outright in both wirings like the charter tools. Subclasses of `CobirbTool` declare `NAME: ClassVar[str]` and inherit `name()`
— the SPI declares `name` as a **method**, and `ToolRegistry.register` rejects anything else.

**`edit_file` changes exactly one region or refuses** (`_plan_edit`). It used to replace the first
match and report success, so in a file of near-identical functions it edited a different function
than the one meant and the model then told the user it had done what was asked — the first real
defect cobirb-bench found. Now: several matches are refused with every line number (`replace_all`
opts in); a miss tries a unique whitespace-tolerant whole-line match (trailing space, CRLF, and an
indent *missing* uniformly from `old_str`, which is re-added to `new_str`); a true miss quotes the
closest region with line numbers. Success reports the edited lines, so the model sees what it did.

**`apply_patch` reads the `*** Begin Patch` format too** (`cobirb/patches.py`), which gpt-oss writes
in place of a unified diff — in the baseline every such patch failed as "no valid hunks". Hunks are
placed by context (an `@@` anchor narrows the search; an unanchored block matching twice is refused,
as in `edit_file`), and a unified diff with bare `@@` and no line numbers takes the same path. **The
file is named inside the patch, so the policy reads it from there** (`policy.patch_target`, shared
with the tool, so what is checked is what is written). Only single-file Update/Add sections are
accepted; several files, a move, a delete, or a `path` disagreeing with the header resolve to no
target and are denied — the one-path permission check cannot vouch for them.

**A write that leaves a file unparseable says so in its result** (`_syntax_note`): Python via
`compile`, JSON, TOML — in-process, nothing run. A note rather than a refusal, since a half-finished
multi-step change is legitimate; the point is that the model hears about it now rather than from a
test failure it has to rediscover. A per-edit `lint_command` was considered and not built: the
post-turn `verify_command` already runs the user's own check.

Every result is bounded: 256 KiB per read (paged — a short read says which lines it returned and
what offset continues), 500 grep matches, 300 chars per matching line, 1000 list/glob entries,
64 KiB of shell output (head **and** tail kept), 8 KiB of approval preview.

`shell` runs in its own process group (POSIX), default timeout 300 s clamped to 600 s, and exposes
`cancel_running()` so the TUI's Ctrl+C can unstick it. `glob`/`grep` honour `.gitignore` plus a
built-in vendor/cache list unless `include_ignored: true`.

**Each `shell` call is its own process, so a `cd` does not survive it** — and reported as a bare
`exit=0` that was indistinguishable from one that had. Two halves, both needed: an optional `cwd`
argument (resolved through `_resolve`, so it honours `--cwd` and `~`) gives the model a stateless
way to say where, and `_changes_directory_only()` appends a note to a line that is *only* `cd`s
saying it did not persist. That detector is advisory and deliberately fail-**open** — it decides
whether a result carries a note, never what may run — which is why it does not share an
implementation with `policy._segments`, which must fail closed. Making the cwd genuinely
persistent was not done: see §17's note on `ShellTool`'s per-call process state.

## 9. Model provider (`plugins/core/model.py`)

- `POST /api/chat` with a real role-tagged `messages` array rebuilt from the orchestrator's JSON
  turn history. Flattening history into one opaque message is what previously stopped the
  tool-calling loop converging — a tool result with no assistant call preceding it gives the model
  no signal the call was already satisfied.
- `GET /v1/models` (OpenAI-compatible, not `/api/tags`) for the model list, so llama.cpp/vLLM/LM
  Studio work unmodified. Called only by interactive mode.
- `/api/show` supplies the Modelfile `SYSTEM` and the context window (`num_ctx` parameter first,
  else advertised `context_length`), cached per model.
- `compose_system()`: empty in → empty out (no system message at all); model has none → send ours;
  both → model's first, ours appended.
- **Tool calls written as text are read** (`plugins/core/toolcalls.py`, via `parse_tool_calls`) when
  the structured `tool_calls` field is empty: Hermes `<tool_call>` JSON, Qwen3-coder `<function=…>`
  XML, leaked gpt-oss channel markup, and JSON that *is* the whole reply. This reverses an earlier
  "native field only" rule (decision D1): Qwen3-coder switches to XML in its content once offered
  more than ~5 tools, so the loop was taking calls for final answers. Guards against inventing calls:
  only names offered this turn count; explicit call syntax is read anywhere, plain JSON only when the
  surrounding prose is under `_BARE_JSON_SLACK`. Every call still goes through policy and approval.
  A call that is clearly attempted but unreadable — unknown tool, broken JSON, or **the server's own
  tool-call parser failing** (Ollama returns that as the reply with HTTP 200, as a streamed `error`
  line, or as a 500 body; ollama/ollama#18563) — is reported through the optional
  `malformed_tool_call()` hook, and `_loop` feeds it back as a user turn and counts it toward the
  failure brake. Assistant turns replay with the markup stripped, so a call is not sent twice.
- Streaming is NDJSON; `_last_tool_calls` is only accurate once the generator is exhausted. An
  `error` line mid-stream is raised, unless it is a tool-call parse failure (above).
  `cancel()` latches the provider closed; `interrupt_current_reply()` cuts one reply and leaves the
  provider usable — `_steer_signal` is cleared before every request so a late interrupt never
  reports the *next* request's failure as a steer.
- `supports_vision()` reads `capabilities` off the cached `/api/show` payload (§6c).
- **`OpenAICompatibleProvider` (`plugins/core/openai.py`) speaks `/v1/chat/completions`** for
  llama.cpp, LM Studio and vLLM, selected by `models.<role>.api = "openai"` (config, never probing;
  `runtime.models.model_api`). Before it existed those servers listed models over `/v1/models` and
  then failed every chat, because chat went to Ollama's `/api/chat`. It **subclasses** the Ollama
  provider so connection tracking, cancel, steering, the two timeouts and text tool calls are shared
  — `_stream_lines` is the protocol-independent half of streaming. Differences: tool-call ids are
  minted on replay (`call_<turn>_<k>`, echoed by the matching tool result), arguments are JSON
  strings and stream as index-keyed fragments, options are top-level request fields, there is no
  Modelfile `SYSTEM`, and **the window is read rather than requested** (llama.cpp `/props`
  `n_ctx`, else `/v1/models` `max_model_len`/`context_length`/`meta.n_ctx`), capped by
  `max_num_ctx`. Vision: `models.<role>.vision`, else llama.cpp's `modalities`.

## 10. Plugin SPI (`typing/spi.py`)

- `SPI_VERSION = 1`, `MIN_SUPPORTED_SPI_VERSION = 1`, **frozen**: within a version, changes are
  additive only (new *optional, duck-typed* hooks are fine; new abstract methods, renames and
  signature changes are not). A plugin declares `COBIRB_SPI = <int>`; absent means 1. A
  non-integer declaration is an error, not a shrug. Incompatible → `IncompatiblePlugin`, refused at
  the loader boundary, non-fatally.
- Interfaces: `Tool`, `ModelProvider`, `I_OAdapter`, `SessionCrypto`; data: `ToolCall`,
  `ToolResult`, `ApprovalRequest`, `ApprovalOutcome`, `Persona` (unused since personas were removed
  in 0.22.0; kept because the SPI is frozen and may carry them again as a plugin), `SteeringInterrupted`, and the
  `once`/`always`/`session`/`deny` decision constants. `confirm_scoped` and `confirm_request` are
  **optional duck-typed hooks**, probed with `getattr` and never on the ABC; `_request_approval`
  tries them richest first and normalises anything unrecognised to deny.
- Entry-point group `cobirb.plugins`. **Tools are additive** (every discovered one registers);
  **model/io/crypto are singleton slots** that only replace the core default when named in
  `plugins.<slot>` config. Local plugins live in `~/.cobirb/plugins/<name>/` and must be installed
  as a distribution named `cobirb_plugins_<name>` — that is what `cobirb plugin install` automates
  (local paths only; it never fetches).

## 11. Project grounding

- `runtime/instructions.py` reads the **first** of `AGENTS.md`, `CoBirb.md`, `COBIRB.md` in the
  working directory only — it deliberately does not walk up to a git root. Capped at 32000 chars,
  truncation announced.
- `plugins/core/repomap.py` builds a ranked outline (Python symbols via `ast`, regexes elsewhere;
  imports weight ranking, entry points boosted, tests pushed down), budgeted to 16000 chars. It is
  **both** injected into the session-start system prompt and exposed as the `repo_map` tool.
- Both compose into `Orchestrator.project_context`, which rides on every request's system prompt.

## 12. User extension points

| Mechanism | Shape | Notes |
|---|---|---|
| Hooks (`runtime/hooks.py`) | `before_tool`, `after_tool`, `before_turn`, `after_turn` | Event arrives as JSON on stdin; 30 s timeout. A non-zero `before_tool` exit **blocks** the call and its output becomes the model's reason. Others are observational; failures are surfaced, never fatal. |
| Timeouts (`plugins/core/model.py`) | `connect_timeout` (10), `request_timeout` (600) | **Two numbers because they answer different questions.** One 120s socket timeout did both and was wrong for both: a socket timeout measures *silence, not work*, and a request an endpoint has queued behind another generation sends nothing until it starts generating — so Worker Birbs died at 120s having never sent a prompt, reported as "Is Ollama running?" about a busy server. Split genuinely in `_stream_chat` (`_connect` builds on the short clock, `_open` swaps the socket to the long one) — the path every worker turn takes. `_post` cannot split (the socket is inside `urllib`) and takes the long one; a dead endpoint is caught on the short clock by `list_models`, which the startup check and `preflight.missing_models` use first. `runtime.models._timeout` drops an unreadable value rather than raising. |
| Verify (`runtime/verify.py`) | `"verify_command": "pytest -q"` | Off unless set — never guessed. Runs **outside** the permission layer by design (the user's own config, run by CoBirb, unchangeable by the model). 120 s timeout, 1 fix attempt. |
| Custom commands (`runtime/custom_commands.py`) | `~/.cobirb/commands/*.md`, `<project>/.cobirb/commands/*.md` | `/name` sends the body; `$ARGUMENTS` and `$1`…`$9` substitute, otherwise arguments are appended. Optional `---\ndescription: …\n---` frontmatter. |
| MCP (`mcp/`) | `mcp_servers` in config, **stdio only** | Tools register as `mcp__<server>__<tool>` and go through the same policy, prompt, audit and redaction path. Environment is **not** inherited (only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` + configured `env`, unless `inherit_env`). Not members of `READ_TOOLS` — "always" grants that one tool, for the session. |
| Per-role models (`runtime/models.py`) | `models.default` / `.orchestrator` / `.worker` | Roles inherit from `default` field by field; `options` (sampling, passed to the server verbatim) merges key by key, minus `num_ctx`, which `max_num_ctx` owns. Name resolution: `--model` → `models.<role>.name` → `models.default.name` → then the deprecated `model` / `default_model` (0.33.0: they used to outrank `models.default.name`; `doctor.DEPRECATED_KEYS` warns). `cobirb models` prints the result. |

## 13. The Flock (`flock/`)

**A charter is built a validated move at a time, not proposed as one document**
(`plan.PlanDraft`, and the four tools over it in `brainy`). The document shape asked a model to
be right about eight things at once — TOML, disjoint writes, no undeclared read/write, no cycles,
tests ⊆ writes, every needed file listed, N coherent briefs — and refused all of it when it was
wrong about one, with everything downstream gated on that single artifact. The whole §13 history
above is patches on that wall. So: `declare_seam`, `add_worker`, `drop_worker`, `seal_charter`,
each checked against the plan so far the moment it is made.

Three properties are the reason, and they are worth keeping:

- **An overlapping partition is unbuildable rather than reported.** `add_worker` refuses a path
  another ticket owns, so `find_conflicts` stops being a validator over a finished document and
  becomes an invariant of construction. `seal` still runs it as a belt-and-braces check: if it
  ever fires, a move-level check has a hole, and that is a thing to find with the charter in hand.
- **A refusal names one path and costs one move**, while the model is still writing the ticket
  that caused it. "4 overlap(s) in the partition" after every ticket was written cost all of them.
- **Order does not matter.** Both directions of every check run on every move — a ticket claiming
  a file an existing ticket reads is refused, and so is one reading a file an existing ticket
  claims — so nothing imposes writers-before-readers. `needs` is the one thing deferred to `seal`,
  since a plan built in the order the work occurred to somebody names a dependency before adding
  it.

`drop_worker` exists because the checks are strict and a strict check with no way back turns one
wrong move into a plan that has to be abandoned; it refuses to drop a ticket others declare `needs`
on. **`propose_charter` stays** for a plan small enough to say in one document, and because
`recover_charter` needs TOML to read out of a reply — but `BRAINY_RULES` steers to the moves for
anything larger, since a document can come back overlapping with no single move to blame.

**Both routes end at `CharterDesk.accept`.** The desk owns the draft, the charter, and every
counter, and all five tools are moves on it — one tool holding its own draft would build a charter
the others could not see, and two surfaces with separate state drift until only the one nobody
tests still works. `install_charter_tool` registers and permits all five together: the rules in
context name all of them, so a missing one is an `Unknown tool` the model cannot argue past. The
front-end still reaches a charter through `tools[PROPOSE_CHARTER]`, whose `charter`/`attempts`/
`reset` delegate to the desk.

**Brainy Birb's planning turn runs with the project's `verify_command` off**
(`run._without_project_verification`). Its job is to write *failing* tests — `BRAINY_RULES` step 3
— so the planning turn ends with a project whose check fails by design. That went straight into
`_verify_and_fix`, which saw changed files, ran the user's command, watched it fail, and handed
Brainy Birb "VERIFICATION FAILED. Fix the cause." with four turns and its file tools still
attached. The obedient answer is to implement its own stubs or weaken its own tests, destroying the
artifact the whole design rests on and the one the workers were about to build against. It also
spent a second full run of the suite and clobbered `turns_exhausted` (reset at the top of every
`_loop`), silently degrading the "ran out of planning turns" outcome into "decided not to divide".
`build_subagent` already scopes each worker's verification so it "never meets somebody else's
failing test to helpfully fix"; this is the same rule for the agent that *authors* the failing
tests, which had simply been missed. Safe to mutate the orchestrator because a flock holds the
session. The workers' own checks and the review passes are untouched.

**Pass 2 refuses the cases it cannot discriminate rather than reporting a pass**
(`review.put_the_stub_back`). `implementation` is `writes` minus `tests`, so with `tests`
undeclared it is *every* file the worker owns — the restore returns the whole scope to the skeleton,
whose check fails by construction, which this read as "caught". It reported a pass for every worker
in that shape whatever the work was, and `CHARTER_TEMPLATE`'s own second ticket omits `tests`. Two
states are now refused with "could not be checked": the worker changed nothing, and (`tests`
undeclared, more than one file owned, every changed file being restored). The extra `writes`
condition matters — a worker owning one file whose acceptance tests live in a skeleton-owned file
*is* checkable, and refusing that would fail an honest ticket. No filename heuristic, which
§"tests" rules out for good reason. `plan.add_worker` also warns when `accept` is set and `tests`
is not.

**A plan built and never sealed is its own outcome** (`PlanResult.unsealed` → `stopped_at="unsealed"`,
and `brainy.seal_reminder_prompt` for the one nudge that precedes it). It is the incremental
route's version of the bug `e91c565` fixed for the document route, and it arrives by the same
door: `attempts` counts attempts to *seal*, so a model that called `add_worker` five times and
stopped has `attempts == 0` and `charter is None` — indistinguishable from "this work does not
divide", which is what the user was told while a finished plan sat in the session unrun. The
nudge quotes the draft back, because a model told only "call `seal_charter`" starts re-adding
tickets and collides with itself. An empty draft with no tool call behind it is still the
legitimate decision it always was; an empty draft the model *worked* at is a stall, and is
reported as one.

**A Worker Birb runs its own acceptance check**, and `shell` is granted for the *programs* that
check names — `charter.policy_for` calls `Policy.allow_command(worker.accept, any_arguments=True)`.
This has been corrected twice and both corrections are worth keeping:

- **It was granted for nothing at all**, and the check was run *for* the worker after its turn. So
  the ticket's definition of done was the one thing it could not observe: write blind, learn once,
  one fix attempt (`DEFAULT_MAX_FIX_ATTEMPTS = 1`), finished — with the report saying "acceptance
  check FAILED" about work it never had a chance to iterate on.
- **Then it was granted as a prefix over the exact invocation**, which was technically least
  privilege and practically a keyhole. `allow(tool, command)` also grants only the *first* segment,
  so `accept = "pytest -q && ruff check src"` produced a grant that denied the command it was made
  from (hence `allow_command`, which covers every segment). And a worker iterating on its ticket
  runs one file at a time, adds `-x`, adds `-k` — every variation missed the prefix and became an
  approval dialog for a command the user had already approved in the charter. "Run your own
  acceptance check" was advertised and not actually granted.

So the grant is `pytest` with any arguments, plus every other program the accept command chains to.
**What it still does not grant is anything the check never names**, and that is deliberate rather
than an oversight: a pipe is a second program, so `pytest -q | head -50` needs `head` and is asked
about. Shell grants carry **no path scoping at all**, so admitting general-purpose commands like
`cat` or `head` would reach outside the worker's read scope entirely — which its file tools cannot.
A command the scan cannot read through (substitution, subshell, `find -exec`) grants nothing and the
worker simply has no shell. The post-turn verification runs either way, so no ticket is lost to that
and a worker cannot leave its check failing and talk its way past it.

**Config does not reach a Worker Birb at all** (`wiring.build_subagent`). `allow_tools`,
`allow_read_dirs` and `allow_write_dirs` are all ignored: the charter's scopes *are* the isolation,
and the user approved the charter rather than the config. Worth stating because it surprises people
— `"shell(pytest)"` in `~/.cobirb/config.json` grants a worker nothing, and `find`/`pwd`/`ls` prompt
for the same single reason rather than for any isolation rule of their own. There is no need-to-know
rule about `find`; the only find-specific code is `_EXEC_FLAGS`, which makes a command unverifiable
so it is refused rather than granted.

**`propose_charter` is registered for the whole session, not just the planning turn**
(`run.install_charter_tool`), and permitted outright. It was previously added before planning and
popped in a `finally` afterwards; but the planning transcript — `BRAINY_RULES` included, which
tells Brainy Birb to deliver a charter by calling it — stays in context for the rest of the
session, so "redo the plan" produced a call to a tool that had been taken away. The blanket
permission is a considered exception to §2's default-deny rather than an oversight: that rule
gates *capability* (filesystem, network, subprocess) and this tool reaches none of it — it parses
text and keeps the result in memory.

**The Flock tab carries Brainy Birb's working-out while it plans** (`FlockPane.planning_note` /
`planning_waiting` / `end_planning`, fed from `TuiIO`'s `render_tool_call`, `render_answer` and
`spinner`). `/flock` switches to that tab, whose panes are built from `on_charter` — the *end* of
the longest phase — so it was blank throughout. The last `PLANNING_TAIL` lines are kept and the
section is torn down in `prepare_flock_panes`. **Streamed tokens deliberately do not feed it**:
per-token updates rewrite the strip faster than it can be read, and what makes progress legible is
the sequence of things done, not the sentences being formed.

**A charter written into a reply is read from there** (`charter.recover_charter`, used by
`_plan` only when the tool was not called). A model that writes the TOML into its *answer* as
prose (not as a tool call — that case `plugins/core/toolcalls` now reads, §9) produces no call, and
`_loop` reads the reply as a final answer. That ends planning with the charter sitting in the transcript and nothing having happened,
which was the single commonest way a flock died. `PlanResult.recovered` says it happened and the
run tells the user. **`BRAINY_RULES` also states the mechanism** — that a charter in a reply
proposes nothing — because the instruction to call the tool never said what *not* calling it costs.

**Running out of planning turns is a distinct outcome** (`Orchestrator.turns_exhausted` →
`PlanResult.exhausted_turns` → `stopped_at="turns"`). A reply alone cannot say the budget ran out
(see §4's `RunStop`), and a skeleton costs one turn per file
written, so a large partition reaches the budget before it proposes anything.

**An overlapping partition is a second loop, and it needed its own brake**
(`MAX_OVERLAP_ATTEMPTS = 2`, `ProposeCharterTool._overlap_notice`). `exhausted` tests
`charter is None`, so the rejection brake below came off permanently the moment any charter
parsed — and an overlapping charter is one that parsed. That left the `ok=True` branch with no
cap, and its text opened with "Charter accepted" and closed with "propose a corrected charter":
two states at once, plus an instruction to act on the second. Same overlaps, same string, every
time; an unchanged result after an unchanged action is the strongest signal a model has to repeat
itself. A run reported this as "used all 30 planning turns without proposing a charter" having
been handed five. Now the charter is **held** rather than "accepted", the invitation to correct it
is issued twice, and a resubmission whose conflicts are identical is told so — that being the one
fact distinguishing this attempt from the last.

**A formal seam is writable by no worker, and that is refused at parse time**
(`charter._check_seams_are_frozen`). It is the structural cause of the overlap loop above: one
worker claims the shared types file it did not need to write, every other worker reads it, and
`find_conflicts` reports one read/write overlap per reader — four for five workers, none of them
about the readers. Brainy Birb was being handed that as a partition problem when the fix was one
path in one `writes` list. **Loose seams and `::`-qualified ones are exempt**: a loose seam is an
agreement with only a test behind it and may well describe behaviour inside a file a worker
implements (the charter template's own shape does), and refusing those would throw out charters
that were right. **Conflicts are also reported once per file** rather than once per pair, since
the count is the first thing anyone reads and "4 overlaps" describes a partition in ruins.

**A charter plus an exhausted planning budget is reported** (`run._drive`). The approval prompt
looks identical whether planning finished or was cut off mid-skeleton, and the user is about to
authorise workers to build against whatever is actually on disk.

**A charter that keeps failing must stop being asked for.** `MAX_CHARTER_ATTEMPTS = 5`. The
template goes out with the *first* rejection only — repeating twenty-five identical lines after
every failure is the strongest signal available to a model that the right next move is to resend
what it just sent — and at the cap the tool stops correcting and tells Brainy Birb to explain what
it is stuck on. `_plan` skips its retry when `tool.exhausted`, since otherwise a model that has
failed the tool's own limit gets a second turn budget to fail in. `charter._toml_hint` names the
cause where it can: `tomllib` reports where it gave up, and "Invalid value (at line 1, column 13)"
is the same message for a curly quote as for an unquoted string.

**A rejected charter is not the same outcome as no charter.** `ProposeCharterTool` counts
`attempts` and keeps `last_error`; `PlanResult.failed` is what distinguishes "tried and every
attempt was invalid" (`stopped_at="charter"`, reported with the reason) from "decided the work
does not divide" (`stopped_at="planning"`, a legitimate answer). Conflated, the first was reported
through the model's own narration — which, in the run that prompted this, claimed the charter had
been "finalized and submitted successfully" while nothing had run and no approval dialog had
appeared. The rejection is quoted back on the next pass of the planning loop
(`charter_retry_prompt`), because a model told its charter is invalid will otherwise often end the
turn by declaring success.

**Planning is a driven loop with a completion predicate, not one turn plus ad-hoc retries**
(`run._plan`, `MAX_PLAN_STEPS = 5`). A turn ends when the model stops calling tools, which is the
right rule for a conversation and the wrong one here: planning has an objective completion test —
is there a sealed charter? — so a model that worked out its next move and stopped to *say* it
("I will proceed by correcting the first worker's ticket") ended the phase on that sentence
without making the move. The two branches this replaced could not catch it: retry-on-rejection
needed a seal attempt, which had not happened, and the seal nudge needed `draft.workers`, which
the one refused `add_worker` had kept empty — so it fell through to "did not propose a charter",
which reads as the opposite of what the model had concluded. Each pass now asks the predicate and,
where the plan is incomplete, computes the next move from the draft (`brainy.next_move_prompt`:
rejected seal → quote it; tickets → seal; no tickets → `add_worker`, with the last refusal quoted).
Four bounded exits: a charter; a model that called no tool at all and built nothing (prose *is* the
answer, and "do not fan this out" is a legitimate one); `tool.exhausted` or the turn budget; and
`MAX_SILENT_STEPS = 2` nudges running answered with no tool call, which is `stopped_at="stalled"`
— a halt replaced by a loop would not have been an improvement.

**A refusal ends with the call to make.** `CharterDesk.refuse(message, retry=...)` appends "call
`<tool>` again now"; `_DeskTool._refuse` passes its own name. A diagnosis without an instruction —
"Add them to writes, or drop them from tests" — invites a weak model to narrate the correction
rather than send it, and the narration ends the phase. The imperative is dropped at
`MAX_REPEATED_REFUSALS`, where "send it again" is precisely what has been proven not to work.

**`tests ⊄ writes` is adopted, not refused** (`plan.PlanDraft.add_worker`). A worker's acceptance
tests are its own files, so a path under `tests` is a path the ticket writes and the model simply
did not say so twice; refusing it asked for a whole ticket to be re-sent to move one string. The
adopted paths go through `_check_writes_are_free` with the rest, so a genuine collision is still
refused, and the verdict says what was adopted because the next ticket may try to claim it. The
whole-document route (`charter._worker`) still refuses, which is the one asymmetry here.

**Stage 3 is the only place a charter is approved**, including for one that arrived outside a
planning turn. `_start_flock_with` deliberately does not ask before handing a charter to
`run_flock_session`: it did once, with the identical sentence, so one decision took two dialogs.
The app clears a charter once its flock has run (`forget_used_charter`), since the tool otherwise
keeps it and `/charter` would offer to run the round again.

**A charter accepted outside a flock run reaches the user through `on_proposed`.** The TUI holds
it (`note_proposed_charter`) rather than acting at once — the call arrives from inside a tool, with
the turn that made it still waiting on the result — and offers it when the turn ends
(`offer_pending_charter`). Approval runs the flock with `run_flock_session(charter=...)`, which
skips planning. `/charter`, and `/flock` with no objective, reach the same dialog for a charter
that was dismissed or proposed while another flock was running.


One **Brainy Birb** plans, designs the seams, writes the skeleton (interfaces, typed stubs,
semantic docstrings, failing tests), and proposes a TOML **charter**. The user approves it — the
single decision point in the run. **Worker Birbs** then run unattended inside charter-derived
scopes, are reviewed, and Brainy Birb reports.

- Charter: `objective`, `concurrency` (default 2, max 16), `[[seams]]` (`kind` ∈ `formal|loose`),
  `[[workers]]` with `writes`/`reads`/`accept`/`brief`/`needs`. Held in the session, never written
  to the repo.
- **`needs` is the exception to independence, and stays the last resort.** The default partition is
  tickets that do not wait on each other, which is the entire reason for fanning out; `BRAINY_RULES`
  says so explicitly, because a model given the field will otherwise serialise a fan-out into a
  queue. It exists for a seam that must be *built* before it can be built against and could not be
  hoisted into the skeleton. Unknown ids, self-reference and cycles are refused in `parse_charter`
  (`_check_dependencies`/`_find_cycle`, which names the cycle) — caught in the scheduler they would
  be a flock that hangs with no explanation. `find_conflicts` stops reporting a read/write overlap
  when the reader declares `needs` on the writer: the file has stopped changing, which is the
  condition the conflict existed to catch. Write/write is a conflict whatever the ordering.
  `Charter.effective_concurrency` is the width of the widest graph level, capped by `concurrency`,
  and `describe()` says so — approving "4 at a time" and getting 1 is the charter lying about the
  round. Workers wait on a `threading.Event` **before** taking a slot, never while holding one
  (that deadlocks any chain longer than the limit); `record()` stores the report *then* sets the
  event, or a released dependent reads an absent result as a failure. A dependent is skipped when
  its dependency did not run (`ok`), not when its acceptance check merely failed — one flaky check
  should not kill a subtree.
- `policy_for()`: **writes are file-strict, reads are open across `cwd`, `shell` is the programs the
  worker's own `accept` command names and nothing else.** Read isolation was tried and removed — workers could not
  orient (denied on every `list_dir`/`glob`) and the knowledge isolation never depended on it,
  since the plan is never on disk and the brief omits it.
- `build_subagent()` differs from a normal run in exactly four ways: policy handed in (not config),
  **no project context at all** (need-to-know), `HeadlessIO`, and verification scoped to the
  worker's own `accept`. Checkpoints, redaction and hooks still apply — those belong to every agent
  in someone's tree.
- **A provider fault no longer costs the ticket** (`worker.START_ATTEMPTS = 3`). `except Exception`
  turned any exception into `ok=False` and the ticket was gone — which is how a flock lost two
  workers to their opening request timing out while the first one generated, with the model never
  having seen the brief. **Retried only when nothing happened**: the condition is an empty
  `last_run_tool_calls`, so a run that had already edited files is *not* restarted — re-sending the
  brief against a tree that has moved under it is worse than a half-finished ticket reporting a
  shortcoming. Not retried during a force-stop either, since retrying after the user asked to stop
  is the opposite of stopping. `START_RETRY_SECONDS = 2` and deliberately **not** a backoff ladder:
  a retry against a busy endpoint queues *behind* the work that made it busy, so spacing attempts
  further apart buys queue depth rather than patience. Patience is `request_timeout`'s job.
- **The brief states the working directory** (`compose_brief(worker, cwd)`). `Orchestrator.run`
  appends its "Working directory:" line only `if (system or self.project_context)`, and a worker has
  neither by design — so the line was dropped and workers spent an approval dialog on `pwd` to learn
  a fact that costs one line to state. `WORKER_RULES` also names the read tools (`list_dir`, `glob`,
  `grep`, `read_file`, `repo_map`) and says the shell is not how to explore, because a worker
  reaching for `find` is one not using what it already has.
- **A worker can ask for what its scope did not give it** (`WorkerPaneIO.confirm_request`). The
  charter stays the only place capability is granted *up front*; this is the escalation out of it,
  and it exists because silently refusing cost the ticket — a worker denied `shell` spent its
  remaining turns retrying or reporting failure, with the capability question answered correctly
  and the work lost anyway. **Asking costs the asker, not the round:** the worker releases its
  concurrency slot (`supervisor.Slots`) so the rest of the flock runs at full speed, and its pane
  shows `held`. Answers are once / session (`policy.SessionGrants`) / deny-with-an-instruction,
  the last of which reaches the model through `orchestrator._denial_message`.
- **The question is asked in the asking worker's own pane, never in a modal** (`panes.WorkerRequest`,
  `WorkerPane.ask`, `app.request_worker_approval`). It was a stacked `WorkerApprovalModal` per
  request, answered top down, on the reasoning that the stack already serialises them and each
  dialog names its worker. That ignored *where* the modals appear — all in one place, so dismissing
  one drops the next under a cursor already committed to clicking, and a click meant for one
  worker's `pytest` answers a different worker's request for something else. In a permission dialog
  that is approving a command nobody read. **Distinct screen positions are the fix, not a queue**:
  panes are columns, so no two workers' buttons ever share coordinates. **Nothing is focused or
  armed by default**, or the same race reappears on the keyboard. It **fails closed with no pane**
  — there is no fallback modal, because inventing a screen that cannot aim at a worker is how the
  bug got written. The preview goes to the pane's log rather than into the request block, which
  would otherwise grow to the height of a diff and push its own buttons off the bottom. Panes are
  90 columns wide (was 44) because a request has to be read rather than skimmed.
- **A write into a file another worker owns is refused without asking** (`charter.writes_owner`).
  Exclusive ownership is what makes concurrency safe by construction, not a convention — granted
  away mid-round, two agents edit one file with no lock and "which worker broke this" stops having
  an answer. It is also not a fair question to put to a person, who would have to hold the whole
  partition in their head at the moment a dialog appears; CoBirb has the charter and can check.
  Headless has nobody to ask and still refuses everything outside the charter.
- **A finished flock does not steal the tab or print its report twice** (`_on_flock_finished`).
  `PromptInput` lives inside the Current `TabPane`, so focusing it makes Textual activate that
  pane — which yanked the user off the Flock tab on *every* run end, including the successful ones
  the tab is most worth reading; it is now focused only when Current is already active. And
  `run.report` is frequently a reply the transcript already holds — for a stopped planning phase it
  *is* the narration, for a finished round it is the verdict turn's own answer, both written by
  `io_bridge.render_answer` on the way past — so `app.note_answer` records the last reply and the
  report is skipped when it matches.
- Workers run concurrently, then join, **then** review one at a time (review reverts a stub
  temporarily, which would break a colleague's check). A failed worker never stops the others.
  Stopping is checked between workers **and between reviews**; a model call in flight cannot be
  interrupted, and neither can a review already under way — its restore is part of the operation.
- Review is **two passes**, cheapest first: (1) read the diff for suspicious changes, (2) restore
  the stub and assert the acceptance check **fails**. Pass 2 is the reliable one, and it reports
  "could not be checked" for the states it cannot judge rather than passing them. Neither uses a
  model, so a review costs no tokens and cannot be argued out of a finding. A third pass that
  mutated each stated behaviour was **deleted** — see §17.
- `preflight.missing_models()` warns before planning if a role's model is absent; `probe` measures
  whether the endpoint truly serves two requests at once (B must start answering before A finishes).
- `branch.py` mints a GUID written into both the main session and a paired flock session file. One
  engagement = one flock session; a second round appends to it.

## 14. Surfaces

**CLI** — subcommands `setup` (`runtime/setup.py`: asks for the server address and protocol, lists
that server's models, saves the pick — atomically, 0600, every other key kept, an unparseable
config refused rather than replaced — then runs doctor; it **asks and never probes**, D7),
`help [topic]`, `models`, `commands`, `flock -p "..."`,
`plugin install <path> [--replace] | list | remove <name>`. Flags: `-p/--prompt`, `--session`,
`-w/--password`, `--model`, `--allow-tool` (repeatable, `name` or `name(arg)`),
`--plan-mode on|off`, `--autopilot` (with `-p`), `--system-prompt off|harness`, `--export PATH`, `--branch PATH`,
`--branch-at N`, `--headless`, `--output text|json`, `--cwd`, `--upgrade [TAG]`, `--force`,
`--continue` (reopens the most recently touched session; implies a password), `--doctor`.
Subcommand `doctor` is the same thing — `runtime/doctor.py` checks config keys/types/references,
endpoint and model presence, and install shape/version (plus branch state for a checkout), exiting
non-zero only on a real failure. It deliberately does not ask whether a newer release exists on a
managed install: that is a request to GitHub, and doctor talks to the configured endpoint only.
`cwd` is resolved to an absolute path once in `main()`. A flag that would silently do nothing
(`--branch-at` without `--branch`, `--force` without `--upgrade`) is an error, not a no-op.

**Headless** (`--headless`, usually with `--output json`) never prompts. Exit codes: `0` clean,
`1` failed, `2` completed but something was refused — `2` is headless-only, because a person who
answered "no" got what they asked for.

**TUI** — four tabs: Current, Flock, Sessions, Plugins. Slash commands `/help`, `/model`,
`/plan`, `/autopilot` (§5), `/context`, `/clear` (§7), `/undo`, `/export`, `/diff`, `/commands`,
`/flock`, `/charter` (§13), `/memories`, `/remember`, `/image`; `@path` in the prompt box opens a five-row fuzzy picker
(`tui/mention_picker.py`, ranked by `runtime/mentions.py`) and sends the named file with the
message — expanded on the way to the model, never into the transcript. Anything else
starting with `/` is tried as a custom command, then sent to the model unchanged.

**`/` opens the command picker** (`tui/command_picker.py`, listed and ranked by
`runtime/command_index.py`) — the same five rows, plus a `5 of 15` counter, plus each command's own
description, taken from the first line of its handler's docstring rather than a table kept beside
them. Custom commands are included and tagged with their source; one shadowed by a built-in name is
not, because the built-in is what would run. `_COMMAND_IN_PROGRESS` matches the **whole message**
(`^/([^\s/]*)$`), not the word under the cursor as `@` does: a slash only means a command as the
first word, so this keeps a path from summoning a list and leaves "remind me to /clear later" as
prose. `command_index.rank` returns every match rather than a capped slice because the counter has
to know how many did not fit, and `#command-picker` sets `text-wrap: nowrap` so a description can
never wrap a row onto a second line and push another off the bottom. Keys: `f1` help,
`f2` next tab, `ctrl+q` quit, `ctrl+c` copy selection else cancel the turn, `up`/`down` recall the
last 100 prompts (memory only). The prompt box stays enabled during a turn — submitting again
steers rather than queueing. Tool approval is a modal (`y` once / `a` always / `n`/escape deny) and
states what "always" would grant. A Worker Birb's request is asked in that worker's own pane instead
(`panes.WorkerRequest`, §13): once / session / deny, naming the worker, saying a session grant
reaches agents that have not started, with a placeholder-only field for "do this instead" — a
placeholder so the prompt text can never be submitted as though the user had typed it. Nothing is
focused by default.

**Config keys** — `default_model`, `models.*`, `system_prompt`, `plugins.{model,io,crypto}`,
`allow_tools`, `allow_read_dirs`, `allow_write_dirs`, `verify_command`, `verify_timeout`,
`verify_fix_attempts`, `redact_secrets`, `checkpoints`, `instructions`, `instructions_max_chars`,
`repo_map`, `repo_map_max_chars`, `context_tokens`, `max_num_ctx`, `plan_mode`, `audit_log`, `hooks`,
`mcp_servers`, `max_turns`, `sandbox`, `connect_timeout`, `request_timeout`. `persona` is retired (0.22.0): `doctor.RETIRED_KEYS` names it as removed rather than
as an unknown key, since "not a setting CoBirb reads" sends someone hunting for a typo.
`runtime/bootstrap.ensure_home()` seeds a starter config on first run (not at install — wheels have
no reliable post-install hook).

**Env** — `COBIRB_HOME` (relocates the whole `.cobirb` tree; how tests isolate),
`COBIRB_MODEL_NAME`, `COBIRB_OLLAMA_URL`, `COBIRB_PROJECT_DIR` (local-plugin discovery root),
`COBIRB_TEST_MODEL` (integration tests).

**Install shapes** — `upgrade.detect_install()` answers which of three is running, and everything
about releases follows from it. `managed`: `install.sh` built a venv at `~/.local/share/cobirb`
(overridable with `COBIRB_INSTALL_DIR`) and left a `cobirb` symlink in `~/.local/bin`, recording
both in an `install.json` marker. `checkout`: a git clone that was `pip install -e`'d. `unmanaged`:
anything else — someone's own venv, a distro package — where `--upgrade` refuses and names what
would work instead. Managed is decided by the marker's `venv` matching `sys.prefix`, not by the
marker existing: having a managed install *and* a clone to work in is ordinary, and the question is
which interpreter is running.

**Releases** — every version bump gets a `vX.Y.Z` tag (§2, invariant 8); `release.yml` builds a
wheel and sdist on that tag push and attaches them, `SHA256SUMS` and `install.sh` to the GitHub
release.

**Cut one with `scripts/release.sh patch|minor|major` — do not improvise the sequence.** Write the
notes under `## [Unreleased]` in `CHANGELOG.md` first; the script renames that heading to the new
version, bumps `pyproject.toml`, commits and tags. It pushes only with `--push`, and runs the
suite only with `--test`. **The steps it omits are as deliberate as the ones it performs**: a
release adds a version string and a CHANGELOG heading to a tree that was already tested when it
was committed and again by `tests.yml` on push, so re-running the suite, polling the workflow, or
installing the published artifact afterwards proves nothing that was in doubt. That ritual grew up
around five hand-run releases and cost minutes each time. If a release ever does need verifying,
that is its own deliberate command, not a tail on this one.

`cobirb --upgrade [tag]` then routes on the install shape:

- **Managed** — runs the `install.sh` that shipped *inside the running version's own wheel*,
  forwarding `--version`/`--force`. **The script is the only implementation of "move to version
  X"**, because it is also what a first-time user curls; resolving releases, guarding downgrades
  and verifying checksums a second time in Python would be two implementations to keep in step. It
  is copied to a tempfile before running because pip is about to rewrite the original underneath a
  shell that reads scripts as it executes them. `UpgradeResult.describe()` is empty for this shape
  — the script already narrated itself to the terminal.
- **Checkout** — fetches tags, resolves the highest `vX.Y.Z` (or the named one, `v` optional),
  refuses a downgrade without `--force` and a dirty working tree outright, then re-runs `pip
  install -e .`. **It fast-forwards the current branch onto the tag rather than checking the tag
  out**, because a detached `HEAD` swallows the next commit anyone makes in that checkout.
  Detaching is the fallback when there is no branch, or the branch carries commits the tag
  doesn't, and `UpgradeResult.branch`/`describe()` says which happened.

## 15. Conventions

- **Docstrings explain *why*.** This is the house style and the reason the code is navigable. When
  you fix a subtle bug, the reason it was a bug goes in the docstring or a comment there.
- **Changing behaviour means updating `docs/`, in the same change.** Those pages state what
  CoBirb does — flags, commands, config keys, defaults, what a command prints — so they are tied
  to the implementation rather than describing it loosely, and a page that has drifted is worse
  than no page: it is the first thing a user reads and they have no way to know it is stale.
  Adding or renaming a `/command`, a CLI flag or a config key, or changing what one of them does,
  is not finished until the matching page says so. `docs/README.md` is the index of which page
  covers what. Keep them short and current — they are the user's first read, not a second
  reference manual.
- `from __future__ import annotations` in every module.
- Source code cites other source (`see X`), never a documentation file.
- **Keep the core thin.** Feature logic that lands in `orchestrator.py` probably belongs in a tool,
  a plugin, or `runtime/`. The orchestrator wires and dispatches; it does not implement features.
- Optional capability is **duck-typed and probed**, never added to an ABC (that is what keeps the
  SPI freeze honest).
- Every path under `~/.cobirb` comes from `paths.py`, resolved at call time.
- Bound every result that a model or a person will read; say when you truncated.
- Prefer stdlib. The three runtime dependencies are each a deliberate decision.
- **A transport failure and a rejected request are different questions and must never share an
  error message.** "Is it running?" is right only when nothing answered; when something answered,
  quote what it said. (`urllib.error.HTTPError` subclasses `URLError`, which is how a missing model
  once reported as an unreachable server — see `_unreachable`/`_error_body`.)

## 16. Testing

```bash
pip install -e ".[dev]"
pytest                                            # CI runs this on 3.11 and 3.12
COBIRB_TEST_MODEL=llama3.1 pytest -m integration  # needs a real local Ollama
```

- **No unit test reaches a model endpoint or the network.** `conftest._no_model_endpoint` (autouse)
  refuses any connection to port 11434 or off loopback, and fails the test at teardown even when the
  code under test swallowed the refusal. Loopback on other ports stays open for the tests' own tiny
  HTTP servers; `integration`-marked tests are exempt. It exists because 26 tests once built a real
  provider and talked to `localhost:11434` — passing while Ollama ran, hanging when it didn't.
- One test file per module. `conftest.py` sets `COBIRB_HOME` to a tmp dir for **every** test
  (autouse), so nothing touches a real home directory; `write_config(home, data)` is the only
  sanctioned way to put a setting in force.
- `asyncio_mode = "auto"` — TUI tests drive the app through Textual's `App.run_test()` Pilot
  harness and need no per-test decoration.
- Crypto and session tests run against the real `cryptography` backend; the round trip is genuine.
- Subprocess boundaries (`pip`, `git`) are usually mocked, with at least one test per module
  exercising the real thing where a mock could not prove it works.
- **Test the contract, not the internals.** The aim is between 85 % and 90 % coverage; a higher number is not a
  target to defend. The concrete test: if a change preserves a function's contract and behaviour,
  its tests should not need to change. Symptoms of over-specification — asserting on whole recorded
  call dicts, exact log strings, private helpers, or call sequences when the observable result is
  what matters — are fixed **in the test**. Loosening or deleting an over-specified test is a
  legitimate outcome, and a small coverage drop is a fine price for a suite that stops obstructing
  change. Shared doubles live in `conftest.py` only when genuinely identical across files;
  specialised ones stay in the file whose questions they answer.
- No linter, formatter or type checker is configured — match surrounding style by hand.

## 17. Decided — do not rebuild these

Absences in the source are not omissions. Each of these was costed and rejected; reopening one is a
fresh decision to take with the user, not a gap to helpfully fill.

- **Local models only, forever.** No shipped or blessed remote provider, ever. The SPI lets a third
  party write one; that is their choice to make.
- **CoBirb is a client, never a model runtime.** An embedded GGUF runtime (in-process
  `llama-cpp-python`, and a supervised `llama-server` child) was designed and cut: the trust problem
  belongs to the endpoint, and fixing it there fixes it for every client of the protocol. Users can
  point CoBirb at an endpoint they wrote themselves.
- ~~No sandbox for `shell`~~ — **reversed in 0.34.0** (decision D2): see §5's sandbox paragraph.
- **No embedding-based RAG over the codebase.** A repo map plus grep beats it for code at a
  fraction of the machinery, with no index to keep warm.
- **Omitted permanently:** cloud sessions, remote control, background agents, telemetry. (Subagents
  are not background agents: local, in-process, bounded by a turn you asked for.) Speech I/O is
  deferred indefinitely — a large platform-specific dependency for a workflow nobody uses here.
- **Parked, not dropped:** parallel read-only tool calls (the model, not disk I/O, is the
  bottleneck; the cost is turn ordering and `ShellTool`'s per-call process state) and git
  auto-commit (writes to someone's repository history — needs decisions about when to commit, what
  to do with pre-existing uncommitted work, and whether to touch their branch; `/diff` covers the
  reviewing half without any of them).
- **The flock review's third pass — mutation testing — was built, never wired up, and has been
  deleted.** It mutated each behaviour a docstring claimed, one deliberately-wrong implementation
  per promise, and required each to be caught; a surviving mutant named a promise nothing was
  testing, which made it a review of the *contract* as much as of the work. But `review_worker`
  took the mutants as a keyword argument, `supervisor.run_flock` (its only caller) never passed
  any, and nothing anywhere constructed a `Mutant` — so it ran **zero times in every real flock**
  while AGENTS.md, `help_text.py` and `docs/manual/flock.md` all described it as part of how review
  works, and `round_summary` asked Brainy Birb to interpret surviving mutants that could not exist.
  The reason it was never connected: writing the mutants needs Brainy Birb, one model round-trip
  *per stated behaviour*, which is far and away the most expensive thing in a review that otherwise
  costs no tokens — and `review` deliberately holds no model, so the piece that would supply them
  was never built. `review.expect_red` is kept as the primitive and does not care where broken
  content comes from, so reviving this means writing the mutants and passing them in. **Do not
  reintroduce it without deciding who pays for those round-trips and when**, and note that the hole
  it was there to cover is now closed from the other side: pass 2 reports "could not be checked"
  for the states it cannot judge instead of reporting them as passes.
- **The TUI header/banner panel was removed. Do not reintroduce it without deciding how it stays
  accurate.** It was written at mount from the model resolved at `__init__`, *before* the async
  startup model check ran — so a startup picker change left it permanently wrong, and the
  transcript (`TranscriptLog`/`RichLog`) is append-only, so a second corrected panel below the
  first reads as a bug, not a correction. `StatusBar` already carries model/plan/cwd/session
  and self-corrects because it is a reactive widget.

Vision (attached images) is built — see §6c. What's still not built is the rest of the deferred
list below: cross-project sharing of memory catalogues, image editing, and anything resembling the
"Projects" container design once floated and superseded by memory catalogues (§6b) — a catalogue is
already the right-sized, independently loadable unit; a project wrapper around sessions + memory +
manifest never got built and shouldn't be revisited without a fresh reason.

## 18. Known gaps (documented, not defects)

- **CoBirb's guarantees end at the model socket.** The endpoint is a separate program; it may bind
  an unauthenticated port and make its own requests. CoBirb is a client and does not run weights.
- **Without bubblewrap, an approved `shell` command runs with full user privileges** — and so does
  one sent with `unsandboxed: true`, or any command with `sandbox: "off"`. The sandbox (§5) covers
  the rest. It hides a fixed list of credential paths, not every secret a machine might hold, and
  the environment is passed through unchanged.
- **Without `git` installed, `/undo` cannot cover what a shell command did** — the per-file
  fallback only knows what a tool declared it would write. With git it can (§6).
- **Installing a plugin executes its code** (`pip` runs the package's build backend) before any
  permission layer exists to ask about it.
- **A configured MCP server can do whatever it likes with the arguments it receives.** Its env is
  minimal by default and its tools are pre-approved by nothing, but nothing detects egress.
- **An attached image makes its session file bigger by roughly the image's size**, and every save
  rewrites the whole blob. Accepted deliberately: AES runs at GB/s so this is disk, not latency,
  and the alternative (bytes outside the session) is what made images unreadable on resume in the
  first place — see §6c. No size cap on `/image`; the format sniff only asks "is this an image".
- **`redact_secrets` matches formats, not names** — no `password=` heuristics. It will miss a
  bespoke credential format, and an agent asked to *edit* a credentials file needs it turned off.
