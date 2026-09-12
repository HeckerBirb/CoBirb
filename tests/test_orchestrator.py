"""Tests for the orchestrator and the agent loop."""
from __future__ import annotations

import json
import os

from cobirb.orchestrator import Orchestrator, _materialize, build_default_policy
from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto
from cobirb.plugins.core.tools import ToolRegistry
from cobirb.policy import Policy
from cobirb.session import SessionManager
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


def test_default_policy_permits_nothing():
    """The starting policy grants no capability at all: every tool has to be
    approved by the user or named in their own config/--allow-tool."""
    policy = build_default_policy()
    assert isinstance(policy, Policy)
    for name in ("read_file", "write_file", "edit_file", "apply_patch", "glob", "grep", "list_dir"):
        assert not policy.is_allowed(name, {"path": "x", "pattern": "x"})
    assert not policy.is_allowed("shell", {"command": "git status"})


def test_build_default_policy_audit_log_is_off_unless_requested(tmp_path, monkeypatch):
    """Regression test: an always-on audit log would duplicate file
    contents/diffs/shell commands into an unencrypted trail, at odds with
    sessions being encrypted at rest — it must stay opt-in end to end,
    including through this factory."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    policy = build_default_policy()
    policy.log("write_file", {"path": "x", "content": "secret"}, cwd=str(tmp_path))
    assert not policy.audit.enabled
    assert not os.path.exists(policy.audit.path)


def test_build_default_policy_audit_log_can_be_turned_on(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    policy = build_default_policy(audit_log_enabled=True)
    policy.log("read_file", {"path": "x"}, cwd=str(tmp_path))
    assert policy.audit.enabled
    assert os.path.exists(policy.audit.path)


def test_default_policy_accepts_user_supplied_rules():
    """Nothing is pre-approved, but the user's own rules are honoured — this
    is the escape hatch that makes a deny-everything default workable."""
    policy = build_default_policy()
    policy.allow("shell", "python -m pytest")
    assert policy.is_allowed("shell", {"command": "python -m pytest tests/"})
    assert not policy.is_allowed("shell", {"command": "python -c 'print(1)'"})


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
    # not the whole answer written in one go), and the fully assembled
    # visible text is exactly what was said — without pinning the exact chunk
    # boundaries or trailing-newline mechanics, which are incidental to
    # *that* streaming happened correctly.
    #
    # Nothing but the model's own words goes through render(): the persona
    # label used to be written into the stream here, which made it part of
    # the text every renderer received. How a reply is introduced is the I/O
    # adapter's business — see the begin_stream hook below.
    assert len(io.rendered) > 1
    assert "".join(io.rendered) == "Hello there\n"
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
    # "always" on a read grants the file's whole directory, so a later read
    # of a sibling — or of something in a subdirectory — skips the prompt.
    (tmp_path / "sub").mkdir()
    assert policy.is_allowed("read_file", {"path": str(tmp_path / "b.txt")})
    assert policy.is_allowed("read_file", {"path": str(tmp_path / "sub" / "c.txt")})
    # ...but it grants reading only. Writing there still has to be asked.
    assert not policy.is_allowed("write_file", {"path": str(tmp_path / "b.txt")})


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


def test_tool_that_raises_is_reported_to_the_model_not_fatal(tmp_path):
    """Regression: a tool raising used to propagate out of run() and tear
    down the whole session. Models routinely emit a mistyped argument name
    (``{"file": ...}`` instead of ``{"path": ...}``), which tools surface as
    a KeyError — that has to come back as a failed tool result the model can
    correct, not end the run."""
    registry = ToolRegistry(str(tmp_path))
    policy = Policy()
    policy.allow("read_file")
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"file": "wrong-argument-name"}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )

    session = orchestrator.run("read it", "sys", cwd=str(tmp_path))

    tool_turn = _tool_turn(session)
    assert "failed" in tool_turn.content
    assert "KeyError" in tool_turn.content
    # The run still reached a normal final answer afterwards.
    assert session.turns[-1].role == "assistant"
    assert not session.summary.startswith("Stopped after")


# --------------------------------------------------------------------------- #
# Phase D: a spinner around the wait for the model, and richer tool-call
# chrome — both duck-typed hooks on ``io`` (``spinner``/``render_tool_call``)
# so an adapter without them (like the plain ``_RecordingIO`` above) is
# never required to implement chrome it can't use.
# --------------------------------------------------------------------------- #
class _SpinningIO(_RecordingIO):
    """A _RecordingIO that also records spinner usage."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spinner_calls = []

    class _Spin:
        def __init__(self, calls, label):
            self._calls = calls
            self._label = label

        def __enter__(self):
            self._calls.append(("enter", self._label))
            return self

        def __exit__(self, *exc_info):
            self._calls.append(("exit", self._label))
            return False

    def spinner(self, label):
        return self._Spin(self.spinner_calls, label)


