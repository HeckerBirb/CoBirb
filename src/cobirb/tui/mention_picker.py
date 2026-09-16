"""The list that appears under the prompt box while an ``@path`` is typed.

Five rows, never more. The prompt box itself grows to eight lines before it
scrolls, so a picker that took the screen would turn "name a file" into "lose
sight of what you were writing".

The widget owns what is on screen and which row is selected, and nothing else:
the matching lives in ``runtime.mentions`` (which knows nothing about
terminals) and the file list comes from whoever constructs it. That split is
what lets the ranking be tested without a running app.
"""
from __future__ import annotations

import os
from typing import Callable

from rich.text import Text
from textual.widgets import Static

from ..plugins.core import render
from ..runtime import mentions

# Never more than this many rows on screen. See the module docstring.
MAX_ROWS = 5


class MentionPicker(Static):
    """Candidate files for the mention being typed, best first."""

    def __init__(self, paths: Callable[[], list[str]], **kwargs: object) -> None:
        super().__init__("", **kwargs)
        # A callable rather than a list: the working tree changes while the
        # app is open, and a list captured at construction would go stale the
        # first time the agent writes a file.
        self._paths = paths
        self._rows: list[str] = []
        self._selected = 0
        self.display = False

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        """Whether the picker is showing anything to choose from."""
        return bool(self._rows)

    @property
    def current(self) -> str | None:
        """The highlighted path, or ``None`` when nothing is showing."""
        return self._rows[self._selected] if self._rows else None

    @property
    def rows(self) -> list[str]:
        """What is on screen, for tests and for the app."""
        return list(self._rows)

    def refresh_for(self, query: str) -> None:
        """Show the best matches for ``query``, or close if there are none."""
        try:
            candidates = self._paths()
        except Exception:  # noqa: BLE001 - an unreadable tree costs the picker, never the turn
            candidates = []
        self._rows = [match.path for match in mentions.rank(query, candidates, limit=MAX_ROWS)]
        self._selected = 0
        self._render_rows()

    def close(self) -> None:
        self._rows = []
        self._selected = 0
        self.display = False

    def move(self, delta: int) -> None:
        """Move the highlight, wrapping — five rows is short enough that
        wrapping is quicker than stopping at the end."""
        if not self._rows:
            return
        self._selected = (self._selected + delta) % len(self._rows)
        self._render_rows()

    # ------------------------------------------------------------------ #
    # Display
    # ------------------------------------------------------------------ #
    def _render_rows(self) -> None:
        if not self._rows:
            self.display = False
            return
        body = Text()
        for index, path in enumerate(self._rows):
            if index:
                body.append("\n")
            selected = index == self._selected
            body.append("› " if selected else "  ", style=render.TAIL_RED if selected else "")
            # The name carries the weight — it is what was typed at — with the
            # directory behind it, dimmed, only for telling two apart.
            directory, name = os.path.split(path)
            body.append(name, style=f"bold {render.CREST_GRAY}" if selected else render.CREST_GRAY)
            if directory:
                body.append(f"  {directory}", style=render.FEATHER_GRAY)
        self.update(body)
        self.display = True
