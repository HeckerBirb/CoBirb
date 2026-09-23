"""Core orchestrator: wires plugins together and drives the agent loop.

The core is intentionally thin — it does not contain feature business logic. It
connects a model provider, a tool registry, a policy layer, an I/O adapter, and a
session manager, and drives the agentic loop.

The loop keeps the familiar agentic coding-assistant shape, but local and approval-gated:

    Prompt → understand → inspect → plan → act → observe → reason → iterate
            → validate → report
"""
from __future__ import annotations

import json
import logging
import queue
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Callable, Iterable

from .checkpoints import Checkpoints
from .context import DEFAULT_CONTEXT_TOKENS, CompactionReport, compact, history_budget
from .policy import READ_TOOLS, AuditLog, Policy, SessionGrants
from .redaction import redact
from .runtime.hooks import (
    EVENT_AFTER_TOOL,
    EVENT_AFTER_TURN,
    EVENT_BEFORE_TOOL,
    EVENT_BEFORE_TURN,
    HookRunner,
)
from .runtime.verify import VerifySettings, run_verification
from .session import (
    PHASE_ACT,
    PHASE_PLAN,
    PHASE_VALIDATE,
    Session,
    SessionManager,
    Turn,
    turns_since_clear,
)
from .typing import spi as cobirb_typing

logger = logging.getLogger("cobirb")

# Sentinel distinguishing "no chunk yet" from a real (possibly empty-string)
# streamed chunk when peeking the first item of a model's stream in _chat().
_STREAM_EMPTY = object()

# Tools whose success means the workspace is not what it was, and therefore
# that the project's own check is worth running. `shell` is included because
# it very often is the thing that changed something, even though it cannot say
# so in advance the way the file tools can.
_CHANGING_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "shell"})

# Prefixed onto a steering message before it re-enters history as a user
# turn, so a small local model — which has no other way to know its own
# reply was just cut off — is told plainly why a new user turn has appeared
# in the middle of what it was saying, rather than reading as a non sequitur.
_STEER_PREAMBLE = (
    "[The user sent this while you were still replying, to redirect you now "
    "rather than wait. Take it into account immediately:] "
)

# Why a run() stopped. A run that ran out of turns used to return a made-up
# reply — "Stopped after N turns without a final answer." — which was then
# stored as the model's answer, shown as though the model had said it, and
# read by every consumer as a conclusion. A stop is a fact about the run, not
# something the model said, so it is reported as one (see RunStop).
STOP_ANSWERED = "answered"
STOP_TURN_LIMIT = "turn_limit"
STOP_NO_PROGRESS = "no_progress"

# A run stops when it stops making progress, not after a fixed number of
# turns. The fixed number used to be 8, which an ordinary task — read three
# files, edit, run the tests, fix — spends before it is half done, so the
# commonest ending of a real task was "Stopped after 8 turns". The ceiling is
# now a backstop against a runaway loop, and the brakes below are what
# actually end an unproductive run:
#
# - the same call, with the same arguments, made back to back: the second gets
#   a note saying so, the fourth ends the run. Nothing changes between two
#   identical consecutive calls, so neither will the answer.
# - a run of failed or denied calls with nothing succeeding in between.
DEFAULT_MAX_TURNS = 40
_REPEAT_NOTE_AT = 2
_REPEAT_STOP_AT = 4
_FAILURE_STOP_AT = 6
_REPEAT_NOTE = (
    "\n\n[CoBirb: this is exactly the call you just made, with the same arguments, "
    "so it gives the same result. Do something different, or give your answer if you have one.]"
)


@dataclass(frozen=True)
class RunStop:
    """How the last ``run()`` ended. ``reason`` is one of the ``STOP_*``
    constants; ``turns`` is the budget that applied when it matters."""

    reason: str = STOP_ANSWERED
    turns: int = 0
    detail: str = ""

    @property
    def finished(self) -> bool:
        """Whether the model reached a final answer on its own."""
        return self.reason == STOP_ANSWERED

    def describe(self) -> str:
        """A sentence for a person, or ``""`` when there is nothing to say."""
        if self.reason == STOP_TURN_LIMIT:
            return (
                f"Stopped after {self.turns} model turn(s) without a final answer. "
                "Send another message to let it carry on."
            )
        if self.reason == STOP_NO_PROGRESS:
            return f"Stopped: {self.detail} Send another message to try a different approach."
        return ""


# What a reply is labelled with unless the caller names someone else — the
# Flock labels Brainy Birb and each Worker Birb by their own names.
REPLY_LABEL = "CoBirb"

# What compaction asks for when it has to drop turns (see _summarise_dropped).
_SUMMARY_REQUEST = (
    "The conversation above is about to be removed from your context to make room. Write a "
    "summary of it for your own later reference: what was asked, which files and facts turned "
    "out to matter, what was changed, what failed, and what is still to do. Bullet points, under "
    "250 words, nothing that is not in the conversation."
)
_SUMMARY_TURN_CHARS = 1500

# Plan mode (Orchestrator.run(plan_mode=True)): a planning pass, then the work.
# The planning pass can *look* — the read-only tools and the checklist — but not
# change anything. It used to be a single reply with no tools at all, which
# asked the model to plan work on code it had not been allowed to see; and a
# third "validation" phase afterwards re-ran what the working-method prompt and
# verify_command already cover. See _run_plan_phase.
_PLAN_PHASE_INSTRUCTIONS = (
    "PLANNING PHASE. Look before you plan: use the read-only tools to find and read the code "
    "this request is about. You cannot change anything in this phase. Then write a short, "
    "numbered plan of the concrete steps you will take, and record it as a checklist with the "
    "todo tool. The plan is shown to the user; you carry it out next."
)
_ACT_PHASE_INSTRUCTIONS = (
    "ACT PHASE. Carry out the plan above, adapting it if what you find contradicts it. Keep the "
    "todo checklist current as you finish each step, check your work, then answer briefly with "
    "what you changed."
)
# How many model turns the planning pass may spend looking around.
_PLAN_MAX_TURNS = 12
_PLAN_TOOLS = READ_TOOLS | {"todo"}


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
    to probe for it and cope with its absence. Written out per call site,
    that probe multiplies across modules, each copy with a slightly different
    fallback — so it lives here once instead.

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


