# Changelog

All notable changes to CoBirb are recorded here, newest first. See
[`AGENTS.md`](./AGENTS.md) for the architecture and design reasoning behind these changes,
and its §12 decisions record for the ones that were designed and then deliberately *not* built.

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
