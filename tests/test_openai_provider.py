"""Tests for the OpenAI-compatible provider (llama.cpp, LM Studio, vLLM)."""
from __future__ import annotations

import http.server
import json
import threading

import pytest
from conftest import write_config

from cobirb.config import Config
from cobirb.plugins.core.openai import OpenAICompatibleProvider, build_messages
from cobirb.runtime.models import build_for_role


class _Tool:
    def name(self):
        return "read_file"

    def description(self):
        return "read"

    def parameters(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}}


class _Server:
    """A tiny OpenAI-style server on loopback, recording what it was sent."""

    def __init__(self, reply=None, stream_events=None, props=None, models=None):
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/props" and props is not None:
                    return self._json(props)
                if self.path == "/v1/models":
                    return self._json({"data": models or [{"id": "m"}]})
                self._json({"error": "no"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event in stream_events or []:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._json(reply or {"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server_factory():
    servers = []

    def make(**kwargs):
        server = _Server(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_a_plain_reply_goes_to_chat_completions(server_factory):
    server = server_factory()
    provider = OpenAICompatibleProvider(model="m", base_url=server.url + "/v1")

    assert provider.chat("", '[{"role": "user", "content": "hello"}]') == "hi"
    path, body = server.requests[-1]
    assert path == "/v1/chat/completions"  # /v1 in the base URL is not doubled
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_a_tool_call_comes_back_with_parsed_arguments(server_factory):
    server = server_factory(reply={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]}}]})
    provider = OpenAICompatibleProvider(model="m", base_url=server.url)

    reply = provider.chat("", "[]", [_Tool()])

    assert reply == ""
    assert [(c.name, c.arguments) for c in provider.parse_tool_calls(reply)] == [("read_file", {"path": "a.py"})]
    assert server.requests[-1][1]["tools"][0]["function"]["name"] == "read_file"


def test_streamed_tool_call_fragments_are_stitched_together(server_factory):
    events = [
        {"choices": [{"delta": {"content": "Let me look."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "read_file", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "b.py"}'}}]}}]},
    ]
    server = server_factory(stream_events=events)
    provider = OpenAICompatibleProvider(model="m", base_url=server.url)

    text = "".join(provider.chat("", "[]", [_Tool()], stream=True))

    assert text == "Let me look."
    assert [(c.name, c.arguments) for c in provider.parse_tool_calls(text)] == [("read_file", {"path": "b.py"})]


def test_history_pairs_every_call_with_its_result_by_id():
    context = json.dumps([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_use": [
            {"name": "read_file", "arguments": {"path": "a"}},
            {"name": "read_file", "arguments": {"path": "b"}}]},
        {"role": "tool", "content": "A", "tool_use": [{"name": "read_file"}]},
        {"role": "tool", "content": "B", "tool_use": [{"name": "read_file"}]},
    ])
    messages = build_messages("sys", context)

    call_ids = [c["id"] for c in messages[2]["tool_calls"]]
    assert [m["tool_call_id"] for m in messages[3:]] == call_ids
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "a"}
    assert messages[0] == {"role": "system", "content": "sys"}


def test_the_window_is_read_from_the_server_and_capped(server_factory):
    server = server_factory(props={"default_generation_settings": {"n_ctx": 65536}})

    assert OpenAICompatibleProvider(model="m", base_url=server.url).context_window() == 65536
    assert OpenAICompatibleProvider(model="m", base_url=server.url, max_num_ctx=32768).context_window() == 32768


def test_the_window_falls_back_to_what_the_model_list_reports(server_factory):
    server = server_factory(models=[{"id": "m", "max_model_len": 40960}])

    assert OpenAICompatibleProvider(model="m", base_url=server.url).context_window() == 40960


def test_no_system_message_is_invented(server_factory):
    """No Modelfile to read back, and nothing of CoBirb's to add: nothing sent."""
    server = server_factory()
    OpenAICompatibleProvider(model="m", base_url=server.url).chat("", '[{"role": "user", "content": "x"}]')

    assert all(m["role"] != "system" for m in server.requests[-1][1]["messages"])


def test_options_are_sent_as_request_fields(server_factory):
    server = server_factory()
    OpenAICompatibleProvider(model="m", base_url=server.url, options={"temperature": 0.2, "seed": 3}).chat("", "[]")

    body = server.requests[-1][1]
    assert body["temperature"] == 0.2 and body["seed"] == 3


def test_config_selects_the_protocol_per_role(tmp_path):
    write_config(tmp_path, {"models": {
        "default": {"name": "m", "base_url": "http://localhost:8080", "api": "openai"},
        "worker": {"api": "ollama", "base_url": "http://localhost:11434"},
    }})

    assert isinstance(build_for_role("orchestrator", Config()), OpenAICompatibleProvider)
    assert not isinstance(build_for_role("worker", Config()), OpenAICompatibleProvider)
