"""Staged planning: an overview, one focused stage per ticket, and rounds.

The one-prompt planner asks Brainy Birb for everything at once — divide the
work, design the seams, write every stub, test and brief, then build the
charter. At the level of detail a weaker Worker Birb needs, one ticket's plan is
about as long as the whole overview, so four tickets is four times the load in
one request, and models answer that load by cutting corners.

So the planning is split the way a person plans with someone else:

0. **Overview**, section by section in one context, read-only: what is asked,
   the decisions (put to the user in ``ask`` mode), the architecture, the seams,
   the tickets, the risks. This is Brainy Birb's design documents.
1. **Skeleton**: the finished shared files and the typed stubs. The charter's
   tickets come from the overview's ticket blocks, derived rather than written
   a second time.
2. **One fresh stage per ticket**: the design documents plus that ticket and
   nothing else. It writes that ticket's tests and its ticket plan, and the
   ticket plan is the Worker Birb's brief.

After a round the verdict is computed (checks re-run on the finished tree,
review), each worker's report is read, and an **evaluation** stage re-plans
only what is still open. That repeats up to ``max_rounds``, and stops early
when a round changed nothing.

**Every stage is enforced by the harness, not asked for.** The first staged
planner offered a seal tool in every step, and the strongest model measured
sealed in step 1 on every seed, so no skeleton step ever ran. Here each stage
gets only the tools it needs, a gate refuses a write outside the stage's files
before anyone is asked about it, and the harness seals — there is no seal tool.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..orchestrator import Orchestrator
from ..policy import READ_TOOLS, patch_target
from ..runtime.hooks import EVENT_BEFORE_TOOL, HookOutcome
from .brainy import NEED_TO_KNOW_DIRECTIVE
from .charter import Charter, CharterError
from .plan import PlanDraft

AUTONOMY_ASK = "ask"
AUTONOMY_AUTO = "auto"
DEFAULT_MAX_ROUNDS = 5
# How many times a section is asked for before planning gives up. Two was too
# few: the first overnight run lost 7 of its 13 failed staged rounds here.
SECTION_ATTEMPTS = 3

_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})

# The overview's sections, in order: heading, then what the section must
# contain as a checklist. Fixed headings in a fixed order, because a missing
# heading is trivial to detect and a weak model fills a checklist better than it
# interprets prose.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("What is asked", """\
- One short paragraph: what the user wants, in your own words.
- The hard constraints, as a bullet list.
- What is out of scope, as a bullet list (write "none" if nothing)."""),
    ("Decisions", """\
- A numbered list of every design choice the request leaves open: language or \
library, formats, limits, behaviour at the edges.
- Each item on one line: `N. <the question> — proposal: <your answer> — why: <one reason>`.
- Only choices that change what gets built. Write "none" if the request settles everything."""),
    ("Architecture", """\
- The parts of the system and what each is responsible for, as a bullet list.
- How data and control move between them, in two or three sentences.
- Name every part exactly as it will appear in code (`engine.step`, not "the step function")."""),
    ("Seams", """\
- Every place where two tickets' code meets, one bullet each.
- For each: the file (or `file::symbol`), the exact signature or data shape, and \
what it promises, e.g. "returns None for a missing key".
- Mark each `(finished)` if it is a shared file written complete now (types, \
constants), or `(stub)` if a ticket implements it."""),
    ("Tickets", """\
- One block per ticket, exactly in this form:

  ### ticket: <id>
  - writes: <every file this ticket creates or changes, comma-separated>
  - tests: <its test files, comma-separated — at least one>
  - accept: <the command that proves it is done, e.g. python -m pytest tests/test_x.py -q>
  - needs: <ticket ids that must finish first, or none>
  - builds: <what it builds, one sentence>
  - done when: <one sentence>

- No file may appear in two tickets' `writes`. A `(finished)` seam file is in no ticket.
- A ticket's tests must pass with its own code and the skeleton alone.
- If this work should not be divided at all, write exactly `NO TICKETS` and one \
sentence saying why."""),
    ("Risks", """\
