"""Tests for the charter — what Brainy Birb proposes and the user approves.

Two contracts matter here and the rest is parsing. A charter that reads must
produce scopes a person can check, and those scopes must become a policy that
actually denies everything else — because the isolation is the policy, not the
prose.
"""
from __future__ import annotations

import textwrap

import pytest

from cobirb.flock.charter import (
    CONFLICT_READ_WRITE,
    CONFLICT_WRITE_WRITE,
    DEFAULT_CONCURRENCY,
    CharterError,
    describe_conflicts,
    find_conflicts,
    parse_charter,
    policy_for,
    writes_owner,
)

_MINIMAL = """
objective = "Add CSV export"

[[workers]]
id     = "a"
writes = ["export/csv_writer.py"]
brief  = "Implement write_csv."
"""


def _charter(body: str):
    return parse_charter(textwrap.dedent(body))


# --------------------------------------------------------------------------- #
# Reading one
# --------------------------------------------------------------------------- #
def test_a_minimal_charter_reads():
    charter = _charter(_MINIMAL)

    assert charter.objective == "Add CSV export"
    assert len(charter.workers) == 1
    assert charter.workers[0].writes == ("export/csv_writer.py",)
    assert charter.concurrency == DEFAULT_CONCURRENCY


def test_scopes_briefs_and_seams_survive_the_round_trip():
    charter = _charter("""
        objective = "Add CSV export"
        concurrency = 3

        [[seams]]
        at   = "export/types.py"
        kind = "formal"
        what = "ExportSpec and Column."

        [[seams]]
        at   = "export/csv_writer.py::write_csv"
        kind = "loose"
        what = "A missing key writes an empty cell."

        [[workers]]
        id     = "a"
        writes = ["export/csv_writer.py", "tests/test_csv_writer.py"]
        reads  = ["export/types.py"]
        accept = "pytest tests/test_csv_writer.py -q"
        brief  = \"\"\"
        Implement write_csv. The tests are already there.
        \"\"\"
    """)

    worker = charter.workers[0]
    assert charter.concurrency == 3
    assert worker.reads == ("export/types.py",)
    assert worker.accept == "pytest tests/test_csv_writer.py -q"
    assert "Implement write_csv" in worker.brief
    assert [seam.kind for seam in charter.seams] == ["formal", "loose"]


def test_a_formal_seam_knows_the_type_system_holds_it_up():
    """The distinction is the point of recording seams at all: a loose
    agreement has nothing enforcing it and must be pinned by a test."""
    charter = _charter("""
        objective = "x"
        [[seams]]
        at = "a.py"
        kind = "loose"
        what = "Returns None for a missing key."
        [[workers]]
        id = "a"
        writes = ["a.py"]
        brief = "do it"
    """)

    seam = charter.seams[0]
    assert not seam.enforced_by_types
    assert "only by a test" in seam.describe()


def test_the_summary_names_every_scope_a_person_has_to_judge():
    """The approval prompt is the single human decision point in a run, so
    what it prints has to be enough to decide on."""
    text = _charter("""
        objective = "Add CSV export"
        [[seams]]
        at = "export/types.py"
        kind = "formal"
        what = "The shared vocabulary."
        [[workers]]
        id = "a"
        writes = ["export/csv_writer.py"]
        reads = ["export/types.py"]
        accept = "pytest -q"
        brief = "go"
    """).describe()

    assert "Add CSV export" in text
    assert "export/csv_writer.py" in text
    assert "export/types.py" in text
    assert "pytest -q" in text


# --------------------------------------------------------------------------- #
# Refusing one
# --------------------------------------------------------------------------- #
def test_malformed_toml_says_so_rather_than_raising_a_parser_error():
    with pytest.raises(CharterError, match="not valid TOML"):
        parse_charter("objective = ")


def test_a_charter_with_no_workers_is_not_a_charter():
    with pytest.raises(CharterError, match="at least one"):
        parse_charter('objective = "x"')


def test_a_worker_that_writes_nothing_is_rejected_by_name():
    """Almost always means the partition is wrong, and the message should send
    someone to the right line — this text was written by a model and is being
    fixed by a person."""
    with pytest.raises(CharterError, match="'a' writes nothing"):
        _charter("""
            objective = "x"
            [[workers]]
            id = "a"
            writes = []
            brief = "go"
        """)


def test_two_workers_cannot_share_an_id():
    with pytest.raises(CharterError, match="share the id"):
        _charter("""
            objective = "x"
            [[workers]]
            id = "a"
            writes = ["one.py"]
            brief = "go"
            [[workers]]
            id = "a"
            writes = ["two.py"]
            brief = "go"
        """)


