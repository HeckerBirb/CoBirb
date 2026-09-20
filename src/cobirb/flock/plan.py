"""Building a charter one validated move at a time.

A charter used to arrive as one document. Brainy Birb wrote the whole TOML —
objective, seams, every ticket, every file list, the dependency graph — and
``parse_charter`` accepted or refused all of it together. That asks a model to
be right about eight things at once, and refuses the lot when it is wrong about
one. Everything downstream is gated on that single artifact, so the highest
variance step in a flock was also its wall.

This is the other way round: the plan is **accumulated**, and each move is
checked against what is already there the moment it is made.

    declare_seam("export/types.py", "formal", "the shared vocabulary")
    add_worker("writer", writes=["export/csv.py"], reads=["export/types.py"], …)
    add_worker("cli",    writes=["cli.py"],        reads=["export/types.py"], …)
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

**Order does not matter.** Both directions of every check run on every move: a
ticket that claims a file an existing ticket reads is refused, and so is a
ticket that reads a file an existing ticket claims. So the model can add
tickets in whatever order it thought of them without a rule about which comes
first — the one thing deferred to ``seal`` is ``needs``, which may legitimately
name a ticket that has not been added yet.

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
    frozen_seam_paths,
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

        # The other direction of the frozen-seam rule. Declaring a formal seam
        # over a file some ticket already owns is the same contradiction as
        # giving a ticket a seam to write, and it is reachable simply by
        # declaring seams after tickets.
        if frozen_seam_paths([seam]):
            owner = self.owner_of(at)
            if owner:
                raise CharterError(
                    f"ticket {owner!r} already writes {at}, so it cannot also be a formal "
                    "seam — a seam is frozen while the workers build against it. Either "
                    f"drop {at} from {owner!r} (drop_worker, then add it back without that "
                    "path), or declare this seam `loose` if it is an agreement about "
                    "behaviour rather than a file you own."
                )
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

        stray = [path for path in test_paths if path not in write_paths]
        if stray:
            raise CharterError(
                f"ticket {worker_id!r} lists {', '.join(stray)} under tests but does not "
                "write them — a worker's acceptance tests are its own files, so that it "
                "can add to them as it works. Add them to writes, or drop them from tests."
            )

        self._check_writes_are_free(worker_id, write_paths)
        self._check_reads_are_still(worker_id, read_paths, need_ids)

        self.workers.append(
            WorkerBrief(
                id=worker_id, brief=brief, writes=write_paths, reads=read_paths,
                accept=(accept or "").strip(), tests=test_paths, needs=need_ids,
            )
        )
        note = f"Ticket {worker_id!r} added: writes {', '.join(write_paths)}."
        if not (accept or "").strip():
            # Said rather than refused: a ticket with no check is legal and
            # occasionally right, but it can never be reported as complete —
            # `WorkerReport.complete` requires the check to have passed — so a
            # charter full of them is a round that cannot succeed.
            note += (
                " It has no `accept` command, so nothing can confirm it is done and it will"
                " never be reported complete. Add one unless there is genuinely no check."
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

        **This ticket's own ``needs`` deliberately does not excuse a reader
        below.** "I write what an earlier ticket reads, and I wait for it" is in
        fact safe — the read happens before the write — but ``find_conflicts``
        only excuses the mirror case, reader-waits-for-writer, and it is what
        ``check_partition`` shows the user and what ``seal`` re-checks. Allowing
        a shape here that ``seal`` then refuses would be a worse bargain than
        turning away one arrangement that would have worked.
        """
        frozen = frozen_seam_paths(self.seams)
        for path in writes:
            owner = self.owner_of(path)
            if owner:
                raise CharterError(
                    f"{path} is already written by ticket {owner!r}, and two owners of one "
                    "file is what makes concurrent workers unsafe. Give this ticket a "
                    f"different file, or — if {owner!r} should not have claimed it — call "
                    f"drop_worker({owner!r}) and add it back without that path."
                )
            seam = frozen.get(path)
            if seam is not None:
                raise CharterError(
                    f"{path} is a formal seam ({seam.what}), so no ticket may write it: you "
                    "wrote it into the skeleton and everyone is building against it. This "
                    "ticket may read it instead."
                )
            # A reader that came first. Same overlap as the case below, found
            # from the other side, because nothing says tickets arrive in an
            # order that puts writers before readers.
            readers = [
                other.id for other in self.workers
                if path in other.reads and worker_id not in other.needs
            ]
            if readers:
                raise CharterError(
                    f"{path} is read by ticket(s) {', '.join(sorted(readers))}, so this "
                    "ticket cannot change it underneath them. If it is the shared seam, "
                    f"leave it to the skeleton and drop it from this ticket's writes. If "
                    f"{sorted(readers)[0]!r} really must run after this ticket, drop it and "
                    f"add it back with needs = [{worker_id!r}]."
                )

    def _check_reads_are_still(
        self, worker_id: str, reads: tuple[str, ...], needs: tuple[str, ...]
    ) -> None:
        """A ticket may only read files that are not moving while it works.

        A declared ``needs`` answers this rather than excusing it: the writer
        has finished before this ticket starts, so the file has stopped
        changing — which is the condition the check exists to catch.
        """
        for path in reads:
            owner = self.owner_of(path)
            if owner and owner != worker_id and owner not in needs:
                raise CharterError(
                    f"this ticket reads {path}, which ticket {owner!r} writes — it would be "
                    "working against a moving target. Either leave that file to the "
                    f"skeleton so neither ticket writes it, or add needs = [{owner!r}] so "
                    f"this one starts after {owner!r} has finished."
                )
