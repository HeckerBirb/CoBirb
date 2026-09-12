"""Small widgets for the CoBirb TUI: the live status bar, the streaming token
preview, and the prompt input with its recall history.

They are deliberately dumb — they hold display state and nothing else. Every
decision about *when* to change them lives in ``app.py``, and every update
reaches them on Textual's main thread (see ``io_bridge.TuiIO``).
"""
from __future__ import annotations

import os

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.reactive import reactive
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Input, RichLog, Static

from ..plugins.core import render


class StatusBar(Static):
    """One persistent line: who's speaking, on what model, with plan mode on
    or off, in which directory.

    This is the CoBirb equivalent of the status line in a Copilot-style CLI,
    minus its "AI credits used" counter — CoBirb is local and free, so there
    is nothing to meter. It is kept *live* rather than printed once at
    startup, so a ``/persona`` or ``/plan`` change is visible for the rest of
    the session instead of scrolling away.
    """

    persona_name: reactive[str] = reactive("")
    model_name: reactive[str] = reactive("")
    plan_mode: reactive[bool] = reactive(False)
    cwd: reactive[str] = reactive(".")
    session_path: reactive[str | None] = reactive(None)
    busy: reactive[str] = reactive("")

    def render(self) -> Text:
        parts = [
            self.persona_name or "cobirb",
            self.model_name or "(no model configured)",
            f"plan: {'on' if self.plan_mode else 'off'}",
            self.cwd,
        ]
        if self.session_path:
            # Just the file name: this is one line competing with a cwd
            # that is often long already, and the full path is in the
            # header panel at the top of the transcript.
            parts.append(f"session: {os.path.basename(self.session_path)}")
        line = Text(" · ".join(parts), style="dim")
        if self.busy:
            # The busy label replaces nothing; it is appended so the context
            # (persona/model/cwd) stays readable while a turn is running.
            line.append("  ")
            line.append(self.busy, style="bold yellow")
        return line


