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
1. **Restatement**: Brainy Birb restates the whole design in precise, literal
   language (``CLEAR_RULES``) — the *cleared* design — and renames every name
   it invented, keeping a mapping (``NameMap``). Names the user's request
   states are requirements and are kept. The harness refuses a restatement in
   which a renamed name survives.
2. **Skeleton**, by **Architect Birb**: the finished shared files and the typed
   stubs. The charter's tickets come from the cleared ticket blocks, derived
   rather than written a second time.
3. **One fresh Architect Birb stage per ticket**: the cleared design plus that
   ticket and nothing else. It writes that ticket's tests, and its reply — the
   ticket plan — is the Worker Birb's brief.

Everything a Worker Birb can see — the skeleton, its tests, its brief — is
written from the cleared design by stages that never saw the request or
Brainy Birb's own wording. Before, Brainy Birb wrote the skeleton and tests
itself and only the brief was restated, so a worker was told one thing in
literal terms while the files it read carried the planner's slang and assumed
knowledge: a leak across the need-to-know boundary. Architect Birb is the one
agent that sees the whole shape of the feature, in cleared form only.

After a round the verdict is computed (checks re-run on the finished tree,
review), each worker's report is read, and an **evaluation** stage — Brainy
Birb, against its own design and the mapping — re-plans only what is still
open. Its ticket blocks are restated the same way before Architect Birb sees
them. That repeats up to ``max_rounds``, and stops early when a round changed
nothing.

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

- Every ticket is implementation work that one Worker Birb does. Do NOT make a ticket for tests, \
for the skeleton or for setup: Architect Birb writes the skeleton and every ticket's tests, in the \
next stages. A ticket's `tests` are the test files for its own code, and it `writes` them too.
- No file may appear in two tickets. A `(finished)` seam file is in no ticket.
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


_NAME_LINE = re.compile(r"^\s*[-*]\s*(kept|renamed)\s*:\s*(.+?)\s*$", re.I)
_ARROW = re.compile(r"\s*(?:->|→|=>)\s*")
# What counts as "the same name" when checking a restatement for survivors:
# an identifier, path or command bounded by anything that could not continue it.
_NAME_EDGE = r"A-Za-z0-9_"


def _is_test_file(path: str) -> bool:
    """Named the way pytest collects: ``test_*.py`` or ``*_test.py``."""
    base = os.path.basename(path)
    return base.startswith("test_") or base.endswith("_test.py")


def _bare(name: str) -> str:
    """The name itself: the first `backticked` span if there is one, since a
    model writes "- kept: `Store` class" as often as "- kept: Store"."""
    quoted = re.search(r"`([^`]+)`", name)
    if quoted:
        return quoted.group(1).strip()
    return name.strip().strip("`'\"").strip()


@dataclass
class NameMap:
    """The names Brainy Birb restated, and the user's names it kept.

    **Two kinds of name, treated differently.** A name the user's request
    states — a `/kill` command, a file they named — is a requirement, so it is
    kept exactly. A name Brainy Birb invented is restated into a precise,
    literal one (`kill_children` → `terminate_child_processes`), because a
    Worker Birb reads the skeleton and a name carries the planner's slang and
    assumed knowledge as surely as prose does. Code that implements a kept name
    still gets an invented internal name like any other.

    Kept by Brainy Birb (for the evaluation) and shown to the user at charter
    approval; never given to Architect Birb or a Worker Birb.
    """

    renamed: dict[str, str] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.renamed or self.kept)

    def forward(self, name: str) -> str:
        return self.renamed.get(name, name)

    def merged(self, other: "NameMap") -> "tuple[NameMap, str]":
        """Both maps as one, or a problem when they disagree about a name."""
        renamed = dict(self.renamed)
        for old, new in other.renamed.items():
            if renamed.get(old, new) != new:
                return self, f"`{old}` was already renamed to `{renamed[old]}`; keep that name"
            renamed[old] = new
        targets: dict[str, str] = {}
        for old, new in renamed.items():
            if new in targets and targets[new] != old:
                return self, f"`{targets[new]}` and `{old}` were both renamed to `{new}`; every name needs its own"
            targets[new] = old
        kept = list(dict.fromkeys([*self.kept, *other.kept]))
        return NameMap(renamed, kept), ""

    def survivors(self, text: str) -> list[str]:
        """The renamed names that still appear in ``text``.

        Kept names and the new names are blanked out first, so `/kill` kept
        for the user does not count as `kill` surviving, and `engine` renamed
        to `engine.core` does not count as `engine`.
        """
        for name in sorted({*self.kept, *self.renamed.values()}, key=len, reverse=True):
            if name:
                text = text.replace(name, " ")
        return [
            old for old in self.renamed
            if re.search(rf"(?<![{_NAME_EDGE}]){re.escape(old)}(?![{_NAME_EDGE}])", text)
        ]

    def describe(self) -> str:
        """Plain text, one name a line; "" when there is nothing to say."""
        lines = [f"  {name}  (yours, kept)" for name in self.kept]
        lines += [f"  {old} → {new}" for old, new in self.renamed.items()]
        return "\n".join(lines)


