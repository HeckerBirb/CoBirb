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

`options` are sent to the server with every request — sampling settings such as `temperature`,
`top_p` or `seed`. A role's options are merged over the default's, key by key:

```json
"models": {
  "default": { "name": "qwen3-coder:30b", "options": { "temperature": 0.7 } },
  "worker":  { "options": { "temperature": 0.2 } }
}
```

`num_ctx` is ignored here; set `max_num_ctx` instead (below).

## Capping the context window

```json
{
  "max_num_ctx": "64k"
}
```

Three numbers can call themselves "the context window," and `max_num_ctx` is the outermost of
them:

1. **Ollama's own default is 4096** — what a model gets served if nothing says otherwise. CoBirb
   never relies on it: every `/api/chat` request states `options.num_ctx` explicitly, because a
   client that lets the server guess gets a conversation silently truncated from the front.
2. **What CoBirb asks for** is the Modelfile's own `num_ctx` if whoever built the model set one,
   and otherwise the architecture's advertised maximum — `qwen2.context_length`, `llama.context_length`,
   read straight off `/api/show`. For a Qwen2-family model with no `num_ctx` in its Modelfile,
   that maximum is 262144, sized as KV cache on top of the weights.
3. **`max_num_ctx` is a ceiling on step 2**, not a replacement for it. A model asking for less than
   the ceiling is untouched; one asking for more — that 262144 — is clamped down to it. It never
   raises a window that came out lower, and it changes nothing about the model or its Modelfile,
   only what CoBirb puts in the request.

Set it to whatever a single conversation's KV cache should top out at on your card — `"64k"` and
`65536` mean the same thing, a `k` being 1024. Leave it unset and CoBirb asks for whatever the
model advertises, uncapped, which is fine on hardware with room for it and is why nothing needs it
by default.

`context_tokens` is a different knob and does not do this job: it overrides how much conversation
*history* CoBirb budgets against, never the `num_ctx` sent to the server.

## Two timeouts, because they answer different questions

```json
  "connect_timeout": 10,
  "request_timeout": 600
```

- **`connect_timeout`** — how long to wait for the endpoint to *accept a connection*. Short on
  purpose: either something is listening on that port or it isn't. This is the timeout behind
  "Is Ollama running?", which is the only question it can actually answer.
- **`request_timeout`** — how long to wait for the endpoint to *say something* once connected.

They used to be one 120-second number, and it was wrong for both jobs. A socket timeout measures
**silence, not work**: a request your endpoint has queued behind another generation sends no bytes at
all until it starts producing tokens, so the clock was measuring queue wait. A flock is exactly the
shape that produces queue wait — several Worker Birbs against one endpoint — so workers died at 120
seconds having never sent a prompt, and were reported as "Is Ollama running?" about a server that was
busy answering their colleague.

Raise `request_timeout` if you serve very large models, run many workers at once, or see workers
failing to start. Lower `connect_timeout` if you'd rather find out sooner that nothing is listening.

If your endpoint serves one request at a time, the more direct fix is on its side —
`OLLAMA_NUM_PARALLEL` for Ollama. `/flock` can measure this for you before it fans out.

## Every key

| Key | Default | Does |
|---|---|---|
| `models` | — | Per-role model and endpoint (above) |
| `system_prompt` | `"off"` | `"harness"` adds CoBirb's own system block |
| `plan_mode` | `false` | Start in plan mode |
| `max_turns` | `40` | Ceiling on model turns per message. A backstop: a run that repeats the same call or keeps failing is stopped long before it |
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
| `connect_timeout` | `10` | Seconds to wait for the endpoint to accept a connection |
| `request_timeout` | `600` | Seconds to wait for it to respond once connected (above) |
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
