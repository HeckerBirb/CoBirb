"""Tests for interactive mode's Textual app (``cobirb.tui``).

Driven through ``CoBirbApp.run_test()`` -> ``Pilot``, with
``cobirb.tui.app.cli._build_orchestrator`` monkeypatched to hand back a stub
orchestrator — the same style ``test_cli.py`` uses for the CLI's own wiring,
so no test here needs a real model.

**A caveat to keep in mind before "fixing" anything here:** a turn runs in a
real OS thread (``@work(thread=True)``), not an asyncio task, so it is *not*
deterministically synchronized with the test event loop. Anything that
observes mid-turn state (the approval modal appearing) or end-of-turn state
(the input re-enabling) has to poll — hence ``_until`` below — rather than
assume a single ``await pilot.pause()`` is enough. Replacing those polls with
a bare pause will pass locally and flake in CI.
"""
from __future__ import annotations

import os
import threading

import pytest
from textual.widgets import Footer, Input, OptionList, RichLog, TabbedContent
from textual.widgets.option_list import Option

from cobirb import cli, session
from cobirb.tui.app import CoBirbApp
from cobirb.tui.panes import PluginsPane, SessionsPane
from cobirb.tui.screens import ApprovalModal, HelpModal, ModelPickerModal, TextPromptModal
from cobirb.tui.widgets import StatusBar

# Captured at import time, before the autouse fixture below ever patches the
# class — tests that want the *real* startup/`/model` behavior restore this.
_REAL_SELECT_MODEL_WORKER = CoBirbApp._select_model_worker


@pytest.fixture(autouse=True)
def _skip_startup_model_check(monkeypatch):
    """``CoBirbApp`` validates its model against a live endpoint at startup
    and ``/model`` does the same on demand (``_select_model_worker``) — both
    hit a real network port, which no test here should do by accident. Every
    test gets this fast no-op by default; a test that exercises the model
    check itself re-patches it back to ``_REAL_SELECT_MODEL_WORKER`` (which
    wins, since it runs after this fixture's setup).
    """
    monkeypatch.setattr(CoBirbApp, "_select_model_worker", lambda self, **kwargs: None)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
async def _until(pilot, predicate, *, tries: int = 200):
    """Pause until ``predicate()`` is true, or fail.

    See the module docstring: the worker is a real thread, so "the turn has
    finished" is only ever observable by looking again.
    """
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError("condition never became true while polling the app")


class _StubSession:
    summary = "ok"
    validation = ""


class _StubSessionManager:
    def __init__(self):
        self.saved_with = []

    def save(self, password=None):
        self.saved_with.append(password)


class _StubOrchestrator:
    """Stands in for a real Orchestrator: records the run() it was given and
    can be told to raise, stream, or make a tool call along the way."""

    def __init__(self, *, run_raises=None, with_session_manager=False, on_run=None, io=None):
        self.session = _StubSessionManager() if with_session_manager else None
        self.io = io
        self.last_turn_streamed = False
        self.calls = []
        self._run_raises = run_raises
        self._on_run = on_run

    def run(self, prompt, system, *, cwd, persona, session_path=None, plan_mode=False):
        self.calls.append({"prompt": prompt, "persona": persona, "plan_mode": plan_mode})
        if self._run_raises is not None:
            raise self._run_raises
        if self._on_run is not None:
            self._on_run(self)
        return _StubSession()


def _stub_build(orchestrator=None, record=None):
    """A ``_build_orchestrator`` replacement that hands back ``orchestrator``.

    Mirrors the real signature (including ``io_factory``) so a test also
    proves the TUI passes its bridge in rather than letting the default
    ``TerminalIO`` print over the app.
    """

    def fake_build_orchestrator(
        cwd, persona, allow_overrides, system, session_path=None, password=None,
        model_name=None, io_factory=None,
    ):
        target = orchestrator if orchestrator is not None else _StubOrchestrator()
        if io_factory is not None and target.io is None:
            target.io = io_factory()
        if record is not None:
            record.append({"persona": persona.name, "system": system, "io_factory": io_factory})
        return target, None, None

    return fake_build_orchestrator


def _make_app(**overrides) -> CoBirbApp:
    persona = cli._load_persona(overrides.pop("persona_name", None))
    kwargs = dict(
        persona=persona,
        system=cli._build_system_prompt(persona),
        allow_overrides={},
        session_path=None,
        password=None,
        cwd="/tmp",
        model_name=None,
        plan_mode=False,
    )
    kwargs.update(overrides)
    return CoBirbApp(**kwargs)


def _static_text(app: CoBirbApp, selector: str) -> str:
    """The visible text of a ``Static`` on whichever screen is on top.

    Queried off ``app.screen`` rather than ``app``, so this also reaches
    inside a pushed modal — ``app.query`` only ever sees the base screen.
    """
    return " ".join(str(node.content) for node in app.screen.query(selector))


