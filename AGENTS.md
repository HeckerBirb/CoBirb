# AGENTS.md — CoBirb

> **Single source of truth for this repo.** Absorbed the former `DESIGN.md` and `PLUGIN_SPEC.md`.
> If code and this file disagree, that's a bug in one of them — say which. Keep it current.

---

## 1. What CoBirb is

A **privacy-first, Copilot-like agentic CLI**: Copilot CLI behavior, minus every default
network/telemetry behavior, plus a hard boundary around the rest. **No data leaves the process
unless the user opts a capability in.** Noah the African Grey is the mascot and an *opt-in* persona,
not the default voice. Status: **v0.2.0, the beta.** Loop, tools, permissions, encrypted sessions, Ollama provider and
the interactive app, plus context compaction, project instructions, `.gitignore` awareness,
diff-before-write, `/undo`, `/export` and a headless CI mode.

## 2. Ironclad constraints (never violate)

1. **Zero telemetry.** No analytics, crash reports, or pings.
2. **No outbound network by default, and no shipped remote provider — ever.** Only the model
   layer may touch the network, only when the user configures a provider, and CoBirb will never
   package or bless one. Decided September 2026: local models only, forever. The SPI still lets a
   third party write a remote provider; that is their choice to make and ours to not make for
   them. **This shapes engineering everywhere else:** everything must work well against a 7B model
   on a 4096-token window, which is why compaction, grounding and small clean contexts matter more
   here than they would in a tool aimed at frontier models.
3. **Never echo the password.** `-w` with no value reads stdin without echo; never logged, stored,
   or retained past the call that needs it.
4. **Sessions encrypted at rest.** Plaintext never hits disk. No PQ-KEM — §7.
5. **Default-deny permissions.** Nothing is pre-approved; every capability comes from the user.
   The one breadth granted on a single "yes" is a *read* directory (§8).
6. **Everything local.** No cloud sessions, remote control, or background agents.

Feature ideas conflicting with these are out of scope. Park them.

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
| `config.py` | Layered user + repo config reader. |
| `typing/spi.py` | **The plugin contract.** All SPI interfaces and shared dataclasses. |
| `checkpoints.py` | Pre-edit snapshots behind `/undo`. |
| `context.py` | Fitting the history into the model's window. |
| `paths.py` | Every path under `~/.cobirb`. Derived in one place, on purpose. |
| `plugins/loader.py` | Discovery (entry points + local dirs), fail-closed. |
| `plugins/core/` | Built-ins: `tools`, `model`, `io`, `crypto`, `persona`, `render`. |
| `personas/*.json` | `professional`, `neighbor`, `kawaii`. Noah is built in code. |
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
| `read_file` | |
| `write_file` | Creates parent directories. |
| `edit_file` | Exact `old_str` match, first occurrence only. |
| `apply_patch` | Unified diff; verifies context/removed lines and refuses rather than guessing. |
| `glob`, `grep` | Skip vendor/cache dirs unless `include_ignored`. |
| `list_dir` | |
| `repo_map` | Ranked outline of the codebase — see §4e. |
| `shell` | **Highest privilege; gated.** Own process group on POSIX so forked children die with it; `cancel_running()` backs the TUI's Ctrl+C. |

All extend `CobirbTool`, whose `_resolve()` joins relative paths against the configured working
directory — otherwise a model's `"src/foo.py"` would resolve against the *process's* cwd, not
`--cwd`.

### 5.4 Packaging & testing

A local plugin is `cobirb/plugins/<name>/` with a `pyproject.toml` declaring entry points and
`src/<name>/__init__.py` exporting the classes. Any version loads; mismatches surface non-fatally.

Tests: per-tool units (`test_tools.py`), a stub-provider loop contract (`test_orchestrator.py`),
crypto round-trip incl. tamper and wrong-password (`test_crypto.py`), plugin load + broken-plugin
survival (`test_loader.py`), live Ollama behind `COBIRB_TEST_MODEL` (`test_integration_ollama.py`).

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
is trust, not enforcement, and it is the largest remaining gap between the pitch and the code. The
answer is the embedded GGUF runtime at v0.4.0 (`libllama` in-process — no daemon, no socket, no
registry), which lands as a `ModelProvider` plugin behind an optional extra. Until then, say so
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
  that greys out mid-turn and returns when done (no `continue? [y/N]` gate), tool approval as a
  modal (`y` once / `a` always / `n`/escape deny).
