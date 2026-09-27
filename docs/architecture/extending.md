# Extending CoBirb

The plugin SPI, what a project tells the model about itself, and the extension points a user
configures.

## Plugin SPI (`typing/spi.py`, `plugins/loader.py`)

- `SPI_VERSION = 1`, `MIN_SUPPORTED_SPI_VERSION = 1`, **frozen**: changes within a version are
  additive (optional duck-typed hooks yes; new abstract methods, renames, signature changes no). A
  plugin module declares `COBIRB_SPI = <int>`; absent means 1, non-integer is an error. Incompatible
  → `IncompatiblePlugin`, refused at the loader, non-fatally.
- Interfaces `Tool`, `ModelProvider`, `I_OAdapter`, `SessionCrypto`; data `ToolCall`, `ToolResult`,
  `ApprovalRequest`, `ApprovalOutcome`, `Persona` (unused since personas were removed; kept because
  the SPI is frozen), `SteeringInterrupted`, and the `once`/`always`/`session`/`deny` constants.
  `confirm_scoped`/`confirm_request` are optional hooks tried richest first; anything unrecognised is
  deny.
- Entry-point group `cobirb.plugins`. **Tools are additive**; **model/io/crypto are singleton slots**
  replaced only when named in `plugins.<slot>`. Local plugins live in `~/.cobirb/plugins/<name>/` as a
  distribution named `cobirb_plugins_<name>`; `cobirb plugin install` automates that and never
  fetches. Discovery is fail-closed per plugin.
- **Open question:** `_plugin_dirs()` also looks in `<project>/cobirb/plugins/` (`COBIRB_PROJECT_DIR`,
  default the working directory). A plugin there loads only if its distribution is installed, so a
  repository cannot bring new code — but it can switch on an installed plugin, which sits badly with
  invariant 2. Undecided; do not build on it.

## Project grounding

- `runtime/instructions.py` reads the **first** of `AGENTS.md`, `CoBirb.md`, `COBIRB.md` in the
  working directory only (no walking up, no nested files), capped at `instructions_max_chars`
  (default 32000) with truncation announced.
- `plugins/core/repomap.py`: a ranked outline (Python via `ast`, regexes elsewhere),
  `repo_map_max_chars` (default 16000), built once when the orchestrator is built **and** exposed as
  the `repo_map` tool for a subtree or a refresh.
- Both compose into `Orchestrator.project_context` (`wiring._project_context`), on every request's
  system prompt. A repository may describe itself this way but never grant capability (invariant 2).

## User extension points

| Mechanism | Shape | Notes |
|---|---|---|
| Hooks (`runtime/hooks.py`) | `before_tool`, `after_tool`, `before_turn`, `after_turn` | JSON on stdin, 30 s timeout. A non-zero `before_tool` **blocks** the call; its output is the model's reason. Others observe; failures never fatal. |
| Verify (`runtime/verify.py`) | `verify_command` | Off unless set, never guessed. Runs outside the permission layer (the user's own config). 120 s, one fix attempt by default. |
| Custom commands (`runtime/custom_commands.py`) | `~/.cobirb/commands/*.md`, `<project>/.cobirb/commands/*.md` | `/name` sends the body; `$ARGUMENTS`, `$1`…`$9`; optional `description` frontmatter. A prompt, never a capability. |
| MCP (`mcp/`) | `mcp_servers`, **stdio only** | Tools as `mcp__<server>__<tool>`, same policy/audit/redaction path. Env **not** inherited (only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` + configured `env`, unless `inherit_env`). "Always" grants that one tool. |
| Model roles (`runtime/models.py`) | `models.default` / `.orchestrator` / `.worker` | See [providers](providers.md). |
| System prompt (`runtime/system_prompt.py`) | `system_prompt`: `off` \| `harness` | Off by default. `harness` is a short working-method block after the model's own `SYSTEM` — measured no better (49/60 either way on the harder benchmark tasks), so opt-in. |
