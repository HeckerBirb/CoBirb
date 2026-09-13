"""The v0.4.0 features, together, through the real CLI.

Each of these has its own focused tests. What none of those can catch is a
wiring mistake — a hook that is loaded but never fired, an MCP tool that is
registered but never reaches the model, a custom command expanded after the
prompt has already gone. Everything here runs ``cli.main()`` with real config,
a real ToolRegistry, a real Policy and a real MCP subprocess; only the model
is scripted, because the alternative is a network call.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from cobirb import cli
from cobirb.mcp import tool_name_for
from cobirb.runtime import wiring
from cobirb.typing.spi import ToolCall

_MCP_SERVER = '''
import json, sys

TOOLS = [{
    "name": "lookup",
    "description": "Looks a widget up by name.",
    "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
}]

def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        reply(msg["id"], {"protocolVersion": "2025-06-18", "capabilities": {},
                          "serverInfo": {"name": "widgets", "version": "1"}})
    elif method == "tools/list":
        reply(msg["id"], {"tools": TOOLS})
    elif method == "tools/call":
        wanted = msg["params"].get("arguments", {}).get("name", "")
        reply(msg["id"], {"content": [{"type": "text",
                                       "text": f"{wanted} is in aisle 7"}]})
'''


class _ScriptedModel:
    """Says one thing, calls one tool, then answers. Records the tool schemas
    it was offered, which is how a test can tell an MCP tool actually reached
    the model rather than merely being registered."""

    def __init__(self, call: ToolCall | None = None):
        self._call = call
        self._step = 0
        self._emitted = False
        self.offered: list[str] = []

    def name(self):
        return "scripted"

    def chat(self, system, context, tools=None, *, stream=False):
        self._step += 1
        self.offered = [tool.name() for tool in (tools or [])]
        return "Looking that up." if self._step == 1 else "Done."

    def parse_tool_calls(self, reply):
        if self._call is not None and not self._emitted:
            self._emitted = True
            return [self._call]
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def _user_config(tmp_path, data):
    """Write the *user's* config. COBIRB_HOME is redirected per-test by the
    autouse fixture in conftest, so this is genuinely the user layer."""
    home = tmp_path / ".cobirb"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(data))


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


# --------------------------------------------------------------------------- #
# MCP, all the way to the model and back
# --------------------------------------------------------------------------- #
def test_an_mcp_tool_reaches_the_model_and_its_answer_reaches_the_transcript(
    monkeypatch, tmp_path, capsys
):
    server = tmp_path / "widgets.py"
    server.write_text(textwrap.dedent(_MCP_SERVER))
    _user_config(
        tmp_path,
        {"mcp_servers": {"widgets": {"command": sys.executable, "args": [str(server)]}}},
    )
    name = tool_name_for("widgets", "lookup")
    model = _ScriptedModel(ToolCall(name=name, arguments={"name": "sprocket"}))
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)

    status = cli.main(
        ["-p", "where is the sprocket", "--cwd", str(_project(tmp_path)), f"--allow-tool={name}"]
    )

    assert status == 0
    output = capsys.readouterr().out
    assert name in model.offered  # the model was actually offered it
    assert "sprocket is in aisle 7" in output  # and the server's answer came back


def test_an_mcp_tool_is_refused_like_any_other_when_it_is_not_permitted(
    monkeypatch, tmp_path, capsys
):
    """MCP is a way of acquiring tools, not a second set of rules about what
    tools may do. Headless refuses anything not permitted up front."""
    server = tmp_path / "widgets.py"
    server.write_text(textwrap.dedent(_MCP_SERVER))
    _user_config(
        tmp_path,
        {"mcp_servers": {"widgets": {"command": sys.executable, "args": [str(server)]}}},
    )
    name = tool_name_for("widgets", "lookup")
    model = _ScriptedModel(ToolCall(name=name, arguments={"name": "sprocket"}))
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)

    status = cli.main(
        ["-p", "where is the sprocket", "--cwd", str(_project(tmp_path)),
         "--headless", "--output", "json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 2  # completed, but something was refused
    assert report["denied"] == [name]


def test_a_broken_mcp_server_is_reported_and_the_run_continues(monkeypatch, tmp_path, capsys):
    _user_config(tmp_path, {"mcp_servers": {"gone": {"command": str(tmp_path / "not-here")}}})
    model = _ScriptedModel()
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)

    status = cli.main(["-p", "hello", "--cwd", str(_project(tmp_path))])

    assert status == 0
    assert "mcp:gone" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Hooks, at the point where they matter
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell scripts")
def test_a_hook_refuses_a_write_the_permission_layer_had_already_allowed(
    monkeypatch, tmp_path, capsys
):
    """The interesting case: --allow-tool says yes, the hook says no, and no
    file is written. A hook that only ran on calls the policy already refused
    would be worth nothing."""
    guard = tmp_path / "guard.sh"
    guard.write_text("#!/bin/sh\necho 'generated/ is off limits'\nexit 1\n")
    guard.chmod(0o755)
    _user_config(tmp_path, {"hooks": {"before_tool": [{"match": "write_file",
                                                       "command": str(guard)}]}})
    project = _project(tmp_path)
    target = project / "generated" / "out.txt"
    model = _ScriptedModel(
        ToolCall(name="write_file", arguments={"path": str(target), "content": "x"})
    )
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)

    status = cli.main(
        ["-p", "write the file", "--cwd", str(project), "--allow-tool=write_file"]
    )

    assert status == 0
    assert not target.exists()
    assert "generated/ is off limits" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell scripts")
def test_a_hook_in_a_projects_config_is_ignored(monkeypatch, tmp_path):
    """Cloning a repository must not be enough to run its author's code."""
    project = _project(tmp_path)
    marker = tmp_path / "hook-ran"
    (project / "cobirb.json").write_text(
        json.dumps({"hooks": {"before_turn": [{"command": f"touch {marker}"}]}})
    )
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _ScriptedModel())

    cli.main(["-p", "hello", "--cwd", str(project)])

    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell scripts")
