"""``CoBirbApp`` — interactive mode as a full-screen Textual application.

Interactive mode is a real terminal app: a persistent tab bar, a live status
footer, a boxed input, a transcript of Rich panels, and tool approval as a
modal rather than a ``[y]es / [a]lways / [N]o:`` line.

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

import logging
import os
import threading
import time
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.theme import Theme
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .. import memory, session
from ..help_text import HELP_TEXT, HELP_TOPICS
from ..runtime import commands, personas, plugins, wiring
from ..runtime.catalogues import CatalogueStore
from . import slash_commands
from ..runtime import command_index, mentions
from .attachments import PendingAttachments
from .command_picker import CommandPicker
from .mention_picker import MentionPicker
from .transcript import TranscriptView
from ..runtime.custom_commands import expand_custom_command
from ..config import Config
from ..orchestrator import Orchestrator, render_through
from ..plugins.core import persona_shapes_voice, render
from ..policy import PermissionError
from ..typing import spi as cobirb_typing
from .io_bridge import TuiIO
from ..flock.brainy import PROPOSE_CHARTER
from ..flock.supervisor import Canceller
from .flock_bridge import TuiAsker, WorkerPaneIO
from .panes import FlockPane, PluginsPane, SessionsPane
from .screens import (
    ApprovalModal,
    ConfirmModal,
    HelpModal,
    ModelPickerModal,
    PersonaPickerModal,
    TextPromptModal,
)
from .widgets import ActivityBar, PromptInput, StatusBar, StreamPreview, TranscriptLog

logger = logging.getLogger("cobirb")

_TAB_ORDER = ["current", "flock", "sessions", "plugins"]

# "Noah" (v0.9.0) — the default theme, the parrot's own five colours
# (defined once in plugins.core.render, alongside the Rich styles that use
# the same palette for the transcript — see that module's own comment).
# Replaces Textual's stock theme (a blue/orange scheme) entirely, rather
# than tweaking pieces of it, which is why `primary` and `accent` both get
# the same value: this app has one accent, not two.
#
# Most of app.tcss never has to name a colour at all — it already reads
# through `$surface`/`$panel`/`$accent`/`$text-muted` (Textual's built-in
# Header/Footer/Tabs do the same internally), so registering this Theme is
# what actually repaints the title bar, the tab underline, the status bar
# and the footer's keybind letters. The three `variables` entries below are
# the only roles with no dedicated `Theme` field, overridden explicitly
# rather than left to their computed defaults (an alpha-blended "auto 60%"
# for muted text, `primary` for the tab underline, `foreground`-on-solid for
# the input cursor) so they land on the exact five hex values rather than
# something merely close.
NOAH_THEME = Theme(
    name="noah",
    dark=True,
    primary=render.TAIL_RED,
    accent=render.TAIL_RED,
    foreground=render.CREST_GRAY,
    background=render.BEAK_BLACK,
    surface=render.BEAK_BLACK,
    panel=render.WING_GRAY,
    error=render.TAIL_RED,
    variables={
        "text-muted": render.FEATHER_GRAY,
        "text-disabled": render.FEATHER_GRAY,
        # The active-tab underline (Tabs' `Underline` widget) and the
        # cursor block in list/table-style widgets both read this rather
        # than `primary` directly.
        "block-cursor-background": render.TAIL_RED,
        "input-cursor-background": render.TAIL_RED,
        "input-cursor-foreground": render.BEAK_BLACK,
        "footer-key-foreground": render.TAIL_RED,
    },
)


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
        self.register_theme(NOAH_THEME)
        self.theme = "noah"
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
        # Which memory catalogues are open, and everything that follows
        # from that (see runtime.catalogues). Not a widget's business, so
        # not a method of this class.
        self.catalogues = CatalogueStore(cwd)
        # Queued by /image, taken by the very next submitted prompt (see
        # _run_turn) — attach, then type your message normally, the way
        # attaching a file works everywhere.
        self.attachments = PendingAttachments()
        # How anything reaches the transcript, and in what order.
        self.transcript = TranscriptView(self)
        # How many tool calls this flock's planning phase has made, for the
        # progress line, and whether planning has finished.
        self._planning_calls = 0
        self.charter_in_hand = False
        # A charter `propose_charter` accepted outside a flock run, waiting
        # for the turn that proposed it to finish so it can be put to the user.
        # See note_proposed_charter.
        self._pending_charter: Any = None
        # Set for the duration of a flock engagement. The Event is what
        # ctrl+c sets: a model call in flight cannot be interrupted, so
        # stopping means no further Worker Birbs start.
        self._flock_stop: threading.Event | None = None
        # Force-stop handle for workers stuck mid-model-call — set alongside
        # _flock_stop for the duration of an engagement. The graceful Event
        # above only keeps new workers from starting; this drops the model
        # connections of the ones already running.
        self._flock_canceller: "Canceller | None" = None
        # Worker id -> state, for the activity line's roll-up. Reset per
        # engagement so a previous flock's workers do not linger in it.
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
                # Above the box, not below: the box is already at the bottom
                # of the screen, so a list under it would have nowhere to go.
                yield MentionPicker(
                    lambda: mentions.candidate_paths(self.cwd), id="mention-picker"
                )
                yield CommandPicker(
                    lambda: command_index.available_commands(
                        slash_commands.COMMANDS, self.cwd
                    ),
                    id="command-picker",
                )
                with Container(id="prompt-box"):
                    yield PromptInput(id="prompt-input")
            with TabPane("Flock", id="flock"):
                yield FlockPane()
            with TabPane("Sessions", id="sessions"):
                yield SessionsPane()
            with TabPane("Plugins", id="plugins"):
                yield PluginsPane()
        # Both of these belong at the bottom of the screen, but two widgets
        # docked to the same edge land on the same row and paint over each
        # other — so they share one docked, two-row container instead: the
        # live status line, and Textual's key-binding bar under it.
        with Container(id="footer-bar"):
            yield ActivityBar(id="activity-bar")
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
        prompt_input = self.query_one("#prompt-input", PromptInput)
        prompt_input.mention_picker = self.query_one("#mention-picker", MentionPicker)
        prompt_input.command_picker = self.query_one("#command-picker", CommandPicker)
        prompt_input.focus()

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
    # The transcript is a view of its own (tui/transcript.py). These stay
    # as forwarders because TuiIO, the panes and the flock bridge all write
    # through the app, and the ordering rule they rely on lives in the view.
    def write_transcript(self, renderable: Any) -> None:
        self.transcript.write(renderable)

    def append_stream(self, text: str) -> None:
        self.transcript.append_stream(text)

    def _flush_stream(self) -> None:
        self.transcript.flush_stream()

    def write_user_prompt(self, prompt: str) -> None:
        self.transcript.write_user_prompt(
            prompt, [item.filename for item in self.attachments.pending]
        )

    def render_history(self, turns: list[Any], label: str) -> None:
        self.transcript.render_history(turns, label, self.persona.name)

    def set_busy(self, label: str) -> None:
        """The orchestrator is waiting on the model.

        Drives the activity line as well as the status bar's suffix. The
        suffix alone was the original state of this and was reported as no
        indication at all — it competes with persona, model, plan mode and cwd
        on one dim row. During a flock the activity line is already showing
        the roll-up of who is doing what, which is more informative than one
        agent's "thinking", so that is left alone.
        """
        self.query_one(StatusBar).busy = label
        if self._flock_stop is None:
            self.set_activity(label)

    async def request_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: str | None = None,
        preview: str = "",
    ) -> str:
        """Show the approval modal and resolve to the user's decision.

        ``scope`` describes what "always" would grant, so the dialog can say
        so (see ``ApprovalModal``). Awaited on the event loop on behalf of a
        worker thread (see ``TuiIO.confirm``). ``push_screen_wait`` requires
        an active worker context, which is exactly what that caller has — so
        this must not be called from anywhere else.
        """
        return await self.push_screen_wait(ApprovalModal(tool_name, arguments, scope, preview))

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #
    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        """A prompt was submitted with enter.

        ``PromptInput`` is a ``TextArea`` now, which has no ``Submitted`` of
        its own — the widget posts one so this handler reads as it did when it
        was an ``Input``. See ``PromptInput`` for why enter had to be claimed
        rather than bound over.

        The box stays enabled through an ordinary turn now (mid-turn
        steering — see ``_steer_current_turn``), so this is also where a
        message submitted *while one is running* gets routed differently:
        not as a new prompt or a slash command, but as ``Orchestrator.steer()``
        redirecting the turn already in flight. A flock engagement still
        disables the box outright (``slash_commands.cmd_flock``) — steering a flock is a
        different, unbuilt question, not this one.
        """
        prompt = event.value.strip()
        event.input.clear()
        if not prompt:
            return

        # Before either branch below, not inside one: "/persona kawaii" is
        # exactly the kind of thing worth arrowing back to, and a history that
        # only remembered messages sent to the model would drop every command
        # the moment it ran. A steering message needs it for a second reason —
        # if the turn finishes in the moment between the keypress and this
        # handler, the box has already been cleared and the refusal notice is
        # all that's left, so up-arrow is the only way back to what was typed.
        event.input.remember(prompt)

        if self._turn_in_progress:
            self._steer_current_turn(prompt)
            return

        if self._dispatch_command(prompt):
            return

        self._send_prompt(prompt)

    def _send_prompt(self, prompt: str) -> None:
        """Show a prompt and start its turn.

        Split out of ``on_input_submitted`` so ``/image <path> <message>``
        sends its message by exactly the same route an ordinary submission
        takes, rather than a parallel one that would drift away from it.
        """
        if self._turn_in_progress:
            self._steer_current_turn(prompt)
            return
        # After the built-ins, before the model: a custom command *is* a
        # prompt, so what it expands to is what gets sent and what the
        # transcript shows. Showing "/review" and sending 400 words would make
        # the session log unreadable to the person who wrote it.
        prompt = self._expand_custom(prompt)
        self.write_user_prompt(prompt)
        # Left enabled, on purpose. Disabling the box until the turn
        # finished would be a "continue? [y/N]" gate with nothing to confirm.
        # Mid-turn steering is what it stays open *for*: submitting again
        # before this one finishes is routed above to _steer_current_turn
        # rather than piling up a second, overlapping run_turn.
        self._turn_in_progress = True
        self._run_turn(prompt)

    def _steer_current_turn(self, message: str) -> None:
        """Redirect the turn already running, instead of starting a new one.

        No command dispatch and no custom-command expansion here, unlike an
        ordinary prompt: text typed while a turn is running reads as talking
        to the model *right now*, not as issuing a meta-command — swapping
        the model or resuming a session mid-turn is a different, riskier
        thing than this is trying to be. ``Orchestrator.steer()`` can refuse
        (no orchestrator yet, or the turn just finished in the gap between
        the keypress and this running) — reported plainly rather than
        silently dropped, since the message the user just typed did not, in
        that case, go anywhere. It is in the prompt history either way (see
        ``on_prompt_input_submitted``), so a refused message is one up-arrow
        from being sent as an ordinary prompt instead.
        """
        if self.orchestrator is None or not self.orchestrator.steer(message):
            self.write_transcript(
                render.build_notice("Nothing to steer — the turn just finished.")
            )
            return
        self.write_transcript(render.build_steer_message(message))

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    def _dispatch_command(self, prompt: str) -> bool:
        """Run ``prompt`` as a slash command, or report that it isn't one.

        A dict rather than the chain of ``startswith`` branches this replaced:
        each of those re-sliced the command name out of the prompt with
        ``prompt[len("/model"):]``, so the literal appeared twice per command
        and adding one meant editing the middle of a chain. Matching the first
        word exactly also fixes ``/planned`` being read as ``/plan`` with the
        argument "ned".

        An unrecognised ``/thing`` is deliberately *not* a command: it goes to
        the model like any other message, since it is far more likely to be
        prose than a typo'd command.

        Returns ``True`` when the input was handled here and nothing should be
        sent to the model. A *custom* command is the exception that isn't one:
        it is handled here but produces a prompt, which is sent — so it is
        expanded before this is ever called (see ``on_input_submitted``) and
        reaches this method only if it also collides with a built-in name,
        which the built-in wins.
        """
        if prompt == "?":
            self.action_help("")
            return True
        if not prompt.startswith("/"):
            return False
        name, _, argument = prompt.partition(" ")
        handler = slash_commands.COMMANDS.get(name)
        if handler is None:
            return False
        handler(self, argument.strip())
        return True

    def _discard_orchestrator(self) -> None:
        """Drop the current orchestrator, stopping anything it started.

        Interactive mode rebuilds one whenever the session changes, so a plain
        ``self.orchestrator = None`` would leave any MCP servers it started
        running with nothing holding them — several sessions into an
        afternoon that is a handful of orphaned child processes.
        """
        if self.orchestrator is not None:
            self.orchestrator.close()
        self.orchestrator = None

    def on_unmount(self) -> None:
        """Shut down cleanly when the app closes."""
        self._discard_orchestrator()

    def _expand_custom(self, prompt: str) -> str:
        """Turn ``/review foo.py`` into the prompt the user wrote down.

        Built-in commands are resolved first (in ``_dispatch_command``), so a
        custom command cannot shadow ``/undo`` and quietly change what it does.
        Discovery is per-submission rather than cached at startup: editing a
        command file and using it in the same session is the normal way these
        get written, and a cache would mean restarting to test a one-line
        change.
        """
        if not prompt.startswith("/") or prompt.partition(" ")[0] in slash_commands.COMMANDS:
            return prompt
        return expand_custom_command(prompt, self.cwd)

    # Catalogue bookkeeping lives in CatalogueStore, not here — none of it
    # touches a widget. These two remain because the modals ask the *app*
    # for them (`cast("CoBirbApp", self.app)`), and forwarding is cheaper
    # than teaching every screen where the store lives.
    def memory_catalogue_rows(self) -> "list[memory.CatalogueFile]":
        return self.catalogues.rows()

    def memory_load(self, row: "memory.CatalogueFile", password: "str | None") -> str:
        return self.catalogues.load(row, password)

    def memory_unload(self, name: str) -> None:
        self.catalogues.unload(name)

    def memory_create(self, name: str, password: str) -> str:
        return self.catalogues.create(name, password)

    def memory_delete(self, name: str) -> str:
        return self.catalogues.delete(name)

    def memory_rename(self, name: str, new_name: str) -> str:
        return self.catalogues.rename(name, new_name)

    def memory_remember(self, catalogue_name: str, fact: str) -> None:
        """Save the fact, then say what happened — the one part of this that
        is the app's job rather than the store's."""
        error = self.catalogues.remember(catalogue_name, fact)
        self.write_transcript(
            render.build_notice(error or f"Remembered, in '{catalogue_name}'.")
        )

    def _on_turn_finished(self) -> None:
        self._turn_in_progress = False
        self._flush_stream()
        self.set_busy("")
        prompt_input = self.query_one("#prompt-input", PromptInput)
        prompt_input.disabled = False
        prompt_input.focus()
        self.offer_pending_charter()

    # ------------------------------------------------------------------ #
    # A charter proposed outside a flock run
    # ------------------------------------------------------------------ #
    def note_proposed_charter(self, charter: Any) -> None:
        """Hold a charter `propose_charter` just accepted. Main thread only.

        Held rather than acted on, because this arrives from inside a tool
        call: the turn that made it is still running and still waiting for the
        tool's result. Putting a modal up here would ask the user to approve
        something while the agent that proposed it is mid-sentence, and
        approving it would start a flock on top of a turn that has not
        finished — the one thing `/flock` already refuses to do.

        Suppressed entirely while a flock is running, because `run_flock_session`
        asks for approval itself at its own stage, and two dialogs for one
        charter is one too many.
        """
        if self._flock_stop is not None:
            return
        self._pending_charter = charter
        self.write_transcript(
            render.build_notice(
                f"Brainy Birb proposed a charter: {len(charter.workers)} worker(s). "
                "You will be asked to approve it when this turn finishes."
            )
        )

    def offer_pending_charter(self) -> None:
        """Put a held charter in front of the user, and run it if they agree."""
        charter = self._pending_charter
        if charter is None or self._flock_stop is not None or self._turn_in_progress:
            return
        self._pending_charter = None
        self._start_flock_with(charter)

    def _start_flock_with(self, charter: Any) -> None:
        """Run a flock from a charter that already exists, skipping planning.

        **Deliberately does not ask.** `run_flock_session` puts the identical
        question — the same sentence, the same charter — at its own stage 3,
        and that stage is the design's single decision point: it is what
        authorises every Worker Birb to run unattended inside scopes the user
        has seen. Asking here as well made one decision take two dialogs, and
        a second dialog repeating the first teaches people to dismiss both
        without reading either.

        So this commits only to *offering* the flock. The user still approves
        or refuses it a moment later, from the one prompt that has always
        been the place to do that.
        """
        if self._turn_in_progress or self._flock_stop is not None:
            self.write_transcript(
                render.build_notice("Something else is running; the charter is still available "
                                    "— /charter when it finishes.")
            )
            return
        self.query_one(TabbedContent).active = "flock"
        self._turn_in_progress = True
        self.query_one("#prompt-input", PromptInput).disabled = True
        self._flock_stop = threading.Event()
        self._flock_canceller = Canceller()
        # No planning phase on this path — the charter already exists — so the
        # progress counter has nothing to count.
        self._planning_calls = 0
        self.charter_in_hand = True
        self.set_activity("Fanning out…")
        self._run_flock(charter.objective, charter=charter)

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
                self._arm_charter_tool()
            # Named `turn_result`, not `session`: this module also imports
            # `cobirb.session` (the Sessions-tab code below needs it), and a
            # same-named local here would shadow it for the rest of this
            # method.
            # Mentions are expanded on the way to the model, not on the way
            # to the transcript: the transcript shows "@src/main.py" the way
            # it was typed, while the model receives the file with it.
            prompt = mentions.expand(
                prompt, self.cwd, redact_secrets=Config().get("redact_secrets") is not False
            )
            memory_block = self.catalogues.system_prompt()
            system = f"{self.system}\n\n{memory_block}" if memory_block else self.system
            images = self.attachments.take()
            turn_result = self.orchestrator.run(
                prompt,
                system,
                cwd=self.cwd,
                persona=self.persona.name,
                session_path=self.session_path,
                plan_mode=self.plan_mode,
                images=images,
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
        prompt_input = self.query_one("#prompt-input", PromptInput)
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
            # And forget the window the *previous* model reported.
            # `_context_budget` asks the provider once and remembers, on the
            # reasoning that a window cannot change mid-run — true of a run,
            # untrue of a session, because this line swaps the provider under
            # it. Left stale, a switch away from a small-window model kept
            # compacting against the previous budget for the rest of the session,
            # which is enough on its own to elide an attached image. Back to
            # None means "ask the new provider", unless config states one.
            self.orchestrator.context_tokens = Config().get("context_tokens")
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

    # ------------------------------------------------------------------ #
    # The Flock
    # ------------------------------------------------------------------ #
    def _arm_charter_tool(self) -> None:
        """Keep `propose_charter` on the orchestrator, and route what it accepts.

        Called wherever the orchestrator is built, so the tool is there for
        every turn rather than only inside a planning one. Without it, the
        planning transcript keeps telling Brainy Birb to call a tool that was
        taken away the moment planning ended, and "redo the plan" dead-ends on
        `Unknown tool`.
        """
        from ..flock.run import install_charter_tool

        if self.orchestrator is None:
            return
        try:
            install_charter_tool(
                self.orchestrator,
                self.cwd,
                on_proposed=lambda charter: self.call_from_thread(
                    self.note_proposed_charter, charter
                ),
            )
        except Exception:  # noqa: BLE001 - flock arming must not cost an ordinary turn
            # Losing the charter tool costs flock mode, which will say so
            # plainly when it cannot find it. Taking the turn down with it
            # would cost the conversation the user is actually having.
            logger.debug("could not install the charter tool", exc_info=True)

    @work(thread=True, exclusive=True, group="flock")
    def _run_flock(self, objective: str, charter: Any = None) -> None:
        """Run a whole flock engagement on a thread worker.

        Same shape as ``_run_turn``: the flock is synchronous and blocking, so
        it lives off the event loop and every callback it makes bridges back
        (see ``flock_bridge``). Building the orchestrator here rather than
        reusing ``self.orchestrator`` would lose this session's approvals, so
        it uses the same lazily-built one every turn uses.
        """
        from ..flock.run import run_flock_session

        try:
            if self.orchestrator is None:
                self.orchestrator = wiring.build_orchestrator(
                    self.cwd, self.persona, self.allow_overrides,
                    self.session_path, self.password, self.model_name,
                    io_factory=lambda: TuiIO(self),
                )
                self._arm_charter_tool()
            run = run_flock_session(
                self.orchestrator,
                objective,
                self.cwd,
                ask=TuiAsker(self),
                stop=self._flock_stop,
                on_event=self._on_flock_event,
                io_for=lambda worker: WorkerPaneIO(self, worker.id),
                on_charter=lambda charter: self.call_from_thread(
                    self.prepare_flock_panes, charter
                ),
                canceller=self._flock_canceller,
                password=self.password,
                charter=charter,
            )
        except Exception as exc:  # noqa: BLE001 - a failed flock is a message, not a crash
            self.call_from_thread(
                self.write_transcript, render.build_error_panel("Brainy Birb", str(exc))
            )
            self.call_from_thread(self._on_flock_finished, None)
            return
        self.call_from_thread(self._on_flock_finished, run)

    def _on_flock_event(self, kind: str, payload) -> None:
        """Route one supervisor event to the Flock tab. Called off-thread."""
        self.call_from_thread(self._apply_flock_event, kind, payload)

    def _apply_flock_event(self, kind: str, payload) -> None:
        """Route one supervisor event onto the worker's pane, then refresh
        the roll-up. A worker whose pane has gone is not an error — it simply
        drops out of the summary, which now reads off the panes themselves."""
        pane = self.query_one(FlockPane)
        worker = pane.pane(getattr(payload, "id", None) or getattr(payload, "worker_id", ""))
        if worker is not None:
            if kind == "started":
                worker.set_state("running")
            elif kind == "finished":
                worker.set_state("done" if payload.complete else "failed")
                worker.write(render.build_notice(payload.describe()))
            elif kind == "reviewed":
                # A clean review leaves a "done" alone; an unclean one demotes
                # it, because an acceptance check passing is not the same as
                # the work standing up to review.
                if not payload.clean:
                    worker.set_state("flagged")
                worker.write(render.build_notice(payload.describe()))
        summary = pane.activity_summary()
        if summary:
            self.set_activity("Flock running", summary)

    def forget_used_charter(self) -> None:
        """Drop the charter once a flock has actually run it.

        `propose_charter` keeps the last charter it accepted until the next
        planning turn resets it, which is what makes a dismissed approval
        dialog recoverable with `/charter`. But a charter whose flock has
        *finished* is not pending anything — and left in place it means
        `/charter` silently offers to run the whole round a second time,
        which is a lot of work to start by accident.
        """
        self._pending_charter = None
        tool = (getattr(self.orchestrator, "tools", None) or {}).get(PROPOSE_CHARTER)
        if tool is not None:
            tool.reset()

    def note_brainy_planning(self, line: str) -> None:
        """One line of Brainy Birb's working-out, for the Flock tab.

        Ignored outside a flock's planning phase: an ordinary turn's output
        belongs in the transcript, and once a charter exists the worker panes
        are what the tab is for.
        """
        if self._flock_stop is None or self.charter_in_hand:
            return
        self.query_one(FlockPane).planning_note(line)

    def note_brainy_waiting(self, waiting: bool) -> None:
        """Whether Brainy Birb is blocked on the model right now."""
        if self._flock_stop is None or self.charter_in_hand:
            return
        self.query_one(FlockPane).planning_waiting(waiting)

    def note_flock_planning_progress(self, tool_name: str) -> None:
        """Keep the Flock tab moving while Brainy Birb builds the skeleton.

        `/flock` switches to the Flock tab immediately, but its panes are only
        built once a charter exists — which is the *end* of the longest phase
        of the run. Until then every tool call renders into Current, the tab
        the user was just moved away from, leaving them watching one static
        line through a skeleton that can take twenty turns to write. A run that
        is working hard and a run that has hung look identical from there.
        """
        if self._flock_stop is None or self.charter_in_hand:
            return
        self._planning_calls += 1
        self.set_activity(
            f"Brainy Birb is planning — {self._planning_calls} tool call(s), "
            f"last: {tool_name}"
        )

    def flock_progress(self, text: str) -> None:
        """Progress from the flock itself. Main thread only."""
        headline = text.splitlines()[0][:120]
        self.query_one(FlockPane).set_status(headline)
        self.set_activity(headline)
        self.write_transcript(render.build_notice(text))

    def set_activity(self, activity: str, detail: str = "") -> None:
        """What is running right now, on the line above the status bar.

        Separate from ``set_busy``, which is a suffix on an already-crowded
        line. This is the one a person watching a quiet screen actually reads.
        """
        bar = self.query_one(ActivityBar)
        bar.activity = activity
        bar.detail = detail

    def flock_write(self, worker_id: str, renderable) -> None:
        """One renderable into one Worker Birb's pane. Main thread only."""
        worker = self.query_one(FlockPane).pane(worker_id)
        if worker is not None:
            worker.write(renderable)

    async def request_confirmation(self, question: str, detail: str = "") -> bool:
        """Show a yes/no modal and resolve to the answer.

        Awaited on the event loop on behalf of the flock's thread, exactly as
        ``request_approval`` is for a tool call.
        """
        return await self.push_screen_wait(ConfirmModal(question, detail))

    async def prepare_flock_panes(self, charter) -> None:
        """Lay out a pane per Worker Birb once the charter is approved."""
        # Planning is over; the panes take over from the tail.
        self.charter_in_hand = True
        self.query_one(FlockPane).end_planning()
        await self.query_one(FlockPane).begin(charter)

    def _on_flock_finished(self, run) -> None:
        self._flock_stop = None
        self._planning_calls = 0
        self.charter_in_hand = False
        self.query_one(FlockPane).end_planning()
        if run is not None and run.ran:
            self.forget_used_charter()
        self._flock_canceller = None
        self.set_activity("")
        self._turn_in_progress = False
        pane = self.query_one(FlockPane)
        if run is None:
            pane.set_status("The flock did not finish.", "bold red")
        elif run.stopped_at:
            pane.set_status(f"Stopped at {run.stopped_at}.", "bold yellow")
        elif run.outcome is not None:
            done = len(run.outcome.complete)
            pane.set_status(
                f"{done} of {len(run.outcome.reports)} ticket(s) complete.",
                "bold green" if run.outcome.all_done else "bold yellow",
            )
        if run is not None and run.report:
            self.write_transcript(render.build_assistant_message(run.report))
        prompt_input = self.query_one("#prompt-input", PromptInput)
        prompt_input.disabled = False
        prompt_input.focus()

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

    def begin_branch_session(self, path: str) -> None:
        self._branch_session_worker(path)

    @work(thread=True, exclusive=True, group="session")
    def _branch_session_worker(self, path: str) -> None:
        """Fork ``path`` (session.fork_session) and switch straight into the
        branch — see SessionsPane's docstring for why landing in it, the same
        way resuming does, is the point rather than an extra step.
        """
        password = self.call_from_thread(
            self.prompt_text, "Branch session", f"Password for {os.path.basename(path)}:", password=True
        )
        if not password:
            return
        self.call_from_thread(self.query_one(SessionsPane).set_status, "Branching…")
        try:
            config = Config()
            _, discovered, _ = plugins.discover_plugins(self.cwd, config)
            crypto, _ = plugins.build_crypto(config, discovered)
            branch = session.fork_session(path, crypto, password)
        except Exception as exc:  # noqa: BLE001 - wrong password/corruption is routine, not fatal
            self.call_from_thread(
                self.query_one(SessionsPane).set_status, f"Could not branch that session — {exc}"
            )
            return
        self.call_from_thread(
            lambda: self._apply_resumed_session(branch.path, password, branch, verb="Branched")
        )

    @work(thread=True, exclusive=True, group="session")
    def _resume_session_worker(self, path: str) -> None:
        password = self.call_from_thread(
            self.prompt_text, "Resume session", f"Password for {os.path.basename(path)}:", password=True
        )
        if not password:
            return
        self.call_from_thread(self.query_one(SessionsPane).set_status, "Unlocking…")
        try:
            config = Config()
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

    def _apply_resumed_session(
        self, path: str, password: str, manager: session.SessionManager, *, verb: str = "Resumed"
    ) -> None:
        self.session_path = path
        self.password = password
        # Discarded rather than live-swapped: the next message rebuilds it
        # bound to this session via the exact same load-if-exists path
        # _build_orchestrator already uses for --session, so there is only
        # one code path that ever opens a session file, tested once.
        self._discard_orchestrator()
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
        self.render_history(turns, os.path.basename(path))

        self.query_one(SessionsPane).set_status(
            f"{verb} '{os.path.basename(path)}' — {len(turns)} turn(s), persona "
            f"{self.persona.name}. Your next message continues it."
        )
        self.refresh_sessions_pane()
        # Straight back to the conversation: resuming is a thing you do in
        # order to keep talking, and leaving the user on the Sessions tab
        # makes them go and find it.
        self.query_one(TabbedContent).active = "current"
        self.query_one("#prompt-input", PromptInput).focus()

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
        self._discard_orchestrator()
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
        if self._flock_stop is not None:
            # A flock is several agents deep in somebody's working tree, so
            # this asks first. An accidental Ctrl+C that silently abandoned a
            # run halfway would leave the tree in a state nobody chose.
            #
            # Two presses, escalating. The first sets the graceful stop — no
            # new workers, in-flight ones finish. But an in-flight worker
            # blocked waiting on the model will never finish, so a second
            # Ctrl+C once we are already stopping offers the hard stop: drop
            # the model connections, which unsticks the workers and tells
            # Ollama to abort.
            if self._flock_stop.is_set():
                self._confirm_flock_force()
            else:
                self._confirm_flock_stop()
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

    @work
    async def _confirm_flock_stop(self) -> None:
        """Ask whether to interrupt a running flock, and stop it if so.

        Stopping means no *further* Worker Birbs start; the ones already
        talking to a model finish their turn, because a model call in flight
        has no handle to interrupt — which is already true of an ordinary
        turn. The dialog says so rather than promising an instant halt it
        cannot deliver.
        """
        stop = self._flock_stop
        if stop is None:
            return
        confirmed = await self.push_screen_wait(
            ConfirmModal(
                "Interrupt the flock?",
                "No further Worker Birbs will start. Any already working will finish "
                "their current turn — a model call in flight cannot be cut off.\n\n"
                "Whatever has already been written to your files stays written; use git "
                "to put it back.",
                confirm_label="Interrupt",
            )
        )
        if not confirmed:
            return
        stop.set()
        self.query_one(FlockPane).set_status(
            "Interrupting — waiting for workers to finish. Ctrl+C again to force-stop.",
            "bold yellow",
        )
        self.notify(
            "Stopping after the current workers finish. Ctrl+C again to force-stop a "
            "worker stuck on the model.",
            title="Flock",
        )

    @work
    async def _confirm_flock_force(self) -> None:
        """Second Ctrl+C: force-stop workers that will not finish on their own.

        A worker blocked waiting on the model never reaches the graceful stop,
        because its thread is not checking anything. This drops its connection
        to the model — which unsticks the read and, with Ollama, makes the
        server abort the half-finished generation. Sharper than the graceful
        stop, so it asks separately rather than doing it on the first press.
        """
        canceller = self._flock_canceller
        if canceller is None:
            return
        confirmed = await self.push_screen_wait(
            ConfirmModal(
                "Force-stop the flock now?",
                "This drops the connection to the model for every worker still "
                "running, which unsticks a worker waiting on Ollama and tells Ollama "
                "to stop generating.\n\n"
                "Those workers end as failed. Whatever was already written to your "
                "files stays written; use git to put it back.",
                confirm_label="Force stop",
            )
        )
        if not confirmed:
            return
        canceller.force()
        self.query_one(FlockPane).set_status("Force-stopping…", "bold red")
        self.notify("Dropping the model connections now.", title="Flock")

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
        if self._flock_stop is not None:
            # Same reasoning as unsticking a shell command below: without this,
            # quitting waits for every remaining Worker Birb to run. Quitting
            # is unambiguous, so it goes straight to the force-stop rather than
            # the graceful one — a worker stuck on the model would otherwise
            # hold the whole app open on the way out.
            self._flock_stop.set()
            if self._flock_canceller is not None:
                self._flock_canceller.force()
        if self._turn_in_progress:
            self._attempt_cancel()
        await super().action_quit()