def test_chat_wraps_a_non_streaming_call_in_the_spinner_when_io_has_one():
    io = _SpinningIO()
    orchestrator = Orchestrator(model=_DummyModel(reply="hi"), tools={}, policy=Policy(), io=io)

    orchestrator.run("hello", "sys", cwd="/tmp", persona="noah")

    assert io.spinner_calls == [("enter", "noah is thinking…"), ("exit", "noah is thinking…")]


def test_chat_wraps_the_first_streamed_chunk_in_the_spinner():
    io = _SpinningIO()
    orchestrator = Orchestrator(model=_StreamingModel(["Hel", "lo"]), tools={}, policy=Policy(), io=io)

    orchestrator.run("hello", "sys", cwd="/tmp", persona="noah")

    assert io.spinner_calls == [("enter", "noah is thinking…"), ("exit", "noah is thinking…")]
    # The spinner must not have swallowed any streamed content.
    assert "".join(io.rendered) == "Hello\n"


def test_chat_works_without_a_spinner_hook():
    """An io without a spinner attribute (like the plain _RecordingIO used
    throughout this file) must not be treated as broken — just no spinner."""
    io = _RecordingIO()
    orchestrator = Orchestrator(model=_DummyModel(reply="hi"), tools={}, policy=Policy(), io=io)

    session = orchestrator.run("hello", "sys", cwd="/tmp")

    assert session.turns[-1].content == "hi"


