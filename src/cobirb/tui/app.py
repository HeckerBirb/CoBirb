"""``CoBirbApp`` — interactive mode as a full-screen Textual application.

Interactive mode used to be a scrolling ``input()`` loop. It is now a real
terminal app: a persistent tab bar, a header, a live status footer, a boxed
input, a transcript of Rich panels, and tool approval as a modal instead of a
``[y]es / [a]lways / [N]o:`` line.

What it is *not* is a second copy of the CLI. Every bit of wiring one-shot
mode does — ``wiring.build_orchestrator``, ``personas.load_persona``,
``personas.build_system_prompt``, ``commands.apply_persona_switch``,
``commands.apply_plan_toggle`` — is reused verbatim here, and ``Orchestrator``
itself is untouched: it stays a synchronous blocking call, driven from a
Textual thread worker and bridged back to the UI thread by ``TuiIO``.

The ``Sessions`` and ``Plugins`` tabs (``tui/panes.py``) are live views, not
placeholders: Sessions lists and can resume/start encrypted session files,
Plugins shows exactly what got discovered/registered and any problems with
it — the same information ``_build_orchestrator`` itself uses, surfaced
where a full-screen app's user can actually see it (unlike the plugin
discovery warnings ``_build_orchestrator`` prints to stderr for the CLI
modes, which a full-screen app's stderr is invisible to).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Header, Input, RichLog, TabbedContent, TabPane

from .. import session
from ..help_text import HELP_TEXT, HELP_TOPICS
from ..runtime import commands, personas, plugins, wiring
from ..config import Config
from ..orchestrator import Orchestrator, render_through
from ..plugins.core import persona_shapes_voice, render
from ..policy import PermissionError
from ..typing import spi as cobirb_typing
from .io_bridge import TuiIO
from .panes import PluginsPane, SessionsPane
from .screens import (
    ApprovalModal,
    HelpModal,
    ModelPickerModal,
    PersonaPickerModal,
    TextPromptModal,
)
from .widgets import PromptInput, StatusBar, StreamPreview, TranscriptLog

_TAB_ORDER = ["current", "sessions", "plugins"]


def _session_turns(orchestrator: Any) -> list[Any]:
    """The saved turns an orchestrator's session holds, or ``[]``.

    ``orchestrator.session`` is a ``SessionManager``; the ``Session`` it
    wraps is one level further in, and either can legitimately be ``None``
    (no session, or a manager that never opened one). Defensive rather than
    chained attribute access because this also runs against the stub
    orchestrators the tests inject.
    """
    manager = getattr(orchestrator, "session", None)
    session_data = getattr(manager, "session", None)
    return list(getattr(session_data, "turns", None) or [])


class CoBirbApp(App[None]):
    """The interactive CoBirb app."""

    CSS_PATH = "app.tcss"
    TITLE = "CoBirb"

    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "next_tab", "Next tab"),
        Binding("ctrl+q", "quit", "Quit"),
        # priority=True: fires even while the (disabled, mid-turn) prompt
        # input nominally holds focus — Ctrl+C should always be able to
        # break out of a stuck turn, not just when something else happens
        # to have focus. Textual's own default Ctrl+C binding
        # (action_help_quit, "press ctrl+q to quit") only fires when there
        # is nothing to cancel — see action_cancel_turn, which also has to
        # hand Ctrl+C back to "copy the selection" itself, since claiming
        # the key at priority takes it away from Screen's own copy binding.
        Binding("ctrl+c", "cancel_turn", "Copy/Cancel", show=True, priority=True),
    ]

    def __init__(
        self,
        persona: cobirb_typing.Persona,
        system: str,
        allow_overrides: dict[str, str],
        session_path: str | None,
        password: str | None,
        cwd: str,
        model_name: str | None = None,
        plan_mode: bool = False,
        harness: bool = False,
    ) -> None:
        super().__init__()
        self.persona = persona
        self.system = system
        self.allow_overrides = allow_overrides
        self.session_path = session_path
        self.password = password
        self.cwd = cwd
        self.model_name = model_name
        self.plan_mode = plan_mode
        # Whether CoBirb contributes its own harness block to the system
        # prompt (--system-prompt harness). Kept because every later rebuild
        # of `self.system` — a /persona switch, resuming a session — has to
        # make the same choice this run started with.
        self.harness = harness
        self.io_bridge = TuiIO(self)
        # Built lazily on the first real turn (never for a slash-command-only
        # session) and reused for every turn after that: reusing the same
        # Policy object is what lets an "always allow this" approval persist
        # for the rest of the session instead of each turn forgetting what
        # was approved during the previous one.
        self.orchestrator: Orchestrator | None = None
        # Resolved once, for the status bar — the orchestrator that would
        # know the real name doesn't exist yet at mount time.
        self.resolved_model_name = wiring.resolve_model_name(model_name, cwd)
        self.ui_thread_id = threading.get_ident()
        # True for exactly the span between submitting a prompt and
        # _on_turn_finished — see action_cancel_turn and action_quit, which
        # both need to know whether there's a turn worth cancelling.
        self._turn_in_progress = False

    # ------------------------------------------------------------------ #
    # Composition
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial="current"):
            with TabPane("Current", id="current"):
                # TranscriptLog, not a plain RichLog: a stock one can't be
                # selected or copied out of at all (see its docstring).
                # action_cancel_turn is the Ctrl+C half of the same fix.
                yield TranscriptLog(id="transcript", markup=False, highlight=False, wrap=True)
                yield StreamPreview(id="streaming-preview")
                with Container(id="prompt-box"):
                    yield PromptInput(id="prompt-input", placeholder="Message CoBirb…")
            with TabPane("Sessions", id="sessions"):
                yield SessionsPane()
            with TabPane("Plugins", id="plugins"):
                yield PluginsPane()
        # Both of these belong at the bottom of the screen, but two widgets
        # docked to the same edge land on the same row and paint over each
        # other — so they share one docked, two-row container instead: the
        # live status line, and Textual's key-binding bar under it.
        with Container(id="footer-bar"):
            yield StatusBar(id="status-bar")
            yield Footer()

    def on_mount(self) -> None:
        # Re-read here rather than trusting __init__: this runs on the thread
        # the event loop actually lives on, which is the one TuiIO must
        # compare against to decide whether it needs to bridge at all.
        self.ui_thread_id = threading.get_ident()

        status = self.query_one(StatusBar)
        status.persona_name = self.persona.name
        status.model_name = self.resolved_model_name
        status.plan_mode = self.plan_mode
        status.cwd = self.cwd
        status.session_path = self.session_path

        self.query_one("#streaming-preview", StreamPreview).display = False

        self.io_bridge.render_header(
            self.persona.name, self.resolved_model_name, self.cwd, self.session_path
        )
        # A persona greets in character; with no persona (the default) there
        # is no character to greet as, so this stays a plain ready line
        # rather than inventing a voice the user didn't ask for.
        self.write_transcript(
            render.build_notice(
                f"{self.persona.name}: {self.persona.greeting}"
                if self.persona.greeting
                else f"{self.persona.name} ready."
            )
        )
        self.write_transcript(
            render.build_notice(
                "/model picks a model · /persona picks a persona · "
                "/plan on|off toggles plan mode · ? or /help for help"
            )
        )
        self.query_one("#prompt-input", Input).focus()

        self.refresh_plugins_pane()
        self.refresh_sessions_pane()
        # A resumed session arrives already open: `_run_tui` unlocks the file
        # before the app is allowed to start, precisely so a wrong password
        # never gets this far (there would be no conversation to show and
        # nothing to interact with). So there is nothing to decrypt here —
        # only turns to draw.
        if self.orchestrator is not None and self.session_path is not None:
            self.render_history(
                _session_turns(self.orchestrator), os.path.basename(self.session_path)
            )
        # Validates whatever model got configured against the endpoint's
        # live list, and opens the /model picker itself if that didn't work
        # out — see _select_model_worker's docstring for the exact rules.
        self._select_model_worker(auto=True)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Keep the Sessions tab's listing current — cheap enough (a plain
        directory scan) to just redo every time it's opened, so a session
        saved from another run shows up without an explicit refresh click."""
        if event.pane.id == "sessions":
            self.refresh_sessions_pane()

    # ------------------------------------------------------------------ #
    # Main-thread UI operations. TuiIO reaches every one of these through
    # App.call_from_thread; none may be called from a worker thread directly.
    # ------------------------------------------------------------------ #
    def write_transcript(self, renderable: Any) -> None:
        """Append a finished renderable to the transcript.

        Flushes the streaming preview first so the reading order matches the
        order things happened: the model's streamed reasoning, and only then
        the panel for the tool call it led to.
        """
        self._flush_stream()
        self.query_one("#transcript", TranscriptLog).write(renderable)

    def render_history(self, turns: list[Any], label: str) -> None:
        """Replay a resumed conversation into the transcript.

        Resuming used to drop you into an empty screen: the conversation was
        loaded and fed to the model, so it knew what had been said, but you
        couldn't see any of it. Every comparable CLI shows the thread you are
        rejoining, and a session you can't read is most of the reason to keep
        one.

        Bracketed by dim rules so restored turns are never mistaken for
        something that just happened.
        """
        log = self.query_one("#transcript", TranscriptLog)
        if not turns:
            log.write(render.build_notice(f"{label} — no turns yet; your next message starts it."))
            return
        log.write(Text(""))
        log.write(render.build_history_divider(f"{label} · {len(turns)} earlier turn(s)"))
        for turn in turns:
            self._replay_turn(log, turn)
        log.write(Text(""))
        log.write(render.build_history_divider("end of restored history"))
        # No trailing blank: whatever comes next writes its own leading one
        # (see write_user_prompt), and two would open a gap.

    def _replay_turn(self, log: TranscriptLog, turn: Any) -> None:
        """Write one saved turn, matching how it looked when it happened."""
        role = getattr(turn, "role", "")
        content = getattr(turn, "content", "") or ""
        tool_use = getattr(turn, "tool_use", None)
        phase = getattr(turn, "phase", None)

        if role == "tool":
            # The tool turn records the result; the call that produced it is
            # on the turn itself, so one panel shows both.
            call = (tool_use or [{}])[0]
            log.write(
                render.build_tool_call_panel(
                    call.get("name", "?"), call.get("arguments", {}) or {}, content, replayed=True
                )
            )
            return

        if role == "user":
            log.write(Text(""))
            log.write(render.build_user_message(content))
            log.write(Text(""))
            return

        if not content.strip():
            # An assistant turn whose only purpose was to announce a tool
            # call — the call itself is rendered by the tool turn that
            # follows, so an empty bubble here would be noise.
            return
        if phase == "plan":
            log.write(render.build_plan_panel(self.persona.name, content))
        elif phase == "validate":
            log.write(render.build_validation_panel(self.persona.name, content))
        else:
            log.write(render.build_assistant_message(content))

    def write_user_prompt(self, prompt: str) -> None:
        """Append what the user just sent, fenced by blank lines.

        The blank line on each side is the point: without it a prompt sat
        flush against the panel above and the reply below, and the whole
        transcript read as one undifferentiated column. Spacing here rather
        than inside ``build_user_message`` keeps the renderable itself
        composable — ``TerminalIO`` spaces its own output with newlines.
        """
        self._flush_stream()
        log = self.query_one("#transcript", TranscriptLog)
        log.write(Text(""))
        log.write(render.build_user_message(prompt))
        log.write(Text(""))

    def append_stream(self, text: str) -> None:
        self.query_one("#streaming-preview", StreamPreview).append(text)

    def _flush_stream(self) -> None:
        """Move anything buffered mid-stream into the transcript for good.

        A streamed final answer is never re-rendered as a panel (the
        orchestrator sets ``last_turn_streamed`` precisely so it isn't shown
        twice), so if this didn't run the reply would vanish from the
        transcript when the preview cleared.
        """
        preview = self.query_one("#streaming-preview", StreamPreview)
        text = preview.take()
        if text.strip():
            # Marked the same way a non-streamed reply is, so the transcript
            # reads uniformly whether or not the model streamed it.
            self.query_one("#transcript", TranscriptLog).write(
                render.build_streamed_message(text.rstrip("\n"))
            )

    def set_busy(self, label: str) -> None:
        self.query_one(StatusBar).busy = label

    async def request_approval(
        self, tool_name: str, arguments: dict[str, Any], scope: str | None = None
    ) -> str:
        """Show the approval modal and resolve to the user's decision.

        ``scope`` describes what "always" would grant, so the dialog can say
        so (see ``ApprovalModal``). Awaited on the event loop on behalf of a
        worker thread (see ``TuiIO.confirm``). ``push_screen_wait`` requires
        an active worker context, which is exactly what that caller has — so
        this must not be called from anywhere else.
        """
        return await self.push_screen_wait(ApprovalModal(tool_name, arguments, scope))

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt:
            return

        # Before the slash-command branches below, not after: "/persona
        # kawaii" is exactly the kind of thing worth arrowing back to, and a
        # history that only remembered messages sent to the model would drop
        # every command the moment it ran.
        if isinstance(event.input, PromptInput):
            event.input.remember(prompt)

        if prompt == "?" or prompt == "/help" or prompt.startswith("/help "):
            topic = prompt[len("/help"):].strip()
            self.action_help(topic)
            return

        if prompt == "/model" or prompt.startswith("/model "):
            arg = prompt[len("/model"):].strip()
            if arg:
                self.write_transcript(render.build_notice("Usage: /model — lists available models to choose from."))
            else:
                self._select_model_worker(auto=False)
            return

        if prompt == "/persona" or prompt.startswith("/persona "):
            arg = prompt[len("/persona"):].strip()
            # Bare /persona opens the picker, exactly like bare /model;
            # /persona <name> still switches directly, so anything scripted
            # or recalled from history keeps working.
            if arg:
                self._apply_persona(arg)
            else:
                self.pick_persona()
            return

        if prompt.startswith("/plan"):
            self.plan_mode, message = commands.apply_plan_toggle(prompt[len("/plan"):], self.plan_mode)
            self.query_one(StatusBar).plan_mode = self.plan_mode
            self.write_transcript(render.build_notice(message))
            return

        self.write_user_prompt(prompt)
        # Disabled here, on the main thread, and re-enabled in
        # _on_turn_finished — this is what replaces the old loop's
        # "continue? [y/N]" gate. There is nothing to confirm: when the box
        # comes back, you just keep typing.
        event.input.disabled = True
        self._turn_in_progress = True
        self._run_turn(prompt)

    def _on_turn_finished(self) -> None:
        self._turn_in_progress = False
        self._flush_stream()
        self.set_busy("")
        prompt_input = self.query_one("#prompt-input", Input)
        prompt_input.disabled = False
        prompt_input.focus()

    # ------------------------------------------------------------------ #
    # The turn itself: a blocking Orchestrator.run() on a worker thread
    # ------------------------------------------------------------------ #
    @work(thread=True, exclusive=True, group="turn")
    def _run_turn(self, prompt: str) -> None:
        """Run one full turn off the event loop.

        ``Orchestrator.run()`` blocks — on the model, on tools, on the user
        answering an approval modal — so it cannot run on the thread that
        has to keep repainting the UI and dismissing that very modal. Every
        callback it makes along the way goes back through ``TuiIO``.

        Nothing is allowed to escape this method. An exception out of a
        Textual worker becomes a ``WorkerFailed`` that tears the whole app
        down onto a crash screen — so a broken config or a provider falling
        over, which the scrolling loop reported and carried on from, is
        caught here and shown in the transcript the same way.
        """
        try:
            if self.orchestrator is None:
                self.orchestrator = wiring.build_orchestrator(
                    self.cwd,
                    self.persona,
                    self.allow_overrides,
                    self.session_path,
                    self.password,
                    self.model_name,
                    io_factory=lambda: self.io_bridge,
                )
            # Named `turn_result`, not `session`: this module also imports
            # `cobirb.session` (the Sessions-tab code below needs it), and a
            # same-named local here would shadow it for the rest of this
            # method.
            turn_result = self.orchestrator.run(
                prompt,
                self.system,
                cwd=self.cwd,
                persona=self.persona.name,
                session_path=self.session_path,
                plan_mode=self.plan_mode,
            )
            # A streamed final answer is already in the transcript; rendering
            # the summary too would just show it twice.
            if not self.orchestrator.last_turn_streamed:
                self._render_final_answer(turn_result.summary)
            if self.session_path is not None and self.orchestrator.session is not None:
                self.orchestrator.session.save(self.password)
        except PermissionError as exc:
            self.io_bridge.write_error(self.persona.name, f"blocked — {exc}")
        except Exception as exc:  # noqa: BLE001 - surface provider/tool errors cleanly
            self.io_bridge.write_error(self.persona.name, f"could not complete — {exc}")
        finally:
            # In a `finally` so nothing can leave the input box disabled with
            # no way to get it back.
            self.call_from_thread(self._on_turn_finished)

    def _render_final_answer(self, summary: str) -> None:
        """Show a finished, non-streamed reply.

        Deliberately not ``cli._render_final_answer``: the CLI's version falls back to
        a bare ``print()`` for an adapter with no ``render_answer`` hook,
        which in a full-screen app would paint a line straight over the
        layout. Here the fallback is the TUI's own bridge, so the answer
        always lands in the transcript — even if a ``plugins.io`` selection
        put some other adapter in the orchestrator's I/O slot.
        """
        answer = summary or "Completed."

        def into_the_transcript() -> None:
            self.io_bridge.render_answer(self.persona.name, answer)

        io_adapter = getattr(self.orchestrator, "io", None)
        if not render_through(
            io_adapter, "render_answer", self.persona.name, answer, fallback=into_the_transcript
        ):
            into_the_transcript()  # no adapter at all

    # ------------------------------------------------------------------ #
    # /model: list what the configured endpoint has, and pick one
    # ------------------------------------------------------------------ #
    @work(thread=True, exclusive=True, group="model")
    def _select_model_worker(self, *, auto: bool) -> None:
        """Fetch the available models and, if needed, let the user choose.

        Two callers, two different jobs:

        - ``auto=True`` (startup, ``on_mount``): *validate* whatever model
          got configured. If it's in the live list, nothing happens — no
          picker, no notice, this is the common case and should be silent.
          If the endpoint can't be reached at all, also do nothing when a
          model is already configured (best effort only; it may still work
          fine once a real turn actually needs it) — but if none was
          configured either, say so, since there's nothing else to fall
          back on. If a model *was* configured but isn't actually there
          (this is "default_model fails silently" from the user's side:
          no error, just quietly not used), note it and open the picker.
        - ``auto=False`` (``/model``): always fetch and always open the
          picker (or report why it couldn't), since the user explicitly
          asked.
        """
        self.call_from_thread(self.set_busy, "Fetching models…")
        self.call_from_thread(self._set_prompt_disabled, True)
        try:
            try:
                models = wiring.build_model(None, self.cwd).list_models()
            except Exception as exc:  # noqa: BLE001 - report, never crash the app over this
                if not auto or not self.model_name:
                    self.call_from_thread(
                        self.io_bridge.write_error,
                        self.persona.name,
                        f"could not list models — {exc}",
                    )
                return

            if auto and self.model_name and self.model_name in models:
                return  # the configured default resolved fine; nothing to do

            if not models:
                self.call_from_thread(
                    self.io_bridge.write_error, self.persona.name, "The model provider has no models available."
                )
                return

            if auto and self.model_name:
                self.call_from_thread(
                    self.write_transcript,
                    render.build_notice(f"Configured model '{self.model_name}' was not found there — pick one:"),
                )

            selected = self.call_from_thread(self.pick_model, models, self.model_name)
            if selected is not None:
                self.call_from_thread(self._apply_selected_model, selected)
        finally:
            self.call_from_thread(self.set_busy, "")
            self.call_from_thread(self._set_prompt_disabled, False)

    def _set_prompt_disabled(self, disabled: bool) -> None:
        prompt_input = self.query_one("#prompt-input", Input)
        prompt_input.disabled = disabled
        if not disabled:
            prompt_input.focus()

    async def pick_model(self, models: list[str], current: str | None) -> str | None:
        """Show the model picker and resolve to the chosen model, or
        ``None`` if cancelled. Awaited on the event loop on behalf of a
        worker thread, the same way ``request_approval`` is."""
        return await self.push_screen_wait(ModelPickerModal(models, current))

    def _apply_selected_model(self, name: str) -> None:
        self.model_name = name
        self.resolved_model_name = wiring.resolve_model_name(name, self.cwd)
        self.query_one(StatusBar).model_name = self.resolved_model_name
        # A model provider plugin swap is a config-time decision (plugins.model);
        # /model only ever manages the core, OpenAI-compatible provider, so it's
        # safe to always rebuild that provider and hand it to an orchestrator
        # that already exists, rather than rebuilding the whole thing (which
        # would also discard any "always allow" approvals from this session).
        if self.orchestrator is not None:
            self.orchestrator.model = wiring.build_model(name, self.cwd)
        self.write_transcript(render.build_notice(f"Model set to {name}."))

    # ------------------------------------------------------------------ #
    # /persona: pick one from a list, the same way /model does
    # ------------------------------------------------------------------ #
    def pick_persona(self) -> None:
        """Open the persona picker.

        Unlike the model picker this needs no worker thread: the choices are
        a directory listing of bundled personas, not a network round trip, so
        there is nothing to block on and ``push_screen`` with a callback is
        enough.
        """
        current = personas.NO_PERSONA if not persona_shapes_voice(self.persona) else None
        if current is None:
            # Match by the name the picker lists (the file/bundle name), not
            # the persona's display name — "kawaii" is the option, "Momo" or
            # whatever it calls itself is what the persona says it is.
            current = next(
                (
                    name
                    for name in personas.available_personas()
                    if personas.load_persona(name).name == self.persona.name
                ),
                None,
            )
        self.push_screen(
            PersonaPickerModal(personas.available_personas(), current), self._on_persona_picked
        )

    def _on_persona_picked(self, name: str | None) -> None:
        if name is not None:
            self._apply_persona(name)

    def _apply_persona(self, name: str) -> None:
        self.persona, self.system, message = commands.apply_persona_switch(
            name, self.persona, self.system, harness=self.harness
        )
        self.query_one(StatusBar).persona_name = self.persona.name
        self.write_transcript(render.build_notice(message))

    # ------------------------------------------------------------------ #
    # Plugins tab
    # ------------------------------------------------------------------ #
    def refresh_plugins_pane(self) -> None:
        self._describe_plugins_worker()

    @work(thread=True, exclusive=True, group="plugins")
    def _describe_plugins_worker(self) -> None:
        try:
            summary = plugins.describe_plugins(self.cwd)
        except Exception as exc:  # noqa: BLE001 - show it in the pane, don't crash the app
            self.call_from_thread(self.query_one(PluginsPane).render_error, str(exc))
            return
        self.call_from_thread(self.query_one(PluginsPane).render_summary, summary)

    # ------------------------------------------------------------------ #
    # Sessions tab
    # ------------------------------------------------------------------ #
    def refresh_sessions_pane(self) -> None:
        """Just a directory listing (name/size/modified) — cheap enough to
        run on the main thread rather than needing a worker."""
        self.query_one(SessionsPane).refresh_sessions(self.session_path)

    async def prompt_text(self, title: str, label: str, default: str = "", *, password: bool = False) -> str | None:
        """Show a text/password prompt and resolve to what was entered, or
        ``None`` if cancelled — the Sessions tab's only way to collect a
        session password or a new session's name, since a full-screen app
        can't fall back to ``getpass.getpass()``/``input()``."""
        return await self.push_screen_wait(TextPromptModal(title, label, default, password=password))

    def begin_resume_session(self, path: str) -> None:
        self._resume_session_worker(path)

    def begin_new_session(self) -> None:
        self._new_session_worker()

    @work(thread=True, exclusive=True, group="session")
    def _resume_session_worker(self, path: str) -> None:
        password = self.call_from_thread(
            self.prompt_text, "Resume session", f"Password for {os.path.basename(path)}:", password=True
        )
        if not password:
            return
        self.call_from_thread(self.query_one(SessionsPane).set_status, "Unlocking…")
        try:
            config = Config(cwd=self.cwd)
            _, discovered, _ = plugins.discover_plugins(self.cwd, config)
            crypto, _ = plugins.build_crypto(config, discovered)
            manager = session.SessionManager.load(
                path, crypto, password, self.cwd, personas.persona_key(self.persona)
            )
        except Exception as exc:  # noqa: BLE001 - wrong password/corruption is routine, not fatal
            self.call_from_thread(
                self.query_one(SessionsPane).set_status, f"Could not open that session — {exc}"
            )
            return
        self.call_from_thread(self._apply_resumed_session, path, password, manager)

    def _apply_resumed_session(self, path: str, password: str, manager: session.SessionManager) -> None:
        self.session_path = path
        self.password = password
        # Discarded rather than live-swapped: the next message rebuilds it
        # bound to this session via the exact same load-if-exists path
        # _build_orchestrator already uses for --session, so there is only
        # one code path that ever opens a session file, tested once.
        self.orchestrator = None
        self.persona = personas.load_persona(manager.session.persona)
        self.system = personas.build_system_prompt(self.persona, harness=self.harness)
        status = self.query_one(StatusBar)
        status.persona_name = self.persona.name
        status.session_path = path
        turns = list(manager.session.turns)

        # A different conversation is being opened, so the transcript of the
        # old one is cleared rather than having the resumed turns appended
        # under it — two threads in one scrollback with no boundary would be
        # worse than either alone. The manager here is already decrypted, so
        # the history is replayed straight from it.
        self.query_one("#transcript", TranscriptLog).clear()
        self.io_bridge.render_header(
            self.persona.name, self.resolved_model_name, self.cwd, path
        )
        self.render_history(turns, os.path.basename(path))

        self.query_one(SessionsPane).set_status(
            f"Resumed '{os.path.basename(path)}' — {len(turns)} turn(s), persona "
            f"{self.persona.name}. Your next message continues it."
        )
        self.refresh_sessions_pane()
        # Straight back to the conversation: resuming is a thing you do in
        # order to keep talking, and leaving the user on the Sessions tab
        # makes them go and find it.
        self.query_one(TabbedContent).active = "current"
        self.query_one("#prompt-input", Input).focus()

    @work(thread=True, exclusive=True, group="session")
    def _new_session_worker(self) -> None:
        default_name = f"session-{time.strftime('%Y%m%d-%H%M%S')}"
        name = self.call_from_thread(self.prompt_text, "New session", "Name this session:", default_name)
        if not name:
            return
        password = self.call_from_thread(
            self.prompt_text, "New session", "Choose a password for this session:", password=True
        )
        if not password:
            return
        path = os.path.join(session.default_sessions_dir(), f"{name}.json")
        if os.path.isfile(path):
            self.call_from_thread(
                self.query_one(SessionsPane).set_status, f"'{name}' already exists — pick a different name."
            )
            return
        self.call_from_thread(self._apply_new_session, path, password)

    def _apply_new_session(self, path: str, password: str) -> None:
        self.session_path = path
        self.password = password
        self.orchestrator = None
        self.query_one(StatusBar).session_path = path
        self.query_one(SessionsPane).set_status(
            f"'{os.path.basename(path)}' will be created on your next message."
        )
        self.refresh_sessions_pane()

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def action_help(self, topic: str = "") -> None:
        text = HELP_TOPICS.get(topic, HELP_TEXT) if topic else HELP_TEXT
        if topic and topic not in HELP_TOPICS:
            text = (
                f"No help topic '{topic}'. Available: {', '.join(sorted(HELP_TOPICS))}\n\n"
                + HELP_TEXT
            )
        self.push_screen(HelpModal(text))

    def action_next_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        index = _TAB_ORDER.index(tabs.active) if tabs.active in _TAB_ORDER else -1
        tabs.active = _TAB_ORDER[(index + 1) % len(_TAB_ORDER)]

    def action_cancel_turn(self) -> None:
        """Ctrl+C: copy the selection if there is one, else stop a stuck turn.

        Copy comes first, and has to be handled here, because this binding is
        ``priority=True``: that wins over every other Ctrl+C in the app,
        including ``Screen``'s own ``ctrl+c -> screen.copy_text``. Claiming
        the key for cancellation is what left the transcript impossible to
        copy out of — text could be selected with the mouse, but the key that
        copies it never reached Textual. Delegating here gives both meanings
        one unambiguous key, disambiguated by whether anything is selected.

        Cancelling matters because a long-running or hung ``shell`` call (a
        command that doesn't produce output until you interact with it, a
        server, a game loop — anything that doesn't exit on its own) would
        otherwise leave the input disabled with no way back short of waiting
        out its timeout (up to 5 minutes by default) or killing the whole app
        from outside.

        Falls back to Textual's own default Ctrl+C behavior (a "press
        ctrl+q to quit" toast) when there is nothing to copy and no turn is
        running — that's what this key did before it was bound to something
        more useful here.
        """
        if self.action_copy_selection():
            return
        if not self._turn_in_progress:
            self.action_help_quit()
            return
        if self._attempt_cancel():
            self.notify("Cancelling the running command…", title="Cancel")
        else:
            self.notify(
                "Still waiting on the model — there's no running command to stop yet.",
                title="Cancel",
            )

    def action_copy_selection(self) -> bool:
        """Copy whatever is selected in the transcript, if anything.

        Returns whether it copied, so ``action_cancel_turn`` can tell whether
        Ctrl+C has already been spoken for this press.

        ``copy_to_clipboard`` writes via the terminal's OSC 52 escape, which
        reaches the real system clipboard even across SSH, but not every
        terminal honours it — hence the explicit toast rather than copying
        silently, so a terminal that drops it is visibly distinguishable from
        nothing having been selected.
        """
        selection = self.screen.get_selected_text()
        if not selection:
            return False
        self.copy_to_clipboard(selection)
        self.screen.clear_selection()
        lines = len(selection.splitlines())
        self.notify(
            f"Copied {lines} line{'s' if lines != 1 else ''} to the clipboard.", title="Copy"
        )
        return True

    def _attempt_cancel(self) -> bool:
        """Try to unstick whatever the in-flight turn is currently blocked
        on. Only the ``shell`` tool is actually interruptible this way
        today — a stuck model call has no handle to interrupt from here,
        it just has a much shorter timeout of its own (120s) than a shell
        command's (300s default). Returns whether anything was stopped.
        """
        if self.orchestrator is None:
            return False
        shell_tool = self.orchestrator.tools.get("shell")
        cancel = getattr(shell_tool, "cancel_running", None)
        return callable(cancel) and cancel()

    async def action_quit(self) -> None:
        """Quit — but first try to unstick a running turn.

        Without this, quitting mid-turn (Ctrl+Q) blocks for however long
        that turn takes to finish on its own, because Textual/asyncio's own
        shutdown sequence waits for the worker thread to actually return —
        which, for a hung shell command, could be its full timeout. This is
        the same bug from the user's side either way (Ctrl+C or Ctrl+Q both
        looked like the whole app had frozen); this fixes it for both.
        """
        if self._turn_in_progress:
            self._attempt_cancel()
        await super().action_quit()
