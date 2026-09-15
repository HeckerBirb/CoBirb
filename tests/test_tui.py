"""Tests for interactive mode's Textual app (``cobirb.tui``).

Driven through ``CoBirbApp.run_test()`` -> ``Pilot``, with
``cobirb.tui.app.wiring.build_orchestrator`` monkeypatched to hand back a stub
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
from types import SimpleNamespace

import pytest
from rich.text import Text
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Footer, Input, OptionList, RichLog, TabbedContent
from textual.widgets.option_list import Option

from conftest import StubSession, StubSessionManager

from cobirb import cli, help_text, memory, paths, session
from cobirb.plugins.core import render
from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto
from cobirb.runtime import commands, personas, plugins, sessions, wiring
from cobirb.tui.app import CoBirbApp
from cobirb.tui.panes import PluginsPane, SessionsPane
from cobirb.tui.screens import (
    ApprovalModal,
    HelpModal,
    MemoryCataloguesModal,
    ModelPickerModal,
    NewCatalogueModal,
    PersonaPickerModal,
    RememberModal,
    TextPromptModal,
    _ordered_catalogue_options,
)
from cobirb.tui.widgets import (
    PromptHistory,
    PromptInput,
    StatusBar,
    StreamPreview,
    TranscriptLog,
)

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


class _StubOrchestrator:
    """Stands in for a real Orchestrator: records the run() it was given and
    can be told to raise, stream, or make a tool call along the way."""

    def __init__(self, *, run_raises=None, with_session_manager=False, on_run=None, io=None, tools=None):
        self.session = StubSessionManager() if with_session_manager else None
        self.io = io
        self.last_turn_streamed = False
        self.calls = []
        self.tools = tools if tools is not None else {}
        self._run_raises = run_raises
        self._on_run = on_run

    def close(self):
        """Part of the Orchestrator contract since MCP servers became
        child processes it owns. A no-op here; the stub starts nothing."""
        self.closed = True

    def run(self, prompt, system, *, cwd, persona, session_path=None, plan_mode=False, images=None):
        self.calls.append(
            {"prompt": prompt, "system": system, "persona": persona, "plan_mode": plan_mode, "images": images}
        )
        if self._run_raises is not None:
            raise self._run_raises
        if self._on_run is not None:
            self._on_run(self)
        return StubSession()


def _stub_build(orchestrator=None, record=None):
    """A ``_build_orchestrator`` replacement that hands back ``orchestrator``.

    Mirrors the real signature (including ``io_factory``) so a test also
    proves the TUI passes its bridge in rather than letting the default
    ``TerminalIO`` print over the app.
    """

    def fake_build_orchestrator(
        cwd, persona, allow_overrides, session_path=None, password=None,
        model_name=None, io_factory=None,
    ):
        target = orchestrator if orchestrator is not None else _StubOrchestrator()
        if io_factory is not None and target.io is None:
            target.io = io_factory()
        if record is not None:
            record.append({"persona": persona.name, "io_factory": io_factory, "built": target})
        return target

    return fake_build_orchestrator


def _make_app(**overrides) -> CoBirbApp:
    persona = personas.load_persona(overrides.pop("persona_name", None))
    # Built here rather than taken as a plain string so `system` and
    # `harness` can't disagree — the real CLI derives one from the other.
    harness = overrides.get("harness", False)
    kwargs = dict(
        persona=persona,
        system=personas.build_system_prompt(persona, harness=harness),
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
    prompt_input = app.query_one("#prompt-input", PromptInput)
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
        assert [pane.id for pane in app.query("TabPane")] == [
            "current", "flock", "sessions", "plugins",
        ]
        assert tabs.active == "current"


async def test_status_bar_shows_persona_model_plan_mode_and_cwd():
    app = _make_app(cwd="/some/where", plan_mode=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        line = str(app.query_one(StatusBar).render())
        assert "CoBirb" in line  # no persona by default
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


async def test_the_greeting_is_in_the_transcript_at_startup():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _transcript_text(app)
        assert "CoBirb ready." in text  # a plain line, not a persona greeting
        assert "/persona" in text  # the hint line


async def test_next_tab_cycles_through_every_tab_and_wraps():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        for expected in ("flock", "sessions", "plugins", "current"):
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
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello there")

        prompt_input = app.query_one("#prompt-input", PromptInput)
        await _until(pilot, lambda: not prompt_input.disabled)

        assert [(c["prompt"], c["persona"]) for c in orchestrator.calls] == [("hello there", "CoBirb")]
        text = _transcript_text(app)
        assert "hello there" in text  # the echoed user line
        assert "ok" in text  # the stub session's summary, via render_answer


async def test_a_blank_submission_never_builds_an_orchestrator(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for a blank prompt")

    monkeypatch.setattr(wiring, "build_orchestrator", fail_if_called)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "   ")
        assert not app.query_one("#prompt-input", PromptInput).disabled


async def test_the_orchestrator_is_built_once_and_reused_across_turns(monkeypatch):
    """Regression guard carried over from the scrolling loop: rebuilding per
    turn would discard the Policy, so an "always allow this" approval would
    silently stop applying on the next turn."""
    builds = []
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator, record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", PromptInput)
        for prompt in ("first", "second"):
            await _submit(pilot, app, prompt)
            await _until(pilot, lambda: not prompt_input.disabled)

        assert len(builds) == 1
        assert [call["prompt"] for call in orchestrator.calls] == ["first", "second"]


async def test_the_tui_passes_its_own_io_bridge_into_the_orchestrator(monkeypatch):
    """Without this, the orchestrator would render through the default
    ``TerminalIO`` and print straight over the full-screen app."""
    builds = []
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert builds[0]["io_factory"]() is app.io_bridge


async def test_a_permission_error_is_reported_and_the_input_comes_back(monkeypatch):
    orchestrator = _StubOrchestrator(run_raises=cli.PermissionError("nope"))
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "do it")
        prompt_input = app.query_one("#prompt-input", PromptInput)
        await _until(pilot, lambda: not prompt_input.disabled)

        assert "blocked" in _transcript_text(app)


async def test_an_unexpected_error_is_reported_and_the_input_comes_back(monkeypatch):
    orchestrator = _StubOrchestrator(run_raises=RuntimeError("model exploded"))
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "do it")
        prompt_input = app.query_one("#prompt-input", PromptInput)
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

    monkeypatch.setattr(wiring, "build_orchestrator", explode)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)


async def test_the_session_is_saved_after_a_successful_turn(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app(session_path="/tmp/s.json", password="pw")
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", PromptInput)
        for prompt in ("first", "second"):
            await _submit(pilot, app, prompt)
            await _until(pilot, lambda: not prompt_input.disabled)

        assert orchestrator.session.saved_with == ["pw", "pw"]


async def test_nothing_is_saved_when_no_session_path_was_given(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert orchestrator.session.saved_with == []


# --------------------------------------------------------------------------- #
# Cancelling a turn (Ctrl+C) and quitting mid-turn (Ctrl+Q).
#
# Regression coverage for a real bug: a `shell` call that hangs (a command
# that doesn't produce output until you interact with it, a game loop, a
# server — anything that doesn't exit on its own) used to leave the input
# disabled with no way out short of waiting up to its 5-minute timeout, and
# even quitting the app blocked for that same span, because Textual/
# asyncio's shutdown waits for the worker thread to actually return. See
# ShellTool.cancel_running() (tools.py) for the other half of the fix.
# --------------------------------------------------------------------------- #
class _CancellableOrchestrator(_StubOrchestrator):
    """An orchestrator whose run() blocks until something calls
    cancel_running() on its fake ``shell`` tool — simulating a hung shell
    command exactly the way the real bug looked from the TUI's side."""

    def __init__(self, *, shell_present=True, steer_accepts=True):
        super().__init__()
        self._release = threading.Event()
        self.cancel_calls = 0
        self.steer_calls: list[str] = []
        self._steer_accepts = steer_accepts
        if shell_present:
            self.tools = {"shell": SimpleNamespace(cancel_running=self._cancel_running)}

    def _cancel_running(self) -> bool:
        self.cancel_calls += 1
        self._release.set()
        return True

    def steer(self, message: str) -> bool:
        self.steer_calls.append(message)
        return self._steer_accepts

    def run(self, prompt, system, *, cwd, persona, session_path=None, plan_mode=False, images=None):
        self.calls.append({"prompt": prompt, "persona": persona, "plan_mode": plan_mode})
        self._release.wait(timeout=5)
        return StubSession()


