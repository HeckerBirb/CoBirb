"""Brainy Birb: the lead engineer of a Flock.

Brainy Birb does the part that genuinely needs the whole picture. It plans,
decides whether the work divides at all, designs the seams the workers will
meet at, builds the skeleton, writes one brief per Worker Birb, and afterwards
reads the reports and reviews and says where the round got to.

Almost all of that is judgement, so almost all of this module is a prompt. The
code here is the two things a prompt cannot do: hand the model a way to return
a charter that does not involve writing a file into the user's repository
(``ProposeCharterTool``), and compose the material a round's verdict is written
from (``round_summary``).

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
from .charter import Charter, CharterError, parse_charter
from .supervisor import FlockOutcome, check_partition

logger = logging.getLogger("cobirb")

PROPOSE_CHARTER = "propose_charter"

# How many rejected charters before the tool stops asking for another one.
# The planning turn is bounded at 30 turns, so a model that cannot produce
# valid TOML would otherwise spend all of them failing at it — and then, since
# `_plan` retries, spend another thirty. Five is enough attempts for a real
# correction and few enough that the user is not watching a loop.
MAX_CHARTER_ATTEMPTS = 5

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
Apply S.O.L.I.D., and DEPENDENCY INVERSION above all else. Here it is not a \
style preference; it is what makes parallel work possible:

  - If Worker A calls something Worker B builds, and the abstraction lives in \
B's file, then A depends on B's concrete implementation. A cannot be written \
until B exists. The work is SERIAL no matter how many workers you create.
  - Hoist that abstraction into a file YOU own and neither of them writes. Now \
both depend on the abstraction and neither depends on the other. One \
dependency edge has become two independent ones, and the work is PARALLEL.
  - Run this check over your own partition before you propose it: for every \
pair of workers, is there an edge where one needs the other's concrete \
implementation? If yes, hoist the abstraction. If you cannot hoist it, those \
two are not parallelisable and your partition is wrong — merge them into one \
ticket or re-cut the work.

A seam does not have to be a declared artifact. An abstract base class, a Java \
interface or a Rust trait is a FORMAL seam and the type system holds it up. \
"Returns None for a missing key", "never raises on a partial record" is a \
LOOSE seam — just as real an interface, with nothing whatsoever enforcing it, \
so it MUST be pinned by a test instead. Say which kind each seam is; that is \
what tells you whether anything is actually holding it.

Seam files belong to you and are writable by no worker. That is what keeps \
them still while everyone builds against them.

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
with no test behind it is a promise nothing is holding, and it will be found \
later by a check that mutates each claim and sees whether anything notices.
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
`writes` list, and it cannot run shell commands. So two things are on you: \
put EVERY file a worker must create or modify in its `writes` — a file it \
needs to change but you did not list will be refused, and it will have to \
stop and report instead of doing its job; and make sure its acceptance check \
(`accept`) is something that can be judged by running it, since the worker \
cannot run anything itself.

WHEN YOU ARE READY
Build the skeleton with your file tools first, then call `propose_charter` \
once with the whole charter as TOML. Do not write the charter to a file in \
the project; it belongs to the session. The user will read it and approve, \
edit or reject it before any worker runs.

A CHARTER IN YOUR REPLY IS NOT A CHARTER. Writing the TOML out in your \
answer — in a code block, or as prose, however complete and however \
well-formed — proposes nothing and starts nothing. `propose_charter` is the \
only thing that creates one. If you write it into your reply and stop, the \
run ends with no Flock, and the person waiting on it sees a charter on screen \
and nothing happening. Call the tool.

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
at   = "path/to/shared_types.py"
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
reads  = ["path/to/shared_types.py"]
accept = "the command that proves this ticket is done"
brief  = """
This worker's ticket. Only its own part.
"""
'''


class ProposeCharterTool(Tool):
    """How Brainy Birb hands a charter back without touching the repository.

    A tool rather than a fenced block in the final answer, for two reasons. The
    charter is validated the moment it arrives, so a malformed one is a
    correctable tool failure on the next turn rather than a parse error after
    the turn is over. And it never becomes a file in the working tree: a
    charter lives in the session, and a charter file sitting in a repository is
    one a clone could plant.
    """

    NAME = PROPOSE_CHARTER

    def __init__(
        self, cwd: str | None = None, on_proposed: "Any | None" = None
    ) -> None:
        self._cwd = cwd
        # Called with a charter the moment one is accepted. This is what lets a
        # charter proposed outside a flock run reach the user: the front-end
        # decides whether to act on it (it does nothing while a flock is
        # already running and about to ask for approval itself), so the tool
        # does not need to know which of the two situations it is in.
        self.on_proposed = on_proposed
        self.charter: Charter | None = None
        self.raw: str = ""
        # What was attempted and what was wrong with it. Without these, a
        # charter that was rejected four times and a decision not to divide
        # the work at all both arrive at the caller as `charter is None` —
        # indistinguishable, so the first gets reported as the second. See
        # `run._plan`.
        self.attempts: int = 0
        self.last_error: str = ""

    def name(self) -> str:
        return self.NAME

    def reset(self) -> None:
        """Forget any charter and any failures, before a fresh planning turn.

        The tool now lives as long as the session does, so a second flock
        would otherwise start holding the first one's charter and be handed it
        as though it had just been proposed.
        """
        self.charter = None
        self.raw = ""
        self.attempts = 0
        self.last_error = ""

    def description(self) -> str:
        return (
            "Propose the charter for this Flock: the objective, the seams, and one "
            "ticket per Worker Birb, as TOML. Call this once, after the skeleton is "
            "written. The user reads and approves it before any worker runs."
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

    @property
    def exhausted(self) -> bool:
        """Whether this has been tried enough times to stop asking."""
        return self.charter is None and self.attempts >= MAX_CHARTER_ATTEMPTS

    def _rejection(self, exc: CharterError) -> str:
        """What a rejected charter is told, which changes as attempts pile up.

        **The template is sent once.** It is twenty-five lines, it is already
        in this tool's own parameter schema, and repeating it after every
        failure fills the context with identical text — which, for a model
        deciding what to send next, is the strongest possible signal that the
        thing to send next is the same again.

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
            )
        return (
            f"That charter could not be read: {exc}\n\n"
            f"Attempt {self.attempts} of {MAX_CHARTER_ATTEMPTS}. The shape is in this tool's "
            "`toml` parameter description — do not resend what you just sent; change the "
            "thing the error names."
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate and keep the charter, or say exactly what is wrong with it.

        A rejected charter comes back with the reason *and* the partition
        check, because the commonest correctable mistake is two workers sharing
        a file, and telling the model that now saves a round trip and a user's
        attention later.
        """
        text = str(arguments.get("toml") or "")
        self.attempts += 1
        try:
            charter = parse_charter(text)
        except CharterError as exc:
            self.last_error = str(exc)
            return ToolResult(ok=False, content=self._rejection(exc), error="bad_charter")

        self.last_error = ""
        self.charter, self.raw = charter, text
        if self.on_proposed is not None:
            try:
                self.on_proposed(charter)
            except Exception:  # noqa: BLE001 - a front-end that cannot display it
                # must not turn an accepted charter into a failed tool call.
                logger.debug("a charter handler raised", exc_info=True)
        disjoint, partition = check_partition(charter)
        if not disjoint:
            return ToolResult(
                ok=True,
                content=(
                    f"Charter accepted, with {len(charter.workers)} worker(s), but the "
                    f"partition overlaps:\n{partition}\n\nThe user will be asked how to "
                    "resolve this. If it was a mistake, propose a corrected charter."
                ),
                meta={"disjoint": False},
            )
        return ToolResult(
            ok=True,
            content=(
                f"Charter accepted: {len(charter.workers)} worker(s), "
                f"{len(charter.seams)} seam(s), partition is disjoint. "
                "It now goes to the user for approval."
            ),
            meta={"disjoint": True},
        )


def plan_prompt(objective: str) -> str:
    """The whole prompt for the planning turn."""
    return f"{BRAINY_RULES}\n\n--- The work ---\n\n{objective.strip()}"


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
        "said since changes that — the only thing that proposes a charter is a call to "
        "`propose_charter`, and the last one was rejected:\n\n"
        f"    {last_error}\n\n"
        "Fix exactly that and call `propose_charter` again now. Do not describe the "
        "charter in prose and do not report success: neither creates one. If you have "
        "concluded that this work should not be divided between workers at all, say so "
        "plainly instead — that is a legitimate answer, and a different one from this."
    )


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
        "DESCRIBE THAT SECOND ROUND IN PROSE. DO NOT CALL `propose_charter` here. "
        "This round is finished and nothing you propose now would start another one; "
        "that is the user's decision, made from the account you are writing. If they "
        "want the round you describe, they will ask for it and you can propose it "
        "then. What you write here is read by a person, not executed.",
        "",
        "A surviving mutant or a stub reversion that was not caught usually means a "
        "behaviour you specified has no test behind it. That is your omission to fix "
        "in the next skeleton, not the worker's.",
    ]
    return "\n".join(lines)