def _transcript_text(app: CoBirbApp) -> str:
    """Everything the transcript has rendered, as plain text.

    ``RichLog`` keeps its writes as already-rendered Rich ``Strip``s, so the
    readable content is reachable through each line's segments rather than
    off the widget as a string.
    """
    log = app.query_one("#transcript", RichLog)
    return "\n".join("".join(segment.text for segment in line) for line in log.lines)


async def _submit(pilot, app: CoBirbApp, text: str) -> None:
    prompt_input = app.query_one("#prompt-input", Input)
    prompt_input.value = text
    await pilot.press("enter")
    await pilot.pause()


# --------------------------------------------------------------------------- #
# Layout and startup
# --------------------------------------------------------------------------- #
async def test_app_boots_with_the_three_tabs_and_current_active():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        assert [pane.id for pane in app.query("TabPane")] == ["current", "sessions", "plugins"]
        assert tabs.active == "current"


async def test_status_bar_shows_persona_model_plan_mode_and_cwd():
    app = _make_app(cwd="/some/where", plan_mode=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        line = str(app.query_one(StatusBar).render())
        assert "Noah" in line
        assert "plan: on" in line
        assert "/some/where" in line


async def test_the_status_bar_has_a_row_of_its_own_above_the_footer():
    """Regression test: the status bar and the key-binding Footer were
    originally both docked to the bottom edge, which put them on the *same*
    row — the Footer painted over the status line, so it was never actually
    visible even though querying it returned perfectly good text. They now
    share one two-row docked container."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar).region
        footer = app.query_one(Footer).region

        assert status.height == 1
        assert not status.overlaps(footer)
        assert status.bottom <= footer.y  # and it sits above, not below


async def test_status_bar_shows_the_session_path_when_one_is_active():
    app = _make_app(session_path="/tmp/s.json")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "session: s.json" in str(app.query_one(StatusBar).render())


async def test_the_greeting_and_header_are_in_the_transcript_at_startup():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _transcript_text(app)
        assert "Noah" in text
        assert "/persona" in text  # the hint line


async def test_next_tab_cycles_through_every_tab_and_wraps():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        for expected in ("sessions", "plugins", "current"):
            app.action_next_tab()
            await pilot.pause()
            assert tabs.active == expected


async def test_the_sessions_and_plugins_tabs_hold_real_panes_not_placeholders():
    """Regression guard: both tabs used to carry a static "coming later"
    message; they're live views now (see PluginsPane/SessionsPane tests
    further down) and must never regress back to a placeholder."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(SessionsPane) is not None
        assert app.query_one(PluginsPane) is not None
        assert not app.query(".placeholder")


# --------------------------------------------------------------------------- #
# Running a turn
# --------------------------------------------------------------------------- #
async def test_submitting_a_prompt_disables_the_input_runs_the_turn_and_re_enables_it(monkeypatch):
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello there")

        prompt_input = app.query_one("#prompt-input", Input)
        await _until(pilot, lambda: not prompt_input.disabled)

        assert orchestrator.calls == [{"prompt": "hello there", "persona": "Noah", "plan_mode": False}]
        text = _transcript_text(app)
        assert "hello there" in text  # the echoed user line
        assert "ok" in text  # the stub session's summary, via render_answer


async def test_a_blank_submission_never_builds_an_orchestrator(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for a blank prompt")

    monkeypatch.setattr(cli, "_build_orchestrator", fail_if_called)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "   ")
        assert not app.query_one("#prompt-input", Input).disabled


async def test_the_orchestrator_is_built_once_and_reused_across_turns(monkeypatch):
    """Regression guard carried over from the scrolling loop: rebuilding per
    turn would discard the Policy, so an "always allow this" approval would
    silently stop applying on the next turn."""
    builds = []
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator, record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", Input)
        for prompt in ("first", "second"):
            await _submit(pilot, app, prompt)
            await _until(pilot, lambda: not prompt_input.disabled)

        assert len(builds) == 1
        assert [call["prompt"] for call in orchestrator.calls] == ["first", "second"]


async def test_the_tui_passes_its_own_io_bridge_into_the_orchestrator(monkeypatch):
    """Without this, the orchestrator would render through the default
    ``TerminalIO`` and print straight over the full-screen app."""
    builds = []
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert builds[0]["io_factory"]() is app.io_bridge


async def test_a_permission_error_is_reported_and_the_input_comes_back(monkeypatch):
    orchestrator = _StubOrchestrator(run_raises=cli.PermissionError("nope"))
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "do it")
        prompt_input = app.query_one("#prompt-input", Input)
        await _until(pilot, lambda: not prompt_input.disabled)

        assert "blocked" in _transcript_text(app)


