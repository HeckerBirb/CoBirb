"""Tests for the MCP client, run against a real server over a real pipe.

The server below is a fifty-line script rather than a mock object, deliberately.
What is worth testing here is the protocol — a handshake, a framing, a
subprocess that can die halfway — and none of that is exercised by a double
that returns dictionaries. It costs a process per test and buys tests that
would catch a wire-format mistake.
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

from cobirb.config import Config
from cobirb.mcp import McpError, StdioClient, connect_servers, tool_name_for
from cobirb.orchestrator import build_default_policy

# A minimal but genuine MCP server: initialize, tools/list, tools/call.
_SERVER = '''
import json, os, sys

TOOLS = [
    {
        "name": "echo",
        "description": "Repeats what it is given.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {"name": "boom", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "leak", "description": "Reports its environment.", "inputSchema": {"type": "object"}},
]


def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        reply(message["id"], {"protocolVersion": "2025-06-18", "capabilities": {},
                              "serverInfo": {"name": "fixture", "version": "1"}})
    elif method == "tools/list":
        reply(message["id"], {"tools": TOOLS})
    elif method == "tools/call":
        name = message["params"]["name"]
        arguments = message["params"].get("arguments", {})
        if name == "boom":
            reply(message["id"], {"content": [{"type": "text", "text": "it broke"}],
                                  "isError": True})
        elif name == "leak":
            reply(message["id"], {"content": [{"type": "text",
                                               "text": json.dumps(sorted(os.environ))}]})
        else:
            reply(message["id"], {"content": [{"type": "text",
                                               "text": arguments.get("text", "")}]})
    elif method and method.startswith("notifications/"):
        continue
'''

_SILENT_SERVER = '''
import sys, time
sys.stderr.write("could not find the database\\n")
sys.stderr.flush()
time.sleep(30)
'''


def _server(tmp_path, name="server.py", source=_SERVER) -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return str(path)


@pytest.fixture
def client(tmp_path):
    connected = StdioClient(
        name="fixture", command=sys.executable, args=[_server(tmp_path)], cwd=str(tmp_path)
    )
    connected.start()
    yield connected
    connected.close()


def _user_config(tmp_path, data) -> Config:
    home = tmp_path / ".cobirb"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(data))
    return Config(cwd=str(tmp_path))


def _server_config(tmp_path, **overrides) -> Config:
    return _user_config(
        tmp_path,
        {
            "mcp_servers": {
                "fixture": {
                    "command": sys.executable,
                    "args": [_server(tmp_path)],
                    **overrides,
                }
            }
        },
    )


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #
def test_a_server_can_be_started_and_says_who_it_is(client):
    assert client.running
    assert client.server_info.get("name") == "fixture"


def test_a_servers_tools_can_be_listed(client):
    assert {tool["name"] for tool in client.list_tools()} == {"echo", "boom", "leak"}


def test_a_tool_can_be_called_and_its_text_comes_back(client):
    ok, text = client.call_tool("echo", {"text": "hello"})

    assert ok
    assert text == "hello"


def test_a_tool_reporting_an_error_is_a_failed_call_not_a_raised_one(client):
    """The model should see the message and adapt, exactly as it does when a
    built-in tool returns ok=False."""
    ok, text = client.call_tool("boom", {})

    assert not ok
    assert "it broke" in text


def test_closing_stops_the_process(client):
    client.close()

    assert not client.running


def test_closing_twice_is_harmless(client):
    client.close()
    client.close()


def test_a_command_that_does_not_exist_is_reported_not_raised_as_oserror(tmp_path):
    broken = StdioClient(name="nope", command=str(tmp_path / "not-here"))

    with pytest.raises(McpError, match="could not start"):
        broken.start()


def test_a_server_that_never_answers_times_out_and_quotes_its_own_stderr(tmp_path):
    """A bare timeout leaves the actual cause — a missing package, a bad path —
    sitting unread in a pipe."""
    silent = StdioClient(
        name="silent",
        command=sys.executable,
        args=[_server(tmp_path, "silent.py", _SILENT_SERVER)],
        startup_timeout=2,
    )

    with pytest.raises(McpError, match="could not find the database"):
        silent.start()
    silent.close()


# --------------------------------------------------------------------------- #
# Privacy: what a server is given
# --------------------------------------------------------------------------- #
def test_a_server_does_not_inherit_the_users_environment(tmp_path, client, monkeypatch):
    """The one place a CoBirb-spawned subprocess could read cloud credentials
    and tokens for services CoBirb has nothing to do with, then send them
    anywhere it likes."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-travel")
    fresh = StdioClient(name="fixture", command=sys.executable, args=[_server(tmp_path)])
    fresh.start()

    _, text = fresh.call_tool("leak", {})
    fresh.close()

    assert "AWS_SECRET_ACCESS_KEY" not in json.loads(text)


