# Config

One file: `~/.cobirb/config.json`. CoBirb reads no config from your project — a repository
can describe itself, never grant permissions.

Start from [`config.json.example`](../config.json.example). `cobirb help config` has the long
version.

## Models

```json
{
  "models": {
    "default":      { "name": "qwen2.5-coder:14b", "base_url": "http://localhost:11434" },
    "orchestrator": { "name": "qwen2.5-coder:32b" },
    "worker":       { "name": "qwen2.5-coder:7b" }
  }
}
```

Every role inherits from `default`, field by field. Name only `default` and everything uses it.
Check with `cobirb models`.

## Every key

| Key | Default | Does |
|---|---|---|
| `models` | — | Per-role model and endpoint (above) |
| `persona` | `"none"` | Persona to adopt |
| `system_prompt` | `"off"` | `"harness"` adds CoBirb's own system block |
| `plan_mode` | `false` | Start in plan mode |
| `allow_tools` | `[]` | Pre-approved tools, e.g. `["read_file", "shell(git status)"]` |
| `allow_read_dirs` | `[]` | Directories readable without asking |
| `allow_write_dirs` | `[]` | Directories writable without asking |
| `checkpoints` | `true` | File snapshots behind `/undo` and `/diff` |
| `redact_secrets` | `true` | Strip credentials from tool output |
| `audit_log` | `false` | Append every tool call to `~/.cobirb/audit.jsonl`, **unencrypted** |
| `instructions` | `true` | Read the project's `AGENTS.md` |
| `instructions_max_chars` | `32000` | Cap on that |
| `repo_map` | `true` | Send a codebase outline |
| `repo_map_max_chars` | `16000` | Cap on that |
| `context_tokens` | *asked for* | Override the history budget (not the `num_ctx` sent to the server) |
| `max_num_ctx` | — | Ceiling on the `num_ctx` CoBirb asks the server for — `65536` or `"64k"` |
| `verify_command` | — | Run after a turn that changed files, e.g. `"pytest -q"` |
| `verify_timeout` | `120` | Seconds |
| `verify_fix_attempts` | `1` | Bounded retries when it fails |
| `hooks` | `{}` | Commands at lifecycle points |
| `mcp_servers` | `{}` | See [Plugins & MCP](plugins-and-mcp.md) |
| `plugins` | core | Which implementation fills each slot |

## Example

```json
{
  "models": { "default": { "name": "qwen2.5-coder:14b" } },
  "allow_read_dirs": ["~/code/myproject"],
  "allow_tools": ["shell(git status)", "shell(git diff)"],
  "verify_command": "pytest -q"
}
```
