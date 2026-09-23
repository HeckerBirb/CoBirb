# AGENTS.md — CoBirb

Working reference for agents editing this repository: the rules, where things live, and the one-line
reason each rule exists. The longer story behind a decision is in `CHANGELOG.md` and in the docstring
next to the code — this file says *what holds*, not how it came to.

## 1. What this is

CoBirb is a **privacy-first agentic coding CLI**: the capabilities people get from cloud-hosted coding
assistants, against models running on hardware the user controls, with no telemetry and no outbound
network unless the user explicitly asks for it.

- Version: `pyproject.toml` only (`cobirb.__version__` reads the installed metadata). Python ≥3.11,
  MIT. Entry point `cobirb = cobirb.cli:main`.
- Runtime deps: `rich`, `cryptography`, `textual` (imported lazily; one-shot never loads it). Dev:
  `pytest`, `pytest-cov`, `pytest-asyncio`. Prefer stdlib; each dependency is a deliberate decision.
- Ships **no models**. It talks to one model server the user configures — Ollama's API by default, or
  any OpenAI-compatible server (`"api": "openai"`) — and only once a model is named.

## 2. Invariants — do not violate

1. **Default-deny, with one contained exception.** A fresh `Policy` permits nothing. Capability comes
   from an approval prompt, `allow_tools` / `allow_read_dirs` / `allow_write_dirs`, `--allow-tool`, or
   auto-pilot. There is no "approve everything" flag. The exception: a shell command **inside the
   sandbox** runs without asking by default — it can reach no network and write nothing outside the
   project — and only where whole-tree checkpoints can undo what it changed (§5, §6).
2. **One config file: `~/.cobirb/config.json`.** No repo-local config is read, merged or looked for. A
   repository may *describe* itself (`AGENTS.md`, repo map, `<project>/.cobirb/commands/`) but may
   never grant capability.
3. **No outbound network by default.** Only the model provider opens a socket, to the endpoint the
   user configured. Two exceptions, both explicitly invoked: `cobirb --upgrade` (a git remote for a
   checkout; GitHub release assets plus PyPI for a managed install) and MCP servers the user
   configured. CoBirb never probes or discovers endpoints — the config says what it may reach.
4. **Sessions are encrypted at rest.** Plaintext conversation exists in RAM only.
5. **Fail closed.** No I/O adapter, an adapter that cannot ask or raises, an unparseable shell command,
   a plugin that will not load, a target a permission check cannot pin down → deny or skip.
6. **Never crash on a recoverable fault.** Broken plugin → reported and skipped; tool raises → a failed
   `ToolResult` the model can correct from; broken config → reported, defaults used. Each broad
   `except Exception` carries a comment saying why.
7. **The model's own system prompt wins.** With nothing to add, CoBirb sends *no* system message, so
   the Modelfile `SYSTEM` stays in force. When it adds something, the model's own goes first.
8. **Every version bump gets a matching `vX.Y.Z` tag** — `--upgrade` resolves releases only from tags.

## 2b. The hardware this targets

**Assume at least 16 GB of VRAM and a 128k context window.** Users have capable machines or rent GPUs;
two models at once, or several subagents, is reasonable. **Performance, memory and model-size figures
are observations, never arguments**: note that something is slow or large, but do not let it quietly
pick a default, narrow a feature or rule an approach out — raise it as a decision. (Compaction was once
sized for a 4096-token window, and that one assumption made four wrong calls.)

## 3. Layout

| Path | Responsibility |
|---|---|
| `cli.py` | argparse, mode dispatch, one-shot run. No wiring logic. |
| `orchestrator.py` | The agent loop: model ↔ tools, policy-gated, stop reasons, plan mode, auto-pilot. No feature logic. |
| `policy.py` | Permissions, shell-command scanning, audit log. |
| `sandbox.py` | Where shell commands run (bubblewrap). |
| `patches.py` | The `*** Begin Patch` format, shared by `apply_patch` and the policy. |
| `session.py` | `Turn`/`Session`, encrypted `SessionManager`, schema migration, `fork_session`. |
| `context.py` | Fitting history into the window (compaction). |
| `memory.py` | Memory-catalogue files. |
| `checkpoints.py` | `/undo` and `/diff`: `TreeCheckpoints` (whole tree, git) or per-file `Checkpoints`. |
| `redaction.py` | Credential stripping for tool output and audit args. |
| `config.py` / `paths.py` | The single config file; every `~/.cobirb` path, derived in one place. |
| `typing/spi.py` | **The plugin contract.** All SPI interfaces and shared dataclasses. |
| `plugins/loader.py` | Discovery (entry points + local dirs), fail-closed. |
| `plugins/core/` | Built-ins: `tools`, `model` (Ollama), `openai` (OpenAI-compatible), `toolcalls` (calls written as text), `io`, `crypto`, `render`, `repomap`, `ignores`. |
| `runtime/` | Shared composition: `wiring`, `plugins`, `models`, `system_prompt`, `setup`, `commands`, `command_index`, `sessions`, `instructions`, `hooks`, `verify`, `custom_commands`, `headless`, `export`, `bootstrap`, `plugin_install`, `upgrade`, `catalogues`, `mentions`, `doctor`. |
| `mcp/` | stdio MCP client and its tool adapter. |
| `flock/` | Multi-agent runs: `charter`, `plan`, `brainy`, `worker`, `supervisor`, `review`, `run`, `branch`, `probe`, `preflight`. |
| `tui/` | Textual app: `app`, `slash_commands` (handlers + `COMMANDS`), `transcript` (flush-before-write), `attachments`, `mention_picker`, `command_picker`, `widgets`, `screens`, `panes`, `io_bridge`, `flock_bridge`, `app.tcss`. |
| `help_text.py` | `cobirb help` — the overview; `cobirb help <topic>` renders the manual (§14). |
| `docs/manual/` | The user manual. Shipped in the package as `cobirb/manual/`. |
| `install.sh` | Installer, upgrader and downgrader; shipped in the package (§14). |
| `bench/` | cobirb-bench, the offline benchmark (§16). Not shipped. |
| `tests/` | One file per module; `conftest.py` isolates `COBIRB_HOME` and blocks the network. |

