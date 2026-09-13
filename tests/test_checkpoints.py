"""Tests for the undo snapshots taken before the agent changes a file."""
from __future__ import annotations

import os

from cobirb.checkpoints import Checkpoints


def _store(tmp_path):
    return Checkpoints(str(tmp_path), store=str(tmp_path / ".store"))


def test_an_edited_file_comes_back(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("original\n")
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))
    target.write_text("the agent's version\n")

    report = checkpoints.undo_last()

    assert target.read_text() == "original\n"
    assert "a.py" in report.restored


def test_a_created_file_is_removed_again(tmp_path):
    """Undoing a file that did not exist means it should not exist."""
    target = tmp_path / "new.py"
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))   # nothing there yet
    target.write_text("created by the agent\n")

    report = checkpoints.undo_last()

    assert not target.exists()
    assert "new.py" in report.deleted


def test_only_the_first_snapshot_of_a_file_in_a_turn_counts(tmp_path):
    """Undo restores the state at the *start* of the turn, so a second edit
    must not overwrite the copy taken before the first."""
    target = tmp_path / "a.py"
    target.write_text("v1\n")
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))
    target.write_text("v2\n")
    checkpoints.record(str(target))
    target.write_text("v3\n")

    checkpoints.undo_last()

    assert target.read_text() == "v1\n"


def test_undo_steps_back_one_turn_at_a_time(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("v1\n")
    checkpoints = _store(tmp_path)

    for version in ("v2\n", "v3\n"):
        checkpoints.begin_turn()
        checkpoints.record(str(target))
        target.write_text(version)

    checkpoints.undo_last()
    assert target.read_text() == "v2\n"
    checkpoints.undo_last()
    assert target.read_text() == "v1\n"


def test_turns_that_changed_nothing_are_skipped(tmp_path):
    """Most turns are questions, not edits. Undo should reach past them to
    the last thing that actually changed something."""
    target = tmp_path / "a.py"
    target.write_text("original\n")
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))
    target.write_text("edited\n")
    for _ in range(3):
        checkpoints.begin_turn()   # three turns of conversation

    checkpoints.undo_last()

    assert target.read_text() == "original\n"


def test_nothing_to_undo_says_so(tmp_path):
    checkpoints = _store(tmp_path)
    checkpoints.begin_turn()

    report = checkpoints.undo_last()

    assert report.nothing_to_undo
    assert "Nothing to undo" in report.describe()


def test_no_snapshot_directory_is_left_by_a_turn_that_changed_nothing(tmp_path):
    """A question shouldn't cost disk."""
    checkpoints = _store(tmp_path)
    checkpoints.begin_turn()

    assert not os.path.exists(str(tmp_path / ".store" / "0"))


def test_history_is_bounded(tmp_path):
    """Every kept turn is disk that is otherwise never reclaimed."""
    target = tmp_path / "a.py"
    target.write_text("v0\n")
    checkpoints = Checkpoints(str(tmp_path), store=str(tmp_path / ".store"), keep=3)

    for n in range(10):
        checkpoints.begin_turn()
        checkpoints.record(str(target))
        target.write_text(f"v{n + 1}\n")

    assert checkpoints.undoable_turns <= 3


def test_a_failed_restore_is_reported_rather_than_swallowed(tmp_path):
    """Telling someone their files are back when they aren't is the one
    outcome worse than not having undo at all."""
    target = tmp_path / "sub" / "a.py"
    target.parent.mkdir()
    target.write_text("original\n")
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))
    # Something outside CoBirb makes the path unrestorable.
    target.unlink()
    target.parent.rmdir()
    (tmp_path / "sub").write_text("now a file, not a directory")

    report = checkpoints.undo_last()

    assert report.failed
    assert "could not undo" in report.describe()


# --------------------------------------------------------------------------- #
# /diff — what the agent changed this session.
#
# Built from the snapshots rather than git, so it works in a directory that is
# not a repository and shows the agent's changes specifically rather than
# conflating them with whatever the user had already edited.
# --------------------------------------------------------------------------- #
def test_the_session_diff_shows_what_the_agent_changed(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def f():\n    return 1\n")
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))
    target.write_text("def f():\n    return 2\n")

    diff = checkpoints.session_diff()

    assert "-    return 1" in diff and "+    return 2" in diff
    assert "a.py" in diff


def test_the_diff_is_against_the_start_of_the_session_not_the_last_turn(tmp_path):
    """"What has this session done to my code" is asked against how things
    looked when it started."""
    target = tmp_path / "a.py"
    target.write_text("v1\n")
    checkpoints = _store(tmp_path)

    for version in ("v2\n", "v3\n"):
        checkpoints.begin_turn()
        checkpoints.record(str(target))
        target.write_text(version)

    diff = checkpoints.session_diff()

    assert "-v1" in diff and "+v3" in diff
    assert "v2" not in diff  # the intermediate state is not part of the answer


def test_a_file_the_agent_created_shows_as_wholly_added(tmp_path):
    checkpoints = _store(tmp_path)
    checkpoints.begin_turn()
    checkpoints.record(str(tmp_path / "new.py"))
    (tmp_path / "new.py").write_text("brand new\n")

    diff = checkpoints.session_diff()

    assert "+brand new" in diff


def test_a_file_edited_back_to_where_it_started_is_not_in_the_diff(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("original\n")
    checkpoints = _store(tmp_path)

    checkpoints.begin_turn()
    checkpoints.record(str(target))
    target.write_text("changed\n")
    target.write_text("original\n")

    assert checkpoints.session_diff().strip() == ""


def test_a_session_that_changed_nothing_has_an_empty_diff(tmp_path):
    checkpoints = _store(tmp_path)
    checkpoints.begin_turn()

    assert checkpoints.session_diff() == ""


def test_the_diff_works_without_git(tmp_path):
    """Deliberate: the checkpoint store is the source, so a directory that
    was never a repository still answers the question."""
    assert not (tmp_path / ".git").exists()
    target = tmp_path / "a.py"
    target.write_text("before\n")
    checkpoints = _store(tmp_path)
    checkpoints.begin_turn()
    checkpoints.record(str(target))
    target.write_text("after\n")

    assert "+after" in checkpoints.session_diff()