class _ToolRenderingIO(_RecordingIO):
    """A _RecordingIO that also records render_tool_call invocations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_calls = []

    def render_tool_call(self, tool_name, arguments, result):
        self.tool_calls.append((tool_name, arguments, result))


def test_successful_tool_call_is_rendered_via_the_render_tool_call_hook(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    io = _ToolRenderingIO()
    policy = Policy()
    policy.allow("read_file")
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=io,
    )

    orchestrator.run("read file", "sys", cwd=str(tmp_path))

    assert len(io.tool_calls) == 1
    tool_name, arguments, result = io.tool_calls[0]
    assert tool_name == "read_file"
    assert arguments == {"path": str(tmp_path / "a.txt")}
    assert result.ok is True
    assert result.content == "hello"


def test_denied_tool_call_is_still_rendered():
    io = _ToolRenderingIO(confirm_decision="deny")
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": "x"}), tools={}, policy=Policy(), io=io
    )

    orchestrator.run("read file", "sys", cwd="/tmp")

    assert len(io.tool_calls) == 1
    tool_name, _, result = io.tool_calls[0]
    assert tool_name == "read_file"
    assert result.ok is False
    assert "Permission denied" in result.content


def test_tool_call_falls_back_to_plain_render_without_the_hook(tmp_path):
    """An io with plain render() but no render_tool_call must still show
    something for a tool call, via the generic render() fallback."""
    (tmp_path / "a.txt").write_text("hello")
    io = _RecordingIO()
    policy = Policy()
    policy.allow("read_file")
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}),
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=io,
    )

    orchestrator.run("read file", "sys", cwd=str(tmp_path))

    assert any("read_file" in text and "hello" in text for text in io.rendered)


def test_no_tool_call_rendering_without_an_io_adapter():
    """No io attached: nothing to render to, and nothing must crash."""
    policy = Policy()
    policy.allow("read_file")
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": "x"}), tools={}, policy=policy
    )

    session = orchestrator.run("read file", "sys", cwd="/tmp")

    assert session.turns[-1].role == "assistant"


# --------------------------------------------------------------------------- #
# Plan mode: an explicit plan → act → validate run, opt-in via
# run(plan_mode=True) (wired up from config/--plan-mode//plan in cli.py).
# Off by default — every test above already covers that the default
# (plan_mode=False) behavior is byte-for-byte unchanged.
# --------------------------------------------------------------------------- #
class _RecordingToolCountModel:
    """Cycles through canned replies per call, recording how many tools it
    was offered each time — lets tests assert the planning phase genuinely
    got none while the act/validate phases got the full set."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.tool_counts: list[int | None] = []

    def name(self):
        return "plan-mode-stub"

    def chat(self, system, context, tools=None, *, stream=False):
        self.tool_counts.append(None if tools is None else len(tools))
        return self._replies.pop(0)

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def test_plan_mode_off_by_default_leaves_turns_untagged_and_no_validation():
    orchestrator = Orchestrator(model=_DummyModel(reply="hi"), tools={}, policy=Policy())

    session = orchestrator.run("hello", "sys", cwd="/tmp")

    assert session.turns[-1].phase is None
    assert session.validation is None


def test_plan_mode_runs_plan_then_act_then_validate_as_separate_phases(tmp_path):
    registry = ToolRegistry(str(tmp_path))
    model = _RecordingToolCountModel(["1. Read the file. 2. Report back.", "Done reading.", "Confirmed: read it."])
    orchestrator = Orchestrator(
        model=model, tools={t.name: t for t in registry.values()}, policy=Policy()
    )

    session = orchestrator.run("read the file", "sys", cwd=str(tmp_path), persona="noah", plan_mode=True)

    # The planning call got no tools at all; the act/validate calls got the
    # full registered set.
    assert model.tool_counts[0] == 0
    assert model.tool_counts[1] > 0
    assert model.tool_counts[2] > 0

    assistant_turns = [t for t in session.turns if t.role == "assistant"]
    assert [t.phase for t in assistant_turns] == ["plan", "act", "validate"]
    assert "Read the file" in assistant_turns[0].content
    assert session.summary == "Done reading."
    assert session.validation == "Confirmed: read it."