async def test_ctrl_c_cancels_a_stuck_turn_and_the_input_comes_back(monkeypatch):
    orchestrator = _CancellableOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run the game")
        await _until(pilot, lambda: app._turn_in_progress)

        await pilot.press("ctrl+c")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert orchestrator.cancel_calls == 1


async def test_ctrl_c_with_nothing_cancellable_does_not_touch_the_turn(monkeypatch):
    """No `shell` tool in play (e.g. still waiting on the model itself) —
    there's nothing this can interrupt, so it must say so and leave the
    turn running rather than pretending to have stopped anything."""
    orchestrator = _CancellableOrchestrator(shell_present=False)
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: app._turn_in_progress)

        await pilot.press("ctrl+c")
        await pilot.pause()

        # The box stays enabled through an ordinary turn now (mid-turn
        # steering — see Orchestrator.steer()), so "still running" is no
        # longer visible as a disabled input; _turn_in_progress is the actual
        # signal, and is what the other tests in this section already check.
        assert app._turn_in_progress
        orchestrator._release.set()  # let it finish so the test cleans up promptly
        await _until(pilot, lambda: not app._turn_in_progress)


# --------------------------------------------------------------------------- #
# Mid-turn steering: a message submitted while a turn is already running
# redirects it (Orchestrator.steer()) instead of starting a second,
# overlapping one. See Orchestrator.steer() for the model-facing half.
# --------------------------------------------------------------------------- #
async def test_a_message_typed_mid_turn_steers_it_instead_of_starting_a_new_one(monkeypatch):
    orchestrator = _CancellableOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run the game")
        await _until(pilot, lambda: app._turn_in_progress)

        await _submit(pilot, app, "actually, stop and check the tests first")

        assert orchestrator.steer_calls == ["actually, stop and check the tests first"]
        assert len(orchestrator.calls) == 1  # no second, overlapping run() started
        assert "actually, stop and check the tests first" in _transcript_text(app)
        # The box stayed usable throughout — never disabled by the steer.
        assert not app.query_one("#prompt-input", PromptInput).disabled

        orchestrator._release.set()
        await _until(pilot, lambda: not app._turn_in_progress)


async def test_steering_refused_by_the_orchestrator_is_reported_plainly(monkeypatch):
    """A race between the keypress and the turn actually finishing —
    Orchestrator.steer() returning False must not be silently swallowed,
    since the user's message genuinely did not go anywhere."""
    orchestrator = _CancellableOrchestrator(steer_accepts=False)
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run the game")
        await _until(pilot, lambda: app._turn_in_progress)

        await _submit(pilot, app, "too late")

        assert orchestrator.steer_calls == ["too late"]
        assert "Nothing to steer" in _transcript_text(app)
        # The box was cleared before we knew it would be refused, so the
        # history is the only way back to what was typed — one up-arrow from
        # sending it as an ordinary prompt instead.
        prompt_input = app.query_one("#prompt-input", PromptInput)
        prompt_input.action_history_prev()
        assert prompt_input.value == "too late"

        orchestrator._release.set()
        await _until(pilot, lambda: not app._turn_in_progress)


async def test_ctrl_c_with_no_turn_running_falls_back_to_the_quit_hint(monkeypatch):
    """Idle Ctrl+C keeps Textual's own default behavior (a "press ctrl+q to
    quit" toast) — this only takes over when there's actually a turn to
    interrupt."""
    calls = []
    monkeypatch.setattr(CoBirbApp, "action_help_quit", lambda self: calls.append(1))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert calls == [1]


async def test_quitting_mid_turn_attempts_to_cancel_first(monkeypatch):
    calls = []
    monkeypatch.setattr(CoBirbApp, "_attempt_cancel", lambda self: calls.append(1) or True)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_in_progress = True
        await app.action_quit()

    assert calls == [1]


async def test_quitting_while_idle_does_not_attempt_to_cancel(monkeypatch):
    calls = []
    monkeypatch.setattr(CoBirbApp, "_attempt_cancel", lambda self: calls.append(1) or True)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_quit()

    assert calls == []


async def test_attempt_cancel_is_false_with_no_orchestrator_yet():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.orchestrator is None
        assert app._attempt_cancel() is False


async def test_attempt_cancel_is_false_when_the_active_tool_has_no_cancel_hook():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.orchestrator = _StubOrchestrator(tools={"shell": object()})
        assert app._attempt_cancel() is False


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
async def test_persona_with_no_argument_opens_the_picker_without_building_anything(monkeypatch):
    """Bare /persona is a menu, exactly like bare /model — you shouldn't have
    to already know how a persona is spelled to switch to it."""
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for /persona")

    monkeypatch.setattr(wiring, "build_orchestrator", fail_if_called)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona")
        await _until(pilot, lambda: isinstance(app.screen, PersonaPickerModal))

        options = app.screen.query_one("#model-options", OptionList)
        listed = [options.get_option_at_index(i).id for i in range(options.option_count)]
        assert listed[0] == "none"  # the way back out of a persona comes first
        for name in ("noah", "professional", "neighbor", "kawaii"):
            assert name in listed


