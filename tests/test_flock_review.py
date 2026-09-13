"""Tests for the review — the "verify" half of trust-but-verify.

The reliable pass is stub reversion, so most of these exercise it against a
real test file running under a real interpreter: a check that only pretended to
run broken code would be worth nothing.
"""
from __future__ import annotations

import sys
import textwrap

from cobirb.flock.charter import parse_charter
from cobirb.flock.review import (
    FINDING_ASSERTION_REMOVED,
    FINDING_DEFINITION_REMOVED,
    FINDING_FILE_EMPTIED,
    FINDING_SKIP_ADDED,
    Baseline,
    Mutant,
    put_the_stub_back,
    read_the_diff,
    review_worker,
)

_STUB = "def double(n):\n    raise NotImplementedError\n"
_REAL = "def double(n):\n    return n * 2\n"

_TESTS = """\
from work import double

def test_it_doubles():
    assert double(2) == 4
"""

_VACUOUS_TESTS = """\
import work

def test_it_exists():
    assert work is not None
"""


def _charter(tmp_path, tests_field='tests = ["test_work.py"]'):
    accept = f'"{sys.executable}" -m pytest test_work.py -q'
    return parse_charter(textwrap.dedent(f"""
        objective = "double things"

        [[workers]]
        id     = "a"
        writes = ["work.py", "test_work.py"]
        {tests_field}
        accept = '{accept}'
        brief  = "Implement double()."
    """))


def _skeleton(tmp_path, tests=_TESTS):
    """Write the skeleton, then capture it — the order a real run uses."""
    (tmp_path / "work.py").write_text(_STUB)
    (tmp_path / "test_work.py").write_text(tests)
    charter = _charter(tmp_path)
    return charter, Baseline.capture(charter, str(tmp_path))


# --------------------------------------------------------------------------- #
# The baseline
# --------------------------------------------------------------------------- #
def test_the_baseline_is_the_skeleton_as_brainy_birb_wrote_it(tmp_path):
    _, baseline = _skeleton(tmp_path)

    assert baseline.content("work.py") == _STUB


def test_a_file_the_skeleton_never_created_is_recorded_as_empty(tmp_path):
    """"The skeleton did not create this" is a real state, and it is what a
    stub reversion should restore such a file to."""
    charter = _charter(tmp_path)
    baseline = Baseline.capture(charter, str(tmp_path))

    assert baseline.content("work.py") == ""


# --------------------------------------------------------------------------- #
# Pass one — read the diff
# --------------------------------------------------------------------------- #
def test_an_untouched_skeleton_has_nothing_to_flag(tmp_path):
    charter, baseline = _skeleton(tmp_path)

    assert read_the_diff(charter.workers[0], baseline, str(tmp_path)) == []


def test_implementing_a_stub_is_not_suspicious(tmp_path):
    """The overwhelmingly common case. A review that flagged ordinary work
    would be ignored within a day."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)

    assert read_the_diff(charter.workers[0], baseline, str(tmp_path)) == []


def test_adding_a_test_is_not_suspicious(tmp_path):
    """Workers are supposed to do this — it is why they can write tests."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "test_work.py").write_text(
        _TESTS + "\ndef test_it_handles_zero():\n    assert double(0) == 0\n"
    )

    assert read_the_diff(charter.workers[0], baseline, str(tmp_path)) == []


def test_a_deleted_assertion_is_flagged(tmp_path):
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "test_work.py").write_text("from work import double\n\ndef test_it_doubles():\n    pass\n")

    kinds = [f.kind for f in read_the_diff(charter.workers[0], baseline, str(tmp_path))]

    assert FINDING_ASSERTION_REMOVED in kinds


def test_a_test_turned_off_is_flagged(tmp_path):
    """The quietest way to make red go green."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "test_work.py").write_text(
        "import pytest\nfrom work import double\n\n"
        "@pytest.mark.skip\ndef test_it_doubles():\n    assert double(2) == 4\n"
    )

    kinds = [f.kind for f in read_the_diff(charter.workers[0], baseline, str(tmp_path))]

    assert FINDING_SKIP_ADDED in kinds


def test_a_changed_signature_is_flagged(tmp_path):
    """The one action that breaks colleagues a worker cannot see."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text("def double(n, base=2):\n    return n * base\n")

    findings = read_the_diff(charter.workers[0], baseline, str(tmp_path))

    assert FINDING_DEFINITION_REMOVED in [f.kind for f in findings]
    assert "def double(n)" in findings[0].describe()


def test_emptying_a_file_the_skeleton_wrote_is_flagged(tmp_path):
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "test_work.py").write_text("\n")

    kinds = [f.kind for f in read_the_diff(charter.workers[0], baseline, str(tmp_path))]

    assert FINDING_FILE_EMPTIED in kinds


# --------------------------------------------------------------------------- #
# Pass two — put the stub back
# --------------------------------------------------------------------------- #
def test_real_tests_go_red_when_the_implementation_is_removed(tmp_path):
    """The check working as intended: the worker did the job, and its tests
    genuinely depend on the job being done."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)

    check = put_the_stub_back(charter.workers[0], baseline, str(tmp_path))

    assert check.caught
    assert "caught" in check.describe()


def test_vacuous_tests_survive_the_stub_going_back(tmp_path):
    """The cheat this pass exists to catch. These tests pass whether or not
    anything was implemented, so they are testing nothing."""
    charter, baseline = _skeleton(tmp_path, tests=_VACUOUS_TESTS)
    (tmp_path / "work.py").write_text(_REAL)

    check = put_the_stub_back(charter.workers[0], baseline, str(tmp_path))

    assert not check.caught
    assert "SURVIVED" in check.describe()


def test_the_workers_files_are_put_back_afterwards(tmp_path):
    """Restoring is part of the operation, not a safety net bolted on. A
    review that left the stub in place would undo the work it was checking."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)

    put_the_stub_back(charter.workers[0], baseline, str(tmp_path))

    assert (tmp_path / "work.py").read_text() == _REAL


