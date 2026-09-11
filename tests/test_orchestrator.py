"""Tests for the orchestrator and the agent loop."""
from __future__ import annotations

import json

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
    assert _materialize("abc") == "abc"


def test_materialize_iterable():
    assert _materialize(iter("abc")) == "abc"


def test_run_returns_session_with_turns():
    policy = Policy()
    orchestrator = Orchestrator(
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
    # 1 user turn + 3 x (assistant turn announcing the call, tool turn with
    # the denied result) = 1 + 3*2 = 7. The assistant turn is recorded so a
    # provider can see the model's own tool-call decision, not just its
    # result — see the orchestrator.run() docstring.
    assert len(session.turns) == 7
    roles = [t.role for t in session.turns]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool"]
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


def test_tool_call_and_result_both_recorded_with_matching_tool_use(tmp_path):
    """Regression test for the tool-calling loop that never converged: the
    model's *decision* to call a tool must be recorded as its own assistant
    turn (with tool_use), immediately before the tool's result turn — not
    just the result appearing with nothing announcing it."""
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

    roles = [t.role for t in session.turns]
    assert roles == ["user", "assistant", "tool", "assistant"]
    call_turn, tool_turn = session.turns[1], session.turns[2]
    expected_tool_use = [{"name": "read_file", "arguments": {"path": str(tmp_path / "a.txt")}}]
    assert call_turn.tool_use == expected_tool_use
    assert tool_turn.tool_use == expected_tool_use


def test_context_passed_to_model_encodes_full_turn_structure(tmp_path):
    """The context string handed to the model on the turn *after* a tool
    call must be JSON that a provider can turn into a proper multi-turn
    messages array (see LocalModelProvider._build_messages) — not a
    flattened blob where a tool result appears with no assistant turn
    announcing it."""
    (tmp_path / "a.txt").write_text("hello")
    policy = Policy()
    policy.allow("read_file")
    registry = ToolRegistry(str(tmp_path))

    captured_contexts = []

    class _CapturingToolCallModel(_ToolCallModel):
        def chat(self, system, context, tools=None, *, stream=False):
            captured_contexts.append(context)
            return super().chat(system, context, tools, stream=stream)

    orchestrator = Orchestrator(
        model=_CapturingToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )
    orchestrator.run("read file", "sys", cwd=str(tmp_path))

    assert len(captured_contexts) == 2
    turns = json.loads(captured_contexts[1])
    assert [t["role"] for t in turns] == ["user", "assistant", "tool"]
    expected_tool_use = [{"name": "read_file", "arguments": {"path": str(tmp_path / "a.txt")}}]
    assert turns[1]["tool_use"] == expected_tool_use
    assert turns[2]["tool_use"] == expected_tool_use


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


class _StreamingModel(_DummyModel):
    """A stub model that streams its reply as separate chunks."""

    def __init__(self, chunks):
        super().__init__(reply="".join(chunks))
        self._chunks = chunks

    def chat(self, system, context, tools=None, *, stream=False):
        if stream:
            return iter(self._chunks)
        return self.reply

    def supports_streaming(self):
        return True


class _RecordingIO:
    def __init__(self, confirm_decision="deny"):
        self.rendered = []
        self.confirm_calls = []
        self._confirm_decision = confirm_decision

    def render(self, text):
        self.rendered.append(text)

    def confirm(self, tool_name, arguments):
        self.confirm_calls.append((tool_name, arguments))
        return self._confirm_decision


def test_run_streams_live_through_io_when_supported():
    io = _RecordingIO()
    orchestrator = Orchestrator(
        model=_StreamingModel(["Hel", "lo", " there"]),
        tools={},
        policy=Policy(),
        io=io,
    )
    session = orchestrator.run("hi", "sys", cwd="/tmp", persona="noah")

    # Rendered as more than one call (proves genuine incremental streaming,
    # not the whole answer written in one go), labeled once up front, and
    # the fully assembled visible text is exactly what was said — without
    # pinning the exact chunk boundaries or trailing-newline mechanics,
    # which are incidental to *that* streaming happened correctly.
    assert len(io.rendered) > 1
    assert io.rendered[0] == "noah: "
    assert "".join(io.rendered) == "noah: Hello there\n"
    # The final turn still gets the fully assembled content.
    assert session.turns[-1].content == "Hello there"
    assert session.summary == "Hello there"


def test_run_does_not_stream_when_io_is_missing():
    """No io adapter attached: falls back to a single non-streaming call,
    even though the model advertises streaming support."""
    orchestrator = Orchestrator(
        model=_StreamingModel(["Hel", "lo"]),
        tools={},
        policy=Policy(),
    )
    session = orchestrator.run("hi", "sys", cwd="/tmp")
    assert session.turns[-1].content == "Hello"


def test_run_does_not_stream_for_duck_typed_model_without_supports_streaming():
    """A model double that doesn't implement supports_streaming() at all
    (like the plain _DummyModel stubs elsewhere in this suite) must not
    crash the orchestrator with an AttributeError."""
    io = _RecordingIO()
    orchestrator = Orchestrator(
        model=_DummyModel(reply="plain reply"),
        tools={},
        policy=Policy(),
        io=io,
    )
    session = orchestrator.run("hi", "sys", cwd="/tmp")
    assert session.turns[-1].content == "plain reply"
    assert io.rendered == []


def test_last_turn_streamed_flag_true_when_final_answer_was_streamed():
    """A caller (the CLI) needs to know whether the final answer was already
    shown live, so it doesn't print session.summary a second time."""
    io = _RecordingIO()
    orchestrator = Orchestrator(
        model=_StreamingModel(["Hello"]),
        tools={},
        policy=Policy(),
        io=io,
    )
    orchestrator.run("hi", "sys", cwd="/tmp")
    assert orchestrator.last_turn_streamed is True


def test_last_turn_streamed_flag_false_without_io():
    orchestrator = Orchestrator(
        model=_StreamingModel(["Hello"]),
        tools={},
        policy=Policy(),
    )
    orchestrator.run("hi", "sys", cwd="/tmp")
    assert orchestrator.last_turn_streamed is False


def test_last_turn_streamed_flag_false_when_max_turns_exhausted():
    """The synthetic "stopped after N turns" message is never streamed (it's
    set directly, not via a model reply), so the flag must be false even
    though earlier tool-calling turns in the same run may have streamed."""
    io = _RecordingIO()

    class _AlwaysToolCallingStreamingModel(_StreamingModel):
        def supports_tool_calling(self):
            return True

        def parse_tool_calls(self, reply):
            return [ToolCall(name="nonexistent_tool", arguments={})]

    orchestrator = Orchestrator(
        model=_AlwaysToolCallingStreamingModel([""]),
        tools={},
        policy=Policy(),
        io=io,
    )
    session = orchestrator.run("loop", "sys", cwd="/tmp", max_turns=2)
    assert session.summary == "Stopped after 2 turns without a final answer."
    assert orchestrator.last_turn_streamed is False


def _tool_turn(session):
    """The (first) "tool" role turn in a session — approval-outcome tests
    care about what the tool call *resulted in*, not the exact position/
    count of surrounding turns, so they look this up rather than indexing."""
    return next(t for t in session.turns if t.role == "tool")


# --------------------------------------------------------------------------- #
# Interactive permission approval (Phase B): an unpermitted tool call is
# asked about via io.confirm() instead of just being silently denied.
# --------------------------------------------------------------------------- #
def test_unpermitted_tool_can_be_approved_once(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    io = _RecordingIO(confirm_decision="once")
    policy = Policy()  # nothing pre-allowed
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=io,
    )
    session = orchestrator.run("read file", "sys", cwd=str(tmp_path))

    assert io.confirm_calls == [("read_file", {"path": str(tmp_path / "a.txt")})]
    assert _tool_turn(session).content == "hello"
    # "once" must not update the policy for future calls.
    assert not policy.is_allowed("read_file", {"path": str(tmp_path / "a.txt")})


def test_unpermitted_tool_approved_always_updates_policy(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    io = _RecordingIO(confirm_decision="always")
    policy = Policy()
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=io,
    )
    session = orchestrator.run("read file", "sys", cwd=str(tmp_path))

    assert _tool_turn(session).content == "hello"
    # "always" must update the policy so a later call skips the prompt.
    assert policy.is_allowed("read_file")


def test_unpermitted_shell_approved_always_narrows_to_exact_command(tmp_path):
    """Approving "always" for a shell call must narrow to that exact
    invocation (like policy.allow("shell", command) already does), not
    blanket-trust the bare binary — same reasoning as the default policy's
    python/pytest narrowing."""
    io = _RecordingIO(confirm_decision="always")
    policy = Policy()
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("shell", {"command": "python -m pytest"}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=io,
    )
    orchestrator.run("run tests", "sys", cwd=str(tmp_path))

    assert policy.is_allowed("shell", {"command": "python -m pytest"})
    assert not policy.is_allowed("shell", {"command": "python -c 'evil'"})


def test_unpermitted_tool_denied_via_prompt(tmp_path):
    io = _RecordingIO(confirm_decision="deny")
    policy = Policy()
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=io,
    )
    session = orchestrator.run("read file", "sys", cwd=str(tmp_path))

    assert io.confirm_calls  # the user was actually asked
    assert "Permission denied" in _tool_turn(session).content


def test_no_io_denies_without_prompting():
    """No adapter attached -> fail closed immediately; there's no one to ask."""
    policy = Policy()
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": "x"}),
        tools={},
        policy=policy,
    )
    session = orchestrator.run("read file", "sys", cwd="/tmp")
    assert "Permission denied" in _tool_turn(session).content


def test_broken_confirm_denies_rather_than_crashing(tmp_path):
    class _BrokenIO(_RecordingIO):
        def confirm(self, tool_name, arguments):
            raise RuntimeError("adapter exploded")

    policy = Policy()
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=_BrokenIO(),
    )
    session = orchestrator.run("read file", "sys", cwd=str(tmp_path))
    assert "Permission denied" in _tool_turn(session).content
