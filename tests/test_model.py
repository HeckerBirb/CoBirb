"""Tests for the local Ollama-backed model provider.

No real network call is made: ``urllib.request.urlopen`` is monkeypatched
with a fake response so these tests exercise the request/response wiring
without requiring a running Ollama server.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from cobirb.plugins.core.model import LocalModelProvider
from cobirb.typing.spi import ToolCall


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _StubTool:
    name = "read_file"

    def description(self) -> str:
        return "Read a file."

    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


def test_chat_without_model_raises(monkeypatch):
    monkeypatch.delenv("COBIRB_MODEL_NAME", raising=False)
    provider = LocalModelProvider()
    with pytest.raises(RuntimeError, match="No model configured"):
        provider.chat("system", "context")


def test_chat_returns_content(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"message": {"role": "assistant", "content": "hello there"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = LocalModelProvider(model="llama3.1")
    reply = provider.chat("system", "context")
    assert reply == "hello there"


def test_chat_captures_tool_calls(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
                    ],
                }
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = LocalModelProvider(model="llama3.1")
    reply = provider.chat("system", "context", [_StubTool()])
    calls = provider.parse_tool_calls(reply)
    assert calls == [ToolCall(name="read_file", arguments={"path": "a.txt"})]


def test_chat_wraps_connection_errors(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = LocalModelProvider(model="llama3.1")
    with pytest.raises(RuntimeError, match="Could not reach the model provider"):
        provider.chat("system", "context")


def test_name_reflects_configured_model():
    assert LocalModelProvider(model="llama3.1").name() == "ollama/llama3.1"
    assert LocalModelProvider().name() == "(unconfigured)"


def test_supports_tool_calling_requires_model():
    assert LocalModelProvider(model="llama3.1").supports_tool_calling() is True
    assert LocalModelProvider().supports_tool_calling() is False
