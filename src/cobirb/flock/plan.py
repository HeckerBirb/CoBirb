"""Building a charter one validated move at a time.

A charter used to arrive as one document. Brainy Birb wrote the whole TOML —
objective, seams, every ticket, every file list, the dependency graph — and
``parse_charter`` accepted or refused all of it together. That asks a model to
be right about eight things at once, and refuses the lot when it is wrong about
one. Everything downstream is gated on that single artifact, so the highest
variance step in a flock was also its wall.

This is the other way round: the plan is **accumulated**, and each move is
checked against what is already there the moment it is made.

    add_worker("store", writes=["store.py", "tests/test_store.py"], …)
    add_worker("cli",   writes=["cli.py", "tests/test_cli.py"], reads=["store.py"], …)
    seal("Add CSV export", concurrency=2)   ->  Charter

Three things follow from that, and they are the whole reason for the module.

**A bad partition becomes unrepresentable rather than reported.** ``add_worker``
refuses a path another ticket already owns, so there is no later stage at which
two owners of one file can be discovered — ``find_conflicts`` stops being a
validator over a finished document and becomes an invariant of construction.
The overlap that could not be fixed by re-proposing cannot be built.

**A refusal names one thing and costs one move.** "export/types.py is already
owned by ticket 'writer'" arrives while the model is writing the ticket that
caused it, with one path to change. The document-shaped version of the same
mistake was "4 overlap(s) in the partition" after every ticket had been
written, and the correction was to send all of them again.

**Order does not matter.** The ownership check is symmetric, so the model can
add tickets in whatever order it thought of them — the one thing deferred to
``seal`` is ``needs``, which may legitimately name a ticket that has not been
added yet. Reading a file another ticket writes is not refused: the reader
builds against the skeleton's signatures, and those are the owner's to keep
(see ``charter.Seam``).

Nothing here writes to the repository or reads a plan from it. A draft lives in
memory for exactly as long as the session's planning does, the same as the
charter it produces.
"""
from __future__ import annotations

import os

from .charter import (
    DEFAULT_CONCURRENCY,
    Charter,
    CharterError,
    Seam,
    WorkerBrief,
    check_dependencies,
    find_conflicts,
)

# Normalised the same way ``charter._paths`` normalises, so a draft compares
# like with like and "export/./types.py" cannot become a second owner of a file
# that already has one.
def _norm(path: str) -> str:
    return os.path.normpath(path.strip()) if path.strip() else ""


def _norm_all(paths: "list[str] | tuple[str, ...] | None", field: str) -> tuple[str, ...]:
    if paths is None:
        return ()
    if isinstance(paths, str):  # a model that sent one path unwrapped
        paths = [paths]
    if not isinstance(paths, (list, tuple)) or any(not isinstance(p, str) for p in paths):
        raise CharterError(f"{field} must be a list of paths")
    cleaned = tuple(_norm(p) for p in paths if p.strip())
    if len(set(cleaned)) != len(cleaned):
        raise CharterError(f"{field} names the same path twice")
    return cleaned


