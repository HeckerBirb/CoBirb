"""The list that appears under the prompt box while a ``/command`` is typed.

The same five rows, and the same split, as ``mention_picker.py``: the widget
owns what is on screen and which row is selected, the ranking lives in
``runtime.command_index``, and the entries come from whoever constructs it.

One thing this has that the mention picker does not: a count of what it is
*not* showing. There are around twenty commands and room for five, so without
it someone who typed ``/`` would reasonably conclude those five are all there
is — which is the opposite of what a discovery aid is for.
"""
from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.widgets import Static

from ..plugins.core import render
from ..runtime import command_index
from ..runtime.command_index import CommandEntry

# Never more than this many rows. The prompt box grows to eight lines before
# it scrolls, so a taller picker would bury what is being written.
MAX_ROWS = 5


class CommandPicker(Static):
    """Commands matching what has been typed after the ``/``, best first."""

    def __init__(self, entries: Callable[[], "list[CommandEntry]"], **kwargs: object) -> None:
        super().__init__("", **kwargs)
        # A callable for the same reason the mention picker takes one: command
        # files can be added while the app is open.
        self._entries = entries
        self._rows: "list[CommandEntry]" = []
        self._total = 0
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
    def current(self) -> "str | None":
        """The highlighted command's name, without its slash."""
        return self._rows[self._selected].name if self._rows else None

    @property
    def rows(self) -> "list[str]":
        """The names on screen, for tests and for the app."""
        return [entry.name for entry in self._rows]

    @property
    def total(self) -> int:
        """How many commands matched, including those with no room."""
        return self._total

    def refresh_for(self, query: str) -> None:
        """Show the best matches for ``query``, or close if there are none."""
        try:
            candidates = self._entries()
        except Exception:  # noqa: BLE001 - a broken command file costs the picker, never the turn
            candidates = []
        matches = command_index.rank(query, candidates)
        self._total = len(matches)
        self._rows = matches[:MAX_ROWS]
        self._selected = 0
        self._render_rows()

    def close(self) -> None:
        self._rows = []
        self._total = 0
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
        width = max(len(entry.name) for entry in self._rows) + 2
        body = Text()
        for index, entry in enumerate(self._rows):
            if index:
                body.append("\n")
            selected = index == self._selected
            body.append("› " if selected else "  ", style=render.TAIL_RED if selected else "")
            name = f"/{entry.name}".ljust(width)
            body.append(name, style=f"bold {render.CREST_GRAY}" if selected else render.CREST_GRAY)
            if entry.description:
                body.append(entry.description, style=render.FEATHER_GRAY)
            if entry.source:
                body.append(f"  ({entry.source})", style=render.FEATHER_GRAY)
        if self._total > len(self._rows):
            body.append(
                f"\n  {len(self._rows)} of {self._total} — keep typing to narrow",
                style=render.FEATHER_GRAY,
            )
        self.update(body)
        self.display = True
