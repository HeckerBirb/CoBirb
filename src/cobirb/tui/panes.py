"""The ``Sessions`` and ``Plugins`` tab content.

Both panes are self-contained widgets: they compose their own layout, handle
their own button/list events, and call back into the running ``CoBirbApp``
(via ``self.app``, cast for typing) to do anything that touches shared app
state — the same "widget handles its own local events, then delegates" split
``ApprovalModal``/``TextPromptModal`` already use, just for tab content
instead of a modal.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalScroll, Vertical
from rich.text import Text
from textual.widgets import Button, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .. import session
from ..plugins.core import render
from .widgets import TranscriptLog

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .. import cli
    from .app import CoBirbApp


class PluginsPane(Vertical):
    """Discovered plugins, registered tools, and any discovery problems.

    Populated by ``CoBirbApp`` (discovery touches the filesystem/entry
    points, so it runs off the main thread) via ``render_summary``/
    ``render_error``; this widget only ever displays what it's given.
    """

    def compose(self) -> ComposeResult:
        yield RichLog(id="plugins-log", markup=False, highlight=False, wrap=True)
        with Horizontal(id="plugins-actions"):
            yield Button("Refresh", id="plugins-refresh")

    def render_summary(self, summary: "cli.PluginsSummary") -> None:
        log = self.query_one("#plugins-log", RichLog)
        log.clear()
        log.write(render.build_plugins_view(summary))

    def render_error(self, message: str) -> None:
        log = self.query_one("#plugins-log", RichLog)
        log.clear()
        log.write(f"Could not discover plugins: {message}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # this is the handler for it; don't let it keep bubbling
        if event.button.id == "plugins-refresh":
            cast("CoBirbApp", self.app).refresh_plugins_pane()


class SessionsPane(Vertical):
    """Browse, resume, branch, and start encrypted sessions.

    Session files are discovered under ``session.default_sessions_dir()`` —
    a plain, un-decrypted directory listing (name/size/modified only); a
    session opened elsewhere via ``--session <path>`` still works exactly as
    before and just isn't listed here unless it happens to live in that
    directory too.

    "Branch" forks the whole of the selected session into a new file
    (``session.fork_session``) and switches straight into it, the same way
    "Resume" does — the point of branching is to keep talking, in a
    different direction, so landing anywhere else would just be one more
    step between choosing to branch and actually doing so. The original
    file is untouched and still listed at its original length. Branching
    from an earlier point in the conversation rather than its current end
    is deliberately CLI-only for now (``cobirb --branch PATH --branch-at
    N``) — picking a cut point needs its own turn-list UI, which is a
    bigger piece than this milestone's scope.
    """

    def compose(self) -> ComposeResult:
        yield Static(id="sessions-active")
        yield OptionList(id="sessions-list")
        with Horizontal(id="sessions-actions"):
            yield Button("Resume", id="sessions-resume", variant="primary")
            yield Button("Branch…", id="sessions-branch")
            yield Button("New session…", id="sessions-new")
            yield Button("Refresh", id="sessions-refresh")
        yield Static(id="sessions-status")

    def refresh_sessions(self, active_path: str | None) -> None:
        active = self.query_one("#sessions-active", Static)
        if active_path:
            active.update(f"Active session: {active_path}")
        else:
            active.update("No session active for this run — resume one below, or start a new one.")

        options = self.query_one("#sessions-list", OptionList)
        options.clear_options()
        files = session.discover_sessions(session.default_sessions_dir())
        if not files:
            options.add_option(Option("(no saved sessions found)", disabled=True))
            return
        for entry in files:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.modified))
            marker = "* " if entry.path == active_path else "  "
            options.add_option(Option(f"{marker}{entry.name}  ({when}, {entry.size} bytes)", id=entry.path))

    def set_status(self, message: str) -> None:
        self.query_one("#sessions-status", Static).update(message)

    def _selected_path(self) -> str | None:
        """The highlighted session's path, or ``None`` with nothing to act on.

        Says so in the status line when nothing is selected, and stays quiet
        for the "(no saved sessions found)" placeholder row — that one is a
        message, not a choice, so telling someone to select a session when
        there are none to select would be the wrong instruction.
        """
        options = self.query_one("#sessions-list", OptionList)
        if options.highlighted is None:
            self.set_status("Select a session from the list first.")
            return None
        return options.get_option_at_index(options.highlighted).id

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # this is the handler for it; don't let it keep bubbling
        app = cast("CoBirbApp", self.app)
        if event.button.id == "sessions-resume":
            if (path := self._selected_path()) is not None:
                app.begin_resume_session(path)
        elif event.button.id == "sessions-branch":
            if (path := self._selected_path()) is not None:
                app.begin_branch_session(path)
        elif event.button.id == "sessions-new":
            app.begin_new_session()
        elif event.button.id == "sessions-refresh":
            app.refresh_sessions_pane()


class WorkerPane(Vertical):
    """One Worker Birb's own column: who it is, what it owns, what it is doing.

    A pane each rather than one interleaved transcript. Two workers writing
    into a single log produce a stream where neither is followable — and
    following one worker is the only way to tell whether it is stuck, which
    is the thing a person watching actually wants to know.
    """

    def __init__(self, worker_id: str, scope: str) -> None:
        super().__init__(id=f"worker-{worker_id}", classes="worker-pane")
        self._worker_id = worker_id
        self._scope = scope
        # Remembered, not just rendered. The pane that shows a state is the
        # thing that has it: a second copy kept elsewhere for the activity
        # roll-up would be the same fact in two places, free to disagree —
        # a worker whose pane is gone still counting in the summary.
        self.state = "waiting"

    def compose(self) -> ComposeResult:
        yield Static(self._title("waiting"), classes="worker-title")
        yield Static(self._scope, classes="worker-scope")
        # TranscriptLog, not a plain RichLog: a stock one cannot be selected
        # or copied out of at all (see its docstring). A worker's pane is
        # where its shortcoming report and its review land, and a report you
        # cannot copy out of is a report you have to retype.
        yield TranscriptLog(
            id=f"worker-log-{self._worker_id}", markup=False, highlight=False, wrap=True,
            classes="worker-log",
        )

    def _title(self, state: str) -> Text:
        colours = {
            "waiting": "dim",
            "running": "bold yellow",
            # Distinct from "waiting", which means "has not started". This one
            # has started, has asked the user for something, and is the only
            # state a person can clear — so it is the loudest colour here.
            "held": "bold magenta",
            "done": "bold green",
            "failed": "bold red",
            "flagged": "bold yellow",
        }
        label = "held — waiting for you" if state == "held" else state
        return Text(f"[{self._worker_id}] {label}", style=colours.get(state, "bold"))

    def set_state(self, state: str) -> None:
        self.state = state
        self.query_one(".worker-title", Static).update(self._title(state))

    def write(self, renderable) -> None:
        self.query_one(TranscriptLog).write(renderable)


class FlockPane(Vertical):
    """The Flock tab: one column per Worker Birb, filled in as they work.

    Rebuilt at the start of each engagement rather than reused, because the
    workers differ every time — a pane left over from the last flock showing a
    ticket that no longer exists is worse than an empty tab.
    """

    # How many lines of Brainy Birb's working-out to keep on screen while it
    # plans. Enough to see movement and read the last thing it did; few enough
    # that it stays a status strip rather than a second transcript.
    PLANNING_TAIL = 8

    WAITING = "[ Waiting for LLM... ]"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._planning_lines: list[str] = []
        self._planning_waiting = False

    def on_mount(self) -> None:
        self.query_one("#brainy-status", Static).display = False

    def compose(self) -> ComposeResult:
        yield Static(
            Text("No flock running. Type /flock <objective> to start one.", style="dim"),
            id="flock-status",
        )
        # Brainy Birb's working-out, while it has the tab to itself. Hidden
        # until planning starts and removed the moment a charter exists: from
        # then on the worker panes are the thing worth looking at, and a stale
        # tail of how the plan was written would only be in their way.
        yield Static("", id="brainy-status")
        # HorizontalScroll, and each pane has a *minimum* width rather than an
        # equal share. Three agents at 1fr each squeeze the text past reading,
        # and a pane too narrow to read is the same as no pane — so they keep
        # their width and the row scrolls sideways instead.
        yield HorizontalScroll(id="flock-workers")

    def set_status(self, text: str, style: str = "") -> None:
        self.query_one("#flock-status", Static).update(Text(text, style=style or "bold"))

    # ------------------------------------------------------------------ #
    # Brainy Birb, while it plans
    # ------------------------------------------------------------------ #
    def planning_note(self, line: str) -> None:
        """Add one line to the tail of what Brainy Birb is doing.

        `/flock` switches to this tab, but the worker panes do not exist until
        a charter does — which is the *end* of the longest phase of the run.
        Without this the tab is blank for all of it, and a flock that is
        working hard is indistinguishable from one that has hung.
        """
        line = " ".join(line.split())
        if not line:
            return
        self._planning_lines.append(line)
        del self._planning_lines[: -self.PLANNING_TAIL]
        self._draw_planning()

    def planning_waiting(self, waiting: bool) -> None:
        """Whether Brainy Birb is currently blocked on the model."""
        self._planning_waiting = waiting
        self._draw_planning()

    def end_planning(self) -> None:
        """Take the section away. The worker panes speak for themselves."""
        self._planning_lines = []
        self._planning_waiting = False
        panel = self.query_one("#brainy-status", Static)
        panel.update("")
        panel.display = False

    def _draw_planning(self) -> None:
        panel = self.query_one("#brainy-status", Static)
        if not self._planning_lines and not self._planning_waiting:
            panel.display = False
            return
        body = Text()
        body.append("Brainy Birb\n", style="bold")
        for line in self._planning_lines:
            body.append(f"  {line[:200]}\n", style="dim")
        if self._planning_waiting:
            body.append("\n")
            body.append(f"  {self.WAITING}", style="bold")
        panel.update(body)
        panel.display = True

    async def begin(self, charter) -> None:
        """Lay out one pane per worker in the approved charter."""
        row = self.query_one("#flock-workers", HorizontalScroll)
        await row.remove_children()
        for worker in charter.workers:
            scope = "writes " + ", ".join(worker.writes)
            if worker.reads:
                scope += "\nreads  " + ", ".join(worker.reads)
            await row.mount(WorkerPane(worker.id, scope))

    def pane(self, worker_id: str) -> "WorkerPane | None":
        try:
            return self.query_one(f"#worker-{worker_id}", WorkerPane)
        except Exception:  # noqa: BLE001 - a pane that is gone is not an error
            return None

    def activity_summary(self) -> str:
        """A roll-up of who is doing what, for the activity line.

        The whole flock on one line rather than only the most recent event:
        during a fan-out the interesting question is not "what just happened"
        but "is anything still going", and that needs every worker visible at
        once. Read off the panes themselves, so it can only ever describe
        workers that are actually on screen.
        """
        # "held" first: it is the only state in this list that needs a person
        # to do something, and a roll-up that buries it under four running
        # workers is a roll-up nobody acts on.
        order = {"held": 0, "running": 1, "waiting": 2, "flagged": 3, "failed": 4, "done": 5}
        panes = sorted(
            self.query(WorkerPane), key=lambda p: (order.get(p.state, 9), p._worker_id)
        )
        return " · ".join(f"{pane._worker_id} {pane.state}" for pane in panes)

    async def clear(self) -> None:
        await self.query_one("#flock-workers", HorizontalScroll).remove_children()
        self.set_status("No flock running. Type /flock <objective> to start one.", "dim")
