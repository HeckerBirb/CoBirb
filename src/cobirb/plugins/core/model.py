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
            "stream": False,
        }
        if tools:
            payload["tools"] = [_tool_schema(t) for t in tools]

        response = self._post("/api/chat", payload)
        message = response.get("message", {})
        self._last_tool_calls = [
            ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments", {}) or {})
            for call in (message.get("tool_calls") or [])
        ]
        content = message.get("content", "")
        if stream:
            return iter([content])
        return content

    def parse_tool_calls(self, raw: str) -> list[ToolCall]:
        return self._last_tool_calls

    def supports_tool_calling(self) -> bool:
        return bool(self._model)

    def supports_streaming(self) -> bool:
        # Ollama supports true incremental streaming, but this provider does
        # not implement it yet (see todo-list.md); chat(stream=True) still
        # works, it just returns the full response as a single chunk.
        return False

    def supports_vision(self) -> bool:
        return False