async def test_an_unexpected_error_is_reported_and_the_input_comes_back(monkeypatch):
    orchestrator = _StubOrchestrator(run_raises=RuntimeError("model exploded"))
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "do it")
        prompt_input = app.query_one("#prompt-input", Input)
        await _until(pilot, lambda: not prompt_input.disabled)

        text = _transcript_text(app)
        assert "could not complete" in text
        assert "model exploded" in text


async def test_a_failure_while_building_the_orchestrator_still_re_enables_the_input(monkeypatch):
    """The input is disabled on the main thread before the worker starts, so
    anything that kills the worker — including a failure before run() is even
    reached — must still hand the box back."""

    def explode(*args, **kwargs):
        raise RuntimeError("config is broken")

    monkeypatch.setattr(cli, "_build_orchestrator", explode)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)


async def test_the_session_is_saved_after_a_successful_turn(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app(session_path="/tmp/s.json", password="pw")
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", Input)
        for prompt in ("first", "second"):
            await _submit(pilot, app, prompt)
            await _until(pilot, lambda: not prompt_input.disabled)

        assert orchestrator.session.saved_with == ["pw", "pw"]


async def test_nothing_is_saved_when_no_session_path_was_given(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert orchestrator.session.saved_with == []


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
async def test_persona_with_no_argument_lists_personas_without_building_anything(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for /persona")

    monkeypatch.setattr(cli, "_build_orchestrator", fail_if_called)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona")

        text = _transcript_text(app)
        assert "Available personas:" in text
        for name in ("noah", "professional", "neighbor", "kawaii"):
            assert name in text


async def test_persona_switch_updates_the_status_bar_and_later_turns(monkeypatch):
    builds = []
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona professional")

        assert app.query_one(StatusBar).persona_name == "Professional"
        assert not builds  # a slash command never reaches the model

        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        # The switch has to change the system prompt too, not just the label.
        assert builds[0]["persona"] == "Professional"
        assert "Professional" in builds[0]["system"]


async def test_plan_status_reports_without_building_an_orchestrator(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for /plan")

    monkeypatch.setattr(cli, "_build_orchestrator", fail_if_called)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/plan")
        assert "Plan mode is off" in _transcript_text(app)


async def test_plan_on_off_toggles_the_status_bar_and_is_confirmed():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)

        await _submit(pilot, app, "/plan on")
        assert status.plan_mode is True
        assert "plan: on" in str(status.render())

        await _submit(pilot, app, "/plan off")
        assert status.plan_mode is False

        text = _transcript_text(app)
        assert "Plan mode: on." in text
        assert "Plan mode: off." in text


async def test_plan_rejects_an_unrecognized_argument():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/plan sideways")
        assert "Usage: /plan on|off" in _transcript_text(app)
        assert app.query_one(StatusBar).plan_mode is False


async def test_plan_on_reaches_orchestrator_run(monkeypatch):
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/plan on")
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert orchestrator.calls[0]["plan_mode"] is True


async def test_the_app_starts_with_the_plan_mode_it_was_given():
    app = _make_app(plan_mode=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/plan")
        assert "Plan mode is on" in _transcript_text(app)


# --------------------------------------------------------------------------- #
# Help
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("typed", ["?", "/help"])
async def test_submitting_a_help_request_opens_the_help_modal(typed):
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, typed)

        assert isinstance(app.screen, HelpModal)
        assert "PRIVACY BY CONSTRUCTION" in _static_text(app, "#help-body")

        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, HelpModal))


async def test_help_with_a_topic_shows_that_topic():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/help plan")
        assert "PLAN MODE" in _static_text(app, "#help-body")


async def test_help_with_an_unknown_topic_says_so_and_falls_back():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/help nonsense")
        body = _static_text(app, "#help-body")
        assert "No help topic 'nonsense'" in body
        assert "MODES" in body  # the general help, still shown underneath


async def test_a_question_mark_inside_a_real_prompt_is_not_a_help_request(monkeypatch):
    """``?`` opens help only when it is the entire message — otherwise you
    could never ask CoBirb a question."""
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "what is a bird?")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert not isinstance(app.screen, HelpModal)
        assert orchestrator.calls[0]["prompt"] == "what is a bird?"


# --------------------------------------------------------------------------- #
# Tool approval: the full worker -> modal -> worker round trip
# --------------------------------------------------------------------------- #
def _confirming_orchestrator(decisions: list[str]):
    """A stub whose run() calls the real ``TuiIO.confirm()`` from the worker
    thread, exactly as ``Orchestrator._execute_tool`` would, and records what
    came back."""

    def on_run(orchestrator):
        decisions.append(orchestrator.io.confirm("shell", {"command": "git status"}))

    return _StubOrchestrator(on_run=on_run)