async def test_persona_switch_updates_the_status_bar_and_later_turns(monkeypatch):
    builds = []
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona professional")

        assert app.query_one(StatusBar).persona_name == "Professional"
        assert not builds  # a slash command never reaches the model

        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        # The switch has to change the system prompt too, not just the label.
        assert builds[0]["persona"] == "Professional"
        assert "Professional" in builds[0]["built"].calls[0]["system"]


async def test_plan_status_reports_without_building_an_orchestrator(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for /plan")

    monkeypatch.setattr(wiring, "build_orchestrator", fail_if_called)

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
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/plan on")
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "what is a bird?")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(_confirming_orchestrator(decisions)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run git status")

        await _until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        assert "git status" in _static_text(app, "#approval-body")

        await pilot.press(key)
        await _until(pilot, lambda: bool(decisions))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert decisions == [expected]


@pytest.mark.parametrize(
    "button,expected",
    [("#approve-once", "once"), ("#approve-always", "always"), ("#approve-deny", "deny")],
)
async def test_the_approval_modal_buttons_resolve_the_same_way(monkeypatch, button, expected):
    decisions: list[str] = []
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(_confirming_orchestrator(decisions)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "run git status")

        await _until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        await pilot.click(button)
        await _until(pilot, lambda: bool(decisions))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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

    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(_StubOrchestrator(on_run=on_run)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "say hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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

    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(_StubOrchestrator(on_run=on_run)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "read the note")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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

    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(_StubOrchestrator(on_run=on_run)))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "think")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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
            "spinner", "render_answer", "render_plan",
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
        await pilot.pause()

        text = _transcript_text(app)
        assert "plan" in text and "First read the file" in text
        assert "validation" in text and "Confirmed it was read" in text
        assert "tool: read_file" in text and "banana" in text


async def test_the_final_answer_falls_back_to_the_bridge_for_an_adapter_without_the_hook(monkeypatch):
    """A ``plugins.io`` selection can put some other adapter in the
    orchestrator's I/O slot. The answer must still reach the transcript —
    the CLI's own fallback is a bare ``print()``, which in a full-screen app
    would paint a line straight over the layout."""
    orchestrator = _StubOrchestrator(io=object())  # no render_answer hook
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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
# default) and stub wiring.build_model so no test hits a real network port.
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
    monkeypatch.setattr(wiring, "build_model", fake_build_model)


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
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert "llama3.1" in str(app.query_one(StatusBar).render())
        assert "Model set to llama3.1." in _transcript_text(app)


async def test_selecting_a_model_hot_swaps_an_existing_orchestrators_model(monkeypatch):
    """A model already picked earlier in the session must take effect on the
    *next* turn without discarding the orchestrator (which would also lose
    any "always allow" approvals from this session)."""
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))
    _stub_list_models(monkeypatch, models=["llama3.1", "gemma4"])

    app = _make_app(model_name="gemma4")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")  # builds the orchestrator
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)
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
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert app.model_name == "gemma4"


async def test_model_command_with_an_empty_list_shows_a_message_and_no_picker(monkeypatch):
    _stub_list_models(monkeypatch, models=[])
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert not isinstance(app.screen, ModelPickerModal)
        assert "no models available" in _transcript_text(app)


async def test_model_command_reports_a_fetch_failure(monkeypatch):
    _stub_list_models(monkeypatch, raises=RuntimeError("connection refused"))
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/model")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

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
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _Tracking([]))

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


async def test_startup_picker_selection_is_confirmed_in_the_transcript(monkeypatch):
    """The only place the startup check's own choice of model is ever
    stated (there is no header banner any more — see AGENTS.md on why it
    was removed), the same "Model set to X." notice `/model` uses
    mid-session."""
    _stub_list_models(monkeypatch, models=["llama3.1", "gemma4"])
    app = _make_app(model_name="does-not-exist")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _until(pilot, lambda: isinstance(app.screen, ModelPickerModal))

        app.screen.query_one("#model-options", OptionList).focus()
        await pilot.press("enter")
        await _until(pilot, lambda: app.model_name == "llama3.1")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert "Model set to llama3.1." in _transcript_text(app)


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
        await _switch_tab(pilot, app, "plugins")
        await _until(pilot, lambda: "read_file" in _plugins_log_text(app))

        text = _plugins_log_text(app)
        assert "shell" in text
        assert "core" in text  # every built-in tool's source


async def test_plugins_pane_refresh_button_repopulates_it(monkeypatch):
    calls = []
    real_describe = plugins.describe_plugins

    def counting_describe(cwd):
        calls.append(1)
        return real_describe(cwd)

    monkeypatch.setattr(plugins, "describe_plugins", counting_describe)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _until(pilot, lambda: len(calls) >= 1)
        before = len(calls)

        app.query_one(PluginsPane).query_one("#plugins-refresh").press()
        await _until(pilot, lambda: len(calls) > before)


async def test_plugins_pane_shows_an_error_instead_of_crashing_on_discovery_failure(monkeypatch):
    monkeypatch.setattr(plugins, "describe_plugins", lambda cwd: (_ for _ in ()).throw(RuntimeError("boom")))
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _switch_tab(pilot, app, "plugins")
        await _until(pilot, lambda: "boom" in _plugins_log_text(app))


def _plugins_log_text(app: CoBirbApp) -> str:
    log = app.query_one("#plugins-log", RichLog)
    return "\n".join("".join(segment.text for segment in line) for line in log.lines)


# --------------------------------------------------------------------------- #
# Sessions tab
# --------------------------------------------------------------------------- #
def _sessions_status(app: CoBirbApp) -> str:
    return str(app.query_one(SessionsPane).query_one("#sessions-status").content)


async def _switch_tab(pilot, app: CoBirbApp, name: str | None = None) -> None:
    """Show a tab and wait for it to actually be laid out before returning.

    ``TabbedContent`` flips its child's ``display`` reactive synchronously,
    but the compositor needs a few idle cycles to give that child a real
    size — a click dispatched before that lands on whatever *used* to be at
    that (0, 0) position (in practice, the Header's app icon, which opens
    the command palette). A fixed pause count flakes; this polls the actual
    geometry instead.

    ``name`` selects a tab outright. Cycling with ``action_next_tab`` instead
    silently couples every caller to the tab *order*, which is how adding the
    Flock tab broke eight tests that had nothing to do with tabs.
    """
    tabs = app.query_one(TabbedContent)
    if name is None:
        app.action_next_tab()
    else:
        tabs.active = name
    await _until(pilot, lambda: app.query_one(f"#{tabs.active}").region.height > 0)


async def _open_sessions_tab(pilot, app: CoBirbApp) -> None:
    await pilot.pause()
    await _switch_tab(pilot, app, "sessions")


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


