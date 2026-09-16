# Contributing to CoBirb

Thanks for considering it. CoBirb is a small, deliberate project — read on before sending a PR,
it'll save both of us a round trip.

## Ground rules

A few things the project will not do, on purpose: no telemetry, no default network access, no
tool running without your approval. A PR that reopens one of these needs a very good reason and a
conversation first, not just code.

Some features have also already been proposed and deliberately turned down (an embedded model
runtime, for one) — if an issue or PR review points you at one of these, that's why; it's been
thought about, so the fastest path is to make the counter-case in the issue rather than resubmit
the same idea.

## Setting up

```bash
git clone https://github.com/HeckerBirb/CoBirb && cd CoBirb
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                            # CI runs this on 3.11 and 3.12
COBIRB_TEST_MODEL=llama3.1 pytest -m integration  # optional, needs a real local Ollama
```

No linter, formatter or type checker is configured — match the surrounding style by hand.

## Making a change

- **Behaviour changes need a matching `docs/` update in the same change.** If you add or rename a
  `/command`, a CLI flag or a config key, or change what one exists does, the relevant page under
  [`docs/manual/`](./docs/manual/) is not optional. A stale doc is worse than no doc — it's the
  first thing a user reads.
- **Test the contract, not the internals.** Aim for tests that describe what a function promises
  to callers, not how it's currently written — if a later change preserves behaviour, its tests
  shouldn't need to change. Don't chase a coverage number; 85–90% is the target, not a floor.
- **Keep the core thin.** Feature logic belongs in a tool, a plugin, or `runtime/` — not bolted
  onto `orchestrator.py`.
- Docstrings explain *why*, not what. If you fix a subtle bug, say why it was a bug.
- The three runtime dependencies (`rich`, `cryptography`, `textual`) are each a deliberate
  decision — prefer stdlib for anything new. A new dependency is a conversation, not a PR.

## Plugins and personas

If your contribution is a new capability rather than a core fix, check whether it belongs as a
plugin instead — CoBirb has a plugin interface specifically so most extensions don't need to touch
the core at all. A persona is just data (`cobirb/personas/*.json`); adding one doesn't need a code
change.

## Sending a pull request

- Keep it focused — one change, one PR. Split unrelated cleanups out.
- Describe the *why*, not just the diff; link the issue it addresses if there is one.
- Make sure `pytest` passes locally first; CI runs the same suite on 3.11 and 3.12.
- Small, well-scoped PRs get reviewed faster than large ones — if you're planning something big,
  open an issue first and sketch the approach.

## Reporting bugs and requesting features

Use the issue templates — they ask for the details that actually get a bug fixed quickly (mainly:
the output of `cobirb doctor`, and how to reproduce it).

Found a security issue instead? Don't open a public issue — see [`SECURITY.md`](./SECURITY.md).

## License

By contributing, you agree your contribution is licensed under the project's [MIT
license](./LICENSE).
