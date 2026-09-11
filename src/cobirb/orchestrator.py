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

        # cwd is per-run metadata, not conversation history, so it rides on
        # the system prompt rather than being spliced into the turn history.
        system_with_cwd = f"{system}\n\nWorking directory: {cwd}"
        context = self._build_context(session)
        logger.info("starting run; turns=%d", len(session.turns))
        self.last_turn_streamed = False
        self._stream_label = persona

        for _ in range(max_turns):
            reply, streamed = self._chat(system_with_cwd, context)

            # If the model wants to act, allow it (policy-gated) and keep going.
            tool_calls = self.model.parse_tool_calls(reply) if self.model.supports_tool_calling() else []
            if tool_calls:
                # Record the model's own decision to call these tools *before*
                # executing them, as its own assistant turn. Without this, a
                # "tool" result turn appears in the history with no assistant
                # turn requesting it — from the model's perspective on the next
                # call, no tool call ever happened, so it has no signal that
                # this one was already satisfied and may just repeat it.
                session.add(
                    Turn(
                        role="assistant",
                        content=_materialize(reply),
                        tool_use=[{"name": c.name, "arguments": c.arguments} for c in tool_calls],
                    )
                )
                self._execute_tool_calls(tool_calls)
                context = self._build_context(session)
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

    def _build_context(self, session: Session) -> str:
        """Serialize the turn history as JSON (role, content, tool_use per
        turn) so a provider can reconstruct a proper multi-turn messages
        array — see ``LocalModelProvider._build_messages`` — instead of
        every turn being flattened into a single opaque blob, which gave
        tool-calling models no reliable signal that a prior tool call was
        already satisfied.
        """
        turns = [{"role": t.role, "content": t.content, "tool_use": t.tool_use} for t in session.turns]
        return json.dumps(turns)

    # ------------------------------------------------------------------ #
    # Tool dispatch (policy-gated)
    # ------------------------------------------------------------------ #
    def _execute_tool_calls(self, tool_calls: list[cobirb_typing.ToolCall]) -> None:
        for call in tool_calls:
            self._execute_tool(call)

    def _request_approval(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Ask ``io`` whether to allow a not-yet-permitted tool call.

        Fails closed (denies) when there's no interactive adapter attached,
        it doesn't implement ``confirm`` (duck-typed test doubles), or it
        raises — there's no one to ask, so the safe answer is no.
        """
        if self.io is None or not hasattr(self.io, "confirm"):
            return "deny"
        try:
            decision = self.io.confirm(tool_name, arguments)
        except Exception:  # noqa: BLE001 - a broken adapter must not open access
            return "deny"
        return decision if decision in ("once", "always", "deny") else "deny"

    def _execute_tool(self, call: cobirb_typing.ToolCall) -> None:
        tool_name = call.name
        arguments = call.arguments
        session = self.session.session
        # Tags this result with the call it answers, so a provider building
        # a proper messages array can label the "tool" message accordingly.
        tool_use = [{"name": tool_name, "arguments": arguments}]

        # Not already permitted: ask the user rather than silently denying,
        # so "default-deny" means "asks first," not "the model never finds
        # out it could have worked." Fails closed (denies) with no adapter,
        # or one that can't ask (see I_OAdapter.confirm's contract).
        if not self.policy.is_allowed(tool_name, arguments):
            decision = self._request_approval(tool_name, arguments)
            if decision == "deny":
                session.add(
                    Turn(
                        role="tool",
                        content=f"Permission denied: tool '{tool_name}' is not permitted.",
                        tool_use=tool_use,
                    )
                )
                return
            if decision == "always":
                shell_command = arguments.get("command") if tool_name == "shell" else None
                self.policy.allow(tool_name, shell_command)

        tool = self.tools.get(tool_name)
        if tool is None:
            session.add(Turn(role="tool", content=f"Unknown tool '{tool_name}'.", tool_use=tool_use))
            return

        self.policy.log(tool_name, arguments, cwd=self.session.working_dir)
        result = tool.execute(arguments)
        session.add(Turn(role="tool", content=result.content, tool_use=tool_use))

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
