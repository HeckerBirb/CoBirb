"""The Flock: one Brainy Birb, several Worker Birbs, need-to-know isolation.

A flock run is how CoBirb divides a piece of work the way a lead engineer
divides an epic. **Brainy Birb** does the part that genuinely needs the whole
picture — it plans, designs the seams between the components, and builds the
skeleton: the interfaces, the typed stubs, the docstrings that state semantics,
and the unit tests that fail. Only then does it fan out, one brief per **Worker
Birb**, the way a lead writes tickets nobody has to read the epic to close.

**The skeleton is the communication channel.** Workers never coordinate,
because everything they would have had to agree on is already written down in
the one place all of them can see. Two engineers whose work meets in the middle
do not need to talk if their lead already designed the interface they meet at.
That is why the isolation can be total rather than merely encouraged: a Worker
Birb does not know what the feature is, how many others there are, or what they
are building. It knows a signature, a docstring and a test.

**A Worker Birb is not special.** It is an ordinary CoBirb run that received
its instructions from another agent instead of from a person. It edits files
the way any agent edits files and it fails the way any agent fails, so this
package builds almost nothing of its own: a worker is ``HeadlessIO`` (already
refuses anything not pre-approved and never prompts) wired by
``build_orchestrator`` with a ``Policy`` derived from the charter, the
``worker`` model role, and its brief as the prompt. What is genuinely new is
the charter, the partition check, the fan-out, and the verification passes.
"""
from __future__ import annotations

from . import branch
from .brainy import (
    BRAINY_RULES,
    PLANNING_TOOLS,
    AddWorkerTool,
    CharterDesk,
    DeclareSeamTool,
    DropWorkerTool,
    ProposeCharterTool,
    SealCharterTool,
    plan_prompt,
    round_summary,
    seal_reminder_prompt,
)
from .charter import (
    Charter,
    CharterError,
    Conflict,
    Seam,
    WorkerBrief,
    describe_conflicts,
    find_conflicts,
    parse_charter,
    policy_for,
)
from .plan import PlanDraft
from .probe import ProbeResult, probe_concurrency
from .review import Baseline, Mutant, Review, review_worker
from .supervisor import FlockOutcome, check_partition, run_flock
from .worker import WorkerReport, run_worker

__all__ = [
    "BRAINY_RULES",
    "PLANNING_TOOLS",
    "branch",
    "AddWorkerTool",
    "Baseline",
    "CharterDesk",
    "Charter",
    "CharterError",
    "Conflict",
    "DeclareSeamTool",
    "DropWorkerTool",
    "FlockOutcome",
    "Mutant",
    "PlanDraft",
    "ProbeResult",
    "ProposeCharterTool",
    "Review",
    "SealCharterTool",
    "Seam",
    "WorkerBrief",
    "WorkerReport",
    "check_partition",
    "describe_conflicts",
    "find_conflicts",
    "parse_charter",
    "plan_prompt",
    "policy_for",
    "probe_concurrency",
    "review_worker",
    "round_summary",
    "run_flock",
    "run_worker",
    "seal_reminder_prompt",
]
