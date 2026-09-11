"""Core orchestrator: wires plugins together and drives the agent loop.

The core is intentionally thin — it does not contain feature business logic. It
connects a model provider, a tool registry, a policy layer, an I/O adapter, and a
session manager, and drives the agentic loop. See DESIGN.md §3 and §4.

The loop preserves the Copilot design but keeps it local and approval-gated:

    Prompt → understand → inspect → plan → act → observe → reason → iterate
            → validate → report
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

from .policy import Policy
from .session import Session, SessionManager, Turn
from .typing import spi as cobirb_typing

SessionCrypto = cobirb_typing.SessionCrypto

logger = logging.getLogger("cobirb")

# Steps of the agent loop, in order.
_STEPS = [
    "understand",
    "inspect",
    "plan",
    "act",
    "observe",
    "reason",
    "iterate",
    "validate",
    "report",
]


class AgentError(Exception):
    """Raised when the agent loop cannot proceed."""


def _materialize(reply: "str | Iterable[str]") -> str:
    """Return a plain string from the model's reply.

    A model may return a string or (when streaming) an iterable of tokens.
    """
    if isinstance(reply, str):
        return reply
    try:
        return "".join(reply)
    except TypeError:
        return str(reply)


class Orchestrator:
    """Drives the loop, dispatching tool calls against the policy layer."""

    def __init__(
        self,
        model: cobirb_typing.ModelProvider,
        tools: dict[str, cobirb_typing.Tool],
        policy: Policy,
        io: cobirb_typing.I_OAdapter | None = None,
        session: SessionManager | None = None,
        crypto: Any = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.policy = policy
        self.io = io
        self.crypto = crypto
        self.session = session
        # Whether the most recent run()'s final answer was already streamed
        # live to `io` (see run()'s docstring) — false until a run happens.
        self.last_turn_streamed = False
        # Overwritten by run() with the active persona name for this call.
        self._stream_label = "assistant"

    # ------------------------------------------------------------------ #
    # Public run
    # ------------------------------------------------------------------ #
    def run(
        self,
        prompt: str,
        system: str,
        *,
        cwd: str = ".",
        persona: str = "noah",
        max_turns: int = 8,
        session_path: str | None = None,
    ) -> Session:
        """Run the loop for a single objective.

        Stops as soon as the model gives a plain-text reply with no further
        tool calls (that reply becomes ``session.summary``), or after
        ``max_turns`` iterations if the model keeps calling tools without
        ever producing a final answer.

        When the model and I/O adapter both support it, the model's reply is
        streamed live to ``io`` as it arrives. ``self.last_turn_streamed`` is
        set to whether the *final* answer specifically was already shown
        this way, so a caller (e.g. the CLI) knows whether it still needs to
        print ``session.summary`` itself or would just be duplicating output.
        """
        session = self._open_session(prompt, system, cwd, persona, session_path)

        context = self._build_context(session, cwd)
        logger.info("starting run; turns=%d", len(session.turns))
        self.last_turn_streamed = False
        self._stream_label = persona

        for _ in range(max_turns):
            reply, streamed = self._chat(system, context)

            # If the model wants to act, allow it (policy-gated) and keep going.
            tool_calls = self.model.parse_tool_calls(reply) if self.model.supports_tool_calling() else []
            if tool_calls:
                self._execute_tool_calls(tool_calls)
                context = self._build_context(session, cwd)
                continue

            # No tool calls: this is the model's final answer for this turn.
            content = _materialize(reply)
            session.add(Turn(role="assistant", content=content))
            session.summary = content
            self.last_turn_streamed = streamed and bool(content)
            return session

        session.summary = f"Stopped after {max_turns} turns without a final answer."
        return session

    def _chat(self, system: str, context: str) -> tuple[str, bool]:
        """Get the model's reply for this turn, streaming it live to ``io``
        when the model supports streaming and an I/O adapter is attached.

        A label (the active persona's name, set by ``run()``) is rendered
        once, right before the first non-empty chunk of *this* turn — we
        can't know in advance whether a turn will end up being a tool call
        or the final answer, so any turn that produces visible content gets
        labeled the same way a non-streaming reply would be.

        Returns ``(content, streamed)``. Falls back to a single
        non-streaming call otherwise (including for duck-typed test doubles
        that don't implement ``supports_streaming``).
        """
        tools = list(self.tools.values())
        supports_streaming = getattr(self.model, "supports_streaming", lambda: False)()
        if self.io is None or not supports_streaming:
            return _materialize(self.model.chat(system, context, tools)), False

        chunks = []
        label_shown = False
        for chunk in self.model.chat(system, context, tools, stream=True):
            if chunk:
                if not label_shown:
                    self.io.render(f"{self._stream_label}: ")
                    label_shown = True
                self.io.render(chunk)
                chunks.append(chunk)
        if chunks:
            self.io.render("\n")
        return "".join(chunks), True

    # ------------------------------------------------------------------ #
    # Session plumbing
    # ------------------------------------------------------------------ #
    def _open_session(
        self, prompt: str, system: str, cwd: str, persona: str, session_path: str | None = None
    ) -> Session:
        # Reuse the supplied session manager when one was injected (e.g. a
        # resumed --session), so prior history and the session path/cipher
        # are preserved. Either way, this prompt is always recorded as the
        # opening turn of *this* run.
        if self.session is None:
            self.session = SessionManager.create(session_path or ".", self.crypto, cwd, persona)
        self.session.session.add(Turn(role="user", content=prompt))
        return self.session.session

    def _build_context(self, session: Session, cwd: str) -> str:
        parts = [f"Working directory: {cwd}", ""]
        for turn in session.turns:
            parts.append(f"{turn.role}: {turn.content}")
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # Tool dispatch (policy-gated)
    # ------------------------------------------------------------------ #
    def _execute_tool_calls(self, tool_calls: list[cobirb_typing.ToolCall]) -> None:
        for call in tool_calls:
            self._execute_tool(call)

    def _execute_tool(self, call: cobirb_typing.ToolCall) -> None:
        tool_name = call.name
        arguments = call.arguments
        session = self.session.session

        # Fail-closed: a denied or unknown tool is recorded in the transcript
        # and audited, never allowed to crash the run.
        if not self.policy.is_allowed(tool_name, arguments):
            session.add(Turn(role="tool", content=f"Permission denied: tool '{tool_name}' is not permitted."))
            return

        tool = self.tools.get(tool_name)
        if tool is None:
            session.add(Turn(role="tool", content=f"Unknown tool '{tool_name}'."))
            return

        self.policy.log(tool_name, arguments, cwd=self.session.working_dir)
        result = tool.execute(arguments)
        session.add(Turn(role="tool", content=result.content))

    # ------------------------------------------------------------------ #
    # Convenience: register a fresh session path
    # ------------------------------------------------------------------ #
    @property
    def session_path(self) -> str | None:
        return self.session.path if self.session else None


def build_default_policy(allowed: set[str] | None = None, denied: set[str] | None = None) -> Policy:
    """Build a policy, allowing the built-in core tools by default.

    The ``shell`` scope is narrowed to a safe default set of first words.
    """
    policy = Policy(allowed=allowed, denied=denied)
    policy.allow_all_core_tools()
    return policy
