"""Tests for where a flock lives in a session.

The contract is a pair of tokens: one in the main session where the
conversation branched, one at the head of the flock's own file. Between them
they make the handoff and the rejoin both findable later.
"""
from __future__ import annotations

import os


from cobirb.flock import branch
from cobirb.plugins.core import AesGcmScryptSessionCrypto
from cobirb.session import Session, SessionManager


def _main_session(tmp_path, password="hunter2"):
    path = str(tmp_path / "session.json")
    manager = SessionManager.create(
        path, AesGcmScryptSessionCrypto(), str(tmp_path), "none", password
    )
    manager.session.add_text("user", "let's build the exporter")
    return manager


def test_engaging_marks_the_main_session_with_a_token(tmp_path):
    session = Session()

    token = branch.engage(session, "add CSV export")

    marker = session.turns[-1]
    assert marker.role == branch.ROLE_ENGAGED
    assert marker.tool_use[0]["arguments"]["token"] == token
    assert "add CSV export" in marker.content


def test_the_objective_is_readable_without_opening_the_other_file(tmp_path):
    """The main transcript should still read as a conversation: here is where
    we handed this off, and what for."""
    session = Session()

    branch.engage(session, "add CSV export")

    assert "add CSV export" in session.turns[-1].content


def test_rejoining_records_where_the_conversation_came_back(tmp_path):
    session = Session()
    token = branch.engage(session, "x")

    branch.rejoin(session, token, "Two of three tickets landed.")

    assert session.turns[-1].role == branch.ROLE_RETURNED
    assert session.turns[-1].tool_use[0]["arguments"]["token"] == token
    assert "Two of three" in session.turns[-1].content


def test_the_flock_file_sits_beside_the_main_one(tmp_path):
    """A flock session is a session; putting it elsewhere would mean the
    Sessions tab lists half a history."""
    path = branch.flock_path(str(tmp_path / "session-20260913.json"), "abcd1234-ef56")

    assert os.path.dirname(path) == str(tmp_path)
    assert "flock" in os.path.basename(path)


def test_the_flock_file_carries_the_same_token_at_its_head(tmp_path):
    main = _main_session(tmp_path)
    token = branch.engage(main.session, "add CSV export")

    flock = branch.open_flock_session(main, token, "add CSV export", "hunter2")

    assert flock.session.flock == token
    assert token in flock.session.turns[0].content


def test_the_token_survives_a_round_trip_through_encryption(tmp_path):
    """The pairing is only useful if it is still there when you come back to
    it months later."""
    main = _main_session(tmp_path)
    token = branch.engage(main.session, "x")
    flock = branch.open_flock_session(main, token, "x", "hunter2")
    flock.save("hunter2")

    reopened = SessionManager.load(flock.path, AesGcmScryptSessionCrypto(), "hunter2", str(tmp_path))

    assert reopened.session.flock == token


def test_an_unsaved_session_gets_no_flock_file(tmp_path):
    """An ephemeral session promised to leave no trace. Traceability is worth
    a lot; it is not worth breaking that."""
    assert branch.open_flock_session(None, "abc", "x", None) is None


def test_a_second_round_continues_the_same_flock_session(tmp_path):
    """One engagement is one flock session. A flock that took three rounds
    should read as one continuous piece of work, not three files."""
    main = _main_session(tmp_path)
    token = branch.engage(main.session, "x")
    flock = branch.open_flock_session(main, token, "x", "hunter2")

    flock.session.add_text("a", "round one")
    flock.session.add_text("a", "round two")

    assert flock.session.flock == token
    assert len(flock.session.turns) == 3  # the head, plus both rounds


def test_engaging_twice_mints_two_different_flocks(tmp_path):
    session = Session()

    first = branch.engage(session, "one thing")
    second = branch.engage(session, "another thing")

    assert first != second


def test_the_branches_can_be_listed_back(tmp_path):
    session = Session()
    token = branch.engage(session, "x")
    branch.rejoin(session, token, "done")

    text = branch.describe_branches(session)

    assert token[:8] in text
    assert "engaged" in text and "returned" in text


def test_a_session_with_no_flock_says_so(tmp_path):
    assert "No flock" in branch.describe_branches(Session())
