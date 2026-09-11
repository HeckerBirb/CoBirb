"""Core model provider: a local Ollama-compatible chat backend.

CoBirb ships **no models** and makes **no outbound network calls by default**.
This provider only ever talks to a *local* Ollama server (default
``http://localhost:11434``, or wherever the user points it) and only once the
user has explicitly named a model — via ``--model``, ``COBIRB_MODEL_NAME``, or
``models.default.name`` in config. See DESIGN.md §6.3.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from ...typing.spi import ModelProvider, Tool, ToolCall

DEFAULT_BASE_URL = "http://localhost:11434"


def _tool_schema(tool: Tool) -> dict[str, Any]:
    """Convert a CoBirb Tool into the function-calling schema Ollama expects."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description(),
            "parameters": tool.parameters(),
        },
    }


def _extract_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Map Ollama's tool_calls shape onto CoBirb ToolCall objects."""
    return [
        ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments", {}) or {})
        for call in (message.get("tool_calls") or [])
    ]


class LocalModelProvider(ModelProvider):
    """Chats with a local Ollama server.

    No inference happens unless a model name is supplied; there is no
    embedded model and no default remote endpoint.
    """

    def __init__(self, model: str = "", base_url: str | None = None, cwd: str | None = None) -> None:
        self._model = model or os.environ.get("COBIRB_MODEL_NAME", "")
        self._base_url = (base_url or os.environ.get("COBIRB_OLLAMA_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.cwd = cwd or os.getcwd()
        # Ollama returns structured tool calls alongside the assistant message
        # in the same response; stash them here so parse_tool_calls() doesn't
        # need to re-parse text or make a second round trip.
        self._last_tool_calls: list[ToolCall] = []

    def name(self) -> str:
        return f"ollama/{self._model}" if self._model else "(unconfigured)"

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach the model provider at {self._base_url}: {exc}. "
                "Is Ollama running? (see DESIGN.md §6.3)"
            ) from exc

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
                "No model configured. Set --model, COBIRB_MODEL_NAME, or "
                "models.default.name in your CoBirb config. No models are "
                "embedded by default (see DESIGN.md §6.3)."
            )
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": context},
            ],
            "stream": stream,
        }
        if tools:
            payload["tools"] = [_tool_schema(t) for t in tools]

        if stream:
            return self._stream_chat(payload)

        response = self._post("/api/chat", payload)
        message = response.get("message", {})
        self._last_tool_calls = _extract_tool_calls(message)
        return message.get("content", "")

    def _stream_chat(self, payload: dict[str, Any]) -> Iterable[str]:
        """Yield content deltas as Ollama streams them (NDJSON response body).

        Each line is a complete JSON object for one increment; the last one
        has ``"done": true`` and carries any tool calls the model decided to
        make. ``_last_tool_calls`` is only accurate once the generator has
        been fully consumed.
        """
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=120)
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach the model provider at {self._base_url}: {exc}. "
                "Is Ollama running? (see DESIGN.md §6.3)"
            ) from exc

        tool_calls: list[ToolCall] = []
        with response:
            for raw_line in response:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                message = chunk.get("message", {})
                content = message.get("content", "")
                if content:
                    yield content
                if message.get("tool_calls"):
                    tool_calls = _extract_tool_calls(message)
                if chunk.get("done"):
                    break
        self._last_tool_calls = tool_calls

    def parse_tool_calls(self, raw: str) -> list[ToolCall]:
        return self._last_tool_calls

    def supports_tool_calling(self) -> bool:
        return bool(self._model)

    def supports_streaming(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False
