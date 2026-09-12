# AGENTS.md — CoBirb

> **This file is the single source of truth for this repository.** It carries the intent, the
> architecture, the plugin contract, the security design, and the working conventions. It absorbed
> the former `DESIGN.md` (architecture/constraints) and `PLUGIN_SPEC.md` (plugin SPI) when those
> were retired; anything that used to cite them cites a section here instead.
>
> If code and this file disagree, that is a bug in one of them — say which, don't quietly pick a
> side. Keep this file current when you change behavior: a stale source of truth is worse than none.

---

## 0. What CoBirb is

A **privacy-first, Copilot-like agentic CLI**. It behaves like a coding agent, then subtracts every
default network/telemetry behavior and adds a hard privacy boundary around the rest.

> **No data leaves the CPU process unless the user explicitly opts a capability in.**

Noah the African Grey parrot is the mascot, and an *opt-in* persona — not the default voice.

| | GitHub Copilot CLI | CoBirb |
|---|---|---|
| Model default | Cloud-hosted | User-configured; local (Ollama) is the default path |
| Network | On by default | Off by default; opt-in per capability |
| Telemetry | Sends usage/analytics | None. Period. |
| Sessions | Encrypted + stored | Encrypted + stored, gated by a password |
| Personality | Neutral assistant | Optional personas; "Noah" the African Grey |
| Architecture | Agent runtime + plugins | Thin core + plugin SPI |

Status: **v0.1.0, working prototype.** Core loop, built-in tools, permissions, encrypted sessions,
local Ollama provider, and the full-screen interactive app are implemented and tested.

---

## 1. The ironclad constraints (do NOT violate these)

Design constraints, not features. They cannot be silently disabled.

1. **Zero telemetry.** No analytics, no crash reports, no pings, no usage collection of any kind.
   The process is a silent guest.
2. **No outbound network by default.** The model layer is the only capability that *may* touch the
   network, and only when the user explicitly configures a provider. Local (Ollama) is the default
   path. A remote provider is a plugin, never a core default.
3. **Never echo the password.** `--password`/`-w` with no value reads from stdin without echo. It
   is never logged, never written to the session file or config, and never retained on an object
   for longer than the call that needs it.
