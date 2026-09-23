# Install

You need Python 3.11+ and a running model server. [Ollama](https://ollama.com) is the default.

Strongly recommended, from your system's package manager: **bubblewrap** (the shell sandbox, Linux)
and **git** (undo for everything a turn changed). CoBirb runs without them, but then every shell
command it runs has your full access, has to be asked about, and cannot be undone — and auto-pilot
will not start.

```bash
sudo apt install bubblewrap git      # Debian/Ubuntu; dnf, pacman, zypper and apk have the same names
```

## 1. Get a model

```bash
ollama pull qwen2.5-coder:14b
```

## 2. Install CoBirb

```bash
curl -fsSL https://github.com/HeckerBirb/CoBirb/releases/latest/download/install.sh | bash
```

That puts a `cobirb` binary on your `PATH`. No venv to activate, no `sudo`, nothing outside your
home directory:

| What | Where |
|---|---|
| Virtualenv | `~/.local/share/cobirb/venv` |
| The `cobirb` command | `~/.local/bin/cobirb` (a symlink into that venv) |
| What version is installed | `~/.local/share/cobirb/install.json` |
| Your config, sessions, memory | `~/.cobirb/` — untouched by install, upgrade or uninstall |

If `~/.local/bin` isn't on your `PATH`, the installer says so and prints the line to add. It
never edits your shell's rc file for you.

It finishes by running [`cobirb doctor`](doctor.md) and explaining its marks. If bubblewrap or git is
missing it says why they are worth installing and prints the command for your package manager — it
never runs `sudo` itself.

Prefer to read it first — it is [`install.sh`](../../src/cobirb/install.sh) in this repository:

```bash
curl -fsSL https://github.com/HeckerBirb/CoBirb/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh
```

The wheel it downloads is checked against the release's `SHA256SUMS` before anything is
installed. A mismatch aborts.

### Options

Pass flags after `-s --` when piping:

```bash
curl -fsSL .../install.sh | bash -s -- --version v0.13.0
```

| Flag | Does |
|---|---|
| `--version v0.13.0` | Install exactly that release instead of the latest |
| `--force` | Allow moving to an older version than the installed one |
| `--uninstall` | Remove CoBirb, keep `~/.cobirb/` |
| `--help` | The list above |

Two environment variables move where it installs to: `COBIRB_INSTALL_DIR` (default
`~/.local/share/cobirb`) and `COBIRB_BIN_DIR` (default `~/.local/bin`).

## 3. Check it

```bash
cobirb --help
cobirb doctor        # confirms the install, config, endpoint and models
```

## Updating

```bash
cobirb --upgrade              # the latest release
cobirb --upgrade v0.13.0      # a specific one
```

This re-runs the installer with a different version, so **upgrading and downgrading are the same
operation**. Going backwards is refused unless you say `--force`:

```bash
cobirb --upgrade v0.13.0 --force
```

Already on the version you asked for? It says so and changes nothing.

## Uninstalling

```bash
curl -fsSL https://github.com/HeckerBirb/CoBirb/releases/latest/download/install.sh \
  | bash -s -- --uninstall
```

Removes the virtualenv and the `cobirb` symlink. **`~/.cobirb/` is left alone** — your config,
sessions and memory catalogues outlive any install. Delete that directory by hand if you
genuinely want it gone.

## Working on CoBirb's own source

A clone installed editable, rather than a release:

```bash
git clone https://github.com/HeckerBirb/CoBirb && cd CoBirb
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"     # dev extras add the test suite
pytest
```

Or `pipx install --editable .` from the clone for a `cobirb` on `PATH` without a venv to
activate.

`cobirb --upgrade` works here too and does the equivalent thing for a clone: fetches, moves the
checkout onto the tag, reinstalls. It fast-forwards the branch you're on rather than leaving you
on a detached `HEAD`, and refuses outright if the checkout has uncommitted changes.

Source edits are picked up automatically. A `pyproject.toml` change (new dependency, new entry
point, version bump) needs the install command run again.

## If `--upgrade` says it can't

CoBirb can upgrade itself when it was installed by `install.sh`, or from a git clone installed
with `pip install -e .`. If you installed it some other way — into a virtualenv of your own, say
— it will tell you so rather than guess. Either upgrade it however you installed it, or switch to
a self-upgrading install by running the installer above.

`cobirb doctor` reports which of the three you have.
