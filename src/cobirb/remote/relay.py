"""A remote worker's model calls, answered by the main session's model.

With ``run_llms_locally`` false (the default) a Remote Worker Birb has no model
of its own. ``RelayProvider`` stands in for one on the remote: each ``chat``
becomes a ``model`` event sent to the main machine, which runs it through the
session's own provider (``answer_model_request``) and streams the reply back.
The remote needs no GPU and no endpoint, and the main session's endpoint is
never exposed to it.

The relay passes on what the main provider decided — the text, the tool calls
it read, and whether a call was malformed — so the remote's orchestrator sees
exactly what a local worker would.
"""
from __future__ import annotations

import itertools
import queue
import threading
from typing import Any, Callable, Iterator

from ..typing import spi as cobirb_typing

_END = object()


class _ToolSchema(cobirb_typing.Tool):
    """A remote tool, as the main provider needs to see it: a schema only."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec

    def name(self) -> str:
        return str(self._spec.get("name", ""))

    def description(self) -> str:
        return str(self._spec.get("description", ""))

    def parameters(self) -> dict[str, Any]:
        parameters = self._spec.get("parameters")
        return parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any]) -> cobirb_typing.ToolResult:  # pragma: no cover
        raise RuntimeError("a relayed tool runs on the remote, never here")


def tool_specs(tools: "list[cobirb_typing.Tool] | None") -> list[dict[str, Any]]:
    return [{"name": t.name(), "description": t.description(), "parameters": t.parameters()}
            for t in (tools or [])]


def model_info(provider: Any) -> dict[str, Any]:
    """What the remote needs to know about the main provider up front."""
    def ask(name: str, default: Any) -> Any:
        hook = getattr(provider, name, None)
        try:
            return hook() if callable(hook) else default
        except Exception:  # noqa: BLE001 - an unanswerable question is the default, not a failed flock
            return default
    return {
        "name": ask("name", "relay"),
        "context_window": ask("context_window", None),
        "tool_calling": bool(ask("supports_tool_calling", True)),
        "vision": bool(ask("supports_vision", False)),
    }


def answer_model_request(provider: Any, request: dict[str, Any], send: Callable[..., None]) -> None:
    """Run one relayed call through ``provider`` and stream the answer back.

    ``send(kind, **fields)`` delivers ``model_chunk`` / ``model_end`` /
    ``model_error`` to the remote. Never raises: a failed call is reported to
    the remote, whose worker then fails the way a local one would.
    """
    req_id = request.get("req_id")
    tools = [_ToolSchema(spec) for spec in request.get("tools") or []]
    system, context = str(request.get("system", "")), request.get("context", "")
    try:
        chunks: list[str] = []
        if request.get("stream") and provider.supports_streaming():
            for chunk in provider.chat(system, context, tools, stream=True):
                chunks.append(chunk)
                send("model_chunk", req_id=req_id, text=chunk)
        else:
            text = provider.chat(system, context, tools, stream=False)
            chunks.append(str(text))
            send("model_chunk", req_id=req_id, text=str(text))
        reply = "".join(chunks)
        calls = provider.parse_tool_calls(reply) if tools and provider.supports_tool_calling() else []
        malformed_hook = getattr(provider, "malformed_tool_call", None)
        malformed = malformed_hook() if callable(malformed_hook) else ""
        send("model_end", req_id=req_id, malformed=malformed or "",
             tool_calls=[{"name": c.name, "arguments": c.arguments} for c in calls])
    except Exception as exc:  # noqa: BLE001 - reported to the remote, whose worker fails as a local one would
        send("model_error", req_id=req_id, error=str(exc))


class RelayProvider(cobirb_typing.ModelProvider):
    """The remote end: a model provider whose every call goes home.

    ``emit(event)`` sends a ``model`` event towards the main machine; the job's
    reader calls ``deliver(message)`` with each ``model_chunk`` / ``model_end``
    / ``model_error`` that comes back.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None], info: dict[str, Any]) -> None:
        self._emit = emit
        self._info = info or {}
        self._ids = itertools.count(1)
        self._waiting: dict[int, "queue.Queue[Any]"] = {}
        self._lock = threading.Lock()
        self._last_calls: list[cobirb_typing.ToolCall] = []
        self._malformed = ""

    def name(self) -> str:
        return f"relay/{self._info.get('name', 'main')}"

    def supports_tool_calling(self) -> bool:
        return bool(self._info.get("tool_calling", True))

    def supports_streaming(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return bool(self._info.get("vision", False))

    def context_window(self) -> int | None:
        window = self._info.get("context_window")
        return int(window) if isinstance(window, int) else None

    def malformed_tool_call(self) -> str:
        return self._malformed

    def parse_tool_calls(self, raw: str) -> list[cobirb_typing.ToolCall]:
        return list(self._last_calls)

    def deliver(self, message: dict[str, Any]) -> None:
        with self._lock:
            waiting = self._waiting.get(message.get("req_id"))
        if waiting is not None:
            waiting.put(message)

    def chat(self, system: str, context: Any, tools=None, *, stream: bool = False):
        req_id = next(self._ids)
        replies: "queue.Queue[Any]" = queue.Queue()
        with self._lock:
            self._waiting[req_id] = replies
        self._last_calls, self._malformed = [], ""
        self._emit({"kind": "model", "req_id": req_id, "system": system, "context": context,
                    "tools": tool_specs(tools), "stream": True})
        chunks = self._stream(req_id, replies)
        return chunks if stream else "".join(chunks)

    def _stream(self, req_id: int, replies: "queue.Queue[Any]") -> Iterator[str]:
        try:
            while True:
                message = replies.get()
                kind = message.get("type")
                if kind == "model_chunk":
                    yield str(message.get("text", ""))
                elif kind == "model_end":
                    self._last_calls = [
                        cobirb_typing.ToolCall(name=str(c.get("name", "")), arguments=dict(c.get("arguments") or {}))
                        for c in message.get("tool_calls") or [] if isinstance(c, dict)
                    ]
                    self._malformed = str(message.get("malformed") or "")
                    return
                else:
                    raise RuntimeError(f"The main session's model failed: {message.get('error', 'unknown error')}")
        finally:
            with self._lock:
                self._waiting.pop(req_id, None)