## 4. The loop

`Orchestrator` wires five pluggable pieces — model provider, tool registry, policy, I/O adapter,
session manager — and contains no feature business logic.

```
prompt → model call → tool calls (policy-gated) → results into history → repeat
       → plain reply with no tool calls == final answer (becomes session.summary)
```

- **Stopped by lack of progress, not a turn count.** `DEFAULT_MAX_TURNS = 40` (config `max_turns`) is a
  backstop. The same call with the same arguments back to back gets a note on its result
  (`_REPEAT_NOTE_AT = 2`) and ends the run at `_REPEAT_STOP_AT = 4`; `_FAILURE_STOP_AT = 6` failed or
  denied calls in a row end it too. (A flat 8 used to end ordinary tasks half done.)
- **How a run ended is `Orchestrator.last_stop`** (`RunStop`: `STOP_ANSWERED`, `STOP_TURN_LIMIT`,
  `STOP_NO_PROGRESS`), never text in the summary: the stop is rendered through `render_notice`, no
  "Completed." is printed under it, and headless reports `stop_reason` and exits 1.
- Each iteration: drain the steering queue → build context → model call → run the calls, or return.
- **A call the provider could not read** (`malformed_tool_call()`, §9) is fed back as a user turn and
  counts toward the failure brake, instead of being taken for the answer.
- **Plan mode** (`--plan-mode`, `/plan`, `plan_mode`): a planning pass (`_PLAN_MAX_TURNS = 12`) offered
  only `READ_TOOLS` and `todo`, then the work. A call naming a tool the phase did not offer is refused
  (`_execute_tool_calls(offered=...)`) — a native call can name anything. `Turn.phase` is `plan|act`;
  old `validate` turns and `Session.validation` still load. `phase` never changes replay but is hashed.
- **Auto-pilot** — §5.
- **Verification** (`_verify_and_fix`) runs the user's `verify_command` after a turn that changed files
  and allows one bounded fix attempt.
- **Mid-turn steering** (`steer()`, thread-safe) queues a message for the next boundary and, if the
  provider has `interrupt_current_reply()`, cuts the stream (`SteeringInterrupted`); the partial reply
  is kept. `cancel()` is the one-way stop.
- **Optional duck-typed hooks the loop probes for** (never on the ABC): model — `context_window()`,
  `cancel()`, `interrupt_current_reply()`, `malformed_tool_call()`; tools — `writes()`, `preview()`,
  `cancel_running()`; checkpoints — `end_turn()`, `close()`; I/O — `spinner`, `begin_stream`,
  `confirm_scoped`, `confirm_request`, `render_answer`, `render_plan`, `render_tool_call`,
  `render_notice`, `write_error`. `render_through()` is the probe-with-fallback helper.

## 5. Permissions (`policy.py`, `sandbox.py`)

- `READ_TOOLS = {read_file, list_dir, glob, grep, repo_map}` and `WRITE_TOOLS = {write_file, edit_file,
  apply_patch, delete_file}` are scoped **by directory**, in separate sets that never imply each other.
  `shell` is in neither: it cannot say what it touches.
- Paths resolve with `realpath` against the policy's `cwd`; `_within` requires a separator
  (`/x/project-secrets` never matches `/x/project`). **`Policy._resolve` and `CobirbTool._resolve`
  mirror each other step for step**, `~` included — a difference is a check on a path the tool won't use.
  A missing or empty `path` is the working directory for `list_dir`, `grep` and `repo_map`, in both.
- `shell` is checked **per segment** (`git status; rm -rf /` is two commands). Rules are a bare binary
  (any arguments) or an exact multi-word prefix. Refused as unverifiable: backticks, `$(...)`,
  subshells, unbalanced quotes, `find -exec/-execdir/-ok/-okdir`.
- `grant()` decides what "always" widens to: a read inside the project → **the whole project** (one
  question per project; `_project_root` refuses `/` and the home directory), any other read or a write
  → its directory, a shell call → that invocation, anything else → the tool name. `describe_grant()`
  says so before the user agrees.
- **A `*** Begin Patch` names its own file**, and `patch_target` (shared with the tool) is what is
  checked. Several files, a move, a delete, or a `path` that disagrees → no target → denied.
- `SessionGrants` applies a `"session"` answer to **every agent in the session**, including Worker Birbs
  built later, with no record of who asked (a permission that depends on its origin is one nobody can
  reason about). Memory only, never written to config; policies held weakly.
