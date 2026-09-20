# AGENTS.md — CoBirb

Working reference for agents editing this repository. Derived from the source and tests.

## 1. What this is

CoBirb is a **privacy-first agentic coding CLI**: the capabilities people get from cloud-hosted
coding assistants, against models running on the user's own machine, with no telemetry and no
outbound network unless the user explicitly asks for it.

- `v0.9.2`, Python ≥3.11, MIT. Entry point `cobirb = cobirb.cli:main`.
- Runtime deps: `rich` (rendering), `cryptography` (session cipher), `textual` (interactive app,
  imported lazily so one-shot never loads it). Dev extras: `pytest`, `pytest-cov`, `pytest-asyncio`.
- Ships **no models**. The core model provider speaks to a local Ollama-compatible endpoint
  (`http://localhost:11434` by default) and only once a model is named.

## 2. Invariants — do not violate

1. **Default-deny.** A fresh `Policy` permits nothing. Capability comes from an approval prompt,
   `allow_tools`/`allow_read_dirs`/`allow_write_dirs` in config, or `--allow-tool`. There is no
   pre-approved set and deliberately no "approve everything" flag.
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
8. **Persona data never affects behaviour or permissions.** It is pure data about voice.
9. **Every version bump gets a matching `vX.Y.Z` git tag** — `cobirb --upgrade` resolves releases
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
| `checkpoints.py` | Pre-edit file snapshots behind `/undo` and `/diff`. |
| `redaction.py` | Credential stripping for tool output and audit args. |
| `config.py` / `paths.py` | The single config file; every `~/.cobirb` path derived in one place. |
| `typing/spi.py` | **The plugin contract.** All SPI interfaces + shared dataclasses. |
| `plugins/loader.py` | Discovery (entry points + local dirs), fail-closed. |
| `plugins/core/` | Built-ins: `tools`, `model`, `io`, `crypto`, `persona`, `render`, `repomap`, `ignores`. |
| `runtime/` | Composition layer both front-ends share: `wiring`, `plugins`, `models`, `personas`, `commands`, `command_index` (what the `/` picker lists and how it ranks), `sessions`, `instructions`, `hooks`, `verify`, `custom_commands`, `headless`, `export`, `bootstrap`, `plugin_install`, `upgrade`, `catalogues` (which catalogues a session has open — see §6b), `mentions` (`@path` ranking + expansion), `doctor` (the readiness checks). |
| `mcp/` | stdio MCP client (`client`) and its tool adapter (`tools`). |
| `flock/` | Multi-agent runs: `charter`, `brainy`, `worker`, `supervisor`, `review`, `run`, `branch`, `probe`, `preflight`. |
| `tui/` | Textual app: `app` (the application itself — mount, input, the turn, workers, actions), `slash_commands` (what each `/command` does, as `(app, argument)` functions + the `COMMANDS` table), `transcript` (everything written to the transcript, and the flush-before-write ordering rule), `attachments` (images queued by `/image` for the next message), `mention_picker` (the five-row `@path` list), `command_picker` (the five-row `/command` list), `widgets`, `screens`, `panes`, `io_bridge`, `flock_bridge`, `app.tcss`. |
| `help_text.py` | The prose `cobirb help [topic]` prints. |
| `install.sh` | The installer, shipped inside the package. Also the upgrader and downgrader — see §14. |
| `personas/*.json` | Bundled personas: `professional`, `neighbor`, `kawaii`. |
| `tests/` | One file per module; `conftest.py` isolates `COBIRB_HOME` for every test. |

## 4. The loop

`Orchestrator` wires five pluggable pieces — model provider, tool registry, policy, I/O adapter,
session manager — and contains no feature business logic.

```
prompt → model call → tool calls (policy-gated) → results into history → repeat
       → plain reply with no tool calls == final answer (becomes session.summary)
```

- Bounded by `max_turns` (default 8; validate phase 4). Exhaustion returns a synthetic
  "Stopped after N turns" answer.
- Each iteration: drain steering queue → build context → model call → either record tool calls and
  execute, or return the final answer.
- **Plan mode** (`--plan-mode on`, `/plan on`, `plan_mode` config; off by default) splits the run
  into three model phases, each recorded with `Turn.phase` ∈ `plan|act|validate`. The plan phase is
  called with `tools=[]` so it cannot act. `phase` is descriptive only — it never changes context
  replay, but it *is* covered by the turn hash.
- **Verification** (`_verify_and_fix`) runs the user's `verify_command` after a turn that changed
  files, feeds a failure back as a user turn, and allows one bounded fix attempt.