@pytest.mark.parametrize(
    "key,expected", [("y", "once"), ("a", "always"), ("n", "deny"), ("escape", "deny")]
)
async def test_the_approval_modal_returns_the_decision_to_the_worker(monkeypatch, key, expected):
    decisions: list[str] = []
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(_confirming_orchestrator(decisions)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run git status")

        await _until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        assert "git status" in _static_text(app, "#approval-body")

        await pilot.press(key)
        await _until(pilot, lambda: bool(decisions))
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert decisions == [expected]


@pytest.mark.parametrize(
    "button,expected",
    [("#approve-once", "once"), ("#approve-always", "always"), ("#approve-deny", "deny")],
)
async def test_the_approval_modal_buttons_resolve_the_same_way(monkeypatch, button, expected):
    decisions: list[str] = []
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(_confirming_orchestrator(decisions)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run git status")

        await _until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        await pilot.click(button)
        await _until(pilot, lambda: bool(decisions))
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert decisions == [expected]


async def test_confirm_fails_closed_when_it_cannot_ask(monkeypatch):
    """``confirm()`` is contracted to deny rather than hang or guess yes when
    there is no one to ask — here, called from the wrong thread entirely, so
    the modal can never be shown."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.io_bridge.confirm("shell", {"command": "rm -rf /"}) == "deny"


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
async def test_streamed_tokens_preview_live_and_land_in_the_transcript(monkeypatch):
    """A streamed reply is never re-rendered as a panel (the orchestrator
    sets ``last_turn_streamed`` so it isn't shown twice), so the preview's
    contents have to be committed to the transcript when the stream ends —
    otherwise the answer would disappear the moment the preview cleared."""
    seen_mid_stream: list[str] = []

    def on_run(orchestrator):
        orchestrator.io.render("Noah: ")
        orchestrator.io.render("hello ")
        seen_mid_stream.append(orchestrator.io._app.query_one("#streaming-preview").buffered)
        orchestrator.io.render("world")
        orchestrator.last_turn_streamed = True

    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(_StubOrchestrator(on_run=on_run)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "say hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert seen_mid_stream == ["Noah: hello "]
        assert "hello world" in _transcript_text(app)
        # Flushed, not left behind to be shown twice.
        assert app.query_one("#streaming-preview").buffered == ""


async def test_a_panel_flushes_the_stream_first_so_ordering_is_preserved(monkeypatch):
    """Streamed reasoning must appear above the tool-call panel it led to,
    not after it."""

    def on_run(orchestrator):
        orchestrator.io.render("Noah: let me check.")
        orchestrator.io.render_tool_call("read_file", {"path": "note.txt"}, _Result("banana"))

    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(_StubOrchestrator(on_run=on_run)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "read the note")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        text = _transcript_text(app)
        assert text.index("let me check.") < text.index("banana")


class _Result:
    def __init__(self, content, ok=True):
        self.content = content
        self.ok = ok


async def test_the_spinner_label_shows_and_clears_on_the_status_bar(monkeypatch):
    observed: list[str] = []

    def on_run(orchestrator):
        with orchestrator.io.spinner("Noah is thinking…"):
            observed.append(orchestrator.io._app.query_one(StatusBar).busy)

    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(_StubOrchestrator(on_run=on_run)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "think")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert observed == ["Noah is thinking…"]
        assert app.query_one(StatusBar).busy == ""


# --------------------------------------------------------------------------- #
# The bridge itself
# --------------------------------------------------------------------------- #
async def test_the_io_bridge_satisfies_the_adapter_contract_and_its_extras():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        bridge = app.io_bridge
        assert bridge.name() == "tui"
        assert bridge.listen() is None
        assert bridge.view(b"", "image/png") is None
        for hook in (
            "spinner", "render_header", "render_answer", "render_plan",
            "render_validation", "render_tool_call", "confirm",
        ):
            assert callable(getattr(bridge, hook))


async def test_the_bridge_writes_plan_and_validation_panels():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Plain prose, not "1. step": these panels render the model's words
        # as markdown, which would turn that into a formatted list item and
        # drop the literal text being asserted on.
        app.io_bridge.render_plan("Noah", "First read the file")
        app.io_bridge.render_validation("Noah", "Confirmed it was read")
        app.io_bridge.render_tool_call("read_file", {"path": "note.txt"}, _Result("banana"))
        app.io_bridge.render_header("Noah", "ollama/llama3.1", "/work", "/tmp/s.json")
        await pilot.pause()

        text = _transcript_text(app)
        assert "plan" in text and "First read the file" in text
        assert "validation" in text and "Confirmed it was read" in text
        assert "tool: read_file" in text and "banana" in text
        assert "ollama/llama3.1" in text


async def test_the_final_answer_falls_back_to_the_bridge_for_an_adapter_without_the_hook(monkeypatch):
    """A ``plugins.io`` selection can put some other adapter in the
    orchestrator's I/O slot. The answer must still reach the transcript —
    the CLI's own fallback is a bare ``print()``, which in a full-screen app
    would paint a line straight over the layout."""
    orchestrator = _StubOrchestrator(io=object())  # no render_answer hook
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert "ok" in _transcript_text(app)


async def test_the_status_bar_shows_the_busy_label_alongside_its_usual_context():
    """The busy label is appended, not substituted: you should still be able
    to see which persona and model you are waiting on."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        status.busy = "Noah is thinking…"
        await pilot.pause()

        line = str(status.render())
        assert "Noah is thinking…" in line
        assert "plan: off" in line  # the context is still there


async def test_empty_renderables_are_not_written_to_the_transcript():
    """Matching ``TerminalIO``: an empty plan/validation/answer renders
    nothing rather than an empty panel."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.query_one("#transcript", RichLog).lines)
        app.io_bridge.render_answer("Noah", "")
        app.io_bridge.render_plan("Noah", "")
        app.io_bridge.render_validation("Noah", "")
        await pilot.pause()
        assert len(app.query_one("#transcript", RichLog).lines) == before


async def test_the_bridge_dispatches_across_a_real_thread_boundary():
    """The whole point of the bridge: a callback from a worker thread must
    reach the widget without touching it from that thread."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()

        def from_another_thread():
            app.io_bridge.render_answer("Noah", "from a thread")

        worker = threading.Thread(target=from_another_thread)
        worker.start()
        await _until(pilot, lambda: not worker.is_alive())
        worker.join()
        await pilot.pause()

        assert "from a thread" in _transcript_text(app)


# --------------------------------------------------------------------------- #
# /model and the startup model check. These re-patch _select_model_worker
# back to the real implementation (the autouse fixture above disables it by
# default) and stub cli._build_model so no test hits a real network port.
# --------------------------------------------------------------------------- #
class _FakeModelProvider:
    """Stands in for LocalModelProvider: reports a fixed model list, or
    raises like ``list_models()`` does when the endpoint can't be reached."""

    def __init__(self, models=None, raises=None):
        self._models = models if models is not None else []
        self._raises = raises
        self._name = ""

    def name(self):
        return f"ollama/{self._name}" if self._name else "(unconfigured)"

    def list_models(self):
        if self._raises is not None:
            raise self._raises
        return list(self._models)


def _stub_list_models(monkeypatch, models=None, raises=None):
    def fake_build_model(model_name, cwd=None, config=None):
        provider = _FakeModelProvider(models, raises)
        provider._name = model_name or ""
        return provider

    monkeypatch.setattr(CoBirbApp, "_select_model_worker", _REAL_SELECT_MODEL_WORKER)
    monkeypatch.setattr(cli, "_build_model", fake_build_model)


async def test_manual_model_command_opens_the_picker_with_the_fetched_list(monkeypatch):
    # A model already valid at startup keeps the startup check silent, so
    # the only picker that opens here is the one /model itself opens.
    _stub_list_models(monkeypatch, models=["llama3.1", "gemma4"])
    app = _make_app(model_name="llama3.1")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")

        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))
        options = app.screen.query_one("#model-options", OptionList)
        assert {options.get_option_at_index(i).id for i in range(options.option_count)} == {
            "llama3.1", "gemma4",
        }


async def test_selecting_a_model_updates_status_bar_and_writes_a_notice(monkeypatch):
    # "gemma4" is already valid (no startup picker); picking "llama3.1" (the
    # first, default-highlighted option) is then a genuine change to verify.
    _stub_list_models(monkeypatch, models=["llama3.1", "gemma4"])
    app = _make_app(model_name="gemma4")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))

        app.screen.query_one("#model-options", OptionList).focus()
        await pilot.press("enter")
        await _until(pilot, lambda: app.model_name == "llama3.1")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert "llama3.1" in str(app.query_one(StatusBar).render())
        assert "Model set to llama3.1." in _transcript_text(app)