def test_the_workers_own_tests_are_kept_during_the_reversion(tmp_path):
    """Restoring the tests too would run the skeleton's failing tests against
    the skeleton's stubs, which proves nothing about what the worker added."""
    charter, baseline = _skeleton(tmp_path, tests=_VACUOUS_TESTS)
    added = _VACUOUS_TESTS + "\ndef test_it_doubles():\n    assert work.double(2) == 4\n"
    (tmp_path / "work.py").write_text(_REAL)
    (tmp_path / "test_work.py").write_text(added)

    check = put_the_stub_back(charter.workers[0], baseline, str(tmp_path))

    # The worker's *added* test is what makes this go red.
    assert check.caught
    assert (tmp_path / "test_work.py").read_text() == added


def test_a_worker_that_writes_only_tests_says_so_rather_than_passing(tmp_path):
    """"We could not check" must never read the same as "we checked and it was
    fine"."""
    charter = parse_charter(textwrap.dedent("""
        objective = "x"
        [[workers]]
        id     = "a"
        writes = ["test_only.py"]
        tests  = ["test_only.py"]
        accept = "true"
        brief  = "write tests"
    """))
    baseline = Baseline.capture(charter, str(tmp_path))

    check = put_the_stub_back(charter.workers[0], baseline, str(tmp_path))

    assert not check.caught
    assert "no implementation" in check.error


def test_a_worker_with_no_acceptance_check_cannot_be_checked_this_way(tmp_path):
    charter = parse_charter(textwrap.dedent("""
        objective = "x"
        [[workers]]
        id     = "a"
        writes = ["work.py"]
        brief  = "go"
    """))
    baseline = Baseline.capture(charter, str(tmp_path))

    check = put_the_stub_back(charter.workers[0], baseline, str(tmp_path))

    assert not check.caught
    assert "no acceptance check" in check.error


# --------------------------------------------------------------------------- #
# Pass three — mutate the stated behaviours
# --------------------------------------------------------------------------- #
def test_a_mutant_that_the_tests_catch_is_reported_as_caught(tmp_path):
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)

    review = review_worker(
        charter.workers[0], baseline, str(tmp_path),
        mutants=[Mutant("doubles its argument", "work.py", "def double(n):\n    return n * 3\n")],
    )

    assert review.mutants[0].caught


def test_a_surviving_mutant_names_the_promise_nothing_tests(tmp_path):
    """Often the skeleton's fault rather than the worker's — it stated a
    behaviour and never wrote an acceptance test for it."""
    charter, baseline = _skeleton(tmp_path, tests=_VACUOUS_TESTS)
    (tmp_path / "work.py").write_text(_REAL)

    review = review_worker(
        charter.workers[0], baseline, str(tmp_path),
        mutants=[Mutant("doubles its argument", "work.py", "def double(n):\n    return n * 3\n")],
    )

    assert not review.mutants[0].caught
    assert "doubles its argument" in review.mutants[0].describe()


def test_a_mutant_is_put_back_afterwards(tmp_path):
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)

    review_worker(
        charter.workers[0], baseline, str(tmp_path),
        mutants=[Mutant("doubles", "work.py", "def double(n):\n    return n * 3\n")],
    )

    assert (tmp_path / "work.py").read_text() == _REAL


def test_a_mutant_cannot_reach_outside_the_workers_own_files(tmp_path):
    """Brainy Birb writes these, and a mutation that edited somebody else's
    work to test this one would be corrupting the partition."""
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)
    (tmp_path / "elsewhere.py").write_text("untouched\n")

    review = review_worker(
        charter.workers[0], baseline, str(tmp_path),
        mutants=[Mutant("something", "elsewhere.py", "broken")],
    )

    assert "not this worker's to mutate" in review.mutants[0].error
    assert (tmp_path / "elsewhere.py").read_text() == "untouched\n"


# --------------------------------------------------------------------------- #
# The whole review
# --------------------------------------------------------------------------- #
def test_honest_work_reviews_clean(tmp_path):
    charter, baseline = _skeleton(tmp_path)
    (tmp_path / "work.py").write_text(_REAL)

    review = review_worker(charter.workers[0], baseline, str(tmp_path))

    assert review.clean
    assert "nothing to flag" in review.describe()


def test_a_vacuous_suite_does_not_review_clean(tmp_path):
    charter, baseline = _skeleton(tmp_path, tests=_VACUOUS_TESTS)
    (tmp_path / "work.py").write_text(_REAL)

    review = review_worker(charter.workers[0], baseline, str(tmp_path))

    assert not review.clean


def test_an_uncheckable_worker_does_not_review_clean(tmp_path):
    """A check that could not run is not a check that passed."""
    charter = parse_charter(textwrap.dedent("""
        objective = "x"
        [[workers]]
        id     = "a"
        writes = ["work.py"]
        brief  = "go"
    """))
    baseline = Baseline.capture(charter, str(tmp_path))

    assert not review_worker(charter.workers[0], baseline, str(tmp_path)).clean