4. **Sessions encrypted at rest.** AES-256-GCM keyed via scrypt over the password. The plaintext
   session file never exists on disk. (No post-quantum KEM — see §7.3 for why one doesn't apply.)
5. **Default-deny permissions.** Any capability that reads, writes, or executes requires explicit
   opt-in. There is no "trust this folder" magic that widens scope. (See §8 for what the shipped
   default policy actually pre-approves — this is narrower than the slogan.)
6. **Everything local.** No cloud sessions, no remote control, no background agents.

If a feature idea conflicts with any of these, it is **out of scope** — park it, don't build it.

---

## 2. Working in this repo

### 2.1. Setup, run, test

```bash
pip install -e ".[dev]"          # Python 3.11+; the repo uses .venv/

cobirb                           # interactive: the full-screen Textual app
cobirb -p "list the files here" --allow-tool=list_dir   # one-shot, pipeable
cobirb help  /  cobirb help <topic>                     # session persona plan model plugins tools config

pytest                           # the whole suite
pytest --cov=cobirb --cov-report=term-missing
COBIRB_TEST_MODEL=llama3.1 pytest -m integration        # hits a real local Ollama
```

CI (`.github/workflows`) runs `pytest` on 3.11 and 3.12 for every push to `main` and every PR.
Baseline to hold: **all tests green, ~97% line coverage.** There is no linter, formatter, or type
checker configured — match the surrounding style by hand.

### 2.2. Layout

| Path | Responsibility |
|---|---|
| `src/cobirb/cli.py` | Argument parsing, help text, one-shot mode — **and** application composition (see §12, this is a known structural problem). |
| `src/cobirb/orchestrator.py` | The agent loop. Drives model ↔ tools, policy-gated. |
| `src/cobirb/policy.py` | Permissions, shell-command scanning, audit log. |
| `src/cobirb/session.py` | `Turn`/`Session` data, encrypted `SessionManager`, session discovery. |
| `src/cobirb/config.py` | Layered user + repo config reader. |
| `src/cobirb/typing/spi.py` | **The plugin contract.** Every SPI interface and shared dataclass. |
| `src/cobirb/plugins/loader.py` | Plugin discovery (entry points + local dirs), fail-closed. |
| `src/cobirb/plugins/core/` | Built-in plugins: `tools`, `model`, `io`, `crypto`, `persona`, `render`. |
| `src/cobirb/personas/*.json` | Bundled persona data (`professional`, `neighbor`, `kawaii`). Noah is built in code. |
| `src/cobirb/tui/` | Interactive mode: `app`, `widgets`, `screens`, `panes`, `io_bridge`, `app.tcss`. |
| `tests/` | One file per module. `conftest.py` isolates `COBIRB_HOME` for every test. |

### 2.3. Conventions

- **Every module and non-trivial function carries a docstring that explains *why*, not just what.**
  This is the house style and the main reason the codebase is navigable — keep it up. When you fix
  a subtle bug, the reason it was a bug belongs in the docstring at that spot.
- `from __future__ import annotations` at the top of every module.
- **Fail-closed, never crash.** A broken plugin is reported and skipped; a tool that raises becomes
  a failed `ToolResult` the model can read and correct from; an I/O adapter that cannot ask for
  approval denies. The many broad `except Exception` blocks are deliberate and each is justified in
  a comment — do not "clean them up" without reading the comment.
- **Permission granularity:** match by tool name by default; narrow `shell` per command segment.
  See §8.
- **Plugin loading:** core depends on the SPI; plugins depend on nothing but the SPI. Discovery is
  lazy, cached per run, and fail-closed — a broken plugin never bricks the core.
- **Keep the core thin.** If you are adding "core" logic for a feature, that feature is probably a
  plugin. This is the #1 architectural decision — don't erode it.
- **Don't let persona data influence behavior or permissions.** Ever. See §6.

---

## 3. Architecture

```
                      USER (terminal)
                          │
                          ▼
             ┌────────────────────────┐
             │   CoBirb Core          │   ← thin, stable runtime
             │   (Orchestrator)       │
             └───────────┬────────────┘
                         │
      ┌──────────────────┼──────────────────┐
      ▼                  ▼                  ▼
Model Provider      Tool Framework      I/O Adapters
 (pluggable)         (pluggable)         (pluggable)
      │                  ├─ shell (highest privilege, gated)
      │                  ├─ read / write / edit / apply_patch
      │                  ├─ glob / grep / list_dir
      ▼                  └─ custom tools (plugins)
 LLM inference
 (local Ollama, or a user-configured
  OpenAI-compatible endpoint)
      │
      ▼
 Session Store (AES-256-GCM + scrypt)

Everything above is bounded by:
 Permissions / Audit / Local-only Policy Layer
```

### 3.1. The core is intentionally thin

The core contains **no feature business logic**. It wires together:

- a **model provider** — produces turns from a system prompt + context;
- a **tool framework** — exposes callable capabilities to the model;
- an **I/O layer** — rendering, input, and future speech/vision adapters;
- a **policy layer** — permissions, audit, local-only enforcement;
- a **session manager** — encrypted local persistence.

This separation is what lets speech, vision, MCP and the rest arrive as plugins without touching
core.

---

## 4. The agent loop

```
Prompt → understand → inspect → plan → act → observe → reason → iterate → validate → report
```

- **understand** — parse the request. **inspect** — `glob`/`grep`/read to learn the repo.
  **plan** — decide the approach. **act** — call tools. **observe** — read tool output.
  **reason** — decide the next step. **iterate** — loop until the objective is met.
  **validate** — run tests/linters/builds if relevant. **report** — summarize.

By default all of this happens **implicitly in one continuous model-driven loop**
(`Orchestrator._loop`): the model plans, acts and checks its own work in the same pass, the way a
human working through a task would, with no hard stop between thinking and doing. The loop ends
when the model returns a plain-text reply with no tool calls (that reply becomes
`Session.summary`), or after `max_turns` (default 8).

### 4.1. Plan mode

`plan_mode` in config, `--plan-mode on|off`, or `/plan on|off` mid-conversation. Off by default.
Turned on, the single loop is bracketed into three phases, each recorded as its own `Turn` tagged
with `Turn.phase`:

1. **plan** — one model call with **no tools offered** (`_run_plan_phase` passes `tools=[]`), so it
   can only think out loud. Shown to the user immediately.
2. **act** — the normal tool-using loop, following that plan.
3. **validate** — a bounded tool-using loop (`max_turns=4`) that re-reads files, re-runs tests and
   reports with concrete references whether the request was actually fulfilled. Stored as
   `Session.validation`.

`Turn.phase` is purely descriptive — it never changes how a turn is replayed into context, but it
*is* covered by the turn hash (§7.4), so a "plan" turn can't be silently relabeled "validate".

Plan mode costs at least one extra model call per turn. In plan mode the act phase's answer is
rendered by `run()` itself rather than by the caller, so the user reads the answer *before* the
validation of it.

---

## 5. Plugin SPI

**This section is the implementation-facing contract.** The core discovers plugins and invokes them
only through these interfaces, never through their internals.

Rules of engagement:

- **Core depends on the SPI.** Core imports the interfaces in `typing/spi.py`.
- **Plugins depend on nothing but the SPI.** Plugins never import other plugins.
- **No coupling beyond interfaces.** Core cannot rely on a plugin's module layout beyond its
  declared entry point.

### 5.1. Discovery

Sources, in order:

1. **Built-in core plugins** — always available, imported directly by `cli.py`, never via entry
   points, so they work even if discovery finds nothing or fails entirely.
2. **Local project plugins** — `$COBIRB_PROJECT_DIR/cobirb/plugins/<name>/`.
3. **User plugins** — `$COBIRB_HOME/.cobirb/plugins/<name>/`.
4. **Installed packages** — Python entry points in the plugin's `pyproject.toml`:

   ```toml
   [project.entry-points."cobirb.plugins"]
   my-model-provider = "my_plugin.provider:MyProvider"
   my-tool           = "my_plugin.tools:MyTool"
   my-io             = "my_plugin.io:MyAdapter"
   ```

   The value is `module.path:ClassName`. The core decides which slot a class fills by **inspecting
   its class hierarchy** against the SPI base classes, not by the entry-point name.

Discovery is lazy and cached per run. Failure to load a plugin is **non-fatal**: the error is
reported (stderr for CLI modes, the Plugins tab for interactive mode) and the core continues. A
broken speech plugin can never brick the core.

### 5.2. How the core merges what it discovers

`load_plugins()` runs once per orchestrator build. The result is merged two different ways, matching
how much trust each type needs:

- **Tools are additive.** Every discovered `Tool` is instantiated and registered alongside the
  built-ins — the permission policy still gates whether it can *run* (§8), so no extra trust
  decision is needed to make it visible. A plugin can never shadow an existing tool name: a
  collision with a different implementation is skipped and reported. One real plugin discovered
  *twice* under different keys (installed entry point **and** local directory) is recognised as a
  harmless duplicate by comparing the registered class, not just the name.
- **Model, I/O and crypto are singleton slots.** Swapping the core default out is **opt-in**: set
  `plugins.model` / `plugins.io` / `plugins.crypto` in config to the plugin's discovered name.
  Leaving it unset — or setting it to `core-model`/`core-io`/`core-crypto` — keeps the core default,
  which is built with its *normal* constructor arguments (model name, base URL) rather than a bare
  no-argument construction. Naming a plugin that wasn't discovered is reported and the core default
  is kept.

Problems never abort a run.

### 5.3. Interfaces

All of these live in `src/cobirb/typing/spi.py` and are re-exported from `cobirb/__init__.py`.

```python
class ModelProvider(abc.ABC):
    def name(self) -> str: ...                      # e.g. "ollama/llama3.1"
    def chat(self, system: str, context: str,
             tools: Optional[list[Tool]] = None, *,
             stream: bool = False) -> "Iterable[str] | str": ...
    def parse_tool_calls(self, raw: str) -> list[ToolCall]: ...
    def supports_tool_calling(self) -> bool: ...
    def supports_streaming(self) -> bool: ...
    def supports_vision(self) -> bool: ...

class Tool(abc.ABC):
    def name(self) -> str: ...                      # short machine name, used for allow/deny matching
    def description(self) -> str: ...
    def parameters(self) -> dict[str, Any]: ...     # JSON-schema-ish
    def execute(self, arguments: dict[str, Any]) -> ToolResult: ...

class I_OAdapter(abc.ABC):
    def name(self) -> str: ...
    def render(self, text: str) -> None: ...        # called once per streamed token — no trailing newline
    def listen(self) -> Optional[str]: ...          # non-text input channel; None if none
    def view(self, data: bytes, mime: str | None = None) -> None: ...
    def confirm(self, tool_name: str, arguments: dict[str, Any]) -> str: ...

class SessionCrypto(abc.ABC):
    def name(self) -> str: ...                      # e.g. "aes256gcm-scrypt"
    def encrypt(self, plaintext_json: str, password: str) -> bytes: ...
    def decrypt(self, blob: bytes, password: str) -> str: ...   # raises on wrong password/corruption

@dataclass
class ToolCall:   name: str; arguments: dict[str, Any]
@dataclass
class ToolResult: ok: bool; content: str; error: Optional[str]; meta: dict[str, Any]
@dataclass
class Persona:    name, species, tone, greeting, phrasings, emoji_density, known_squawks
```

**Contract notes**

- `context` is the full conversation history serialized to a compact string; the provider decides
  how to unpack it. The core serializes it as JSON (`Orchestrator._build_context`) and
  `LocalModelProvider._build_messages` parses it back into a proper role-tagged `messages` array.
  *This matters:* flattening the history into one opaque user message was the root cause of the
  tool-calling loop never converging — a `tool` result appeared with no assistant turn requesting
  it, so the model had no signal a call was already satisfied and simply repeated it.
- `confirm()` must return exactly `"once"`, `"always"` or `"deny"`. An adapter with no way to ask
  **must** return `"deny"` — permission is fail-closed, not fail-open.
- **Remote providers are not core.** They are plugins, gated behind the permission layer.
- **`list_models()` is optional and duck-typed**, not part of the abstract contract. The core
  provider implements it against the endpoint's OpenAI-compatible `GET /v1/models` route (not
  Ollama's native `/api/tags`), so it also works against llama.cpp, vLLM, LM Studio and friends.
  Only interactive mode calls it, and only via `cli._build_model(None, cwd)` — so a `plugins.model`
  selection that lacks it breaks nothing; `/model` just isn't meaningful for that provider.
- **Duck-typed rendering hooks.** Beyond the `I_OAdapter` contract, both shipped adapters expose
  `spinner`, `begin_stream`, `render_header`, `render_answer`, `render_plan`, `render_validation`,
  `render_tool_call` and `write_error`. The orchestrator and CLI reach for these via
  `getattr(io, "...", None)` and fall back to plain `render()` when absent, so a future speech or
  vision adapter is never forced to implement chrome it has no use for.
- ⚠️ **`Tool.name` is contested.** The SPI declares it a *method*; all eight built-ins implement it
  as a plain `str` class attribute. Consumers therefore branch on
  `tool.name() if callable(tool.name) else tool.name`. One consumer (`plugins/core/model.py`'s
  `_tool_schema`) does **not**, which breaks every turn for a spec-conformant plugin. Unresolved —
  see §12.

### 5.4. Built-in tools

| Name | Does | Notes |
|---|---|---|
| `read_file` | Read a file. | |
| `write_file` | Create/overwrite a file. | Creates parent directories. |
| `edit_file` | Replace an exact `old_str` with `new_str`. | First occurrence only; exact match required. |
| `apply_patch` | Apply a unified diff. | Verifies context/removed lines against the file and refuses rather than guessing. |
| `glob` | Find files by pattern. | Skips vendor/cache dirs unless `include_ignored`. |
| `grep` | Search contents by regex. | Same skip list. |
| `list_dir` | List a directory. | |
| `shell` | Run a shell command. | **Highest privilege; gated.** Own process group on POSIX, so a forked/backgrounded child can be torn down as a whole. `cancel_running()` is what lets the TUI's Ctrl+C interrupt it. |

All built-ins extend `CobirbTool`, whose `_resolve()` joins a relative path against the configured
working directory — without it a model's `"src/foo.py"` would resolve against the *process's* cwd
instead of `--cwd`.

### 5.5. Packaging a plugin

A local plugin is a directory:

```
cobirb/plugins/<name>/
├── pyproject.toml          # [project.entry-points."cobirb.plugins"]
└── src/<name>/__init__.py  # exports the SPI-implementing classes
```

Plugins are versioned by their package; the core loads any version and surfaces mismatches
non-fatally.

### 5.6. Security implications of the SPI

- **Least privilege by default** — a plugin that adds a powerful tool inherits the same gate as
  `shell`.
- **No auto-approval from plugins** — a plugin cannot pre-approve its own tools. The permission
  layer is the sole authority.
- **Fail-closed on trust** — a dangerous plugin tool still requires approval.
- **Audit survives plugins** — every tool call, plugin-provided included, goes to the audit log
  when it is enabled.

### 5.7. Testing the SPI

- Unit tests per built-in tool (`tests/test_tools.py`).
- Provider contract test: a stub `ModelProvider` returning canned turns including a tool call,
  to verify the loop wires tools correctly (`tests/test_orchestrator.py`).
- Crypto round-trip: encrypt → tamper → decrypt fails; wrong password raises
  (`tests/test_crypto.py`).
- Plugin load test: a sample local plugin loads without core changes, and breaking it does not
  crash the core (`tests/test_loader.py`).
- Live integration against a real Ollama, skipped unless `COBIRB_TEST_MODEL` is set
  (`tests/test_integration_ollama.py`).

---

## 6. Personas

**Personas are OFF by default.** A persona is a costume put on the model — a name, a species, a
tone, stock phrases — and CoBirb sends it as a system message, which *replaces* whatever `SYSTEM`
directive the local model's own Modelfile sets. Wearing one by default would silently override a
configuration the user built deliberately, on their own machine. An unconfigured run therefore
sends no voice instructions at all and the model sounds like itself.

- `build_plain_persona()` is what an unconfigured run gets: name `"CoBirb"` and every voice-bearing
  field empty. `persona_shapes_voice()` checks exactly that and is the signal to emit no persona
  block.
- **Bundled:** `noah` (built in code, `build_default_persona()`), plus `professional`, `neighbor`
  and `kawaii` as JSON under `cobirb/personas/`. The shortlist those three came from: professional
  and to the point, inspired by Japanese business courtesy; the friendly neighbour who hosts the
  BBQ and dogsits for free; the kawaii little sister, safe for work but very UwU.
- **Resolution order** for a name: bundled personas, then `./<name>.json`, then the user's CoBirb
  home. An unknown name falls back to *no* persona, never to Noah — a typo must not quietly dress
  the model in a character nobody asked for.
- Sessions record `cli._persona_key()` — the name that *reloads* the persona — not the display
  name, because `kawaii.json` calls itself something else and `_load_persona("Imouto")` would find
  no file.
- **Tone:** friendly and playful, **not** saccharine. Light puns, no excessive sparkle.
- **Hard rule:** persona data shapes *how* CoBirb speaks and nothing else. It must never be able to
  instruct the agent to skip permissions, disable encryption, or reach the network. The composed
  system prompt says so explicitly, and no persona field can override it.

Persona file shape:

```json
{
  "name": "Noah",
  "species": "African Grey Parrot",
  "tone": "friendly, playful, not saccharine",
  "greeting": "Squawk! Noah here — what shall we build today?",
  "phrasings": ["Great idea!", "I'll take a look.", "Consider that done."],
  "emoji_density": "light",
  "known_squawks": ["Ha-ha!", "Done-done!", "Not today."]
}
```

### 6.1. The model's own system prompt wins

Ollama accepts one system message per request, and sending one **replaces** the model's built-in
`SYSTEM`. So:

- **By default CoBirb sends no system message at all** (`--system-prompt off`). The model behaves
  inside CoBirb exactly as it does in `ollama run`.
- When CoBirb *does* have something to add — a persona, plan-mode phase instructions, or
  `--system-prompt harness` — `LocalModelProvider.compose_system()` reads the model's own prompt
  back via `/api/show` and places it **first**, then appends CoBirb's part. Yours is supplemented,
  never discarded.
- The `harness` block is a short, deliberately operational note that tool calls are gated by a
  human-answered prompt. Its one concrete benefit: a model that doesn't know a denial is a decision
  will retry a blocked tool until the loop gives up. It is off by default because it is still an
  override.
- None of CoBirb's guarantees depend on any of this. Permissions are enforced in `policy.py` and
  sessions are encrypted by the crypto backend — never by asking a model to cooperate.

---

## 7. Sessions & encryption

### 7.1. Threat model

- The plaintext conversation must never sit on disk in recoverable form.
- The unlock password must never be echoed or logged.
- The scheme must resist offline brute-forcing of a stolen session file.

### 7.2. Scheme

```
session file on disk  =  base64( salt ‖ nonce ‖ AES-256-GCM(plaintext, key) )
                                                             │
                            key = scrypt(password, random salt)   ← RAM only
```

- **Bulk cipher:** AES-256-GCM (authenticated), AAD `b"cobirb"`.
- **KDF:** scrypt, RFC 7914 interactive parameters (N=2¹⁴, r=8, p=1), fresh random salt per
  encryption.
- **Implementation:** the vetted `cryptography` library, never hand-rolled. The core ships the
  *interface*; the backend is a swappable plugin (§5.2).
- Salt and nonce are not secret and travel with the ciphertext. The password derives the key once
  and is then discarded — `SessionManager` deliberately does not retain it; `save()`/`load()` take
  it per call.

### 7.3. No post-quantum KEM — deliberately

An earlier design called for sealing the key with ML-KEM-768 (Kyber). A KEM protects a shared
secret two parties establish over a public key, defending against a future quantum computer
breaking today's RSA/ECC key exchange ("harvest now, decrypt later"). **A password-protected local
session file has no such exchange** — one user, one password, no counterparty, no stored public
key. AES-256 is already quantum-resistant for this case (Grover only halves its effective strength,
leaving 128 bits), and a KEM re-derived from the same password wouldn't raise the cost of a
password-guessing attack: an attacker would redo the KEM math per guess just as cheaply as scrypt.
Shipping one here would look like a security feature without being one.

**Do not resurrect this** without a real asymmetric use case — e.g. encrypting to a specific
device's stored public key for multi-device sync.

### 7.4. Session file format

The JSON below is the **plaintext** shape, which exists only in RAM after decryption. On disk it is
the encrypted blob from §7.2.

```jsonc
{
  "format": "cobirb-session",
  "schema": 1,
  "created_at": "2026-09-11T...Z",
  "working_dir": "/abs/path",
  "persona": "none",
  "turns": [
    { "role": "user",      "content": "...", "tool_use": null,  "phase": null, "ts": "...", "hash": "..." },
    { "role": "assistant", "content": "...", "tool_use": [...], "phase": null, "ts": "...", "hash": "..." },
    { "role": "tool",      "content": "...", "tool_use": [...], "phase": null, "ts": "...", "hash": "..." }
  ],
  "summary": null,
  "validation": null
}
```

- Roles are `user`, `assistant` and `tool`.
- **Each turn carries a content hash** so tampering is detectable on reload
  (`SessionManager._verify_hashes`). The digest covers `role`, `content`, `tool_use` **and**
  `phase` — a hash over the prose alone would happily accept a session whose recorded `read_file`
  call had been rewritten into a `shell` one.
- `validation` is set only by a plan-mode run.
- Sessions live under `$COBIRB_HOME/.cobirb/sessions/` by default; `--session <path>` works
  anywhere.
- Saved after every turn (interactive) or once at the end (one-shot). On exit the exact command
  that reopens the session is printed — with `-w` and no value, so the password never reaches the
  terminal or scrollback.

---

## 8. Permissions & audit

- **Default-deny** is the rule: nothing runs unless explicitly allowed and never denied. A deny
  list always wins.
- **In practice the shipped default policy pre-approves a working set** for convenience, matching
  how Copilot CLI's own ask/execute mode behaves: `build_default_policy()` →
  `Policy.allow_all_core_tools()` allows the seven file tools outright, and allows the binaries
  `git ls cat grep mkdir find pytest` with any arguments, plus the exact prefixes
  `python --version`, `python -m pytest`, `python -m cobirb`. `python` is deliberately **not**
  trusted outright, because `python -c '...'` runs arbitrary code. Anything outside that set is
  genuinely default-deny. ⚠️ See §12 — this set is wider than it looks and is under review.
- **Approval granularity:** per tool name, or per tool with narrow arguments. Two kinds of `shell`
  rule exist:
  - **first-word** — the bare binary is trusted with any arguments (`git` also allows `git push`).
  - **prefix** — a specific multi-word invocation is trusted (`python -m pytest` does **not** allow
    `python -c`). This is what `allow(tool, command)` produces for a multi-word command; narrowing
    to the first word would defeat the point.
- **Every segment is checked.** A shell command may chain several invocations
  (`git status; rm -rf /`) and the shell runs all of them, so `Policy._segments` splits on `;`,
  `|`, `&` and requires **every** segment to be permitted. Quoting is respected, so a separator
  inside a quoted argument does not split the command. Redirection (`>`, `>>`, `<`) stays attached
  to the command it belongs to. Anything the scan cannot verify — command substitution, backticks,
  a subshell, an unbalanced quote — is **refused**, because the shell would execute the whole
  string and what can't be checked can't run.

  The original motivation: allowing `bash` outright is unwise, but `bash -n` (syntax check only) is
  fine; likewise `python -m pytest` is safe where bare `python` is not.
- **Interactive approval.** A tool call the static list doesn't cover is not silently refused — the
  I/O adapter's `confirm()` asks, and the answer is `"once"`, `"always"` (extends the live policy
  for the rest of this run) or `"deny"`. This is what makes "default-deny" mean *asks first* rather
  than *the model never finds out it could have worked*. No adapter, or one that can't ask, fails
  closed.