async def test_selecting_a_model_hot_swaps_an_existing_orchestrators_model(monkeypatch):
    """A model already picked earlier in the session must take effect on the
    *next* turn without discarding the orchestrator (which would also lose
    any "always allow" approvals from this session)."""
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))
    _stub_list_models(monkeypatch, models=["llama3.1", "gemma4"])

    app = _make_app(model_name="gemma4")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")  # builds the orchestrator
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)
        assert app.orchestrator is orchestrator

        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))
        app.screen.query_one("#model-options", OptionList).focus()
        await pilot.press("enter")
        await _until(pilot, lambda: app.model_name == "llama3.1")

        assert app.orchestrator is orchestrator  # not rebuilt
        assert orchestrator.model.name() == "ollama/llama3.1"


async def test_cancelling_the_picker_leaves_the_model_unchanged(monkeypatch):
    _stub_list_models(monkeypatch, models=["llama3.1", "gemma4"])
    app = _make_app(model_name="gemma4")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))
        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, ModelPickerModal))
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert app.model_name == "gemma4"


async def test_model_command_with_an_empty_list_shows_a_message_and_no_picker(monkeypatch):
    _stub_list_models(monkeypatch, models=[])
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        assert not isinstance(app.screen, ModelPickerModal)
        assert "no models available" in _transcript_text(app)