def parse_names(text: str) -> NameMap:
    """The `- kept:` and `- renamed: old -> new` lines of a Names section."""
    names = NameMap()
    for line in text.splitlines():
        match = _NAME_LINE.match(line)
        if not match:
            continue
        kind, value = match.group(1).lower(), match.group(2)
        if kind == "kept":
            name = _bare(value)
            if name and name.lower() != "none" and name not in names.kept:
                names.kept.append(name)
            continue
        parts = _ARROW.split(value, maxsplit=1)
        if len(parts) == 2 and _bare(parts[0]) and _bare(parts[1]) and _bare(parts[0]) != _bare(parts[1]):
            names.renamed[_bare(parts[0])] = _bare(parts[1])
    return names


def _expected_blocks(raw: "list[TicketSpec]", names: NameMap) -> str:
    """The ticket blocks a restatement should contain, its own renames applied.

    Ids, files and commands are structured data, and asking a model to carry
    them through a rewrite of the whole design is where it most often failed:
    both models in the first benchmark runs lost the block form at least once.
    So a refusal shows the blocks with everything mechanical already mapped,
    leaving only the two sentences that are prose to restate.
    """
    if not raw:
        return ""

    def path(p: str) -> str:
        if p in names.renamed:
            return names.renamed[p]
        base = os.path.basename(p)
        return os.path.join(os.path.dirname(p), names.renamed[base]) if base in names.renamed else p

    def command(text: str) -> str:
        for old, new in sorted(names.renamed.items(), key=lambda item: len(item[0]), reverse=True):
            text = re.sub(rf"(?<![{_NAME_EDGE}]){re.escape(old)}(?![{_NAME_EDGE}])", new, text)
        return text

    blocks = [
        TicketSpec(id=names.forward(t.id), writes=tuple(path(p) for p in t.writes),
                   tests=tuple(path(p) for p in t.tests), accept=command(t.accept),
                   needs=tuple(names.forward(n) for n in t.needs),
                   builds="<one sentence, restated>", done="<one sentence, restated>").block()
        for t in raw
    ]
    return ("\nWrite the Tickets section as exactly these blocks, filling in `builds` and "
            "`done when` in restated words, and change anything else only to apply a rename "
            "you list under Names:\n\n" + "\n\n".join(blocks))


def request_literals(request: str) -> tuple[str, ...]:
    """The double-quoted values in the user's request: requirements, verbatim.

    A capture with space at either end is the gap between two quoted values
    (an unpaired quote shifted the pairing), not a value, and is skipped.
    """
    return tuple(dict.fromkeys(m for m in re.findall(r'"([^"\n]{1,80})"', request) if m and m == m.strip()))


def split_sections(text: str) -> dict[str, str]:
    """A reply's ``## Heading`` sections, by heading. ``###`` stays inside."""
    sections: dict[str, str] = {}
    heads = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        sections[head.group(1).strip().strip("#").strip()] = text[head.end():end].strip()
    return sections


# The cleared design's sections: the request, restated, then the overview's.
CLEARED_HEADINGS: tuple[str, ...] = ("The request", *(heading for heading, _ in SECTIONS))