- **Audit log** — append-only, local, never leaves the machine, and **off by default**
  (`"audit_log": true` to enable). Off because the arguments logged are whatever a tool call
  actually carried, unredacted: `write_file`'s full content, `edit_file`'s full old/new text,
  `apply_patch`'s full diff, `shell`'s full command. An always-on log would be a second, plaintext,
  unencrypted, never-rotated copy of everything written or run — directly contradicting sessions
  being encrypted at rest. Nothing is written and no file is created until it is turned on.

---

## 9. CLI & interactive surface

The two modes have deliberately different surfaces, because they are used differently.

### 9.1. One-shot / programmatic

`cobirb -p "..." --allow-tool='shell(git)'` — run one task then exit. Plain stdout that pipes and
scripts, rendered by `TerminalIO`. **Deliberately not the full-screen app**: a full-screen app
cannot be piped, so the split is the point rather than a gap. `--allow-tool` takes `name` or
`name(arg)` and is repeatable.

### 9.2. Interactive — a full-screen Textual app

`cobirb` with no `-p` opens `cobirb/tui/`. Adopting **Textual** is an accepted, explicit exception
to the "few dependencies" stance: a persistent tab bar, fixed layout regions, a boxed input and a
modal need a real TUI framework, not more `rich` polish. It is imported lazily, so one-shot mode
never loads it.