async def test_model_command_reports_a_fetch_failure(monkeypatch):
    _stub_list_models(monkeypatch, raises=RuntimeError("connection refused"))
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)

        text = _transcript_text(app)
        assert "could not list models" in text
        assert "connection refused" in text


async def test_model_command_with_an_argument_is_rejected_without_fetching(monkeypatch):
    fetched = []

    class _Tracking(_FakeModelProvider):
        def list_models(self):
            fetched.append(1)
            return super().list_models()

    monkeypatch.setattr(CoBirbApp, "_select_model_worker", _REAL_SELECT_MODEL_WORKER)
    monkeypatch.setattr(cli, "_build_model", lambda *a, **k: _Tracking([]))

    # model_name=None means the startup check *does* fetch once (there's
    # nothing configured to validate) — the assertion below is on the count
    # not moving *after that*, i.e. that "/model something" itself never
    # fetches, not that nothing on the app ever does.
    app = _make_app()
    async with app.run_test() as pilot:
        await _until(pilot, lambda: fetched)
        before = len(fetched)

        await _submit(pilot, app, "/model something")

        assert len(fetched) == before
        assert "Usage: /model" in _transcript_text(app)


async def test_startup_check_is_silent_when_the_configured_model_is_valid(monkeypatch):
    _stub_list_models(monkeypatch, models=["gemma4"])
    app = _make_app(model_name="gemma4")
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(20):
            await pilot.pause()

        assert not isinstance(app.screen, ModelPickerModal)
        assert "was not found" not in _transcript_text(app)
        assert app.model_name == "gemma4"


async def test_startup_check_opens_the_picker_when_the_configured_model_is_missing(monkeypatch):
    """This is "default_model fails silently" from the user's side: no
    error, just a note and a chance to pick a real one."""
    _stub_list_models(monkeypatch, models=["gemma4"])
    app = _make_app(model_name="does-not-exist")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))

        assert "'does-not-exist' was not found" in _transcript_text(app)
        options = app.screen.query_one("#model-options", OptionList)
        assert options.get_option_at_index(0).id == "gemma4"


async def test_startup_check_opens_the_picker_when_no_model_is_configured(monkeypatch):
    _stub_list_models(monkeypatch, models=["gemma4"])
    app = _make_app(model_name=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))
        # No configured name to complain about — straight to the picker.
        assert "was not found" not in _transcript_text(app)


async def test_startup_check_stays_silent_on_a_fetch_failure_when_a_model_is_already_set(monkeypatch):
    """Best effort only: the model may still work fine once a real turn
    needs it, so a transient/unreachable endpoint at startup must not nag."""
    _stub_list_models(monkeypatch, raises=RuntimeError("connection refused"))
    app = _make_app(model_name="gemma4")
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(20):
            await pilot.pause()

        assert not isinstance(app.screen, ModelPickerModal)
        assert _transcript_text(app).count("connection refused") == 0