def test_a_seam_must_say_which_kind_it_is():
    with pytest.raises(CharterError, match="formal or loose"):
        _charter("""
            objective = "x"
            [[seams]]
            at = "a.py"
            kind = "vibes"
            what = "something"
            [[workers]]
            id = "a"
            writes = ["a.py"]
            brief = "go"
        """)


def test_an_absurd_concurrency_reads_as_the_typo_it_is():
    with pytest.raises(CharterError, match="typo"):
        _charter("""
            objective = "x"
            concurrency = 500
            [[workers]]
            id = "a"
            writes = ["a.py"]
            brief = "go"
        """)


def test_a_missing_brief_is_named_rather_than_defaulted():
    """A worker with no brief would run against an empty prompt. Better to
    refuse the charter than to fan out something that cannot work."""
    with pytest.raises(CharterError, match="brief"):
        _charter("""
            objective = "x"
            [[workers]]
            id = "a"
            writes = ["a.py"]
        """)


# --------------------------------------------------------------------------- #
# The partition check
# --------------------------------------------------------------------------- #
def test_a_disjoint_partition_has_no_conflicts():
    charter = _charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["one.py"]
        reads = ["types.py"]
        brief = "go"
        [[workers]]
        id = "b"
        writes = ["two.py"]
        reads = ["types.py"]
        brief = "go"
    """)

    assert find_conflicts(charter) == []
    assert "disjoint" in describe_conflicts([])


def test_two_workers_writing_one_file_is_a_conflict():
    """Exclusive ownership is what makes concurrent workers safe without
    locking, and what makes 'which worker broke this' answerable."""
    charter = _charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["shared.py"]
        brief = "go"
        [[workers]]
        id = "b"
        writes = ["shared.py"]
        brief = "go"
    """)

    conflicts = find_conflicts(charter)

    assert [c.kind for c in conflicts] == [CONFLICT_WRITE_WRITE]
    assert conflicts[0].workers == ("a", "b")
    assert "shared.py" in describe_conflicts(conflicts)


def test_reading_a_file_another_worker_writes_is_a_conflict():
    """The frozen-seam rule: a worker reading what another writes is working
    against a moving target."""
    charter = _charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["a.py"]
        reads = ["b.py"]
        brief = "go"
        [[workers]]
        id = "b"
        writes = ["b.py"]
        brief = "go"
    """)

    conflicts = find_conflicts(charter)

    assert [c.kind for c in conflicts] == [CONFLICT_READ_WRITE]
    assert "moving target" in conflicts[0].describe()


def test_reading_a_file_nobody_writes_is_exactly_what_a_seam_is():
    charter = _charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["a.py"]
        reads = ["types.py"]
        brief = "go"
        [[workers]]
        id = "b"
        writes = ["b.py"]
        reads = ["types.py"]
        brief = "go"
    """)

    assert find_conflicts(charter) == []


