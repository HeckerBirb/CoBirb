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
from cobirb.typing.spi import SteeringInterrupted, Tool, ToolCall, ToolResult


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


def _streaming_server(chunks):
    """A local NDJSON server, since streaming now uses http.client directly.

    Streaming goes through ``http.client`` (so a stuck read can be interrupted
    by shutting the socket), which a ``urllib.request.urlopen`` mock never
    reaches — so these run against a real socket,
    which also exercises the chunked-transfer reading the mock never did.

    ``chat()`` calls ``/api/show`` before *every* request, streaming or not —
    so this answers that one plainly and reserves the given ``chunks`` for
    ``/api/chat``. Without the split, ``/api/show`` gets the same NDJSON body
    and its ``_post`` fails to parse it as one JSON object; ``_show()`` happens
    to swallow that failure today, but a test relying on an unrelated method's
    error-swallowing to pass is a trap for whoever changes that method next.
    """
    import http.server
    import threading

    class _Fake(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            if self.path.endswith("/api/show"):
                body = json.dumps({"parameters": "", "model_info": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in chunks:
                line = (json.dumps(chunk) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_stream_chat_yields_content_incrementally():
    url = _streaming_server([
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    ])
    provider = LocalModelProvider(model="llama3.1", base_url=url)
    assert list(provider.chat("system", "context", stream=True)) == ["Hel", "lo"]


def test_stream_chat_captures_tool_calls_from_final_chunk():
    url = _streaming_server([
        {"message": {"role": "assistant", "content": ""}, "done": False},
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}],
            },
            "done": True,
        },
    ])
    provider = LocalModelProvider(model="llama3.1", base_url=url)
    reply = provider.chat("system", "context", [_StubTool()], stream=True)
    list(reply)  # fully consume the generator so tool calls are captured
    assert provider.parse_tool_calls("") == [ToolCall(name="read_file", arguments={"path": "a.txt"})]


