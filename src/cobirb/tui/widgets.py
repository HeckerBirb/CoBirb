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
from textual.message import Message
from textual.widgets import Input, RichLog, Static, TextArea

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
        line = Text(" · ".join(parts), style=render.FEATHER_GRAY)
        if self.busy:
            # The busy label replaces nothing; it is appended so the context
            # (persona/model/cwd) stays readable while a turn is running.
            line.append("  ")
            line.append(self.busy, style="bold yellow")
        return line


class ActivityBar(Static):
    """What is running right now, on its own line, with a moving indicator.

    ``StatusBar`` already had a ``busy`` field, and it was not enough: a dim
    suffix on a line that already carries persona, model, plan mode and cwd is
    easy to miss entirely. The report that prompted this was that Brainy Birb
    built a whole skeleton with no sign of life anywhere in CoBirb — the only
    evidence it was working was Ollama's own terminal scrolling in another
    window.

    Two things make this readable where that was not. It is a line of its own,
    so nothing competes with it. And the indicator *moves*: a static label
    cannot distinguish "working" from "hung", which is the actual question
    somebody staring at a quiet screen is asking.

    Hidden entirely when nothing is running, rather than showing "idle" — a
    permanent row that usually says nothing is a row people stop reading.
    """

    # Braille rather than ASCII: it animates in place at one cell wide, so the
    # text beside it never shifts.
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    INTERVAL = 0.1

    activity: reactive[str] = reactive("")
    detail: reactive[str] = reactive("")

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._frame = 0
        self._timer: object | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(self.INTERVAL, self._tick, pause=True)
        self.display = False

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self.refresh()

    def watch_activity(self, activity: str) -> None:
        """Start and stop the animation with the work, so an idle app is not
        repainting a spinner forever."""
        self.display = bool(activity)
        timer = self._timer
        if timer is None:
            return
        if activity:
            timer.resume()
        else:
            timer.pause()

    def render(self) -> Text:
        if not self.activity:
            return Text("")
        line = Text()
        line.append(self.FRAMES[self._frame], style="bold cyan")
        line.append(" ")
        line.append(self.activity, style="bold")
        if self.detail:
            line.append("   ")
            line.append(self.detail, style="dim")
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


class PromptInput(TextArea):
    """The message box: wraps, grows to 8 lines, then scrolls.

    A ``TextArea`` rather than an ``Input``, and the change cost two keys.
    ``PromptInput`` used to extend ``Input``, whose docstring said why that
    worked: *"``Input`` is single-line, so it binds neither arrow key itself
    and both are free to mean 'walk the history'."* A multi-line box takes
    both arrows back, and takes ``enter`` with it.

    **Enter submits; shift+enter, alt+enter and ctrl+j insert a newline.**
    Three keys for one action because shift+enter is the one people reach for
    and the one that cannot be relied on: many terminals send an identical
    escape sequence for enter and shift+enter, and on those it would submit
    with no way to type a second line at all. ``alt+enter`` and ``ctrl+j`` (a
    literal line feed, distinct from carriage return since teletypes) get
    through where it does not.

    **Up and down walk the history only from the edges.** On the first line
    ``up`` recalls, anywhere else it moves the cursor; ``down`` mirrors that on
    the last line. It is the shell convention, and people do not notice it
    happening — which is the point, since the alternative is inventing a third
    key for something that already has an obvious one.

    Growing is done here rather than in CSS because Textual sizes a
    ``TextArea`` to its container, not its content. The height comes from the
    *wrapped* line count, so one long wrapped line counts for as much as it
    occupies.
    """

    # Never taller than this. Past it the box scrolls instead of eating the
    # transcript: the thing you are writing about is worth more screen than
    # the writing.
    MAX_LINES = 8
    MIN_LINES = 1

    # Newline keys in the order they are worth trying. Named rather than
    # inlined so the help text and the placeholder can list them.
    NEWLINE_KEYS = ("shift+enter", "alt+enter", "ctrl+j")

    BINDINGS = [
        Binding("up", "history_prev", "Previous prompt", show=False),
        Binding("down", "history_next", "Next prompt", show=False),
    ]

    class Submitted(Message):
        """Enter was pressed. Carries the text, as ``Input.Submitted`` did."""

        def __init__(self, prompt: "PromptInput", value: str) -> None:
            super().__init__()
            self.input = prompt
            self.value = value

    def __init__(self, history: "PromptHistory | None" = None, **kwargs: object) -> None:
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("show_line_numbers", False)
        # `placeholder` is an Input argument; TextArea has no such thing, and
        # every existing call site passes one.
        kwargs.pop("placeholder", None)
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # `prompt_history`, not `history`: TextArea already owns an attribute
        # by that name for its undo stack, and shadowing it makes focusing the
        # widget raise. A collision worth naming, because the obvious name is
        # taken and the failure is nowhere near the assignment.
        self.prompt_history = history if history is not None else PromptHistory()
        # None means "not walking the history"; otherwise an index into it.
        self._position: int | None = None
        self._draft = ""

    # ------------------------------------------------------------------ #
    # Value, named as the old widget named it
    # ------------------------------------------------------------------ #
    @property
    def value(self) -> str:
        """The typed text.

        ``TextArea`` calls this ``text``. Keeping ``value`` too means the app
        and its tests read as they did against ``Input``, so swapping the
        widget stayed a widget swap rather than a rename across five files.
        """
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text

    # ------------------------------------------------------------------ #
    # Keys
    # ------------------------------------------------------------------ #
    async def _on_key(self, event) -> None:
        """Claim enter and the newline keys before ``TextArea`` inserts them.

        ``TextArea._on_key`` maps ``enter`` to inserting a newline, so this has
        to run ahead of it rather than binding over it.
        """
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if self.text.strip():
                self.post_message(self.Submitted(self, self.text))
            return
        if event.key in self.NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    # ------------------------------------------------------------------ #
    # Growing
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:
        self._resize()

    def _on_text_area_changed(self, event) -> None:
        self._resize()

    def on_resize(self) -> None:
        """Re-measure when the window changes: the same text wraps into a
        different number of lines at a different width."""
        self._resize()

    def _resize(self) -> None:
        try:
            wrapped = self.wrapped_document.height
        except Exception:  # noqa: BLE001 - nothing to measure before first layout
            wrapped = 1
        self.styles.height = max(self.MIN_LINES, min(self.MAX_LINES, wrapped))

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #
    def remember(self, text: str) -> None:
        """Record a just-submitted prompt and return to the live draft."""
        self.prompt_history.add(text)
        self._position = None
        self._draft = ""

    def clear(self) -> None:
        """Empty the box, and shrink it back with the text."""
        self.text = ""
        self._resize()

    @property
    def _on_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    @property
    def _on_last_line(self) -> bool:
        return self.cursor_location[0] >= self.document.line_count - 1

    def action_history_prev(self) -> None:
        """Recall on the first line; move the cursor anywhere else."""
        if not self._on_first_line:
            self.action_cursor_up()
            return
        entries = self.prompt_history.entries
        if not entries:
            return
        if self._position is None:
            self._draft = self.text
            self._position = len(entries) - 1
        elif self._position > 0:
            self._position -= 1
        else:
            return  # already at the oldest entry
        self._recall(entries[self._position])

    def action_history_next(self) -> None:
        """Mirror of the above, from the last line."""
        if not self._on_last_line:
            self.action_cursor_down()
            return
        entries = self.prompt_history.entries
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
        self.text = text
        self.move_cursor(self.document.end)
        self._resize()
