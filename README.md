# CoBirb 🦜

A privacy-first, Copilot-like agentic CLI. Your CoBirb Noah lives in your terminal —
helpful, playful, and entirely local.

> **Privacy is not a feature. It's the foundation.**

CoBirb behaves like a coding agent, then subtracts every default network and telemetry behavior
and adds a hard boundary around the rest. **No data leaves the CPU process unless you explicitly
opt a capability in.**

- 🔒 No telemetry. No analytics. No pings.
- 🔒 No outbound network by default. Models are yours to configure.
- 🔒 Sessions are encrypted at rest (AES-256-GCM, keyed via scrypt). A
  post-quantum KEM seal is a design goal, not yet implemented.
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
COBIRB_MODEL_NAME="llama3.1" cobirb -p "list the files in this directory" --allow-tool=list_dir
```

Run the test suite with `pytest`.

## Documentation

- [DESIGN.md](./DESIGN.md) — architecture, constraints, roadmap.
- [PLUGIN_SPEC.md](./PLUGIN_SPEC.md) — formal plugin interface specification.
- [AGENTS.md](./AGENTS.md) — developer notes for this project (for me and future contributors).

## License

MIT — see [LICENSE](./LICENSE).