@dataclass
class Design:
    """The design documents, carried from stage to stage.

    ``sections`` are Brainy Birb's own words; ``cleared`` the restatement
    Architect Birb works from. ``tickets`` and ``plans`` are the cleared ones —
    the ids, files and briefs the charter and every later round are keyed by.
    """

    objective: str
    sections: dict[str, str] = field(default_factory=dict)
    tickets: list[TicketSpec] = field(default_factory=list)
    plans: dict[str, str] = field(default_factory=dict)
    cleared: dict[str, str] = field(default_factory=dict)
    names: NameMap = field(default_factory=NameMap)

    def document(self) -> str:
        """Brainy Birb's design, the request included. Never shown to Architect Birb."""
        parts = [f"# Design\n\n## The request\n\n{self.objective.strip()}"]
        for heading, _ in SECTIONS:
            if heading in self.sections:
                parts.append(f"## {heading}\n\n{self.sections[heading].strip()}")
        return "\n\n".join(parts)

    def cleared_document(self) -> str:
        """The restated design: all Architect Birb is given."""
        parts = ["# Design"]
        for heading in CLEARED_HEADINGS:
            if heading in self.cleared:
                parts.append(f"## {heading}\n\n{self.cleared[heading].strip()}")
        return "\n\n".join(parts)

    def record(self) -> str:
        """Everything, for the user and the flock's encrypted session."""
        parts = [self.document()]
        if self.names:
            parts.append("## Names restated for Architect Birb and the Worker Birbs\n\n" + self.names.describe())
        if self.cleared:
            parts.append("# The restated design\n\n" + self.cleared_document().removeprefix("# Design").strip())
        return "\n\n".join(parts)


INTRO = """\
You are Brainy Birb, the lead engineer of a Flock. You divide a piece of work \
between Worker Birbs who never speak to each other and see nothing but the \
ticket plan you write for them. You plan in stages, and every stage gives you \
only what it needs."""

# Architect Birb never sees the request or Brainy Birb's wording, and is not
# told that there is anything it has not seen: the cleared design is simply
# the design, as far as it knows.
ARCHITECT_INTRO = """\
You are Architect Birb, the engineer who prepares the work of a Flock. You \
write the skeleton — shared types and typed stubs — and every ticket's tests \
and ticket plan. Worker Birbs then implement the tickets; they never speak to \
each other and see nothing but the ticket plan you write for them and the \
files in the project. You work in stages, and every stage gives you only what \
it needs."""


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

{blocks}

Write the skeleton for these tickets into the project now:

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


# Run on the design before anyone but Brainy Birb works from it. The design is
# written in Brainy Birb's own words, with the request and everything it knows
# of the project behind them; Architect Birb and the Worker Birbs have none of
# that, so a term that is plain to the planner ("kill the children", "the usual
# handshake") can be ambiguous to them, and a model that reads it the wrong way
# refuses benign work. Names are restated too: the skeleton is made of them.
CLEAR_RULES = """\
Restate the text in precise, literal technical language, so that a reader
with no shared context understands exactly what is to be built.
- Keep the meaning exactly: every behaviour, every requirement. Leave nothing
  out and water nothing down.
- Copy every literal exactly as the design gives it: output strings, messages,
  formats, numbers, limits, code, signatures, file paths and commands. `"OK"`
  stays `"OK"`, and `"1"`/`"0"` stays `"1"`/`"0"`. The only change to a literal
  is a name you rename under Names.
- Replace slang and ambiguous everyday words with the technical terms they
  stand for.
- Wherever a term relies on assumed knowledge — a protocol, format, standard,
  acronym, tool or domain term — keep the term and add, in parentheses, what it
  consists of and what happens when it is used, as far as that matters to this
  task. Describe the term's standard meaning only; do not add features,
  options or behaviour it does not imply.
- Unpack one level deep. Do not explain a term inside an explanation.
- Do not add claims about who is allowed to do what, or why.

Names:
- A name the user's request states itself — a command, file, function, option
  or message the user asked for by name — is a requirement. Keep it exactly,
  and list it as `- kept: <name>`.
- Every name you invented — a file path, module, class, function, method,
  variable, constant, test file, ticket id — restate as a precise, literal name
  for what it does or holds, unless it already is one. List each one you
  change as `- renamed: <old> -> <new>`. A name whose form a tool or
  convention depends on keeps that form, and only the part you chose is
  restated: a test file or test function keeps its `test_` prefix
  (`test_kill.py` -> `test_send_sigterm.py`), and `__init__.py`,
  `conftest.py`, `__main__.py` and dunder methods stay as they are. Code that implements a kept name gets
  an invented internal name like any other: a `/kill` command the user asked
  for stays `/kill`, and its handler `kill` becomes `send_sigterm`.
- The Flock's own terms are not names in the design: Flock, Brainy Birb,
  Architect Birb, Worker Birb, ticket, ticket plan. Never list or rename them.
- After this, use only the new names, everywhere — in prose, signatures, file
  lists and commands. An old name must not appear anywhere in your reply
  except on its own `- renamed:` line."""

