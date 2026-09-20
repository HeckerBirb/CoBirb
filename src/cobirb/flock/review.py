"""Trust, then verify: checking what a Worker Birb actually did.

A Worker Birb may change anything inside its own scope, tests included. That is
deliberate — an engineer who implements something finds edge cases the ticket
did not anticipate, and those deserve tests; locking a worker out of its own
test file would ship under-tested code to prevent a cheat that review catches
anyway. What holds a worker honest is not a lock but a review, and this module
is the review.

It works because **Brainy Birb authored the skeleton and still has it**. There
is a baseline to compare against and a stub to put back, so "did this worker
cut corners?" is a question with evidence behind it rather than a vibe.

Three passes, cheapest first, each independently useful:

1. **Read the diff** (free). What changed in the acceptance criteria, with the
   suspicious parts called out: an assertion that was there and is not, a skip
   marker that was not there and is, a definition that has gone. These are
   *candidates for attention*, not verdicts — they are pattern matches across
   languages CoBirb cannot parse, and Brainy Birb reads them with judgement.
2. **Put the stub back** (one scoped run, no model). Restore the original
   implementation, keep the worker's tests, and run the acceptance check. It
   **must fail**. A test suite that passes against an unimplemented function is
   testing nothing, and this catches it with no parsing, no language knowledge
   and no model involvement. This is the reliable one.
3. **Mutate the stated behaviours** (one run per claim). A docstring is a list
   of promises; that list is the mutation list. Brainy Birb writes one
   deliberately-wrong implementation per promise and each must be caught. A
   surviving mutant is a promise nothing is holding — often the skeleton's
   fault rather than the worker's, which makes this a review of the contract
   as much as of the work.

Passes 2 and 3 are the same primitive with different content (see
``expect_red``), which is why this module does not care who wrote the mutant.

**Files are edited in place and put back afterwards.** No sentinel, no
crash-recovery machinery: a kill between mutating and restoring leaves a broken
file, which is the same situation as any agent being killed mid-edit and is
version control's problem. A Worker Birb is not special, and neither is this.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from ..runtime.verify import DEFAULT_TIMEOUT_SECONDS, run_verification
from .charter import Charter, WorkerBrief

logger = logging.getLogger("cobirb")

# Lines that look like an assertion, across the languages this is likely to
# meet. Deliberately broad and deliberately not a parser: a false positive
# costs a line in a report someone reads, a false negative costs a missed
# weakening, and the asymmetry says to over-match.
_ASSERTION = re.compile(
    r"\b(assert\w*|expect|should\w*|require\w*|XCTAssert\w*|t\.(Error|Fatal)f?)\b"
)

# Markers that turn a test off without deleting it — the quietest way to make
# red go green.
_SKIP = re.compile(
    r"(@\w*\.?(skip|xfail)\w*|\bt\.Skip\b|#\[ignore\]|\.skip\(|\bpytest\.mark\.skip)",
    re.IGNORECASE,
)

# Lines that declare something other code depends on. Used to notice a
# signature that has quietly changed, which is the one action that breaks
# colleagues a worker cannot see.
_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:public|private|protected|internal|static|async|pub)?\s*"
    r"\b(def|class|func|function|fn|interface|struct|impl|trait|type)\b"
)

FINDING_ASSERTION_REMOVED = "assertion_removed"
FINDING_SKIP_ADDED = "skip_added"
FINDING_DEFINITION_REMOVED = "definition_removed"
FINDING_FILE_EMPTIED = "file_emptied"


@dataclass(frozen=True)
class Finding:
    """One thing in a diff worth a second look.

    Not an accusation. Every one of these has an innocent explanation — a
    renamed test, a refactored helper, a docstring rewritten for clarity — and
    the point is to put the handful of lines worth reading in front of Brainy
    Birb rather than the whole diff.
    """

    kind: str
    path: str
    detail: str

    def describe(self) -> str:
        return f"{self.path}: {self.detail}"


@dataclass
class Baseline:
    """The skeleton exactly as Brainy Birb wrote it, before any worker ran.

    Captured after the skeleton is built and before fan-out. This is the only
    reason review has teeth: without it there is nothing to diff against and
    nothing to put back.
    """

    files: dict[str, str] = field(default_factory=dict)

    @classmethod
    def capture(cls, charter: Charter, cwd: str) -> "Baseline":
        """Read every file any worker may change.

        A path that does not exist yet is recorded as empty rather than
        skipped: "the skeleton did not create this" is a real state, and it is
        the correct thing to restore a file to when putting a stub back.
        """
        files: dict[str, str] = {}
        for worker in charter.workers:
            for path in worker.writes:
                files[path] = _read(os.path.join(cwd, path))
        return cls(files=files)

    def content(self, path: str) -> str:
        return self.files.get(path, "")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# --------------------------------------------------------------------------- #
# Pass one — read the diff
# --------------------------------------------------------------------------- #
def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines()]


def read_the_diff(worker: WorkerBrief, baseline: Baseline, cwd: str) -> list[Finding]:
    """What changed in this worker's files, with the parts worth reading flagged.

    Compares whole lines rather than tokens, so a reformatted line reads as a
    removal. That is the right trade here: this produces candidates for a
    reviewer, and a reformatted assertion is worth a glance anyway.
    """
    findings: list[Finding] = []
    for path in worker.writes:
        before = baseline.content(path)
        after = _read(os.path.join(cwd, path))
        if before.strip() and not after.strip():
            findings.append(
                Finding(FINDING_FILE_EMPTIED, path, "was written by the skeleton and is now empty")
            )
            continue

        before_lines, after_lines = _lines(before), _lines(after)
        present = set(after_lines)

        gone_assertions = [
            line.strip()
            for line in before_lines
            if _ASSERTION.search(line) and line not in present
        ]
        for line in gone_assertions[:5]:
            findings.append(
                Finding(FINDING_ASSERTION_REMOVED, path, f"an assertion is gone — {line}")
            )
        if len(gone_assertions) > 5:
            findings.append(
                Finding(
                    FINDING_ASSERTION_REMOVED,
                    path,
                    f"and {len(gone_assertions) - 5} further assertion(s) from the skeleton",
                )
            )

        was_there = set(before_lines)
        for line in after_lines:
            if _SKIP.search(line) and line not in was_there:
                findings.append(
                    Finding(FINDING_SKIP_ADDED, path, f"a test was turned off — {line.strip()}")
                )

        for line in before_lines:
            if _DEFINITION.match(line) and line not in present:
                findings.append(
                    Finding(
                        FINDING_DEFINITION_REMOVED,
                        path,
                        f"a declaration from the skeleton changed or vanished — {line.strip()}",
                    )
                )
    return findings


# --------------------------------------------------------------------------- #
# Passes two and three — break it, confirm red
# --------------------------------------------------------------------------- #
@dataclass
class RedCheck:
    """Whether a deliberately broken version of the code was actually caught."""

    label: str
    caught: bool
    output: str = ""
    error: str = ""

    def describe(self) -> str:
        if self.error:
            return f"{self.label}: could not be checked — {self.error}"
        verdict = "caught" if self.caught else "SURVIVED — nothing tests this"
        return f"{self.label}: {verdict}"


def expect_red(
    replacements: dict[str, str],
    accept: str,
    cwd: str,
    *,
    label: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> RedCheck:
    """Swap in broken content, run the check, and put everything back.

    The shared primitive behind passes two and three: pass two's "broken
    content" is the original stub, pass three's is a mutant Brainy Birb wrote.
    Neither cares where it came from.

    Restoring happens in a ``finally`` because putting the files back is part
    of the operation, not a safety net bolted onto it. A process killed outright
    still leaves the break behind, and that is accepted — it is the same
    situation as any agent killed mid-edit.
    """
    if not accept.strip():
        return RedCheck(label=label, caught=False, error="no acceptance check to run")

    saved = {path: _read(os.path.join(cwd, path)) for path in replacements}
    try:
        for path, content in replacements.items():
            _write(os.path.join(cwd, path), content)
        result = run_verification(accept, cwd, timeout)
    except OSError as exc:
        return RedCheck(label=label, caught=False, error=str(exc))
    finally:
        for path, content in saved.items():
            try:
                _write(os.path.join(cwd, path), content)
            except OSError:  # pragma: no cover - nothing useful left to do
                logger.error("could not restore %s after a review pass", path)

    if result.error:
        return RedCheck(label=label, caught=False, error=result.error)
    # "Caught" means the check failed, which is the whole point: broken code
    # that still passes is code nothing is testing.
    return RedCheck(label=label, caught=not result.ok, output=result.output)


def put_the_stub_back(worker: WorkerBrief, baseline: Baseline, cwd: str, **kwargs) -> RedCheck:
    """Pass two. Restore the implementation, keep the worker's tests, expect red.

    The worker's tests are deliberately left in place — restoring those too
    would run the skeleton's own failing tests against the skeleton's own
    stubs, which proves nothing about anything the worker added.

    A worker whose brief named no implementation files (all of its writes are
    tests) cannot be checked this way, and says so rather than passing quietly.

    **The vacuous case is refused rather than reported as a pass**, and it was
    reachable by omitting one optional field. ``implementation`` is ``writes``
    minus ``tests``; with ``tests`` undeclared it is *every* file the worker
    owns, so restoring it puts the tree back to the skeleton exactly — and the
    skeleton's check fails by construction, which this pass then read as
    "caught". It reported a pass for every worker in that shape, whatever the
    work was, and the charter template's own second ticket omits ``tests``.

    Detected without a filename heuristic — the thing AGENTS.md rules out,
    because a rule matching ``test_*.py`` and missing ``*_test.go`` would turn
    the strongest check in the design into a silent no-op. Instead, two states
    where this pass provably cannot discriminate:

    - **The worker changed nothing.** Removing its implementation removes
      nothing, so the check fails for the skeleton's own reasons.
    - **``tests`` is undeclared, the worker owns more than one file, and every
      file it changed is one being restored.** Then the restore returns its
      whole scope to the skeleton, acceptance tests included. The extra
      ``writes`` condition matters: a worker owning a single file whose
      acceptance tests live in a skeleton-owned file *is* checkable this way,
      and refusing that would fail an honest ticket.

    Reported as "could not be checked", so ``Review.clean`` is False: we did not
    check, and that must never read the same as checking and finding it fine.
    """
    label = f"[{worker.id}] stub reversion"
    implementation = worker.implementation
    if not implementation:
        return RedCheck(
            label=label,
            caught=False,
            error="this worker writes only tests, so there is no implementation to remove",
        )

    if not worker.accept.strip():
        # ``expect_red``'s own answer, reached before the checks below so that
        # "nobody said what done looks like" is not reported as something
        # subtler than it is. Empty replacements: it returns before writing.
        return expect_red({}, worker.accept, cwd, label=label, **kwargs)

    changed = {
        path for path in worker.writes
        if _read(os.path.join(cwd, path)) != baseline.content(path)
    }
    if not changed:
        return RedCheck(
            label=label,
            caught=False,
            error=(
                "could not be checked: this worker changed none of its files, so removing "
                "its implementation removes nothing and the check fails for the skeleton's "
                "own reasons rather than for anything about this ticket"
            ),
        )
    if not worker.tests and len(worker.writes) > 1 and not changed - set(implementation):
        return RedCheck(
            label=label,
            caught=False,
            error=(
                "could not be checked: this ticket declares no `tests`, so every file it "
                "owns counts as implementation and is restored — including whichever of "
                "them holds the acceptance tests. The check would then run against the "
                "skeleton alone and fail whatever the worker did. Name the test files in "
                "the charter's `tests` for this worker and this pass works"
            ),
        )

    return expect_red(
        {path: baseline.content(path) for path in implementation},
        worker.accept,
        cwd,
        label=label,
        **kwargs,
    )


@dataclass(frozen=True)
class Mutant:
    """One promise from a docstring, and the code that breaks it.

    Written by Brainy Birb, which read the contract and knows what it claims.
    ``promise`` is the claim in the docstring's own terms ("a missing key
    writes an empty cell") so that a surviving mutant names the behaviour
    nothing is testing, rather than a line number.
    """

    promise: str
    path: str
    content: str


def check_mutants(worker: WorkerBrief, mutants: list[Mutant], cwd: str, **kwargs) -> list[RedCheck]:
    """Pass three. One deliberately-wrong implementation per stated behaviour.

    A surviving mutant is a promise nothing is holding. Read it as a finding
    about the *contract* first: more often than not the skeleton stated a
    behaviour and never wrote an acceptance test for it, which is Brainy Birb's
    omission rather than the worker's.

    A mutant naming a file this worker does not own is refused rather than
    applied. Brainy Birb writes these, and a mutation that reached outside the
    partition would be editing somebody else's work to test this one.
    """
    owned = set(worker.implementation)
    checks: list[RedCheck] = []
    for mutant in mutants:
        label = f"[{worker.id}] {mutant.promise}"
        if mutant.path not in owned:
            checks.append(
                RedCheck(
                    label=label,
                    caught=False,
                    error=f"{mutant.path} is not this worker's to mutate",
                )
            )
            continue
        checks.append(
            expect_red({mutant.path: mutant.content}, worker.accept, cwd, label=label, **kwargs)
        )
    return checks


# --------------------------------------------------------------------------- #
# The whole review
# --------------------------------------------------------------------------- #
@dataclass
class Review:
    """Everything the three passes found, for one Worker Birb."""

    worker_id: str
    findings: list[Finding] = field(default_factory=list)
    stub: RedCheck | None = None
    mutants: list[RedCheck] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Whether nothing wants a human's attention.

        A stub reversion that could not run counts as unclean. "We could not
        check" must never read the same as "we checked and it was fine".
        """
        if self.findings:
            return False
        if self.stub is not None and not self.stub.caught:
            return False
        return all(check.caught for check in self.mutants)

    def describe(self) -> str:
        lines = [f"[{self.worker_id}] review"]
        if self.stub is not None:
            lines.append(f"  {self.stub.describe()}")
        for check in self.mutants:
            lines.append(f"  {check.describe()}")
        if self.findings:
            lines.append("  worth a look in the diff:")
            lines += [f"    • {finding.describe()}" for finding in self.findings]
        if self.clean:
            lines.append("  nothing to flag.")
        return "\n".join(lines)


def review_worker(
    worker: WorkerBrief,
    baseline: Baseline,
    cwd: str,
    *,
    mutants: list[Mutant] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Review:
    """Run every pass that can be run for this worker.

    Pass three is skipped when no mutants are supplied, because writing them
    needs Brainy Birb and this module deliberately holds no model.
    """
    return Review(
        worker_id=worker.id,
        findings=read_the_diff(worker, baseline, cwd),
        stub=put_the_stub_back(worker, baseline, cwd, timeout=timeout),
        mutants=check_mutants(worker, mutants or [], cwd, timeout=timeout),
    )