async def test_branch_forks_the_selected_session_and_switches_into_it(tmp_path, monkeypatch):
    """The Sessions tab's "Branch…" — see SessionsPane's docstring for why
    this lands straight in the branch the same way Resume does, and
    session.fork_session's own tests for the branching logic itself."""
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
        await pilot.click("#sessions-branch")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "right-password"
        await pilot.press("enter")
        await _until(pilot, lambda: app.session_path != path and app.session_path is not None)

        # A new file, not the one that was selected — the original is left
        # exactly as it was.
        assert app.session_path != path
        assert os.path.exists(path)
        original = session.SessionManager.load(path, AesGcmScryptSessionCrypto(), "right-password")
        assert len(original.session.turns) == 2

        assert app.password == "right-password"
        assert app.orchestrator is None  # rebuilt fresh, bound to the branch, on the next turn
        assert app.persona.name == "Professional"
        assert "Branched" in _sessions_status(app)
        assert "2 turn(s)" in _sessions_status(app)


async def test_branch_with_the_wrong_password_reports_an_error_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto

    sessions_dir = session.default_sessions_dir()
    os.makedirs(sessions_dir)
    path = os.path.join(sessions_dir, "s.json")
    manager = session.SessionManager.create(path, AesGcmScryptSessionCrypto(), str(tmp_path))
    manager.save("right-password")

    app = _make_app()
    async with app.run_test() as pilot:
        await _open_sessions_tab(pilot, app)
        options = app.query_one("#sessions-list", OptionList)
        options.highlighted = 0
        await pilot.click("#sessions-branch")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "wrong-password"
        await pilot.press("enter")
        await _until(pilot, lambda: "Could not branch" in _sessions_status(app))

        assert app.session_path is None


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
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)
        assert app.orchestrator is orchestrator
        assert [(c["prompt"], c["persona"]) for c in orchestrator.calls] == [("hello", "CoBirb")]

        await _switch_tab(pilot, app, "sessions")
        await pilot.click("#sessions-new")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "fresh"
        await pilot.press("enter")
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))
        app.screen.query_one("#prompt-value", Input).value = "pw"
        await pilot.press("enter")
        await _until(pilot, lambda: app.orchestrator is None)

        # Neither prompt was run as a chat turn against the old orchestrator.
        assert [(c["prompt"], c["persona"]) for c in orchestrator.calls] == [("hello", "CoBirb")]


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

        await _switch_tab(pilot, app, "sessions")
        options = app.query_one("#sessions-list", OptionList)
        assert options.get_option_at_index(0).id == os.path.join(sessions_dir, "late.json")


# --------------------------------------------------------------------------- #
# Prompt history (up/down recall)
# --------------------------------------------------------------------------- #
def test_history_keeps_the_most_recent_entries_and_drops_the_oldest():
    history = PromptHistory(max_entries=3)
    for text in ("one", "two", "three", "four"):
        history.add(text)
    assert history.entries == ["two", "three", "four"]


def test_history_evicts_on_the_byte_budget_even_when_under_the_entry_count():
    """The two ceilings are independent: a handful of large prompts has to be
    trimmed by size long before it reaches the entry limit."""
    history = PromptHistory(max_entries=100, max_bytes=10)
    history.add("aaaaa")
    history.add("bbbbb")
    history.add("ccccc")
    assert history.entries == ["bbbbb", "ccccc"]
    assert history.size_bytes == 10


def test_history_measures_bytes_not_characters():
    history = PromptHistory()
    history.add("é")  # two bytes in UTF-8, one character
    assert history.size_bytes == 2


def test_history_ignores_blank_submissions_and_immediate_repeats():
    history = PromptHistory()
    history.add("  ")
    history.add("")
    history.add("ls")
    history.add("ls")
    history.add("pwd")
    history.add("ls")
    assert history.entries == ["ls", "pwd", "ls"]


def test_history_entries_cannot_be_mutated_by_a_caller():
    history = PromptHistory()
    history.add("keep me")
    history.entries.clear()
    assert history.entries == ["keep me"]


async def test_up_and_down_walk_the_prompt_history(monkeypatch):
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", PromptInput)

        await _submit(pilot, app, "/plan on")
        await _until(pilot, lambda: not prompt_input.disabled)
        await _submit(pilot, app, "first task")
        await _until(pilot, lambda: not prompt_input.disabled)

        await pilot.press("up")
        assert prompt_input.value == "first task"
        await pilot.press("up")
        assert prompt_input.value == "/plan on"  # slash commands are recalled too
        await pilot.press("up")
        assert prompt_input.value == "/plan on"  # already at the oldest; stays put
        await pilot.press("down")
        assert prompt_input.value == "first task"


async def test_arrowing_back_down_past_the_newest_entry_restores_the_draft(monkeypatch):
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", PromptInput)

        await _submit(pilot, app, "sent earlier")
        await _until(pilot, lambda: not prompt_input.disabled)

        prompt_input.value = "half-typed"
        await pilot.press("up")
        assert prompt_input.value == "sent earlier"
        await pilot.press("down")
        assert prompt_input.value == "half-typed"


async def test_down_does_nothing_when_not_walking_the_history():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", PromptInput)
        prompt_input.value = "typing"
        await pilot.press("down")
        assert prompt_input.value == "typing"


async def test_up_does_nothing_with_an_empty_history():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt_input = app.query_one("#prompt-input", PromptInput)
        prompt_input.value = "typing"
        await pilot.press("up")
        assert prompt_input.value == "typing"


# --------------------------------------------------------------------------- #
# Copying out of the transcript
# --------------------------------------------------------------------------- #
async def test_ctrl_c_copies_the_transcript_selection_instead_of_cancelling():
    """Ctrl+C is bound at priority for cancellation, which took the key away
    from Textual's own ``screen.copy_text`` — so the app has to hand it back
    when there is actually something selected."""
    copied: list[str] = []
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.copy_to_clipboard = copied.append
        transcript = app.query_one("#transcript", RichLog)
        # Selection(None, None) is Textual's "everything in this widget" —
        # the same shape a mouse drag across the whole transcript produces.
        app.screen.selections = {transcript: Selection(None, None)}

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert copied and "CoBirb ready." in copied[0]
        assert not app.screen.selections  # cleared, so the next ctrl+c cancels


async def test_ctrl_c_still_cancels_a_turn_when_nothing_is_selected(monkeypatch):
    cancelled: list[bool] = []
    monkeypatch.setattr(CoBirbApp, "_attempt_cancel", lambda self: bool(cancelled.append(True)) or True)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_in_progress = True
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert cancelled == [True]


