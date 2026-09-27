# Model providers

`plugins/core/model.py` (Ollama), `plugins/core/openai.py` (OpenAI-compatible),
`plugins/core/toolcalls.py`, `runtime/models.py`.

## Ollama (`LocalModelProvider`)

- `POST /api/chat` with a real role-tagged `messages` array — history flattened into one message
  stops the tool loop converging. `/api/show` (cached per model) supplies the Modelfile `SYSTEM`, the
  window and `capabilities` (vision). `list_models` uses `GET /v1/models`, on the short timeout.
- `compose_system()`: empty in → no system message; model has none → ours; both → model's first
  (invariant 7).
- **A thinking model's reasoning is replayed with the tool calls it led to** (`_thinking_by_call`,
  keyed by `_call_signature`, bounded, memory only) — gpt-oss's format expects it, and without it the
  model re-read the same file. Never attached to a final answer, and never shown on screen.
- `models.<role>.options` are sent with every request, merged over `models.default.options`;
  `num_ctx` is dropped there because `max_num_ctx` owns the window (see [the loop](loop.md)).

## OpenAI-compatible (`OpenAICompatibleProvider`)

`models.<role>.api = "openai"`, never probed. `/v1/chat/completions`; tool-call ids minted on replay
and echoed by the matching result; arguments are JSON strings and stream as index-keyed fragments;
options are top-level fields; no Modelfile. **The window is read, not requested** (llama.cpp
`/props` `n_ctx`, else `/v1/models` `max_model_len`/`context_length`/`meta.n_ctx`), capped by
`max_num_ctx`. Vision: `models.<role>.vision`, else llama.cpp's `modalities`.

## Tool calls written as text (`toolcalls.py`)

Read when the structured field is empty: Hermes `<tool_call>` JSON, Qwen3-coder `<function=…>` XML,
leaked gpt-oss channel markup, and JSON that is the whole reply. Only names offered this turn count;
plain JSON only when the prose around it is under `_BARE_JSON_SLACK`. Unreadable attempts — unknown
tool, broken JSON, **the server's own parser failing** (as the reply with HTTP 200, a streamed
`error` line, or a 500 body) — go to `malformed_tool_call()`. Replayed assistant turns have the
markup stripped.

## Streaming and failures (both providers)

- Streaming is NDJSON; `_last_tool_calls` is valid once the generator is exhausted. `cancel()`
  latches the provider closed; `interrupt_current_reply()` cuts one reply; `_steer_signal` is cleared
  before every request. `_stream_lines` is the protocol-independent half, shared by both providers.
- **A request reset before the first byte of its reply is sent once more** (`_stream_lines`,
  `_RESET_ATTEMPTS = 2`): nothing was yielded, and one reset from a busy server used to cost a Worker
  Birb its ticket. Only a reset — a refusal or a timeout is not retried.
- **Two timeouts** (`connect_timeout` 10 s, `request_timeout` 600 s): a socket timeout measures
  silence, not work, and a queued request is silent. Split in the streaming path (`_connect` short,
  `_open` long); `_post` takes the long one; `list_models` catches a dead endpoint on the short one.
- **`/api/show` stays on plain `urllib`, outside `cancel()`'s reach** — see Known gaps in
  [`AGENTS.md`](../../AGENTS.md).
- **A transport failure and a rejected request never share a message**: "Is it running?" only when
  nothing answered; otherwise quote the server (`_unreachable`, `_error_body`; `HTTPError` subclasses
  `URLError`). Messages name the server in use.

## Roles (`runtime/models.py`)

`models.default` / `.orchestrator` / `.worker` inherit from `default` field by field; `options`
merge key by key. Name: `--model` → `models.<role>.name` → `models.default.name` → deprecated
`model` / `default_model`; with none of those, the provider falls back to `COBIRB_MODEL_NAME` (and
the Ollama URL to `COBIRB_OLLAMA_URL`). `cobirb models` prints the result.