- What could go wrong, as a bullet list, each with what you will do about it.
- Write "none" if you see none."""),
)

_TICKET_HEADING = re.compile(r"^\s{0,4}#{2,4}\s*ticket\s*[:：]\s*`?([A-Za-z0-9_.-]+)`?\s*$", re.I | re.M)
_FIELD = re.compile(r"^\s*[-*]\s*([a-z ]+?)\s*:\s*(.*)$", re.I)


@dataclass
class TicketSpec:
    """One ticket as the overview (or an evaluation) describes it."""

    id: str
    writes: tuple[str, ...]
    tests: tuple[str, ...]
    accept: str
    needs: tuple[str, ...] = ()
    builds: str = ""
    done: str = ""

    def block(self) -> str:
        """The ticket in the same form the overview uses."""
        return "\n".join([
            f"### ticket: {self.id}",
            f"- writes: {', '.join(self.writes)}",
            f"- tests: {', '.join(self.tests)}",
            f"- accept: {self.accept}",
            f"- needs: {', '.join(self.needs) or 'none'}",
            f"- builds: {self.builds}",
            f"- done when: {self.done}",
        ])


def _paths(value: str) -> tuple[str, ...]:
    parts = [p.strip().strip("`").strip() for p in re.split(r"[,\s]+", value) if p.strip()]
    return tuple(os.path.normpath(p) for p in parts if p and p.lower() != "none")


def parse_tickets(text: str) -> list[TicketSpec]:
    """Every ticket block in ``text``; tolerant of spacing, backticks and case."""
    heads = list(_TICKET_HEADING.finditer(text))
    tickets = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        fields: dict[str, str] = {}
        for line in text[head.end():end].splitlines():
            match = _FIELD.match(line)
            if match:
                fields[match.group(1).strip().lower()] = match.group(2).strip()
        needs = tuple(
            n.strip().strip("`") for n in fields.get("needs", "").split(",")
            if n.strip() and n.strip().lower() != "none"
        )
        tickets.append(TicketSpec(
            id=head.group(1),
            writes=_paths(fields.get("writes", "")),
            tests=_paths(fields.get("tests", "")),
            accept=fields.get("accept", "").strip().strip("`"),
            needs=needs,
            builds=fields.get("builds", ""),
            done=fields.get("done when", fields.get("done", "")),
        ))
    return tickets


def check_tickets(tickets: list[TicketSpec]) -> str:
    """Why these tickets cannot become a charter, or "" if they can.

    Run through a scratch ``PlanDraft`` — the same checks the charter moves
    apply — so a ticket table that overlaps is caught while the overview is
    still being written, with one path named, rather than after the skeleton.
    """
    if not tickets:
        return "no ticket blocks were found — each needs a `### ticket: <id>` heading"
    # A file in two tickets, said in the overview's own terms. The charter
    # moves' refusal ("call drop_worker…") names a tool no overview stage has,
    # and every model in the first overnight run that met it failed the same
    # way twice — most often by listing another ticket's test file under its
    # own `tests`, meaning "the tests I must pass".
    owners: dict[str, list[str]] = {}
    for ticket in tickets:
        for path in dict.fromkeys((*ticket.writes, *ticket.tests)):
            owners.setdefault(path, []).append(ticket.id)
    for path, who in owners.items():
        if len(who) > 1:
            return (
                f"`{path}` is listed by more than one ticket ({', '.join(repr(w) for w in who)}). "
                "Every file belongs to exactly one ticket: keep it only in the ticket that writes it. "
                "A ticket's `tests` are its own test files, never another ticket's"
            )
    draft = PlanDraft()
    for ticket in tickets:
        if not ticket.tests:
            return f"ticket {ticket.id!r} names no test files in `tests` — every ticket needs at least one"
        if not ticket.accept:
            return f"ticket {ticket.id!r} has no `accept` command"
        try:
            draft.add_worker(ticket.id, brief="-", writes=list(ticket.writes), accept=ticket.accept,
                             tests=list(ticket.tests), needs=list(ticket.needs))
        except CharterError as exc:
            return str(exc)
    try:
        draft.seal("check")
    except CharterError as exc:
        return str(exc)
    return ""


class _GatedHooks:
    """The user's hooks, with this stage's file scope checked first.

    A write outside the stage's files is refused through the ``before_tool``
    path, which the orchestrator runs **before** the policy — so it is refused
    without anyone being asked about it, the way a stage boundary should be.
    Everything else goes to the user's own hooks unchanged.
    """

    def __init__(self, base: Any, cwd: str, allowed: Callable[[str], bool], why: str) -> None:
        self._base = base
        self._cwd = cwd
        self._allowed = allowed
        self._why = why

    def fire(self, event: str, **kwargs: Any) -> HookOutcome:
        if event == EVENT_BEFORE_TOOL and kwargs.get("tool_name") in _WRITE_TOOLS:
            arguments = kwargs.get("arguments") or {}
            target = arguments.get("path")
            if kwargs.get("tool_name") == "apply_patch" and not target:
                target = patch_target(arguments)
            path = os.path.normpath(os.path.relpath(os.path.realpath(os.path.join(self._cwd, str(target or ""))),
                                                    os.path.realpath(self._cwd)))
            if not target or path.startswith("..") or not self._allowed(path):
                return HookOutcome(blocked=True, reason=f"Not in this step: {self._why}")
        return self._base.fire(event, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


@dataclass
class Design:
    """Brainy Birb's design documents, carried from stage to stage."""

    objective: str
    sections: dict[str, str] = field(default_factory=dict)
    tickets: list[TicketSpec] = field(default_factory=list)
    plans: dict[str, str] = field(default_factory=dict)

    def document(self) -> str:
        parts = [f"# Design\n\n## The request\n\n{self.objective.strip()}"]
        for heading, _ in SECTIONS:
            if heading in self.sections:
                parts.append(f"## {heading}\n\n{self.sections[heading].strip()}")
        return "\n\n".join(parts)


