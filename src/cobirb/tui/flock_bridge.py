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
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..flock.charter import writes_owner
from ..plugins.core import render
from ..policy import WRITE_TOOLS
from ..runtime.headless import HeadlessIO
from ..typing.spi import DECISION_DENY, DECISIONS, ApprovalOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .app import CoBirbApp


def _unpack(answer: Any) -> tuple[str, str]:
    """A modal's ``(decision, instruction)``, normalised.

    The dialog is ours and returns the pair, but everything between here and
    the orchestrator treats an unrecognised answer as a refusal, and this is
    the boundary where a shutting-down app hands back ``None``. Anything that
    is not a decision string this codebase knows is a deny — fail closed, same
    as every other approval path.
    """
    if not isinstance(answer, tuple) or len(answer) != 2:
        return DECISION_DENY, ""
    decision, instruction = answer
    if decision not in DECISIONS:
        return DECISION_DENY, ""
    return str(decision), str(instruction or "")


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
    """A Worker Birb's I/O: its own pane, and its way of asking for something.

    Still a ``HeadlessIO`` underneath, and the inherited ``confirm`` still
    refuses — which is what any adapter without a screen in front of it must
    do. What this adds is the one hook that has somewhere to ask:
    ``confirm_request``.

    **This used to refuse everything, deliberately, and that turned out to be
    the wrong half of a trade.** The reasoning was that the charter approval
    had already happened and a worker prompting afterwards would undo the
    single decision point the design was built around. What it missed is what
    a refusal actually costs: a worker that needs to run the project's
    formatter, or reach a tool the charter's file scopes never mentioned, was
    silently denied and spent its remaining turns either retrying or writing
    up why it could not finish. The capability question was answered
    correctly and the work was lost anyway.

    So the charter is still the only place *capability* is granted up front,
    and this is an escalation path out of it, with two properties that keep it
    from becoming a back door:

    - **Asking costs the asker, not the round.** The worker puts its
      concurrency slot down while it waits (``supervisor.Slots``), so the rest
      of the flock carries on at full speed and the user answers when they get
      to it.
    - **One thing can never be asked for.** A write into a file another worker
      owns is refused here, before any dialog — see ``charter.writes_owner``.

    Streamed tokens are still dropped, as they are for any headless run. A
    pane per worker is for watching *what a worker did* — the files it
    touched, the checks it ran — and several models streaming prose into
    narrow columns at once is noise rather than progress.
    """

    def __init__(self, app: "CoBirbApp", worker_id: str, slots: Any = None) -> None:
        super().__init__()
        self._app = app
        self._worker_id = worker_id
        # The flock's concurrency budget (``supervisor.Slots``). Held so that
        # a worker stopping to ask the user something can put its slot down
        # for the duration — a worker waiting on a person is not using the
        # endpoint, and holding a slot while parked is what used to stall the
        # whole round. None when nobody supplied one, in which case asking
        # simply costs the slot it is already holding.
        self._slots = slots

    def name(self) -> str:
        return f"flock-pane:{self._worker_id}"

    def confirm_request(self, request: Any) -> ApprovalOutcome:
        """Put this worker's request to the user, parked until they answer.

        Fails closed on every path that is not an explicit answer — the app
        shutting down mid-question, a force-stop, a pane that has gone. The
        worker is told no and carries on with the turns it has left, which is
        the same outcome it used to get unconditionally.
        """
        owner = self._blocked_owner(request)
        if owner:
            # Refused without asking, and the model is told why rather than
            # just "denied": a worker that does not know this boundary exists
            # will spend its next turn trying a neighbouring path.
            self._announce(
                f"refused: {request.tool_name} would write into {owner}'s files"
            )
            return ApprovalOutcome(
                decision=DECISION_DENY,
                instruction=(
                    f"That path belongs to Worker Birb '{owner}'. Workers never write "
                    "into each other's files — that is what makes them safe to run at "
                    "the same time — so this cannot be permitted for you at all. Work "
                    "within your own files, and report what you could not do."
                ),
            )

        self._announce(f"waiting for you: may it use {request.tool_name}?")
        self._state("held")
        try:
            with self._released_slot():
                answer = self._app.call_from_thread(
                    self._app.request_worker_approval,
                    self._worker_id,
                    request.tool_name,
                    request.arguments,
                    request.scope,
                    request.preview,
                )
        except Exception:  # noqa: BLE001 - nobody to ask means no, never guess yes
            return ApprovalOutcome(decision=DECISION_DENY)
        finally:
            self._state("running")
        decision, instruction = _unpack(answer)
        self._announce(f"you said: {decision}")
        return ApprovalOutcome(decision=decision, instruction=instruction)

    @contextmanager
    def _released_slot(self):
        """Give up the concurrency slot for as long as the question is open.

        A worker waiting on a person is not using the model endpoint, so
        holding a slot while parked would mean a flock with `concurrency = 2`
        and two open dialogs running nothing at all. No slots wired (a test
        double, a caller that passed none) simply keeps the one it has.
        """
        if self._slots is None:
            yield
            return
        with self._slots.released():
            yield

    def _blocked_owner(self, request: Any) -> str:
        """Another worker's id when this request would reach into their files.

        Read off the round's own charter, so it answers about the partition
        actually running. No charter to check against — outside a flock, or
        before the panes exist — means nothing to protect, so nothing is
        blocked here and the ordinary policy still applies.
        """
        path = request.arguments.get("path") or request.arguments.get("file_path") or ""
        if not path or request.tool_name not in WRITE_TOOLS:
            return ""
        charter = getattr(self._app, "flock_charter", None)
        if charter is None:
            return ""
        try:
            return writes_owner(charter, str(path), besides=self._worker_id)
        except Exception:  # noqa: BLE001 - an unreadable charter blocks nothing
            return ""

    def _state(self, state: str) -> None:
        try:
            self._app.call_from_thread(self._app.flock_worker_state, self._worker_id, state)
        except Exception:  # noqa: BLE001 - a pane that cannot be drawn is not a failed run
            return

    def _announce(self, text: str) -> None:
        self._write(render.build_notice(f"[{self._worker_id}] {text}"))

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
