"""Tests for the local Ollama-backed model provider.

No real network call is made: ``urllib.request.urlopen`` is monkeypatched
with a fake response so these tests exercise the request/response wiring
without requiring a running Ollama server.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from cobirb.plugins.core.model import LocalModelProvider, _build_messages
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


class _FakeStreamResponse:
    """Mimics an http.client.HTTPResponse's line-iteration for NDJSON bodies."""

    def __init__(self, chunks: list[dict]):
        self._lines = [json.dumps(c).encode("utf-8") + b"\n" for c in chunks]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        return iter(self._lines)


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


def test_list_models_returns_sorted_ids(monkeypatch):
    def fake_urlopen(request, timeout=None):
        assert request.full_url == "http://localhost:11434/v1/models"
        return _FakeResponse(
            {"object": "list", "data": [{"id": "gemma4", "object": "model"}, {"id": "llama3.1"}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert LocalModelProvider().list_models() == ["gemma4", "llama3.1"]


def test_list_models_ignores_entries_without_an_id(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"data": [{"id": "llama3.1"}, {"object": "model"}, {"id": ""}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert LocalModelProvider().list_models() == ["llama3.1"]


def test_list_models_handles_a_missing_data_key(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=None: _FakeResponse({}))
    assert LocalModelProvider().list_models() == []


def test_list_models_wraps_connection_errors(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="Could not reach the model provider"):
        LocalModelProvider().list_models()


def test_supports_tool_calling_requires_model():
    assert LocalModelProvider(model="llama3.1").supports_tool_calling() is True
    assert LocalModelProvider().supports_tool_calling() is False


def test_supports_streaming_is_true():
    assert LocalModelProvider(model="llama3.1").supports_streaming() is True


def test_stream_chat_yields_content_incrementally(monkeypatch):
    chunks = [
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    ]

    def fake_urlopen(request, timeout=None):
        return _FakeStreamResponse(chunks)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = LocalModelProvider(model="llama3.1")
    pieces = list(provider.chat("system", "context", stream=True))
    assert pieces == ["Hel", "lo"]


def test_stream_chat_captures_tool_calls_from_final_chunk(monkeypatch):
    chunks = [
        {"message": {"role": "assistant", "content": ""}, "done": False},
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}],
            },
            "done": True,
        },
    ]

    def fake_urlopen(request, timeout=None):
        return _FakeStreamResponse(chunks)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = LocalModelProvider(model="llama3.1")
    reply = provider.chat("system", "context", [_StubTool()], stream=True)
    list(reply)  # fully consume the generator so tool calls are captured
    assert provider.parse_tool_calls("") == [ToolCall(name="read_file", arguments={"path": "a.txt"})]


def test_stream_chat_wraps_connection_errors(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = LocalModelProvider(model="llama3.1")
    with pytest.raises(RuntimeError, match="Could not reach the model provider"):
        list(provider.chat("system", "context", stream=True))


# --------------------------------------------------------------------------- #
# _build_messages: turns the orchestrator's JSON turn history into a proper
# multi-turn Ollama messages array (the fix for the tool-calling loop that
# never converged — a tool result with no preceding assistant tool-call
# message gave the model no signal a call was already satisfied).
# --------------------------------------------------------------------------- #
def test_build_messages_empty_context_is_just_system():
    assert _build_messages("sys", "") == [{"role": "system", "content": "sys"}]
    assert _build_messages("sys", "[]") == [{"role": "system", "content": "sys"}]


def test_build_messages_plain_user_turn():
    context = json.dumps([{"role": "user", "content": "hello", "tool_use": None}])
    messages = _build_messages("sys", context)
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


def test_build_messages_reconstructs_tool_call_and_result():
    context = json.dumps(
        [
            {"role": "user", "content": "read a.txt", "tool_use": None},
            {
                "role": "assistant",
                "content": "",
                "tool_use": [{"name": "read_file", "arguments": {"path": "a.txt"}}],
            },
            {
                "role": "tool",
                "content": "file contents",
                "tool_use": [{"name": "read_file", "arguments": {"path": "a.txt"}}],
            },
        ]
    )
    messages = _build_messages("sys", context)
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read a.txt"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}],
        },
        {"role": "tool", "content": "file contents", "tool_name": "read_file"},
    ]


def test_build_messages_falls_back_for_non_json_context():
    """A plain (non-JSON) string context must still work as an opaque user
    message, rather than crashing or silently dropping it."""
    messages = _build_messages("sys", "just a plain string, not JSON")
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "just a plain string, not JSON"},
    ]