INTRO = """\
You are Brainy Birb, the lead engineer of a Flock. You divide a piece of work \
between Worker Birbs who never speak to each other and see nothing but the \
ticket plan you write for them. You plan in stages, and every stage gives you \
only what it needs."""


def section_prompt(design: Design, heading: str, checklist: str, problem: str = "") -> str:
    written = design.document()
    lines = [
        INTRO, "", written, "",
        f"--- Write the next section: {heading} ---", "",
        f"Reply with the content of the \"{heading}\" section only, in Markdown. "
        "It must contain:", "", checklist, "",
        "Use the read tools to look at the project if you need to. Do not write any files.",
    ]
    if heading == "Tickets":
        lines += ["", NEED_TO_KNOW_DIRECTIVE]
    if problem:
        lines += ["", f"Your last answer for this section could not be used: {problem}",
                  "Write the whole section again with that fixed."]
    return "\n".join(lines)


SKELETON_PROMPT = """\
{intro}

{document}

--- Stage: the skeleton ---

Write the skeleton into the project now:

- Every `(finished)` seam file, complete — shared types and constants.
- For every ticket, the files in its `writes` that are not tests, as typed stubs: \
every class and function the ticket implements, fully typed, with a docstring \
that states what it does ("returns None for a missing key", not "handles keys"), \
and a body that raises NotImplementedError — never one that returns a plausible value.

Do NOT write test files: each ticket's tests are written in its own stage, next. \
When the skeleton is written, stop and say which files you wrote."""


TICKET_PROMPT = """\
{intro}

{document}

--- Stage: the plan for ticket '{id}' ---

{block}
{previous}
This stage is about this ticket alone. Two things to do.

**1. Write this ticket's tests** in {tests}. They are the Worker Birb's \
acceptance criteria, and they must:

- test only the contract: the signatures and data shapes this ticket implements. \
Given this input, this output.
- use `pytest.mark.parametrize` over the relevant cases wherever the cases \
differ only in data. Keep each test short. Do not build elaborate structures \
inside a test.
- never rely on how the rest of the system is designed, and never steer toward \
one particular implementation.
- fail against the stubs as they are now.

**2. Reply with the ticket plan** — the Worker Birb's whole brief. It never sees \
the design above, only this, so everything it needs must be in it. Use exactly \
these headings:

## Job
Two lines: what to implement.
## Files
Which files it may change, and which it should read but not change.
## Done when
The command in `accept`, and that its tests pass unchanged.
## Contract
The signatures and data shapes it implements and uses, copied exactly.
## Procedure
Each function as numbered steps, in order, with every exact value: limits, \
formats, messages, edge cases and what happens in each.
## Example
One worked example, step by step, with the values it produces.
## How to work
The order to implement in; run the check after each function; if a test and \
this plan disagree the plan is right; if something is impossible, say which \
step and why.
## Out of scope
What other parts of the system handle, without describing them.

State every requirement as a local fact of this ticket, without the reason \
behind it ("`step` runs in O(length of the snake)", never "because the game \
renders at 60 fps"). Do not mention the other tickets or the overall feature."""


EVALUATE_PROMPT = """\
{intro}

{document}

--- Stage: after round {round} ---

The round has finished. CoBirb re-ran every ticket's check on the finished tree \
and reviewed the work. This is the result, and it is the evidence — a worker's \
own account is not:

{verdict}

Decide the next round. Reply with ticket blocks, in exactly the form used in \
the Tickets section, for:

- each ticket that is not done and should be tried again (same id; change \
`writes` only if it truly needs another file), and
- any new ticket the work turns out to need.

Leave out tickets that are complete. Under each block add one line \
`- why: <what the next attempt must do differently>`. If a report says a test \
contradicts the contract, say so in `why` — its test is rewritten next round.

If everything is done, or another round would not change the outcome, reply \
exactly `NO TICKETS` and one sentence why."""