- **Sessions** — lists `.json` files under `default_sessions_dir()` with size/mtime read **without
  decrypting**; resumes one (a `TextPromptModal` collects the password, since a full-screen app
  can't use `getpass`) or starts a new one. Resuming validates the password, reads persona/turn
  count, sets `self.orchestrator = None`, and lets the *next* message rebuild through the same
  load-or-create path `--session` uses — so **only one code path ever opens a session file**.
- **Plugins** — `cli.describe_plugins()`'s live snapshot: registered tools with source, active
  implementation per slot, discovery problems. It exists because a full-screen app's stderr is
  invisible, and that's where CLI modes report the same issues.

Commands `/model`, `/persona [name]`, `/plan [on|off]`, `?`/`/help [topic]`. Keys: `f1` help, `f2`
next tab, `ctrl+q` quit, `up`/`down` recall the last 100 prompts (memory only), `ctrl+c` copies a
selection if there is one else cancels the turn. `/model` hot-swaps `orchestrator.model` in place
rather than rebuilding, preserving this session's "always allow" approvals. `default_model` is
validated at startup *silently* — unavailable is ignored, not an error — and if nothing resolves the
picker opens unprompted.

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

`$COBIRB_HOME/.cobirb/config.json`, then `<cwd>/cobirb.json`; **repo overrides user**, merged
deeply. Nothing defaults to a networked provider. See `cobirb.json.example`.

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
| `plan_mode`, `audit_log` | Both default `false`. Read §8 before enabling the latter. |

Model name resolution: `--model` → `model` → `models.default.name` → `default_model`. All name the
same thing; the multiplicity is backward compatibility, not three behaviors.

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
- **v0.3.0 "Grounded"** — repo map, git integration, self-verification loop, secret redaction.
  Write-scope grants *(done, 0.2)* and session export *(done, 0.2)* landed early.
- **v0.4.0 "Extensible"** — MCP client (stdio only), **embedded GGUF runtime**, per-role model
  selection, hooks, custom commands/skills.
- **v0.5.0 "The Flock"** — subagent orchestration: charter → scaffold → fan-out → integrate.
- **v0.6.0–0.9.0** — plugin distribution, cross-session memory, vision, mid-turn steering,
  session branching, SPI freeze and session migrations.
- **v1.1.0+** — runtime isolation for the embedded model (subprocess, no network namespace).

**Time horizon.** CoBirb is built for the hardware of the next few years, not this one — local
models on ordinary machines will be considerably more capable in one to three years than they are
now. So **performance, memory and model-size figures are observations, never arguments.** Noting
that something is slow or large today is useful; letting that quietly pick a default, narrow a
feature, or rule an approach out is not. When a hardware consideration looks like it should change
the design, raise it with the user as a decision rather than resolving it in an estimate.
Designing to this year's ceiling is how a tool arrives obsolete.

### 12.1 Deferred, needing a decision

**Parallel read-only tool calls** was on the 0.2 list and is not built. Executing several reads
concurrently is a performance change whose benefit here is unproven — the model, not local disk
I/O, is the bottleneck — while the cost is real: turn ordering, and `ShellTool` holding per-call
process state. Per §12's time-horizon rule that is a decision to take deliberately rather than one
for an estimate to make quietly, so it is parked rather than dropped.

**Settled decisions.** *Local models only, forever* — no shipped remote provider, ever (§2), and
from 0.4 not even a local *server*: CoBirb runs the weights itself. The shell privilege gap is
documented rather than sandboxed (§8). Speech I/O is deliberately deferred past 1.0 — a large
platform-specific dependency for a workflow almost nobody uses on a coding agent. No
embedding-based RAG: a repo map plus grep beats it for code at a fraction of the machinery.

**Omitted forever:** cloud sessions, remote control, background agents, telemetry. They contradict
the founding principle. (Subagents are not background agents — they are local, in-process, and
bounded by a turn you asked for.)
