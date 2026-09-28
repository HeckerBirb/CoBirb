"""The lists that appear under the prompt box while an ``@path`` or a
``/command`` is typed.

Five rows, never more. The prompt box itself grows to eight lines before it
scrolls, so a picker that took the screen would turn "name a file" into "lose
sight of what you were writing".

A picker owns what is on screen and which row is selected, and nothing else:
the ranking lives in ``runtime.mentions`` and ``runtime.command_index`` (which
know nothing about terminals), and the candidates come from whoever constructs
it. That split is what lets the ranking be tested without a running app.
``Picker`` holds the part both share; each subclass says how to rank and how
to draw one row.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.widgets import Static

from ..plugins.core import render
from ..runtime import command_index, mentions
from ..runtime.command_index import CommandEntry

# Never more than this many rows on screen. See the module docstring.
MAX_ROWS = 5


class Picker(Static):
    """Ranked candidates for what is being typed, best first."""

    def __init__(self, candidates: Callable[[], list[Any]], **kwargs: object) -> None:
        super().__init__("", **kwargs)
        # A callable rather than a list: the working tree and the command
        # files change while the app is open, and a list captured at
        # construction would go stale the first time the agent writes a file.
        self._candidates = candidates
        self._rows: list[Any] = []
        self._total = 0
        self._selected = 0
        self.display = False

    # -- what a subclass says ------------------------------------------- #
    def _rank(self, query: str, candidates: list[Any]) -> list[Any]:
        """Every candidate matching ``query``, best first."""
        raise NotImplementedError

    def _label(self, row: Any) -> str:
        """What choosing ``row`` inserts, and what ``rows`` reports."""
        raise NotImplementedError

    def _draw_row(self, body: Text, row: Any, selected: bool) -> None:
        """Append one row, after its selection marker, to ``body``."""
        raise NotImplementedError

    # -- state ----------------------------------------------------------- #
    @property
    def active(self) -> bool:
        """Whether the picker is showing anything to choose from."""
        return bool(self._rows)

    @property
    def current(self) -> str | None:
        """The highlighted row, or ``None`` when nothing is showing."""
        return self._label(self._rows[self._selected]) if self._rows else None

    @property
    def rows(self) -> list[str]:
        """What is on screen, for tests and for the app."""
        return [self._label(row) for row in self._rows]

    @property
    def total(self) -> int:
        """How many candidates matched, including those with no room."""
        return self._total

    def refresh_for(self, query: str) -> None:
        """Show the best matches for ``query``, or close if there are none."""
        try:
            candidates = self._candidates()
        except Exception:  # noqa: BLE001 - an unreadable tree or command file costs the picker, never the turn
            candidates = []
        matches = self._rank(query, candidates)
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

    # -- display --------------------------------------------------------- #
    def _render_rows(self) -> None:
        if not self._rows:
            self.display = False
            return
        body = Text()
        for index, row in enumerate(self._rows):
            if index:
                body.append("\n")
            selected = index == self._selected
            body.append("› " if selected else "  ", style=render.TAIL_RED if selected else "")
            self._draw_row(body, row, selected)
        self._draw_footer(body)
        self.update(body)
        self.display = True

    def _draw_footer(self, body: Text) -> None:
        """Anything after the rows; nothing by default."""


class MentionPicker(Picker):
    """Candidate files for the ``@`` mention being typed."""

    def _rank(self, query: str, candidates: list[str]) -> list[str]:
        return [match.path for match in mentions.rank(query, candidates, limit=MAX_ROWS)]

    def _label(self, row: str) -> str:
        return row

    def _draw_row(self, body: Text, row: str, selected: bool) -> None:
        # The name carries the weight — it is what was typed at — with the
        # directory behind it, dimmed, only for telling two apart.
        directory, name = os.path.split(row)
        body.append(name, style=f"bold {render.CREST_GRAY}" if selected else render.CREST_GRAY)
        if directory:
            body.append(f"  {directory}", style=render.FEATHER_GRAY)


class CommandPicker(Picker):
    """Commands matching what has been typed after the ``/``.

    Unlike the mention picker it says what it is *not* showing. There are
    around twenty commands and room for five, so without the count someone
    who typed ``/`` would reasonably conclude those five are all there is —
    the opposite of what a discovery aid is for.
    """

    def _rank(self, query: str, candidates: list[CommandEntry]) -> list[CommandEntry]:
        return command_index.rank(query, candidates)

    def _label(self, row: CommandEntry) -> str:
        return row.name

    def _draw_row(self, body: Text, row: CommandEntry, selected: bool) -> None:
        width = max(len(entry.name) for entry in self._rows) + 2
        body.append(
            f"/{row.name}".ljust(width), style=f"bold {render.CREST_GRAY}" if selected else render.CREST_GRAY
        )
        if row.description:
            body.append(row.description, style=render.FEATHER_GRAY)
        if row.source:
            body.append(f"  ({row.source})", style=render.FEATHER_GRAY)

    def _draw_footer(self, body: Text) -> None:
        if self._total > len(self._rows):
            body.append(
                f"\n  {len(self._rows)} of {self._total} — keep typing to narrow", style=render.FEATHER_GRAY
            )
