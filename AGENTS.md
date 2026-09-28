# AGENTS.md — CoBirb

The rules for agents editing this repository, and the one-line reason each rule exists. **How each
part works is in [`docs/architecture/`](./docs/architecture/README.md)** — before changing an area,
read its page (the layout table below says which). The longer story behind a decision is in
`CHANGELOG.md` and in the docstring next to the code — this file says *what holds*, not how it came
to.

## 1. What this is

CoBirb is a **privacy-first agentic coding CLI**: the capabilities people get from cloud-hosted coding
assistants, against models running on hardware the user controls, with no telemetry and no outbound
network unless the user explicitly asks for it.

- Version: `pyproject.toml` only (`cobirb.__version__` reads the installed metadata). Python ≥3.11,
  MIT. Entry point `cobirb = cobirb.cli:main`.
- Runtime deps: `rich`, `cryptography`, `textual` (imported lazily; one-shot never loads it),
  `websockets` (only for Remote Worker Birbs; imported only when one is used). Dev:
  `pytest`, `pytest-cov`, `pytest-asyncio`, `pytest-xdist`, `ruff`. Prefer stdlib; each dependency is a
  deliberate decision.
- Ships **no models**. It talks to one model server the user configures — Ollama's API by default, or
  any OpenAI-compatible server (`"api": "openai"`) — and only once a model is named.

## 2. Invariants — do not violate

1. **Default-deny, with one contained exception.** A fresh `Policy` permits nothing. Capability comes
   from an approval prompt, `allow_tools` / `allow_read_dirs` / `allow_write_dirs`, `--allow-tool`, or
   auto-pilot. There is no "approve everything" flag. The exception: a shell command **inside the
   sandbox** runs without asking by default — it can reach no network and write nothing outside the
   project — and only where whole-tree checkpoints can undo what it changed.
2. **One config file: `~/.cobirb/config.json`.** No repo-local config is read, merged or looked for. A
   repository may *describe* itself (`AGENTS.md`, repo map, `<project>/.cobirb/commands/`) but may
   never grant capability.
3. **No outbound network by default.** Only the model provider opens a socket, to the endpoint the
   user configured. Three exceptions, all explicitly configured or invoked: `cobirb --upgrade` (a
   git remote for a checkout; GitHub release assets plus PyPI for a managed install), MCP servers the
   user configured, and the Remote Worker Birbs in `remote_workers` (CoBirb connects; it never
   listens, except as `cobirb remote-worker` on the remote). CoBirb never probes or discovers endpoints — the config says what it may reach.
4. **Sessions are encrypted at rest.** Plaintext conversation exists in RAM only.
5. **Fail closed.** No I/O adapter, an adapter that cannot ask or raises, an unparseable shell command,
   a plugin that will not load, a target a permission check cannot pin down → deny or skip.
6. **Never crash on a recoverable fault.** Broken plugin → reported and skipped; tool raises → a failed
   `ToolResult` the model can correct from; broken config → reported, defaults used. Each broad
   `except Exception` carries a comment saying why.
7. **The model's own system prompt wins.** With nothing to add, CoBirb sends *no* system message, so
   the Modelfile `SYSTEM` stays in force. When it adds something, the model's own goes first.
8. **Every version bump gets a matching `vX.Y.Z` tag** — `--upgrade` resolves releases only from tags.

## 3. The hardware this targets

**Assume at least 16 GB of VRAM and a 128k context window.** Users have capable machines or rent GPUs;
two models at once, or several subagents, is reasonable. **Performance, memory and model-size figures
are observations, never arguments**: note that something is slow or large, but do not let it quietly
pick a default, narrow a feature or rule an approach out — raise it as a decision. (Compaction was once
sized for a 4096-token window, and that one assumption made four wrong calls.)

## 4. Layout

Code paths are under `src/cobirb/`; the rest are from the repository root.