def test_stream_chat_wraps_connection_errors(monkeypatch):
    """The transport failure keeps the "is it running?" message, which is the
    right question when the socket is dead.

    Forced rather than aimed at a real closed port: an unbound low port is
    "connection refused" (fast) on a normal machine, but this sandbox's
    network silently drops the packets instead, so the connect blocks for the
    full 120s timeout baked into ``_connect``. Monkeypatching ``connect``
    itself makes the failure immediate and independent of what any given
    machine's network does with an unbound port.
    """
    import http.client

    def refuse(self):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    provider = LocalModelProvider(model="llama3.1", base_url="http://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="Could not reach the model provider"):
        list(provider.chat("system", "context", stream=True))


def test_a_stuck_stream_can_be_cancelled_from_another_thread():
    """The force-stop's foundation: a worker blocked waiting for the next token
    must be interruptible. Closing the response is not enough — a blocked recv
    only wakes on socket shutdown — so this guards the http.client rewrite that
    made it possible."""
    import http.server
    import threading
    import time

    class _Hang(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            # chat() calls /api/show before every request, streaming or not —
            # answer that one immediately, exactly as a healthy Ollama would,
            # and only hang the actual generation. A handler that hangs on
            # every POST indiscriminately never reaches the streaming call at
            # all, and "stuck in /api/show" is a real but different bug (see
            # _connect's docstring) from the one this test is about.
            if self.path.endswith("/api/show"):
                body = json.dumps({"parameters": "", "model_info": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            line = (json.dumps({"message": {"content": "one "}}) + "\n").encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            self.wfile.flush()
            time.sleep(30)  # then hang, like a stuck generation

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hang)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = LocalModelProvider(model="m", base_url=f"http://127.0.0.1:{server.server_address[1]}")

    received, error = [], []

    def consume():
        try:
            for chunk in provider.chat("", "hi", stream=True):
                received.append(chunk)
        except Exception as exc:  # noqa: BLE001
            error.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    time.sleep(1.0)  # let the first token arrive and the read block

    provider.cancel()
    worker.join(timeout=5)

    assert not worker.is_alive()  # actually unblocked, not merely asked to stop
    assert received == ["one "]
    assert isinstance(error[0], RuntimeError)
    assert "cancelled" in str(error[0])


def test_interrupting_a_reply_raises_but_leaves_the_provider_usable():
    """Mid-turn steering's resumable half. Unlike ``cancel()``, interrupting a
    stream must not stop the provider for good — the very next request has to
    work normally. Proven against a real socket for the same reason
    force-stop was (a mock can't show a shutdown from another thread waking a
    blocked recv, or that the connection *after* it is unaffected).
    """
    import http.server
    import threading
    import time

    calls = {"chat": 0}

    class _Hang(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            if self.path.endswith("/api/show"):
                body = json.dumps({"parameters": "", "model_info": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            calls["chat"] += 1
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if calls["chat"] == 1:
                line = (json.dumps({"message": {"content": "one "}}) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
                time.sleep(30)  # then hang, as if mid-generation
                return
            for chunk in (
                {"message": {"content": "hi"}, "done": False},
                {"message": {"content": ""}, "done": True},
            ):
                line = (json.dumps(chunk) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hang)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = LocalModelProvider(model="m", base_url=f"http://127.0.0.1:{server.server_address[1]}")

    received, error = [], []

    def consume():
        try:
            for chunk in provider.chat("", "hi", stream=True):
                received.append(chunk)
        except Exception as exc:  # noqa: BLE001
            error.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    time.sleep(1.0)  # let the first token arrive and the read block

    had_something_to_cut_off = provider.interrupt_current_reply()
    worker.join(timeout=5)

    assert had_something_to_cut_off
    assert received == ["one "]
    assert len(error) == 1 and isinstance(error[0], SteeringInterrupted)

    # The provider is not latched closed the way cancel() leaves it — the
    # very next request behaves exactly as if nothing had happened.
    assert list(provider.chat("", "hi again", stream=True)) == ["hi"]


def test_interrupting_a_reply_with_nothing_in_flight_reports_that_plainly():
    provider = LocalModelProvider(model="m", base_url="http://127.0.0.1:1")
    assert provider.interrupt_current_reply() is False


def test_a_leftover_steer_signal_cannot_disguise_the_next_real_failure(monkeypatch):
    """An interrupt that lands in the gap between the last chunk and the
    generator finishing sets the steer signal with no exception left to
    consume it. If that signal survived into the next request, the next
    genuine failure — an unreachable server — would be reported as a steer
    instead of as itself, which is the same class of mistake as the 404 once
    reported as "is Ollama running?". A fresh request clears it."""
    import http.client

    def refuse(self):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    provider = LocalModelProvider(model="m", base_url="http://127.0.0.1:1")
    provider._steer_signal.set()  # as a mistimed interrupt would have left it

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
# whatever SYSTEM the model was built with. Sending one on every turn makes a
# model created with `ollama create` around a custom SYSTEM behave differently
# inside CoBirb than it does in `ollama run` — the user's
# own configuration was silently overridden and there was no way to turn that
# off.
# --------------------------------------------------------------------------- #
class _RoutingResponses:
    """Answers /api/show and /api/chat differently, recording what was asked.

    The provider now makes two different calls, so a single canned response
    can't exercise the interesting part any more.
    """

    def __init__(self, system: str | None = None, chat_payload: dict | None = None,
                 model_info: dict | None = None, capabilities: list | None = None):
        self.system = system
        self.model_info = model_info
        self.capabilities = capabilities
        self.chat_payload = chat_payload or {"message": {"role": "assistant", "content": "ok"}}
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append((url, body))
        if url.endswith("/api/show"):
            shown: dict = {}
            if self.system is not None:
                shown["system"] = self.system
            if self.model_info is not None:
                shown["model_info"] = self.model_info
            if self.capabilities is not None:
                shown["capabilities"] = self.capabilities
            return _FakeResponse(shown)
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


def test_the_model_is_described_once_however_much_is_wanted_from_it(monkeypatch):
    """The Modelfile's SYSTEM directive and its context window both live in
    the same /api/show payload, so it is fetched once per model and shared.
    A turn that sends no system prompt still needs the window, so this is no
    longer zero — but it must not be two."""
    responses = _RoutingResponses(system="You are Karen Gemmason.")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    provider = LocalModelProvider(model="gemma4-unchained")
    provider.chat("", "[]")
    provider.chat("with a system prompt this time", "[]")

    assert responses.show_calls == 1


def test_the_models_own_prompt_leads_when_cobirb_adds_its_own(monkeypatch):
    responses = _RoutingResponses(system="You are Karen Gemmason, a large language model.")
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="gemma4-unchained").chat("CoBirb's harness block.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system == "You are Karen Gemmason, a large language model.\n\nCoBirb's harness block."


def test_cobirbs_prompt_stands_alone_when_the_model_declares_no_system(monkeypatch):
    responses = _RoutingResponses(system=None)
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="plain").chat("CoBirb's harness block.", "[]")

    system = next(m for m in responses.chat_messages if m["role"] == "system")["content"]
    assert system == "CoBirb's harness block."


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


# --------------------------------------------------------------------------- #
# Context window discovery.
#
# A model advertises a context_length far larger than Ollama will actually
# serve: without num_ctx in the Modelfile, Ollama uses its own default no
# matter what the model claims. Believing the advertised figure means packing
# a request the server then silently truncates — the exact failure
# cobirb.context exists to prevent — so only num_ctx is trusted.
# --------------------------------------------------------------------------- #
class _ShowResponses:
    """Answers /api/show with a given parameters block."""

    def __init__(self, parameters=None, model_info=None, capabilities=None):
        self.payload = {}
        if parameters is not None:
            self.payload["parameters"] = parameters
        if model_info is not None:
            self.payload["model_info"] = model_info
        if capabilities is not None:
            self.payload["capabilities"] = capabilities

    def __call__(self, request, timeout=None):
        return _FakeResponse(self.payload)


def test_context_window_reads_num_ctx(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _ShowResponses("stop \"<|im_end|>\"\nnum_ctx 32768"))

    assert LocalModelProvider(model="m").context_window() == 32768


def test_the_advertised_context_length_is_used_when_no_num_ctx_is_set(monkeypatch):
    """CoBirb asks for a window rather than discovering one. Ollama serves
    4096 by default, but /api/chat takes options.num_ctx — so what the model
    says it can do is what to request, not something to distrust."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _ShowResponses(parameters="stop \"x\"", model_info={"llama.context_length": 131072}),
    )

    assert LocalModelProvider(model="m").context_window() == 131072


def test_an_explicit_num_ctx_outranks_the_advertised_length(monkeypatch):
    """A Modelfile naming num_ctx is a deliberate choice by whoever built the
    model."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _ShowResponses(parameters="num_ctx 32768", model_info={"llama.context_length": 131072}),
    )

    assert LocalModelProvider(model="m").context_window() == 32768


def test_the_window_is_stated_on_every_chat_request(monkeypatch):
    """Without this Ollama serves its own default however large the model is,
    and a long conversation is truncated from the front with nobody told."""
    responses = _RoutingResponses(model_info={"llama.context_length": 65536})
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="m").chat("", "[]")

    assert responses.chat_request["options"]["num_ctx"] == 65536


def test_context_window_is_none_when_the_endpoint_cannot_answer(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    assert LocalModelProvider(model="m").context_window() is None


def test_context_window_is_looked_up_once_per_model(monkeypatch):
    calls = []

    def counting(request, timeout=None):
        calls.append(request.full_url)
        return _FakeResponse({"parameters": "num_ctx 16384"})

    monkeypatch.setattr("urllib.request.urlopen", counting)
    provider = LocalModelProvider(model="m")

    assert provider.context_window() == 16384
    assert provider.context_window() == 16384
    assert len(calls) == 1


def test_max_num_ctx_clamps_an_advertised_window_that_exceeds_it(monkeypatch):
    """An architecture's advertised max (Qwen2's is 262144) sized as KV cache
    can exceed a card's VRAM well before weights and everything else sharing
    it are counted. max_num_ctx is the ceiling for that."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _ShowResponses(parameters="stop \"x\"", model_info={"qwen2.context_length": 262144}),
    )

    assert LocalModelProvider(model="m", max_num_ctx=32768).context_window() == 32768


def test_max_num_ctx_never_raises_a_window_that_came_out_lower(monkeypatch):
    """The model still dictates the window whenever it asks for less than the
    ceiling — max_num_ctx only ever clamps down, never up."""
    monkeypatch.setattr("urllib.request.urlopen", _ShowResponses(parameters="num_ctx 8192"))

    assert LocalModelProvider(model="m", max_num_ctx=32768).context_window() == 8192


def test_max_num_ctx_does_nothing_when_the_endpoint_cannot_answer(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    assert LocalModelProvider(model="m", max_num_ctx=32768).context_window() is None


# --------------------------------------------------------------------------- #
# What a failure actually says.
#
# `urllib.error.HTTPError` is a *subclass* of `URLError`, so one
# `except URLError` caught both "nothing is listening" and "the server
# answered, and the answer was no" — and reported them identically. A model
# the server did not have came back as "Is Ollama running?", with the real
# reason sitting unread in the response body. Found by a user whose Worker
# Birbs all failed against a running Ollama.
# --------------------------------------------------------------------------- #
import json as _json
import threading as _threading
import urllib.error as _urllib_error
from http.server import BaseHTTPRequestHandler as _Handler, HTTPServer as _HTTPServer

import pytest as _pytest

from cobirb.plugins.core.model import LocalModelProvider as _Provider


def _server(code, body):
    class _Fake(_Handler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            raw = _json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return None

    server = _HTTPServer(("127.0.0.1", 0), _Fake)
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_a_model_the_server_does_not_have_says_so():
    """The server already said what was wrong. Repeating it beats guessing."""
    url = _server(404, {"error": 'model "ornith-1.5" not found, try pulling it first'})

    with _pytest.raises(RuntimeError) as caught:
        _Provider(model="ornith-1.5", base_url=url).chat("", "hi")

    message = str(caught.value)
    assert "does not have 'ornith-1.5'" in message
    assert "try pulling it first" in message  # the server's own words
    assert "Is Ollama running?" not in message  # it plainly was


def test_a_server_error_is_reported_as_one_not_as_a_dead_socket():
    url = _server(500, {"error": "out of memory"})

    with _pytest.raises(RuntimeError, match="out of memory"):
        _Provider(model="m", base_url=url).chat("", "hi")


def test_an_openai_style_nested_error_is_also_read():
    """Ollama puts the message under `error`; other compatible servers nest it
    under `error.message`."""
    url = _server(400, {"error": {"message": "context length exceeded", "type": "invalid"}})

    with _pytest.raises(RuntimeError, match="context length exceeded"):
        _Provider(model="m", base_url=url).chat("", "hi")


def test_a_server_that_is_genuinely_not_there_still_asks_the_right_question(monkeypatch):
    """The old message was correct for this case, and only this case.

    Forced rather than aimed at a real closed port, for the same reason as the
    streaming version of this test: an unbound low port is "connection
    refused" (fast) on a normal machine, but this sandbox's network silently
    drops the packets instead, so a real connection attempt blocks for the
    full 120s timeout. ``urllib.request.urlopen`` builds an
    ``http.client.HTTPConnection`` under the hood, so the same monkeypatch
    used for the streaming path works here too.
    """
    import http.client

    def refuse(self):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    with _pytest.raises(RuntimeError, match="Is Ollama running?"):
        _Provider(model="m", base_url="http://127.0.0.1:1").chat("", "hi")


# --------------------------------------------------------------------------- #
# Vision
# --------------------------------------------------------------------------- #
def test_supports_vision_reads_capabilities(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _ShowResponses(capabilities=["completion", "vision"]))
    assert LocalModelProvider(model="m").supports_vision() is True


def test_supports_vision_is_false_without_the_capability(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _ShowResponses(capabilities=["completion", "tools"]))
    assert LocalModelProvider(model="m").supports_vision() is False


def test_supports_vision_is_false_when_show_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert LocalModelProvider(model="m").supports_vision() is False


def test_images_are_attached_when_the_model_supports_vision(monkeypatch):
    responses = _RoutingResponses(capabilities=["vision"])
    monkeypatch.setattr("urllib.request.urlopen", responses)
    context = json.dumps(
        [{"role": "user", "content": "what is this", "images": [{"id": "a", "filename": "x.png", "data": "QUJD"}]}]
    )

    LocalModelProvider(model="m").chat("", context)

    assert responses.chat_messages[0]["images"] == ["QUJD"]


def test_images_are_not_attached_when_the_model_lacks_vision(monkeypatch):
    responses = _RoutingResponses(capabilities=["completion"])
    monkeypatch.setattr("urllib.request.urlopen", responses)
    context = json.dumps(
        [{"role": "user", "content": "what is this", "images": [{"id": "a", "filename": "x.png", "data": "QUJD"}]}]
    )

    LocalModelProvider(model="m").chat("", context)

    assert "images" not in responses.chat_messages[0]


def test_a_historical_turn_with_no_data_field_attaches_nothing():
    """Compaction already turned older attachments into a text marker with
    no `data` key left — nothing here for the provider to attach either way."""
    from cobirb.plugins.core.model import _build_messages

    context = json.dumps(
        [{"role": "user", "content": "[image: old.png]", "images": [{"id": "a", "filename": "old.png"}]}]
    )
    messages = _build_messages("", context, include_images=True)
    assert "images" not in messages[0]


def test_configured_options_ride_on_every_chat_request_beside_the_window(monkeypatch):
    responses = _RoutingResponses(model_info={"llama.context_length": 8192})
    monkeypatch.setattr("urllib.request.urlopen", responses)

    LocalModelProvider(model="m", options={"seed": 42, "temperature": 0.2}).chat("", "[]")

    assert responses.chat_request["options"] == {"seed": 42, "temperature": 0.2, "num_ctx": 8192}


# --------------------------------------------------------------------------- #
# Tool calls written as text
# --------------------------------------------------------------------------- #
class _Tool:
    def name(self):
        return "read_file"

    def description(self):
        return "read"

    def parameters(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}}


def test_a_call_written_into_the_reply_is_read_when_no_native_call_came(monkeypatch):
    content = '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'
    responses = _RoutingResponses(chat_payload={"message": {"role": "assistant", "content": content}})
    monkeypatch.setattr("urllib.request.urlopen", responses)
    provider = LocalModelProvider(model="m")

    reply = provider.chat("", "[]", [_Tool()])
    calls = provider.parse_tool_calls(reply)

    assert [(c.name, c.arguments) for c in calls] == [("read_file", {"path": "a.py"})]


def test_text_is_not_read_for_calls_when_no_tools_were_offered(monkeypatch):
    content = '{"name": "read_file", "arguments": {"path": "a.py"}}'
    responses = _RoutingResponses(chat_payload={"message": {"role": "assistant", "content": content}})
    monkeypatch.setattr("urllib.request.urlopen", responses)
    provider = LocalModelProvider(model="m")

    assert provider.parse_tool_calls(provider.chat("", "[]")) == []


def test_the_servers_own_parse_failure_is_a_problem_not_an_answer(monkeypatch):
    responses = _RoutingResponses(chat_payload={"message": {
        "role": "assistant", "content": "error parsing tool call: raw='<function=read_file>', err=XML syntax error"}})
    monkeypatch.setattr("urllib.request.urlopen", responses)
    provider = LocalModelProvider(model="m")

    reply = provider.chat("", "[]", [_Tool()])

    assert reply == ""
    assert provider.parse_tool_calls(reply) == []
    assert "could not parse" in provider.malformed_tool_call()


def test_replayed_history_carries_a_text_call_once_not_twice():
    context = json.dumps([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": 'Reading.<tool_call>{"name":"read_file"}</tool_call>',
         "tool_use": [{"name": "read_file", "arguments": {"path": "a"}}]},
    ])
    messages = _build_messages("", context)
    assert messages[-1]["content"] == "Reading."
    assert messages[-1]["tool_calls"][0]["function"]["name"] == "read_file"


def test_a_thinking_models_reasoning_comes_back_with_the_calls_it_led_to(monkeypatch):
    """gpt-oss expects its earlier reasoning within a task to be replayed with
    its tool calls; without it the model lost its own working between steps."""
    responses = _RoutingResponses(chat_payload={"message": {
        "role": "assistant", "content": "", "thinking": "I should read a.py first.",
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}})
    monkeypatch.setattr("urllib.request.urlopen", responses)
    provider = LocalModelProvider(model="m")

    reply = provider.chat("", '[{"role": "user", "content": "go"}]', [_Tool()])
    provider.parse_tool_calls(reply)
    history = json.dumps([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_use": [{"name": "read_file", "arguments": {"path": "a.py"}}]},
        {"role": "tool", "content": "x = 1", "tool_use": [{"name": "read_file"}]},
        {"role": "assistant", "content": "Final answer."},
    ])
    provider.chat("", history, [_Tool()])

    replayed = responses.chat_messages
    assert replayed[1]["thinking"] == "I should read a.py first."
    assert "thinking" not in replayed[3]  # only calls carry it, never a final answer