Three live tabs — none are placeholders:

- **Current** — the conversation. A live status line (persona · model · plan mode · cwd · session),
  kept current as `/persona`, `/plan` and `/model` change it. A boxed input that greys out while a
  turn runs and comes back when it finishes — there is no `continue? [y/N]` gate any more. Tool
  approval is a modal (`y` once / `a` always / `n` or escape deny).
- **Sessions** (`panes.py::SessionsPane`) — lists `.json` files under
  `session.default_sessions_dir()` with size/modified read **without decrypting**, and resumes one
  (a `TextPromptModal` collects the password — a full-screen app can't fall back to
  `getpass.getpass()`) or starts a new one. Resuming decrypts once to validate the password and
  read `persona`/turn count, discards any orchestrator already built (`self.orchestrator = None`),
  and switches persona/system prompt to match the session. The *next* message rebuilds the
  orchestrator through the exact same load-or-create path `--session` already uses, so **there is
  only one code path that ever opens a session file**.
- **Plugins** (`panes.py::PluginsPane`) — `cli.describe_plugins()`'s live snapshot: every
  registered tool with its source, which implementation is active per slot, and any discovery
  problems. These exist because a full-screen app's stderr is invisible to its user, and that is
  where the CLI modes report the same issues.

Commands: `/model`, `/persona [name]`, `/plan [on|off]`, `?` or `/help [topic]`.
Keys: `f1` help, `f2` next tab, `ctrl+q` quit, `up`/`down` recall the last 100 prompts (in memory
only — nothing typed is written to disk), `ctrl+c` copies a mouse selection if there is one and
otherwise cancels a running turn.

