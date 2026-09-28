# Permissions and the sandbox

`policy.py`, `sandbox.py`, `patches.py`. A fresh `Policy` permits nothing (invariant 1 in
[`AGENTS.md`](../../AGENTS.md)).

## What a check sees

- `READ_TOOLS = {read_file, list_dir, glob, grep, repo_map}` and `WRITE_TOOLS = {write_file,
  edit_file, apply_patch, delete_file}` are scoped **by directory**, in separate sets that never imply
  each other. `shell` is in neither: it cannot say what it touches.
- Paths resolve with `realpath` against the policy's `cwd`; `_within` requires a separator
  (`/x/project-secrets` never matches `/x/project`). **`Policy._resolve` and `CobirbTool._resolve`
  mirror each other step for step**, `~` included — a difference is a check on a path the tool won't
  use. A missing or empty `path` is the working directory for `list_dir`, `grep` and `repo_map`, in
  both.
- `shell` is checked **per segment** (`git status; rm -rf /` is two commands). Rules are a bare binary
  (any arguments) or an exact multi-word prefix. Refused as unverifiable: backticks, `$(...)`,
  subshells, unbalanced quotes, `find -exec/-execdir/-ok/-okdir`. `command_segments` fails closed; anything
  advisory that reads commands (`tools._changes_directory_only`) must not share code with it.
- **A `*** Begin Patch` names its own file**, and `patch_target` (shared with the tool) is what is
  checked. Several files, a move, a delete, or a `path` that disagrees → no target → denied.
- `todo`, the charter tools and a Worker Birb's `report` reach nothing and are permitted outright — a
  considered exception: default-deny gates capability (filesystem, network, subprocess), and these
  touch none of it.

## What an answer grants

- `grant()` decides what "always" widens to: a read inside the project → **the whole project** (one
  question per project; `project_root` refuses `/` and the home directory), any other read or a
  write → its directory, a shell call → that invocation, anything else → the tool name.
  `describe_grant()` says so before the user agrees.
- `SessionGrants` applies a `"session"` answer to **every agent in the session**, including Worker
  Birbs built later, with no record of who asked (a permission that depends on its origin is one
  nobody can reason about). Memory only, never written to config; policies held weakly.

## The sandbox (bubblewrap)

- Filesystem read-only except the project and a private `/tmp` (a fresh `tmpfs` for every command);
  the project's **`.git` read-only** (undo restores files, not history); no network namespace (a
  loopback interface only); own PID/IPC/UTS namespaces.
- Credential paths hidden: `DEFAULT_HIDDEN` plus `paths.cobirb_dir()` and any `sandbox.hide`
  entries, masked at their real paths because bubblewrap resolves symlinks inside the new root.
- **On WSL, the interop socket directory `WSL_INTEROP_DIR` (`/run/WSL`) is masked**: a Windows
  program started from inside ran on the Windows side, outside the sandbox entirely. Unsetting
  `WSL_INTEROP` alone does not stop it.
- Modes: `"auto"` (default — contained and not asked, via `Policy.sandbox_auto`), `"ask"`, `"off"`.
  `sandbox_auto` is set on the main agent's policy, which the Flock's planning stages share; Worker
  Birbs have their own policy, so the sandbox never auto-approves a worker's shell. A *default* auto
  applies only where whole-tree checkpoints exist (`wiring`: `box.explicit or end_turn`); a mode the
  user set is honoured as written.
- In auto even a command the segment scan cannot read is allowed — containment is the guarantee.
  `unsandboxed: true` runs outside and always goes through the policy.
- `find_bwrap` probes once with the real flags; without a working bubblewrap everything behaves as
  before and `doctor` says so.

## Auto-pilot

`enable_autopilot`, `/autopilot`, `f3`, `--autopilot` with `-p`. Reads and writes inside the project
(`Policy.autopilot_root`) and contained commands run unasked; **everything else is refused without
asking** (`_autopilot_refusal`). It will not start without the sandbox *and* whole-tree checkpoints,
or in a project that is `/` or the home directory — it is exactly as safe as what contains it.

**Its state is `Policy.autopilot_root`** (`Orchestrator.autopilot` is a property), so the Flock's
planning stages, which share the main agent's policy, follow a toggle mid-run. How it reaches the
rest of a flock is in [the Flock](flock.md).

## Audit log

`AuditLog` is **off** unless `"audit_log": true`; it stores arguments verbatim, so it is created
`0600` via `os.open`, redacted, and never created until enabled.