class StreamPreview(Static):
    """Buffers the model's reply while it is still streaming in.

    The finished transcript is a ``RichLog``, which appends each write as new
    lines and can't rewrite the last one — so per-token output can't go there
    directly. Tokens accumulate here as plain text instead (exactly how the
    scrolling ``TerminalIO`` shows a stream: raw, unparsed, not markdown),
    and ``take()`` hands the completed text over to the transcript once the
    stream ends.

    It carries the same ``>`` marker a finished reply gets, so a reply
    doesn't visibly shift left when the stream ends and the text moves into
    the transcript.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__("", **kwargs)  # type: ignore[arg-type]
        self._buffer: list[str] = []

    @property
    def buffered(self) -> str:
        return "".join(self._buffer)

    def append(self, text: str) -> None:
        self._buffer.append(text)
        self.update(render.build_streamed_message(self.buffered))
        self.display = True

    def take(self) -> str:
        """Return everything buffered and reset to empty/hidden."""
        text = self.buffered
        self._buffer.clear()
        self.update("")
        self.display = False
        return text


# Two independent ceilings, both from the brief: keep the most recent 100
# prompts, and never let the history hold more than 100 MiB. Whichever one a
# session hits first, the oldest entries are dropped to get back under it —
# so a session of ordinary one-line prompts is bounded by the count, and one
# that pastes large blobs into the box is bounded by the bytes.
_HISTORY_MAX_ENTRIES = 100
_HISTORY_MAX_BYTES = 100 * 1024 * 1024


class PromptHistory:
    """The prompts typed this session, newest last.

    In memory only, and deliberately so: CoBirb's privacy rule is that
    anything capable of capturing what you type is opt-in and encrypted, and
    a recall history written to disk would be a second, plaintext copy of
    every prompt. This one dies with the process.
    """

    def __init__(
        self,
        max_entries: int = _HISTORY_MAX_ENTRIES,
        max_bytes: int = _HISTORY_MAX_BYTES,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: list[str] = []
        self._bytes = 0

    @property
    def entries(self) -> list[str]:
        """A copy — callers index into this while navigating, and must not be
        able to mutate the history by doing so."""
        return list(self._entries)

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, text: str) -> None:
        """Record a submitted prompt, evicting the oldest to stay in budget.

        Blank submissions and an immediate repeat of the previous prompt are
        dropped rather than stored: re-sending the same thing twice is common
        and filling the history with it would push out entries you'd actually
        want to recall.
        """
        text = text.rstrip("\n")
        if not text.strip():
            return
        if self._entries and self._entries[-1] == text:
            return
        self._entries.append(text)
        self._bytes += len(text.encode("utf-8"))
        while self._entries and (
            len(self._entries) > self.max_entries or self._bytes > self.max_bytes
        ):
            self._bytes -= len(self._entries.pop(0).encode("utf-8"))


class TranscriptLog(RichLog):
    """The conversation transcript — a ``RichLog`` that can be selected and
    copied out of.

    A stock ``RichLog`` cannot: dragging across one selects nothing and the
    copy key comes back empty. Textual's selection machinery needs two things
    from a widget, and ``RichLog`` provides neither.

    1. **Position metadata on rendered segments.** The compositor turns a
       mouse position into a character offset by reading an ``offset`` entry
       in each segment's style metadata. ``RichLog`` emits raw Rich segments
       with no such entry, so every drag inside it resolved to "no offset at
       this point" and the screen fell back to its coarse whole-widget path.
       ``render_line`` below tags each line via ``Strip.apply_offsets``.
    2. **A ``get_selection`` that can extract the text.** The default one
       only understands a widget rendering to a single ``Text``/``Content``;
       a ``RichLog`` renders to a list of already-rendered ``Strip``s, so it
       returned ``None`` — which ``Screen.get_selected_text`` reads as "this
       widget has no text", leaving the clipboard empty even when a
       selection did exist.

    Drawing the highlight is this class's job for the same underlying reason:
    selection styling is applied inside the visual-rendering path that a
    ``RichLog``'s pre-rendered strips never travel through.

    Offsets here are *character* offsets while ``Strip`` cuts are *cell*
    positions. They agree for everything the transcript normally holds
    (text, box drawing, diffs); a double-width character can shift the
    highlight by a cell, which is cosmetic — the text that actually gets
    copied is extracted from the same character offsets the selection was
    built from, so it stays correct either way.
    """

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        line_index = scroll_y + y
        strip = super().render_line(y)
        selection = self.text_selection
        if selection is not None:
            span = selection.get_span(line_index)
            if span is not None:
                strip = self._highlight(strip, *span)
        return strip.apply_offsets(scroll_x, line_index)

    def _highlight(self, strip: Strip, start: int, end: int) -> Strip:
        """Paint ``start``..``end`` of one line in the selection colour.

        ``end`` of -1 is Textual's "to the end of the line" — used for every
        line of a multi-line selection except the last.
        """
        cell_length = strip.cell_length
        end = cell_length if end == -1 else min(end, cell_length)
        start = min(start, cell_length)
        if start >= end:
            return strip
        before, selected, after = strip.divide([start, end, cell_length])
        # ``post_style``, not ``Strip.apply_style``: that one applies a style
        # underneath each segment's own, so the theme background every line
        # already carries would win and the selection would be invisible.
        # A post style is layered on top, which is what a highlight is.
        #
        # Background only, and deliberately so. The theme defines
        # ``screen--selection`` as a translucent background over a
        # *transparent* foreground, and flattening that to a Rich style
        # resolves the transparent foreground to the very same colour as the
        # background — so applying the whole style painted selected text as a
        # solid block you could not read. Taking just the background leaves
        # every segment's own colour intact, which is what highlighting
        # means: same text, marked.
        highlight = Style(bgcolor=self.screen.get_component_rich_style("screen--selection").bgcolor)
        highlighted = Strip(
            Segment.apply_style(list(selected), None, post_style=highlight), selected.cell_length
        )
        return Strip.join([before, highlighted, after])

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract the selected text from the log's own rendered lines.

        Lines are right-stripped first: ``RichLog`` pads every stored line
        out to the full width, and without this a selection spanning several
        lines would come back with a tail of spaces on each one.
        """
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"


class PromptInput(Input):
    """The message box, with shell-style up/down recall.

    ``Input`` is single-line, so it binds neither arrow key itself and both
    are free to mean "walk the history" without taking anything away.

    Navigation keeps a cursor into the history plus the half-typed draft the
    user was on when they started walking, so arrowing all the way back down
    returns what they had rather than leaving the box on the newest history
    entry.
    """

    BINDINGS = [
        Binding("up", "history_prev", "Previous prompt", show=False),
        Binding("down", "history_next", "Next prompt", show=False),
    ]

    def __init__(self, history: PromptHistory | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.history = history if history is not None else PromptHistory()
        # None means "not walking the history"; otherwise an index into it.
        self._position: int | None = None
        self._draft = ""

    def remember(self, text: str) -> None:
        """Record a just-submitted prompt and return to the live draft."""
        self.history.add(text)
        self._position = None
        self._draft = ""

    def action_history_prev(self) -> None:
        entries = self.history.entries
        if not entries:
            return
        if self._position is None:
            self._draft = self.value
            self._position = len(entries) - 1
        elif self._position > 0:
            self._position -= 1
        else:
            return  # already at the oldest entry
        self._recall(entries[self._position])

    def action_history_next(self) -> None:
        entries = self.history.entries
        if self._position is None:
            return  # not walking the history; nothing newer to go to
        if self._position < len(entries) - 1:
            self._position += 1
            self._recall(entries[self._position])
            return
        self._position = None
        self._recall(self._draft)
        self._draft = ""

    def _recall(self, text: str) -> None:
        self.value = text
        self.cursor_position = len(text)
