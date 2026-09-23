"""Brainy Birb: the lead engineer of a Flock.

Brainy Birb does the part that genuinely needs the whole picture. It plans,
decides whether the work divides at all, designs the seams the workers will
meet at, builds the skeleton, writes one brief per Worker Birb, and afterwards
reads the reports and reviews and says where the round got to.

Almost all of that is judgement, so almost all of this module is a prompt. The
code here is the two things a prompt cannot do: hand the model a way to return
a charter that does not involve writing a file into the user's repository, and
compose the material a round's verdict is written from (``round_summary``).

**There are two routes to a charter and one place it lands.** ``CharterDesk``
holds the plan, the accepted charter and every counter; the five tools over it
are moves. ``DeclareSeamTool``, ``AddWorkerTool``, ``DropWorkerTool`` and
``SealCharterTool`` build a plan up a piece at a time over ``plan.PlanDraft``,
each call checked against what is already there — so an overlapping partition
cannot be constructed. ``ProposeCharterTool`` still takes a whole charter as
TOML, which is less work for a small plan and is validated after the fact.
Both end at ``CharterDesk.accept``, so whichever produced a charter, the
front-end finds it in the same place.

**The S.O.L.I.D. guidance below is deliberately hardcoded rather than left to
the model's instincts.** Dependency Inversion in particular is not a style
preference in a Flock — it is what decides whether a partition can be parallel
at all, and a lead that gets it wrong produces a charter whose workers cannot
actually run independently no matter how carefully their files are separated.
"""
from __future__ import annotations

import logging
from typing import Any

from ..typing.spi import Tool, ToolResult
from .charter import (
    DEFAULT_CONCURRENCY,
    MAX_CONCURRENCY,
    SEAM_KINDS,
    Charter,
    CharterError,
    find_conflicts,
    parse_charter,
)
from .plan import PlanDraft, snapshot_project, written_since
from .supervisor import FlockOutcome, check_partition

logger = logging.getLogger("cobirb")

PROPOSE_CHARTER = "propose_charter"
DECLARE_SEAM = "declare_seam"
ADD_WORKER = "add_worker"
DROP_WORKER = "drop_worker"
SEAL_CHARTER = "seal_charter"

# Every tool Brainy Birb uses to build a charter. Named in one place because
# `run.install_charter_tool` registers and permits all of them together — a
# planning surface with one tool missing is one the transcript still tells the
# model to use.
PLANNING_TOOLS = (PROPOSE_CHARTER, DECLARE_SEAM, ADD_WORKER, DROP_WORKER, SEAL_CHARTER)

# How many times the same refusal, word for word, before the answer stops being
# a correction and becomes an instruction to stop. The incremental moves fail
# locally and cheaply, which is the point of them — but "cheap" is not "free",
# and a model that has been told three times that a path is taken is not going
# to discover on the fourth attempt that it is not.
MAX_REPEATED_REFUSALS = 3

# How many rejected charters before the tool stops asking for another one.
# The planning turn is bounded at 30 turns, so a model that cannot produce
# valid TOML would otherwise spend all of them failing at it — and then, since
# `_plan` retries, spend another thirty. Five is enough attempts for a real
# correction and few enough that the user is not watching a loop.
MAX_CHARTER_ATTEMPTS = 5

# How many *valid* charters with an overlapping partition before the tool stops
# inviting a corrected one. Lower than `MAX_CHARTER_ATTEMPTS` because the model
# has already cleared the hard part — this charter parses, it is held, and the
# user can approve it as it stands — so the only thing more attempts buy is a
# better partition, and a model that did not improve it on the first correction
# does not improve it on the fourth.
MAX_OVERLAP_ATTEMPTS = 2