def _denial_message(tool_name: str, instruction: str = "") -> str:
    """What a refused tool call tells the model.

    The bare refusal is a dead end: the model learns it may not do the thing
    and nothing about what it should do instead, so it either retries the
    identical call or abandons the work and reports being stuck. Where the
    user took the trouble to say what to do instead, that is the useful half
    of the answer and it goes in the result the model reads.

    Marked as coming from the user rather than from CoBirb, because the two
    carry different authority: one is a policy outcome, the other is an
    instruction from the person the agent is working for.
    """
    denial = f"Permission denied: tool '{tool_name}' is not permitted."
    instruction = instruction.strip()
    if not instruction:
        return denial
    return f"{denial}\nThe user says to do this instead: {instruction}"


def _autopilot_refusal(tool_name: str) -> str:
    """What the model is told when auto-pilot refuses something rather than
    stopping to ask — nobody is there to ask, by design."""
    return (
        f"Refused: '{tool_name}' needs approval, and auto-pilot does not stop to ask. Inside "
        "auto-pilot you may read and change files in the project and run commands in the "
        "sandbox. Do without this, or finish and say in your answer what still needs doing."
    )


def _malformed_message(problem: str) -> str:
    """What the model is told when a tool call it made could not be read."""
    return (
        f"[CoBirb: your last reply contained {problem}, so nothing was run. "
        "Make the call again through the tool-calling interface, with valid arguments — "
        "or, if you were finished, give your final answer as plain text.]"
    )


