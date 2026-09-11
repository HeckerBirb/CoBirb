# AGENTS.md — CoBirb developer notes

> This file is the notes file for the agent (me). It captures the **intent, decisions, and
> conventions** of this project so that design rationale survives across sessions. If a doc says
> something but this file doesn't agree, **the intent in this file wins** — surface the discrepancy
> and fix the doc.

---

## 0. What CoBirb is

A **privacy-first, Copilot-like agentic CLI**. Noah the African Grey parrot is the default persona.

The one-line truth: **CoBirb = Copilot CLI behavior, with every default network/telemetry behavior
subtracted and a hard privacy boundary added around the rest.**

## 1. The ironclad constraints (do NOT violate these)

1. **Zero telemetry.** No analytics, no crash reports, no usage collection. Period.
2. **No outbound network by default.** Only the model layer *may* touch the network, and only when
   the user explicitly configures a remote provider. Local (e.g. Ollama) is the default path.
3. **Never echo the password.** `--password`/`-p` reads from stdin with no echo; never logged/stored.
4. **Sessions encrypted at rest.** AES-256-GCM bulk cipher, sealed by post-quantum KEM. Plaintext
   never sits on disk.
5. **Default-deny permissions.** No capability touches fs/network without explicit opt-in.
6. **Everything local.** No cloud sessions, no remote control, no background agents.

If a feature idea conflicts with any of these, it's **out of scope for v0.1.0** — park it, don't
build it.

## 2. Key decisions

- **Language:** Python 3.11+, in a `venv` (`.venv/`).
- **Architecture:** thin core + plugin SPI. The core does NOT contain feature business logic; it
  wires together pluggable providers. This is the #1 architectural decision — don't erode it.
- **Models:** entirely user-configured. We ship a *default example* config for local Ollama, but we
  embed **no models** and make **no outbound calls by default**.
- **Mascot:** Noah, African Grey. Personality is **data** (persona file), never behavior injection.
  Persona files must never be allowed to instruct the agent to skip permissions/encryption/network.
- **PQC encryption:** hybrid AES-256-GCM + ML-KEM-768 (Kyber), optional ML-DSA-67 (Dilithium). Use
  a **vetted library** (`pqcrypto`/liboqs bindings), never hand-rolled crypto. The exact KEM/DSA
  variant is a runtime strategy choice, not hardcoded.
- **Tone:** friendly + playful, **not** saccharine. Light puns, no excessive sparkle.

## 3. Docs & where things live

- `DESIGN.md` — architecture, constraints, roadmap. **Design source of truth.**
- `PLUGIN_SPEC.md` — formal plugin SPI contract. **Implementation source of truth for plugins.**
- `README.md` — intentionally minimal right now; grows as the tool becomes functional.
- `todo-list.md` — human-written feature ideas (written by the user).
- `copilot-description.md` — the original consolidated Copilot CLI description we synthesized from.

## 4. Conventions

- **Permissions granularity:** match by tool name by default; narrow the `shell` tool by first word
  after splitting on `; | && &` (so `bash -n` can be allowed without allowing `bash`). Motivation
  in `todo-list.md`.
- **Plugin loading:** core depends on plugins; plugins depend on nothing but the SPI. Discovery is
  lazy, cached per run, and **fail-closed** — a broken plugin never bricks the core.
- **Audit:** every tool call (including plugin tools) is recorded in a local, append-only audit log.
  Never leaves the machine.
- **Session format:** plaintext JSON shape lives only in RAM after decryption; on disk it's the
  encrypted blob. Each turn carries a content hash for tamper detection.

## 5. Roadmap

- **v0.1.0 (in progress):** docs + scaffolding. Core runtime, local model adapter, built-in tools,
  permission model, encrypted sessions, Noah persona, CLI surface.
- **v0.2.0:** speech (in/out), vision, MCP (opt-in/add-only), subagent parallelism.
- **v0.3.0:** custom agents & skills (config-first), live hooks.
- **v0.4.0:** plugin distribution.
- **Forever out:** cloud sessions, remote control, background agents, telemetry. Contradict the
  founding principle.

## 6. Gotchas / lessons

- Don't hardcode crypto. Ship the interface; the crypto backend is a swappable plugin.
- Don't let persona data influence behavior/permissions.
- Remote model providers = plugins gated by permissions, never core defaults.
- Keep the core thin. If you're adding "core" logic for a feature, that feature is probably a plugin.

## 7. Unresolved questions (to resolve before/while scaffolding)

- Exact KEM/DSA variant at runtime (default ML-KEM-768; DILITHIUM seal optional).
- UI: `rich`-based TUI for interactive; subprocess-style CLI for `-p`. (Chosen; revisit if needed.)
- Whether to add a `py.typed` marker and package `pyproject.toml` for distribution.