# What Brainy Birb is told before it plans anything. Long, because every
# paragraph here is either an invariant of the design or a mistake this would
# otherwise make — and short compared to the cost of a partition whose workers
# turn out to depend on each other.
BRAINY_RULES = """\
You are Brainy Birb, the lead engineer of a Flock. You are about to divide a \
piece of work between several Worker Birbs who will never speak to each other \
and will never see anything you do not give them. Each one gets a single \
ticket and knows nothing about the rest of the system — not what the feature \
is, not how many others there are, not what they are building.

That works only if you do your job properly. Your job has four parts.

1. PLAN, AND DECIDE WHETHER THIS DIVIDES AT ALL.
Work a Flock can do must split into disjoint sets of files, with seams that \
can be designed up front. Plenty of work cannot. "This is a single person's \
job, do not fan it out" is a correct and useful answer — say so rather than \
inventing a partition that will not hold.

2. DESIGN THE SEAMS. THIS IS THE PART THAT DECIDES WHETHER THE RUN WORKS.
A seam is where two workers' code meets: a function, a class, a type that one \
of them builds and another uses. You design it by writing its SIGNATURE into \
the skeleton — fully typed, with a docstring that says what it does. That \
signature is the contract. The worker who owns the file fills in the bodies \
and must not change the signature; everyone else builds against it at the \
same time.

So a stub file IS a seam, and it belongs to the worker who implements it. \
Give every file that still has a stub in it to exactly one ticket. A file that \
is finished as you wrote it — shared types, constants — belongs to no ticket, \
and since a worker can change only its own files, nobody can change it.

Apply DEPENDENCY INVERSION wherever it keeps the work parallel: code that \
calls another worker's code depends on the signature you wrote, never on how \
that worker will implement it. The same goes for tests. A ticket's acceptance \
tests must pass using its own code and the skeleton alone. If they need \
another ticket's unfinished code to run, give them a small fake to test \
against instead, or give the ticket `needs` — otherwise that worker spends \
its whole run looking at somebody else's NotImplementedError.

A seam may be FORMAL — an abstract base class, an interface, a typed \
signature, something the type system holds up — or LOOSE: "returns None for a \
missing key", "never raises on a partial record", with nothing holding it up \
but a test, so it MUST have one. `declare_seam` records each for the person \
approving the plan to read; it does not lock anything.

3. BUILD THE SKELETON. IT IS THE ONLY THING THE WORKERS SHARE.
Write the interfaces, the typed stubs, and the failing tests into the project \
before you fan out. This is not scaffolding to be replaced — it is how the \
workers communicate with each other without talking, and it is the only \
context they will ever have.

  - Signatures fully typed, in whatever the project's language is.
  - Docstrings that state SEMANTICS, not intent. "Parses the config" tells a \
worker nothing. "Returns None for a missing key; writes an empty cell rather \
than raising on a partial record; the count excludes the header" is a \
contract someone can implement against blind.
  - Failing tests that pin EVERY behaviour the docstring claims. A promise \
with no test behind it is a promise nothing is holding, and nothing downstream \
will catch it for you: review puts your stub back and requires the tests to go \
red, so a behaviour you stated and never tested passes that check silently.
  - Stubs that fail loudly — raise NotImplementedError, or whatever the \
language's equivalent is. Never a stub that returns a plausible value.

Write all of it in this project's own style, because the skeleton doubles as \
the style guide: a worker filling in your stub inherits your error types, your \
docstring shape and your test idiom by imitation, and it is never told the \
project has conventions.

4. WRITE THE BRIEFS.
One per worker, describing only that worker's part. Do not mention the \
feature, the other workers, or the shape of the whole. A brief should read \
like a good ticket: what to implement, what done looks like, and nothing that \
would let the reader reconstruct the epic.

Workers MAY edit their own test files and SHOULD add tests for what they \
find. Their work is reviewed afterwards against the skeleton you wrote, so \
weakened assertions and vacuous tests are caught — you do not need to defend \
against them in the brief.

A worker can READ the whole project but may only CHANGE the files in its \
`writes` list. Put EVERY file a worker must create or modify in its `writes`: \
a file it needs but you did not list will be refused, and it will have to \
stop and report instead of doing its job. A worker can ask the user for \
anything else it turns out to need — a command to run, a tool nobody gave it \
— but asking costs it time and the user's attention, so it is not a substitute \
for a `writes` list you got right.

5. DO NOT MAKE WORKERS WAIT FOR EACH OTHER UNLESS THEY MUST.
`needs` exists and it is the last thing to reach for. Workers with no `needs` \
all start at once, which is the entire reason for fanning out; every `needs` \
you add removes one of them from that. The partition you want is tickets that \
are genuinely independent because YOU already built the seam they meet at \
in step 3 — that is what the skeleton is for.

Use `needs` only where a worker's job is to BUILD something another worker \
must then build ON, and you could not hoist it into the skeleton yourself. If \
you find yourself giving most workers a `needs`, the partition is wrong: you \
have written a sequence of steps, not a division of labour, and it should \
either be one worker's ticket or a smaller skeleton with a real seam in it.

WHEN YOU ARE READY
Build the skeleton with your file tools first. Then build the charter up a \
piece at a time:

  - `declare_seam` once per seam worth describing, with its kind.
  - `add_worker` once per ticket: its id, its brief, and every file it may \
write. Each call is CHECKED AGAINST THE PLAN SO FAR and answers immediately — \
a file another ticket already writes is refused right there and named. So you \
find out while you are still writing that one ticket, with one path to change.
  - `drop_worker` if a ticket claimed something that turns out to belong to \
another one. Drop it, add it back corrected.
  - `seal_charter` once, with the objective, when every ticket is in. If a \
file you wrote while planning belongs to no ticket, it asks you once whether \
that file is finished.

Add the tickets in whatever order you thought of them; nothing depends on \
writers coming before readers. A charter built this way CANNOT come out with an overlapping \
partition, which is the commonest way a round fails before it starts.

`propose_charter` still takes a whole charter as TOML in one call, and for a \
small plan — two tickets and a seam — that is less work. But it is checked \
after the fact: it can come back overlapping, and then there is no single move \
to blame. Prefer the pieces for anything larger.

Do not write the charter to a file in the project; it belongs to the session. \
The user will read it and approve, edit or reject it before any worker runs.

A CHARTER IN YOUR REPLY IS NOT A CHARTER. Writing the TOML out in your \
answer — in a code block, or as prose, however complete and however \
well-formed — proposes nothing and starts nothing. `seal_charter` and \
`propose_charter` are the only things that create one. If you write it into \
your reply and stop, the run ends with no Flock, and the person waiting on it \
sees a charter on screen and nothing happening. Call the tool.

BUDGET YOUR TURNS. Every file you write costs one, and you have a limited \
number for the whole planning phase — skeleton and charter together. A \
skeleton so large that it uses them all means the run ends before you propose \
anything, which is worse than a smaller skeleton that got proposed. If the \
work needs more files than you have turns, cut the partition down and say so."""