`/model` fetches the endpoint's list and opens a picker; choosing one hot-swaps
`orchestrator.model` in place rather than rebuilding, which preserves this session's "always allow"
approvals. `"default_model"` is validated at startup **silently** — an unavailable name is ignored,
not an error — and if nothing usable resolves, the app opens the same picker unprompted.

### 9.3. Threading — how a blocking orchestrator drives an async app

`Orchestrator.run()` stays **synchronous and untouched**. The app runs it on a Textual *thread
worker*, and `TuiIO` (`tui/io_bridge.py`) carries every callback back onto the UI thread via
`App.call_from_thread`. None of those callbacks may touch a widget directly.

`confirm()` is the interesting one: it must block the worker until a human answers and then return
the decision string synchronously into `Orchestrator._execute_tool`. It does so by having
`call_from_thread` schedule a coroutine that awaits `push_screen_wait` on the event loop, handing
the resolved value back across the thread boundary. It is deliberately *not* routed through
`TuiIO._call`'s same-thread shortcut — `push_screen_wait` only works inside a worker, and calling
it on the UI thread would deadlock the loop that has to draw and dismiss the modal.

**A real bug worth not repeating:** `TextPromptModal`'s `Input.Submitted` wasn't stopped, so it
bubbled past the modal to the App's own `on_input_submitted` — the chat box's handler, which cannot
tell one `Input` from another. Typing a session name or password and pressing enter *also* ran that
text as a chat prompt. Fixed with `event.stop()` in every modal-level handler that could reach an
App-level one of the same name. Do this in new modals too.

