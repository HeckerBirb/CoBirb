"""Tests for the incremental plan builder.

One contract carries the module: **an overlapping partition must be
unbuildable**. Everything else here is about whether a refusal tells the model
enough to fix the move it just made, since that is the whole reason for
accumulating a plan rather than validating a document.
"""
from __future__ import annotations

import pytest

from cobirb.flock.charter import CharterError, find_conflicts
from cobirb.flock.plan import PlanDraft


def _draft_with(*tickets) -> PlanDraft:
    draft = PlanDraft()
    for ticket_id, writes in tickets:
        draft.add_worker(ticket_id, brief="go", writes=list(writes))
    return draft


# --------------------------------------------------------------------------- #
# The invariant: an overlap cannot be constructed
# --------------------------------------------------------------------------- #
def test_a_sealed_plan_is_disjoint():
    draft = _draft_with(("a", ["a.py"]), ("b", ["b.py"]))

    charter = draft.seal("do two things")

    assert find_conflicts(charter) == []
    assert [w.id for w in charter.workers] == ["a", "b"]


def test_claiming_a_file_another_ticket_owns_is_refused_with_the_owner_named():
    draft = _draft_with(("a", ["shared.py"]))

    with pytest.raises(CharterError, match="already written by ticket 'a'") as raised:
        draft.add_worker("b", brief="go", writes=["shared.py"])

    assert "drop_worker('a')" in str(raised.value)
    assert draft.worker("b") is None  # nothing partially applied


def test_one_ticket_may_build_against_a_stub_another_implements():
    """The ordinary plan: `store` fills in Store's bodies while `cli` builds
    against its signatures. In either order."""
    for first, second in ((("store", ["store.py"], []), ("cli", ["cli.py"], ["store.py"])),
                          (("cli", ["cli.py"], ["store.py"]), ("store", ["store.py"], []))):
        draft = PlanDraft()
        for ticket, writes, reads in (first, second):
            draft.add_worker(ticket, brief="go", writes=writes, reads=reads)

        charter = draft.seal("x")

        assert charter.worker("store").writes == ("store.py",)
        assert find_conflicts(charter) == []


def test_a_declared_dependency_answers_the_read_of_a_written_file():
    """It has stopped changing by the time the reader starts, which is the
    condition the check exists to catch."""
    draft = _draft_with(("a", ["types.py"]))

    draft.add_worker("b", brief="go", writes=["b.py"], reads=["types.py"], needs=["a"])

    assert find_conflicts(draft.seal("x")) == []


def test_a_ticket_reading_what_it_writes_itself_is_fine():
    draft = PlanDraft()

    draft.add_worker("a", brief="go", writes=["a.py"], reads=["a.py"])

    assert draft.seal("x").worker("a").reads == ("a.py",)


# --------------------------------------------------------------------------- #
# Seams
# --------------------------------------------------------------------------- #
def test_a_formal_seam_locks_nothing():
    """A seam describes where tickets meet; the stub file it lives in is still
    the implementing ticket's to write, before or after it is declared."""
    draft = PlanDraft()
    draft.declare_seam("store.py", "formal", "Store's public methods")
    draft.add_worker("store", brief="go", writes=["store.py"])
    draft.declare_seam("cli.py::run", "formal", "the command entry point")
    draft.add_worker("cli", brief="go", writes=["cli.py"], reads=["store.py"])

    assert len(draft.seal("x").seams) == 2


def test_a_loose_seam_may_sit_in_a_file_a_ticket_writes():
    draft = PlanDraft()
    draft.declare_seam("reader.py", "loose", "returns None for a missing key")

    draft.add_worker("a", brief="go", writes=["reader.py"])

    assert draft.seal("x").worker("a") is not None


def test_a_seam_needs_to_say_what_it_is():
    draft = PlanDraft()

    with pytest.raises(CharterError, match="what"):
        draft.declare_seam("types.py", "formal", "")


def test_the_same_seam_is_not_declared_twice():
    draft = PlanDraft()
    draft.declare_seam("types.py", "formal", "the shared vocabulary")

    with pytest.raises(CharterError, match="already declared"):
        draft.declare_seam("types.py", "formal", "again")


# --------------------------------------------------------------------------- #
# Backing out
# --------------------------------------------------------------------------- #
def test_dropping_a_ticket_frees_its_files():
    draft = _draft_with(("a", ["shared.py"]))

    draft.drop_worker("a")
    draft.add_worker("b", brief="go", writes=["shared.py"])

    assert draft.owner_of("shared.py") == "b"