def test_a_worker_reading_what_it_writes_is_not_a_conflict():
    """edit_file reads before it writes. Flagging that would be noise."""
    charter = _charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["a.py"]
        reads = ["a.py"]
        brief = "go"
    """)

    assert find_conflicts(charter) == []


def test_a_conflict_is_reported_not_refused():
    """The resolution varies with the situation in ways CoBirb cannot guess,
    so parsing succeeds and the user is asked."""
    charter = _charter("""
        objective = "x"
        [[workers]]
        id = "a"
        writes = ["shared.py"]
        brief = "go"
        [[workers]]
        id = "b"
        writes = ["shared.py"]
        brief = "go"
    """)

    assert charter.objective == "x"  # it parsed
    assert find_conflicts(charter)  # and the problem is available to report


# --------------------------------------------------------------------------- #
# Scopes become policy — the isolation itself
# --------------------------------------------------------------------------- #
def _worker_policy(tmp_path, **fields):
    charter = _charter(f"""
        objective = "x"
        [[workers]]
        id = "a"
        writes = {fields.get("writes", '["mine.py"]')}
        reads = {fields.get("reads", "[]")}
        brief = "go"
    """)
    return policy_for(charter.workers[0], str(tmp_path))


def test_a_worker_may_write_the_file_it_owns(tmp_path):
    policy = _worker_policy(tmp_path)

    assert policy.is_allowed("write_file", {"path": str(tmp_path / "mine.py")})
    assert policy.is_allowed("edit_file", {"path": "mine.py"})


def test_a_worker_may_not_write_anything_else(tmp_path):
    policy = _worker_policy(tmp_path)

    assert not policy.is_allowed("write_file", {"path": str(tmp_path / "theirs.py")})
    assert not policy.is_allowed("write_file", {"path": str(tmp_path / "mine.py.bak")})


def test_a_writable_file_is_also_readable(tmp_path):
    """edit_file reads before it writes; a worker that could change a file it
    could not open would be working blind."""
    policy = _worker_policy(tmp_path)

    assert policy.is_allowed("read_file", {"path": "mine.py"})


def test_a_seam_is_readable_but_never_writable(tmp_path):
    policy = _worker_policy(tmp_path, reads='["types.py"]')

    assert policy.is_allowed("read_file", {"path": "types.py"})
    assert not policy.is_allowed("write_file", {"path": "types.py"})


def test_a_worker_may_read_anything_in_the_project(tmp_path):
    """Writes are the isolation, not reads. A worker that cannot orient itself
    — cannot list, glob or read a sibling — flails against "permission denied"
    and gets no work done, which is what the first real run showed."""
    policy = _worker_policy(tmp_path)

    assert policy.is_allowed("read_file", {"path": "someone_elses_module.py"})
    assert policy.is_allowed("list_dir", {"path": "."})
    assert policy.is_allowed("glob", {"pattern": "**/*.py"})
    assert policy.is_allowed("grep", {"pattern": "def "})
    assert policy.is_allowed("repo_map", {})


def test_a_worker_cannot_read_outside_the_working_directory(tmp_path):
    """Project-wide read is not machine-wide read. The one boundary that stays
    is the working directory itself — a worker has no more business in your
    home directory than any other CoBirb run does."""
    policy = _worker_policy(tmp_path)

    assert not policy.is_allowed("read_file", {"path": "/etc/passwd"})
    assert not policy.is_allowed("read_file", {"path": "../outside.py"})


def test_a_worker_cannot_run_shell(tmp_path):
    """Its check is run for it by the verify loop, from a command the user
    approved in the charter. Nothing about the brief implies a shell."""
    policy = _worker_policy(tmp_path)

    assert not policy.is_allowed("shell", {"command": "pytest"})
    assert not policy.is_allowed("shell", {"command": "cat mine.py"})


def test_a_worker_starts_from_default_deny(tmp_path):
    """Whatever else changes, a fresh worker policy must permit nothing it was
    not handed — including tools that do not exist yet."""
    policy = _worker_policy(tmp_path)

    assert not policy.is_allowed("apply_patch", {"path": "elsewhere.py"})
    assert not policy.is_allowed("some_future_plugin_tool", {})


def test_a_path_escape_does_not_leave_the_granted_file(tmp_path):
    """`..` and symlinks are resolved before the comparison, so a path that
    names something outside the scope while looking inside it is still denied."""
    policy = _worker_policy(tmp_path)

    assert not policy.is_allowed("write_file", {"path": "../mine.py"})
    assert not policy.is_allowed("write_file", {"path": "sub/../../mine.py"})


# --------------------------------------------------------------------------- #
# Who owns a file — the one thing a worker may never be asked about.
# --------------------------------------------------------------------------- #
_TWO_OWNERS = """
objective = "two tickets"

[[workers]]
id     = "a"
writes = ["src/a.py"]
brief  = "Do a."

[[workers]]
id     = "b"
writes = ["src/b.py"]
brief  = "Do b."
"""


def test_a_file_another_worker_owns_names_that_worker():
    """What makes the refusal answerable without asking anyone: the charter
    already says who owns what."""
    charter = parse_charter(_TWO_OWNERS)

    assert writes_owner(charter, "src/b.py", besides="a") == "b"


def test_a_workers_own_file_is_not_somebody_elses():
    charter = parse_charter(_TWO_OWNERS)

    assert writes_owner(charter, "src/a.py", besides="a") == ""


def test_a_file_nobody_owns_is_nobody_elses():
    """Only cross-silo writes are refused outright. Everything else a worker
    wants is a question the user gets to answer."""
    charter = parse_charter(_TWO_OWNERS)

    assert writes_owner(charter, "src/new_helper.py", besides="a") == ""


def test_ownership_is_matched_the_way_the_charter_states_it():
    """Both sides come from the same normalisation, so a path written the
    long way round still finds its owner."""
    charter = parse_charter(_TWO_OWNERS)

    assert writes_owner(charter, "src/./b.py", besides="a") == "b"


def test_an_empty_path_owns_nothing():
    charter = parse_charter(_TWO_OWNERS)

    assert writes_owner(charter, "   ", besides="a") == ""
