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
"""
from __future__ import annotations

import os
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

    def describe(self) -> str:
        """A plain-text summary, for the approval prompt and the log."""
        lines = [f"Objective: {self.objective.strip()}", ""]
        if self.seams:
            lines.append("Seams:")
            lines += [f"  {seam.describe()}" for seam in self.seams]
            lines.append("")
        lines.append(f"{len(self.workers)} Worker Birb(s), {self.concurrency} at a time:")
        for worker in self.workers:
            lines.append(f"  [{worker.id}] writes {', '.join(worker.writes)}")
            if worker.reads:
                lines.append(f"      reads  {', '.join(worker.reads)}")
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
    )


def parse_charter(text: str) -> Charter:
    """Read a charter from TOML, or say clearly why it cannot be read.

    Every message names the field it is about, because this text was written by
    a model and is being fixed by a person — "worker 'a' writes nothing" sends
    someone to the right line, where a bare TOML error does not.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CharterError(f"not valid TOML — {exc}") from exc
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

    raw_seams = data.get("seams") or []
    if not isinstance(raw_seams, list):
        raise CharterError("seams must be a list of [[seams]] tables")

    return Charter(
        objective=_text(data.get("objective"), "objective"),
        workers=workers,
        seams=tuple(_seam(entry, index) for index, entry in enumerate(raw_seams)),
        concurrency=_concurrency(data.get("concurrency")),
    )


# --------------------------------------------------------------------------- #
# The partition check
# --------------------------------------------------------------------------- #
CONFLICT_WRITE_WRITE = "write_write"
CONFLICT_READ_WRITE = "read_write"


@dataclass(frozen=True)
class Conflict:
    """Two workers whose scopes overlap in a way that breaks an invariant."""

    kind: str
    path: str
    workers: tuple[str, ...]

    def describe(self) -> str:
        who = " and ".join(self.workers)
        if self.kind == CONFLICT_WRITE_WRITE:
            return (
                f"{self.path}: {who} both write it. Exclusive ownership is what makes "
                "concurrent workers safe without locking, and what makes 'which worker "
                "broke this' answerable."
            )
        reader, writer = self.workers
        return (
            f"{self.path}: {reader} reads what {writer} writes. The reader would be working "
            "against a moving target — a seam both depend on belongs to Brainy Birb, writable "
            "by neither."
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
      makes concurrency safe by construction rather than by locking.
    - **read/write** breaks the frozen-seam rule. If A reads a file B writes,
      A is reading something that changes underneath it; the shared vocabulary
      two workers meet at has to be owned by neither of them.
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

    for worker in charter.workers:
        for path in worker.reads:
            for other in writers.get(path, []):
                if other != worker.id:
                    conflicts.append(
                        Conflict(CONFLICT_READ_WRITE, path, (worker.id, other))
                    )

    return conflicts


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
    parse — it owns files, not symbols. ``shell`` is granted to no worker;
    running commands unattended is the one capability the charter approval does
    not extend, and the acceptance check is run *for* the worker instead.
    """
    policy = build_default_policy(audit_log_enabled=audit_log_enabled, cwd=cwd)
    for path in worker.writes:
        policy.allow_write_dir(path)
        policy.allow_read_dir(path)
    # Read the whole project. The declared ``reads`` are now a subset of this
    # and kept only for the conflict check and the brief; the grant that makes
    # a worker able to orient itself is the working directory.
    policy.allow_read_dir(cwd)
    return policy