def _tool_failure_message(tool_name: str, tool: Any, exc: Exception) -> str:
    """Turn a tool's exception into something the model can act on.

    A `KeyError: 'path'` is the single commonest failure in this loop: the
    model invents an argument name, the tool subscripts a dict, and the
    traceback names the key it wanted without saying what the tool actually
    accepts. Restating the schema costs a few dozen tokens and usually turns a
    repeated failure into a corrected call on the very next turn, which is
    worth far more against a small local model than against a large one.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, KeyError):
        detail = f"missing required argument {exc}"
    elif isinstance(exc, TypeError):
        detail = f"the arguments did not fit the tool: {exc}"
    else:
        return f"Tool '{tool_name}' failed: {detail}"

    try:
        schema = tool.parameters()
        properties = ", ".join(sorted(schema.get("properties", {})))
        required = ", ".join(schema.get("required", []))
    except Exception:  # noqa: BLE001 - a broken schema must not mask the real error
        return f"Tool '{tool_name}' failed: {detail}"
    return (
        f"Tool '{tool_name}' failed: {detail}. "
        f"It accepts: {properties or '(none)'}. Required: {required or '(none)'}."
    )


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
        context_tokens: int | None = None,
        project_context: str = "",
        redact_secrets: bool = True,
        verify: "VerifySettings | None" = None,
        checkpoints: "Checkpoints | None" = None,
        hooks: "HookRunner | None" = None,
        mcp_clients: list[Any] | None = None,
        grants: "SessionGrants | None" = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.model = model
        # The ceiling for a run() that names none — see DEFAULT_MAX_TURNS.
        self.max_turns = max_turns
        # Auto-pilot (enable_autopilot): unattended work inside the project and
        # the sandbox; anything else is refused instead of asked about.
        self.autopilot = False
        self._before_autopilot = False
        self.tools = tools
        self.policy = policy
        self.io = io
        # Approvals that outlive this agent's own policy, shared with every
        # other agent in the session (see policy.SessionGrants). Always an
        # object so no call site needs a "are grants wired?" branch, and this
        # policy joins it immediately — a session grant made by a Worker Birb
        # has to reach the agent the user is talking to as well.
        self.grants = grants if grants is not None else SessionGrants()
        self.grants.register(policy)
        # Who this agent is, when it is not the one the user is talking to:
        # a Worker Birb's id, set by `wiring.build_subagent`. Travels on an
        # approval request so a prompt can say which of several concurrent
        # agents wants the capability — in a flock that is half the question.
        self.agent_id = ""
        self.crypto = crypto
        self.session = session
        # The project's own check, run after a turn that changed files. None
        # unless the user nominated a command — CoBirb never guesses one.
        self.verify = verify
        # Whether tool output is scanned for credentials before it reaches
        # the model, the session or the audit log. See cobirb.redaction.
        self.redact_secrets = redact_secrets
        # Snapshots taken before the agent changes a file, backing /undo.
        # None disables it entirely (`"checkpoints": false`).
        self.checkpoints = checkpoints
        # The user's own commands at the lifecycle points. Always an object,
        # never None — an empty runner returns immediately, which keeps every
        # call site below free of a "are hooks configured?" branch.
        self.hooks = hooks or HookRunner()
        # MCP servers running as child processes for the life of this
        # orchestrator. Held only so `close()` can stop them: their tools are
        # already in `tools` and nothing here treats them specially.
        self.mcp_clients = list(mcp_clients or [])
        # What this project is and what it asks of an agent: its AGENTS.md,
        # and an outline of its codebase. Resolved at wiring time because
        # reading project files is not core's job, and composed into every
        # turn's system prompt.
        self.project_context = project_context
        # The model's usable context window. None means "ask the provider on
        # first use, then remember" — see _context_budget.
        self.context_tokens = context_tokens
        # What the tools did during the most recent run, for the headless
        # report. Derived here rather than sniffed out of turn text later,
        # because "was this denied?" should be a fact and not a string match.
        self.last_run_tool_calls: list[dict[str, Any]] = []
        # The last verification run, for the headless report and /verify.
        self.last_verification = None
        # What the last _build_context had to throw away, for /context.
        self.last_compaction: CompactionReport | None = None
        # How the last run() ended — see RunStop. The flock needs this as much
        # as a person does: a planning turn that spent its whole budget writing
        # a skeleton has not decided anything, and reporting it as though the
        # model had reached a conclusion is how a run that simply needed more
        # room reads as one that refused.
        self.last_stop = RunStop()
        # Whether the most recent run()'s final answer was already streamed
        # live to `io` (see run()'s docstring) — false until a run happens.
        self.last_turn_streamed = False
        # Overwritten by run() with the reply label for this call.
        self._stream_label = REPLY_LABEL
        # Mid-turn steering (see steer()): messages queued from another
        # thread while run() is executing, applied at the next loop boundary
        # by _drain_steer. A plain queue.SimpleQueue rather than a list plus
        # a lock — nothing here needs more than thread-safe put/get.
        self._steer_queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        # True only while run() is actually executing, so steer() can refuse
        # a message with no turn to redirect instead of queuing it for some
        # future, unrelated run.
        self._turn_active = False
        # (how many dropped turns it covers, the summary) — see _summarise_dropped.
        self._summary_cache: tuple[int, str] = (0, "")
        # Set by _chat() when a stream was cut off by a steer rather than
        # finishing on its own — see SteeringInterrupted and _loop().
        self._last_chat_was_steered = False

    # ------------------------------------------------------------------ #
    # Public run
    # ------------------------------------------------------------------ #
    def run(
        self,
        prompt: str,
        system: str,
        *,
        cwd: str = ".",
        label: str = REPLY_LABEL,
        max_turns: int | None = None,
        session_path: str | None = None,
        plan_mode: bool = False,
        images: "list[dict[str, str]] | None" = None,
    ) -> Session:
        """Run the loop for a single objective.

        ``images``, when given, is ``[{"id", "filename", "data"}]`` for
        whatever was attached to this specific prompt — ``data`` is base64,
        already-plaintext bytes the caller read off local disk (see
        ``tui.app.CoBirbApp._cmd_image``). The bytes are stored into
        ``Session.images`` and so travel with the session from then on: a
        resumed conversation shows the model the image again, not a note
        saying one belongs there.

        Stops as soon as the model gives a plain-text reply with no further
        tool calls (that reply becomes ``session.summary``), or after
        ``max_turns`` iterations (default ``self.max_turns``) if the model keeps calling tools without
        ever producing a final answer.

        When the model and I/O adapter both support it, the model's reply is
        streamed live to ``io`` as it arrives. ``self.last_turn_streamed`` is
        set to whether the *final* answer specifically was already shown
        this way, so a caller (e.g. the CLI) knows whether it still needs to
        print ``session.summary`` itself or would just be duplicating output.

        With ``plan_mode=True`` (config/``/plan`` — see cli.py), the act
        loop is preceded by a **plan** phase: a bounded loop offered only the
        read-only tools and the checklist (``_run_plan_phase``), so the plan
        is made after looking at the code, and shown to the user at once.
        The act loop then follows it, its turns tagged "act". Off (the
        default), the model plans and acts in one continuous loop.
        """
        session = self._open_session(prompt, system, cwd, session_path, images)
        self.last_run_tool_calls = []
        if self.checkpoints is not None:
            self.checkpoints.begin_turn()

        # Clear first, *then* open for steering — never the other way round.
        # Anything left in the queue is stale (a run that ended without
        # draining it, an exception mid-loop) and must not leak into this
        # run; but once `_turn_active` is true, steer() starts accepting
        # messages and telling the caller so, and clearing after that point
        # would throw away a message the user was just told had landed.
        self._clear_steer_queue()
        self._turn_active = True
        try:
            return self._run_body(
                prompt=prompt,
                system=system,
                cwd=cwd,
                label=label,
                max_turns=max_turns or self.max_turns,
                plan_mode=plan_mode,
                session=session,
            )
        finally:
            self._turn_active = False
            # Whole-tree checkpoints close the turn's snapshot here, so /undo
            # knows exactly what this turn changed (see TreeCheckpoints).
            end_turn = getattr(self.checkpoints, "end_turn", None)
            if callable(end_turn):
                try:
                    end_turn()
                except Exception:  # noqa: BLE001 - undo is a convenience; never cost a turn
                    logger.debug("could not close the turn's checkpoint", exc_info=True)

    def _run_body(
        self,
        *,
        prompt: str,
        system: str,
        cwd: str,
        label: str,
        max_turns: int,
        plan_mode: bool,
        session: Session,
    ) -> Session:
        """The actual run() logic, wrapped by run() itself so ``_turn_active``
        is reliably cleared on every exit path — see run()'s ``try/finally``.
        """
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
        # Project instructions count as something CoBirb has to say, so their
        # presence alone is enough to start sending a system message — the
        # empty-stays-empty rule above is about not inventing one, not about
        # withholding what the project explicitly asked to be told.
        system_with_cwd = _join_system(
            system, self.project_context, f"Working directory: {cwd}"
        ) if (system or self.project_context) else ""
        logger.info("starting run; turns=%d plan_mode=%s", len(session.turns), plan_mode)
        self.last_turn_streamed = False
        self._stream_label = label
        self._fire_and_report(EVENT_BEFORE_TURN, payload={"prompt": prompt})

        if plan_mode:
            plan_text, plan_streamed = self._run_plan_phase(
                _join_system(system_with_cwd, _PLAN_PHASE_INSTRUCTIONS), session
            )
            if not plan_streamed:
                self._render_phase(PHASE_PLAN, label, plan_text)

        act_system = (
            _join_system(system_with_cwd, _ACT_PHASE_INSTRUCTIONS) if plan_mode else system_with_cwd
        )
        content, streamed = self._loop(act_system, session, max_turns, PHASE_ACT if plan_mode else None)
        content, streamed = self._verify_and_fix(
            act_system, session, content, streamed, plan_mode
        )
        session.summary = content
        self.last_turn_streamed = streamed
        stop = self.last_stop

        if not stop.finished:
            render_through(self.io, "render_notice", stop.describe())

        # Last thing before the session is handed back, so an after_turn hook
        # that reads the workspace sees it in its finished state — including
        # anything the verify-and-fix pass changed.
        self._fire_and_report(
            EVENT_AFTER_TURN, payload={"changed_files": self._changed_anything()}
        )
        return session

    def _verify_and_fix(
        self, system: str, session: Session, content: str, streamed: bool, plan_mode: bool
    ) -> tuple[str, bool]:
        """Run the project's own check, and let the model react if it fails.

        Only after a turn that actually changed something — running a test
        suite because someone asked a question would be absurd. Bounded hard:
        a model that cannot fix a failing suite in one focused attempt is not
        usually one more turn away, and every extra round is model time
        nobody asked for.
        """
        if self.verify is None:
            return content, streamed
        if self.verify.only_after_changes and not self._changed_anything():
            return content, streamed

        for attempt in range(self.verify.max_fix_attempts + 1):
            result = run_verification(self.verify.command, self.verify.cwd, self.verify.timeout)
            self.last_verification = result
            render_through(
                self.io, "render_notice", result.describe(),
                fallback=lambda: self.io.render(f"\n[verify] {result.describe()}\n"),
            )
            if result.ok or result.error or attempt == self.verify.max_fix_attempts:
                return content, streamed
            # Handed to the model as a user turn: it is a fact about the world
            # that arrived after its last answer, which is exactly what a user
            # turn is for.
            session.add(Turn(role="user", content=result.as_turn()))
            content, streamed = self._loop(
                system, session, self.verify.max_turns, phase=PHASE_ACT if plan_mode else None
            )
        return content, streamed

    def _changed_anything(self) -> bool:
        """Whether this run has actually modified the workspace."""
        return any(
            call["ok"] and call["name"] in _CHANGING_TOOLS
            for call in self.last_run_tool_calls
        )

    def _run_plan_phase(self, system: str, session: Session) -> tuple[str, bool]:
        """Look, then plan: a bounded loop offered only the read-only tools
        and the checklist, so it can see the code but cannot change it. Its
        final reply is recorded as the "plan"-phase turn.

        The stop reason is reset afterwards: how the *planning* ended says
        nothing about the run, which the act phase decides.
        """
        tools = [tool for name, tool in self.tools.items() if name in _PLAN_TOOLS]
        content, streamed = self._loop(system, session, _PLAN_MAX_TURNS, PHASE_PLAN, tools=tools)
        self.last_stop = RunStop()
        return content, streamed

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

        Mid-turn steering (see ``steer()``) is applied at two points: a
        queued message is drained into history at the top of every
        iteration — the boundary between tool calls, and before the very
        first model call — and, if the model was cut off mid-stream (see
        ``_chat``'s ``SteeringInterrupted`` handling), its partial reply is
        kept as a genuine (if incomplete) turn and the loop goes straight
        back around to pick up the message that interrupted it, rather than
        parsing tool calls out of a reply that never finished.
        """
        self.last_stop = RunStop()
        last_calls, repeats, failures = "", 0, 0
        for _ in range(max_turns):
            # One place builds the context, once per model call, from
            # whatever the session holds right now — so every path that adds
            # a turn (a tool result, a drained steering message, a reply cut
            # short) is automatically reflected in the next request without
            # having to remember to rebuild it on the way out.
            self._drain_steer(session)
            context = self._build_context(session)
            reply, streamed = self._chat(system, context, tools)

            if self._last_chat_was_steered:
                # Cut off deliberately, not broken — recorded as-is so the
                # model's next reply is informed by what it already said,
                # and looped straight back around (consuming one of
                # max_turns) rather than treated as a finished answer or
                # scanned for tool calls it never got to emit.
                session.add(Turn(role="assistant", content=_materialize(reply), phase=phase))
                # No fallback: an adapter with no `render_notice` hook simply
                # doesn't show this. The steering message itself is already
                # visible wherever the user typed it, so a plain-render
                # fallback would be noise rather than information.
                render_through(self.io, "render_notice", "↳ redirected by a new message")
                continue

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
                before = len(self.last_run_tool_calls)
                offered = None if tools is None else {t.name() for t in tools}
                self._execute_tool_calls(tool_calls, phase=phase, offered=offered)
                outcomes = self.last_run_tool_calls[before:]

                signature = json.dumps(
                    [[c.name, c.arguments] for c in tool_calls], sort_keys=True, default=str
                )
                repeats = repeats + 1 if signature == last_calls else 1
                last_calls = signature
                failed = bool(outcomes) and not any(o["ok"] for o in outcomes)
                failures = failures + len(outcomes) if failed else 0

                if repeats >= _REPEAT_STOP_AT:
                    self.last_stop = RunStop(
                        STOP_NO_PROGRESS, max_turns,
                        f"the model made the same {tool_calls[0].name} call {repeats} times in a row.",
                    )
                    return "", False
                if failures >= _FAILURE_STOP_AT:
                    self.last_stop = RunStop(
                        STOP_NO_PROGRESS, max_turns,
                        f"{failures} tool calls in a row failed or were refused.",
                    )
                    return "", False
                if repeats >= _REPEAT_NOTE_AT and session.turns and session.turns[-1].role == "tool":
                    session.turns[-1].content += _REPEAT_NOTE
                continue

            problem = self._malformed_tool_call(tools)
            if problem:
                # The model plainly tried to act and the call could not be read.
                # Taking this reply for its final answer ended the task on a
                # call that never ran; tell it what went wrong and let it send
                # the call again. Counted as a failure, so a model that cannot
                # produce a readable call is stopped by the same brake.
                session.add(Turn(role="assistant", content=_materialize(reply), phase=phase))
                session.add(Turn(role="user", content=_malformed_message(problem), phase=phase))
                failures += 1
                if failures >= _FAILURE_STOP_AT:
                    self.last_stop = RunStop(
                        STOP_NO_PROGRESS, max_turns,
                        f"the model's last {failures} tool calls could not be read or failed.",
                    )
                    return "", False
                continue

            # No tool calls: this is the model's final answer for this turn.
            content = _materialize(reply)
            session.add(Turn(role="assistant", content=content, phase=phase))
            return content, streamed and bool(content)

        # No reply is invented to stand in for the missing answer: the summary
        # stays empty, and the stop is reported as a fact about the run.
        self.last_stop = RunStop(STOP_TURN_LIMIT, max_turns)
        return "", False

    def _malformed_tool_call(self, tools: "list[cobirb_typing.Tool] | None") -> str:
        """The provider's report of a tool call it could not read, if it has
        the optional ``malformed_tool_call`` hook and tools were on offer."""
        if tools == [] or not self.model.supports_tool_calling():
            return ""
        probe = getattr(self.model, "malformed_tool_call", None)
        if not callable(probe):
            return ""
        try:
            return str(probe() or "")
        except Exception:  # noqa: BLE001 - an optional hook never breaks a turn
            return ""

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

        A label (``run()``'s ``label``) is rendered
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

        Sets ``self._last_chat_was_steered`` for ``_loop`` to check: a model
        that implements the optional ``interrupt_current_reply`` may raise
        ``SteeringInterrupted`` mid-stream (see ``steer()``), which is caught
        here and turned into "return whatever streamed so far, flagged as
        cut short" rather than an error — a non-streaming call is never
        interruptible this way, since it has already fully returned by the
        time anything could react.
        """
        self._last_chat_was_steered = False
        if tools is None:
            tools = list(self.tools.values())
        supports_streaming = getattr(self.model, "supports_streaming", lambda: False)()
        label = f"{self._stream_label} is thinking…"
        if self.io is None or not supports_streaming:
            reply = self._spun(label, lambda: self.model.chat(system, context, tools))
            return _materialize(reply), False

        stream = iter(self.model.chat(system, context, tools, stream=True))
        chunks: list[str] = []
        started = False

        def _emit(chunk: str) -> None:
            nonlocal started
            if chunk:
                if not started:
                    # Tell the adapter a reply is starting and let *it* decide
                    # what to draw. Rendering f"{label}: " into the stream
                    # here would make the label part of the text the
                    # renderer receives, so a transcript that marks replies
                    # with "> " would read "> CoBirb: hello" instead of
                    # "> hello". Chrome is the adapter's business; the
                    # orchestrator only knows *when*
                    # the first token arrived, which is the one thing an
                    # adapter can't work out for itself.
                    begin = getattr(self.io, "begin_stream", None)
                    if callable(begin):
                        begin(self._stream_label)
                    started = True
                self.io.render(chunk)
                chunks.append(chunk)

        try:
            first = self._spun(label, lambda: next(stream, _STREAM_EMPTY))
            if first is not _STREAM_EMPTY:
                _emit(first)
            for chunk in stream:
                _emit(chunk)
        except cobirb_typing.SteeringInterrupted:
            self._last_chat_was_steered = True
        if chunks:
            self.io.render("\n")
        return "".join(chunks), True

    # ------------------------------------------------------------------ #
    # Session plumbing
    # ------------------------------------------------------------------ #
    def _open_session(
        self,
        prompt: str,
        system: str,
        cwd: str,
        session_path: str | None = None,
        images: "list[dict[str, str]] | None" = None,
    ) -> Session:
        # Reuse the supplied session manager when one was injected (e.g. a
        # resumed --session), so prior history and the session path/cipher
        # are preserved. Either way, this prompt is always recorded as the
        # opening turn of *this* run.
        if self.session is None:
            self.session = SessionManager.create(session_path or ".", self.crypto, cwd)
        # The turn keeps a reference; the bytes go in the session's own image
        # table, keyed by content hash — so the same screenshot attached twice
        # is stored once, and everything rides inside the one encrypted blob
        # (see Session.images for why that placement, not a sibling file).
        stored_images = None
        if images:
            session = self.session.session
            for image in images:
                if image.get("data"):
                    session.images[image["id"]] = image["data"]
            stored_images = [{"id": i["id"], "filename": i["filename"]} for i in images]
        self.session.session.add(Turn(role="user", content=prompt, images=stored_images))
        return self.session.session

    def _context_budget(self) -> int:
        """Tokens of history this model can be given.

        Asked of the provider once (the optional ``context_window`` hook) and
        then remembered, since it cannot change mid-run. Falls back to a
        deliberately conservative default when the provider can't say —
        over-estimating means the server truncates silently, which is the
        failure this whole path exists to avoid.
        """
        if self.context_tokens is None:
            window = getattr(self.model, "context_window", None)
            if callable(window):
                try:
                    self.context_tokens = window()
                except Exception:  # noqa: BLE001 - never block a turn on this
                    self.context_tokens = None
        return history_budget(self.context_tokens or DEFAULT_CONTEXT_TOKENS)

    def _build_context(self, session: Session) -> str:
        """Serialize the turn history as JSON (role, content, tool_use per
        turn) so a provider can reconstruct a proper multi-turn messages
        array — see ``LocalModelProvider._build_messages`` — instead of
        every turn being flattened into a single opaque blob, which gave
        tool-calling models no reliable signal that a prior tool call was
        already satisfied.

        Trimmed to fit the model's window on the way out (see
        ``cobirb.context``). A short session is returned unchanged; a long one
        loses its oldest tool results first and its oldest turns only if that
        wasn't enough.

        Starts after the most recent ``/clear`` when there is one. That marker
        is a point in the conversation rather than a deletion — the turns
        before it are still in the session file — so this is the one place
        that decides they are no longer part of what the model is answering.
        """
        turns = []
        for t in turns_since_clear(session.turns):
            entry: dict[str, Any] = {"role": t.role, "content": t.content, "tool_use": t.tool_use}
            if t.images:
                # Resolved against the session's own (already-decrypted) image
                # table, for *every* image-bearing turn rather than only the
                # newest. An attachment is part of the conversation the same
                # way its text is: resume a session and the model sees the
                # image again. Bytes that have gone missing (a hand-edited
                # session, a turn branched away from its table) degrade to a
                # marker rather than failing the turn.
                resolved = [
                    {**img, "data": session.images[img["id"]]}
                    for img in t.images
                    if img.get("id") in session.images
                ]
                if resolved:
                    entry["images"] = resolved
                missing = [img for img in t.images if img.get("id") not in session.images]
                if missing:
                    marker = " ".join(f"[image: {img.get('filename') or 'attachment'}]" for img in missing)
                    entry["content"] = f"{entry['content']}\n{marker}".strip() if entry["content"] else marker
            turns.append(entry)
        turns, report = compact(turns, self._context_budget(), summarise=self._summarise_dropped)
        self.last_compaction = report
        if report.changed:
            logger.info("compacted context: %s", report.describe())
        return json.dumps(turns)

    def _summarise_dropped(self, dropped: list[dict[str, Any]]) -> str:
        """A model-written summary of turns compaction is about to drop.

        Cached against how many turns it covered: the dropped prefix only grows
        as a session does, so a later compaction summarises the previous summary
        plus the newly dropped turns, rather than everything again — one call
        each time the prefix grows. The material is trimmed before it is sent,
        since what is being dropped is by definition too large to send whole.
        No tools are offered, so this call cannot act.
        """
        cached_count, cached_text = self._summary_cache
        if cached_text and cached_count == len(dropped):
            return cached_text
        material: list[dict[str, Any]] = []
        new = dropped
        if cached_text and cached_count < len(dropped):
            material.append({"role": "user", "content": f"Summary so far:\n{cached_text}", "tool_use": None})
            new = dropped[cached_count:]
        for turn in new:
            content = str(turn.get("content") or "")
            if len(content) > _SUMMARY_TURN_CHARS:
                content = content[:_SUMMARY_TURN_CHARS] + " …[cut]"
            material.append({"role": turn.get("role", "user"), "content": content,
                             "tool_use": turn.get("tool_use")})
        limit = max(4000, self._context_budget() * 2)  # characters, well inside the window
        while len(json.dumps(material)) > limit and len(material) > 1:
            material.pop(1 if material[0]["content"].startswith("Summary so far") else 0)
        material.append({"role": "user", "content": _SUMMARY_REQUEST, "tool_use": None})
        reply = self.model.chat("", json.dumps(material), [])
        text = _materialize(reply).strip()
        if text:
            self._summary_cache = (len(dropped), text)
        return text

    # ------------------------------------------------------------------ #
    # Tool dispatch (policy-gated)
    # ------------------------------------------------------------------ #
    def _execute_tool_calls(
        self,
        tool_calls: list[cobirb_typing.ToolCall],
        phase: str | None = None,
        offered: "set[str] | None" = None,
    ) -> None:
        """Run each call — except one naming a tool this phase did not offer.

        A model is offered a restricted set in plan mode's planning pass (the
        read-only tools), but a native tool call can name anything; running a
        ``write_file`` because it was asked for would make "planning cannot
        change anything" a hope instead of a rule.
        """
        for call in tool_calls:
            if offered is not None and call.name not in offered and call.name in self.tools:
                self._record_call(call.name, ok=False, denied=True)
                message = (f"'{call.name}' is not available in this phase — only "
                           f"{', '.join(sorted(offered)) or 'no tools'}. Nothing was changed.")
                tool_use = [{"name": call.name, "arguments": call.arguments}]
                self.session.session.add(Turn(role="tool", content=message, tool_use=tool_use, phase=phase))
                continue
            self._execute_tool(call, phase)

    def enable_autopilot(self) -> str:
        """Turn auto-pilot on. Returns ``""``, or why it cannot be turned on.

        Auto-pilot is only as safe as what contains it, so it refuses to start
        without both halves: the shell sandbox (nothing a command does reaches
        past the project or onto the network) and whole-tree checkpoints
        (anything it changes in the project can be undone). A project that is
        really the home directory or ``/`` is refused too — "change anything
        in the project" would mean "change anything".
        """
        box = getattr(self.tools.get("shell"), "sandbox", None)
        if box is None or not getattr(box, "active", False):
            return "the shell sandbox is not active here (bubblewrap is needed — see 'cobirb doctor')"
        if not callable(getattr(self.checkpoints, "end_turn", None)):
            return ("whole-tree checkpoints are not active (git is needed, and \"checkpoints\" "
                    "must not be false), so what it changed could not be undone")
        root = self.policy._project_root()
        if root is None:
            return "the working directory is your home directory or /, which is too broad a project"
        self._before_autopilot = self.policy.sandbox_auto
        self.policy.autopilot_root = root
        self.policy.sandbox_auto = True
        self.autopilot = True
        return ""

    def disable_autopilot(self) -> None:
        if self.autopilot:
            self.policy.autopilot_root = None
            self.policy.sandbox_auto = self._before_autopilot
        self.autopilot = False

    def _request_approval(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[str, str]:
        """Ask ``io`` whether to allow a not-yet-permitted tool call.

        Returns the decision and, when the answer was a refusal carrying one,
        what the user said to do instead.

        Three adapter shapes, richest first, each probed with ``getattr``
        rather than declared on the ABC — which is what keeps the SPI freeze
        honest (see ``I_OAdapter.confirm``):

        - ``confirm_request`` answers with an ``ApprovalOutcome``, so a
          refusal can carry an instruction for the model.
        - ``confirm_scoped`` receives a plain-language description of what
          "always" would actually grant (``Policy.describe_grant``) —
          approving a read widens access to a whole directory tree, and a
          prompt that can't say so is asking the user to agree to something
          it hasn't told them.
        - ``confirm`` is the documented minimum and asks the narrowest
          question.

        Fails closed (denies) when there's no interactive adapter attached,
        it doesn't implement ``confirm`` (duck-typed test doubles), or it
        raises — there's no one to ask, so the safe answer is no.
        """
        deny = (cobirb_typing.DECISION_DENY, "")
        if self.io is None or not hasattr(self.io, "confirm"):
            return deny
        instruction = ""
        try:
            request = cobirb_typing.ApprovalRequest(
                tool_name=tool_name,
                arguments=arguments,
                scope=self.policy.describe_grant(tool_name, arguments),
                preview=self._preview(tool_name, arguments),
                asked_by=self.agent_id,
            )
            confirm_request = getattr(self.io, "confirm_request", None)
            confirm_scoped = getattr(self.io, "confirm_scoped", None)
            if callable(confirm_request):
                outcome = confirm_request(request)
                decision = getattr(outcome, "decision", "")
                instruction = str(getattr(outcome, "instruction", "") or "")
            elif callable(confirm_scoped):
                decision = confirm_scoped(request)
            else:
                decision = self.io.confirm(tool_name, arguments)
        except Exception:  # noqa: BLE001 - a broken adapter must not open access
            return deny
        if decision not in cobirb_typing.DECISIONS:
            return deny
        # An instruction only means anything attached to a refusal. Carried
        # alongside an approval it would be text the model never sees, which
        # is worse than not offering it: the user would have typed something
        # and watched it vanish.
        return decision, instruction if decision == cobirb_typing.DECISION_DENY else ""

    def _execute_tool(self, call: cobirb_typing.ToolCall, phase: str | None = None) -> None:
        tool_name = call.name
        arguments = call.arguments
        session = self.session.session
        # Tags this result with the call it answers, so a provider building
        # a proper messages array can label the "tool" message accordingly.
        tool_use = [{"name": tool_name, "arguments": arguments}]

        # Existence before permission. Asking a human to approve a tool that
        # does not exist is a nonsense question, and the model gets "permission
        # denied" for a typo — which tells it to give up rather than to fix the
        # name. Nothing is leaked by answering first: the tool list is already
        # in the request the model just replied to.
        tool = self.tools.get(tool_name)
        if tool is None:
            self._record_call(tool_name, ok=False, denied=False)
            result = cobirb_typing.ToolResult(ok=False, content=self._unknown_tool_message(tool_name))
            session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
            self._render_tool_call(tool_name, arguments, result)
            return

        # The user's own rules, before the user's own judgement. A hook that
        # is going to refuse this should refuse it before anyone is asked to
        # approve it — otherwise the prompt puts a question to a person whose
        # answer has already been overruled.
        gate = self.hooks.fire(EVENT_BEFORE_TOOL, tool_name=tool_name, arguments=arguments)
        if gate.blocked:
            self._record_call(tool_name, ok=False, denied=True)
            result = cobirb_typing.ToolResult(
                ok=False,
                content=f"Blocked by a before_tool hook: {gate.reason}",
                error="blocked_by_hook",
            )
            session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
            self._render_tool_call(tool_name, arguments, result)
            return

        # Not already permitted: ask the user rather than silently denying,
        # so "default-deny" means "asks first," not "the model never finds
        # out it could have worked." Fails closed (denies) with no adapter,
        # or one that can't ask (see I_OAdapter.confirm's contract).
        if not self.policy.is_allowed(tool_name, arguments):
            if self.autopilot:
                decision, instruction = cobirb_typing.DECISION_DENY, ""
            else:
                decision, instruction = self._request_approval(tool_name, arguments)
            if decision == cobirb_typing.DECISION_DENY:
                self._record_call(tool_name, ok=False, denied=True)
                result = cobirb_typing.ToolResult(
                    ok=False,
                    content=_autopilot_refusal(tool_name) if self.autopilot
                    else _denial_message(tool_name, instruction),
                )
                session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
                self._render_tool_call(tool_name, arguments, result)
                return
            if decision == cobirb_typing.DECISION_ALWAYS:
                # What "always" widens to is the policy's decision, not the
                # orchestrator's — a read grants a directory, a shell call
                # grants its invocation, everything else grants the tool.
                self.policy.grant(tool_name, arguments)
            elif decision == cobirb_typing.DECISION_SESSION:
                # The same widening, applied to every agent in the session
                # instead of only this one. Registering this policy at
                # construction is what makes that include the caller.
                self.grants.grant(tool_name, arguments)

        self._snapshot_before(tool, arguments)
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
            self._record_call(tool_name, ok=False, denied=False)
            result = cobirb_typing.ToolResult(
                ok=False, content=_tool_failure_message(tool_name, tool, exc)
            )
            session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
            self._render_tool_call(tool_name, arguments, result)
            return
        result = self._redacted(result)
        self._record_call(tool_name, ok=result.ok, denied=False)
        session.add(Turn(role="tool", content=result.content, tool_use=tool_use, phase=phase))
        self._render_tool_call(tool_name, arguments, result)
        # Observation only: an after_tool hook has nothing left to prevent, so
        # its exit code is logged rather than acted on. It gets whether the
        # call succeeded, not the result body — a formatter needs to know a
        # file changed, and does not need the file.
        self._fire_and_report(
            EVENT_AFTER_TOOL, tool_name=tool_name, arguments=arguments, payload={"ok": result.ok}
        )

    def _redacted(self, result: cobirb_typing.ToolResult) -> cobirb_typing.ToolResult:
        """Strip credentials from a tool result before it goes anywhere.

        Done here, once, rather than in each tool: a result is about to become
        a session turn, a message in the next request and possibly an audit
        line, and a plugin tool that never heard of this gets the same
        treatment as a built-in.
        """
        if not self.redact_secrets or not result.content:
            return result
        redaction = redact(result.content)
        if not redaction.changed:
            return result
        logger.info("%s in a tool result", redaction.describe())
        return cobirb_typing.ToolResult(
            ok=result.ok, content=redaction.text, error=result.error, meta=result.meta
        )

    def _fire_and_report(self, event: str, **kwargs: Any) -> None:
        """Run the hooks for an observational event and surface any failures.

        Shown rather than swallowed: a hook the user wrote to format their code
        after every edit, which has been silently exiting 1 for a week, is
        worse than no hook at all. It never stops the run — by this point
        there is nothing left to stop.
        """
        outcome = self.hooks.fire(event, **kwargs)
        for failure in outcome.failures:
            message = f"{event} hook failed: {failure}"
            logger.warning(message)
            render_through(
                self.io, "render_notice", message,
                fallback=lambda: self.io.render(f"\n[hook] {message}\n"),
            )

    def _record_call(self, tool_name: str, *, ok: bool, denied: bool) -> None:
        self.last_run_tool_calls.append({"name": tool_name, "ok": ok, "denied": denied})

    def _snapshot_before(self, tool: Any, arguments: dict[str, Any]) -> None:
        """Save whatever this call is about to change, so it can be undone.

        Runs after approval and before execution: a denied call leaves no
        snapshot behind, and an approved one always has something to restore.
        Guarded, because failing to take a snapshot must never stop the edit
        the user just approved — the cost is one file that cannot be undone,
        which /undo reports rather than hiding.
        """
        if self.checkpoints is None:
            return
        writes = getattr(tool, "writes", None)
        if not callable(writes):
            return
        try:
            for path in writes(arguments) or []:
                self.checkpoints.record(str(path))
        except Exception:  # noqa: BLE001 - never block an approved edit
            return

    def _preview(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """What this call would do, if the tool can say before doing it.

        Optional and duck-typed. Guarded because this runs on the path to an
        approval prompt: a tool whose preview raises must cost the user a
        diff, never the ability to answer the question.
        """
        preview = getattr(self.tools.get(tool_name), "preview", None)
        if not callable(preview):
            return ""
        try:
            return str(preview(arguments) or "")
        except Exception:  # noqa: BLE001 - a broken preview is not worth the prompt
            return ""

    def _unknown_tool_message(self, tool_name: str) -> str:
        """Tell the model which tools actually exist.

        A bare "Unknown tool 'read'" leaves a model to guess again, and small
        local models guess the same wrong name repeatedly — which burns the
        turn budget on a mistake one line of text can fix. Near-misses lead,
        because the usual cause is a name that is close but not right.
        """
        available = sorted(self.tools)
        if not available:
            return f"Unknown tool '{tool_name}'. No tools are available in this session."
        close = get_close_matches(tool_name, available, n=3, cutoff=0.6)
        suffix = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"Unknown tool '{tool_name}'.{suffix} Available tools: {', '.join(available)}."

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

    def _render_phase(self, phase: str, label: str, text: str) -> None:
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
        # The plain fallback says "validation", not "validate": the phase
        # constant names a stage of the run, and this is prose shown to a
        # person. Keeping them separate is why the label is mapped rather
        # than interpolated straight from `phase`.
        heading = "validation" if phase == PHASE_VALIDATE else phase
        render_through(
            self.io,
            "render_plan" if phase == PHASE_PLAN else "render_validation",
            label,
            text,
            fallback=lambda: self.io.render(f"\n[{heading}] {text}\n"),
        )

    # ------------------------------------------------------------------ #
    # Convenience: register a fresh session path
    # ------------------------------------------------------------------ #
    @property
    def turns_exhausted(self) -> bool:
        """Whether the last run ran out of turns (kept for the flock, which
        asks exactly this question)."""
        return self.last_stop.reason == STOP_TURN_LIMIT

    @property
    def session_path(self) -> str | None:
        return self.session.path if self.session else None

    def steer(self, message: str) -> bool:
        """Redirect the turn in progress with ``message``, without ending it.

        Safe to call from another thread — the whole point, since the thread
        running ``run()`` is busy with the loop. Unlike ``cancel()``, nothing
        is lost: ``message`` is queued and applied at the next natural
        boundary, and whatever the model already said stays in history.

        Two things happen, and either alone is enough for the message to
        eventually land:

        - It is queued (``_steer_queue``), to be added as a user turn the
          next time ``_loop`` is between model calls — immediately, if the
          model is between tool calls right now.
        - If the model is *currently* streaming its reply and implements the
          optional ``interrupt_current_reply()`` (see ``LocalModelProvider``
          and ``SteeringInterrupted``), that stream is cut off right away
          rather than left to finish — so redirecting a model that is
          three paragraphs into the wrong answer doesn't mean reading the
          rest of it first.

        Returns whether there is a run to steer at all. A call with no turn
        in progress is not queued — steering a turn that has not started
        would be indistinguishable from just sending an ordinary message,
        and queuing it here would let it apply to some unrelated future run
        instead of being sent as what it actually is.
        """
        if not self._turn_active:
            return False
        self._steer_queue.put(message)
        interrupt = getattr(self.model, "interrupt_current_reply", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:  # noqa: BLE001 - the queued message still lands at the next boundary
                logger.debug("interrupt_current_reply raised", exc_info=True)
        return True

    def _drain_steer(self, session: Session) -> None:
        """Apply every steering message queued so far as a user turn.

        Called at each loop boundary (see ``_loop``), which rebuilds the
        context from the session immediately afterwards either way — so this
        reports nothing back and the caller needs no branch. Draining in a
        loop rather than taking one message matters when several arrive
        before the next boundary: a person typing two quick corrections
        should see both taken into account, in order, not just the last one
        silently winning.
        """
        while True:
            try:
                message = self._steer_queue.get_nowait()
            except queue.Empty:
                return
            session.add(Turn(role="user", content=f"{_STEER_PREAMBLE}{message}"))

    def _clear_steer_queue(self) -> None:
        """Discard anything left in the queue from a previous, finished run.

        Only ``run()`` calls this, once, before starting — a message that
        arrived too late for the run it was meant to steer must not silently
        reattach itself to an unrelated later one.
        """
        while True:
            try:
                self._steer_queue.get_nowait()
            except queue.Empty:
                return

    def cancel(self) -> None:
        """Interrupt whatever this orchestrator is doing right now.

        Best-effort and safe to call from another thread — which is the whole
        point, since the thread running the turn is blocked. Two things can be
        blocking: a ``shell`` command (already interruptible, the same handle
        Ctrl+C uses on an ordinary turn) and a model request (closing its
        connection unblocks the read and tells the server to stop). Neither is
        present on every orchestrator, so both are probed rather than assumed.

        This is the one-way, end-the-run stop — see ``steer()`` for the
        resumable alternative that redirects a turn instead of ending it.
        """
        shell = self.tools.get("shell")
        stop_shell = getattr(shell, "cancel_running", None)
        if callable(stop_shell):
            try:
                stop_shell()
            except Exception:  # noqa: BLE001 - a stuck turn is not worth a crash on the way out
                logger.debug("shell cancel raised", exc_info=True)
        stop_model = getattr(self.model, "cancel", None)
        if callable(stop_model):
            try:
                stop_model()
            except Exception:  # noqa: BLE001
                logger.debug("model cancel raised", exc_info=True)

    def close(self) -> None:
        """Release anything this orchestrator started.

        Today that is the MCP servers, which are child processes and would
        otherwise outlive the run that spawned them. Idempotent and never
        raising: this runs on the way out, including on the way out of a
        failure, and a shutdown that can itself fail is worse than the leak
        it was trying to prevent.
        """
        for client in self.mcp_clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - nothing to do about it at this point
                logger.debug("an MCP server did not shut down cleanly", exc_info=True)
        self.mcp_clients = []
        # Whole-tree checkpoints live as long as the session (TreeCheckpoints).
        close = getattr(self.checkpoints, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                logger.debug("could not remove the checkpoint store", exc_info=True)


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
