"""Core orchestrator: wires plugins together and drives the agent loop.

The core is intentionally thin — it does not contain feature business logic. It
connects a model provider, a tool registry, a policy layer, an I/O adapter, and a
session manager, and drives the agentic loop.

The loop preserves the Copilot design but keeps it local and approval-gated:

    Prompt → understand → inspect → plan → act → observe → reason → iterate
            → validate → report
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable

from .policy import AuditLog, Policy
from .session import Session, SessionManager, Turn
from .typing import spi as cobirb_typing

logger = logging.getLogger("cobirb")

# Sentinel distinguishing "no chunk yet" from a real (possibly empty-string)
# streamed chunk when peeking the first item of a model's stream in _chat().
_STREAM_EMPTY = object()

# System-prompt addenda for plan mode's three phases (Orchestrator.run,
# plan_mode=True). Off by default — the model plans, acts and validates
# implicitly within one continuous loop, the way someone working through a
# task does, with no hard stop between thinking and doing. On, each phase
# gets its own model call(s) and its own labeled Turn(s) (see Turn.phase),
# for anyone who wants those checkpoints made explicit.
_PLAN_PHASE_INSTRUCTIONS = (
    "PLANNING PHASE. Do not call any tools and do not attempt the task itself yet — "
    "no tools are available to you for this reply. Write a short, numbered plan "
    "describing the concrete steps you will take to fulfill the user's request above. "
    "This plan will be shown to the user now and stays in the conversation history for "
    "your own reference in the next phase."
)
_ACT_PHASE_INSTRUCTIONS = (
    "ACT PHASE. A plan for this request was already produced in the assistant turn "
    "above — follow it, adapting as needed if what you find while working contradicts "
    "it. Use tools as needed to complete the user's request."
)
_VALIDATE_PHASE_INSTRUCTIONS = (
    "VALIDATION PHASE. The task above is believed complete. Verify that it actually "
    "was: re-read changed files, re-run any relevant tests or commands, or otherwise "
    "check your own work using the available tools. Then report, with concrete "
    "references (file paths, line numbers, command/test output), how and why the "
    "user's request was successfully fulfilled. If it was not fully fulfilled, say so "
    "plainly and explain what remains — do not claim success you can't back up."
)


def _join_system(*parts: str) -> str:
    """Join system-prompt blocks, dropping empty ones.

    Empties are dropped rather than joined so that a run with no CoBirb
    prompt of its own doesn't produce one made entirely of blank lines: a
    system message of ``"\\n\\nACT PHASE..."`` is still a system message, and
    still replaces the model's own Modelfile ``SYSTEM`` directive. When every
    part is empty the result is ``""``, which the provider reads as "send no
    system message at all".
    """
    return "\n\n".join(part for part in parts if part and part.strip())


def render_through(
    io: Any, hook: str, *args: Any, fallback: Callable[[], None] | None = None
) -> bool:
    """Show something via ``io``'s optional ``hook``, else via ``fallback``.

    The I/O adapter contract is four methods, but both shipped adapters also
    expose a set of richer, duck-typed rendering hooks (``render_answer``,
    ``render_tool_call``, ``render_plan``, ...). Anything that wants one has
    to probe for it and cope with its absence, and that probe had been
    written out five times across three modules, each with a slightly
    different fallback and a docstring explaining why it wasn't one of the
    others.

    ``fallback`` is a callable supplied by the caller rather than a format
    string decided here: what a hookless adapter should be shown is the
    caller's business, and the core has no opinion about how a reply looks.

    Returns whether anything was actually shown, so a caller can tell whether
    it still needs to display the text some other way.
    """
    if io is None:
        return False
    render_hook = getattr(io, hook, None)
    if callable(render_hook):
        render_hook(*args)
        return True
    if fallback is not None:
        fallback()
        return True
    return False


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
        plan_mode: bool = False,
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

        With ``plan_mode=True`` (config/``/plan`` — see cli.py), the single
        act loop below is bracketed by two extra model calls instead of
        being the whole run: a **plan** phase first (one reply, no tools,
        recorded as a "plan"-phase turn and shown to the user immediately),
        then the same act loop as always (now "act"-phase turns, following
        the plan), then a **validate** phase (its own bounded tool-using
        loop, "validate"-phase turns) that checks and reports on the result
        in ``session.validation``. Off (the default), the model plans/acts/
        validates implicitly in one pass, as before.

        In plan mode specifically, the act phase's own answer is also shown
        live here (via ``_render_answer``) rather than left to the caller's
        usual post-``run()`` print (see ``cli._render_final_answer``): the
        validate phase renders its own report *before* ``run()`` returns, so
        if the act answer were left for the caller to print afterward, the
        user would read the validation of a request before ever seeing what
        the answer to it was. ``last_turn_streamed`` is set accordingly so
        that post-``run()`` print becomes a no-op rather than a duplicate.
        """
        session = self._open_session(prompt, system, cwd, persona, session_path)

        # cwd is per-run metadata, not conversation history, so it rides on
        # the system prompt rather than being spliced into the turn history.
        #
        # An empty `system` stays empty. That is how the provider is told to
        # send no system message at all and leave the model's own Modelfile
        # SYSTEM directive in force (see LocalModelProvider.compose_system) —
        # appending a working-directory line unconditionally would turn
        # "CoBirb adds nothing" into a one-line system prompt that silently
        # replaced the user's own. The tools resolve relative paths against
        # cwd themselves, so nothing breaks without the line; the model just
        # isn't told up front which directory it is in.
        system_with_cwd = f"{system}\n\nWorking directory: {cwd}" if system else ""
        logger.info("starting run; turns=%d plan_mode=%s", len(session.turns), plan_mode)
        self.last_turn_streamed = False
        self._stream_label = persona

        if plan_mode:
            plan_text, plan_streamed = self._run_plan_phase(
                _join_system(system_with_cwd, _PLAN_PHASE_INSTRUCTIONS), session
            )
            if not plan_streamed:
                self._render_phase("plan", persona, plan_text)

        act_system = (
            _join_system(system_with_cwd, _ACT_PHASE_INSTRUCTIONS) if plan_mode else system_with_cwd
        )
        content, streamed = self._loop(act_system, session, max_turns, phase="act" if plan_mode else None)
        session.summary = content
        self.last_turn_streamed = streamed

        if plan_mode:
            # Show the answer now — before validating it — rather than
            # leaving it to the caller's usual post-run() print, which
            # would land after the validate phase's own panel below and
            # read as "validated, then here's what was validated."
            self.last_turn_streamed = streamed or self._render_answer(persona, content)

            validation_text, validation_streamed = self._loop(
                _join_system(system_with_cwd, _VALIDATE_PHASE_INSTRUCTIONS),
                session,
                max_turns=4,
                phase="validate",
            )
            session.validation = validation_text
            if not validation_streamed:
                self._render_phase("validation", persona, validation_text)

        return session

    def _render_answer(self, persona_name: str, text: str) -> bool:
        """Show a finished answer live, via ``io``'s ``render_answer`` hook
        if it has one (the same hook ``cli._render_final_answer`` uses for
        a normal, non-plan-mode run), else a plain fallback through
        ``render()``. Only used directly by ``run()`` for plan mode's act
        phase (see its docstring for why); a normal run leaves rendering
        the final answer to the caller instead. Returns whether anything
        was actually shown (``False`` with no ``io`` attached), so the
        caller knows whether it still needs to show the text some other way.
        """
        if not text:
            return False
        return render_through(
            self.io,
            "render_answer",
            persona_name,
            text,
            fallback=lambda: self.io.render(f"\n{persona_name}: {text}\n"),
        )

    def _run_plan_phase(self, system: str, session: Session) -> tuple[str, bool]:
        """One reply with no tools offered — the model can only think out
        loud, never act. Recorded as its own "plan"-phase turn."""
        context = self._build_context(session)
        reply, streamed = self._chat(system, context, tools=[])
        content = _materialize(reply)
        session.add(Turn(role="assistant", content=content, phase="plan"))
        return content, streamed and bool(content)

    def _loop(
        self,
        system: str,
        session: Session,
        max_turns: int,
        phase: str | None,
        tools: "list[cobirb_typing.Tool] | None" = None,
    ) -> tuple[str, bool]:
        """Drive one bounded model<->tool loop until the model gives a
        plain final answer, or ``max_turns`` is exhausted (a synthetic
        "stopped after" message is returned instead, matching the un-added,
        summary-only behavior the plain loop always had — see
        ``run()``'s history before plan mode existed). Used both for the
        normal act loop and, in plan mode, the validate phase too.
        """
        context = self._build_context(session)
        for _ in range(max_turns):
            reply, streamed = self._chat(system, context, tools)

            # If the model wants to act, allow it (policy-gated) and keep going.
            tool_calls = (
                self.model.parse_tool_calls(reply)
                if tools != [] and self.model.supports_tool_calling()
                else []
            )
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
                        phase=phase,
                    )
                )
                self._execute_tool_calls(tool_calls, phase=phase)
                context = self._build_context(session)
                continue

            # No tool calls: this is the model's final answer for this turn.
            content = _materialize(reply)
            session.add(Turn(role="assistant", content=content, phase=phase))
            return content, streamed and bool(content)

        return f"Stopped after {max_turns} turns without a final answer.", False

    def _spun(self, label: str, fn: Callable[[], Any]) -> Any:
        """Run ``fn()``, showing ``io``'s spinner (if it has one) around the
        call. Waiting on the model is the one genuinely unpredictable
        latency in the loop — everything else (tool execution) is local.
        Falls back to calling ``fn()`` directly for an ``io`` with no
        ``spinner`` hook (including ``None`` and duck-typed test doubles),
        matching the ``supports_streaming`` duck-typing just below.
        """
        spin = getattr(self.io, "spinner", None) if self.io is not None else None
        if not callable(spin):
            return fn()
        with spin(label):
            return fn()

    def _chat(
        self, system: str, context: str, tools: "list[cobirb_typing.Tool] | None" = None
    ) -> tuple[str, bool]:
        """Get the model's reply for this turn, streaming it live to ``io``
        when the model supports streaming and an I/O adapter is attached.

        ``tools`` defaults to every registered tool (``self.tools``); pass
        an explicit ``[]`` to offer none — used by plan mode's planning
        phase, which must not be able to act at all (see
        ``Orchestrator._run_plan_phase``).

        A label (the active persona's name, set by ``run()``) is rendered
        once, right before the first non-empty chunk of *this* turn — we
        can't know in advance whether a turn will end up being a tool call
        or the final answer, so any turn that produces visible content gets
        labeled the same way a non-streaming reply would be. A spinner (see
        ``_spun``) covers the wait for that first chunk (or the whole call,
        when not streaming) so the terminal isn't just idle while the model
        thinks.

        Returns ``(content, streamed)``. Falls back to a single
        non-streaming call otherwise (including for duck-typed test doubles
        that don't implement ``supports_streaming``).
        """
        if tools is None:
            tools = list(self.tools.values())
        supports_streaming = getattr(self.model, "supports_streaming", lambda: False)()
        label = f"{self._stream_label} is thinking…"
        if self.io is None or not supports_streaming:
            reply = self._spun(label, lambda: self.model.chat(system, context, tools))
            return _materialize(reply), False

        stream = iter(self.model.chat(system, context, tools, stream=True))
        first = self._spun(label, lambda: next(stream, _STREAM_EMPTY))

        chunks: list[str] = []
        started = False

        def _emit(chunk: str) -> None:
            nonlocal started
            if chunk:
                if not started:
                    # Tell the adapter a reply is starting and let *it* decide
                    # what to draw. This used to render f"{label}: " straight
                    # into the stream, which meant the persona name became
                    # part of the text the renderer received — so once replies
                    # were marked with "> " the transcript read
                    # "> CoBirb: hello" instead of "> hello". Chrome is the
                    # adapter's business; the orchestrator only knows *when*
                    # the first token arrived, which is the one thing an
                    # adapter can't work out for itself.
                    begin = getattr(self.io, "begin_stream", None)
                    if callable(begin):
                        begin(self._stream_label)
                    started = True
                self.io.render(chunk)
                chunks.append(chunk)

        if first is not _STREAM_EMPTY:
            _emit(first)
        for chunk in stream:
            _emit(chunk)
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
    def _execute_tool_calls(self, tool_calls: list[cobirb_typing.ToolCall], phase: str | None = None) -> None:
        for call in tool_calls:
            self._execute_tool(call, phase)

    def _request_approval(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Ask ``io`` whether to allow a not-yet-permitted tool call.

        An adapter may implement the optional ``confirm_scoped`` hook to
        receive a plain-language description of what "always" would actually
        grant (``Policy.describe_grant``) — approving a read widens access to
        a whole directory tree, and a prompt that can't say so is asking the
        user to agree to something it hasn't told them. Adapters that only
        implement the documented ``confirm`` still work, they just ask the
        narrower question.

        Fails closed (denies) when there's no interactive adapter attached,
        it doesn't implement ``confirm`` (duck-typed test doubles), or it
        raises — there's no one to ask, so the safe answer is no.
        """
        if self.io is None or not hasattr(self.io, "confirm"):
            return "deny"
        try:
            confirm_scoped = getattr(self.io, "confirm_scoped", None)
            if callable(confirm_scoped):
                decision = confirm_scoped(
                    tool_name, arguments, self.policy.describe_grant(tool_name, arguments)
                )
            else:
                decision = self.io.confirm(tool_name, arguments)
        except Exception:  # noqa: BLE001 - a broken adapter must not open access
            return "deny"
        return decision if decision in ("once", "always", "deny") else "deny"

    def _execute_tool(self, call: cobirb_typing.ToolCall, phase: str | None = None) -> None:
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
                result = cobirb_typing.ToolResult(
                    ok=False, content=f"Permission denied: tool '{tool_name}' is not permitted."
                )
                session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
                self._render_tool_call(tool_name, arguments, result)
                return
            if decision == "always":
                # What "always" widens to is the policy's decision, not the
                # orchestrator's — a read grants a directory, a shell call
                # grants its invocation, everything else grants the tool.
                self.policy.grant(tool_name, arguments)

        tool = self.tools.get(tool_name)
        if tool is None:
            result = cobirb_typing.ToolResult(ok=False, content=f"Unknown tool '{tool_name}'.")
            session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
            self._render_tool_call(tool_name, arguments, result)
            return

        self.policy.log(tool_name, arguments, cwd=self.session.working_dir)
        try:
            result = tool.execute(arguments)
        except Exception as exc:  # noqa: BLE001 - a tool must not abort the run
            # A tool raising is routine, not fatal: models regularly emit a
            # mistyped or missing argument (``{"file": ...}`` instead of
            # ``{"path": ...}``), which most tools surface as a KeyError.
            # Report it as a failed tool result so the model can see what
            # went wrong and correct itself on the next turn, rather than
            # tearing down the whole run over a recoverable mistake.
            result = cobirb_typing.ToolResult(
                ok=False, content=f"Tool '{tool_name}' failed: {type(exc).__name__}: {exc}"
            )
            session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
            self._render_tool_call(tool_name, arguments, result)
            return
        session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
        self._render_tool_call(tool_name, arguments, result)

    def _render_tool_call(
        self, tool_name: str, arguments: dict[str, Any], result: cobirb_typing.ToolResult
    ) -> None:
        """Show a tool call and its result live, via ``io``'s
        ``render_tool_call`` hook if it has one (see
        ``TerminalIO.render_tool_call``), else a plain one-line fallback
        through ``render()``. No-op with no ``io`` attached — there's
        nowhere to show it, and the session history already has it.
        """
        status = "ok" if result.ok else "failed"
        render_through(
            self.io,
            "render_tool_call",
            tool_name,
            arguments,
            result,
            fallback=lambda: self.io.render(f"\n[{tool_name}: {status}] {result.content}\n"),
        )

    def _render_phase(self, phase: str, persona_name: str, text: str) -> None:
        """Show a plan-mode phase's result (plan or validation report) live,
        via ``io``'s ``render_plan``/``render_validation`` hook if it has
        one (see ``TerminalIO``), else a plain fallback through
        ``render()``. Not called when the phase's own reply already
        streamed live (see ``run()``) — that would just duplicate it. The
        act phase's own final answer is handled separately by the CLI (see
        ``cli._render_final_answer``), not here.
        """
        if not text:
            return
        render_through(
            self.io,
            "render_plan" if phase == "plan" else "render_validation",
            persona_name,
            text,
            fallback=lambda: self.io.render(f"\n[{phase}] {text}\n"),
        )

    # ------------------------------------------------------------------ #
    # Convenience: register a fresh session path
    # ------------------------------------------------------------------ #
    @property
    def session_path(self) -> str | None:
        return self.session.path if self.session else None


def build_default_policy(
    allowed: set[str] | None = None,
    denied: set[str] | None = None,
    audit_log_enabled: bool = False,
    cwd: str | None = None,
) -> Policy:
    """Build the starting policy for a run, which allows **nothing**.

    There is deliberately no pre-approved set. An earlier version of this
    granted the seven file tools outright plus a handful of shell binaries
    with any arguments, which made "default-deny" untrue in the one direction
    that matters: ``git`` with any arguments included ``git push``, and
    ``find`` with any arguments included ``-exec``. Every capability now
    arrives from the user — an approval prompt, ``allow_tools`` in config, or
    ``--allow-tool``.

    ``cwd`` must match the working directory the tools resolve paths against,
    or a directory-scoped read approval will be compared against the wrong
    tree (see ``Policy._resolve``).

    ``audit_log_enabled`` is off unless explicitly turned on (``"audit_log":
    true`` in config — see ``cobirb help config`` and ``AuditLog``'s own
    docstring for why): the audit trail would otherwise duplicate file
    contents, diffs, and shell commands into an unencrypted log every run,
    regardless of anyone ever asking for one.
    """
    return Policy(
        allowed=allowed,
        denied=denied,
        audit=AuditLog(enabled=audit_log_enabled),
        cwd=cwd,
    )