| Path | Responsibility | Page |
|---|---|---|
| `cli.py` | argparse, mode dispatch, one-shot run. No wiring logic. | [surfaces](docs/architecture/surfaces.md) |
| `orchestrator.py` | The agent loop: model ↔ tools, policy-gated, stop reasons, plan mode, steering. No feature logic. | [loop](docs/architecture/loop.md) |
| `context.py` | Fitting history into the window (compaction). | [loop](docs/architecture/loop.md) |
| `policy.py`, `sandbox.py`, `patches.py` | Permissions, shell-command scanning, the audit log; where shell commands run (bubblewrap); the `*** Begin Patch` format. | [permissions](docs/architecture/permissions.md) |
| `session.py`, `checkpoints.py`, `memory.py` | Encrypted sessions, schema, forking; `/undo` and `/diff`; memory catalogues. | [sessions](docs/architecture/sessions.md) |
| `redaction.py` | Credential stripping for tool output and audit args. | — |
| `config.py`, `paths.py` | The single config file; every `~/.cobirb` path, derived in one place. | [surfaces](docs/architecture/surfaces.md) |
| `typing/spi.py`, `plugins/loader.py` | **The plugin contract**; discovery, fail-closed. | [extending](docs/architecture/extending.md) |
| `plugins/core/` | Built-ins: `tools`, `model` (Ollama), `openai`, `toolcalls`, `io`, `crypto`, `render`, `repomap`, `ignores`. | [tools](docs/architecture/tools.md), [providers](docs/architecture/providers.md) |
| `runtime/` | Shared composition: `wiring`, `plugins`, `models`, `system_prompt`, `setup`, `commands`, `command_index`, `sessions`, `instructions`, `hooks`, `verify`, `custom_commands`, `headless`, `export`, `bootstrap`, `plugin_install`, `upgrade`, `catalogues`, `mentions`, `doctor`. | [extending](docs/architecture/extending.md), [surfaces](docs/architecture/surfaces.md) |
| `mcp/` | stdio MCP client and its tool adapter. | [extending](docs/architecture/extending.md) |
| `remote/` | Remote Worker Birbs: `settings`, `osnames`, `trust`, `certs`, `protocol`, `client`, `server`, `job`, `relay`, `runner`, `pool`. | [remote](docs/architecture/remote.md) |
| `flock/` | Multi-agent runs: `charter`, `plan`, `brainy`, `stages`, `tickets`, `worker`, `supervisor`, `review`, `run`, `branch`, `probe`, `preflight`. | [flock](docs/architecture/flock.md) |
| `tui/` | The Textual app: `app`, `slash_commands`, `transcript`, `attachments`, `pickers`, `widgets`, `screens`, `panes`, `io_bridge`, `flock_bridge`, `app.tcss`. | [surfaces](docs/architecture/surfaces.md) |
| `help_text.py` | `cobirb help` — the overview; `cobirb help <topic>` renders the manual. | [surfaces](docs/architecture/surfaces.md) |
| `install.sh` | Installer, upgrader and downgrader; shipped in the package and attached to each release. | [surfaces](docs/architecture/surfaces.md) |
| `docs/manual/` | The user manual. Shipped in the package as `cobirb/manual/`; also `cobirb help`. | — |
| `docs/architecture/` | How each part works, for whoever changes it. Not shipped. | — |
| `examples/`, `config.json.example` | Configurations to start from; the annotated example config. | — |
| `bench/` | cobirb-bench, the offline benchmark (§6). Not shipped. | — |
| `tests/` | Grouped by module (`tests/test_<module>.py`); `conftest.py` isolates `COBIRB_HOME` and blocks the network. | — |
| `scripts/release.sh`, `.github/workflows/` | Cutting a release (§7); CI (`tests.yml`) and release builds (`release.yml`). | — |

## 5. Conventions

- **Docstrings explain *why*.** When you fix a subtle bug, the reason it was a bug goes next to the fix.
- **Changing behaviour means updating the docs, in the same change** — a stale page is worse than
  none. The user-facing page is under `docs/manual/` (indexed by `docs/README.md`; also `cobirb
  help`); the page for the area under `docs/architecture/`; and this file when a rule changes.
- **Each fact lives in one place.** This file holds the rules; `docs/architecture/` how things work;
  `docs/manual/` what the user does. Refer to other sections by name, not number.
- `from __future__ import annotations` in every module. Source cites source (`see X`), never a doc file.
- **Keep the core thin**: feature logic belongs in a tool, a plugin or `runtime/`, not `orchestrator.py`.
- Optional capability is **duck-typed and probed**, never added to an ABC (the SPI freeze depends on it).
- Every path under `~/.cobirb` comes from `paths.py`, at call time.
- Bound every result a model or a person will read, and say when it was truncated.
- A hardware or performance figure is an observation, not a decision (see the hardware section).

## 6. Testing and measuring

```bash
pip install -e ".[dev]"
pytest                                            # parallel (-n auto); CI: 3.11 and 3.12
ruff check && ruff format --check                 # lint and format; CI runs both
pytest -n 0 tests/test_x.py -k name               # serial, for one test
COBIRB_TEST_MODEL=llama3.1 pytest -m integration  # needs a real local Ollama
python bench/cobirb_bench.py --models <m1,m2> --reps 2   # the offline benchmark
```

- **No unit test reaches a model endpoint or the network**: `conftest._no_model_endpoint` refuses port
  11434 and anything off loopback, and fails at teardown even if the refusal was swallowed.
  `integration` tests are exempt.
