# Permissions

Nothing is pre-approved. Every tool call is either allowed by a rule you wrote, or you are
asked.

## The prompt

```
Allow shell
  git status

  Once (y)   Always (a)   Deny (n)
```

| Answer | Grants |
|---|---|
| **Once** | This call |
| **Always** | For a file tool: that directory, for the session. For `shell`: that command |
| **Deny** / escape | Nothing |

The prompt says what "Always" would actually grant before you press it.

## Reading and writing are separate

| Set | Tools |
|---|---|
| Read | `read_file`, `list_dir`, `glob`, `grep`, `repo_map` |
| Write | `write_file`, `edit_file`, `apply_patch` |

Approving a read never grants a write. A grant covers the directory and everything under it.

## Pre-approving in config

```json
{
  "allow_read_dirs": ["~/code/myproject"],
  "allow_write_dirs": ["~/code/myproject/src"],
  "allow_tools": ["read_file", "shell(git status)"]
}
```

Or per run: `--allow-tool=read_file --allow-tool='shell(git status)'` (repeatable).

## Shell is checked per command

`shell` is in neither directory set — a command can't say what it touches. Instead **every
segment** of a chained command must be permitted:

```bash
git status; rm -rf /      # "git" being allowed does not approve "rm"
```

Anything CoBirb can't read confidently — command substitution `$(...)`, backticks, a subshell,
`find -exec`, an unbalanced quote — is refused rather than guessed at. You still get the
prompt, so being strict costs a keystroke, not a capability.

Narrow rules beat broad ones: `shell(git status)` over `shell(git)` over `shell`.

### Each command is its own process

A `cd` does not carry over to the next `shell` call — the shell it moved has already exited. Put
it in the same command (`cd build && make`), or pass `cwd` to run one call somewhere else. A
command that is *only* a `cd` says so in its result rather than reporting a bare success.

## Paths and `~`

A path a tool is given is resolved the same way whether it is being checked or used: `~` expands
to your home directory, and anything else relative resolves against the working directory
(`--cwd`, or where you started CoBirb).

That matters for approvals. `~/notes/x.md` is gated as the file in your home directory it will
really become — so approving the directory you are working in does **not** carry it, and an
"always" answer to it approves `~/notes`, not something under your project.

## Headless

`--headless` never prompts and refuses anything not already permitted. Exit code `2` means it
completed but refused something. There is deliberately no flag that approves everything.

## What this does not cover

An approved command runs with your full privileges. There is no sandbox — approve `npm test`
and that command can read `~/.ssh` like any program you'd run yourself.