async def test_startup_check_reports_a_fetch_failure_when_no_model_is_configured(monkeypatch):
    """Here there's truly nothing to fall back on, so this one does speak up."""
    _stub_list_models(monkeypatch, raises=RuntimeError("connection refused"))
    app = _make_app(model_name=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _until(pilot, lambda: "connection refused" in _transcript_text(app))
        assert not isinstance(app.screen, ModelPickerModal)


# --------------------------------------------------------------------------- #
# Plugins tab
# --------------------------------------------------------------------------- #
async def test_plugins_pane_shows_the_registered_tools_and_active_slots():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        # RichLog only wraps its buffered content into readable `.lines` once
        # it has an actual on-screen width, which an inactive tab's content
        # never gets — so the pane has to be switched to before checking it.
        await _switch_tab(pilot, app)
        await _switch_tab(pilot, app)
        await _until(pilot, lambda: "read_file" in _plugins_log_text(app))

        text = _plugins_log_text(app)
        assert "shell" in text
        assert "core" in text  # every built-in tool's source


async def test_plugins_pane_refresh_button_repopulates_it(monkeypatch):
    calls = []
    real_describe = cli.describe_plugins

    def counting_describe(cwd):
        calls.append(1)
        return real_describe(cwd)

    monkeypatch.setattr(cli, "describe_plugins", counting_describe)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _until(pilot, lambda: len(calls) >= 1)
        before = len(calls)

        app.query_one(PluginsPane).query_one("#plugins-refresh").press()
        await _until(pilot, lambda: len(calls) > before)


async def test_plugins_pane_shows_an_error_instead_of_crashing_on_discovery_failure(monkeypatch):
    monkeypatch.setattr(cli, "describe_plugins", lambda cwd: (_ for _ in ()).throw(RuntimeError("boom")))
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _switch_tab(pilot, app)
        await _switch_tab(pilot, app)
        await _until(pilot, lambda: "boom" in _plugins_log_text(app))


def _plugins_log_text(app: CoBirbApp) -> str:
    log = app.query_one("#plugins-log", RichLog)
    return "\n".join("".join(segment.text for segment in line) for line in log.lines)


# --------------------------------------------------------------------------- #
# Sessions tab
# --------------------------------------------------------------------------- #
def _sessions_status(app: CoBirbApp) -> str:
    return str(app.query_one(SessionsPane).query_one("#sessions-status").content)


async def _switch_tab(pilot, app: CoBirbApp) -> None:
    """Switch tabs and wait for the newly-active pane to actually be laid
    out before returning.

    ``TabbedContent`` flips its child's ``display`` reactive synchronously,
    but the compositor needs a few idle cycles to give that child a real
    size — a click dispatched before that lands on whatever *used* to be at
    that (0, 0) position (in practice, the Header's app icon, which opens
    the command palette). A fixed pause count flakes; this polls the actual
    geometry instead.
    """
    tabs = app.query_one(TabbedContent)
    app.action_next_tab()
    await _until(pilot, lambda: app.query_one(f"#{tabs.active}").region.height > 0)


async def _open_sessions_tab(pilot, app: CoBirbApp) -> None:
    await pilot.pause()
    await _switch_tab(pilot, app)


async def test_sessions_pane_lists_no_saved_sessions_when_the_directory_is_empty():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        assert options.option_count == 1
        assert options.get_option_at_index(0).id is None  # the disabled placeholder row


async def test_sessions_pane_lists_files_found_in_the_default_sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    sessions_dir = session.default_sessions_dir()
    os.makedirs(sessions_dir)
    (open(os.path.join(sessions_dir, "one.json"), "w")).close()
    (open(os.path.join(sessions_dir, "two.json"), "w")).close()
    (open(os.path.join(sessions_dir, "not-a-session.txt"), "w")).close()

    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        ids = {options.get_option_at_index(i).id for i in range(options.option_count)}
        assert ids == {os.path.join(sessions_dir, "one.json"), os.path.join(sessions_dir, "two.json")}


async def test_sessions_pane_shows_the_active_session_path():
    app = _make_app(session_path="/tmp/active.json")
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        assert "/tmp/active.json" in str(app.query_one("#sessions-active").content)


async def test_resume_with_nothing_selected_asks_you_to_pick_one():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-resume")
        await pilot.pause()
        assert "Select a session" in _sessions_status(app)


async def test_resume_with_the_empty_list_placeholder_highlighted_does_nothing():
    """The "(no saved sessions found)" row is disabled but still
    highlightable by keyboard navigation; it must not be treated as a real
    session to resume."""
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        options.highlighted = 0
        await pilot.pause()
        await pilot.click("#sessions-resume")
        await pilot.pause()
        assert app.session_path is None


async def test_resume_with_the_wrong_password_reports_an_error_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto

    sessions_dir = session.default_sessions_dir()
    os.makedirs(sessions_dir)
    path = os.path.join(sessions_dir, "s.json")
    manager = session.SessionManager.create(path, AesGcmScryptSessionCrypto(), str(tmp_path), "noah")
    manager.session.add_text("user", "hi")
    manager.save("right-password")

    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        options.highlighted = 0
        await pilot.click("#sessions-resume")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "wrong-password"
        await pilot.press("enter")
        await _until(pilot, lambda: "Could not open" in _sessions_status(app))

        assert app.session_path is None


async def test_resume_with_the_right_password_switches_to_that_session(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto

    sessions_dir = session.default_sessions_dir()
    os.makedirs(sessions_dir)
    path = os.path.join(sessions_dir, "s.json")
    manager = session.SessionManager.create(path, AesGcmScryptSessionCrypto(), str(tmp_path), "professional")
    manager.session.add_text("user", "hi")
    manager.session.add_text("assistant", "hello")
    manager.save("right-password")

    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        options.highlighted = 0
        await pilot.click("#sessions-resume")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "right-password"
        await pilot.press("enter")
        await _until(pilot, lambda: app.session_path == path)

        assert app.password == "right-password"
        assert app.orchestrator is None  # rebuilt fresh, bound to this session, on the next turn
        assert app.persona.name == "Professional"  # matches what the session was created under
        assert "Professional" in app.system
        assert app.query_one(StatusBar).persona_name == "Professional"
        assert "2 turn(s)" in _sessions_status(app)


async def test_resuming_discards_an_already_built_orchestrator(monkeypatch):
    """A session switch mid-run must not silently keep talking to the old
    session through an orchestrator built before the switch.

    Also a regression test for real: submitting either of the new-session
    prompts (a plain ``Input.Submitted``) used to bubble past the modal to
    the App's own ``on_input_submitted`` — the chat box's handler — silently
    running the typed session name or password as a stray chat prompt on
    whatever orchestrator was already active. See TextPromptModal's
    ``on_input_submitted`` (``event.stop()``) for the fix."""
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(cli, "_build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", Input).disabled)
        assert app.orchestrator is orchestrator
        assert orchestrator.calls == [{"prompt": "hello", "persona": "Noah", "plan_mode": False}]

        await _switch_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "fresh"
        await pilot.press("enter")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "pw"
        await pilot.press("enter")
        await _until(pilot, lambda: app.orchestrator is None)

        # Neither prompt was run as a chat turn against the old orchestrator.
        assert orchestrator.calls == [{"prompt": "hello", "persona": "Noah", "plan_mode": False}]


async def test_new_session_prompts_for_a_name_then_a_password():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "my-session"
        await pilot.press("enter")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "s3cret"
        await pilot.press("enter")
        await _until(pilot, lambda: app.session_path is not None)

        assert app.session_path == os.path.join(session.default_sessions_dir(), "my-session.json")
        assert app.password == "s3cret"
        assert "will be created" in _sessions_status(app)
        assert app.query_one(StatusBar).session_path == app.session_path


async def test_cancelling_the_name_prompt_creates_nothing():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, TextPromptModal))

        assert app.session_path is None


