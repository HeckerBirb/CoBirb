"""Tests for the orchestrator and the agent loop."""
from __future__ import annotations

import pytest

from cobirb.orchestrator import Orchestrator, _materialize, build_default_policy
from cobirb.plugins.core.tools import ShellTool, ToolRegistry
from cobirb.policy import Policy
from cobirb.typing.spi import ToolCall


class _DummyModel:
    """A stub model that returns a fixed reply and parses no tool calls."""

    def __init__(self, reply="hello world"):
        self.reply = reply

    def chat(self, *args, **kwargs):
        return self.reply

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return False


class _ToolCallModel(_DummyModel):
    """A stub model that emits a single tool call, then a final reply."""

    def __init__(self, tool_name, arguments, reply="done"):
        super().__init__(reply)
        self._tool_call = ToolCall(name=tool_name, arguments=arguments)
        self._called = False

    def supports_tool_calling(self):
        return True

    def parse_tool_calls(self, reply):
        if self._called:
            return []
        self._called = True
        return [self._tool_call]


def test_materialize_string():
    from cobirb.orchestrator import _materialize

    assert _materialize("abc") == "abc"


def test_materialize_iterable():
    from cobirb.orchestrator import _materialize

    assert _materialize(iter("abc")) == "abc"


def test_run_returns_session_with_turns():
    policy = Policy()
    orchestrator = __import__("cobirb.orchestrator", fromlist=["Orchestrator"]).Orchestrator(
        model=_DummyModel(),
        tools={},
        policy=policy,
    )
    session = orchestrator.run("do something", "system prompt", cwd="/tmp")
    assert session.turns[0].role == "user"
    assert session.turns[-1].role == "assistant"


def test_run_returns_immediately_on_first_plain_reply():
    """A model that never calls tools should stop after one assistant turn,
    not burn through the whole max_turns budget."""
    orchestrator = Orchestrator(
        model=_DummyModel(reply="the final answer"),
        tools={},
        policy=Policy(),
    )
    session = orchestrator.run("do something", "sys", cwd="/tmp", max_turns=8)
    # 1 user turn + 1 assistant turn = 2, even though max_turns is much higher.
    assert len(session.turns) == 2
    assert session.summary == "the final answer"


def test_run_stops_after_max_turns_when_model_never_finishes():
    """A model that always calls tools (and never gives a plain answer) must
    not loop forever; max_turns is the safety cap."""
    from cobirb.orchestrator import Orchestrator

    class AlwaysCallingModel(_DummyModel):
        def supports_tool_calling(self):
            return True

        def parse_tool_calls(self, reply):
            return [ToolCall(name="nonexistent_tool", arguments={})]

    orchestrator = Orchestrator(
        model=AlwaysCallingModel(),
        tools={},
        policy=Policy(),
    )
    session = orchestrator.run("loop", "sys", cwd="/tmp", max_turns=3)
    # 1 user turn + 3 tool-dispatch turns (each denied: unknown tool) = 4.
    assert len(session.turns) == 4
    assert session.summary == "Stopped after 3 turns without a final answer."


def test_run_tool_dispatch(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    policy = Policy()
    policy.allow("read_file")
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )
    session = orchestrator.run("read file", "sys", cwd=str(tmp_path))
    assert any(turn.role == "tool" for turn in session.turns)


def test_run_tool_dispatch_denied(tmp_path):
    policy = Policy()
    policy.deny("shell")
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("shell", {"command": "rm file"}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )
    session = orchestrator.run("rm file", "sys", cwd=str(tmp_path))
    assert any(turn.role == "tool" for turn in session.turns)


def test_run_without_permission(tmp_path):
    policy = Policy()
    policy.deny("shell")
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("shell", {"command": "danger"}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )
    session = orchestrator.run("danger", "sys", cwd=str(tmp_path))
    assert any(turn.role == "tool" for turn in session.turns)


def test_policy_gates_tool_access():
    policy = Policy()
    registry = ToolRegistry(".")
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": "unsafe op"}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )
    session = orchestrator.run("unsafe op", "sys", cwd="/tmp")
    assert any(turn.role == "tool" for turn in session.turns)


def test_cli_exposes_default_policy():
    policy = build_default_policy()
    assert isinstance(policy, Policy)
    # Default policy permits the built-in core tools.
    assert policy.is_allowed("read_file")
    assert policy.is_allowed("shell", {"command": "git status"})


def test_default_policy_allows_shell_scope():
    policy = Policy()
    policy.allow_all_core_tools()
    assert policy.is_allowed("shell", {"command": "git status"})
    assert policy.is_allowed("shell", {"command": "python -m cobirb"})
    assert not policy.is_allowed("shell", {"command": "rm -rf /"})


def test_loader_registers_core_plugins():
    from cobirb.plugins.loader import load_plugins

    discovered, errors = load_plugins()
    assert discovered  # core plugins are present
