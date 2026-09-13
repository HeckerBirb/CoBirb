"""Connecting a blocking flock run to a running Textual app.

``run_flock_session`` is synchronous, like ``Orchestrator.run``, and in the
TUI it runs on a thread worker. So the same rule applies as in ``TuiIO``:
nothing here may touch a widget directly, and everything forwards onto the
main thread via ``App.call_from_thread``.

Two bridges live here.

``TuiAsker`` answers the flock's questions with modals. The sharp one is the
charter approval, which has to block the flock's thread until a person
answers and then *return* their decision — the same shape as
``TuiIO.confirm``, and for the same reason: the call site is inline in a
synchronous function.

``WorkerPaneIO`` gives each Worker Birb somewhere to draw. It is a
``HeadlessIO`` — it still refuses everything not pre-approved and never
prompts, which is what the charter approval bought — that happens to render
its tool calls into a pane on the way past. Watching a worker work and being
asked to approve its calls are different things, and only the first is wanted
here.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from ..plugins.core import render
from ..runtime.headless import HeadlessIO

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .app import CoBirbApp


class TuiAsker:
    """The flock's ``Asker`` protocol, answered by modals.

    Constructed with the app rather than the pane, because the two halves go
    to different places: questions become modals on the screen, progress goes
    into the Flock tab's status line and the transcript.
    """

    def __init__(self, app: "CoBirbApp") -> None:
        self._app = app

    def confirm(self, question: str, detail: str = "") -> bool:
        """Ask, blocking the flock's thread until a person answers.

        Fails closed on anything going wrong — the app shutting down
        mid-question, most likely. An unanswerable charter approval must
        resolve to *no*: it is the only place a person sees what the Worker
        Birbs will be allowed to touch, and a flock that approved its own
        charter would be an agent granting itself permissions.

        ``detail`` is the thing being asked about — the charter itself, most
        importantly — and it goes *inside* the dialog. Shown separately behind
        it, a centred modal covers the very text it is asking approval for.

        Deliberately not routed through a same-thread shortcut, exactly as
        ``TuiIO.confirm`` is not: ``push_screen_wait`` only works inside a
        worker, and calling it on the UI thread would deadlock the loop that
        has to draw and dismiss the modal.
        """
        try:
            answer = self._app.call_from_thread(
                self._app.request_confirmation, question, detail
            )
        except Exception:  # noqa: BLE001 - nobody to ask means no, never guess yes
            return False
        return bool(answer)

    def show(self, text: str) -> None:
        """Progress, into the Flock tab and the transcript.

        Both, because they answer different questions. The status line says
        what is happening *now* and is overwritten; the transcript keeps the
        charter and the round's report where they can be scrolled back to.
        """
        if threading.get_ident() == self._app.ui_thread_id:
            self._app.flock_progress(text)
        else:
            self._app.call_from_thread(self._app.flock_progress, text)


class WorkerPaneIO(HeadlessIO):
    """A Worker Birb's I/O, rendered into its own pane.

    Subclasses ``HeadlessIO`` rather than wrapping it so that ``confirm``
    keeps refusing by inheritance. If this ever grows a prompt it will be
    because somebody deliberately overrode a method whose docstring says not
    to, rather than because a wrapper quietly forwarded one.

    Streamed tokens are dropped, as they are for any headless run. A pane per
    worker is for watching *what a worker did* — the files it touched, the
    checks it ran — and several models streaming prose into narrow columns at
    once is noise rather than progress.
    """

    def __init__(self, app: "CoBirbApp", worker_id: str) -> None:
        super().__init__()
        self._app = app
        self._worker_id = worker_id

    def name(self) -> str:
        return f"flock-pane:{self._worker_id}"

    def render_tool_call(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        self._write(render.build_tool_call_panel(tool_name, arguments, result))

    def render_notice(self, text: str) -> None:
        """Where a worker's acceptance check reports, since the verify loop
        runs inside the orchestrator rather than in the supervisor."""
        self._write(render.build_notice(text))

    def _write(self, renderable: Any) -> None:
        try:
            self._app.call_from_thread(self._app.flock_write, self._worker_id, renderable)
        except Exception:  # noqa: BLE001 - a pane that cannot be drawn is not a failed run
            return