- **The sandbox** (bubblewrap): filesystem read-only except the project and a private `/tmp`; the
  project's **`.git` read-only** (undo restores files, not history); no network namespace; own
  PID/IPC/UTS namespaces; credential paths hidden (`DEFAULT_HIDDEN` plus `paths.cobirb_dir()`, masked at
  their real paths because bubblewrap resolves symlinks inside the new root). Modes: `"auto"` (default
  — contained and not asked, via `Policy.sandbox_auto`, main agent only; a *default* auto applies only
  where whole-tree checkpoints exist), `"ask"`, `"off"`. In auto even a command the segment scan cannot
  read is allowed — containment is the guarantee. `unsandboxed: true` runs outside and always goes
  through the policy. `find_bwrap` probes once with the real flags; without a working bubblewrap
  everything behaves as before and `doctor` says so.
- **Auto-pilot** (`enable_autopilot`, `/autopilot`, `--autopilot` with `-p`): reads and writes inside the
  project (`Policy.autopilot_root`) and contained commands run unasked; **everything else is refused
  without asking** (`_autopilot_refusal`). It will not start without the sandbox *and* whole-tree
  checkpoints, or in a project that is `/` or the home directory — it is exactly as safe as what
  contains it.
- `todo` and the charter tools reach nothing and are permitted outright — a considered exception:
  default-deny gates capability (filesystem, network, subprocess), and these touch none of it.
- `AuditLog` is **off** unless `"audit_log": true`; it stores arguments verbatim, so it is 0600,
  redacted, and never created until enabled.

## 6. Sessions, crypto, undo

- `SCHEMA_VERSION = 1`. `Session.from_dict` is the only read path and always calls `migrate()`; a
  future schema is refused. Fields since schema 1 are optional, so `_MIGRATIONS` is empty — add
  optional fields, not migrations, unless a change makes an old payload *wrong*
  (`test_every_schema_step_has_a_migration` enforces the pairing).
- `Turn.digest()` hashes role + content + `tool_use` + `phase`; `load` verifies every hash. Never
  extend the formula — older sessions would fail.
- Crypto: AES-256-GCM, key from scrypt `N=2**17, r=8, p=1`. Blob = `b"cobirb1"` + base64 JSON header
  naming the KDF parameters + `\n` + base64(`salt||nonce||ciphertext`). Headerless blobs are pre-header
  files read at `N=2**14` and rewritten on save. No PQ KEM: a password-encrypted local file has no key
  exchange to protect.
- Session files and the audit log are created `0600` via `os.open` (no chmod window).
- `fork_session()` only reads the source; a truncated branch drops `summary`/`validation`, keeps
  `flock`, records `forked_from = "<path>@turn<N>"`, and carries only the images its turns reference.
- **`TreeCheckpoints` snapshots the whole tree before and after every turn**, so `/undo` and `/diff`
  cover shell changes. A separate git dir with the project as work tree: the project need not be a
  repository, its own `.git` is never read or written, its `.gitignore` holds, `ALWAYS_IGNORED` and
  `.git` are excluded, and the user's git config is kept out (no hooks, no signing, fixed identity).
  `0700`, lives for the session (`close()`), dead-process stores swept. **Undo restores only files the
  last turn changed that are still as it left them.** Chosen when `git` is installed.