def test_a_ticket_others_wait_on_cannot_be_dropped_silently():
    draft = _draft_with(("a", ["a.py"]))
    draft.add_worker("b", brief="go", writes=["b.py"], needs=["a"])

    with pytest.raises(CharterError, match="declare needs on it"):
        draft.drop_worker("a")


def test_dropping_a_ticket_that_is_not_there_lists_the_ones_that_are():
    draft = _draft_with(("a", ["a.py"]))

    with pytest.raises(CharterError, match="a"):
        draft.drop_worker("nope")


def test_a_duplicate_id_says_how_to_replace_it():
    draft = _draft_with(("a", ["a.py"]))

    with pytest.raises(CharterError, match="drop_worker"):
        draft.add_worker("a", brief="go", writes=["other.py"])


# --------------------------------------------------------------------------- #
# Sealing
# --------------------------------------------------------------------------- #
def test_sealing_an_empty_plan_says_not_dividing_is_a_legitimate_answer():
    with pytest.raises(CharterError, match="legitimate answer"):
        PlanDraft().seal("x")


def test_a_charter_needs_an_objective():
    draft = _draft_with(("a", ["a.py"]))

    with pytest.raises(CharterError, match="objective"):
        draft.seal("")


def test_an_unknown_dependency_is_caught_at_the_seal_not_the_move():
    """A plan built in the order the work occurred to somebody names a
    dependency before adding it, and refusing that would impose an ordering
    rule for no gain."""
    draft = PlanDraft()
    draft.add_worker("b", brief="go", writes=["b.py"], needs=["a"])  # accepted

    with pytest.raises(CharterError, match="not in this charter"):
        draft.seal("x")


def test_a_cycle_is_caught_at_the_seal_and_named():
    draft = PlanDraft()
    draft.add_worker("a", brief="go", writes=["a.py"], needs=["b"])
    draft.add_worker("b", brief="go", writes=["b.py"], needs=["a"])

    with pytest.raises(CharterError, match="circle"):
        draft.seal("x")


def test_the_seams_and_concurrency_reach_the_charter():
    draft = PlanDraft()
    draft.declare_seam("types.py", "formal", "the shared vocabulary")
    draft.add_worker(
        "a", brief="go", writes=["a.py", "test_a.py"], tests=["test_a.py"],
        reads=["types.py"], accept="pytest test_a.py",
    )

    charter = draft.seal("add the thing", concurrency=4)

    assert charter.concurrency == 4
    assert charter.seams[0].at == "types.py"
    assert charter.worker("a").tests == ("test_a.py",)
    assert charter.worker("a").accept == "pytest test_a.py"


# --------------------------------------------------------------------------- #
# What a move says back
# --------------------------------------------------------------------------- #
def test_a_verdict_says_where_the_plan_now_stands():
    """A model calling four tools in sequence has no other way to know how much
    of its own plan has landed, and one that has lost count re-adds a ticket."""
    draft = PlanDraft()

    verdict = draft.add_worker("a", brief="go", writes=["a.py"], accept="pytest")

    assert "'a'" in verdict
    assert "1 ticket(s)" in verdict


def test_a_ticket_with_no_acceptance_check_is_told_it_can_never_be_complete():
    draft = PlanDraft()

    verdict = draft.add_worker("a", brief="go", writes=["a.py"])

    assert "never be reported complete" in verdict


def test_a_test_file_missing_from_writes_is_adopted_rather_than_refused():
    """There is one thing it can mean — a worker's tests are its own files — so
    refusing it asked the model to re-send a whole ticket to move one string."""
    draft = PlanDraft()

    verdict = draft.add_worker("a", brief="go", writes=["a.py"], tests=["test_a.py"])

    assert draft.worker("a").writes == ("a.py", "test_a.py")
    assert "test_a.py" in verdict


def test_an_adopted_test_file_is_still_refused_when_someone_else_owns_it():
    """Adoption is not an exemption: the adopted paths go through the ownership
    check with the rest, or a ticket could claim a neighbour's file by calling
    it a test."""
    draft = PlanDraft()
    draft.add_worker("a", brief="go", writes=["a.py", "test_shared.py"])

    with pytest.raises(CharterError, match="already written by ticket 'a'"):
        draft.add_worker("b", brief="go", writes=["b.py"], tests=["test_shared.py"])


def test_a_ticket_that_writes_nothing_is_refused():
    draft = PlanDraft()

    with pytest.raises(CharterError, match="writes nothing"):
        draft.add_worker("a", brief="go", writes=[])


def test_a_ticket_needs_a_brief():
    draft = PlanDraft()

    with pytest.raises(CharterError, match="brief"):
        draft.add_worker("a", brief="", writes=["a.py"])