async def test_the_transcript_allows_text_selection():
    """``RichLog`` opts into Textual's selection support by default; this
    pins that the transcript is never switched to a non-selectable widget."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#transcript", RichLog).allow_select is True


# --------------------------------------------------------------------------- #
# Telling "you" and the assistant apart
# --------------------------------------------------------------------------- #
async def test_a_submitted_prompt_is_marked_and_closed_by_a_rule(monkeypatch):
    """Open space above, a rule below.

    The rule replaced a blank line, and does the job the blank line was doing
    better: both halves of an exchange are a `>` in different colours, which
    is enough to tell whose turn a line is and not enough to find the seam
    when scrolling back. Closing the prompt says the answer follows.
    """
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello there")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        lines = _transcript_text(app).splitlines()
        marked = next(i for i, line in enumerate(lines) if line.startswith("> hello there"))
        assert lines[marked - 1].strip() == ""
        assert set(lines[marked + 1].strip()) == {"─"}
        assert "You:" not in _transcript_text(app)


async def test_the_transcript_extracts_a_partial_selection_precisely():
    """The whole point of the ``TranscriptLog`` subclass: a stock ``RichLog``
    returns ``None`` here, so any selection copied as nothing."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        transcript.write(Text("hello world"))
        transcript.write(Text("second line"))
        await pilot.pause()

        extracted, ending = transcript.get_selection(
            Selection.from_offsets(Offset(6, 0), Offset(6, 1))
        )
        assert extracted == "world\nsecond"
        assert ending == "\n"


async def test_the_transcript_does_not_copy_richlogs_line_padding():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        transcript.write(Text("short"))
        transcript.write(Text("also short"))
        await pilot.pause()

        extracted, _ = transcript.get_selection(Selection(None, None))
        assert extracted == "short\nalso short"


async def test_rendered_transcript_lines_carry_the_offset_metadata():
    """Without this metadata the compositor can't map a mouse position to a
    character offset, and every drag collapses to a whole-widget selection."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        transcript.write(Text("abcdef"))
        await pilot.pause()

        strip = transcript.render_line(0)
        offsets = [
            segment.style.meta.get("offset")
            for segment in strip
            if segment.style is not None and segment.style._meta is not None
        ]
        assert offsets and offsets[0] == (0, 0)


async def test_a_selected_span_is_highlighted_in_the_transcript():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        transcript.write(Text("abcdef"))
        await pilot.pause()

        plain = transcript.render_line(0)
        app.screen.selections = {
            transcript: Selection.from_offsets(Offset(2, 0), Offset(4, 0))
        }
        await pilot.pause()
        highlighted = transcript.render_line(0)

        assert highlighted.text == plain.text  # same characters...
        assert highlighted != plain  # ...differently styled
        selection_style = app.screen.get_component_rich_style("screen--selection")
        styled = "".join(
            segment.text
            for segment in highlighted
            if segment.style is not None and segment.style.bgcolor == selection_style.bgcolor
        )
        assert styled == "cd"


# --------------------------------------------------------------------------- #
# /persona as a picker (the same shape as /model)
# --------------------------------------------------------------------------- #
async def test_choosing_a_persona_from_the_picker_applies_it(monkeypatch):
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona")
        await _until(pilot, lambda: isinstance(app.screen, PersonaPickerModal))

        options = app.screen.query_one("#model-options", OptionList)
        options.focus()
        options.highlighted = [
            options.get_option_at_index(i).id for i in range(options.option_count)
        ].index("noah")
        await pilot.press("enter")
        await _until(pilot, lambda: not isinstance(app.screen, PersonaPickerModal))

        assert app.persona.name == "Noah"
        assert "African Grey Parrot" in app.system
        assert app.query_one(StatusBar).persona_name == "Noah"


async def test_the_persona_picker_marks_none_as_current_by_default():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona")
        await _until(pilot, lambda: isinstance(app.screen, PersonaPickerModal))

        options = app.screen.query_one("#model-options", OptionList)
        marked = [
            str(options.get_option_at_index(i).prompt)
            for i in range(options.option_count)
            if str(options.get_option_at_index(i).prompt).startswith("> ")
        ]
        assert marked == ["> none"]


async def test_the_persona_picker_marks_the_active_persona_by_its_key():
    """kawaii.json calls itself "Imouto" — the picker lists keys, so the
    marker has to be resolved through the key, not the display name."""
    app = _make_app(persona_name="kawaii")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona")
        await _until(pilot, lambda: isinstance(app.screen, PersonaPickerModal))

        options = app.screen.query_one("#model-options", OptionList)
        marked = [
            str(options.get_option_at_index(i).prompt)
            for i in range(options.option_count)
            if str(options.get_option_at_index(i).prompt).startswith("> ")
        ]
        assert marked == ["> kawaii"]


async def test_cancelling_the_persona_picker_changes_nothing():
    app = _make_app(persona_name="noah")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona")
        await _until(pilot, lambda: isinstance(app.screen, PersonaPickerModal))

        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, PersonaPickerModal))

        assert app.persona.name == "Noah"


async def test_persona_with_a_name_still_switches_directly_without_the_picker():
    """History recall replays "/persona kawaii" verbatim, so the named form
    has to keep working rather than always opening a menu."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/persona kawaii")
        await pilot.pause()

        assert not isinstance(app.screen, PersonaPickerModal)
        assert app.persona.name == "Imouto"


async def test_switching_to_none_strips_the_voice_from_the_system_prompt():
    app = _make_app(persona_name="noah")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "African Grey Parrot" in app.system

        await _submit(pilot, app, "/persona none")
        await pilot.pause()

        assert app.system == ""  # nothing left for CoBirb to send at all
        assert "own voice" in _transcript_text(app)


async def test_a_persona_switch_keeps_the_harness_choice_the_run_started_with():
    """--system-prompt harness has to survive a mid-session /persona change;
    rebuilding the prompt without it would silently drop the setting."""
    app = _make_app(harness=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.system == personas._HARNESS_PROMPT

        await _submit(pilot, app, "/persona noah")
        await pilot.pause()

        assert app.system.startswith(personas._HARNESS_PROMPT)
        assert "African Grey Parrot" in app.system


# --------------------------------------------------------------------------- #
# The transcript as one marked column
# --------------------------------------------------------------------------- #
def _marker_colour_name(style: str) -> str:
    """The colour name/hex Rich's own renderer would report for a style
    string — kept alongside ``_marker_colours`` rather than hardcoding the
    palette a second time, so a future colour change doesn't need a matching
    edit here."""
    from rich.style import Style

    return Style.parse(style).color.name


def _marker_colours(app: CoBirbApp) -> list[tuple[str, str]]:
    """(marker colour, rest of the line) for every marked line, in order."""
    out = []
    for line in app.query_one("#transcript", TranscriptLog).lines:
        segments = list(line)
        if segments and segments[0].text == "> ":
            body = "".join(s.text for s in segments[1:]).rstrip()
            out.append((segments[0].style.color.name, body))
    return out


async def test_prompts_and_replies_share_a_marker_in_different_colours(monkeypatch):
    """The requested shape: no "CoBirb:" label and no panel around replies —
    one column of `>` lines, told apart by the marker's colour."""
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "Hi")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        marked = _marker_colours(app)
        assert [body for _, body in marked] == ["Hi", "ok"]  # prompt, then the reply
        assert marked[0][0] != marked[1][0]  # different colours
        assert "CoBirb: ok" not in _transcript_text(app)