class PlanDraft:
    """A charter under construction, validated move by move.

    Every mutator either applies the move and returns a sentence describing
    where the plan now stands, or raises ``CharterError`` naming the single
    thing that is wrong. Nothing partially applies: a refused ``add_worker``
    leaves the draft exactly as it was, so the model's next attempt starts from
    a state it can still reason about.

    The verdict strings carry the running totals on purpose. A model calling
    four tools in sequence has no other way to know how much of its own plan
    has landed, and one that has lost count re-adds a ticket it already added.
    """

    def __init__(self) -> None:
        self.seams: list[Seam] = []
        self.workers: list[WorkerBrief] = []

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def empty(self) -> bool:
        return not self.workers and not self.seams

    def worker(self, worker_id: str) -> WorkerBrief | None:
        for worker in self.workers:
            if worker.id == worker_id:
                return worker
        return None

    def owner_of(self, path: str) -> str:
        """Which ticket already writes ``path``, if any."""
        target = _norm(path)
        for worker in self.workers:
            if target in worker.writes:
                return worker.id
        return ""

    def describe(self) -> str:
        """Where the plan stands, for a verdict string."""
        seams = f"{len(self.seams)} seam(s)"
        tickets = f"{len(self.workers)} ticket(s)"
        if self.workers:
            tickets += " (" + ", ".join(worker.id for worker in self.workers) + ")"
        return f"{seams}, {tickets}"

    # ------------------------------------------------------------------ #
    # Moves
    # ------------------------------------------------------------------ #
    def declare_seam(self, at: str, kind: str, what: str) -> str:
        """Record one agreement the workers meet at."""
        at = (at or "").strip()
        if not at:
            raise CharterError("a seam needs `at`: the file, or file::symbol, it lives at")
        kind = (kind or "").strip().lower()
        what = (what or "").strip()
        if not what:
            raise CharterError(
                f"seam at {at} needs `what` — one sentence saying what the agreement is. "
                "It is the part of the charter the approver most needs and cannot infer."
            )
        seam = Seam(at=at, kind=kind, what=what)
        if kind not in ("formal", "loose"):
            raise CharterError(
                f"seam kind must be 'formal' or 'loose', not {kind!r} — 'formal' means the "
                "type system holds it up, 'loose' means only a test does"
            )
        if any(existing.at == at for existing in self.seams):
            raise CharterError(f"a seam at {at} is already declared")

        self.seams.append(seam)
        return f"Seam recorded at {at} ({kind}). Plan so far: {self.describe()}."

    def add_worker(
        self,
        worker_id: str,
        *,
        brief: str,
        writes: "list[str] | None" = None,
        reads: "list[str] | None" = None,
        accept: str = "",
        tests: "list[str] | None" = None,
        needs: "list[str] | None" = None,
    ) -> str:
        """Add one ticket, or say precisely why it cannot be added.

        The refusals are the point of this method. Each one names a single
        path, the ticket that already has it, and what to do — because the
        model reading it is mid-way through writing a plan and can still fix
        this cheaply, which was never true of a whole charter rejected at the
        end.
        """
        worker_id = (worker_id or "").strip()
        if not worker_id:
            raise CharterError("a ticket needs an id")
        if self.worker(worker_id) is not None:
            raise CharterError(
                f"ticket {worker_id!r} already exists. Use a different id, or call "
                f"drop_worker({worker_id!r}) first if you mean to replace it."
            )
        brief = (brief or "").strip()
        if not brief:
            raise CharterError(
                f"ticket {worker_id!r} needs a brief — it is the only thing that worker "
                "will ever be told about what to do"
            )

        write_paths = _norm_all(writes, f"ticket {worker_id!r} writes")
        if not write_paths:
            raise CharterError(
                f"ticket {worker_id!r} writes nothing — a Worker Birb with no files to "
                "change has no work to do, and probably means the partition is wrong"
            )
        read_paths = _norm_all(reads, f"ticket {worker_id!r} reads")
        test_paths = _norm_all(tests, f"ticket {worker_id!r} tests")
        need_ids = self._needs(needs, worker_id)

        # A test file the ticket did not also claim to write. **Adopted rather
        # than refused**, because there is exactly one thing it can mean: a
        # worker's acceptance tests are its own files — it has to be able to
        # add to them as it works — so a path under `tests` is a path this
        # ticket writes, and the model simply did not say so twice. Refusing it
        # asked a model to re-send a whole ticket to move one string between two
        # lists, and a model that narrates the correction instead of sending it
        # loses the round. Nothing is given away by adopting: the adopted paths
        # go through `_check_writes_are_free` with the rest, so a genuine
        # collision with another ticket is still refused below.
        adopted = tuple(path for path in test_paths if path not in write_paths)
        write_paths = write_paths + adopted

        self._check_writes_are_free(worker_id, write_paths)

        self.workers.append(
            WorkerBrief(
                id=worker_id, brief=brief, writes=write_paths, reads=read_paths,
                accept=(accept or "").strip(), tests=test_paths, needs=need_ids,
            )
        )
        note = f"Ticket {worker_id!r} added: writes {', '.join(write_paths)}."
        if adopted:
            # Said, not silent. The model's next move may well be a second
            # ticket claiming one of these paths, and it needs to know this
            # ticket now owns them.
            note += (
                f" {', '.join(adopted)} was listed under tests but not writes, so it has "
                "been added to writes — a worker's acceptance tests are files it owns."
            )
        if not (accept or "").strip():
            # Said rather than refused: a ticket with no check is legal and
            # occasionally right, but it can never be reported as complete —
            # `WorkerReport.complete` requires the check to have passed — so a
            # charter full of them is a round that cannot succeed.
            note += (
                " It has no `accept` command, so nothing can confirm it is done and it will"
                " never be reported complete. Add one unless there is genuinely no check."
            )
        elif not test_paths and len(write_paths) > 1:
            # Also said rather than refused, and for a sharper reason than it
            # looks: `tests` is what review subtracts to find the
            # implementation to put back. Undeclared, a worker's test files
            # count as implementation, get restored along with it, and the
            # strongest check in the round has nothing of the worker's work left
            # to check against. Review now reports that it could not check
            # rather than passing it — but a charter that says so up front is
            # better than a round that finds out afterwards.
            note += (
                " It declares no `tests`. If any of those files hold this ticket's "
                "acceptance tests, name them in `tests` — review restores the "
                "implementation and keeps the tests, and it cannot tell them apart by "
                "filename."
            )
        return f"{note} Plan so far: {self.describe()}."

    def drop_worker(self, worker_id: str) -> str:
        """Remove a ticket, so a plan can be backed out of rather than restarted.

        Exists because the checks above are strict, and a strict check with no
        way back turns one wrong move into a plan that has to be abandoned. The
        commonest case: a ticket claimed a file, and the ticket written two
        moves later turns out to be the one that should own it.
        """
        worker_id = (worker_id or "").strip()
        worker = self.worker(worker_id)
        if worker is None:
            known = ", ".join(w.id for w in self.workers) or "none yet"
            raise CharterError(f"there is no ticket {worker_id!r} — the tickets are: {known}")
        dependents = [w.id for w in self.workers if worker_id in w.needs]
        if dependents:
            raise CharterError(
                f"ticket {worker_id!r} cannot be dropped: {', '.join(sorted(dependents))} "
                f"declare needs on it. Drop {'those' if len(dependents) > 1 else 'that'} "
                "first, or keep this one."
            )
        self.workers.remove(worker)
        return f"Ticket {worker_id!r} dropped. Plan so far: {self.describe()}."

    def seal(self, objective: str, concurrency: int | None = None) -> Charter:
        """Turn the draft into a charter, or refuse to.

        Only the checks that need the whole plan live here: that there is a
        plan at all, that every ``needs`` resolves, and that nothing waits in a
        circle. Everything else was settled when the move was made, which is
        why this rarely fails — and why, when it does, it is about the graph
        rather than about a file.
        """
        objective = (objective or "").strip()
        if not objective:
            raise CharterError(
                "the charter needs an objective — one paragraph on what this round of "
                "work is for. It is the first thing the approver reads."
            )
        if not self.workers:
            raise CharterError(
                "there are no tickets to run. Call add_worker for each Worker Birb before "
                "sealing. If this work should not be divided at all, do not seal a charter "
                "— say so in your reply instead, which is a legitimate answer."
            )

        workers = tuple(self.workers)
        check_dependencies(workers)  # unknown ids and cycles, named

        charter = Charter(
            objective=objective,
            workers=workers,
            seams=tuple(self.seams),
            concurrency=DEFAULT_CONCURRENCY if concurrency is None else int(concurrency),
        )
        # Belt and braces. By construction this is empty, and if it is ever not
        # then a move-level check has a hole in it — which is a thing to
        # discover here, with the charter in hand, rather than at approval.
        leftover = find_conflicts(charter)
        if leftover:
            raise CharterError(
                "the sealed plan still overlaps, which should not be reachable: "
                + "; ".join(conflict.describe() for conflict in leftover)
            )
        return charter

    # ------------------------------------------------------------------ #
    # The per-move checks
    # ------------------------------------------------------------------ #
    def _needs(self, value: "list[str] | None", worker_id: str) -> tuple[str, ...]:
        """The ids this ticket waits for. Existence is ``seal``'s business.

        Deliberately not checked against the tickets added so far: a plan built
        in the order the work occurred to somebody will name a dependency
        before adding it, and refusing that would impose an ordering rule for
        no gain. ``check_dependencies`` sees the whole set.
        """
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
            raise CharterError(f"ticket {worker_id!r} needs must be a list of ticket ids")
        cleaned = tuple(v.strip() for v in value if v.strip())
        if worker_id in cleaned:
            raise CharterError(f"ticket {worker_id!r} needs itself, which can never be satisfied")
        if len(set(cleaned)) != len(cleaned):
            raise CharterError(f"ticket {worker_id!r} names the same dependency twice")
        return cleaned

    def _check_writes_are_free(self, worker_id: str, writes: tuple[str, ...]) -> None:
        """Exclusive ownership, enforced at the moment a path is claimed.

        This is the check that makes an overlapping partition unbuildable. Two
        owners of one file is what made concurrency unsafe and made "which
        worker broke this" unanswerable, and it was previously discovered after
        the whole charter existed.

        **It is the only check on a path.** Claiming a declared seam, or a file
        another ticket reads, used to be refused too, and the refusal told the
        model to "leave it to the skeleton and drop it from this ticket's
        writes" — which, for a stub file, is an instruction to leave it
        unimplemented. See ``charter.Seam``.
        """
        for path in writes:
            owner = self.owner_of(path)
            if owner:
                raise CharterError(
                    f"{path} is already written by ticket {owner!r}, and two owners of one "
                    "file is what makes concurrent workers unsafe. Give this ticket a "
                    f"different file, or — if {owner!r} should not have claimed it — call "
                    f"drop_worker({owner!r}) and add it back without that path."
                )