def test_a_turn_hook_runs_once_the_work_is_finished(monkeypatch, tmp_path):
    marker = tmp_path / "finished"
    _user_config(tmp_path, {"hooks": {"after_turn": [f"touch {marker}"]}})
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _ScriptedModel())

    cli.main(["-p", "hello", "--cwd", str(_project(tmp_path))])

    assert marker.exists()


# --------------------------------------------------------------------------- #
# Custom commands
# --------------------------------------------------------------------------- #
def test_a_custom_command_is_what_actually_reaches_the_model(monkeypatch, tmp_path):
    """Expanded before the prompt is sent, not after — a command expanded too
    late is a command that did nothing."""
    project = _project(tmp_path)
    directory = project / ".cobirb" / "commands"
    directory.mkdir(parents=True)
    (directory / "review.md").write_text("Review $ARGUMENTS against the house style.")
    sent = []

    class _Capturing(_ScriptedModel):
        def chat(self, system, context, tools=None, *, stream=False):
            sent.append(context)
            return super().chat(system, context, tools, stream=stream)

    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _Capturing())

    cli.main(["-p", "/review parser.py", "--cwd", str(project)])

    assert "Review parser.py against the house style." in sent[0]


def test_cobirb_commands_lists_what_is_available_here(tmp_path, capsys):
    project = _project(tmp_path)
    directory = project / ".cobirb" / "commands"
    directory.mkdir(parents=True)
    (directory / "release.md").write_text("---\ndescription: Cut a release\n---\nDo it.")

    assert cli.main(["commands", "--cwd", str(project)]) == 0
    assert "Cut a release" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Per-role models
# --------------------------------------------------------------------------- #
def test_cobirb_models_reports_how_each_role_resolves(tmp_path, capsys):
    project = _project(tmp_path)
    (project / "cobirb.json").write_text(
        json.dumps({"models": {"default": {"name": "big"}, "worker": {"name": "small"}}})
    )

    assert cli.main(["models", "--cwd", str(project)]) == 0

    output = capsys.readouterr().out
    assert "orchestrator" in output and "big" in output
    assert "small" in output
