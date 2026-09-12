"""Small widgets for the CoBirb TUI: the live status bar and the streaming
token preview.

Both are deliberately dumb — they hold display state and nothing else. Every
decision about *when* to change them lives in ``app.py``, and every update
reaches them on Textual's main thread (see ``io_bridge.TuiIO``).
"""
from __future__ import annotations

import os

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


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
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__("", **kwargs)  # type: ignore[arg-type]
        self._buffer: list[str] = []

    @property
    def buffered(self) -> str:
        return "".join(self._buffer)

    def append(self, text: str) -> None:
        self._buffer.append(text)
        self.update(Text(self.buffered))
        self.display = True

    def take(self) -> str:
        """Return everything buffered and reset to empty/hidden."""
        text = self.buffered
        self._buffer.clear()
        self.update("")
        self.display = False
        return text