async def test_a_streamed_reply_is_marked_like_any_other(monkeypatch):
    """A streamed reply is flushed straight into the transcript rather than
    going through render_answer, so it needs marking on its own path."""
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.io_bridge.render("streamed reply")
        await pilot.pause()
        app._flush_stream()
        await pilot.pause()

        assert (_marker_colour_name(render.ASSISTANT_MARKER_STYLE), "streamed reply") in _marker_colours(app)


async def test_the_streaming_preview_is_marked_so_the_reply_does_not_shift(monkeypatch):
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.io_bridge.render("partial")
        await pilot.pause()

        preview = app.query_one("#streaming-preview", StreamPreview)
        assert preview.display is True
        assert preview.buffered == "partial"


# --------------------------------------------------------------------------- #
# Selection highlighting
# --------------------------------------------------------------------------- #
async def test_selected_text_keeps_its_own_colour_and_only_the_background_changes():
    """The theme defines screen--selection with a *transparent* foreground,
    which flattens to the same colour as its background — applying the whole
    style painted selected text as an unreadable solid block."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        transcript.write(Text("abcdef"))
        await pilot.pause()

        plain = {seg.text: seg.style for seg in transcript.render_line(0)}
        app.screen.selections = {
            transcript: Selection.from_offsets(Offset(0, 0), Offset(6, 0))
        }
        await pilot.pause()
        selected = [seg for seg in transcript.render_line(0) if seg.text == "abcdef"][0]

        assert selected.style.color == plain["abcdef"].color  # text still readable
        assert selected.style.bgcolor != plain["abcdef"].bgcolor  # but marked
        assert selected.style.color != selected.style.bgcolor  # not a solid block


async def test_a_streamed_reply_carries_no_persona_label(monkeypatch):
    """The bug this fixes: the orchestrator wrote "CoBirb: " into the stream
    itself, so the marked transcript read "> CoBirb: hello"."""
    class _StreamingOrchestrator(_StubOrchestrator):
        def run(self, prompt, system, **kwargs):
            begin = getattr(self.io, "begin_stream", None)
            if callable(begin):
                begin("CoBirb")
            self.io.render("Hello, how are you today?")
            self.io.render("\n")
            self.last_turn_streamed = True
            return SimpleNamespace(summary="Hello, how are you today?")

    orchestrator = _StreamingOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "Hi")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        text = _transcript_text(app)
        assert "CoBirb:" not in text
        assert (
            _marker_colour_name(render.ASSISTANT_MARKER_STYLE),
            "Hello, how are you today?",
        ) in _marker_colours(app)


async def test_the_tui_draws_nothing_for_begin_stream(monkeypatch):
    """Its streaming preview re-renders the whole buffer with the marker on
    every token, so a marker drawn here would end up inside the text."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.io_bridge.begin_stream("CoBirb")
        app.io_bridge.render("hello")
        await pilot.pause()

        assert app.query_one("#streaming-preview", StreamPreview).buffered == "hello"


# --------------------------------------------------------------------------- #
# Resuming a session shows the conversation you are rejoining.
#
# It used to leave the transcript blank: the turns were loaded and fed to the
# model, so it knew what had been said, but none of it was on screen.
# --------------------------------------------------------------------------- #
def _session_with_turns(*turns) -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(turns=list(turns), persona="none"))


def _turn(role, content, tool_use=None, phase=None) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, tool_use=tool_use, phase=phase)


def _resumed_app(*turns, **overrides) -> CoBirbApp:
    """An app handed an already-open session, the way ``cli._run_tui`` hands
    one over after unlocking the file before the app is allowed to start."""
    app = _make_app(session_path=overrides.pop("path", "/tmp/s.json"), password="pw", **overrides)
    orchestrator = _StubOrchestrator()
    orchestrator.session = _session_with_turns(*turns)
    app.orchestrator = orchestrator
    return app


async def test_resuming_replays_the_conversation_into_the_transcript():
    app = _resumed_app(
        _turn("user", "What is two plus two?"),
        _turn("assistant", "Four."),
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        text = _transcript_text(app)
        assert "Four." in text
        assert "What is two plus two?" in text
        assert "earlier turn(s)" in text
        assert "end of restored history" in text


async def test_replayed_prompts_and_replies_keep_their_markers():
    app = _resumed_app(_turn("user", "Hi"), _turn("assistant", "Hello."))
    async with app.run_test() as pilot:
        await pilot.pause()

        assert _marker_colours(app) == [
            (_marker_colour_name(render.USER_MARKER_STYLE), "Hi"),
            (_marker_colour_name(render.ASSISTANT_MARKER_STYLE), "Hello."),
        ]


async def test_a_replayed_tool_call_shows_the_call_and_its_output():
    call = [{"name": "read_file", "arguments": {"path": "a.txt"}}]
    app = _resumed_app(
        _turn("user", "read it"),
        # The assistant turn that only announces a call carries no prose.
        _turn("assistant", "", tool_use=call),
        _turn("tool", "file contents", tool_use=call),
        _turn("assistant", "It says hello."),
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        text = _transcript_text(app)
        assert "tool: read_file" in text
        assert "file contents" in text
        # The empty announce-the-call turn must not leave a bare marker
        # floating between the prompt and the tool panel.
        assert [body for _, body in _marker_colours(app)] == ["read it", "It says hello."]


async def test_a_brand_new_session_is_not_announced_as_restored(tmp_path):
    """--session pointing at a path that doesn't exist yet is a *new*
    session: nothing was opened, so there is no history and no rule to draw."""
    app = _make_app(session_path=str(tmp_path / "not-yet.json"), password="pw")
    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.orchestrator is None  # nothing was handed over
        assert "restored history" not in _transcript_text(app)


async def test_resuming_from_the_sessions_tab_replays_and_returns_to_the_conversation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    manager = _session_with_turns(_turn("user", "earlier question"), _turn("assistant", "earlier answer"))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._apply_resumed_session(str(tmp_path / "s.json"), "pw", manager)
        await pilot.pause()

        text = _transcript_text(app)
        assert "earlier question" in text and "earlier answer" in text
        # Switching conversations clears the old one rather than stacking them.
        assert "CoBirb ready." not in text
        assert app.query_one(TabbedContent).active == "current"


async def test_resuming_an_empty_session_says_so_instead_of_drawing_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._apply_resumed_session(str(tmp_path / "s.json"), "pw", _session_with_turns())
        await pilot.pause()

        text = _transcript_text(app)
        assert "no turns yet" in text
        assert "end of restored history" not in text


async def test_an_unknown_slash_command_goes_to_the_model(monkeypatch):
    """`/deploy the thing` is far more likely to be prose than a typo'd
    command, so it is sent rather than swallowed."""
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/deploy the thing")
        await _until(pilot, lambda: bool(orchestrator.calls))

        assert orchestrator.calls[0]["prompt"] == "/deploy the thing"


async def test_a_command_prefix_is_matched_as_a_whole_word(monkeypatch):
    """The old chain used startswith, so "/planned" parsed as /plan with the
    argument "ned" and silently reported plan mode instead of being sent."""
    orchestrator = _StubOrchestrator()
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(orchestrator))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/planned obsolescence")
        await _until(pilot, lambda: bool(orchestrator.calls))

        assert orchestrator.calls[0]["prompt"] == "/planned obsolescence"


# --------------------------------------------------------------------------- #
# The multi-line prompt box.
#
# It was an `Input`: one line, scrolling sideways for ever, which made
# composing a paragraph — a flock objective, say — genuinely hard. Becoming a
# `TextArea` took back both arrow keys and `enter`, so most of what is tested
# here is that those still mean what they meant.
# --------------------------------------------------------------------------- #
async def test_the_box_grows_with_wrapped_text_and_stops_at_eight_lines():
    """Wrapped lines, not newlines: one long paragraph should grow the box,
    which is the case that made a single-line field painful."""
    app = _make_app()
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)

        assert box.region.height == 1
        box.text = "x" * 250
        await pilot.pause()
        await pilot.pause()
        grew = box.region.height

        box.text = "\n".join(f"line {n}" for n in range(30))
        await pilot.pause()
        await pilot.pause()

        assert 1 < grew <= PromptInput.MAX_LINES
        assert box.region.height == PromptInput.MAX_LINES  # then it scrolls