- **Runs in parallel by default** (`pytest-xdist`, `-n auto --dist loadgroup`), which works because
  every test is isolated. A test that touches something genuinely shared — the real `pip` runs in
  `test_plugin_install.py` write into the one virtualenv — goes in an `xdist_group` so its file runs
  on one worker. Run one test serially with `-n 0` (`-p no:xdist` fails: the config passes `-n`).
- **Unit tests don't use git snapshots**: `conftest._per_file_checkpoints` wires per-file `Checkpoints`;
  a test about whole-tree behaviour opts in with `@pytest.mark.tree_checkpoints`.
- `COBIRB_HOME` is a tmp dir for every test; `write_config(home, data)` is the only sanctioned way to
  set config. `asyncio_mode = "auto"` for the Textual Pilot tests. Crypto runs against the real
  backend. Subprocess boundaries are usually mocked, with at least one real test.
- **Test the contract, not the internals**: aim for 85–90 % coverage, not more. If a change preserves a
  contract, its tests should not change; over-specified tests are fixed in the test.
- **`ruff check` and `ruff format` must pass** (settings in `pyproject.toml`; CI runs both). A broad
  `except Exception` that swallows the error carries `# noqa: BLE001 - <why>`; one that logs or
  re-raises carries a plain `# <why>`. No type checker.
- **cobirb-bench** (`bench/`): fixture repos, an instruction, a hidden checker; runs CoBirb headless
  from a frozen `git worktree` of one commit, seeded per repetition (not temperature 0), and classifies
  every failure by cause. `bench/selftest.py` proves each checker fails the untouched fixture and
  passes the reference `solution/`. `--flock` runs a flock session through `bench/flock_driver.py`,
  the only place a charter is auto-approved — throwaway copies only; a pass with no worker behind it
  is `no_flock`, not a pass. `compat_table.py` generates `docs/manual/models.md`.
  **A claim that something improves reliability is checked here**, beyond the noise between runs.
  `bench/README.md` is the method: running it, adding a task, reading results. Finished runs are
  committed under `bench/results/`; `bench/compare.py` compares runs and puts a Fisher exact p on
  every change, because at 3 reps most differences are not yet evidence.
- **The golden test** (`bench/tasks/golden-snake`, category `golden`) runs only with `--golden`, only
  as the final confidence check once everything else passes, and **only after asking the user, every
  time** — it takes hours and costs them real electricity.

## 7. Releases

Cut with `scripts/release.sh patch|minor|major` (or a version); never improvise the sequence. Notes go
under `## [Unreleased]` in `CHANGELOG.md` first; the script renames the heading, bumps
`pyproject.toml`, commits and tags, and pushes only with `--push`. It deliberately does not re-run
tests, poll CI or install the artifact. `release.yml` builds wheel and sdist on the tag and attaches
them with `SHA256SUMS` and `install.sh`.

## 8. Decided — do not rebuild these

Absences are not omissions. Reopening one is a fresh decision to take with the user.

- **Local models only, forever** — no shipped or blessed remote provider. The SPI lets a third party write one.
- **CoBirb is a client, never a model runtime** — an embedded GGUF runtime was designed and cut; the
  trust problem belongs to the endpoint.
- **The working-method prompt stays opt-in** — measured no better (see
  [extending](docs/architecture/extending.md)). Re-measure rather than re-argue.
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

## 9. Known gaps (documented, not defects)

- **Guarantees end at the model socket.** The endpoint is a separate program.
- **Without a working bubblewrap, an approved `shell` command runs with full user privileges** — as
  does one sent `unsandboxed`, or any with `sandbox: "off"`. The sandbox hides a fixed list of
  credential paths, not every secret, and passes the environment through. On WSL that list does not
  reach the Windows drive: `/mnt/c` is readable, like the rest of the filesystem.
- **Without `git`, `/undo` cannot cover shell changes** — only what a tool declared.
- **Installing a plugin executes its code** before any permission layer exists.
- **A configured MCP server can do what it likes with the arguments it receives.**
- **An attached image grows its session file by about its size**, and every save rewrites the blob.
- **`redact_secrets` matches formats, not names** — it misses bespoke credential formats.
- **A Remote Worker Birb on Windows is not sandboxed**: the machine is the containment. On the
  user's own Windows computer it acts as their Windows user. A remote's test results are as
  trustworthy as the remote.
- **A hang in Ollama's `/api/show` cannot be force-stopped**: it stays on plain `urllib`, outside
  `cancel()`'s tracked sockets, because moving it would rewrite every test that mocks that call. It is
  a fast metadata call that hangs only when the whole server is wedged.