def test_a_server_can_be_given_exactly_what_it_needs(tmp_path):
    fresh = StdioClient(
        name="fixture",
        command=sys.executable,
        args=[_server(tmp_path)],
        env={"DATABASE_URL": "postgres://localhost/dev"},
    )
    fresh.start()

    _, text = fresh.call_tool("leak", {})
    fresh.close()

    assert "DATABASE_URL" in json.loads(text)


def test_inheriting_the_environment_is_possible_but_asked_for(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "x")
    fresh = StdioClient(
        name="fixture", command=sys.executable, args=[_server(tmp_path)], inherit_env=True
    )
    fresh.start()

    _, text = fresh.call_tool("leak", {})
    fresh.close()

    assert "SOME_TOKEN" in json.loads(text)


# --------------------------------------------------------------------------- #
# Presenting them as CoBirb tools
# --------------------------------------------------------------------------- #
def test_configured_servers_become_tools_named_for_where_they_came_from(tmp_path):
    tools, clients, issues = connect_servers(_server_config(tmp_path), str(tmp_path))

    assert issues == {}
    assert tool_name_for("fixture", "echo") in {tool.name() for tool in tools}
    for client in clients:
        client.close()


def test_a_wrapped_tool_carries_the_servers_own_schema_and_attribution(tmp_path):
    tools, clients, _ = connect_servers(_server_config(tmp_path), str(tmp_path))
    echo = next(tool for tool in tools if tool.name().endswith("echo"))

    assert echo.parameters()["properties"]["text"]["type"] == "string"
    assert "fixture" in echo.description()
    for client in clients:
        client.close()


def test_a_wrapped_tool_executes_like_any_other(tmp_path):
    tools, clients, _ = connect_servers(_server_config(tmp_path), str(tmp_path))
    echo = next(tool for tool in tools if tool.name().endswith("echo"))

    result = echo.execute({"text": "round trip"})

    assert result.ok
    assert result.content == "round trip"
    for client in clients:
        client.close()


def test_calling_a_tool_whose_server_has_died_fails_the_call_not_the_run(tmp_path):
    tools, clients, _ = connect_servers(_server_config(tmp_path), str(tmp_path))
    echo = next(tool for tool in tools if tool.name().endswith("echo"))
    for client in clients:
        client.close()

    result = echo.execute({"text": "anyone there?"})

    assert not result.ok
    assert "MCP" in result.content


def test_a_server_that_will_not_start_is_an_issue_and_not_an_exception(tmp_path):
    """The session is worth more than any one server — but a silent absence
    would present as the model inexplicably lacking a configured tool."""
    config = _user_config(
        tmp_path, {"mcp_servers": {"broken": {"command": str(tmp_path / "not-here")}}}
    )

    tools, clients, issues = connect_servers(config, str(tmp_path))

    assert tools == []
    assert "mcp:broken" in issues


def test_a_server_can_be_turned_off_without_deleting_its_configuration(tmp_path):
    tools, clients, issues = connect_servers(_server_config(tmp_path, enabled=False), str(tmp_path))

    assert (tools, clients, issues) == ([], [], {})


def test_no_configuration_starts_no_processes(tmp_path):
    assert connect_servers(Config(cwd=str(tmp_path)), str(tmp_path)) == ([], [], {})


def test_a_repository_cannot_configure_an_mcp_server(tmp_path):
    """A server entry is a command line. Honouring one from a cloned directory
    would make cloning it sufficient to run its author's code."""
    (tmp_path / "cobirb.json").write_text(
        json.dumps({"mcp_servers": {"evil": {"command": sys.executable, "args": ["-c", "pass"]}}})
    )

    assert connect_servers(Config(cwd=str(tmp_path)), str(tmp_path)) == ([], [], {})


def test_an_mcp_tool_is_not_pre_approved_by_a_read_grant(tmp_path):
    """CoBirb cannot know whether a remote tool reads, writes or bills someone,
    so the directory-scoped read grant must not generalise to it."""
    policy = build_default_policy(cwd=str(tmp_path))
    policy.allow_read_dir(str(tmp_path))

    assert not policy.is_allowed(tool_name_for("fixture", "echo"), {"path": str(tmp_path)})