# The shape the model has to produce, shown rather than described. A schema in
# prose gets approximated; an example gets copied.
CHARTER_TEMPLATE = '''\
objective = """
One paragraph: what this round of work is for.
"""
concurrency = 2

[[seams]]
at   = "path/to/module.py::ClassName"
kind = "formal"                  # "formal" = the type system holds it up
what = "What this seam is and who meets at it."

[[seams]]
at   = "path/to/module.py::function_name"
kind = "loose"                   # "loose" = nothing holds it up but a test
what = "The agreement in words, e.g. returns None for a missing key."

[[workers]]
id     = "a"
writes = ["path/to/module.py", "tests/test_module.py"]
tests  = ["tests/test_module.py"]   # which of `writes` are the tests
reads  = ["path/to/shared_types.py"]   # finished files it builds against
accept = "the command that proves this ticket is done"
brief  = """
This worker's ticket. Only its own part.
"""

[[workers]]
id     = "b"
writes = ["path/to/other.py"]
needs  = ["a"]                   # OPTIONAL. Omit it unless b truly cannot
                                 # start until a has finished. Independent
                                 # tickets run at the same time; every `needs`
                                 # you add takes one away.
brief  = """
Another ticket.
"""
'''


class CharterDesk:
    """Where a charter is assembled and kept, however it arrives.

    Two routes reach the same place. Brainy Birb can build the plan up move by
    move (``declare_seam``, ``add_worker``, ``drop_worker``, ``seal_charter``,
    over ``plan.PlanDraft``), or it can send one whole TOML document
    (``propose_charter``). Both end at ``accept``, so there is one place that
    decides what an accepted charter means, one set of counters, and one
    notification to the front-end — rather than two surfaces that drift until
    only the one nobody tests still works.

    It holds the state because the state outlives any one call: a charter has
    to survive the turn that proposed it (the user approves it later, possibly
    after a dialog was dismissed), and the counters have to survive the model's
    next attempt, which is the entire mechanism by which a loop is noticed.
    """

    def __init__(self, cwd: str | None = None, on_proposed: "Any | None" = None) -> None:
        self.cwd = cwd
        # Called with a charter the moment one is accepted. This is what lets a
        # charter proposed outside a flock run reach the user: the front-end
        # decides whether to act on it (it does nothing while a flock is
        # already running and about to ask for approval itself), so the desk
        # does not need to know which of the two situations it is in.
        self.on_proposed = on_proposed
        self.draft = PlanDraft()
        self.charter: Charter | None = None
        self.raw: str = ""
        # What was attempted and what was wrong with it. Without these, a
        # charter that was rejected four times and a decision not to divide
        # the work at all both arrive at the caller as `charter is None` —
        # indistinguishable, so the first gets reported as the second. See
        # `run._plan`.
        self.attempts: int = 0
        self.last_error: str = ""
        # Valid charters whose partition overlapped, and the overlaps the last
        # one had. Counted separately from `attempts`, which only ever braked
        # charters that would not parse: `exhausted` tests `charter is None`,
        # so the moment one parsed the brake came off for the rest of the
        # session — and an overlapping charter is a charter that parsed. That
        # left the accepted-but-overlapping path with no brake at all and
        # nothing but the planning turn budget to stop it.
        self.overlaps: int = 0
        self._last_overlaps: tuple[tuple[str, str], ...] = ()
        # The last refusal, and how many times running it has been identical.
        # A move-level refusal is cheap and specific, which is the point of
        # building a plan incrementally — but a model can still send the same
        # rejected move forever, and nothing else here would notice.
        self._last_refusal: str = ""
        self._repeats: int = 0
        # The project as it stood before planning, and the unowned files the
        # last ownership question named (see `ownership_question`).
        self._before: "dict[str, tuple[int, int]] | None" = None
        self._asked_unowned: tuple[str, ...] = ()

    def reset(self) -> None:
        """Forget everything, before a fresh planning turn.

        The desk now lives as long as the session does, so a second flock would
        otherwise start holding the first one's charter — and half of its
        draft, which is worse: a plan that is partly somebody else's is one no
        refusal makes sense against.
        """
        self.draft = PlanDraft()
        self.charter = None
        self.raw = ""
        self.attempts = 0
        self.last_error = ""
        self.overlaps = 0
        self._last_overlaps = ()
        self._last_refusal = ""
        self._repeats = 0
        self._before = None
        self._asked_unowned = ()

    def watch_skeleton(self, cwd: str) -> None:
        """Remember the project as it is now, before the skeleton is written."""
        self._before = snapshot_project(cwd)

    def ownership_question(self, charter: Charter, retry: str) -> str:
        """Ask once about skeleton files no ticket writes, or return "".

        **A file nobody owns stays exactly as the skeleton left it.** That is
        right for a finished shared-types file and fatal for a stub: in the
        first flock benchmark ``store.py`` was left out of every ticket, the
        charter sealed, the workers built the CLI against it, and ``Store`` was
        never implemented by anyone. Nothing in the partition checks could see
        it, because a file with no owner overlaps with nothing.

        A question rather than a refusal, because only the model knows which
        of the two a file is. Asked once per set of files: sealing again with
        the same set is the answer "they are finished", and it seals.
        """
        if self._before is None or not self.cwd:
            return ""
        owned = {path for worker in charter.workers for path in worker.writes}
        unowned = tuple(path for path in written_since(self._before, self.cwd) if path not in owned)
        if not unowned or unowned == self._asked_unowned:
            return ""
        self._asked_unowned = unowned
        return (
            "Not sealed yet — one question first. These files were written while you planned "
            f"and no ticket writes them: {', '.join(unowned)}.\n\n"
            "A file nobody owns stays exactly as the skeleton left it: no worker can implement "
            "a stub in it, and no worker can make its tests pass. If every one of them is "
            f"finished as it is (shared types, fixtures, an `__init__`), call `{retry}` again "
            "unchanged and it will seal. If one still has work in it, give it to the ticket "
            "that does that work — `drop_worker` and `add_worker` it back with that file in "
            "`writes` — and then seal."
        )

    @property
    def exhausted(self) -> bool:
        """Whether a charter has been tried enough times to stop asking."""
        return self.charter is None and self.attempts >= MAX_CHARTER_ATTEMPTS

    # ------------------------------------------------------------------ #
    # Accepting one
    # ------------------------------------------------------------------ #
    def accept(self, charter: Charter, raw: str = "") -> "tuple[str, bool]":
        """Keep a charter and say what happens to it next, and whether it is disjoint.

        The single tail both routes run through. ``on_proposed`` is fired here
        rather than by the callers, guarded, because a front-end that cannot
        display a charter must not turn an accepted one into a failed call.
        """
        self.last_error = ""
        self.charter, self.raw = charter, raw
        if self.on_proposed is not None:
            try:
                self.on_proposed(charter)
            except Exception:  # noqa: BLE001 - a front-end that cannot display it
                # must not turn an accepted charter into a failed tool call.
                logger.debug("a charter handler raised", exc_info=True)
        disjoint, partition = check_partition(charter)
        if not disjoint:
            return self._overlap_notice(charter, partition), False
        return (
            f"Charter accepted: {len(charter.workers)} worker(s), "
            f"{len(charter.seams)} seam(s), partition is disjoint. "
            "It now goes to the user for approval."
        ), True

    @property
    def last_refusal(self) -> str:
        """The last move-level refusal, for a nudge that has to quote it."""
        return self._last_refusal

    def refuse(self, message: str, retry: str = "") -> str:
        """A move-level refusal, with the call to make next attached.

        The refusals from ``PlanDraft`` are already specific — one path, one
        owner, what to do about it — but **a diagnosis is not an instruction**,
        and that gap is where a weak model stops. "Add them to writes, or drop
        them from tests" describes the fix perfectly and never says to send the
        call again, so the obedient answer is to narrate the correction; the
        planning turn ends on the sentence, and the round ends with it. So
        ``retry`` names the tool to call and the refusal closes with the
        imperative.

        Once the same refusal starts repeating, the imperative is exactly what
        has to stop: telling a model to send it again is the one thing already
        proven not to work, so at the cap it is replaced by a stop.
        """
        if message == self._last_refusal:
            self._repeats += 1
        else:
            self._last_refusal, self._repeats = message, 1
        if self._repeats < MAX_REPEATED_REFUSALS:
            if not retry:
                return message
            return (
                f"{message}\n\nNothing has been added to the plan. Fix that one thing and "
                f"call `{retry}` again now — correcting it in your reply changes nothing, "
                "because the plan only holds what the tools were told."
            )
        return (
            f"{message}\n\nThat is the {self._repeats}th time you have sent this same move "
            "and been refused it, so sending it again will not work either. Do something "
            "different: change the path, drop the ticket that holds it, or stop calling "
            "planning tools and say in your reply what you cannot resolve."
        )

    def _overlap_notice(self, charter: Charter, partition: str) -> str:
        """What a valid charter with an overlapping partition is told.

        Only ``propose_charter`` can reach this — a sealed plan is disjoint by
        construction — and that asymmetry is deliberate rather than an
        oversight. A whole document is checked after the fact and can therefore
        be wrong in a way there is no move to blame.

        This message used to open with "Charter accepted" and close with "If it
        was a mistake, propose a corrected charter", which is two states at
        once and an instruction to act on the second. Nothing counted the
        attempts, the text was byte-identical every time — same partition, same
        string — and an unchanged tool result after an unchanged action is the
        strongest signal a model has that the thing to do next is the same
        again. The run then spent its whole planning budget re-proposing, and
        reported at the end that no charter had been proposed at all.

        So: one state, said once. The charter is **held** and the run can go on
        without another call; a better partition is invited exactly
        ``MAX_OVERLAP_ATTEMPTS`` times; and a resubmission that overlaps in the
        same places is told so, because "you changed something and it did not
        help" is the one fact that distinguishes this attempt from the last.
        """
        self.overlaps += 1
        signature = tuple((conflict.kind, conflict.path) for conflict in find_conflicts(charter))
        unchanged = signature == self._last_overlaps
        self._last_overlaps = signature

        held = (
            f"Charter held: {len(charter.workers)} worker(s). It is NOT disjoint, so it "
            f"cannot run at full concurrency as it stands:\n{partition}\n\n"
        )
        if self.overlaps >= MAX_OVERLAP_ATTEMPTS or unchanged:
            return held + (
                "STOP calling propose_charter. "
                + (
                    "That is the same overlap you proposed last time, so another attempt "
                    "will not clear it. "
                    if unchanged
                    else f"You have proposed {self.overlaps} charters with overlapping "
                    "partitions. "
                )
                + "This charter is kept and the user will be asked whether to run it one "
                "worker at a time. Say plainly in your reply, for the person deciding: "
                "whether the overlap is deliberate, and if not, which file you could not "
                "find an owner for and why."
            )
        return held + (
            "Give each of those files to the one worker that implements it, drop it from the "
            "others' `writes` (they may still read it), and call propose_charter ONCE more. Or "
            "build the plan with add_worker instead, which refuses an overlap at the move "
            "that causes it. If the overlap is deliberate, do not call this again: say so "
            "in your reply and stop, and the user will be asked whether to run one worker "
            "at a time."
        )

    def rejection(self, exc: CharterError) -> str:
        """What a rejected whole-document charter is told.

        Changes as attempts pile up. **The template is sent once.** It is
        twenty-five lines, it is already in ``propose_charter``'s own parameter
        schema, and repeating it after every failure fills the context with
        identical text — which, for a model deciding what to send next, is the
        strongest possible signal that the thing to send next is the same again.

        At the cap the answer stops being a correction and becomes an
        instruction to stop. A tool that only ever says "no, try again" to a
        model that cannot get it right is a loop with a turn limit for a brake,
        and the user watching it has no idea whether anything is happening.
        """
        if self.attempts >= MAX_CHARTER_ATTEMPTS:
            return (
                f"That charter could not be read: {exc}\n\n"
                f"This is attempt {self.attempts}, and every one has been rejected. "
                "STOP calling propose_charter. Say plainly, in your reply, what you were "
                "trying to express and what you cannot get past — a person will read it. "
                "Repeating the same charter will not produce a different answer."
            )
        if self.attempts == 1:
            return (
                f"That charter could not be read: {exc}\n\nThe shape is:\n{CHARTER_TEMPLATE}"
                "\n\nOr build it a piece at a time with declare_seam and add_worker, which "
                "check each move as you make it and never refuse a whole plan at once."
            )
        return (
            f"That charter could not be read: {exc}\n\n"
            f"Attempt {self.attempts} of {MAX_CHARTER_ATTEMPTS}. The shape is in this tool's "
            "`toml` parameter description — do not resend what you just sent; change the "
            "thing the error names. If the TOML keeps failing, use declare_seam and "
            "add_worker instead: no TOML, and one check per move."
        )


