"""The charter: what Brainy Birb proposes and the user approves.

A charter has to be two things at once. Precise enough that CoBirb can build a
``Policy`` out of it — the scopes *are* the isolation, and they are enforced
rather than requested. And readable enough that a person can judge a partition
they did not design, since approving the charter is the only decision they get
to make in the whole run.

So the scopes are data and the reasoning is prose, in one file:

    objective = \"\"\"
    Add CSV export: a writer, column selection, and a --csv flag.
    \"\"\"
    concurrency = 2

    [[seams]]
    at   = "export/types.py"
    kind = "formal"          # the type system holds this one up
    what = "ExportSpec and Column — the shared vocabulary."

    [[workers]]
    id     = "a"
    writes = ["export/csv_writer.py", "tests/test_csv_writer.py"]
    reads  = ["export/types.py"]
    accept = "pytest tests/test_csv_writer.py -q"
    brief  = \"\"\"
    Implement `write_csv`. The signature, semantics and acceptance tests are
    already in the file: make them pass without changing what they assert.
    \"\"\"

**TOML**, parsed by ``tomllib`` from the standard library, so this costs no
dependency. It is a third format for this project — config is JSON, custom
commands are markdown — and that is the price of multiline strings that hold a
paragraph without the escaped newlines that make a JSON brief unreadable. A
charter is meant to be edited by the person approving it.

**Nothing here reads a charter off disk on its own.** A charter lives in the
session, and a file found in a working directory is not one CoBirb wrote.
``parse_charter`` takes text; a caller may hand it the contents of a file the
user explicitly named, exactly as ``--session`` names a session.

``parse_charter`` reads a whole document and validates it in one go. The other
way to build one is ``plan.PlanDraft``, which accumulates the same structures a
validated move at a time; the checks they share live here, as
``check_dependencies``, ``check_seams_are_frozen`` and ``frozen_seam_paths``.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from typing import Any

from ..orchestrator import build_default_policy
from ..policy import Policy

# A seam either has a compiler behind it or it does not, and which one decides
# what is actually holding the agreement up.
SEAM_FORMAL = "formal"
SEAM_LOOSE = "loose"
SEAM_KINDS = (SEAM_FORMAL, SEAM_LOOSE)

# Two Worker Birbs by default. Enough that the concurrency is real from the
# first run rather than a promise to add later and discover does not fit.
DEFAULT_CONCURRENCY = 2

# Nothing in the design caps this; the bound exists so a typo'd `concurrency =
# 200` reads as a mistake at approval time rather than as two hundred child
# runs. Raise it if a run genuinely wants more.
MAX_CONCURRENCY = 16


class CharterError(ValueError):
    """A charter that cannot be read, or that says something impossible.

    Raised only for problems that make the charter unusable — malformed TOML, a
    worker with no files. A *conflict* between two workers' scopes is not one
    of these: it is a real situation with several reasonable resolutions, so it
    is reported (see ``find_conflicts``) for the user to settle rather than
    refused here.
    """


@dataclass(frozen=True)
class Seam:
    """One agreement the workers meet at.

    Recorded because it is the part of a charter a person most needs to judge:
    anyone can read a file list, but whether the boundaries were *designed* or
    merely drawn is the thing only the approver can assess.

    ``kind`` matters more than it looks. A **formal** seam is a declared
    artifact — an ``ABC``, a Java ``interface``, a Rust trait — and the type
    system holds it up. A **loose** one is an agreement with no code behind it
    at all ("returns None for a missing key", "never raises on a partial
    record"); it is just as real an interface and has nothing enforcing it, so
    it must be pinned by a test instead.
    """

    at: str
    kind: str
    what: str

    @property
    def enforced_by_types(self) -> bool:
        return self.kind == SEAM_FORMAL

    def describe(self) -> str:
        held = "held up by the type system" if self.enforced_by_types else "held up only by a test"
        return f"{self.at} — {self.what} ({self.kind}, {held})"


@dataclass(frozen=True)
class WorkerBrief:
    """One Worker Birb's entire world.

    ``writes`` and ``reads`` become its ``Policy`` directly — that is the whole
    of its isolation, and it is visible to the user before anything runs.
    ``accept`` is its own scoped check, so a worker never runs the full suite
    and therefore never meets somebody else's failing test to helpfully fix.
    """

    id: str
    brief: str
    writes: tuple[str, ...]
    reads: tuple[str, ...] = ()
    accept: str = ""
    # Which of ``writes`` hold the acceptance tests. A subset, never a separate
    # set — a worker must be able to edit its own tests, because implementing
    # something turns up edge cases the skeleton did not anticipate and those
    # deserve tests.
    #
    # Declared rather than guessed from filenames. Review needs to put the
    # *implementation* back to its stub while keeping the worker's tests, and
    # a naming heuristic that works for `test_*.py` and fails for `*_test.go`
    # would quietly turn the strongest check in the design into a no-op.
    tests: tuple[str, ...] = ()
    # Workers that must finish before this one starts. Empty for the common
    # case, which is the point: the design's first answer to "who goes first?"
    # is "nobody, they are independent", and a charter that declares
    # dependencies everywhere has serialised a fan-out into a queue.
    #
    # It exists for the one shape independence cannot express: a seam that has
    # to be *built* before anything can be built against it. Without this that
    # work needs two rounds and a second trip through the user, which is a lot
    # of ceremony for "b reads what a writes".
    needs: tuple[str, ...] = ()

    @property
    def implementation(self) -> tuple[str, ...]:
        """The files a worker writes that are not its tests."""
        return tuple(path for path in self.writes if path not in set(self.tests))

    @property
    def touches(self) -> tuple[str, ...]:
        """Every path this worker may open, for the conflict check."""
        return tuple({*self.writes, *self.reads})


@dataclass(frozen=True)
class Charter:
    """A whole proposed run."""

    objective: str
    workers: tuple[WorkerBrief, ...]
    seams: tuple[Seam, ...] = ()
    concurrency: int = DEFAULT_CONCURRENCY

    def worker(self, worker_id: str) -> WorkerBrief | None:
        for entry in self.workers:
            if entry.id == worker_id:
                return entry
        return None

    @property
    def effective_concurrency(self) -> int:
        """How many workers can really be in flight at once.

        A dependency graph is a ceiling on parallelism that ``concurrency``
        knows nothing about: a chain of four runs one at a time however large
        the number says. Computed as the widest level of the graph — the
        workers that become admissible together — so the approval prompt can
        say what will actually happen rather than what was asked for.

        Someone approving "4 at a time" and getting 1 has been told something
        untrue by their own charter, and the cost of finding out is a round
        that takes four times as long as they planned for.
        """
        remaining = {worker.id: set(worker.needs) for worker in self.workers}
        widest = 0
        while remaining:
            ready = [wid for wid, needs in remaining.items() if not needs]
            if not ready:  # a cycle; parse_charter refuses these first
                break
            widest = max(widest, len(ready))
            for wid in ready:
                del remaining[wid]
            for needs in remaining.values():
                needs.difference_update(ready)
        return max(1, min(self.concurrency, widest or len(self.workers)))

    def describe(self) -> str:
        """A plain-text summary, for the approval prompt and the log."""
        lines = [f"Objective: {self.objective.strip()}", ""]
        if self.seams:
            lines.append("Seams:")
            lines += [f"  {seam.describe()}" for seam in self.seams]
            lines.append("")
        headline = f"{len(self.workers)} Worker Birb(s), {self.concurrency} at a time"
        if self.effective_concurrency < self.concurrency:
            headline += (
                f" — but {self.effective_concurrency} in practice, because some wait "
                "for others"
            )
        lines.append(headline + ":")
        for worker in self.workers:
            lines.append(f"  [{worker.id}] writes {', '.join(worker.writes)}")
            if worker.reads:
                lines.append(f"      reads  {', '.join(worker.reads)}")
            if worker.needs:
                lines.append(f"      after  {', '.join(worker.needs)}")
            if worker.accept:
                lines.append(f"      accept {worker.accept}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _text(value: Any, field: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise CharterError(f"{field} is missing")
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise CharterError(f"{field} must be a non-empty string")
    return value.strip()


def _paths(value: Any, field: str) -> tuple[str, ...]:
    """A list of paths, normalised but *not* resolved.

    Kept relative here so the charter still reads as being about this project
    rather than about one machine's filesystem. Resolution happens in
    ``policy_for``, against the working directory the tools themselves use.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise CharterError(f"{field} must be a list of paths")
    cleaned = tuple(os.path.normpath(entry.strip()) for entry in value if entry.strip())
    if len(set(cleaned)) != len(cleaned):
        raise CharterError(f"{field} names the same path twice")
    return cleaned


def _concurrency(value: Any) -> int:
    if value is None:
        return DEFAULT_CONCURRENCY
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise CharterError("concurrency must be a whole number") from None
    if parsed < 1:
        raise CharterError("concurrency must be at least 1")
    if parsed > MAX_CONCURRENCY:
        raise CharterError(f"concurrency above {MAX_CONCURRENCY} is almost certainly a typo")
    return parsed


def _seam(entry: Any, index: int) -> Seam:
    if not isinstance(entry, dict):
        raise CharterError(f"seam {index} must be a table")
    kind = _text(entry.get("kind"), f"seam {index} kind").lower()
    if kind not in SEAM_KINDS:
        raise CharterError(
            f"seam {index} kind must be {' or '.join(SEAM_KINDS)}, not {kind!r} — "
            "'formal' means the type system holds it up, 'loose' means only a test does"
        )
    return Seam(
        at=_text(entry.get("at"), f"seam {index} at"),
        kind=kind,
        what=_text(entry.get("what"), f"seam {index} what"),
    )


def _worker(entry: Any, index: int) -> WorkerBrief:
    if not isinstance(entry, dict):
        raise CharterError(f"worker {index} must be a table")
    worker_id = _text(entry.get("id"), f"worker {index} id")
    writes = _paths(entry.get("writes"), f"worker {worker_id!r} writes")
    if not writes:
        raise CharterError(
            f"worker {worker_id!r} writes nothing — a Worker Birb with no files to change "
            "has no work to do, and probably means the partition is wrong"
        )
    tests = _paths(entry.get("tests"), f"worker {worker_id!r} tests")
    stray = [path for path in tests if path not in writes]
    if stray:
        raise CharterError(
            f"worker {worker_id!r} lists {', '.join(stray)} under tests but does not write "
            "them — a worker's acceptance tests are its own files, so that it can add to them "
            f"as it works. Either add {', '.join(stray)} to that worker's writes, or drop "
            "them from its tests."
        )
    return WorkerBrief(
        id=worker_id,
        brief=_text(entry.get("brief"), f"worker {worker_id!r} brief"),
        writes=writes,
        reads=_paths(entry.get("reads"), f"worker {worker_id!r} reads"),
        accept=_text(entry.get("accept"), f"worker {worker_id!r} accept", required=False),
        tests=tests,
        needs=_needs(entry.get("needs"), worker_id),
    )


def _needs(value: Any, worker_id: str) -> tuple[str, ...]:
    """The ids this worker waits for, as written.

    Whether they *exist* is checked in ``parse_charter``, which is the only
    place that can see the other workers. Here is just the shape.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise CharterError(f"worker {worker_id!r} needs must be a list of worker ids")
    cleaned = tuple(entry.strip() for entry in value if entry.strip())
    if worker_id in cleaned:
        raise CharterError(
            f"worker {worker_id!r} needs itself, which can never be satisfied"
        )
    if len(set(cleaned)) != len(cleaned):
        raise CharterError(f"worker {worker_id!r} names the same dependency twice")
    return cleaned


# Typographic quotes. A model writing prose and TOML in the same breath emits
# these regularly, and TOML has no idea what they are.
_CURLY_QUOTES = "\u201c\u201d\u2018\u2019"

# A key whose value starts with none of the things a TOML value can start with:
# a quote, a bracket, a brace, a digit, or true/false. Almost always a bare
# string somebody forgot to quote.
_UNQUOTED_VALUE = re.compile(r"^\s*[A-Za-z_][\w-]*\s*=\s*(?![\"'\[{\d]|true\b|false\b)\S")


# A fenced block, with or without a language tag. Models hedge by writing the
# charter into their reply in one of these rather than calling the tool.
_FENCED = re.compile(r"```(?:[A-Za-z0-9_+-]*)\s*\n(.*?)```", re.S)


def recover_charter(text: str) -> "Charter | None":
    """A charter the model wrote into its reply instead of proposing it.

    Nothing about a reply makes a charter — the only thing that proposes one is
    a ``propose_charter`` call. But a model that writes a perfectly good charter
    into a fenced block and then stops has done all the work and missed only the
    mechanism, and the alternative to reading it is telling the user their flock
    produced nothing while the charter sits in the transcript in front of them.

    Fenced blocks are tried first and the whole text last, so a reply that is
    *only* TOML still works. Returns ``None`` for anything that does not parse,
    which is the common case and not an error: most replies are just prose.
    """
    if not text or not text.strip():
        return None
    for candidate in [match.group(1) for match in _FENCED.finditer(text)] + [text]:
        try:
            return parse_charter(candidate)
        except CharterError:
            continue
    return None


def _toml_hint(text: str, exc: Exception) -> str:
    """A specific cause for a TOML error, when one can be identified.

    ``tomllib`` reports *where* it gave up, not what the author did wrong, and
    "Invalid value (at line 1, column 13)" is the identical message for a curly
    quote and for an unquoted string. That is enough for a person with the file
    in front of them and nowhere near enough for a model trying to correct its
    own output — which will otherwise send the same thing again, and again,
    because nothing it was told distinguishes one attempt from the next.

    The whole text is scanned rather than the line the error names:
    ``TOMLDecodeError`` only grew ``lineno`` in Python 3.13, and a hint that
    silently stops working on the interpreter most people are running is worse
    than one that occasionally names a second suspect line.
    """
    hints = []
    if any(ch in text for ch in _CURLY_QUOTES):
        hints.append(
            "the text contains typographic quotes (" + " ".join(_CURLY_QUOTES) + ") — "
            "TOML only understands straight ones (\" and ')"
        )
    for number, line in enumerate(text.splitlines(), start=1):
        if _UNQUOTED_VALUE.match(line):
            hints.append(
                f"line {number} looks like a string value with no quotes around it "
                f"({line.strip()[:60]!r}) — every string in TOML needs them"
            )
            break
    if not hints:
        return ""
    return "\n\nLikely cause: " + "; ".join(hints) + "."


def parse_charter(text: str) -> Charter:
    """Read a charter from TOML, or say clearly why it cannot be read.

    Every message names the field it is about, because this text was written by
    a model and is being fixed by a person — "worker 'a' writes nothing" sends
    someone to the right line, where a bare TOML error does not.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CharterError(f"not valid TOML — {exc}{_toml_hint(text, exc)}") from exc
    except (TypeError, AttributeError) as exc:  # not a string at all
        raise CharterError(f"a charter must be text — {exc}") from exc

    raw_workers = data.get("workers")
    if not isinstance(raw_workers, list) or not raw_workers:
        raise CharterError("a charter needs at least one [[workers]] entry")

    workers = tuple(_worker(entry, index) for index, entry in enumerate(raw_workers))
    identifiers = [worker.id for worker in workers]
    duplicates = {i for i in identifiers if identifiers.count(i) > 1}
    if duplicates:
        raise CharterError(f"two workers share the id {', '.join(sorted(duplicates))}")

    check_dependencies(workers)

    raw_seams = data.get("seams") or []
    if not isinstance(raw_seams, list):
        raise CharterError("seams must be a list of [[seams]] tables")

    seams = tuple(_seam(entry, index) for index, entry in enumerate(raw_seams))
    check_seams_are_frozen(seams, workers)

    return Charter(
        objective=_text(data.get("objective"), "objective"),
        workers=workers,
        seams=seams,
        concurrency=_concurrency(data.get("concurrency")),
    )


def frozen_seam_paths(seams: "tuple[Seam, ...] | list[Seam]") -> dict[str, Seam]:
    """The seam files no worker may write, keyed by normalised path.

    Formal whole-file seams only — see ``check_seams_are_frozen`` for why the
    loose and ``::``-qualified ones are exempt. Public because the incremental
    builder (``plan.PlanDraft``) has to answer the same question one move at a
    time, and two implementations of "is this path frozen?" would drift.
    """
    return {
        os.path.normpath(seam.at): seam
        for seam in seams
        if seam.enforced_by_types and "::" not in seam.at
    }


def check_seams_are_frozen(
    seams: "tuple[Seam, ...] | list[Seam]", workers: "tuple[WorkerBrief, ...] | list[WorkerBrief]"
) -> None:
    """Refuse a charter that hands a formal seam to a worker to write.

    A formal seam is a declared artifact that Brainy Birb wrote into the
    skeleton and that the type system holds up. Giving one to a worker
    contradicts the charter's own words twice over: the file is already there,
    and everybody else is building against it while that worker changes it.

    Refused rather than reported, because it is the structural cause of the
    commonest way a partition falls apart. One worker claims the shared types
    file, every other worker reads it, and the run produces one read/write
    overlap per reader — four of them for five workers — none of which is
    really about the readers. The overlaps were being reported to Brainy Birb
    as a partition problem, and the fix it needed was to stop claiming a file
    it did not have to write. Named here, it is one sentence about one line.

    **Loose seams are left alone, and whole-file ones only are checked.** A
    loose seam is an agreement with nothing but a test behind it, and it is
    perfectly reasonable for it to describe behaviour *inside* a file some
    worker implements ("returns None for a missing key"). A seam whose ``at``
    carries a ``::`` qualifier names a symbol rather than a file for the same
    reason. Both of those the read/write check already covers, and refusing
    them here would throw out charters that were right.
    """
    frozen = frozen_seam_paths(seams)
    for worker in workers:
        for path in worker.writes:
            seam = frozen.get(path)
            if seam is None:
                continue
            raise CharterError(
                f"worker {worker.id!r} writes {path}, which this charter declares as a "
                f"formal seam ({seam.what}). Seam files belong to you and are writable "
                "by no worker — you already wrote it into the skeleton, and everyone "
                f"else is building against it. Remove {path} from worker "
                f"{worker.id!r} writes; it may still read it. If that worker really has "
                "to change it, then it is not a seam: drop the [[seams]] entry instead."
            )


def check_dependencies(workers: tuple[WorkerBrief, ...]) -> None:
    """Refuse a dependency graph that cannot be run, at parse time.

    Both failures here are ones a model produces regularly and neither is
    survivable later: an id that does not exist would leave a worker waiting
    for something that never finishes, and a cycle would leave every worker in
    it waiting for the others forever. Caught here, they are a tool failure
    Brainy Birb can correct on the next turn; caught in the scheduler, they are
    a flock that hangs with no idea why.
    """
    known = {worker.id for worker in workers}
    for worker in workers:
        unknown = [need for need in worker.needs if need not in known]
        if unknown:
            raise CharterError(
                f"worker {worker.id!r} needs {', '.join(sorted(unknown))}, which "
                f"{'is' if len(unknown) == 1 else 'are'} not in this charter — "
                f"the workers are {', '.join(sorted(known))}"
            )

    cycle = _find_cycle(workers)
    if cycle:
        raise CharterError(
            "these workers wait on each other in a circle and none could ever "
            f"start: {' -> '.join(cycle)}"
        )


def _find_cycle(workers: tuple[WorkerBrief, ...]) -> list[str]:
    """One cycle in the dependency graph, named in order, or an empty list.

    Named rather than merely detected: "there is a cycle" sends whoever is
    fixing it to read the whole charter, and the charter was written by a model
    that will need to be told which edge to drop.
    """
    needs = {worker.id: worker.needs for worker in workers}
    visiting: set[str] = set()
    done: set[str] = set()
    path: list[str] = []

    def walk(worker_id: str) -> list[str]:
        if worker_id in done:
            return []
        if worker_id in visiting:
            # Back to something on the current path: the cycle is the tail of
            # the path from that point, closed by repeating it.
            start = path.index(worker_id)
            return path[start:] + [worker_id]
        visiting.add(worker_id)
        path.append(worker_id)
        for need in needs.get(worker_id, ()):
            found = walk(need)
            if found:
                return found
        path.pop()
        visiting.discard(worker_id)
        done.add(worker_id)
        return []

    for worker in workers:
        found = walk(worker.id)
        if found:
            return found
    return []


# --------------------------------------------------------------------------- #
# The partition check
# --------------------------------------------------------------------------- #
CONFLICT_WRITE_WRITE = "write_write"
CONFLICT_READ_WRITE = "read_write"


def _join(names: tuple[str, ...]) -> str:
    """``"a"``, ``"a and b"``, ``"a, b and c"`` — for prose a person reads."""
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


@dataclass(frozen=True)
class Conflict:
    """Workers whose scopes overlap in a way that breaks an invariant.

    **One per path, not one per pair.** A file that several workers read and
    one writes is a single mistake about a single line of the charter, and
    reporting it once per reader turns it into four. That mattered more than it
    sounds: the count is the first thing anyone reads, and "4 overlaps in the
    partition" describes a partition in ruins rather than one shared types file
    that ended up in somebody's ``writes``.

    So ``workers`` holds every worker on the reported side of the overlap — all
    the owners of a write/write, all the readers of a read/write — and
    ``writer`` names the single owner that a read/write is about.
    """

    kind: str
    path: str
    workers: tuple[str, ...]
    writer: str = ""

    def describe(self) -> str:
        who = _join(self.workers)
        several = len(self.workers) > 1
        if self.kind == CONFLICT_WRITE_WRITE:
            return (
                f"{self.path}: {who} {'all' if len(self.workers) > 2 else 'both'} write it. "
                "Exclusive ownership is what makes concurrent workers safe without locking, "
                "and what makes 'which worker broke this' answerable."
            )
        return (
            f"{self.path}: {who} {'read' if several else 'reads'} what {self.writer} writes. "
            f"{'They' if several else 'The reader'} would be working against a moving target "
            "— a seam workers depend on belongs to Brainy Birb, writable by none of them. "
            f"Usually {self.writer} did not need to write it at all."
        )


def find_conflicts(charter: Charter) -> list[Conflict]:
    """Every overlap in the partition, for the user to resolve.

    Deliberately *reported* rather than refused. The design's rule is exclusive
    ownership, but how to get there varies with the situation in ways CoBirb
    cannot guess — merge the two workers, hoist the shared file into a seam
    Brainy Birb owns, split it, let git reconcile them afterwards. So this
    answers "what is wrong" and leaves "what to do about it" to the person
    approving the run.

    Two kinds, both violating an invariant:

    - **write/write** breaks exclusive ownership, which is the property that
      makes concurrency safe by construction rather than by locking. Always a
      conflict, whatever the ordering: two owners of one file means "which
      worker broke this" stops having an answer.
    - **read/write** breaks the frozen-seam rule. If A reads a file B writes,
      A is reading something that changes underneath it; the shared vocabulary
      two workers meet at has to be owned by neither of them.

    **A declared dependency answers the read/write case rather than excusing
    it.** If A says it ``needs`` B, then B has finished before A starts, so the
    file is not changing underneath A — the condition the conflict exists to
    catch is absent, and reporting it anyway would force the user to wave
    through the very thing the charter said explicitly. An *undeclared*
    overlap is still reported, because that is a worker reading a moving file
    without anyone having decided it should.
    """
    conflicts: list[Conflict] = []

    writers: dict[str, list[str]] = {}
    for worker in charter.workers:
        for path in worker.writes:
            writers.setdefault(path, []).append(worker.id)

    for path, owners in sorted(writers.items()):
        if len(owners) > 1:
            conflicts.append(
                Conflict(CONFLICT_WRITE_WRITE, path, tuple(sorted(owners)))
            )

    # Gathered per (path, writer) and reported once, not once per reader. Five
    # workers around one shared types file produced four identical overlaps,
    # and four of anything reads as a partition that needs re-cutting when the
    # truth was one file in one worker's `writes`.
    readers: dict[tuple[str, str], list[str]] = {}
    for worker in charter.workers:
        for path in worker.reads:
            for other in writers.get(path, []):
                if other != worker.id and other not in worker.needs:
                    readers.setdefault((path, other), []).append(worker.id)

    for (path, writer), who in sorted(readers.items()):
        conflicts.append(
            Conflict(CONFLICT_READ_WRITE, path, tuple(sorted(who)), writer=writer)
        )

    return conflicts


def writes_owner(charter: Charter, path: str, *, besides: str = "") -> str:
    """Which Worker Birb owns ``path``, if one does and it is not ``besides``.

    The check behind the one thing a worker may never be *asked* about. A
    worker that needs a capability it was not given — to run a command, to
    reach a tool nobody granted it — can put that question to the user, and
    most of the time the answer is a reasonable yes. Writing into a file
    another worker owns is the exception, and it is refused outright rather
    than offered as a dialog.

    Two reasons it is not a question. Exclusive ownership of files is not a
    convention here, it is the property that makes concurrent workers safe by
    construction: grant it away mid-round and two agents are editing one file
    with no lock between them, and "which worker broke this" stops having an
    answer. And the person answering cannot reasonably be expected to hold the
    whole partition in their head at the moment a dialog appears — CoBirb has
    the charter right there and can simply check.

    ``path`` is matched as the charter states it. Both sides come from the same
    normalisation (``_paths``), so this compares like with like rather than
    trying to resolve a path the charter deliberately kept relative.
    """
    target = os.path.normpath(path.strip()) if path.strip() else ""
    if not target:
        return ""
    for worker in charter.workers:
        if worker.id == besides:
            continue
        if target in worker.writes:
            return worker.id
    return ""


def describe_conflicts(conflicts: list[Conflict]) -> str:
    """The text shown when a partition does not hold together."""
    if not conflicts:
        return "The partition is disjoint: every file has exactly one owner."
    lines = [f"{len(conflicts)} overlap(s) in the partition:"]
    lines += [f"  • {conflict.describe()}" for conflict in conflicts]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Scopes become policy
# --------------------------------------------------------------------------- #
def policy_for(
    worker: WorkerBrief, cwd: str, *, audit_log_enabled: bool = False
) -> Policy:
    """The ``Policy`` a Worker Birb runs under.

    **Writes are strict; reads are open across the project.** A worker may
    change only the files its charter named — that is the isolation that
    matters, and the reason two workers can run at once without clobbering each
    other. But it may *read* anything under the working directory: every read
    tool (``read_file``, ``list_dir``, ``glob``, ``grep``, ``repo_map``) works,
    and nothing outside ``cwd`` does.

    This was not the first design. Reads were once scoped to the exact files in
    the brief too, on the theory that a worker "does not know the other files
    exist". In the first real run that theory met reality: the workers could
    not orient themselves at all — denied on every ``list_dir`` and ``glob`` —
    and spent their turns flailing against a wall of "permission denied". The
    knowledge isolation it was supposed to protect never depended on the read
    permission anyway. The plan (the charter) is never written to disk, and the
    brief deliberately omits it, so a worker reading a sibling file sees *code*,
    not the plan. Read isolation bought almost nothing and broke the work.

    What survives is the part that was always doing the isolating: the brief
    says only what this worker must do, and the write scope keeps it in its
    lane. The charter approval is what authorises project-wide read — the user
    saw exactly what would run before any of it did.

    Writes stay file-level out of directory-level machinery: ``Policy`` grants
    by prefix and ``_within`` matches a path that *is* the granted one or sits
    beneath it, so granting a file matches that file and nothing else. That
    granularity is also what keeps this working in languages CoBirb cannot
    parse — it owns files, not symbols.

    **``shell`` is granted for the worker's own ``accept`` command and nothing
    else.** It used to be granted for nothing at all, and the acceptance check
    was run *for* the worker after its turn. That made the ticket's definition
    of done the one thing it could not observe: it wrote an implementation
    blind, learned once whether the check passed, got a single fix attempt, and
    was finished — with its report saying "acceptance check FAILED" about work
    it never had a chance to iterate on. Being able to run the check is what
    turns a blind write into converging on green, and it is the difference
    between a ticket that lands and one that reports a shortcoming.

    It is still least privilege. The command is one string out of the charter
    the user read and approved, granted as ``Policy`` prefix rules, so every
    segment of anything the worker runs has to match part of that invocation —
    ``pytest tests/test_csv.py -q`` does not become ``rm``, and a chained
    command that adds something else is refused whole. A single-word ``accept``
    (``make``, ``pytest``) is the one loose case, since a one-word prefix
    trusts that binary with any arguments; that is the user's own stated check,
    named by them, and narrowing it further is not something ``Policy`` can
    express. Everything else a worker turns out to need still goes through the
    escalation path, where a person answers.

    **``allow_command`` rather than ``allow``**, because an ``accept`` is
    frequently more than one segment — ``pytest -q && ruff check src`` is an
    ordinary definition of done — and ``allow`` grants the first segment only.
    That produced a grant which denied the very command it was made from: the
    worker was refused its own acceptance check, escalated to the user for a
    command the user had already approved in the charter, and did it again on
    every attempt. A command the scan cannot read through grants nothing and
    the worker simply has no shell, which is the same as before this existed;
    the post-turn verification runs the check either way, so the ticket is
    never lost to this.
    """
    policy = build_default_policy(audit_log_enabled=audit_log_enabled, cwd=cwd)
    for path in worker.writes:
        policy.allow_write_dir(path)
        policy.allow_read_dir(path)
    # Read the whole project. The declared ``reads`` are now a subset of this
    # and kept only for the conflict check and the brief; the grant that makes
    # a worker able to orient itself is the working directory.
    policy.allow_read_dir(cwd)
    if worker.accept.strip():
        policy.allow_command(worker.accept)
    return policy
