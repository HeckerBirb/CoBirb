# Install

You need Python 3.11+ and a running model server. [Ollama](https://ollama.com) is the default.

## 1. Get a model

```bash
ollama pull qwen2.5-coder:14b
```

## 2. Install CoBirb

```bash
git clone https://github.com/HeckerBirb/CoBirb && cd CoBirb
pipx install --editable .
```

That puts a `cobirb` binary on your `PATH`. No venv to activate.

## 3. Check it

```bash
cobirb --help
```

## Working on CoBirb's own source

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"     # dev extras add the test suite
pytest
```

## Updating

```bash
cobirb --upgrade            # latest tagged release
cobirb --upgrade v0.8.0     # a specific one
```

Refuses to go backwards without `--force`, and refuses outright if the checkout has
uncommitted changes.

Source edits are picked up automatically. A `pyproject.toml` change (new dependency, new
entry point, version bump) needs the install command run again.