def evaluation_why(text: str) -> dict[str, str]:
    """The `- why:` line under each ticket block, by ticket id."""
    whys: dict[str, str] = {}
    heads = list(_TICKET_HEADING.finditer(text))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        for line in text[head.end():end].splitlines():
            match = _FIELD.match(line)
            if match and match.group(1).strip().lower() == "why":
                whys[head.group(1)] = match.group(2).strip()
    return whys


class Stager:
    """Runs the stages for one flock engagement.

    Holds the design documents between stages and between rounds. Each stage is
    a fresh ``Orchestrator`` — its own context — built on Brainy Birb's model,
    the session's policy and grants (so the user's consent works as it always
    has), the main front-end (so the Flock tab shows the work), and a gate
    limiting which files the stage may write.
    """

    def __init__(self, main: Orchestrator, cwd: str, objective: str, *,
                 turns: int, trace: list[dict], decide: Callable[[str], "str | None"] | None = None,
                 show: Callable[[str], None] | None = None) -> None:
        self.main = main
        self.cwd = cwd
        self.design = Design(objective=objective)
        self.turns = turns
        self.trace = trace
        self.decide = decide
        self.show = show or (lambda text: None)

    # ------------------------------------------------------------------ #
    def _stage(self, tools: set[str], allowed: Callable[[str], bool] | None, why: str) -> Orchestrator:
        main = self.main
        hooks = main.hooks
        if allowed is not None:
            hooks = _GatedHooks(main.hooks, self.cwd, allowed, why)
        return Orchestrator(
            model=main.model,
            tools={name: tool for name, tool in main.tools.items() if name in tools},
            policy=main.policy,
            io=main.io,
            session=None,
            project_context=main.project_context,
            redact_secrets=main.redact_secrets,
            verify=None,
            checkpoints=main.checkpoints,
            hooks=hooks,
            grants=main.grants,
            max_turns=self.turns,
        )

    def _run(self, orchestrator: Orchestrator, step: str, prompt: str) -> str:
        try:
            session = orchestrator.run(prompt, system="", cwd=self.cwd, label="Brainy Birb",
                                       max_turns=self.turns)
        except RuntimeError:
            # Sent once more. Staged planning makes many more model calls than
            # one prompt does, and in the first overnight run one model lost
            # four of six rounds to a single dropped connection each. A second
            # failure is real and goes to the caller.
            session = orchestrator.run(prompt, system="", cwd=self.cwd, label="Brainy Birb",
                                       max_turns=self.turns)
        self.trace.append({"step": step, "calls": [c["name"] for c in orchestrator.last_run_tool_calls]})
        return (session.summary or "").strip()

    # ------------------------------------------------------------------ #
    def overview(self) -> str:
        """Write the overview. Returns "" when done, or why it could not be.

        A declined division ("NO TICKETS …") returns the model's reason with
        the prefix ``declined:``.
        """
        reader = self._stage(set(READ_TOOLS), None, "")
        for heading, checklist in SECTIONS:
            problem = ""
            for _attempt in range(SECTION_ATTEMPTS):
                text = self._run(reader, f"overview: {heading}",
                                 section_prompt(self.design, heading, checklist, problem))
                problem = self._check_section(heading, text)
                if not problem or problem.startswith("declined:"):
                    break
                # Kept, so a failed section can be read back afterwards.
                self.trace[-1].update(problem=problem, text=text[:4000])
            if problem and not problem.startswith("declined:"):
                return f"the {heading} section could not be completed: {problem}"
            self.design.sections[heading] = text
            if problem.startswith("declined:"):
                return problem
            if heading == "Decisions":
                self._put_decisions()
            if heading == "Tickets":
                self.design.tickets = parse_tickets(text)
            self.show(f"Overview: {heading} written.")
        return ""

    def _check_section(self, heading: str, text: str) -> str:
        if not text:
            return "it was empty"
        if heading == "Tickets":
            if text.lstrip().upper().startswith("NO TICKETS"):
                return "declined: " + text.lstrip()[len("NO TICKETS"):].strip(" .:—-")
            return check_tickets(parse_tickets(text))
        return ""

    def _put_decisions(self) -> None:
        """In ``ask`` mode, the decisions go to the user before anything is built."""
        text = self.design.sections.get("Decisions", "")
        if self.decide is None or not text or text.strip().lower().startswith("none"):
            return
        answer = self.decide(text)
        if answer and answer.strip():
            self.design.sections["Decisions"] = (
                f"{text}\n\n**The user's answers — these override the proposals above:**\n\n{answer.strip()}"
            )

    def skeleton(self, tickets: list[TicketSpec]) -> None:
        tests = {path for ticket in tickets for path in ticket.tests}
        stage = self._stage(set(READ_TOOLS) | _WRITE_TOOLS, lambda path: path not in tests,
                            "test files are written in each ticket's own stage, after the skeleton.")
        self._run(stage, "skeleton", SKELETON_PROMPT.format(intro=INTRO, document=self.design.document()))

    def ticket_plan(self, ticket: TicketSpec, previous: str = "") -> str:
        """Write one ticket's tests and return its ticket plan (its brief)."""
        tests = set(ticket.tests)
        stage = self._stage(set(READ_TOOLS) | _WRITE_TOOLS, lambda path: path in tests,
                            f"this stage writes only the tests of ticket {ticket.id!r}: {', '.join(ticket.tests)}.")
        prior = f"\nThe last round's attempt at this ticket, and what happened:\n\n{previous}\n" if previous else ""
        prompt = TICKET_PROMPT.format(intro=INTRO, document=self.design.document(), id=ticket.id,
                                      block=ticket.block(), previous=prior, tests=", ".join(ticket.tests))
        text = ""
        for _attempt in range(2):
            text = self._run(stage, f"ticket: {ticket.id}", prompt)
            missing = [p for p in ticket.tests if not os.path.isfile(os.path.join(self.cwd, p))]
            if len(text) >= 200 and not missing:
                break
            prompt += (
                "\n\nThat stage is not finished: "
                + ("the ticket plan in your reply was missing or too short; " if len(text) < 200 else "")
                + (f"these test files were not written: {', '.join(missing)}. " if missing else "")
                + "Do both now."
            )
        self.design.plans[ticket.id] = text
        return text

    def evaluate(self, round_number: int, verdict: str,
                 outstanding: "list[str] | None" = None) -> "tuple[list[TicketSpec], dict[str, str], str]":
        """The next round's tickets, the reason for each, and the reply.

        **Stopping takes an explicit `NO TICKETS`.** A reply that could not be
        read, or whose tickets cannot become a charter, used to end the flock —
        in the first overnight run, four runs stopped after round 1 with
        tickets still failing. Now the tickets whose checks still fail are
        simply tried again, with the evaluation's reply as the reason.
        """
        reader = self._stage(set(READ_TOOLS), None, "")
        text = self._run(reader, f"evaluate round {round_number}", EVALUATE_PROMPT.format(
            intro=INTRO, document=self.design.document(), round=round_number, verdict=verdict))
        if text.lstrip().upper().startswith("NO TICKETS"):
            return [], {}, text
        tickets = parse_tickets(text)
        if tickets and not check_tickets(tickets):
            return tickets, evaluation_why(text), text
        retry = [t for t in self.design.tickets if t.id in set(outstanding or ())]
        why = "its check still fails — see the report" + (f"; the evaluation said: {text[:600]}" if text else "")
        return retry, {t.id: why for t in retry}, text

    # ------------------------------------------------------------------ #
    def charter(self, tickets: list[TicketSpec], briefs: dict[str, str]) -> Charter:
        """Seal these tickets into a charter — the harness's move, not the model's."""
        draft = PlanDraft()
        known = {t.id for t in tickets}
        for ticket in tickets:
            draft.add_worker(
                ticket.id, brief=briefs.get(ticket.id) or ticket.builds or ticket.id,
                writes=list(ticket.writes), accept=ticket.accept, tests=list(ticket.tests),
                # A dependency on a ticket already finished in an earlier round
                # is satisfied; only ones in this round are waited for.
                needs=[n for n in ticket.needs if n in known],
            )
        return draft.seal(self.design.objective)


def approval_changes(charter: Charter, approved: list[Charter]) -> str:
    """What a later round's charter asks for that no earlier approval covered."""
    files = {p for c in approved for w in c.workers for p in w.writes}
    commands = {w.accept for c in approved for w in c.workers}
    lines = []
    for worker in charter.workers:
        new_files = [p for p in worker.writes if p not in files]
        line = f"  {worker.id}: writes {', '.join(worker.writes)}"
        if new_files:
            line += f"   NEW: {', '.join(new_files)}"
        if worker.accept and worker.accept not in commands:
            line += f"   NEW command: {worker.accept}"
        lines.append(line)
    fresh = any("NEW" in line for line in lines)
    head = ("This round asks for files or commands you have not approved before:"
            if fresh else "Everything this round touches, you approved in an earlier round:")
    return head + "\n" + "\n".join(lines)