CLEAR_PROMPT = """\
{intro}

{document}

--- Stage: restate the design ---

Everything after this stage — the skeleton, the tests and every Worker Birb's \
ticket plan — is written by Architect Birb, who never sees the request or the \
design above. It sees only the restatement you write now, so everything it \
needs must be in it.

{rules}

Reply with exactly these sections, in this order, each under its own `## ` \
heading, and nothing else:

## Names
The `- kept:` and `- renamed:` lines. Write `- none` if there are none.
{headings}

Restate every section of the design above, including the request. The Tickets \
section keeps exactly the block form it has now — the same tickets, with their \
ids, files and commands under the new names."""

CLEAR_ROUND_PROMPT = """\
{intro}

{document}

--- Stage: restate the next round ---

The names restated so far — use the new names, and keep the kept ones:

{names}

These are the ticket blocks for the next round. Architect Birb, who never \
sees the design above, plans them from what you write now.

{rules}

{blocks}

Reply with exactly these two sections, each under its own `## ` heading, and \
nothing else:

## Names
The `- kept:` and `- renamed:` lines for names that are new in these blocks. \
Write `- none` if there are none.
## Tickets
The same ticket blocks, in exactly the same form, restated — each `- why:` \
line kept under its block."""


EVALUATE_PROMPT = """\
{intro}

{document}

--- Stage: after round {round} ---

The round has finished. CoBirb re-ran every ticket's check on the finished tree \
and reviewed the work. This is the result, and it is the evidence — a worker's \
own account is not:

{verdict}

The tickets, files and reports above use the restated names Architect Birb and \
the Worker Birbs were given. Your names on the left, theirs on the right:

{names}

Decide the next round. Reply with ticket blocks, in exactly the form used in \
the Tickets section, with the restated ids and files, for:

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
    def _stage(self, tools: set[str], allowed: Callable[[str], bool] | None, why: str, *,
               architect: bool = False) -> Orchestrator:
        """A fresh stage. ``architect`` makes it Architect Birb's: no project
        context, since the project's instructions and repo map are written in
        the same uncleared terms as the request — it has the read tools, and
        the files themselves, to orient."""
        main = self.main
        hooks = main.hooks
        if allowed is not None:
            hooks = _GatedHooks(main.hooks, self.cwd, allowed, why)
        stage = Orchestrator(
            model=main.model,
            tools={name: tool for name, tool in main.tools.items() if name in tools},
            policy=main.policy,
            io=main.io,
            session=None,
            project_context="" if architect else main.project_context,
            redact_secrets=main.redact_secrets,
            verify=None,
            # A stage with no tools can change nothing, so there is nothing to
            # snapshot; a whole-tree snapshot either side of it is only cost.
            checkpoints=main.checkpoints if tools else None,
            hooks=hooks,
            grants=main.grants,
            max_turns=self.turns,
        )
        # Under /autopilot a stage refuses rather than asks, like the main
        # agent. The permissions themselves (the project, the sandbox) are
        # already on the shared policy; this is the "nobody asks" half.
        stage.autopilot = bool(getattr(main, "autopilot", False))
        return stage

    def _run(self, orchestrator: Orchestrator, step: str, prompt: str, label: str = "Brainy Birb") -> str:
        try:
            session = orchestrator.run(prompt, system="", cwd=self.cwd, label=label,
                                       max_turns=self.turns)
        except RuntimeError:
            # Sent once more. Staged planning makes many more model calls than
            # one prompt does, and in the first overnight run one model lost
            # four of six rounds to a single dropped connection each. A second
            # failure is real and goes to the caller.
            session = orchestrator.run(prompt, system="", cwd=self.cwd, label=label,
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

    def clear(self) -> str:
        """Restate the design for Architect Birb. "" when done, else why not.

        No tools: it rewrites what it is given. Asked up to
        ``SECTION_ATTEMPTS`` times, each refusal naming what to fix; the
        tickets it produces replace the overview's as the ones the charter
        is built from.
        """
        raw = parse_tickets(self.design.sections.get("Tickets", ""))
        headings = "\n".join(f"## {heading}" for heading in CLEARED_HEADINGS)
        prompt = CLEAR_PROMPT.format(intro=INTRO, document=self.design.document(), rules=CLEAR_RULES,
                                     headings=headings)
        cleared, names, problem = self._restated(prompt, "restate the design", CLEARED_HEADINGS, raw,
                                                 literals=request_literals(self.design.objective))
        if problem:
            return problem
        self.design.cleared = cleared
        self.design.names = names
        self.design.tickets = parse_tickets(cleared["Tickets"])
        return ""

    def clear_round(self, text: str) -> "tuple[list[TicketSpec], dict[str, str], str]":
        """An evaluation's ticket blocks, restated: the tickets, their `why`, or a problem."""
        raw = parse_tickets(text)
        prompt = CLEAR_ROUND_PROMPT.format(
            intro=INTRO, document=self.design.document(), names=self.design.names.describe() or "  (none)",
            rules=CLEAR_RULES, blocks=text.strip())
        cleared, names, problem = self._restated(prompt, "restate the next round", ("Tickets",), raw)
        if problem:
            return [], {}, problem
        self.design.names = names
        return parse_tickets(cleared["Tickets"]), evaluation_why(cleared["Tickets"]), ""

    def _restated(self, prompt: str, step: str, headings: "tuple[str, ...]",
                  raw: list[TicketSpec], literals: "tuple[str, ...]" = ()) -> "tuple[dict[str, str], NameMap, str]":
        stage = self._stage(set(), None, "")
        problem = ""
        for _attempt in range(SECTION_ATTEMPTS):
            asked = prompt if not problem else (
                f"{prompt}\n\nYour last restatement could not be used: {problem}\n"
                "Write the whole reply again with that fixed.")
            text = self._run(stage, step, asked)
            sections, names, problem = self._check_restatement(text, headings, raw, literals)
            if not problem:
                return sections, names, ""
            # Kept, so a failed restatement can be read back afterwards —
            # head and tail, because the Tickets section it most often gets
            # wrong comes last and a 4000-character head never reached it.
            kept = text if len(text) <= 6000 else f"{text[:1500]}\n[…]\n{text[-4500:]}"
            self.trace[-1].update(problem=problem, text=kept)
        return {}, self.design.names, f"the design could not be restated: {problem}"

    def _check_restatement(self, text: str, headings: "tuple[str, ...]", raw: list[TicketSpec],
                           literals: "tuple[str, ...]" = ()) -> "tuple[dict[str, str], NameMap, str]":
        """The restated sections and the names so far, or why they cannot be used.

        **A renamed name that survives is refused, not trusted.** The model
        saying it renamed everything is not the check; the text is.
        """
        sections = split_sections(text)
        missing = [h for h in ("Names", *headings) if h not in sections]
        if missing:
            return {}, self.design.names, (
                "these sections are missing: " + ", ".join(f"`## {h}`" for h in missing))
        names, problem = self.design.names.merged(parse_names(sections["Names"]))
        if problem:
            return {}, self.design.names, problem
        cleared = {h: sections[h] for h in headings}
        survivors = names.survivors("\n".join(cleared.values()))
        if survivors:
            # Said with where, and with the other way out: a bench run renamed
            # the request's own "CLI", was told only that it survived, and
            # spent every attempt failing to scrub it.
            first = survivors[0]
            line = next((ln.strip() for ln in "\n".join(cleared.values()).splitlines()
                         if NameMap({first: ""}).survivors(ln)), "")
            hint = (f" `{first}` is in the user's request: if the request names it as something to "
                    f"build, it is a kept name — list it as `- kept: {first}` instead of renaming it."
                    if NameMap({first: ""}).survivors(self.design.objective) else "")
            return {}, self.design.names, (
                "these names were renamed but still appear in the text: "
                + ", ".join(f"`{s}`" for s in survivors)
                + f" — for example: \"{line[:160]}\". Use only the new names outside the "
                "`- renamed:` lines." + hint)
        # The user's quoted values are requirements, copied exactly. A benchmark
        # run's CLI answered "OK: set" where the request says "OK".
        body = "\n".join(cleared.values())
        lost = [literal for literal in literals if not re.search(
            "[\"'`“‘]" + re.escape(names.forward(literal)) + "[\"'`”’]", body)]
        if lost:
            return {}, self.design.names, (
                "these exact values from the request are missing: "
                + ", ".join(f'"{literal}"' for literal in lost)
                + " — copy every value the request gives exactly")
        tickets = parse_tickets(cleared["Tickets"])
        problem = check_tickets(tickets)
        if problem:
            return {}, self.design.names, (
                f"the restated tickets cannot be used: {problem}." + _expected_blocks(raw, names))
        expected = sorted(names.forward(t.id) for t in raw)
        found = sorted(t.id for t in tickets)
        if expected != found:
            return {}, self.design.names, (
                f"the restated tickets must be the same tickets under their new ids — expected "
                f"{', '.join(expected)}, found {', '.join(found)}." + _expected_blocks(raw, names))
        # A test file renamed out of pytest's naming is never collected. The
        # first benchmark run of this stage renamed `test_roman.py` to
        # `verification_for_roman.py`: a literal name, and a test nobody runs.
        by_id = {t.id: t for t in tickets}
        for ticket in raw:
            if not any(_is_test_file(p) for p in ticket.tests):
                continue
            lost = [p for p in by_id[names.forward(ticket.id)].tests if not _is_test_file(p)]
            if lost:
                return {}, self.design.names, (
                    f"`{lost[0]}` has lost the `test_` prefix pytest needs to find it — "
                    "a test file keeps `test_`; restate only the part after it")
        return cleared, names, ""

    def skeleton(self, tickets: list[TicketSpec]) -> None:
        tests = {path for ticket in tickets for path in ticket.tests}
        stage = self._stage(set(READ_TOOLS) | _WRITE_TOOLS, lambda path: path not in tests,
                            "test files are written in each ticket's own stage, after the skeleton.",
                            architect=True)
        blocks = "\n\n".join(ticket.block() for ticket in tickets)
        self._run(stage, "skeleton", SKELETON_PROMPT.format(
            intro=ARCHITECT_INTRO, document=self.design.cleared_document(), blocks=blocks),
            label="Architect Birb")

    def ticket_plan(self, ticket: TicketSpec, previous: str = "") -> str:
        """Write one ticket's tests and return its ticket plan: the worker's brief."""
        tests = set(ticket.tests)
        stage = self._stage(set(READ_TOOLS) | _WRITE_TOOLS, lambda path: path in tests,
                            f"this stage writes only the tests of ticket {ticket.id!r}: {', '.join(ticket.tests)}.",
                            architect=True)
        prior = f"\nThe last round's attempt at this ticket, and what happened:\n\n{previous}\n" if previous else ""
        prompt = TICKET_PROMPT.format(intro=ARCHITECT_INTRO, document=self.design.cleared_document(),
                                      id=ticket.id, block=ticket.block(), previous=prior,
                                      tests=", ".join(ticket.tests))
        text = ""
        for _attempt in range(2):
            text = self._run(stage, f"ticket: {ticket.id}", prompt, label="Architect Birb")
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
        """The next round's tickets, restated, the reason for each, and the reply.

        **Stopping takes an explicit `NO TICKETS`.** A reply that could not be
        read, or whose tickets cannot become a charter, used to end the flock —
        in the first overnight run, four runs stopped after round 1 with
        tickets still failing. Now the tickets whose checks still fail are
        simply tried again. The reason given for those is the harness's own,
        never the evaluation's words: they are Brainy Birb's, and have not been
        restated.
        """
        reader = self._stage(set(READ_TOOLS), None, "")
        text = self._run(reader, f"evaluate round {round_number}", EVALUATE_PROMPT.format(
            intro=INTRO, document=self.design.document(), round=round_number, verdict=verdict,
            names=self.design.names.describe() or "  (no names were restated)"))
        if text.lstrip().upper().startswith("NO TICKETS"):
            return [], {}, text
        tickets = parse_tickets(text)
        if tickets and not check_tickets(tickets):
            cleared, whys, problem = self.clear_round(text)
            if not problem:
                return cleared, whys, text
        retry = [t for t in self.design.tickets if t.id in set(outstanding or ())]
        return retry, {t.id: "its check still fails — see the report" for t in retry}, text

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


class CharterApproval(str):
    """The text of a charter approval, carrying what it describes.

    A ``str`` — the plain text every asker already prints (the CLI, headless,
    a test) — with the charter, the charters approved before it and the name
    mapping attached, so a front-end that can draw it better does: the TUI
    draws this one in colour, in a dialog wide enough to read. Nothing that
    only reads the text needs to know it is anything more.
    """

    charter: Charter
    approved: "tuple[Charter, ...]"
    names: "NameMap | None"

    def __new__(cls, charter: Charter, approved: "list[Charter] | tuple[Charter, ...]" = (),
                names: "NameMap | None" = None) -> "CharterApproval":
        parts = []
        if approved:
            parts.append(approval_changes(charter, list(approved)))
        parts.append(charter.describe())
        if names:
            parts.append("Names restated for Architect Birb and the Worker Birbs:\n" + names.describe())
        text = super().__new__(cls, "\n\n".join(parts))
        text.charter = charter
        text.approved = tuple(approved)
        text.names = names if names else None
        return text
