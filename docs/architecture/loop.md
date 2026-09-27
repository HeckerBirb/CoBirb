# The loop

`orchestrator.py` and `context.py`. `Orchestrator` wires five pluggable pieces — model provider,
tool registry, policy, I/O adapter, session manager — and contains no feature business logic.

```
prompt → model call → tool calls (policy-gated) → results into history → repeat
       → plain reply with no tool calls == final answer (becomes session.summary)
```

## Running a turn

- **Stopped by lack of progress, not a turn count.** `DEFAULT_MAX_TURNS = 40` (config `max_turns`) is
  a backstop. The same call with the same arguments back to back gets a note on its result
  (`_REPEAT_NOTE_AT = 2`) and ends the run at `_REPEAT_STOP_AT = 4`; `_FAILURE_STOP_AT = 6` failed or
  denied calls in a row end it too. (A flat 8 used to end ordinary tasks half done.)
- **How a run ended is `Orchestrator.last_stop`** (`RunStop`: `STOP_ANSWERED`, `STOP_TURN_LIMIT`,
  `STOP_NO_PROGRESS`), never text in the summary: the stop is rendered through `render_notice`, no
  "Completed." is printed under it, and headless reports `stop_reason` and exits 1.
- Each iteration: drain the steering queue → build context → model call → run the calls, or return.
- **A call the provider could not read** (`malformed_tool_call()`, see [providers](providers.md)) is
  fed back as a user turn and counts toward the failure brake, instead of being taken for the answer.
- **Plan mode** (`--plan-mode`, `/plan`, `plan_mode`): a planning pass (`_PLAN_MAX_TURNS = 12`)
  offered only `READ_TOOLS` and `todo`, then the work. A call naming a tool the phase did not offer is
  refused (`_execute_tool_calls(offered=...)`) — a native call can name anything. `Turn.phase` is
  `plan|act`. Sessions from before the validate phase was removed still load, with their `validate`
  turns and `Session.validation`, and the transcript replays them.
- **Auto-pilot** — see [permissions](permissions.md).
- **Verification** (`_verify_and_fix`) runs the user's `verify_command` after a turn that changed
  files and allows `verify_fix_attempts` fix attempts (default 1).

## Steering and stopping

- **Mid-turn steering** (`steer()`, thread-safe) queues a message for the next boundary and, if the
  provider has `interrupt_current_reply()`, cuts the stream (`SteeringInterrupted`); the partial
  reply is kept.
- **A message is shown when it is applied** (`_drain_steer` → the optional `render_steer` hook),
  never when typed: after what it interrupted, and before "↳ redirected by a new message". One that
  arrives after the last boundary is reported as not used; `_steer_lock` closes the gap between
  checking the turn is live and queuing.
- `cancel()` is the one-way stop.

## Optional hooks

Probed with `getattr`, never added to an ABC (the SPI is frozen — see [extending](extending.md)):

- model — `context_window()`, `cancel()`, `interrupt_current_reply()`, `malformed_tool_call()`;
- tools — `writes()`, `preview()`, `cancel_running()`;
- checkpoints — `end_turn()`, `close()`;
- I/O — `spinner`, `begin_stream`, `confirm_scoped`, `confirm_request`, `render_answer`,
  `render_plan`, `render_tool_call`, `render_notice`, `render_steer`, `write_error`.

`render_through()` is the probe-with-fallback helper.

## Context management (`context.py`)

- `DEFAULT_CONTEXT_TOKENS = 32768` is the floor only when the provider cannot say. The Ollama provider
  *states* `num_ctx` on every request (the Modelfile's `num_ctx`, else the advertised maximum), capped
  by `max_num_ctx` (`"64k"` = 65536; unparseable → `None`, reported by `doctor`). The cap only
  lowers, and `_context_budget` packs against the same number. `context_tokens` is a different knob
  (history budget, not the wire `num_ctx`). `history_budget` reserves 20 %, clamped to 2048–16384;
  estimation is `len(text) // 4`.
- `/clear` appends a `role == "clear"` turn — a role, not a field, because the digest formula is
  fixed. `turns_since_clear` is shared by `_build_context`, `/context` and `render_history`, which
  must not drift. Everything before the marker stays in the file.
- `compact()` passes, cheapest loss first; short sessions return unchanged:
  1. Elide old tool results outside the last `_KEEP_RECENT = 6` turns (≥400 chars; the call stays).
  2. Drop a contiguous run after turn 0, never leaving an orphaned tool result — replaced by a
     **model-written summary** when a `summarise` callback is offered (`Orchestrator._summarise_dropped`:
     no tools, material trimmed, cached so it is one call per growth of the dropped prefix, the plain
     note on failure, capped at `_SUMMARY_MAX_CHARS = 4000`).
  3. Elide inside the working set if it is itself over budget.
  4. Trim the largest bodies repeatedly (≤64 iterations) — the pass that guarantees a fit.
- An attached image is priced at a flat `_IMAGE_TOKENS = 1500` — never by its base64 length — and
  old ones are elided first (see [sessions](sessions.md)).
