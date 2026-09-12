"""Core I/O adapter: a rich-powered terminal renderer.

This is the *scrolling* renderer — the one one-shot/programmatic mode uses,
where output is plain stdout a caller can pipe. Interactive mode is a
full-screen Textual app instead (``cobirb.tui``), whose ``TuiIO`` adapter
builds the very same panels from ``render.py`` and writes them into a
``RichLog`` rather than printing them. Speech and vision adapters are
I_OAdapter implementations that can be added later without touching core.
See DESIGN.md §5.3 and §11.
"""
from __future__ import annotations

from typing import Any

from rich.console import Console

from ...typing.spi import I_OAdapter
from . import render


class TerminalIO(I_OAdapter):
    """Default v0.1.0 renderer. Renders text to stdout; captures nothing yet.

    Beyond the ``I_OAdapter`` contract (``render``/``listen``/``view``/
    ``confirm``), this concrete adapter exposes a few extra, duck-typed
    rendering hooks — ``spinner``, ``render_header``, ``render_answer``,
    ``render_tool_call`` — that the orchestrator and CLI reach for via
    ``getattr(io, "...", None)`` when present, falling back to plain text
    otherwise. Keeping them off the abstract SPI means a future speech/
    vision adapter (or a bare-bones test double) is never forced to
    implement chrome it has no use for.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def name(self) -> str:
        return "terminal"

    def render(self, text: str) -> None:
        # No trailing newline: the orchestrator calls this once per streamed
        # token, so a forced newline here would put every token on its own
        # line. Callers that want a line break include it in ``text``.
        # markup/highlight are off: this is arbitrary, possibly-partial
        # model output, not authored rich markup — a stray "[" from the
        # model must never be parsed as (or crash on) a markup tag.
        self._console.print(text, end="", markup=False, highlight=False, soft_wrap=True)
        self._console.file.flush()

    def listen(self) -> str | None:
        """v0.1.0 input path is the terminal itself; no extra channel to listen on."""
        return None

    def view(self, data: bytes, mime: str | None = None) -> None:
        """No-op in v0.1.0. Vision rendering will be added in v0.2.0."""
        return None

    def confirm(self, tool_name: str, arguments: dict[str, Any]) -> str:
        detail = arguments.get("command") or arguments.get("path") or ""
        suffix = f" ({detail})" if detail else ""
        prompt = f"\nAllow '{tool_name}'{suffix}? [y]es / [a]lways this session / [N]o: "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Fail closed: no one to ask (closed/non-interactive stdin, or
            # Ctrl-C) means deny, not hang or guess yes.
            return "deny"
        if answer in ("y", "yes"):
            return "once"
        if answer in ("a", "always"):
            return "always"
        return "deny"

    # ------------------------------------------------------------------ #
    # Extra chrome. Not part of I_OAdapter — reached for via getattr, with
    # a plain-text fallback wherever these are called from.
    # ------------------------------------------------------------------ #
    def spinner(self, label: str):
        """A context manager showing a spinner around a blocking call (e.g.
        waiting on the model for its first token). See
        ``Orchestrator._chat``. Degrades to a static line on a non-tty."""
        return self._console.status(label, spinner="dots")

    def render_header(
        self, persona_name: str, model_name: str, cwd: str, session_path: str | None = None
    ) -> None:
        """A compact banner shown once at the top of a session."""
        self._console.print(render.build_header_panel(persona_name, model_name, cwd, session_path))

    def render_answer(self, persona_name: str, text: str) -> None:
        """The assistant's finished reply, rendered as markdown in a panel.

        Used for a reply that arrives whole (not streamed) — a streamed
        reply was already shown token-by-token via ``render`` and would
        just be duplicated here. See ``Orchestrator.last_turn_streamed``.
        """
        if not text:
            return
        self._console.print(render.build_answer_panel(persona_name, text))

    def render_plan(self, persona_name: str, text: str) -> None:
        """Plan mode's planning-phase reply (see
        ``Orchestrator._run_plan_phase``): the plan the model is about to
        follow, shown to the user before any tool runs."""
        if not text:
            return
        self._console.print(render.build_plan_panel(persona_name, text))

    def render_validation(self, persona_name: str, text: str) -> None:
        """Plan mode's validate-phase report (see ``Orchestrator.run``):
        whether/how the request was actually fulfilled, with references."""
        if not text:
            return
        self._console.print(render.build_validation_panel(persona_name, text))

    def render_tool_call(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        """A tool call and its result, set apart from the conversation so
        the flow of "assistant reasons → calls a tool → sees the result" is
        visually distinct rather than buried in a single wall of text.

        ``apply_patch``/``edit_file`` calls get their diff syntax-
        highlighted; everything else shows the tool's own result text.
        """
        self._console.print(render.build_tool_call_panel(tool_name, arguments, result))