### 9.4. One render layer, two renderers

Both adapters build their panels from the *same* pure builder functions in
`plugins/core/render.py` — they return a Rich renderable and never touch a console. `TerminalIO`
prints them; `TuiIO` writes the very same objects into a `RichLog`. That is what keeps the
scrolling output and the transcript visually identical, and why the chrome is defined once instead
of drifting between the two. A config-selected `plugins.io` plugin replaces whichever adapter the
mode would have used — note that in interactive mode this means the *selected adapter*, not the
app, decides where output goes.

---

## 10. Configuration

Read from `$COBIRB_HOME/.cobirb/config.json`, then `<cwd>/cobirb.json`; **repo overrides user**,
merged deeply. Nothing here ever defaults to a networked provider. See `cobirb.json.example`.

| Key | Meaning |
|---|---|
| `default_model` | Model to use by default. Validated at interactive startup; an unavailable name is ignored, not an error. |
| `model` | Older, equivalent name for the same thing. |
| `models.default.name` / `.base_url` | Model name and endpoint. Any OpenAI-compatible server works. |
| `persona` | Persona to adopt by default. Unset or `"none"` means none. |
| `system_prompt` | `"off"` (default) or `"harness"` — see §6.1. |
| `plugins.model` / `.io` / `.crypto` | Select a discovered plugin for that slot; core default kept when unset. |
| `plan_mode` | Start with plan mode on (default `false`). |
| `audit_log` | Keep a local plaintext record of every tool call (default `false`) — read §8 first. |