# --------------------------------------------------------------------------- #
# What the skeleton wrote
# --------------------------------------------------------------------------- #
# Past this many files the snapshot is skipped rather than taken. It feeds a
# question, never a refusal, so a project too large to walk quickly loses the
# question and nothing else.
SNAPSHOT_MAX_FILES = 20000


def snapshot_project(cwd: str) -> "dict[str, tuple[int, int]] | None":
    """Every file under ``cwd`` with its modification time and size, or None.

    Taken before Brainy Birb plans, so the files it wrote into the skeleton can
    be told apart from the ones that were already there — ``orchestrator``
    records which tools were called, not which paths they touched. Honours
    ``.gitignore`` and the built-in ignore list, like ``glob`` and ``grep``.
    """
    from .. import paths
    from ..plugins.core.ignores import IgnoreRules

    rules = IgnoreRules.for_directory(cwd)
    # CoBirb's own tree is skipped too: run from the home directory, the
    # project contains ~/.cobirb, and the session and checkpoint files planning
    # writes there are not the skeleton.
    own = os.path.realpath(paths.cobirb_dir())
    files: dict[str, tuple[int, int]] = {}
    for root, dirs, names in os.walk(cwd):
        dirs[:] = [
            d for d in dirs
            if not rules.is_ignored(os.path.join(root, d), is_dir=True)
            and os.path.realpath(os.path.join(root, d)) != own
        ]
        for name in names:
            path = os.path.join(root, name)
            if rules.is_ignored(path, is_dir=False):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files[_norm(os.path.relpath(path, cwd))] = (stat.st_mtime_ns, stat.st_size)
            if len(files) > SNAPSHOT_MAX_FILES:
                return None
    return files


def written_since(before: "dict[str, tuple[int, int]] | None", cwd: str) -> list[str]:
    """Files created or changed under ``cwd`` since ``before`` was taken."""
    if before is None:
        return []
    after = snapshot_project(cwd)
    if after is None:
        return []
    return sorted(path for path, stamp in after.items() if before.get(path) != stamp)
