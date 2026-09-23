# Hooks

Your own commands at CoBirb's decision points. The permission prompt answers "may this run?" by
asking you; a hook answers it with a rule — never touch anything under `infra/`, always run the
formatter after an edit, tell me when a turn finishes.

Hooks live in `~/.cobirb/config.json`, like everything else. A repository cannot install one: a hook
runs with no approval prompt in the way, so a project able to define one would make cloning it enough
to run its author's code.

```json
{
  "hooks": {
    "before_tool": [
      { "match": "write_file", "command": "~/.cobirb/hooks/guard-infra.sh" }
    ],
    "after_turn": ["notify-send 'CoBirb finished'"]
  }
}
```

## Events

| Event | When | Can it change anything? |
|---|---|---|
| `before_tool` | Before a tool call — before you are even asked to approve it | Yes: a non-zero exit blocks the call |
| `after_tool` | After a call has run | No |
| `before_turn` | Before the model is given the prompt | No |
| `after_turn` | After the turn, including any verify-and-fix pass | No |

`match` is a glob against the tool name (`write_*`, `mcp__db__*`) for the two tool events. Omit it to
match everything.

## The contract

The event arrives on stdin as one JSON object:

```json
{"event": "before_tool", "tool": "write_file",
 "arguments": {"path": "infra/main.tf", "content": "..."},
 "cwd": "/home/you/project"}
```

- **Exit 0 means proceed.** A non-zero exit from a `before_tool` hook **blocks** the call, and whatever
  the hook printed becomes the reason the *model* is given — so say something it can act on ("infra/ is
  generated; edit the module instead") rather than a flat refusal it will retry.
- On the other three events a non-zero exit is reported to you and nothing else.
- A hook that cannot be run, or that times out (`"timeout"`, default 30 s), counts as a refusal —
  a guard that failed open would stop guarding exactly when it broke.

A `before_tool` hook can only refuse, never approve: everything it lets through still goes to the
permission layer.
