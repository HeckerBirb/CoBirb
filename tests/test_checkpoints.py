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


# --------------------------------------------------------------------------- #
# Whole-tree checkpoints: shell changes included, repository or not
# --------------------------------------------------------------------------- #
import shutil as _shutil
import subprocess as _subprocess

import pytest as _pytest

from cobirb.checkpoints import TreeCheckpoints, for_workspace

_needs_git = _pytest.mark.skipif(_shutil.which("git") is None, reason="git not installed")


def _tree(tmp_path):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    return project, TreeCheckpoints(str(project), _shutil.which("git"), store=str(tmp_path / "store"))


@_needs_git
def test_a_file_a_command_deleted_comes_back(tmp_path):
    """The old gap: shell could not declare what it wrote, so undo never saw it."""
    project, cp = _tree(tmp_path)
    (project / "keep.py").write_text("x = 1\n")
    cp.begin_turn()
    (project / "keep.py").unlink()          # what `rm keep.py` would do
    (project / "made.txt").write_text("new")
    cp.end_turn()

    report = cp.undo_last()

    assert (project / "keep.py").read_text() == "x = 1\n"
    assert not (project / "made.txt").exists()
    assert "keep.py" in report.restored and "made.txt" in report.deleted


@_needs_git
def test_undo_leaves_a_file_you_changed_since_alone(tmp_path):
    project, cp = _tree(tmp_path)
    (project / "a.py").write_text("v1\n")
    cp.begin_turn()
    (project / "a.py").write_text("v2 by the agent\n")
    cp.end_turn()
    (project / "a.py").write_text("v3 by you\n")

    report = cp.undo_last()

    assert (project / "a.py").read_text() == "v3 by you\n"
    assert "a.py" in report.failed


@_needs_git
def test_a_repository_project_is_never_touched_and_its_ignores_hold(tmp_path):
    project, cp = _tree(tmp_path)
    git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t"]
    _subprocess.run([*git, "init", "-q"], check=True)
    (project / ".gitignore").write_text("secret.env\n")
    (project / "app.py").write_text("print(1)\n")
    _subprocess.run([*git, "add", "-A"], check=True)
    _subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    head = _subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    (project / "secret.env").write_text("TOKEN=abc")

    cp.begin_turn()
    (project / "app.py").write_text("print(2)\n")
    cp.end_turn()
    diff = cp.session_diff()
    cp.undo_last()

    assert "print(2)" in diff and "TOKEN" not in diff
    assert (project / "app.py").read_text() == "print(1)\n"
    status = _subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True).stdout
    assert status == ""  # the project's own index and HEAD are exactly as they were
    assert _subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head


@_needs_git
def test_the_store_does_not_outlive_the_session(tmp_path):
    project, cp = _tree(tmp_path)
    cp.begin_turn()
    cp.end_turn()

    cp.close()

    assert not (tmp_path / "store").exists()


@_needs_git
def test_a_turn_that_changed_nothing_is_not_an_undo(tmp_path):
    project, cp = _tree(tmp_path)
    (project / "a").write_text("1")
    cp.begin_turn()
    cp.end_turn()

    assert cp.undo_last().nothing_to_undo


def test_without_git_the_per_file_snapshots_are_used(tmp_path, monkeypatch):
    monkeypatch.setattr("cobirb.checkpoints.shutil.which", lambda name: None)

    assert not isinstance(for_workspace(str(tmp_path)), TreeCheckpoints)


@_needs_git
def test_a_store_left_by_a_dead_process_is_swept(tmp_path, monkeypatch):
    from cobirb import paths

    parent = os.path.join(paths.cobirb_dir(), "checkpoints")
    os.makedirs(os.path.join(parent, "proj-abc-tree-999999"))
    TreeCheckpoints(str(tmp_path), _shutil.which("git"))

    assert not os.path.exists(os.path.join(parent, "proj-abc-tree-999999"))
