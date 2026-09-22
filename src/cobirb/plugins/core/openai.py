"""Chatting with an OpenAI-compatible server: llama.cpp, LM Studio, vLLM.

CoBirb listed models over ``/v1/models`` from the start, so those servers
appeared to work — and then every chat went to Ollama's own ``/api/chat``,
which none of them serve. This provider speaks ``/v1/chat/completions``.

It subclasses the Ollama provider rather than standing beside it, because
nearly everything that makes a provider safe to run unattended is
protocol-independent: the tracked socket ``cancel()`` shuts down, the split
between the connect and request timeouts, mid-reply steering, and reading tool
calls a model wrote as text. What differs is only the wire format:

- **Tool calls carry ids.** CoBirb's turn history has none, so ids are minted
  when the history is replayed — the same id on the assistant's call and on
  the tool result that answers it, which is all the protocol checks.
- **Arguments are a JSON string**, in both directions, and streamed calls
  arrive as fragments keyed by index that have to be stitched together.
- **The window cannot be asked for.** Ollama takes ``num_ctx`` per request;
  these servers fix the context when they start. So instead of stating it,
  this provider reads what the server reports (llama.cpp's ``/props``, vLLM's
  ``max_model_len``) and the history budget is packed against that.
- **There is no Modelfile.** A server-side system prompt, where one exists, is
  applied by the server itself; CoBirb sends a system message only when it has
  something of its own to say, exactly as with Ollama.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from ...typing.spi import Tool, ToolCall
from . import toolcalls
from .model import LocalModelProvider, _server_parse_problem, _tool_schema


class OpenAICompatibleProvider(LocalModelProvider):
    """A local server speaking the OpenAI chat-completions protocol."""

    def __init__(self, *args: Any, vision: bool | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Servers are configured with or without a trailing /v1; every path
        # here adds it, so it is removed once rather than doubled.
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url[: -len("/v1")]
        self._server = "the model server"
        self._vision = vision
        self._window_probe: int | None = None
        self._props: dict[str, Any] | None = None

    def name(self) -> str:
        return f"openai/{self._model}" if self._model else "(unconfigured)"

    # ------------------------------------------------------------------ #
    # What the server says about itself
    # ------------------------------------------------------------------ #
    def model_system_prompt(self) -> str:
        return ""

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(f"{self._base_url}{path}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self._connect_timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _server_props(self) -> dict[str, Any]:
        """llama.cpp's ``/props``, fetched once; ``{}`` from anything else."""
        if self._props is None:
            try:
                payload = self._get("/props")
                self._props = payload if isinstance(payload, dict) else {}
            except Exception:  # noqa: BLE001 - only llama.cpp serves it
                self._props = {}
        return self._props

    def context_window(self) -> int | None:
        """The window the server was started with, capped by ``max_num_ctx``.

        Read, not requested — see the module docstring. ``None`` when the server
        does not say, in which case the orchestrator falls back to its own
        conservative default rather than guessing high.
        """
        if self._window_probe is None:
            self._window_probe = self._reported_window() or 0
        window = self._window_probe or None
        if window and self._max_num_ctx:
            return min(window, self._max_num_ctx)
        return window

    def _reported_window(self) -> int | None:
        settings = self._server_props().get("default_generation_settings") or {}
        for value in (settings.get("n_ctx"), self._server_props().get("n_ctx")):
            if isinstance(value, int) and value > 0:
                return value
        try:
            entries = self._get("/v1/models").get("data") or []
        except Exception:  # noqa: BLE001 - best effort
            return None
        for entry in entries:
            if entry.get("id") != self._model:
                continue
            meta = entry.get("meta") or {}
            for value in (entry.get("max_model_len"), entry.get("context_length"),
                          entry.get("max_context_length"), meta.get("n_ctx"), meta.get("n_ctx_train")):
                if isinstance(value, int) and value > 0:
                    return value
        return None

    def supports_vision(self) -> bool:
        """Configured (``models.<role>.vision``), or what llama.cpp reports."""
        if self._vision is not None:
            return self._vision
        modalities = self._server_props().get("modalities") or {}
        return bool(isinstance(modalities, dict) and modalities.get("vision"))

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #
    def chat(
        self,
        system: str,
        context: str,
        tools: Optional[list[Tool]] = None,
        *,
        stream: bool = False,
    ) -> "Iterable[str] | str":
        if not self._model:
            raise RuntimeError(
                "No model configured. Set --model, COBIRB_MODEL_NAME, or models.default.name "
                "in your CoBirb config — or, in interactive mode, pick one with /model."
            )
        payload: dict[str, Any] = {
            **self._options,
            "model": self._model,
            "messages": build_messages(
                self.compose_system(system), context, include_images=self.supports_vision()
            ),
            "stream": stream,
        }
        if tools:
            payload["tools"] = [_tool_schema(t) for t in tools]
        self._offered = {t.name(): t.parameters() for t in (tools or [])}
        self._malformed = ""
        self._last_tool_calls = []

        if stream:
            return self._stream_chat(payload)
        try:
            response = self._post("/v1/chat/completions", payload)
        except RuntimeError as exc:
            if self._offered and toolcalls.looks_like_server_parse_error(str(exc)):
                self._malformed = _server_parse_problem(str(exc))
                return ""
            raise
        message = ((response.get("choices") or [{}])[0]).get("message") or {}
        self._last_tool_calls = _calls_from(message.get("tool_calls") or [])
        return message.get("content") or ""

    def _stream_chat(self, payload: dict[str, Any]) -> Iterable[str]:
        """Server-sent events: ``data: {...}`` lines, ending with ``data: [DONE]``.

        Tool calls arrive as fragments — the first carries the index, id and
        name, later ones append to the arguments string — so they are collected
        by index and parsed only once the stream has finished.
        """
        pieces: dict[int, dict[str, str]] = {}
        for raw_line in self._stream_lines("/v1/chat/completions", payload):
            line = raw_line.decode("utf-8", "replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                error = chunk["error"]
                message = error.get("message") if isinstance(error, dict) else error
                if self._stream_error(str(message)):
                    break
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
                for fragment in delta.get("tool_calls") or []:
                    slot = pieces.setdefault(int(fragment.get("index", len(pieces))),
                                             {"name": "", "arguments": ""})
                    function = fragment.get("function") or {}
                    slot["name"] += function.get("name") or ""
                    slot["arguments"] += function.get("arguments") or ""
        self._last_tool_calls = _calls_from(
            [{"function": pieces[i]} for i in sorted(pieces)]
        )


def _calls_from(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
    """OpenAI-shaped tool calls, arguments as a JSON string, into ToolCalls."""
    calls = []
    for call in raw_calls:
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        calls.append(ToolCall(name=name, arguments=arguments if isinstance(arguments, dict) else {}))
    return calls


def _image_part(data: str) -> dict[str, Any]:
    """An inline image in the OpenAI shape, with its type sniffed from the bytes."""
    head = base64.b64decode(data[:64] + "=" * (-len(data[:64]) % 4), validate=False)
    mime = "image/png"
    if head.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif head.startswith(b"GIF8"):
        mime = "image/gif"
    elif head[8:12] == b"WEBP":
        mime = "image/webp"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def build_messages(system: str, context: str, *, include_images: bool = False) -> list[dict[str, Any]]:
    """The orchestrator's JSON turn history as an OpenAI ``messages`` array.

    The same history ``model._build_messages`` turns into Ollama's shape, with
    the two differences this protocol insists on: every tool call has an id
    that its result echoes, and arguments travel as a JSON string.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    try:
        turns = json.loads(context) if context else []
    except (json.JSONDecodeError, TypeError):
        turns = None
    if not isinstance(turns, list):
        if context:
            messages.append({"role": "user", "content": context})
        return messages

    pending: list[str] = []  # ids of calls whose results have not been replayed yet
    for number, turn in enumerate(turns):
        role = turn.get("role", "user")
        content = turn.get("content", "") or ""
        tool_use = turn.get("tool_use")
        if role == "assistant" and tool_use:
            ids = [f"call_{number}_{k}" for k in range(len(tool_use))]
            pending = list(ids)
            messages.append({
                "role": "assistant",
                # "" rather than null: chat templates on the server side do
                # string operations on it, and a None fails some of them.
                "content": toolcalls.strip_markup(content),
                "tool_calls": [
                    {"id": call_id, "type": "function",
                     "function": {"name": tu["name"], "arguments": json.dumps(tu.get("arguments", {}))}}
                    for call_id, tu in zip(ids, tool_use)
                ],
            })
        elif role == "tool":
            call_id = pending.pop(0) if pending else f"call_{number}_orphan"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        else:
            message: dict[str, Any] = {"role": "assistant" if role == "assistant" else "user",
                                       "content": content}
            images = [img["data"] for img in (turn.get("images") or []) if img.get("data")]
            if images and include_images:
                message["content"] = [{"type": "text", "text": content}] + [_image_part(d) for d in images]
            messages.append(message)
    return messages