def test_plan_mode_still_executes_tool_calls_during_the_act_phase(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    policy = Policy()
    policy.allow("read_file")
    registry = ToolRegistry(str(tmp_path))
    orchestrator = Orchestrator(
        model=_ToolCallModel("read_file", {"path": str(tmp_path / "a.txt")}, reply="done"),
        tools={t.name: t for t in registry.values()},
        policy=policy,
    )

    session = orchestrator.run("read file", "sys", cwd=str(tmp_path), plan_mode=True)

    tool_turn = _tool_turn(session)
    assert tool_turn.content == "hello"
    assert tool_turn.phase == "act"


class _RecordingPhaseIO(_RecordingIO):
    """A _RecordingIO that also records render_plan/render_answer/
    render_validation calls, both individually and (via ``events``) in the
    order they actually happened across all three."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plans = []
        self.answers = []
        self.validations = []
        self.events: list[tuple[str, str, str]] = []

    def render_plan(self, persona_name, text):
        self.plans.append((persona_name, text))
        self.events.append(("plan", persona_name, text))

    def render_answer(self, persona_name, text):
        self.answers.append((persona_name, text))
        self.events.append(("answer", persona_name, text))

    def render_validation(self, persona_name, text):
        self.validations.append((persona_name, text))
        self.events.append(("validation", persona_name, text))


def test_plan_mode_renders_the_plan_and_validation_via_the_io_hooks(tmp_path):
    io = _RecordingPhaseIO()
    model = _RecordingToolCountModel(["The plan.", "Acted.", "Validated."])
    orchestrator = Orchestrator(model=model, tools={}, policy=Policy(), io=io)

    orchestrator.run("do it", "sys", cwd=str(tmp_path), persona="noah", plan_mode=True)

    assert io.plans == [("noah", "The plan.")]
    assert io.validations == [("noah", "Validated.")]


def test_plan_mode_falls_back_to_plain_render_without_the_hooks(tmp_path):
    io = _RecordingIO()
    model = _RecordingToolCountModel(["The plan.", "Acted.", "Validated."])
    orchestrator = Orchestrator(model=model, tools={}, policy=Policy(), io=io)

    orchestrator.run("do it", "sys", cwd=str(tmp_path), persona="noah", plan_mode=True)

    assert any("plan" in text and "The plan." in text for text in io.rendered)
    assert any("noah" in text and "Acted." in text for text in io.rendered)
    assert any("validation" in text and "Validated." in text for text in io.rendered)


def test_plan_mode_renders_the_act_answer_before_validation(tmp_path):
    """Regression test: the validate phase's panel used to appear before
    the answer it was validating, because the act phase's final answer was
    only ever printed by the CLI after run() fully returned — by which
    point the validate phase (rendered live, from inside run()) had
    already shown its own panel. The orchestrator must render the act
    answer itself, in order, before running validate. See cli.py's
    end-to-end test of the same regression against a real TerminalIO."""
    io = _RecordingPhaseIO()
    model = _RecordingToolCountModel(["The plan.", "The answer.", "The validation."])
    orchestrator = Orchestrator(model=model, tools={}, policy=Policy(), io=io)

    orchestrator.run("do it", "sys", cwd=str(tmp_path), persona="noah", plan_mode=True)

    assert [kind for kind, _, _ in io.events] == ["plan", "answer", "validation"]
    assert io.answers == [("noah", "The answer.")]
    # The caller (e.g. the CLI) must not print the answer a second time.
    assert orchestrator.last_turn_streamed is True


def test_plan_mode_session_round_trips_through_real_encrypted_storage(tmp_path):
    """Integration: a real encrypted SessionManager (not just Session.to_dict
    /from_dict in isolation — see test_session.py) must actually preserve
    phase-tagged turns and the validation report across a save/reload
    cycle, the two pieces plan mode adds to the session schema. Mirrors
    exactly how cli._build_orchestrator wires a session in: build the
    crypto, then SessionManager.create, then hand it to the Orchestrator."""
    session_path = str(tmp_path / "session.json")
    crypto = AesGcmScryptSessionCrypto()
    manager = SessionManager.create(session_path, crypto, str(tmp_path), "noah", "pw")

    model = _RecordingToolCountModel(["1. Do X.", "Did X.", "Confirmed: X was done."])
    orchestrator = Orchestrator(model=model, tools={}, policy=Policy(), session=manager)

    orchestrator.run("do X", "sys", cwd=str(tmp_path), persona="noah", plan_mode=True)
    manager.save("pw")

    reloaded = SessionManager.load(session_path, AesGcmScryptSessionCrypto(), "pw", str(tmp_path), "noah")
    phases = [t.phase for t in reloaded.session.turns if t.role == "assistant"]
    assert phases == ["plan", "act", "validate"]
    assert reloaded.session.validation == "Confirmed: X was done."
    assert reloaded.session.summary == "Did X."


class _AllStreamingModel:
    """Streams a distinct single chunk on each successive call — supports
    streaming (and, vacuously, tool calling with no calls ever emitted),
    unlike _RecordingToolCountModel above."""

    def __init__(self, chunks_per_call):
        self._calls = list(chunks_per_call)

    def name(self):
        return "stub"

    def chat(self, system, context, tools=None, *, stream=False):
        chunks = self._calls.pop(0)
        return iter(chunks) if stream else "".join(chunks)

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return True


def test_plan_mode_does_not_double_render_a_streamed_phase():
    """If a phase's reply already streamed live (model+io both support
    streaming — a model-wide capability, so every phase streams together),
    _render_phase must not also box it up afterward — that would show the
    same content twice, same reasoning as last_turn_streamed for the act
    phase's own final answer."""
    io = _RecordingPhaseIO()
    model = _AllStreamingModel([["plan text"], ["act text"], ["validate text"]])
    orchestrator = Orchestrator(model=model, tools={}, policy=Policy(), io=io)

    orchestrator.run("do it", "sys", cwd="/tmp", persona="noah", plan_mode=True)

    assert io.plans == []
    assert io.validations == []
    joined = "".join(io.rendered)
    assert "plan text" in joined
    assert "act text" in joined
    assert "validate text" in joined