Environment: `COBIRB_HOME` (relocates the whole `.cobirb` tree — this is how tests isolate),
`COBIRB_MODEL_NAME`, `COBIRB_OLLAMA_URL`, `COBIRB_PROJECT_DIR`, `COBIRB_TEST_MODEL`.

Resolution order for the model name: `--model` → `model` → `models.default.name` → `default_model`.
All three config keys name the same thing; the multiplicity is backward compatibility, not three
behaviors.

---

## 11. Key decisions & their rationale

- **Language:** Python 3.11+, in a `venv` (`.venv/`).
- **Thin core + plugin SPI.** The #1 architectural decision. Don't erode it.
- **Models are entirely user-configured.** A default *example* config ships for local Ollama; no
  models are embedded and no outbound calls happen by default.
- **Three runtime dependencies, each a decision:** `rich` (rendering), `cryptography` (vetted
  session crypto, required because the AES-GCM backend ships as a built-in core plugin rather than
  an opt-in extra), `textual` (interactive mode only, lazily imported).
- **Personas are data, never behavior injection**, and off by default (§6).
- **No PQ-KEM seal** (§7.3).
- **Textual for interactive, plain stdout for one-shot** (§9).
- **Audit log off by default** (§8).
- **`rich` for both renderers via shared builders** (§9.4).
- **`py.typed` ships** (PEP 561) with the `Typing :: Typed` classifier, so a consuming project's
  type checker reads the inline hints instead of treating `cobirb` as untyped.

