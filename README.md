# CoBirb 🦜

A privacy-first, Copilot-like agentic CLI. Your CoBirb Noah lives in your terminal —
helpful, playful, and entirely local.

> **Privacy is not a feature. It's the foundation.**

CoBirb behaves like a coding agent, then subtracts every default network and telemetry behavior
and adds a hard boundary around the rest. **No data leaves the CPU process unless you explicitly
opt a capability in.**

- 🔒 No telemetry. No analytics. No pings.
- 🔒 No outbound network by default. Models are yours to configure.
- 🔒 Sessions are encrypted at rest (AES-256-GCM, keyed via scrypt — already
  quantum-resistant for a password-protected local file; see `crypto.py` for
  why a KEM seal wouldn't add anything here).
- 🦜 Friendly parrot persona — playful, never saccharine.

## Status

🚧 **Early v0.1.0 — working prototype.** The core loop, built-in tools, default-deny
permissions, encrypted sessions, and a local Ollama model provider are implemented and
tested. See [`DESIGN.md`](./DESIGN.md) for the full design and
[`PLUGIN_SPEC.md`](./PLUGIN_SPEC.md) for the plugin SPI.

## Quick start

Requires a local [Ollama](https://ollama.com) server with a model pulled.

```bash
pip install -e ".[dev]"

# Interactive: the full-screen app.
COBIRB_MODEL_NAME="llama3.1" cobirb

# One-shot: plain stdout, pipes and scripts like any CLI.
COBIRB_MODEL_NAME="llama3.1" cobirb -p "list the files in this directory" --allow-tool=list_dir
```

Run the test suite with `pytest`.

Three runtime dependencies, all local-only: `rich` (rendering), `cryptography` (session
encryption), and `textual` (the interactive app — imported lazily, so one-shot mode never
loads it).

## Interactive mode

Running `cobirb` with no `-p` opens a full-screen terminal app, with three tabs:

- **Current** — the conversation. A **live status line** shows persona · model · plan mode ·
  working directory, kept current as you change it. A **boxed input**: submit and it greys out
  while the turn runs, then comes back — there's no "continue? [y/N]" to answer, you just keep
  typing. The **transcript** shows the same panels one-shot mode prints: the reply as rendered
  markdown, tool calls and results with syntax-highlighted diffs, plan/validation panels in plan
  mode. **Tool approval is a dialog** — `y` allow once, `a` allow for the rest of the session,
  `n` or escape to deny. Permission is still default-deny; this just makes "denied" mean "asks
  first".
- **Sessions** — lists encrypted session files found in `~/.cobirb/sessions/` (or wherever
  `COBIRB_HOME` points), lets you resume one (prompts for its password) or start a new one
  (prompts for a name and password), all without needing `--session` on the command line.
- **Plugins** — a live view of every registered tool, which model/I/O/crypto implementation is
  active for each slot, and any plugin discovery problems — the same information a broken
  `plugins.*` config selection would otherwise only report to a stderr the full-screen app hides.

In the input: `/model` lists the models the configured endpoint currently has and lets you pick
one for this session; `/persona <name>` switches personas (`/persona` alone lists them); `/plan
on|off` toggles plan mode (`/plan` alone reports it); `?` or `/help` opens the help screen
(`/help <topic>` for one topic). Keys: `f1` help, `f2` next tab, `ctrl+q` quit.

If no model is configured, or the configured one isn't actually available, interactive mode
fetches the endpoint's model list itself and opens the same picker `/model` would — set
`"default_model"` in config to skip that when it resolves to a real model, and don't worry about
it when it doesn't: an unavailable `default_model` is ignored, not an error.

## Configuration

Copy [`cobirb.json.example`](./cobirb.json.example) to `cobirb.json` (repo-scoped) or
`~/.cobirb/config.json` (user-scoped) and edit it — nothing here is loaded until you do, and
nothing defaults to a networked provider. See `cobirb help config` for what each key does.

## Documentation

- [DESIGN.md](./DESIGN.md) — architecture, constraints, roadmap.
- [PLUGIN_SPEC.md](./PLUGIN_SPEC.md) — formal plugin interface specification.
- [AGENTS.md](./AGENTS.md) — developer notes for this project (for me and future contributors).

## License

MIT — see [LICENSE](./LICENSE).