# --------------------------------------------------------------------------- #
# begin_stream: how a streamed reply is introduced.
#
# The orchestrator used to write f"{persona}: " straight into the stream via
# io.render(), which made the persona label part of the text every renderer
# received. Once replies were marked with "> ", transcripts read
# "> CoBirb: hello" — the label had been baked into the content and no
# renderer could tell it apart from what the model actually said.
# --------------------------------------------------------------------------- #
class _StreamAwareIO(_RecordingIO):
    def __init__(self):
        super().__init__()
        self.begin_calls = []

    def begin_stream(self, persona_name):
        self.begin_calls.append(persona_name)


def test_streaming_announces_the_reply_through_the_hook_not_the_content():
    io = _StreamAwareIO()
    orchestrator = Orchestrator(
        model=_StreamingModel(["Hel", "lo"]), tools={}, policy=Policy(), io=io
    )

    orchestrator.run("hi", "sys", cwd="/tmp", persona="noah")

    assert io.begin_calls == ["noah"]
    assert "noah" not in "".join(io.rendered)


def test_the_hook_fires_once_per_reply_not_once_per_chunk():
    io = _StreamAwareIO()
    orchestrator = Orchestrator(
        model=_StreamingModel(["a", "b", "c", "d"]), tools={}, policy=Policy(), io=io
    )

    orchestrator.run("hi", "sys", cwd="/tmp", persona="noah")

    assert io.begin_calls == ["noah"]


def test_the_hook_never_fires_for_a_reply_with_no_content():
    io = _StreamAwareIO()
    orchestrator = Orchestrator(model=_StreamingModel([]), tools={}, policy=Policy(), io=io)

    orchestrator.run("hi", "sys", cwd="/tmp", persona="noah")

    assert io.begin_calls == []


def test_streaming_still_works_for_an_adapter_without_the_hook():
    """Duck-typed like every other chrome hook: an adapter that doesn't
    implement it still gets the content."""
    io = _RecordingIO()  # no begin_stream at all
    orchestrator = Orchestrator(
        model=_StreamingModel(["Hel", "lo"]), tools={}, policy=Policy(), io=io
    )

    session = orchestrator.run("hi", "sys", cwd="/tmp", persona="noah")

    assert "".join(io.rendered) == "Hello\n"
    assert session.summary == "Hello"


def test_the_persona_label_still_reaches_the_spinner():
    """It was only ever wrong in the *content*; "noah is thinking…" is a
    status line and stays."""
    io = _SpinningIO()
    orchestrator = Orchestrator(model=_StreamingModel(["Hi"]), tools={}, policy=Policy(), io=io)

    orchestrator.run("hi", "sys", cwd="/tmp", persona="noah")

    assert io.spinner_calls[0] == ("enter", "noah is thinking…")
