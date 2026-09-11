# CoBirb 🦜

A privacy-first, Copilot-like agentic CLI. Your CoBirb Noah lives in your terminal —
helpful, playful, and entirely local.

> **Privacy is not a feature. It's the foundation.**

CoBirb behaves like a coding agent, then subtracts every default network and telemetry behavior
and adds a hard boundary around the rest. **No data leaves the CPU process unless you explicitly
opt a capability in.**

- 🔒 No telemetry. No analytics. No pings.
- 🔒 No outbound network by default. Models are yours to configure.
- 🔒 Sessions are encrypted at rest (AES-256-GCM sealed with post-quantum KEM).
- 🦜 Friendly parrot persona — playful, never saccharine.

## Status

🚧 **Design phase — v0.1.0.** See [`DESIGN.md`](./DESIGN.md) for the full design and
[`PLUGIN_SPEC.md`](./PLUGIN_SPEC.md) for the plugin SPI.

**This is not yet functional code.** The documentation is being written before scaffolding begins.

## Documentation

- [DESIGN.md](./DESIGN.md) — architecture, constraints, roadmap.
- [PLUGIN_SPEC.md](./PLUGIN_SPEC.md) — formal plugin interface specification.
- [AGENTS.md](./AGENTS.md) — developer notes for this project (for me and future contributors).

## License

MIT — see [LICENSE](./LICENSE).