async def test_the_box_shrinks_back_when_it_is_cleared():
    app = _make_app()
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.text = "a\nb\nc"
        await pilot.pause()
        await pilot.pause()

        box.clear()
        await pilot.pause()

        assert box.region.height == 1


async def test_enter_submits_rather_than_inserting_a_newline(monkeypatch):
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.focus()
        for character in "hello":
            await pilot.press(character)

        await pilot.press("enter")
        await _until(pilot, lambda: not box.disabled)

        assert box.text == ""
        assert box.prompt_history.entries == ["hello"]


@pytest.mark.parametrize("key", PromptInput.NEWLINE_KEYS)
async def test_every_newline_key_inserts_a_newline(key):
    """Three keys for one action. shift+enter is the one people reach for and
    the one that cannot be relied on — many terminals send it identically to
    enter — so there has to be another way to type a second line."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.focus()

        await pilot.press("a")
        await pilot.press(key)
        await pilot.press("b")
        await pilot.pause()

        assert box.text == "a\nb"


async def test_a_blank_box_submits_nothing():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.focus()
        box.text = "   \n  "

        await pilot.press("enter")
        await pilot.pause()

        assert box.prompt_history.entries == []


async def test_up_moves_the_cursor_when_there_is_a_line_above_it():
    """The shell convention: recall from the edges, move in the middle. Doing
    it the other way would make a multi-line prompt uneditable."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.prompt_history.add("an older prompt")
        box.focus()
        box.text = "one\ntwo"
        box.move_cursor(box.document.end)
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        assert box.text == "one\ntwo"  # not recalled
        assert box.cursor_location[0] == 0  # moved


async def test_up_recalls_once_the_cursor_is_on_the_first_line():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.prompt_history.add("an older prompt")
        box.focus()
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        assert box.text == "an older prompt"


async def test_down_moves_the_cursor_when_there_is_a_line_below_it():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)
        box.focus()
        box.text = "one\ntwo"
        box.move_cursor((0, 0))
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()

        assert box.cursor_location[0] == 1
        assert box.text == "one\ntwo"


