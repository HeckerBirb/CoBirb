"""``CoBirbApp`` — interactive mode as a full-screen Textual application.

Interactive mode used to be a scrolling ``input()`` loop. It is now a real
terminal app: a persistent tab bar, a header, a live status footer, a boxed
input, a transcript of Rich panels, and tool approval as a modal instead of a
``[y]es / [a]lways / [N]o:`` line.

What it is *not* is a second copy of the CLI. Every bit of wiring one-shot
mode does — ``cli._build_orchestrator``, ``cli._load_persona``,
``cli._build_system_prompt``, ``cli._apply_persona_switch``,
``cli._apply_plan_toggle`` — is reused verbatim here, and ``Orchestrator``
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

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Header, Input, RichLog, TabbedContent, TabPane

from .. import cli, session
from ..config import Config
from ..orchestrator import Orchestrator
from ..plugins.core import render
from ..policy import PermissionError
from ..typing import spi as cobirb_typing
from .io_bridge import TuiIO
from .panes import PluginsPane, SessionsPane
from .screens import ApprovalModal, HelpModal, ModelPickerModal, TextPromptModal
from .widgets import StatusBar, StreamPreview

_TAB_ORDER = ["current", "sessions", "plugins"]


class CoBirbApp(App[None]):
    """The interactive CoBirb app."""

    CSS_PATH = "app.tcss"
    TITLE = "CoBirb"

    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "next_tab", "Next tab"),
        Binding("ctrl+q", "quit", "Quit"),
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
        self.io_bridge = TuiIO(self)
        # Built lazily on the first real turn (never for a slash-command-only
        # session) and reused for every turn after that: reusing the same
        # Policy object is what lets an "always allow this" approval persist
        # for the rest of the session instead of each turn forgetting what
        # was approved during the previous one.
        self.orchestrator: Orchestrator | None = None
        # Resolved once, for the status bar — the orchestrator that would
        # know the real name doesn't exist yet at mount time.
        self.resolved_model_name = cli._resolve_model_name(model_name, cwd)
        self.ui_thread_id = threading.get_ident()

    # ------------------------------------------------------------------ #
    # Composition
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial="current"):
            with TabPane("Current", id="current"):
                yield RichLog(id="transcript", markup=False, highlight=False, wrap=True)
                yield StreamPreview(id="streaming-preview")
                with Container(id="prompt-box"):
                    yield Input(id="prompt-input", placeholder="Message CoBirb…")
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
        greeting = self.persona.greeting or f"{self.persona.name} is here."
        self.write_transcript(
            render.build_notice(
                f"{self.persona.name}: {greeting}"
                if self.persona.greeting
                else greeting
            )
        )
        self.write_transcript(
            render.build_notice(
                "/model picks a model · /persona <name> switches personas · "
                "/plan on|off toggles plan mode · ? or /help for help"
            )
        )
        self.query_one("#prompt-input", Input).focus()

        self.refresh_plugins_pane()
        self.refresh_sessions_pane()
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
        self.query_one("#transcript", RichLog).write(renderable)

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
            self.query_one("#transcript", RichLog).write(text.rstrip("\n"))

    def set_busy(self, label: str) -> None:
        self.query_one(StatusBar).busy = label

    async def request_approval(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Show the approval modal and resolve to the user's decision.

        Awaited on the event loop on behalf of a worker thread (see
        ``TuiIO.confirm``). ``push_screen_wait`` requires an active worker
        context, which is exactly what that caller has — so this must not be
        called from anywhere else.
        """
        return await self.push_screen_wait(ApprovalModal(tool_name, arguments))

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt:
            return

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

        if prompt.startswith("/persona"):
            self.persona, self.system, message = cli._apply_persona_switch(
                prompt[len("/persona"):].strip(), self.persona, self.system
            )
            self.query_one(StatusBar).persona_name = self.persona.name
            self.write_transcript(render.build_notice(message))
            return

        if prompt.startswith("/plan"):
            self.plan_mode, message = cli._apply_plan_toggle(prompt[len("/plan"):], self.plan_mode)
            self.query_one(StatusBar).plan_mode = self.plan_mode
            self.write_transcript(render.build_notice(message))
            return

        self.write_transcript(render.build_notice(f"You: {prompt}"))
        # Disabled here, on the main thread, and re-enabled in
        # _on_turn_finished — this is what replaces the old loop's
        # "continue? [y/N]" gate. There is nothing to confirm: when the box
        # comes back, you just keep typing.
        event.input.disabled = True
        self._run_turn(prompt)

    def _on_turn_finished(self) -> None:
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
                self.orchestrator, _, _ = cli._build_orchestrator(
                    self.cwd,
                    self.persona,
                    self.allow_overrides,
                    self.system,
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

        Deliberately not ``cli._render_final_answer``: that one falls back to
        a bare ``print()`` for an adapter with no ``render_answer`` hook,
        which in a full-screen app would paint a line straight over the
        layout. Here the fallback is the TUI's own bridge, so the answer
        always lands in the transcript — even if a ``plugins.io`` selection
        put some other adapter in the orchestrator's I/O slot.
        """
        render_answer = getattr(getattr(self.orchestrator, "io", None), "render_answer", None)
        if callable(render_answer):
            render_answer(self.persona.name, summary or "Completed.")
        else:
            self.io_bridge.render_answer(self.persona.name, summary or "Completed.")

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
                models = cli._build_model(None, self.cwd).list_models()
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
        self.resolved_model_name = cli._resolve_model_name(name, self.cwd)
        self.query_one(StatusBar).model_name = self.resolved_model_name
        # A model provider plugin swap is a config-time decision (plugins.model);
        # /model only ever manages the core, OpenAI-compatible provider, so it's
        # safe to always rebuild that provider and hand it to an orchestrator
        # that already exists, rather than rebuilding the whole thing (which
        # would also discard any "always allow" approvals from this session).
        if self.orchestrator is not None:
            self.orchestrator.model = cli._build_model(name, self.cwd)
        self.write_transcript(render.build_notice(f"Model set to {name}."))

    # ------------------------------------------------------------------ #
    # Plugins tab
    # ------------------------------------------------------------------ #
    def refresh_plugins_pane(self) -> None:
        self._describe_plugins_worker()

    @work(thread=True, exclusive=True, group="plugins")
    def _describe_plugins_worker(self) -> None:
        try:
            summary = cli.describe_plugins(self.cwd)
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
            _, discovered, _ = cli._discover_plugins(self.cwd, config)
            crypto, _ = cli._build_crypto(config, discovered)
            manager = session.SessionManager.load(path, crypto, password, self.cwd, self.persona.name)
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
        self.persona = cli._load_persona(manager.session.persona)
        self.system = cli._build_system_prompt(self.persona)
        status = self.query_one(StatusBar)
        status.persona_name = self.persona.name
        status.session_path = path
        turns = len(manager.session.turns)
        self.query_one(SessionsPane).set_status(
            f"Resumed '{os.path.basename(path)}' — {turns} turn(s), persona {self.persona.name}. "
            "Your next message continues it."
        )
        self.refresh_sessions_pane()

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
        text = cli._HELP_TOPICS.get(topic, cli._HELP_TEXT) if topic else cli._HELP_TEXT
        if topic and topic not in cli._HELP_TOPICS:
            text = (
                f"No help topic '{topic}'. Available: {', '.join(sorted(cli._HELP_TOPICS))}\n\n"
                + cli._HELP_TEXT
            )
        self.push_screen(HelpModal(text))

    def action_next_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        index = _TAB_ORDER.index(tabs.active) if tabs.active in _TAB_ORDER else -1
        tabs.active = _TAB_ORDER[(index + 1) % len(_TAB_ORDER)]
