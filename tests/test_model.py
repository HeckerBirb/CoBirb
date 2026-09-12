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
from cobirb.plugins.core.tools import ReadFileTool
from cobirb.typing.spi import Tool, ToolCall, ToolResult


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
    def name(self) -> str:
        return "read_file"


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


# --------------------------------------------------------------------------- #
# Respecting the model's own Modelfile SYSTEM directive.
#
# Ollama accepts one system message per request and sending one *replaces*
# whatever SYSTEM the model was built with. CoBirb used to send one on every
# single turn, so a model created with `ollama create` around a custom SYSTEM
# behaved differently inside CoBirb than it did in `ollama run` — the user's
# own configuration was silently overridden and there was no way to turn that
# off.
# --------------------------------------------------------------------------- #
class _RoutingResponses:
    """Answers /api/show and /api/chat differently, recording what was asked.

    The provider now makes two different calls, so a single canned response
    can't exercise the interesting part any more.
    """

    def __init__(self, system: str | None = None, chat_payload: dict | None = None):
        self.system = system
        self.chat_payload = chat_payload or {"message": {"role": "assistant", "content": "ok"}}
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append((url, body))
        if url.endswith("/api/show"):
            return _FakeResponse({} if self.system is None else {"system": self.system})
        return _FakeResponse(self.chat_payload)

    @property
    def chat_messages(self) -> list[dict]:
        """The *most recent* chat request's messages — tests that send more
        than one turn care about the last one, not the first."""
        return [body for url, body in self.requests if url.endswith("/api/chat")][-1]["messages"]

    @property
    def chat_request(self) -> dict:
        """The most recent chat request's body."""
        return [body for url, body in self.requests if url.endswith("/api/chat")][-1]

    @property
    def show_calls(self) -> int:
        return sum(1 for url, _ in self.requests if url.endswith("/api/show"))


def test_no_system_message_is_sent_when_cobirb_has_nothing_to_add(monkeypatch):
    """The default. Omitting the message entirely is what lets Ollama apply
    the model's own SYSTEM — an empty system message would still replace it."""
    responses = _RoutingResponses(system="You are Karen Gemmason.")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="gemma4-unchained").chat("", "[]")

    assert all(m["role"] != "system" for m in responses.chat_messages)


def test_no_lookup_is_made_when_there_is_nothing_to_compose(monkeypatch):
    """Sending nothing needs no knowledge of the model's prompt, so the
    default path costs no extra round trip."""
    responses = _RoutingResponses(system="You are Karen Gemmason.")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="gemma4-unchained").chat("", "[]")

    assert responses.show_calls == 0