class _DeskTool(Tool):
    """A planning tool over a shared ``CharterDesk``.

    The desk is passed in rather than owned, because all five tools are moves
    on one plan — one holding its own draft would build a charter the others
    could not see.
    """

    NAME = ""

    def __init__(self, desk: CharterDesk) -> None:
        self.desk = desk

    def name(self) -> str:
        return self.NAME

    def _refuse(self, exc: CharterError) -> ToolResult:
        # ``retry`` is this tool's own name: a refused move is corrected by
        # making the same move again, and naming it is what turns the
        # diagnosis into something to do.
        return ToolResult(
            ok=False, content=self.desk.refuse(str(exc), retry=self.NAME), error="bad_move"
        )


class DeclareSeamTool(_DeskTool):
    """Record one agreement the workers meet at."""

    NAME = DECLARE_SEAM

    def description(self) -> str:
        return (
            "Record one seam — a file, or file::symbol, where two Worker Birbs' code meets — "
            "for the person approving the plan. It locks nothing: the stub file a seam lives "
            "in still belongs to the ticket that implements it."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "at": {
                    "type": "string",
                    "description": "The file the seam lives at, e.g. 'export/types.py' — "
                    "or 'export/reader.py::load' for an agreement about one symbol.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(SEAM_KINDS),
                    "description": "'formal' if the type system holds it up, 'loose' if "
                    "only a test does.",
                },
                "what": {
                    "type": "string",
                    "description": "One sentence: what the agreement is and who meets at it.",
                },
            },
            "required": ["at", "kind", "what"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            verdict = self.desk.draft.declare_seam(
                str(arguments.get("at") or ""),
                str(arguments.get("kind") or ""),
                str(arguments.get("what") or ""),
            )
        except CharterError as exc:
            return self._refuse(exc)
        return ToolResult(ok=True, content=verdict)


class AddWorkerTool(_DeskTool):
    """Add one ticket to the plan, checked against every ticket already in it."""

    NAME = ADD_WORKER

    def description(self) -> str:
        return (
            "Add one Worker Birb's ticket to the plan. Checked against the plan so far as "
            "soon as you call it: a file another ticket already writes is refused here and "
            "named — so the partition cannot come out overlapping. "
            "Call it once per worker, then seal_charter."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Short id for this ticket, e.g. 'writer'. Unique.",
                },
                "brief": {
                    "type": "string",
                    "description": "This worker's whole ticket: what to implement and what "
                    "done looks like. Only its own part — never the feature, the other "
                    "workers, or the shape of the whole.",
                },
                "writes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every file this worker may create or modify. Nothing "
                    "else will be permitted, so a file you leave out costs it the ticket.",
                },
                "reads": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The interfaces this work has to fit. It may read the "
                    "whole project regardless; these are the ones to point it at.",
                },
                "accept": {
                    "type": "string",
                    "description": "The command that proves this ticket is done, e.g. "
                    "'pytest tests/test_csv.py -q'. The worker may run this itself.",
                },
                "tests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Which of `writes` hold the acceptance tests. A path "
                    "named here that is missing from `writes` is added to it for you — a "
                    "worker's tests are files it owns.",
                },
                "needs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticket ids that must finish first. Omit unless this "
                    "worker genuinely cannot start until another has finished.",
                },
            },
            "required": ["id", "brief", "writes"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            verdict = self.desk.draft.add_worker(
                str(arguments.get("id") or ""),
                brief=str(arguments.get("brief") or ""),
                writes=arguments.get("writes"),
                reads=arguments.get("reads"),
                accept=str(arguments.get("accept") or ""),
                tests=arguments.get("tests"),
                needs=arguments.get("needs"),
            )
        except CharterError as exc:
            return self._refuse(exc)
        return ToolResult(ok=True, content=verdict)


class DropWorkerTool(_DeskTool):
    """Remove a ticket, so a plan can be backed out of rather than restarted."""

    NAME = DROP_WORKER

    def description(self) -> str:
        return (
            "Remove a ticket from the plan. Use it when a ticket claimed a file that "
            "belongs to another one — drop it and add it back corrected."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The ticket id to drop."}},
            "required": ["id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            verdict = self.desk.draft.drop_worker(str(arguments.get("id") or ""))
        except CharterError as exc:
            return self._refuse(exc)
        return ToolResult(ok=True, content=verdict)


class SealCharterTool(_DeskTool):
    """Turn the accumulated plan into the charter the user approves."""

    NAME = SEAL_CHARTER

    def description(self) -> str:
        return (
            "Seal the plan you have built into a charter and send it to the user for "
            "approval. Call it once, after every seam is declared and every ticket added."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "One paragraph: what this round of work is for. The "
                    "first thing the person approving it reads.",
                },
                "concurrency": {
                    "type": "integer",
                    "description": f"How many workers may run at once. Default "
                    f"{DEFAULT_CONCURRENCY}, max {MAX_CONCURRENCY}.",
                },
            },
            "required": ["objective"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.desk.attempts += 1
        concurrency = arguments.get("concurrency")
        try:
            charter = self.desk.draft.seal(
                str(arguments.get("objective") or ""),
                None if concurrency in (None, "") else int(concurrency),
            )
        except (CharterError, TypeError, ValueError) as exc:
            self.desk.last_error = str(exc)
            return ToolResult(
                ok=False,
                content=self.desk.refuse(str(exc), retry=SEAL_CHARTER),
                error="bad_charter",
            )
        question = self.desk.ownership_question(charter, retry=SEAL_CHARTER)
        if question:
            # Not an attempt: nothing was wrong with the plan, it was asked about.
            self.desk.attempts -= 1
            return ToolResult(ok=False, content=question, error="unowned_skeleton")
        content, disjoint = self.desk.accept(charter)
        return ToolResult(ok=True, content=content, meta={"disjoint": disjoint})


class ProposeCharterTool(_DeskTool):
    """A whole charter in one call, as TOML.

    The original route and still the right one for a plan small enough to hold
    in a single document — two tickets and a seam is less work said once than
    said in four calls. For anything larger, ``add_worker`` is what the rules
    steer to, because this validates after the fact: it can come back
    overlapping, and there is no single move to blame for it.

    A tool rather than a fenced block in the final answer, for two reasons. The
    charter is validated the moment it arrives, so a malformed one is a
    correctable tool failure on the next turn rather than a parse error after
    the turn is over. And it never becomes a file in the working tree: a
    charter lives in the session, and a charter file sitting in a repository is
    one a clone could plant.

    The state lives on the desk, and the properties below delegate to it so
    that the front-end — which reaches a charter through
    ``tools[PROPOSE_CHARTER]`` — keeps working whichever route produced one.
    """

    NAME = PROPOSE_CHARTER

    def __init__(
        self,
        cwd: str | None = None,
        on_proposed: "Any | None" = None,
        desk: "CharterDesk | None" = None,
    ) -> None:
        super().__init__(desk or CharterDesk(cwd, on_proposed=on_proposed))

    # -- the desk's state, reachable through the tool the front-end holds -- #
    @property
    def charter(self) -> Charter | None:
        return self.desk.charter

    @charter.setter
    def charter(self, value: "Charter | None") -> None:
        self.desk.charter = value

    @property
    def raw(self) -> str:
        return self.desk.raw

    @property
    def attempts(self) -> int:
        return self.desk.attempts

    @property
    def last_error(self) -> str:
        return self.desk.last_error

    @property
    def overlaps(self) -> int:
        return self.desk.overlaps

    @property
    def exhausted(self) -> bool:
        return self.desk.exhausted

    @property
    def on_proposed(self) -> "Any | None":
        return self.desk.on_proposed

    @on_proposed.setter
    def on_proposed(self, value: "Any | None") -> None:
        self.desk.on_proposed = value

    def reset(self) -> None:
        self.desk.reset()

    def description(self) -> str:
        return (
            "Propose the whole charter for this Flock in one call, as TOML: the objective, "
            "the seams, and one ticket per Worker Birb. Good for a small plan. For a larger "
            "one prefer declare_seam and add_worker, which check each piece as you add it."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "toml": {
                    "type": "string",
                    "description": f"The whole charter as TOML. Shape:\n{CHARTER_TEMPLATE}",
                }
            },
            "required": ["toml"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate and keep the charter, or say exactly what is wrong with it.

        A rejected charter comes back with the reason *and* the partition
        check, because the commonest correctable mistake is two workers sharing
        a file, and telling the model that now saves a round trip and a user's
        attention later.
        """
        text = str(arguments.get("toml") or "")
        self.desk.attempts += 1
        try:
            charter = parse_charter(text)
        except CharterError as exc:
            self.desk.last_error = str(exc)
            return ToolResult(
                ok=False, content=self.desk.rejection(exc), error="bad_charter"
            )

        question = self.desk.ownership_question(charter, retry=PROPOSE_CHARTER)
        if question:
            self.desk.attempts -= 1
            return ToolResult(ok=False, content=question, error="unowned_skeleton")
        content, disjoint = self.desk.accept(charter, text)
        return ToolResult(ok=True, content=content, meta={"disjoint": disjoint})


# How finely to cut, said after the objective rather than inside
# ``BRAINY_RULES`` so that "previously specified" covers everything above it:
# the rules and the user's own objective both override it. The isolation this
# asks for is a security property, not a performance one — a worker whose brief
# lets it infer the epic knows more than its job requires, which is the same
# need-to-know boundary section 4 draws, applied to the size of the pieces
# rather than the wording of the briefs.
NEED_TO_KNOW_DIRECTIVE = """\
Unless previously specified, use as many Worker Birbs as appropriate for this \
assignment so that each fragmentation of the plan is isolated so well from the \
context that it is impossible for each individual Worker Birb to correctly \
guess the full picture."""


def plan_prompt(objective: str) -> str:
    """The whole prompt for the planning turn."""
    return (
        f"{BRAINY_RULES}\n\n--- The work ---\n\n{objective.strip()}"
        f"\n\n--- How to divide it ---\n\n{NEED_TO_KNOW_DIRECTIVE}"
    )


def charter_retry_prompt(last_error: str) -> str:
    """One more attempt, with the rejection quoted back.

    A planning turn ends when the model stops calling tools, and a model whose
    charter has just been rejected can end it by announcing success instead of
    correcting the charter — at which point the flock stops with nothing to
    show. Quoting the reason and asking plainly for another call is cheap and
    usually enough; the alternative is a run that reports itself finished
    without a single worker having started.
    """
    return (
        "Your charter was NOT accepted, and no Flock has been created. Nothing you have "
        "said since changes that — the only things that propose a charter are "
        "`seal_charter` and `propose_charter`, and the last attempt was rejected:\n\n"
        f"    {last_error}\n\n"
        "Fix exactly that and propose it again now. If the whole-document TOML route keeps "
        "failing, build the charter up instead: `declare_seam` and `add_worker` per piece, "
        "then `seal_charter` — each call is checked on its own, so nothing is refused all "
        "at once. Do not describe the charter in prose and do not report success: neither "
        "creates one. If you have concluded that this work should not be divided between "
        "workers at all, say so plainly instead — that is a legitimate answer, and a "
        "different one from this."
    )


def seal_reminder_prompt(draft: PlanDraft) -> str:
    """One nudge for a plan that was built and never sealed.

    The incremental route's counterpart to ``charter_retry_prompt``, and needed
    for the same reason: a planning turn ends when the model stops calling
    tools, and a model that has added every ticket can stop by describing the
    plan it just built rather than sealing it. The work is all done at that
    point — the tickets are in the draft — and the only thing between it and a
    running flock is one more call, so asking for that call is cheap and
    recovers the whole round.

    The plan is quoted back because it is the evidence that nothing needs
    rebuilding. Told only "call seal_charter", a model that has lost track of
    what landed starts adding the tickets again and collides with itself.
    """
    return (
        "You have NOT proposed a charter, and no Flock has been created. The plan you built "
        f"is still here — {draft.describe()} — but a plan is not a charter until it is "
        "sealed, and `seal_charter` is the only thing that seals it.\n\n"
        "Call `seal_charter` now with the objective. Do not add the tickets again: they are "
        "already in the plan, and adding them a second time will be refused as duplicates. "
        "Do not describe the charter in prose and do not report success — neither creates "
        "one.\n\n"
        "If you have concluded that this work should not be divided between workers after "
        "all, say so plainly instead and do not seal anything. That is a legitimate answer, "
        "and a different one from this."
    )


def no_tickets_prompt(desk: "CharterDesk") -> str:
    """The nudge for a plan with no tickets in it yet.

    The state the old recovery branches had no answer for. ``_plan`` retried a
    *rejected* charter and nudged an *unsealed* draft, and a draft holding
    seams and nothing else is neither: nothing was ever sealed, so there was no
    rejection to quote, and there were no tickets to be reminded about. It fell
    through to "did not propose a charter", which the user reads as "decided
    the work does not divide" — the opposite of what a model that has just
    declared two seams has concluded.

    The last refusal is quoted when there is one, because the commonest way to
    arrive here is an ``add_worker`` that was refused and never re-sent.
    """
    draft = desk.draft
    if draft.seams:
        state = (
            f"The plan holds {draft.describe()}. The seams are recorded; not one ticket is."
        )
        move = (
            "Call `add_worker` now — once for each ticket, with its id, its brief and every "
            "file it may write. The seams you declared stay as they are; do not declare them "
            "again. When every ticket is in, call `seal_charter`."
        )
    else:
        state = "The plan is empty: no seams and no tickets."
        move = (
            "Call `add_worker` once for each ticket — every file that still has a stub in it "
            "goes to exactly one — then `seal_charter`. `declare_seam` is optional, to "
            "describe where the tickets meet."
        )
    lines = [
        "You have NOT proposed a charter, and no Flock has been created. " + state,
        "",
    ]
    if desk.last_refusal:
        lines += [
            "Your last planning move was refused:",
            "",
            f"    {desk.last_refusal}",
            "",
            "Correct exactly that and send the call again — a correction you only describe "
            "changes nothing, because the plan holds what the tools were told and nothing "
            "else.",
            "",
        ]
    lines += [
        move,
        "",
        "If you have concluded that this work should not be divided between workers at all, "
        "say so plainly instead and call nothing. That is a legitimate answer, and a "
        "different one from this.",
    ]
    return "\n".join(lines)


def next_move_prompt(desk: "CharterDesk") -> str:
    """The one move to make next, read off the draft.

    **The planner's completion test is objective** — there is a sealed charter
    or there is not — so the phase does not have to end where the model's
    sentence does. This is the other half of that: given an incomplete plan,
    exactly what is missing is a question the draft answers, so CoBirb holds
    the state and names the next call rather than hoping the model sustains a
    long chain unaided.

    Three states, in the order they have to be tested. A rejected seal is
    quoted back first because it is the most specific thing known. Tickets with
    no charter means the work is done and the last call was missed. No tickets
    means the plan has not been built yet — the state that used to end a run.
    """
    if desk.attempts and desk.last_error:
        return charter_retry_prompt(desk.last_error)
    if desk.draft.workers:
        return seal_reminder_prompt(desk.draft)
    return no_tickets_prompt(desk)


def round_summary(outcome: FlockOutcome) -> str:
    """What Brainy Birb is given to write the round's verdict from.

    Reports and reviews together, because they answer different questions and
    the interesting cases are where they disagree — a worker whose acceptance
    check passed but whose review found a vacuous suite is the exact situation
    the review exists to surface, and it reads as success in the report alone.

    **It has to say not to call ``propose_charter``, and that is not
    defensive noise.** This runs on the same orchestrator and the same session
    as the planning phase, so ``BRAINY_RULES`` — which tells Brainy Birb to
    deliver a charter by calling that tool — is still in its context, along
    with its own successful call from earlier. Meanwhile ``run._plan`` pops the
    tool out of the registry as soon as planning ends. Ask for "an amended
    charter" in those conditions and a round that went badly produces exactly
    the obedient thing: a call to a tool that is no longer there, an "Unknown
    tool" result the model cannot argue with, and the remaining review turns
    spent failing to recover. Re-registering it would be worse rather than
    better — nothing reads a second charter, because a round ends here by
    design (``run.py``'s module docstring), so the call would succeed and be
    silently discarded.
    """
    lines = [
        "The round has finished. Here is what each Worker Birb reported, and what "
        "review found when its work was checked against the skeleton you wrote.",
        "",
        outcome.describe(),
        "",
        "--- Review in full ---",
    ]
    for review in outcome.reviews:
        lines.append(review.describe())
    lines += [
        "",
        "Write a short account for the user: what works now, what is still stubbed, "
        "and anything that looks like corner-cutting. Then say what a second round "
        "should be — the amended charter you would write if one assumption broke the "
        "design, or just the affected workers re-briefed if the remaining work is "
        "isolated. Say plainly if no second round is needed.",
        "",
        "DESCRIBE THAT SECOND ROUND IN PROSE. DO NOT CALL `propose_charter`, "
        "`seal_charter`, `add_worker` or `declare_seam` here. "
        "This round is finished and nothing you propose now would start another one; "
        "that is the user's decision, made from the account you are writing. If they "
        "want the round you describe, they will ask for it and you can propose it "
        "then. What you write here is read by a person, not executed.",
        "",
        "A check that SURVIVED having the stubs put back usually means a behaviour you specified "
        "has no test behind it. That is your omission to fix in the next skeleton, not "
        "the worker's. One reported as 'could not be checked' is a charter problem "
        "instead — most often a ticket that never named its test files in `tests`.",
    ]
    return "\n".join(lines)