- Per-file `Checkpoints` (the paths a tool's `writes()` declares, `DEFAULT_KEEP_TURNS = 20`) is the
  fallback without git, and what Worker Birbs use — concurrent workers would capture each other's work
  in a tree snapshot.

## 6b. Memory catalogues (`memory.py`)

Named lists of facts fed into the system prompt: `<name>.md` (plaintext) or `<name>.md.enc`
(password-protected, same crypto as sessions) under `paths.memories_dir()`. `public.md` always exists.
Bodies are a flat `- fact` list — no metadata, since it is read straight into a prompt. Created `0600`
via `os.open`: a plaintext catalogue feeds the prompt, so a write window is a way to put words in the
model's mouth. `/memories` manages catalogues and `/remember <fact>` adds one — slash commands, not a
model tool, so they fire exactly once whatever the model can do. Which are open is
`runtime.catalogues.CatalogueStore`, session-lifetime, never auto-loaded; `system_prompt()` is composed
fresh on every `run()`. **Never reaches a Worker Birb** (`project_context=""`).

## 6c. Vision — attached images

`/image <path> [message]` attaches a file to the next prompt (or sends at once with a message); a
quoted path wins, and an unquoted argument naming an existing file is taken whole. **The bytes live in
`Session.images` (`{id: base64}`, keyed by content hash) inside the encrypted blob; `Turn.images` holds
only references.** Do not move them out: nothing retains a password, so a separate file could never be
decrypted to build context. `_build_context` resolves every image turn (a missing id degrades to a
marker). `context.py` prices an image at a flat `_IMAGE_TOKENS = 1500` — never by its base64 length —
and elides old ones first. Data is sent only when `supports_vision()` says the model can see.

## 7. Context management (`context.py`)

- `DEFAULT_CONTEXT_TOKENS = 32768` is the floor only when the provider cannot say. The Ollama provider
  *states* `num_ctx` on every request (the Modelfile's `num_ctx`, else the advertised maximum), capped
  by `max_num_ctx` (`"64k"` = 65536; unparseable → `None`, reported by `doctor`). The cap only lowers,
  and `_context_budget` packs against the same number. `context_tokens` is a different knob (history
  budget, not the wire `num_ctx`). `history_budget` reserves 20 %, clamped to 2048–16384; estimation is
  `len(text) // 4`.
- `/clear` appends a `role == "clear"` turn — a role, not a field, because the digest formula is fixed.
  `turns_since_clear` is shared by `_build_context` and `transcript.render_history`, which must not
  drift. Everything before the marker stays in the file.
- `compact()` passes, cheapest loss first; short sessions return unchanged:
  1. Elide old tool results outside the last `_KEEP_RECENT = 6` turns (≥400 chars; the call stays).
  2. Drop a contiguous run after turn 0, never leaving an orphaned tool result — replaced by a
     **model-written summary** when a `summarise` callback is offered (`Orchestrator._summarise_dropped`:
     no tools, material trimmed, cached so it is one call per growth of the dropped prefix, the plain
     note on failure, capped at `_SUMMARY_MAX_CHARS`).
  3. Elide inside the working set if it is itself over budget.
  4. Trim the largest bodies repeatedly (≤64 iterations) — the pass that guarantees a fit.

## 8. Tools (`plugins/core/tools.py`)

Built-ins: `read_file`, `write_file`, `edit_file`, `apply_patch`, `delete_file`, `glob`, `grep`,
`list_dir`, `repo_map`, `shell`, `todo`. Subclasses of `CobirbTool` declare `NAME: ClassVar[str]`; the
SPI declares `name` as a **method** and `ToolRegistry.register` rejects anything else. Descriptions say
when to use a tool and which neighbour fits better — small models lean on them.

- **`edit_file` changes exactly one region or refuses** (`_plan_edit`): several matches are refused
  with their line numbers (`replace_all` opts in); a miss tries a unique whitespace-tolerant whole-line
  match (trailing space, CRLF, an indent missing uniformly, re-added to `new_str`); a true miss quotes
  the closest region. Success shows the edited lines.
- **`apply_patch`** takes unified diffs, diffs with bare `@@` (placed by context), and `*** Begin Patch`
  (`patches.py`), single-file only. Context placement refuses a block that matches twice without an
  `@@` anchor.
- **`delete_file`** removes files, never directories; scoped, previewed and undoable like any write.
- **`todo`** is a checklist replaced whole on each call; progress shows on the status bar.
- **A write that leaves Python, JSON or TOML unparseable says so** (`_syntax_note`, in-process). A note,
  not a refusal. No per-edit lint command — `verify_command` covers the user's own check.
- Results are bounded: 256 KiB per read (paged, saying how to continue), 500 grep matches, 300 chars per
  line, 1000 list/glob entries, 64 KiB of shell output (head **and** tail), 8 KiB of preview.
- `shell`: own process group, default timeout 300 s (max 600), `cancel_running()` for Ctrl+C, runs in the
  sandbox when active. Each call is its own process, so a `cd` does not persist: `cwd` is the stateless
  way, and `_changes_directory_only()` notes a `cd`-only line. That detector is advisory and fail-open,
  so it must not share code with `policy._segments`, which fails closed.

## 9. Model providers (`plugins/core/model.py`, `openai.py`)

- **Ollama** (`LocalModelProvider`): `POST /api/chat` with a real role-tagged `messages` array — history
  flattened into one message stops the tool loop converging. `/api/show` (cached per model) supplies the
  Modelfile `SYSTEM`, the window and `capabilities` (vision). `GET /v1/models` lists models.
- `compose_system()`: empty in → no system message; model has none → ours; both → model's first.
- **Tool calls written as text are read** (`toolcalls.py`) when the structured field is empty: Hermes
  `<tool_call>` JSON, Qwen3-coder `<function=…>` XML, leaked gpt-oss channel markup, and JSON that is
  the whole reply. Only names offered this turn count; plain JSON only when the prose around it is under
  `_BARE_JSON_SLACK`. Unreadable attempts — unknown tool, broken JSON, **the server's own parser
  failing** (as the reply with HTTP 200, a streamed `error` line, or a 500 body) — go to
  `malformed_tool_call()`. Replayed assistant turns have the markup stripped.
- **A thinking model's reasoning is replayed with the tool calls it led to** (`_thinking_by_call`, keyed
  by `_call_signature`, bounded, memory only) — gpt-oss's format expects it, and without it the model
  re-read the same file. Never attached to a final answer.
- `models.<role>.options` are sent with every request, merged over `models.default.options`; `num_ctx`
  is dropped there because `max_num_ctx` owns the window.
- Streaming is NDJSON; `_last_tool_calls` is valid once the generator is exhausted. `cancel()` latches
  the provider closed; `interrupt_current_reply()` cuts one reply; `_steer_signal` is cleared before
  every request. `_stream_lines` is the protocol-independent half, shared by both providers.
- **A request reset before the first byte of its reply is sent once more** (`_stream_lines`,
  `_RESET_ATTEMPTS = 2`): nothing was yielded, and one reset from a busy server used to cost a Worker
  Birb its ticket. Only a reset — a refusal or a timeout is not retried.
- **Two timeouts** (`connect_timeout` 10 s, `request_timeout` 600 s): a socket timeout measures silence,
  not work, and a queued request is silent. Split in the streaming path (`_connect` short, `_open`
  long); `_post` takes the long one; `list_models` catches a dead endpoint on the short one.
- **OpenAI-compatible** (`OpenAICompatibleProvider`, `models.<role>.api = "openai"`, never probed):
  `/v1/chat/completions`; tool-call ids minted on replay and echoed by the matching result; arguments
  are JSON strings and stream as index-keyed fragments; options are top-level fields; no Modelfile.
  **The window is read, not requested** (llama.cpp `/props` `n_ctx`, else `/v1/models`
  `max_model_len`/`context_length`/`meta.n_ctx`), capped by `max_num_ctx`. Vision:
  `models.<role>.vision`, else llama.cpp's `modalities`.
- **A transport failure and a rejected request never share a message**: "Is it running?" only when
  nothing answered; otherwise quote the server (`_unreachable`, `_error_body`; `HTTPError` subclasses
  `URLError`). Messages name the server in use.

## 10. Plugin SPI (`typing/spi.py`)

- `SPI_VERSION = 1`, `MIN_SUPPORTED_SPI_VERSION = 1`, **frozen**: changes within a version are additive
  (optional duck-typed hooks yes; new abstract methods, renames, signature changes no). `COBIRB_SPI =
  <int>`, absent means 1, non-integer is an error. Incompatible → `IncompatiblePlugin`, refused at the
  loader, non-fatally.
- Interfaces `Tool`, `ModelProvider`, `I_OAdapter`, `SessionCrypto`; data `ToolCall`, `ToolResult`,
  `ApprovalRequest`, `ApprovalOutcome`, `Persona` (unused since personas were removed; kept because the
  SPI is frozen), `SteeringInterrupted`, and the `once`/`always`/`session`/`deny` constants.
  `confirm_scoped`/`confirm_request` are optional hooks tried richest first; anything unrecognised is deny.
- Entry-point group `cobirb.plugins`. **Tools are additive**; **model/io/crypto are singleton slots**
  replaced only when named in `plugins.<slot>`. Local plugins live in `~/.cobirb/plugins/<name>/` as a
  distribution named `cobirb_plugins_<name>`; `cobirb plugin install` automates that and never fetches.

## 11. Project grounding

- `runtime/instructions.py` reads the **first** of `AGENTS.md`, `CoBirb.md`, `COBIRB.md` in the working
  directory only (no walking up), capped at 32000 chars with truncation announced.
- `plugins/core/repomap.py`: a ranked outline (Python via `ast`, regexes elsewhere), 16000 chars,
  injected into the session-start prompt **and** exposed as the `repo_map` tool.
- Both compose into `Orchestrator.project_context`, on every request's system prompt.

## 12. User extension points

| Mechanism | Shape | Notes |
|---|---|---|
| Hooks (`runtime/hooks.py`) | `before_tool`, `after_tool`, `before_turn`, `after_turn` | JSON on stdin, 30 s timeout. A non-zero `before_tool` **blocks** the call; its output is the model's reason. Others observe; failures never fatal. |
| Verify (`runtime/verify.py`) | `verify_command` | Off unless set, never guessed. Runs outside the permission layer (the user's own config). 120 s, one fix attempt. |
| Custom commands | `~/.cobirb/commands/*.md`, `<project>/.cobirb/commands/*.md` | `/name` sends the body; `$ARGUMENTS`, `$1`…`$9`; optional `description` frontmatter. |
| MCP (`mcp/`) | `mcp_servers`, **stdio only** | Tools as `mcp__<server>__<tool>`, same policy/audit/redaction path. Env **not** inherited (only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` + configured `env`, unless `inherit_env`). "Always" grants that one tool. |
| Model roles (`runtime/models.py`) | `models.default` / `.orchestrator` / `.worker` | Inherit from `default` field by field; `options` merge key by key. Name: `--model` → `models.<role>.name` → `models.default.name` → deprecated `model` / `default_model`. `cobirb models` prints the result. |
| System prompt (`runtime/system_prompt.py`) | `system_prompt`: `off` \| `harness` | Off by default. `harness` is a short working-method block after the model's own `SYSTEM` — measured no better (49/60 either way on the harder benchmark tasks), so opt-in. |

## 13. The Flock (`flock/`)

One **Brainy Birb** plans, designs the seams, writes the skeleton (interfaces, typed stubs, semantic
docstrings, failing tests) and builds a **charter**. The user approves it — the single decision point.
**Worker Birbs** then run inside charter-derived scopes, are reviewed, and Brainy Birb reports.

**Measured, and not yet the headline** (`bench --flock`, 0.40): on two divisible tasks and three models
it passed 1 of 6 against a single agent's 4 of 6, and took 2–6× longer; tickets were left unimplemented
while the round report called the round a success. **The round report is not evidence — the checker
is.** The bench records each worker's own report (`worker_reports`, with what each refused call was
aimed at) and the charter's file assignment; start there. The whole gap was one task: `Store` was
frozen as a seam and never implemented — see *Seams* below.

**Charter.** `objective`, `concurrency` (default 2, max 16), `[[seams]]` (`kind` ∈ `formal|loose`),
`[[workers]]` with `writes`/`reads`/`tests`/`accept`/`brief`/`needs`. Held in the session, never written
to the repo. Stage 3 is the only place one is approved, however it arrived.

**Planning.**
- **Built a validated move at a time** (`plan.PlanDraft`; `declare_seam`, `add_worker`, `drop_worker`,
  `seal_charter`): an overlapping partition is unbuildable rather than reported, a refusal names one
  path and costs one move, and order does not matter (`needs` is settled at seal). `drop_worker` refuses to drop a ticket others `need`.
  `propose_charter` stays for small plans and because `recover_charter` reads TOML out of a reply.
- **All five tools are moves on one `CharterDesk`** (owner of the draft, the charter and every counter),
  registered and permitted together for the whole session by `install_charter_tool` — the planning
  rules stay in context, so a missing tool is an `Unknown tool` the model cannot argue past.
- **Staged planning, opt-in** (`run._staged`, `flock_planning` = `"single"` (default) | `"staged"`): divide
  (`brainy.partition_prompt`, tickets via `add_worker` before any file exists), then one
  `skeleton_prompt` per ticket, then `seal_prompt` — each a separate `run` on the same session, each
  re-stating the objective and carrying only its own instructions, because the one-prompt planner asked
  for everything at once and the weakest model ended half its rounds with no charter. Each ticket's
  brief is rewritten after its skeleton. Measured: charters every time for ornith 9B (2/6 → 5/6), but
  qwen3-coder lost a spec detail in every rep (6/6 → 2/6) — so not the default until a run shows it
  costs nothing. The bench keeps the planned skeleton and briefs for a failed round to find where.
  What is left incomplete falls through to the driven loop below; a decline in step 1 ends planning.
- **A driven loop with a completion predicate** (`run._plan`, `MAX_PLAN_STEPS = 5`): after each pass, no
  sealed charter → `brainy.next_move_prompt` asks for exactly the missing move. Exits: a charter; no
  tool called and nothing built (a legitimate "do not divide"); `tool.exhausted`, the turn budget, or
  `STOP_NO_PROGRESS`; `MAX_SILENT_STEPS = 2` unanswered nudges → `stopped_at="stalled"`.
- **A refusal ends with the call to make** (`CharterDesk.refuse(retry=...)`), dropped at
  `MAX_REPEATED_REFUSALS`. `tests ⊄ writes` is adopted, not refused, on the move route.
- **Seams.** A seam is the signature, not the file, and it locks nothing: the stub file belongs to the
  ticket that implements it, others build against the signature concurrently, and the owner keeping it
  is the worker's rules plus review. The only partition conflict is **write/write**, reported once per
  file. (Seams were once writable by no worker and a file another ticket read was refused; together
  they made "one implements `Store`, one builds against it" illegal, and the refusal told Brainy Birb
  to leave the stub out of every ticket.) A finished shared file simply has no owner.
- **`seal` asks once about skeleton files nobody owns** (`CharterDesk.ownership_question`, a project
  snapshot taken before planning, CoBirb's own directory excluded): a file nobody owns stays as the
  skeleton left it. Sealing again unchanged means "finished"; not counted as an attempt. An overlapping held charter gets `MAX_OVERLAP_ATTEMPTS = 2`
  invitations; a failing one stops being asked for at `MAX_CHARTER_ATTEMPTS = 5` (template sent with
  the first rejection only; `_toml_hint` names the cause).
- **Distinct outcomes, never conflated**: no charter (`planning`), rejected (`charter`, with the reason
  quoted back), built but never sealed (`unsealed`, after one `seal_reminder_prompt`), out of turns
  (`turns`), stalled (`stalled`), recovered from a reply (`recovered`). A charter plus an exhausted
  budget is reported before approval.
- Planning runs with the project's `verify_command` **off** (`_without_project_verification`) — its job
  is to write failing tests, which verification would tell it to "fix".
- The Flock tab shows Brainy Birb's tool calls while it plans (`FlockPane.planning_note`, last
  `PLANNING_TAIL` lines); streamed tokens deliberately do not feed it.

**Workers.**
- `build_subagent()` differs from a normal run in exactly four ways: policy handed in (config's
  `allow_*` keys do not apply), **no project context** (need-to-know), `HeadlessIO`, and verification
  scoped to the worker's own `accept`. Sandbox, per-file checkpoints, redaction and hooks still apply;
  the sandbox never auto-approves a worker's shell.
- `policy_for()`: **writes file-strict, reads open across `cwd`** (read isolation was tried and left
  workers unable to orient), **shell = the programs the worker's own `accept` names, any arguments**
  (`allow_command` covers every segment); a `cd` inside `cwd` is never what refuses a command
  (`Policy._harmless_cd`). Nothing the check never names — a pipe to `head` is a second
  program, and shell grants carry no path scoping. An unreadable `accept` grants nothing.
- A write into a file another worker owns is refused without asking (`writes_owner`): exclusive
  ownership is what makes concurrency safe.
- A worker may ask for what its scope lacks (`WorkerPaneIO.confirm_request`) — **in its own pane, never
  a modal** (distinct positions, nothing focused by default, fail closed with no pane) — and releases its
  concurrency slot while it waits. Answers: once / session / deny-with-instruction.
- `START_ATTEMPTS = 3`, only when nothing happened yet (no tool calls) and not during a force-stop;
  `START_RETRY_SECONDS = 2`, no backoff (a retry queues behind the work that made the endpoint busy).
  `DEFAULT_MAX_TURNS = 30`. The brief states the working directory and names the read tools.
- **`needs` is the last resort** (it serialises a fan-out). Unknown ids, self-reference and cycles are
  refused at parse. A ticket's tests should pass with its own code and the skeleton alone (a fake, or
  `needs`) — `BRAINY_RULES` says so.
  `effective_concurrency` is the widest graph level. Workers wait **before** taking a slot; `record()`
  stores the report, then sets the event. A dependent is skipped only when its dependency did not run.

**Re-check.** After the join, `supervisor.recheck` runs every finished ticket's `accept` again on the
final tree (no model) and updates `accepted`, keeping `accepted_when_finished`: a worker's own verdict
is from the moment it finished, often before a colleague's code landed. A ticket that passed only on
the final tree is named in the round's account.

**Review.** Workers run concurrently, join, **then** are reviewed one at a time (review reverts a stub
temporarily). Two passes, no model, no tokens: (1) read the diff for suspicious changes, including a
changed declaration, (2) restore the stub and require the acceptance check to **fail** — worded
"PASS — … it is this worker's code that makes them pass", because "stub reversion: caught" was read
by two of three models as the worker having reverted — reporting "could not be checked" when the
worker changed nothing, or when `tests` is undeclared, several files are owned and every changed file
would be restored. Stopping is checked between workers and between reviews; a review under way finishes.

**Around it.** `preflight.missing_models()` warns before planning; `probe` measures real concurrency.
`branch.py` pairs the main session with one flock session per engagement. A finished flock does not
steal the tab or reprint a report the transcript already holds. A charter proposed outside a flock run
is held and offered when the turn ends (`on_proposed`, `offer_pending_charter`, `/charter`).

## 14. Surfaces

**CLI** — subcommands `setup` (asks for the server and protocol, lists its models, saves the pick —
atomic, 0600, every other key kept, an unparseable config refused; never probes), `doctor`, `help
[topic]`, `models`, `commands`, `flock -p`, `plugin install <path> [--replace] | list | remove <name>`.
Flags: `-p`, `--session`, `-w/--password`, `--model`, `--allow-tool`, `--plan-mode on|off`,
`--autopilot` (with `-p`), `--system-prompt off|harness`, `--export PATH`, `--branch PATH`,
`--branch-at N`, `--headless`, `--output text|json`, `--cwd`, `--upgrade [TAG]`, `--force`,
`--continue`, `--doctor`. A flag that would silently do nothing is an error. `doctor` checks config
keys and types (naming retired and deprecated keys as such), the endpoint and models, the sandbox, and
the install; it never asks GitHub about releases.

**Headless** never prompts. Exit `0` clean, `1` failed (including a run that stopped short), `2`
completed but something was refused — headless only, since a person who answered "no" got what they asked.

**TUI** — tabs Current, Flock, Sessions, Plugins. Commands `/help`, `/model` (offers to save a pick when
none is configured), `/plan`, `/autopilot`, `/context`, `/clear`, `/undo`, `/export`, `/diff`,
`/commands`, `/flock`, `/charter`, `/memories`, `/remember`, `/image`. `@path` opens a five-row fuzzy
picker and sends the file with the message (expanded for the model, never in the transcript). `/` opens
the command picker (descriptions from each handler's docstring; `_COMMAND_IN_PROGRESS` matches the whole
message, so a slash mid-sentence is prose). Anything else starting with `/` is tried as a custom command,
then sent as typed. Keys: `f1`, `f2`, `ctrl+q`, `ctrl+c` (copy, else cancel), `up`/`down` history. The
prompt stays enabled during a turn — submitting steers. Approval is a modal (`y`/`a`/`n`) stating what
"always" grants. The status bar shows AUTOPILOT, checklist progress, model, plan mode, cwd, session.

**Config keys** — `models.*`, `system_prompt`, `plugins.{model,io,crypto}`, `allow_tools`,
`allow_read_dirs`, `allow_write_dirs`, `sandbox`, `max_turns`, `verify_command`, `verify_timeout`,
`verify_fix_attempts`, `redact_secrets`, `checkpoints`, `instructions`, `instructions_max_chars`,
`repo_map`, `repo_map_max_chars`, `context_tokens`, `max_num_ctx`, `connect_timeout`, `request_timeout`,
`plan_mode`, `audit_log`, `hooks`, `mcp_servers`, `flock_planning`. Deprecated: `model`, `default_model`. Retired:
`persona`. `ensure_home()` seeds a starter config on first run.

**Help** — `cobirb help` prints the overview (`help_text._OVERVIEW` plus the page list);
`cobirb help <topic>` renders the matching manual page — `rich` markdown on a terminal, plain when
piped — from `cobirb/manual/` in a wheel (copied from `docs/manual/` by the `setup.py` build hook;
`MANIFEST.in` carries the pages into the sdist) or `docs/manual/` in a checkout. `help_text.ALIASES`
keeps old topic names working. **The manual is the one source**: change a page, and the help changes
with it — there is no second copy to update.

**Env** — `COBIRB_HOME` (relocates `.cobirb`; how tests isolate), `COBIRB_MODEL_NAME`,
`COBIRB_OLLAMA_URL`, `COBIRB_PROJECT_DIR`, `COBIRB_TEST_MODEL`, `COBIRB_INSTALL_DIR`.

**Install shapes** — `upgrade.detect_install()`: `managed` (`install.sh`'s venv at
`~/.local/share/cobirb`, decided by the marker's `venv` matching `sys.prefix`), `checkout` (a clone,
`pip install -e`), `unmanaged` (refused, naming what would work).

**Releases** — cut with `scripts/release.sh patch|minor|major`; never improvise the sequence. Notes go
under `## [Unreleased]` first; the script renames the heading, bumps `pyproject.toml`, commits and tags,
and pushes only with `--push`. It deliberately does not re-run tests, poll CI or install the artifact.
`release.yml` builds wheel and sdist on the tag and attaches them with `SHA256SUMS` and `install.sh`.
`install.sh` ends every install and upgrade with `post_install_report`: `cobirb doctor` (its exit code
ignored — it reports on configuration, not on the install), one line explaining the marks, and, when
`bubblewrap` (Linux) or `git` is missing, why to install them and the package-manager command. It never
runs `sudo`. `COBIRB_INSTALL_SOURCED=1` sources the script without installing, for tests.
`--upgrade` then: **managed** runs the `install.sh` shipped in the running wheel (the one implementation
of "move to version X"; copied to a tempfile first); **checkout** fetches tags, refuses a downgrade
without `--force` and a dirty tree outright, **fast-forwards the current branch** onto the tag (a
detached `HEAD` swallows the next commit), and reinstalls.

## 15. Conventions

- **Docstrings explain *why*.** When you fix a subtle bug, the reason it was a bug goes next to the fix.
- **Changing behaviour means updating `docs/`, in the same change** — a stale page is worse than none.
  `docs/README.md` indexes the pages; they are also `cobirb help`.
- `from __future__ import annotations` in every module. Source cites source (`see X`), never a doc file.
- **Keep the core thin**: feature logic belongs in a tool, a plugin or `runtime/`, not `orchestrator.py`.
- Optional capability is **duck-typed and probed**, never added to an ABC (the SPI freeze depends on it).
- Every path under `~/.cobirb` comes from `paths.py`, at call time.
- Bound every result a model or a person will read, and say when it was truncated.
- A hardware or performance figure is an observation, not a decision (§2b).

## 16. Testing and measuring

```bash
pip install -e ".[dev]"
pytest                                            # parallel (-n auto), ~15 s; CI: 3.11 and 3.12
pytest -p no:xdist tests/test_x.py -k name        # serial, for one test
COBIRB_TEST_MODEL=llama3.1 pytest -m integration  # needs a real local Ollama
python bench/cobirb_bench.py --models <m1,m2> --reps 2   # the offline benchmark
```

- **No unit test reaches a model endpoint or the network**: `conftest._no_model_endpoint` refuses port
  11434 and anything off loopback, and fails at teardown even if the refusal was swallowed.
  `integration` tests are exempt.
- **Runs in parallel by default** (`pytest-xdist`, `-n auto --dist loadgroup`), which works because
  every test is isolated. A test that touches something genuinely shared — the real `pip` runs in
  `test_plugin_install.py` write into the one virtualenv — goes in an `xdist_group` so its file runs
  on one worker.
- One test file per module; `COBIRB_HOME` is a tmp dir for every test; `write_config(home, data)` is the
  only sanctioned way to set config. `asyncio_mode = "auto"` for the Textual Pilot tests. Crypto runs
  against the real backend. Subprocess boundaries are usually mocked, with at least one real test.
- **Test the contract, not the internals**: aim for 85–90 % coverage, not more. If a change preserves a
  contract, its tests should not change; over-specified tests are fixed in the test.
- No linter, formatter or type checker — match the surrounding style.
- **cobirb-bench** (`bench/`): fixture repos, an instruction, a hidden checker; runs CoBirb headless from
  a frozen `git worktree` of one commit, seeded per repetition (not temperature 0), and classifies every
  failure by cause. `bench/selftest.py` proves each checker fails the untouched fixture and passes the
  reference `solution/`. `--flock` runs a flock session instead (the only place a charter is
  auto-approved — throwaway copies only). `compat_table.py` generates `docs/manual/models.md`.
  **A claim that something improves reliability is checked here**, beyond the noise between runs.

## 17. Decided — do not rebuild these

Absences are not omissions. Reopening one is a fresh decision to take with the user.

- **Local models only, forever** — no shipped or blessed remote provider. The SPI lets a third party write one.
- **CoBirb is a client, never a model runtime** — an embedded GGUF runtime was designed and cut; the
  trust problem belongs to the endpoint.
- **The working-method prompt stays opt-in** — measured no better (§12). Re-measure rather than re-argue.
- **No embedding-based RAG** — a repo map plus grep, with no index to keep warm.
- **Omitted permanently:** cloud sessions, remote control, background agents, telemetry. Speech I/O
  deferred indefinitely.
- **Parked:** parallel read-only tool calls (the model is the bottleneck), git auto-commit (someone's
  history; `/diff` covers review), model profiles (no measured per-family difference yet), `move_file`
  (two paths; the permission check is single-target — `mv` works in the sandbox).
- **The review's mutation pass was deleted** — built but never wired, so it ran zero times while the
  docs described it. `review.expect_red` remains; do not revive it without deciding who pays for one
  model round-trip per stated behaviour.
- **No TUI header panel** — it could not stay accurate in an append-only transcript; `StatusBar` does.
- **No "Projects" container** — memory catalogues are the right-sized unit.

Ideas not yet built, with why each waits, are in [`ROADMAP.md`](./ROADMAP.md).

## 18. Known gaps (documented, not defects)

- **Guarantees end at the model socket.** The endpoint is a separate program.
- **Without a working bubblewrap, an approved `shell` command runs with full user privileges** — as
  does one sent `unsandboxed`, or any with `sandbox: "off"`. The sandbox hides a fixed list of
  credential paths, not every secret, and passes the environment through.
- **Without `git`, `/undo` cannot cover shell changes** — only what a tool declared.
- **Installing a plugin executes its code** before any permission layer exists.
- **A configured MCP server can do what it likes with the arguments it receives.**
- **An attached image grows its session file by about its size**, and every save rewrites the blob.
- **`redact_secrets` matches formats, not names** — it misses bespoke credential formats.