def test_the_models_own_prompt_leads_when_cobirb_adds_a_persona(monkeypatch):
    responses = _RoutingResponses(system="You are Karen Gemmason, a large language model.")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="gemma4-unchained").chat("You are Noah, a Parrot.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system == "You are Karen Gemmason, a large language model.\n\nYou are Noah, a Parrot."


def test_cobirbs_prompt_stands_alone_when_the_model_declares_no_system(monkeypatch):
    responses = _RoutingResponses(system=None)
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="plain").chat("You are Noah, a Parrot.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system == "You are Noah, a Parrot."


def test_the_models_own_prompt_is_fetched_once_and_reused(monkeypatch):
    """It can't change between requests, and paying a round trip per turn to
    re-read it would be pure latency."""
    responses = _RoutingResponses(system="Model prompt.")
    monkeypatch.setattr("urllib.request.urlopen", responses)
    provider = LocalModelProvider(model="gemma4-unchained")

    provider.chat("CoBirb prompt.", "[]")
    provider.chat("CoBirb prompt.", "[]")
    provider.chat("CoBirb prompt.", "[]")

    assert responses.show_calls == 1


def test_switching_models_re_reads_the_prompt(monkeypatch):
    responses = _RoutingResponses(system="First model's prompt.")
    monkeypatch.setattr("urllib.request.urlopen", responses)
    provider = LocalModelProvider(model="first")
    provider.chat("CoBirb prompt.", "[]")

    responses.system = "Second model's prompt."
    provider._model = "second"
    provider.chat("CoBirb prompt.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system.startswith("Second model's prompt.")
    assert responses.show_calls == 2


def test_an_unreadable_model_prompt_never_blocks_the_turn(monkeypatch):
    """Best-effort context, not a precondition: if the lookup fails the turn
    still runs with whatever CoBirb had to say."""
    def urlopen(request, timeout=None):
        if request.full_url.endswith("/api/show"):
            raise urllib.error.URLError("nope")
        return _FakeResponse({"message": {"role": "assistant", "content": "ok"}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    reply = LocalModelProvider(model="llama3.1").chat("CoBirb prompt.", "[]")

    assert reply == "ok"


def test_a_failed_lookup_is_not_cached_so_it_can_recover(monkeypatch):
    """A transient blip must not permanently drop the user's own prompt for
    the rest of the session."""
    provider = LocalModelProvider(model="llama3.1")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    assert provider.model_system_prompt() == ""

    responses = _RoutingResponses(system="Back up.")
    monkeypatch.setattr("urllib.request.urlopen", responses)
    assert provider.model_system_prompt() == "Back up."


def test_a_whitespace_only_model_prompt_is_treated_as_absent(monkeypatch):
    """Modelfile SYSTEM blocks routinely carry leading/trailing newlines."""
    responses = _RoutingResponses(system="\r\n   \r\n")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="m").chat("CoBirb prompt.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system == "CoBirb prompt."


def test_a_model_prompts_surrounding_whitespace_is_trimmed_when_composing(monkeypatch):
    responses = _RoutingResponses(system="\r\nYou are Karen Gemmason.\r\n")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="m").chat("Extra.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system == "You are Karen Gemmason.\n\nExtra."


def test_build_messages_omits_an_empty_system_message():
    assert _build_messages("", '[{"role": "user", "content": "hi"}]') == [
        {"role": "user", "content": "hi"}
    ]


def test_build_messages_keeps_a_system_message_when_there_is_one():
    messages = _build_messages("rules", '[{"role": "user", "content": "hi"}]')

    assert messages[0] == {"role": "system", "content": "rules"}


# --------------------------------------------------------------------------- #
# Plugin conformance (regression).
#
# The SPI declares Tool.name as a method. Every built-in implemented it as a
# plain string class attribute instead, so four consumers branched on
# `callable(tool.name)` — and the one that forgot, _tool_schema, put a bound
# method into the request payload. json.dumps then raised for every turn, for
# as long as a spec-conformant plugin was installed. Nothing covered a
# method-named tool reaching the provider, which is why it shipped.
# --------------------------------------------------------------------------- #
class _ConformantPluginTool(Tool):
    """A minimal tool written exactly as the SPI documents: nothing but the
    four required methods, `name` among them."""

    def name(self) -> str:
        return "plugin_tool"

    def description(self) -> str:
        return "does a plugin thing"

    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict) -> ToolResult:
        return ToolResult(ok=True, content="done")


def test_a_spec_conformant_tool_plugin_serializes_into_the_request(monkeypatch):
    responses = _RoutingResponses()
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="m").chat("", "[]", tools=[_ConformantPluginTool()])

    tool = responses.chat_request["tools"][0]
    assert tool["function"]["name"] == "plugin_tool"
    assert tool["function"]["description"] == "does a plugin thing"


def test_builtin_and_plugin_tools_serialize_identically(monkeypatch):
    """Whatever the built-ins do, a plugin implementing the documented
    interface must produce the same shape — that equivalence is the contract."""
    responses = _RoutingResponses()
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="m").chat("", "[]", tools=[ReadFileTool(), _ConformantPluginTool()])

    names = [t["function"]["name"] for t in responses.chat_request["tools"]]
    assert names == ["read_file", "plugin_tool"]