- **Mid-turn steering** (`steer()`, thread-safe): queues a message applied as a user turn at the
  next loop boundary, and — if the provider implements `interrupt_current_reply()` — cuts the
  in-flight stream via `SteeringInterrupted`. The partial reply is kept as a real turn. Distinct
  from `cancel()`, which is the one-way stop.
- **Optional, duck-typed hooks the loop probes for** (never part of the ABC): on the model,
  `context_window()`, `cancel()`, `interrupt_current_reply()`; on tools, `writes()`, `preview()`,
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
- `Checkpoints` copies aside only the paths a tool declares via `writes()`, once per turn, keeping
  `DEFAULT_KEEP_TURNS = 20`. Snapshots are plaintext under `0700` (they duplicate files already in
  the workspace). **`shell` is the honest gap** — undo cannot cover what a command did.

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
2. Drop a contiguous run of oldest turns after turn 0, never starting the remainder on an orphaned tool result, leaving a note.
3. Elide inside the working set if it is itself over budget.
4. Trim the largest bodies repeatedly (bounded at 64 iterations) — the pass that guarantees it fits.

## 8. Tools (`plugins/core/tools.py`)

Built-ins: `read_file`, `write_file`, `edit_file`, `apply_patch`, `glob`, `grep`, `list_dir`,
`repo_map`, `shell`. Subclasses of `CobirbTool` declare `NAME: ClassVar[str]` and inherit `name()`
— the SPI declares `name` as a **method**, and `ToolRegistry.register` rejects anything else.

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
- Streaming is NDJSON; `_last_tool_calls` is only accurate once the generator is exhausted.
  `cancel()` latches the provider closed; `interrupt_current_reply()` cuts one reply and leaves the
  provider usable — `_steer_signal` is cleared before every request so a late interrupt never
  reports the *next* request's failure as a steer.
- `supports_vision()` is a constant `False`; nothing consumes images anywhere.

## 10. Plugin SPI (`typing/spi.py`)

- `SPI_VERSION = 1`, `MIN_SUPPORTED_SPI_VERSION = 1`, **frozen**: within a version, changes are
  additive only (new *optional, duck-typed* hooks are fine; new abstract methods, renames and
  signature changes are not). A plugin declares `COBIRB_SPI = <int>`; absent means 1. A
  non-integer declaration is an error, not a shrug. Incompatible → `IncompatiblePlugin`, refused at
  the loader boundary, non-fatally.
