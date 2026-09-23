# CLI

```bash
cobirb                                   # the full-screen app
cobirb -p "what does main.py do?"        # one shot, then exit
cobirb help config                       # help on one topic
```

## Modes

| Flag | Does |
|---|---|
| *(none)* | Interactive app |
| `-p`, `--prompt TEXT` | Run one prompt, print, exit |
| `--session PATH` | Open an encrypted session |
| `--continue` | Reopen the session you were last in. Implies `-w` |
| `-w`, `--password [PW]` | Encrypted session. Omit the value to be prompted without echo |

## Options

| Flag | Does |
|---|---|
| `--model NAME` | Which model, this run |
| `--allow-tool SPEC` | Pre-approve a tool. Repeatable. `read_file` or `shell(git status)` |
| `--plan-mode on\|off` | Look and plan first (read-only), then act |
| `--autopilot` | With `-p`: work unattended in the project and the sandbox; refuse anything else instead of asking |
| `--system-prompt off\|harness` | Whether CoBirb adds its own system block |
| `--cwd DIR` | Work somewhere other than here |
| `--headless` | Never prompt; refuse anything not pre-approved |
| `--output text\|json` | `json` prints one machine-readable object and nothing else |
| `--export PATH` | Write a session out as markdown |
| `--branch PATH` | Fork a session into a new file |
| `--branch-at N` | With `--branch`: keep turns `0..N` |
| `--upgrade [TAG]` | Move to another release — latest, or the one named. See [install](install.md) |
| `--force` | With `--upgrade`: allow going backwards |
| `--doctor` | Check everything is ready, then exit. Same as `cobirb doctor` |

## Subcommands

```bash
cobirb help [topic]      # any page of this manual, e.g. commands, config,
                         # flock, hooks, mcp, permissions, tools
cobirb setup             # choose your model server and model, saved to your config
cobirb doctor            # is everything ready to go?
cobirb models            # how each role resolves
cobirb commands          # your custom commands here
cobirb flock -p "..."    # a flock run without the app
cobirb plugin install <dir> [--replace]
cobirb plugin list
cobirb plugin remove <name>
```

## Scripting it

```bash
cobirb -p "run the tests and report" \
  --headless --output json \
  --allow-tool='shell(python -m pytest)'
```

Exit codes: `0` clean, `1` failed, `2` completed but something was refused (`--headless` only).
A run that did not reach an answer is a failure: `"ok": false`, with `"stop_reason"` either
`"turn_limit"` (hit `max_turns`) or `"no_progress"` (the model kept making the same call, or kept
failing), and `"error"` saying which.
A finished run reports `"stop_reason": "answered"`.

## Environment

| Variable | Does |
|---|---|
| `COBIRB_HOME` | Relocate the whole `~/.cobirb` tree |
| `COBIRB_MODEL_NAME` | Default model |
| `COBIRB_OLLAMA_URL` | Default endpoint |
