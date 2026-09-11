"""Core model provider adapter.

CoBirb ships **no models** and makes **no outbound calls by default**. This
provider simply shells out to whatever provider the user configured (Ollama,
an OpenAI-compatible endpoint, etc.). It is therefore a *local adapter* that
delegates inference to a tool the user has opted into. See DESIGN.md §6.3.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Optional

from ...typing.spi import ModelProvider, ToolCall, ToolResult
from .tools import ToolRegistry, _first_word


class LocalModelProvider(ModelProvider):
    """A model provider that delegates to a user-configured inference tool.

    The tool name/path is configured by the user (e.g. an Ollama chat endpoint,
    or an OpenAI-compatible API). No models are embedded here.
    """

    def __init__(self, tool: str = "", cwd: str | None = None) -> None:
        self._tool = tool or os.environ.get("COBIRB_MODEL_TOOL", "")
        self.cwd = cwd or os.getcwd()
        self._registry = ToolRegistry(self.cwd)

    def name(self) -> str:
        return self._tool or "(unconfigured)"

    def _invoke(self, system: str, context: str, tools: Optional[list[Tool]]) -> str:
        if not self._tool:
            raise RuntimeError(
                "No model provider configured. Set COBIRB_MODEL_TOOL or configure a model "
                "in your CoBirb config (see DESIGN.md §6.3). No models are embedded by default."
            )
        # The actual inference call is performed by the configured tool. The exact
        # payload is provider-specific; this is where a concrete provider plugin
        # would inject its request/response logic.
        return self._registry.get(self._tool.split()[0]).execute(
            {"prompt": system + "\n" + context, "tools": [t.name for t in (tools or [])]}
        ).content

    def chat(
        self,
        system: str,
        context: str,
        tools: Optional[list[Tool]] = None,
        *,
        stream: bool = False,
    ) -> "Iterable[str] | str":
        if stream:
            # Streaming is delegated to the provider tool; yield tokens as it returns.
            content = self._invoke(system, context, tools)
            for token in content.splitlines():
                yield token
        else:
            yield self._invoke(system, context, tools)

    def parse_tool_calls(self, raw: str) -> list[ToolCall]:
        # Concrete providers parse tool-call JSON into ToolCall objects.
        return []

    def supports_tool_calling(self) -> bool:
        return bool(self._tool)

    def supports_streaming(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False
