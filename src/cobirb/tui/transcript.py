"""Everything that puts text into the transcript, in the right order.

One job, and an ordering rule that the app kept getting handed responsibility
for: a streamed reply lives in the preview widget until something else needs
to be written, at which point it has to be committed *first* or the transcript
reads out of order — the panel for a tool call appearing above the reasoning
that led to it. Every writer here flushes before it writes, which is why they
belong together rather than scattered across the application class.

Deliberately knows nothing about the application's state. It holds a query
root so it can find its two widgets, and takes everything else — the persona
name, the attachments on a prompt — as arguments. A view that reads the app's
fields would have to change whenever they do; this one changes when the
*transcript* changes.
"""
from __future__ import annotations

from typing import Any, Iterable

from rich.text import Text

from ..plugins.core import render
from .widgets import StreamPreview, TranscriptLog


class TranscriptView:
    """The conversation as the user reads it.

    ``root`` is any Textual node that can ``query_one`` its way to the
    transcript — in practice the app. Looked up per call rather than cached
    at construction, because the widgets do not exist until mount and a view
    built earlier would hold stale handles after one is replaced.
    """

    def __init__(self, root: Any) -> None:
        self._root = root

    @property
    def _log(self) -> TranscriptLog:
        return self._root.query_one("#transcript", TranscriptLog)

    @property
    def _preview(self) -> StreamPreview:
        return self._root.query_one("#streaming-preview", StreamPreview)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def write(self, renderable: Any) -> None:
        """Append a finished renderable.

        Flushes the streaming preview first so the reading order matches the
        order things happened: the model's streamed reasoning, and only then
        the panel for the tool call it led to.
        """
        self.flush_stream()
        self._log.write(renderable)

    def append_stream(self, text: str) -> None:
        self._preview.append(text)

    def flush_stream(self) -> None:
        """Move anything buffered mid-stream into the transcript for good.

        A streamed final answer is never re-rendered as a panel (the
        orchestrator sets ``last_turn_streamed`` precisely so it isn't shown
        twice), so if this didn't run the reply would vanish from the
        transcript when the preview cleared.
        """
        text = self._preview.take()
        if text.strip():
            # Marked the same way a non-streamed reply is, so the transcript
            # reads uniformly whether or not the model streamed it.
            self._log.write(render.build_streamed_message(text.rstrip("\n")))

    def write_user_prompt(self, prompt: str, attachments: Iterable[str] = ()) -> None:
        """Append what the user just sent, fenced by blank lines.

        The blank line on each side is the point: without it a prompt sat
        flush against the panel above and the reply below, and the whole
        transcript read as one undifferentiated column. Spacing here rather
        than inside ``build_user_message`` keeps the renderable itself
        composable — ``TerminalIO`` spaces its own output with newlines.

        ``attachments`` are the filenames riding along with this message,
        marked under it so the transcript shows what was actually sent.
        """
        self.flush_stream()
        log = self._log
        log.write(Text(""))
        log.write(render.build_user_message(prompt))
        for filename in attachments:
            log.write(Text(f"\U0001f4ce {filename}", style="dim"))
        log.write(Text(""))

    # ------------------------------------------------------------------ #
    # Replaying a resumed conversation
    # ------------------------------------------------------------------ #
    def render_history(self, turns: list[Any], label: str, persona_name: str) -> None:
        """Replay a resumed conversation into the transcript.

        Without this, resuming drops you into an empty screen: the
        conversation is loaded and fed to the model, so it knows what was
        said, while you can see none of it. A session you can't read is most
        of the reason not to keep one.

        Bracketed by dim rules so restored turns are never mistaken for
        something that just happened.
        """
        log = self._log
        if not turns:
            log.write(render.build_notice(f"{label} — no turns yet; your next message starts it."))
            return
        log.write(Text(""))
        log.write(render.build_history_divider(f"{label} · {len(turns)} earlier turn(s)"))
        for turn in turns:
            self._replay_turn(log, turn, persona_name)
        log.write(Text(""))
        log.write(render.build_history_divider("end of restored history"))

    def _replay_turn(self, log: TranscriptLog, turn: Any, persona_name: str) -> None:
        """Write one saved turn, matching how it looked when it happened."""
        role = getattr(turn, "role", "")
        content = getattr(turn, "content", "") or ""
        tool_use = getattr(turn, "tool_use", None)
        phase = getattr(turn, "phase", None)

        if role == "tool":
            # The tool turn records the result; the call that produced it is
            # on the turn itself, so one panel shows both.
            call = (tool_use or [{}])[0]
            log.write(
                render.build_tool_call_panel(
                    call.get("name", "?"), call.get("arguments", {}) or {}, content, replayed=True
                )
            )
            return

        if role == "user":
            log.write(Text(""))
            log.write(render.build_user_message(content))
            log.write(Text(""))
            return

        if not content.strip():
            # An assistant turn whose only purpose was to announce a tool
            # call — the call itself is rendered by the tool turn that
            # follows, so an empty bubble here would be noise.
            return
        if phase == "plan":
            log.write(render.build_plan_panel(persona_name, content))
        elif phase == "validate":
            log.write(render.build_validation_panel(persona_name, content))
        else:
            log.write(render.build_assistant_message(content))
