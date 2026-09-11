"""Core I/O adapter: plain-text terminal renderer.

v0.1.0 ships only a terminal renderer. Speech and vision adapters are I/OAdapter
implementations that can be added later without touching core. See DESIGN.md §5.3.
"""
from __future__ import annotations

import sys
from typing import Any

from ...typing.spi import I_OAdapter


class TerminalIO(I_OAdapter):
    """Default v0.1.0 renderer. Renders text to stdout; captures nothing yet."""

    def name(self) -> str:
        return "terminal"

    def render(self, text: str) -> None:
        # No trailing newline: the orchestrator calls this once per streamed
        # token, so a forced newline here would put every token on its own
        # line. Callers that want a line break include it in ``text``.
        print(text, end="", flush=True)

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
