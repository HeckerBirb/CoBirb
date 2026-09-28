# Surfaces

`cli.py`, `runtime/headless.py`, `tui/`, `help_text.py`, `config.py`, `runtime/upgrade.py`,
`src/cobirb/install.sh`. What each is for the user is in [`docs/manual/`](../manual/); this page is
how they are built.

## CLI (`cli.py`)

- **Subcommands**: `setup` (asks for the server and protocol, lists its models, saves the pick —
  atomic, 0600, every other key kept, an unparseable config refused; never probes), `doctor`,
  `help [topic]`, `models`, `commands`, `flock -p`, `plugin install <path> [--replace] | list |
  remove <name>`, `remote-worker [--listen HOST:PORT]` (see [remote](remote.md)).
- **Flags**: `-p/--prompt`, `--session`, `-w/--password`, `--model`, `--allow-tool`,
  `--plan-mode on|off`, `--autopilot` (with `-p`), `--system-prompt off|harness`, `--export PATH`,
  `--branch PATH`, `--branch-at N`, `--headless`, `--output text|json`, `--cwd`, `--upgrade [TAG]`,
  `--force`, `--continue`, `--doctor`. A flag that would silently do nothing is an error.
- **`doctor`** checks config keys and types (naming retired and deprecated keys as such), the endpoint
  and models, the sandbox (and, on WSL, that Windows programs cannot be started from it), and the
  install; it never asks GitHub about releases.

## Headless (`runtime/headless.py`)

Never prompts. Exit `0` clean, `1` failed (including a run that stopped short), `2` completed but
something was refused — headless only, since a person who answered "no" got what they asked.

## TUI (`tui/`)

- **Tabs** Current, Flock, Sessions, Plugins. **Commands** (`slash_commands.COMMANDS`) `/help`,
  `/model` (offers to save a pick when none is configured), `/plan`, `/autopilot`, `/context`,
  `/clear`, `/undo`, `/export`, `/diff`, `/commands`, `/flock`, `/charter`, `/memories`, `/remember`,
  `/image`; `?` is `/help`. Anything else starting with `/` is tried as a custom command, then sent as
  typed.
- **Pickers** (`tui/pickers.py`: a `Picker` base with the rows and selection, and a subclass per kind
  that ranks and draws). `@path` opens a five-row fuzzy picker and sends the file with the message
  (expanded for the model, never in the transcript). `/` opens the command picker (descriptions from
  each handler's docstring; `_COMMAND_IN_PROGRESS` matches the whole message, so a slash mid-sentence
  is prose).
- **Keys**: `f1`, `f2`, `f3` (auto-pilot; two bindings on one key, `check_action` shows the one
  matching the state, so the footer label is the indicator), `ctrl+q`, `ctrl+c` (copy, else cancel),
  `up`/`down` history.
- **Steering.** The prompt stays enabled during a turn — submitting steers (see [the loop](loop.md)),
  and the box is titled `STEER_LABEL` ("Steering conversation:") once something is typed then.
- **Streaming.** Streamed tokens reach the screen at most every `TuiIO.STREAM_INTERVAL` (per-token
  trips to the UI thread made the app lag). The preview (`StreamPreview`) draws only its last
  `StreamPreview.ROWS` rows (`_LastRows`, no scrolling — a scroll a refresh late made it jump), of a
  window cut at a line start, under a rule titled from `begin_stream`'s label. `TuiIO.drain` sends
  held-back tokens before every flush into the transcript (`transcript` flushes before every write).
- **Approval** is a modal (`y`/`a`/`n`) stating what "always" grants; a Worker Birb's request is asked
  in its own pane instead (see [the Flock](flock.md)). A pick-one question with long answers is
  `ChoiceModal` (nothing highlighted at first).
- **Status bar** shows AUTOPILOT, checklist progress, model, plan mode, cwd, session.
- **One orchestrator per session**, built on first use by `CoBirbApp.ensure_orchestrator` — for the
  first turn, the first flock, or a command such as `/autopilot` — and reused, which is what keeps an
  "always" approval for the rest of the session. Switching or branching a session discards it
  (`_discard_orchestrator`, which also stops its MCP servers). Session files opened outside a run
  get their crypto from `runtime.plugins.session_crypto`, as the CLI's `--export` and `--branch` do.

## Config (`config.py`, `paths.py`)

One file, `~/.cobirb/config.json` (invariant 2). Keys: `models.*`, `system_prompt`,
`plugins.{model,io,crypto}`, `allow_tools`, `allow_read_dirs`, `allow_write_dirs`, `sandbox` (a mode,
or `{"mode", "hide"}`), `max_turns`, `verify_command`, `verify_timeout`, `verify_fix_attempts`,
`redact_secrets`, `checkpoints`, `instructions`, `instructions_max_chars`, `repo_map`,
`repo_map_max_chars`, `context_tokens`, `max_num_ctx`, `connect_timeout`, `request_timeout`,
`plan_mode`, `audit_log`, `hooks`, `mcp_servers`, `flock` (`planning`, `autonomy`, `max_rounds`),
`remote_workers` (see [remote](remote.md)).
`doctor.KNOWN_KEYS` is the list `doctor` checks against. Deprecated: `model`, `default_model`.
Retired: `persona`. `ensure_home()` seeds a starter config on first run; `config.json.example` in the
repository is the annotated example.

## Help (`help_text.py`)

`cobirb help` prints the overview (`help_text._OVERVIEW` plus the page list); `cobirb help <topic>`
renders the matching manual page — `rich` markdown on a terminal, plain when piped — from
`cobirb/manual/` in a wheel (copied from `docs/manual/` by the `setup.py` build hook; `MANIFEST.in`
carries the pages into the sdist) or `docs/manual/` in a checkout. `help_text.ALIASES` keeps old topic
names working. **The manual is the one source**: change a page, and the help changes with it.

## Environment

- `COBIRB_HOME` relocates `~/.cobirb` (how tests isolate).
- `COBIRB_MODEL_NAME` and `COBIRB_OLLAMA_URL` are the model and the Ollama URL a provider falls back
  to when nothing resolved names one (see [providers](providers.md)).
- `COBIRB_PROJECT_DIR` is where the plugin loader looks for a project's `cobirb/plugins/` directory
  (default: the working directory).
- `COBIRB_TEST_MODEL` enables the integration tests.
- The installer reads `COBIRB_INSTALL_DIR`, `COBIRB_BIN_DIR` and `COBIRB_RELEASE_BASE`;
  `COBIRB_INSTALL_SOURCED=1` sources it without installing, for tests.

## Install shapes and upgrading (`runtime/upgrade.py`, `install.sh`)

- `upgrade.detect_install()`: `managed` (`install.sh`'s venv at `~/.local/share/cobirb`, decided by
  the marker's `venv` matching `sys.prefix`), `checkout` (a clone, `pip install -e`), `unmanaged`
  (refused, naming what would work).
- `--upgrade`: **managed** runs the `install.sh` shipped in the running wheel (the one implementation
  of "move to version X"; copied to a tempfile first); **checkout** fetches tags, refuses a downgrade
  without `--force` and a dirty tree outright, **fast-forwards the current branch** onto the tag (a
  detached `HEAD` swallows the next commit), and reinstalls.
- `install.sh` (`src/cobirb/install.sh`, shipped in the package and attached to each release) installs,
  upgrades and downgrades. It ends every install and upgrade with `post_install_report`:
  `cobirb doctor` (its exit code ignored — it reports on configuration, not on the install), one line
  explaining the marks, and, when `bubblewrap` (Linux) or `git` is missing, why to install them and
  the package-manager command. It never runs `sudo`.
