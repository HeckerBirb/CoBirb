"""``TuiIO`` — the I/O adapter that connects a blocking ``Orchestrator`` to a
running Textual app.

``Orchestrator.run()`` is deliberately synchronous: it is a plain blocking
call that renders through ``io`` as it goes. In the TUI it therefore runs on
a Textual *thread worker*, off the event loop, which means none of its
callbacks may touch a widget directly. Every method here is called on that
worker thread and forwards its work onto the main thread via
``App.call_from_thread``, which blocks the worker until the callback has run.

``confirm()`` is the interesting one: it must block the worker until a human
answers a modal and then *return* their decision synchronously, because
``Orchestrator._execute_tool`` calls it inline. See its docstring.

The renderables themselves are built by ``plugins.core.render`` — the same
builders ``TerminalIO`` prints — so the transcript here is visually identical
to the scrolling output of one-shot mode.
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

from ..plugins.core import render
from ..typing.spi import DECISION_DENY, DECISIONS, I_OAdapter

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .app import CoBirbApp


class TuiIO(I_OAdapter):
    """Renders orchestrator output into a ``CoBirbApp``'s transcript.

    Implements the ``I_OAdapter`` contract plus the same duck-typed extras
    ``TerminalIO`` exposes (``spinner``, ``render_answer``,
    ``render_plan``, ``render_validation``, ``render_tool_call``), so the
    orchestrator's ``getattr(io, "...", None)`` hooks all find what they look
    for and nothing falls back to plain text.
    """

    def __init__(self, app: "CoBirbApp") -> None:
        self._app = app

    def name(self) -> str:
        return "tui"

    # ------------------------------------------------------------------ #
    # Thread bridging
    # ------------------------------------------------------------------ #
    def _call(self, callback: Callable[..., Any], *args: Any) -> Any:
        """Run ``callback(*args)`` on Textual's main thread and wait for it.

        Called directly when we are already on that thread — which happens
        in tests that drive the adapter without a worker, and would
        otherwise raise, since ``call_from_thread`` refuses to be called
        from the thread it dispatches to.
        """
        if threading.get_ident() == self._app.ui_thread_id:
            return callback(*args)
        return self._app.call_from_thread(callback, *args)

    # ------------------------------------------------------------------ #
    # I_OAdapter
    # ------------------------------------------------------------------ #
    def render(self, text: str) -> None:
        """One streamed chunk (the orchestrator calls this per token).

        Goes to the streaming preview rather than the transcript: a
        ``RichLog`` appends whole lines and can't rewrite the last one, so
        per-token writes would stack every token on its own line.
        """
        self._call(self._app.append_stream, text)

    def listen(self) -> str | None:
        """The TUI's input path is its own ``Input`` widget; there is no
        separate channel to listen on."""
        return None

    def view(self, data: bytes, mime: str | None = None) -> None:
        """No-op, matching ``TerminalIO``. Vision rendering lands in v0.2.0."""
        return None

    def confirm(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Ask the user to approve a tool call, blocking until they answer.

        ``call_from_thread`` accepts a coroutine function: it schedules it
        on the event loop, awaits it there, and hands the resolved value
        back across the thread boundary. ``CoBirbApp.request_approval``
        awaits ``push_screen_wait``, which suspends until the modal is
        dismissed — so this returns exactly the ``"once"``/``"always"``/
        ``"deny"`` string the modal was dismissed with.

        Deliberately not routed through ``_call``: there is no same-thread
        shortcut for this one. ``push_screen_wait`` only works from inside a
        worker, and calling it on the UI thread would deadlock the very loop
        that has to draw and dismiss the modal. ``call_from_thread`` refuses
        that case outright, which lands in the handler below.

        Fails closed on anything going wrong (the app shutting down
        mid-question, most likely): there is then no one to ask, and the
        safe answer to "may I run this?" is no.
        """
        return self._ask(tool_name, arguments, None, "")

    def confirm_scoped(self, request: Any) -> str:
        """``confirm``, plus what "always" would grant and what the call would
        change — both shown in the modal, so neither is agreed to blind."""
        return self._ask(request.tool_name, request.arguments, request.scope, request.preview)

    def _ask(
        self, tool_name: str, arguments: dict[str, Any], scope: str | None, preview: str = ""
    ) -> str:
        try:
            decision = self._app.call_from_thread(
                self._app.request_approval, tool_name, arguments, scope, preview
            )
        except Exception:  # noqa: BLE001 - no one to ask means deny, never guess yes
            return DECISION_DENY
        return decision if decision in DECISIONS else DECISION_DENY

    # ------------------------------------------------------------------ #
    # Extra chrome. Not part of I_OAdapter — reached for via getattr by the
    # orchestrator and CLI, mirroring TerminalIO's own duck-typed hooks.
    # ------------------------------------------------------------------ #
    @contextmanager
    def spinner(self, label: str) -> Iterator[None]:
        """Show ``label`` in the status bar for the duration of a blocking
        call (waiting on the model for its first token).

        ``TerminalIO`` uses a Rich spinner here; a full-screen app can't
        have one renderer painting over another, so the status bar carries
        the same information instead.
        """
        self._call(self._app.set_busy, label)
        self._call(self._app.note_brainy_waiting, True)
        try:
            yield
        finally:
            self._call(self._app.set_busy, "")
            self._call(self._app.note_brainy_waiting, False)

    def begin_stream(self, label: str) -> None:
        """Deliberately draws nothing.

        The scrolling renderer writes a ``>`` marker here because it can only
        append. This one doesn't need to: ``StreamPreview`` re-renders its
        whole buffer on every token through ``render.build_streamed_message``,
        which already carries the marker — drawing one here would put a second
        marker *inside* the streamed text.

        It exists so that the hook is answered rather than skipped, which is
        what keeps the reply label out of the stream (see
        ``Orchestrator._chat``).
        """
        return None

    def render_answer(self, label: str, text: str) -> None:
        """The finished reply, as a marked message rather than a titled panel.

        ``label`` is accepted (the hook's signature is shared with
        ``TerminalIO`` and the orchestrator calls it positionally) but not
        shown: who is speaking is carried by the marker's colour and by the
        status bar, not by a label on every single reply.
        """
        if not text:
            return
        self._write(render.build_assistant_message(text))
        # What the transcript now ends with, so that a flock report which is
        # this same text is not written into it a second time.
        self._call(self._app.note_answer, text)
        # Prose Brainy Birb wrote on the way to a charter, for the Flock tab's
        # tail. Streamed tokens deliberately do not feed it: a per-token feed
        # would rewrite the strip faster than anyone can read it, and what
        # makes progress legible is the sequence of things done, not the
        # sentences being formed.
        self._call(self._app.note_brainy_planning, text)

    def render_plan(self, label: str, text: str) -> None:
        if not text:
            return
        self._write(render.build_plan_panel(label, text))

    def render_validation(self, label: str, text: str) -> None:
        if not text:
            return
        self._write(render.build_validation_panel(label, text))

    def render_notice(self, text: str) -> None:
        """A note about the session, into the transcript.

        There is deliberately no equivalent for slash commands — those are
        handled on the main thread and the app writes them directly. This one
        exists because verification runs inside the orchestrator, on a worker.
        """
        self._write(render.build_notice(text))

    def render_tool_call(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        self._write(render.build_tool_call_panel(tool_name, arguments, result))
        if tool_name == "todo" and getattr(result, "ok", False):
            # The checklist's progress rides on the status bar, so where the
            # agent is in a long task is visible without scrolling back.
            first = str(getattr(result, "content", "")).splitlines()[:1]
            match = re.search(r"\((\d+)/(\d+) done\)", first[0]) if first else None
            if match:
                self._call(self._app.set_checklist, f"{match.group(1)}/{match.group(2)}")
        # Through `_call` like everything else here: this runs on the
        # orchestrator's thread and touches widgets.
        self._call(self._app.note_flock_planning_progress, tool_name)
        # `id` and `at` are the planning tools' subjects: without them the Flock
        # tab's strip shows four bare `add_worker` lines while Brainy Birb is
        # building a charter, which is exactly the stretch it exists to make
        # legible.
        subject = (
            arguments.get("path") or arguments.get("pattern") or arguments.get("command")
            or arguments.get("id") or arguments.get("at") or ""
        )
        self._call(self._app.note_brainy_planning, f"{tool_name} {subject}".strip())

    def write_error(self, label: str, text: str) -> None:
        """A blocked tool call or a provider that fell over.

        The scrolling CLI prints these; a full-screen app owns the whole
        terminal, so they become a panel in the transcript instead of a
        stray line painted over the layout.
        """
        self._write(render.build_error_panel(label, text))


    def _write(self, renderable: Any) -> None:
        """Put a finished renderable in the transcript.

        Anything buffered in the streaming preview is flushed there first,
        so the order the user reads matches the order things happened:
        streamed reasoning, *then* the tool-call panel it led to.
        """
        self._call(self._app.write_transcript, renderable)