async def test_the_ok_button_on_a_text_prompt_submits_the_typed_value():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "clicked-name"
        await pilot.click("#prompt-ok")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))  # the password prompt
        app.screen.query_one("#prompt-value", Input).value = "pw"
        await pilot.click("#prompt-ok")
        await _until(pilot, lambda: app.session_path is not None)

        assert app.session_path == os.path.join(session.default_sessions_dir(), "clicked-name.json")


async def test_the_cancel_button_on_a_text_prompt_also_cancels():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        await pilot.click("#prompt-cancel")
        await _until(pilot, lambda: not isinstance(app.screen, TextPromptModal))

        assert app.session_path is None


async def test_cancelling_the_resume_password_prompt_changes_nothing():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        # Nothing to actually select in an empty list — the point here is
        # only that a cancelled password prompt short-circuits before
        # SessionsPane.set_status("Unlocking…") ever runs.
        options.add_option(Option("x", id="/tmp/whatever.json"))
        options.highlighted = options.option_count - 1
        await pilot.pause()
        await pilot.click("#sessions-resume")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, TextPromptModal))
        await pilot.pause()

        assert app.session_path is None
        assert "Unlocking" not in _sessions_status(app)


async def test_cancelling_the_password_prompt_creates_nothing():
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "my-session"
        await pilot.press("enter")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, TextPromptModal))

        assert app.session_path is None


async def test_new_session_refuses_to_overwrite_an_existing_name(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    sessions_dir = session.default_sessions_dir()
    os.makedirs(sessions_dir)
    open(os.path.join(sessions_dir, "taken.json"), "w").close()

    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "taken"
        await pilot.press("enter")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "pw"
        await pilot.press("enter")
        await _until(pilot, lambda: "already exists" in _sessions_status(app))

        assert app.session_path is None


async def test_the_refresh_button_rescans_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        assert options.option_count == 1  # the empty-list placeholder

        sessions_dir = session.default_sessions_dir()
        os.makedirs(sessions_dir)
        open(os.path.join(sessions_dir, "new.json"), "w").close()

        await pilot.click("#sessions-refresh")
        await pilot.pause()
        assert options.get_option_at_index(0).id == os.path.join(sessions_dir, "new.json")


async def test_activating_the_sessions_tab_refreshes_its_listing(tmp_path, monkeypatch):
    """The listing is scanned once at mount (before anything could exist in
    a fresh temp dir) — switching to the tab later must re-scan rather than
    show what was there at startup."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()

        sessions_dir = session.default_sessions_dir()
        os.makedirs(sessions_dir)
        open(os.path.join(sessions_dir, "late.json"), "w").close()

        await _switch_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        assert options.get_option_at_index(0).id == os.path.join(sessions_dir, "late.json")