async def test_the_undo_stack_is_not_shadowed_by_the_prompt_history():
    """`TextArea` already owns an attribute called `history` for its undo
    stack. Shadowing it made simply focusing the widget raise — a collision
    worth a test, because the failure lands nowhere near the assignment."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-input", PromptInput)

        box.focus()
        await pilot.pause()

        assert box.has_focus
        assert hasattr(box.history, "checkpoint")  # TextArea's, intact
        assert isinstance(box.prompt_history, PromptHistory)  # ours, alongside


# --------------------------------------------------------------------------- #
# Memory catalogues
# --------------------------------------------------------------------------- #
def test_ordered_catalogue_options_puts_loaded_ones_first_with_a_separator():
    rows = [
        memory.CatalogueFile(path="/a", name="zeta", encrypted=False),
        memory.CatalogueFile(path="/b", name="alpha", encrypted=True),
        memory.CatalogueFile(path="/c", name="beta", encrypted=False),
    ]
    loaded = {"beta": object()}
    options = _ordered_catalogue_options(rows, loaded)
    ids = [o.id for o in options]
    assert ids == ["beta", "__separator__", "alpha", "zeta"]
    assert options[1].disabled


def test_ordered_catalogue_options_has_no_separator_when_nothing_is_loaded():
    rows = [memory.CatalogueFile(path="/a", name="alpha", encrypted=False)]
    options = _ordered_catalogue_options(rows, {})
    assert [o.id for o in options] == ["alpha"]


async def test_remember_with_no_argument_shows_usage_and_opens_nothing():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/remember")
        assert "Usage: /remember" in _transcript_text(app)
        assert not isinstance(app.screen, RememberModal)


async def test_remember_opens_the_picker_with_the_fact_text():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/remember I prefer tabs over spaces")
        await _until(pilot, lambda: isinstance(app.screen, RememberModal))
        assert "I prefer tabs over spaces" in _static_text(app, "#remember-fact")


async def test_remembering_into_an_unencrypted_catalogue_writes_the_fact():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        memory.create(paths.memories_dir(), "work", AesGcmScryptSessionCrypto(), None)

        await _submit(pilot, app, "/remember keep commits short")
        await _until(pilot, lambda: isinstance(app.screen, RememberModal))

        options = app.screen.query_one("#remember-options", OptionList)
        options.highlighted = next(
            i for i in range(options.option_count) if options.get_option_at_index(i).id == "work"
        )
        options.action_select()
        await _until(pilot, lambda: not isinstance(app.screen, RememberModal))

        assert app.loaded_catalogues["work"].facts == ["keep commits short"]
        reloaded = memory.load(app.loaded_catalogues["work"].path, AesGcmScryptSessionCrypto())
        assert reloaded.facts == ["keep commits short"]


async def test_remembering_into_a_locked_catalogue_prompts_for_a_password_first():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        memory.create(paths.memories_dir(), "secret", AesGcmScryptSessionCrypto(), "hunter2")

        await _submit(pilot, app, "/remember a private fact")
        await _until(pilot, lambda: isinstance(app.screen, RememberModal))

        options = app.screen.query_one("#remember-options", OptionList)
        options.highlighted = next(
            i for i in range(options.option_count) if options.get_option_at_index(i).id == "secret"
        )
        options.action_select()
        await _until(pilot, lambda: isinstance(app.screen, TextPromptModal))

        app.screen.query_one("#prompt-value", Input).value = "hunter2"
        await pilot.press("enter")
        await _until(pilot, lambda: not isinstance(app.screen, RememberModal) and "secret" in app.loaded_catalogues)

        assert app.loaded_catalogues["secret"].facts == ["a private fact"]


async def test_memories_command_opens_the_catalogues_modal():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/memories")
        await _until(pilot, lambda: isinstance(app.screen, MemoryCataloguesModal))


async def test_memories_new_creates_an_unencrypted_catalogue_on_blank_passwords():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/memories")
        await _until(pilot, lambda: isinstance(app.screen, MemoryCataloguesModal))

        await pilot.click("#memory-new")
        await _until(pilot, lambda: isinstance(app.screen, NewCatalogueModal))
        app.screen.query_one("#new-catalogue-name", Input).value = "work"
        await pilot.click("#new-catalogue-create")
        await _until(pilot, lambda: isinstance(app.screen, MemoryCataloguesModal))

        assert "work" in app.loaded_catalogues
        assert app.loaded_catalogues["work"].encrypted is False


async def test_memories_new_rejects_mismatched_passwords_without_creating_anything():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/memories")
        await _until(pilot, lambda: isinstance(app.screen, MemoryCataloguesModal))

        await pilot.click("#memory-new")
        await _until(pilot, lambda: isinstance(app.screen, NewCatalogueModal))
        app.screen.query_one("#new-catalogue-name", Input).value = "personal"
        app.screen.query_one("#new-catalogue-password", Input).value = "one"
        app.screen.query_one("#new-catalogue-confirm", Input).value = "two"
        await pilot.click("#new-catalogue-create")
        await pilot.pause()

        assert isinstance(app.screen, NewCatalogueModal)  # still open, not dismissed
        assert "match" in _static_text(app, "#new-catalogue-error")
        assert memory.discover_catalogues(paths.memories_dir()) == []


async def test_memories_selecting_a_loaded_row_unloads_it():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        cat = memory.create(paths.memories_dir(), "work", AesGcmScryptSessionCrypto(), None)
        app.loaded_catalogues["work"] = cat

        await _submit(pilot, app, "/memories")
        await _until(pilot, lambda: isinstance(app.screen, MemoryCataloguesModal))
        options = app.screen.query_one("#memory-options", OptionList)
        options.highlighted = next(
            i for i in range(options.option_count) if options.get_option_at_index(i).id == "work"
        )
        options.action_select()
        await pilot.pause()

        assert "work" not in app.loaded_catalogues


async def test_a_loaded_catalogue_reaches_the_system_prompt_but_is_dropped_when_empty(monkeypatch):
    builds = []
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(record=builds))

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        cat = memory.MemoryCatalogue(name="work", path="/tmp/work.md", encrypted=False, facts=["fact one"])
        app.loaded_catalogues["work"] = cat

        await _submit(pilot, app, "hello")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert "fact one" in builds[0]["built"].calls[0]["system"]
        assert "work" in builds[0]["built"].calls[0]["system"]


# --------------------------------------------------------------------------- #
# /image attachments
# --------------------------------------------------------------------------- #
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 20


async def test_image_with_no_argument_shows_usage():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/image")
        assert "Usage: /image" in _transcript_text(app)


async def test_image_rejects_a_file_that_is_not_an_image(tmp_path):
    not_an_image = tmp_path / "notes.txt"
    not_an_image.write_text("just some text")

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, f"/image {not_an_image}")
        assert "doesn't look like an image" in _transcript_text(app)
        assert app._pending_images == []


async def test_image_reports_a_missing_file():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/image /no/such/file.png")
        assert "Could not read" in _transcript_text(app)


async def test_a_queued_image_rides_the_next_submitted_prompt(tmp_path, monkeypatch):
    builds = []
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(record=builds))
    png = tmp_path / "shot.png"
    png.write_bytes(_PNG_BYTES)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, f"/image {png}")
        assert app._pending_images and app._pending_images[0]["filename"] == "shot.png"

        await _submit(pilot, app, "what is this")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        images = builds[0]["built"].calls[0]["images"]
        assert images[0]["filename"] == "shot.png"
        assert images[0]["data"]  # base64, non-empty
        assert app._pending_images == []  # consumed, not left queued for a later turn


async def test_the_transcript_shows_a_marker_for_a_queued_image(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())
    png = tmp_path / "shot.png"
    png.write_bytes(_PNG_BYTES)

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, f"/image {png}")
        await _submit(pilot, app, "what is this")
        await _until(pilot, lambda: "shot.png" in _transcript_text(app))


async def test_an_image_on_a_later_turn_works_without_a_session(monkeypatch):
    """The default configuration: no --session. The orchestrator manufactures
    a SessionManager of its own after turn 1, and that one has no crypto
    backend — so attaching an image on turn 2 used to raise mid-turn
    ('NoneType' object has no attribute 'encrypt'), swallow the user's
    message, and leave a stray '..images' directory in the working tree."""
    builds = []
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build(record=builds))

    app = _make_app(session_path=None, password=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, "first, no image")
        await _until(pilot, lambda: bool(builds))
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        import tempfile, pathlib
        png = pathlib.Path(tempfile.mkdtemp()) / "shot.png"
        png.write_bytes(_PNG_BYTES)

        await _submit(pilot, app, f"/image {png}")
        await _submit(pilot, app, "second, with image")
        await _until(pilot, lambda: len(builds[0]["built"].calls) == 2)
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

        assert "could not complete" not in _transcript_text(app)
        assert builds[0]["built"].calls[1]["images"][0]["filename"] == "shot.png"


async def test_attaching_an_image_writes_nothing_beside_the_session(tmp_path, monkeypatch):
    """Bytes belong in the session blob. Nothing is written next to it, and
    above all nothing is written into the working tree."""
    monkeypatch.setattr(wiring, "build_orchestrator", _stub_build())
    png = tmp_path / "shot.png"
    png.write_bytes(_PNG_BYTES)
    before = set(os.listdir(tmp_path))

    app = _make_app(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, app, f"/image {png}")
        await _submit(pilot, app, "what is this")
        await _until(pilot, lambda: not app.query_one("#prompt-input", PromptInput).disabled)

    assert set(os.listdir(tmp_path)) == before
