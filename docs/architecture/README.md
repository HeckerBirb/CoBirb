# CoBirb architecture

How each part of CoBirb works, for whoever changes it. The rules that hold everywhere — the
invariants, the conventions, how to test, what was decided — are in [`AGENTS.md`](../../AGENTS.md);
read that first. The user manual is [`docs/manual/`](../manual/), which is also `cobirb help`.

Each fact lives in one place. A behaviour change updates the page for the area it touches, in the
same change; the layout table in `AGENTS.md` says which page covers which module.

| Page | What it covers |
|---|---|
| [The loop](loop.md) | `Orchestrator`: the model ↔ tools loop, stop reasons, plan mode, verification, steering, optional hooks, context management |
| [Permissions and the sandbox](permissions.md) | `Policy`, what "always" grants, session grants, bubblewrap, auto-pilot, the audit log |
| [Sessions, crypto and undo](sessions.md) | Encrypted sessions, schema, forking, checkpoints, memory catalogues, attached images |
| [Tools](tools.md) | The built-in tools and their limits |
| [Model providers](providers.md) | Ollama and OpenAI-compatible servers, tool calls written as text, streaming, timeouts |
| [Extending CoBirb](extending.md) | The plugin SPI, project grounding, hooks, verify, custom commands, MCP, model roles |
| [The Flock](flock.md) | Brainy Birb, Architect Birb, Worker Birbs: planning, the charter, rounds, workers, review |
| [Remote Worker Birbs](remote.md) | A Worker Birb on another machine and OS: pairing, the connection, jobs, and how the Flock uses them |
| [Surfaces](surfaces.md) | The CLI, headless mode, the TUI, config keys, help, environment, install shapes |