---

## 12. Known gaps (audited, decisions pending)

Recorded so they aren't rediscovered or "fixed" while under discussion. Full write-up lives in the
Phase 2 audit.

- **`X1` — the default policy is wider than the docs claim.** `find . -exec rm -rf {} ;` passes the
  allow-list: `_segments` reads the `;` terminating the `-exec` clause as a command separator, so
  the exec'd program lands in an unchecked tail segment. And `git` trusted with any arguments
  includes `git push` — outbound network, unprompted. README and the `help` text still say "no tool
  runs without approval". **Decision pending on how wide the default set should be.**
- **`B1` — `_tool_schema` breaks spec-conformant tool plugins.** See §5.3. `json.dumps` raises on a
  bound method, so every turn fails while such a plugin is installed.
- **`B2` — malformed config JSON tracebacks** out of every command, including `cobirb help`.
- **`B3` — user personas are looked up in `~/cobirb/`**, while config, sessions, the audit log and
  plugins all live in `~/.cobirb/`.
- **`B4` — `session.py` still defaults `persona` to `"noah"`** in four places, left over from
  before personas became opt-in.
- **`S1`/`S2` — `cli.py` is the composition root.** It holds argument parsing *and* persona
  resolution, plugin slot selection, orchestrator construction and session resolution;
  `tui/app.py` imports twelve private `cli._*` functions because there is nowhere else for that
  wiring to live. The leading underscores are fiction.

---

## 13. Roadmap

- **v0.1.0 (current)** — core runtime, local model adapter, built-in tools, permission model,
  encrypted sessions, personas, CLI + interactive surface.
- **v0.2.0** — speech and vision I/O adapters (built on the existing adapter SPI); MCP integration
  (opt-in, add-only, never default); subagent parallelism.
- **v0.3.0** — custom agents & skills (config-first), live hooks.
- **v0.4.0** — plugin distribution.
- **Omitted forever** — cloud sessions, remote control, background agents, telemetry. They
  contradict the founding principle and are out of scope by design.

---

## 14. Glossary

- **Core** — the thin runtime that wires plugins together.
- **Plugin** — a Python package or directory implementing one or more SPI interfaces.
- **Turn** — a single user, assistant or tool message within a session.
- **Phase** — which stage of a plan-mode run produced a turn (`plan`/`act`/`validate`).
- **Persona** — a data file describing how CoBirb speaks. Never behavior.
- **Tool** — a capability exposed to the model.
- **Slot** — one of the three singleton plugin positions (`model`, `io`, `crypto`).
- **Session** — an encrypted, persisted conversation.
