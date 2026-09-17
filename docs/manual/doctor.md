# Checking your setup

```bash
cobirb doctor        # or: cobirb --doctor
```

Runs every check and prints a report. Changes nothing.

```
  ✓ config file — /home/you/.cobirb/config.json
  ✓ config keys — 4 recognised
  ✓ config value types
  ✓ config references
  ✓ model endpoint — 8 model(s) available
  ✗ model (default) — 'gemma4' is configured but not on the endpoint — 'ollama pull gemma4'
  ✓ install — managed — /home/you/.local/share/cobirb/venv
  ✓ version — v0.13.1

  1 problem(s) to fix.
```

| Mark | Means |
|---|---|
| `✓` | Fine |
| `!` | Worth knowing; won't stop you working |
| `✗` | Actually broken |

Exits `1` if anything is `✗`, otherwise `0`. A `!` never fails it.

## What it checks

**Config** — that it parses, that **every key is one CoBirb reads**, that values are the right
type, and that what it points at exists (tools in `allow_tools`, directories in
`allow_read_dirs`).

That second one matters most. CoBirb ignores keys it doesn't know, in silence — so
`redact_secret` instead of `redact_secrets` leaves you believing redaction is **off** when it is
still **on**. `doctor` is what catches that.

**Endpoint and models** — that the server answers, that every model you named is actually
pulled, and whether it supports vision. These are the ones that otherwise fail mid-turn.

**Install** — which of the three install shapes you have, and so whether `--upgrade` can work
here at all:

| It says | Means |
|---|---|
| `install — managed — ~/.local/share/cobirb/venv` | Installed by `install.sh`. `--upgrade` re-runs it. |
| `install — /path/to/CoBirb` | A git clone installed editable. `--upgrade` moves the checkout. |
| `install — neither a managed install nor a git checkout` | `--upgrade` can't work; upgrade it the way you installed it. |

For a clone it also reports the branch, because a detached `HEAD` swallows the next commit you
make, and checks your version against the release tags it already has.

For a managed install it reports the version but **does not** check whether a newer release
exists — that would mean asking GitHub, and `doctor` talks to the endpoint you configured and
nothing else. `cobirb --upgrade` is where that question gets asked.

## Common fixes

| It says | Do |
|---|---|
| not settings CoBirb reads | Fix the spelling — see [config](config.md) |
| configured but not on the endpoint | `ollama pull <model>` |
| not reachable | Start your model server |
| not on a branch | `git checkout main` |
| released — `cobirb --upgrade` | `cobirb --upgrade` |
| neither a managed install nor a git checkout | Reinstall with [install.sh](install.md), or upgrade it however you installed it |