- Interfaces: `Tool`, `ModelProvider`, `I_OAdapter`, `SessionCrypto`; data: `ToolCall`,
  `ToolResult`, `ApprovalRequest`, `Persona`, `SteeringInterrupted`, and the
  `once`/`always`/`deny` decision constants.
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
| Verify (`runtime/verify.py`) | `"verify_command": "pytest -q"` | Off unless set — never guessed. Runs **outside** the permission layer by design (the user's own config, run by CoBirb, unchangeable by the model). 120 s timeout, 1 fix attempt. |
| Custom commands (`runtime/custom_commands.py`) | `~/.cobirb/commands/*.md`, `<project>/.cobirb/commands/*.md` | `/name` sends the body; `$ARGUMENTS` and `$1`…`$9` substitute, otherwise arguments are appended. Optional `---\ndescription: …\n---` frontmatter. |
| MCP (`mcp/`) | `mcp_servers` in config, **stdio only** | Tools register as `mcp__<server>__<tool>` and go through the same policy, prompt, audit and redaction path. Environment is **not** inherited (only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` + configured `env`, unless `inherit_env`). Not members of `READ_TOOLS` — "always" grants that one tool, for the session. |
| Per-role models (`runtime/models.py`) | `models.default` / `.orchestrator` / `.worker` | Roles inherit from `default` field by field. Name resolution: `--model` → `models.<role>.name` → `model` → `models.default.name` → `default_model`. `cobirb models` prints the result. |
| Personas (`runtime/personas.py`) | `--persona`, `/persona`, `"persona"` | Off by default (`none` → a label, no voice instructions). Bundled: `professional`, `neighbor`, `kawaii`; user files in `~/.cobirb/personas/`. |

## 13. The Flock (`flock/`)

**`propose_charter` is registered for the whole session, not just the planning turn**
(`run.install_charter_tool`), and permitted outright. It was previously added before planning and
popped in a `finally` afterwards; but the planning transcript — `BRAINY_RULES` included, which
tells Brainy Birb to deliver a charter by calling it — stays in context for the rest of the
session, so "redo the plan" produced a call to a tool that had been taken away. The blanket
permission is a considered exception to §2's default-deny rather than an oversight: that rule
gates *capability* (filesystem, network, subprocess) and this tool reaches none of it — it parses
text and keeps the result in memory.

**A rejected charter is not the same outcome as no charter.** `ProposeCharterTool` counts
`attempts` and keeps `last_error`; `PlanResult.failed` is what distinguishes "tried and every
attempt was invalid" (`stopped_at="charter"`, reported with the reason) from "decided the work
does not divide" (`stopped_at="planning"`, a legitimate answer). Conflated, the first was reported
through the model's own narration — which, in the run that prompted this, claimed the charter had
been "finalized and submitted successfully" while nothing had run and no approval dialog had
appeared. `_plan` also retries once with the rejection quoted back (`charter_retry_prompt`),
because a model told its charter is invalid will otherwise often end the turn by declaring
success.

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
  `[[workers]]` with `writes`/`reads`/`accept`/`brief`. Held in the session, never written to the repo.
- `policy_for()`: **writes are file-strict, reads are open across `cwd`.** Read isolation was tried
  and removed — workers could not orient (denied on every `list_dir`/`glob`) and the knowledge
  isolation never depended on it, since the plan is never on disk and the brief omits it.
- `build_subagent()` differs from a normal run in exactly four ways: policy handed in (not config),
  **no project context at all** (need-to-know), `HeadlessIO`, and verification scoped to the
  worker's own `accept`. Checkpoints, redaction and hooks still apply — those belong to every agent
  in someone's tree.
- Workers run concurrently, then join, **then** review one at a time (review reverts a stub
  temporarily, which would break a colleague's check). A failed worker never stops the others.
  Stopping is checked between workers; a model call in flight cannot be interrupted.
- Review, cheapest first: (1) read the diff for suspicious changes, (2) restore the stub and assert
  the acceptance check **fails**, (3) mutate each stated behaviour. Pass 2 is the reliable one.
- `preflight.missing_models()` warns before planning if a role's model is absent; `probe` measures
  whether the endpoint truly serves two requests at once (B must start answering before A finishes).
- `branch.py` mints a GUID written into both the main session and a paired flock session file. One
  engagement = one flock session; a second round appends to it.

## 14. Surfaces

**CLI** — subcommands `help [topic]`, `models`, `commands`, `flock -p "..."`,
`plugin install <path> [--replace] | list | remove <name>`. Flags: `-p/--prompt`, `--session`,
`-w/--password`, `--model`, `--persona`, `--allow-tool` (repeatable, `name` or `name(arg)`),
`--plan-mode on|off`, `--system-prompt off|harness`, `--export PATH`, `--branch PATH`,
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
`/persona`, `/plan`, `/context`, `/clear` (§7), `/undo`, `/export`, `/diff`, `/commands`,
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
states what "always" would grant.

**Config keys** — `default_model`, `models.*`, `persona`, `system_prompt`, `plugins.{model,io,crypto}`,
`allow_tools`, `allow_read_dirs`, `allow_write_dirs`, `verify_command`, `verify_timeout`,
`verify_fix_attempts`, `redact_secrets`, `checkpoints`, `instructions`, `instructions_max_chars`,
`repo_map`, `repo_map_max_chars`, `context_tokens`, `plan_mode`, `audit_log`, `hooks`, `mcp_servers`.
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

**Releases** — every version bump gets a `vX.Y.Z` tag (§2, invariant 9); `release.yml` builds a
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
- **No sandbox for `shell`.** Documented rather than built — the SPI allows replacing the `shell`
  tool with a sandboxing one, which is where that work belongs.
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
- **The TUI header/banner panel was removed. Do not reintroduce it without deciding how it stays
  accurate.** It was written at mount from the model resolved at `__init__`, *before* the async
  startup model check ran — so a startup picker change left it permanently wrong, and the
  transcript (`TranscriptLog`/`RichLog`) is append-only, so a second corrected panel below the
  first reads as a bug, not a correction. `StatusBar` already carries persona/model/plan/cwd/session
  and self-corrects because it is a reactive widget.

Vision (attached images) is built — see §6c. What's still not built is the rest of the deferred
list below: cross-project sharing of memory catalogues, image editing, and anything resembling the
"Projects" container design once floated and superseded by memory catalogues (§6b) — a catalogue is
already the right-sized, independently loadable unit; a project wrapper around sessions + memory +
manifest never got built and shouldn't be revisited without a fresh reason.

## 18. Known gaps (documented, not defects)

- **CoBirb's guarantees end at the model socket.** The endpoint is a separate program; it may bind
  an unauthenticated port and make its own requests. CoBirb is a client and does not run weights.
- **An approved `shell` command runs with full user privileges.** The policy decides *whether* a
  command runs, never what it can reach. There is no sandbox; the SPI allows a third party to
  replace `shell` with a sandboxing tool.
- **`/undo` cannot cover what a shell command did** — `shell` cannot declare what it writes.
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
