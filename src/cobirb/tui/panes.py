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
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .. import session
from ..plugins.core import render

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
    """Browse, resume, and start encrypted sessions.

    Session files are discovered under ``session.default_sessions_dir()`` —
    a plain, un-decrypted directory listing (name/size/modified only); a
    session opened elsewhere via ``--session <path>`` still works exactly as
    before and just isn't listed here unless it happens to live in that
    directory too.
    """

    def compose(self) -> ComposeResult:
        yield Static(id="sessions-active")
        yield OptionList(id="sessions-list")
        with Horizontal(id="sessions-actions"):
            yield Button("Resume", id="sessions-resume", variant="primary")
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # this is the handler for it; don't let it keep bubbling
        app = cast("CoBirbApp", self.app)
        if event.button.id == "sessions-resume":
            options = self.query_one("#sessions-list", OptionList)
            if options.highlighted is None:
                self.set_status("Select a session from the list first.")
                return
            option = options.get_option_at_index(options.highlighted)
            if option.id is None:
                return  # the "(no saved sessions found)" placeholder row
            app.begin_resume_session(option.id)
        elif event.button.id == "sessions-new":
            app.begin_new_session()
        elif event.button.id == "sessions-refresh":
            app.refresh_sessions_pane()
