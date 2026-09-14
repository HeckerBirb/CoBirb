"""Tests for the encrypted session manager (tamper detection + round-trip)."""
from __future__ import annotations

import json
import os

import pytest

from cobirb import session as session_module
from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto
from cobirb.session import Session, SessionManager, Turn, fork_session


@pytest.fixture
def manager(tmp_path):
    path = str(tmp_path / "session.json")
    return SessionManager.create(path, AesGcmScryptSessionCrypto(), persona="noah", password="pw")


def test_create_adds_opening_turn(manager):
    manager.session.add_text("user", "hello")
    assert len(manager.session.turns) == 1
    assert manager.session.turns[0].role == "user"


def test_create_records_the_persona_and_working_dir_on_the_session(tmp_path):
    """Regression test: SessionManager.create stored ``persona``/``working_dir``
    on the manager but never passed them into the ``Session`` object it
    builds, so every created session's saved JSON silently recorded "noah"
    and "." regardless of what was actually passed — caught by the TUI's
    Sessions tab, which resumes a session under the persona it names."""
    path = str(tmp_path / "s.json")
    manager = SessionManager.create(path, AesGcmScryptSessionCrypto(), str(tmp_path), "professional", "pw")

    assert manager.session.persona == "professional"
    assert manager.session.working_dir == str(tmp_path)


def test_save_writes_encrypted_not_plaintext(manager):
    manager.session.add_text("user", "hello")
    blob = manager.save("pw")
    assert blob is not None
    # The file must be the opaque encrypted blob, not the JSON.
    with open(manager.path, "r", encoding="utf-8") as fh:
        on_disk = fh.read()
    assert "hello" not in on_disk
    assert '{"role"' not in on_disk


def test_save_load_round_trip(manager):
    manager.session.add_text("user", "hello")
    manager.session.add_text("assistant", "world")
    manager.save("pw")

    manager2 = SessionManager.load(manager.path, AesGcmScryptSessionCrypto(), "pw")
    assert [t.content for t in manager2.session.turns] == ["hello", "world"]
    # The hash is preserved for tamper detection.
    assert manager2.session.turns[0].hash == manager.session.turns[0].hash


def test_wrong_password_rejected(tmp_path):
    path = str(tmp_path / "session.json")
    m = SessionManager.create(path, AesGcmScryptSessionCrypto(), password="correct")
    m.session.add_text("user", "hello")
    m.save("correct")
    with pytest.raises(Exception):
        SessionManager.load(path, AesGcmScryptSessionCrypto(), "wrong")


def test_tamper_detected_on_load(tmp_path):
    path = str(tmp_path / "session.json")
    crypto = AesGcmScryptSessionCrypto()
    m = SessionManager.create(path, crypto, password="pw")
    turn = Turn(role="user", content="hello")
    m.session.add(turn)
    m.save("pw")

    # Simulate tampering: edit the plaintext content without updating its
    # stored hash, then re-encrypt and overwrite the file on disk.
    data = m.session.to_dict()
    data["turns"][0]["content"] = "TAMPERED"
    blob = crypto.encrypt(json.dumps(data), "pw")
    with open(path, "wb") as fh:
        fh.write(blob)

    with pytest.raises(ValueError, match="tampered"):
        SessionManager.load(path, crypto, "pw")


def test_save_returns_none_when_no_session(tmp_path):
    path = str(tmp_path / "session.json")
    m = SessionManager(path, AesGcmScryptSessionCrypto(), password="pw")
    assert m.session is None
    assert m.save("pw") is None


def test_turn_digest_is_stable():
    t = Turn(role="user", content="hello")
    assert t.digest() == Turn(role="user", content="hello").digest()


def test_tamper_detected_when_only_tool_use_is_rewritten(tmp_path):
    """Regression: the turn digest hashed role+content only, so a session
    whose recorded ``read_file`` call was rewritten into a ``shell`` one
    loaded clean — the tool history is exactly the part worth forging."""
    path = str(tmp_path / "session.json")
    crypto = AesGcmScryptSessionCrypto()
    m = SessionManager.create(path, crypto, password="pw")
    m.session.add(Turn(role="tool", content="ok", tool_use=[{"name": "read_file", "arguments": {"path": "safe.txt"}}]))
    m.save("pw")

    data = json.loads(crypto.decrypt(open(path, "rb").read(), "pw"))
    data["turns"][0]["tool_use"] = [{"name": "shell", "arguments": {"command": "rm -rf /"}}]
    with open(path, "wb") as fh:
        fh.write(crypto.encrypt(json.dumps(data), "pw"))

    with pytest.raises(ValueError, match="tampered"):
        SessionManager.load(path, crypto, "pw")


def test_digest_is_stable_across_equivalent_tool_use_orderings():
    """The digest is serialized with sorted keys, so an unrelated key order
    change doesn't read as tampering."""
    a = Turn(role="tool", content="x", tool_use=[{"name": "read_file", "arguments": {"path": "a", "encoding": "utf-8"}}])
    b = Turn(role="tool", content="x", tool_use=[{"arguments": {"encoding": "utf-8", "path": "a"}, "name": "read_file"}])
    assert a.digest() == b.digest()


def test_session_from_dict_defaults_created_at_when_missing():
    """Every other field defaults; created_at used to deserialize to None
    and then get written back out as null."""
    session = Session.from_dict({"turns": []})
    assert session.created_at
    assert session.to_dict()["created_at"]


def test_manager_does_not_retain_the_password():
    """The unlock password is passed per call to save()/load(); keeping a
    copy on the manager would hold it in memory for no reason."""
    manager = SessionManager("x", AesGcmScryptSessionCrypto(), password="s3cret")
    assert "s3cret" not in repr(vars(manager))


# --------------------------------------------------------------------------- #
# Plan mode: Turn.phase and Session.validation (Orchestrator.run(plan_mode=True)).
# --------------------------------------------------------------------------- #
def test_turn_phase_round_trips_through_to_dict_and_from_dict():
    turn = Turn(role="assistant", content="step 1, step 2", phase="plan")
    restored = Turn.from_dict(turn.to_dict())
    assert restored.phase == "plan"


def test_turn_phase_defaults_to_none_for_a_normal_turn():
    turn = Turn(role="user", content="hi")
    assert turn.phase is None
    assert Turn.from_dict(turn.to_dict()).phase is None


def test_turn_phase_defaults_to_none_when_absent_from_an_old_session_file():
    """A session file written before plan mode existed has no "phase" key
    at all — it must load as None, not raise a KeyError."""
    restored = Turn.from_dict({"role": "user", "content": "hi"})
    assert restored.phase is None


def test_relabeling_a_turns_phase_is_detected_as_tampering(tmp_path):
    """The same rewrite risk that applies to tool_use
    applies to phase: relabeling a "plan" turn as "validate" after the
    fact must not load clean."""
    path = str(tmp_path / "session.json")
    crypto = AesGcmScryptSessionCrypto()
    m = SessionManager.create(path, crypto, password="pw")
    m.session.add(Turn(role="assistant", content="looks fine", phase="plan"))
    m.save("pw")

    data = json.loads(crypto.decrypt(open(path, "rb").read(), "pw"))
    data["turns"][0]["phase"] = "validate"
    with open(path, "wb") as fh:
        fh.write(crypto.encrypt(json.dumps(data), "pw"))

    with pytest.raises(ValueError, match="tampered"):
        SessionManager.load(path, crypto, "pw")


def test_session_validation_round_trips():
    session = Session(validation="Confirmed: the file was edited as intended.")
    restored = Session.from_dict(session.to_dict())
    assert restored.validation == "Confirmed: the file was edited as intended."


def test_session_validation_defaults_to_none():
    assert Session.from_dict({"turns": []}).validation is None


# --------------------------------------------------------------------------- #
# The Sessions tab's directory conventions (default_sessions_dir,
# discover_sessions) — used to list/resume/create sessions without needing
# --session <path> up front. See cobirb.tui.panes.SessionsPane.
# --------------------------------------------------------------------------- #
def test_default_sessions_dir_uses_cobirb_home(monkeypatch, tmp_path):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    assert session_module.default_sessions_dir() == str(tmp_path / ".cobirb" / "sessions")


def test_discover_sessions_returns_empty_for_a_missing_directory(tmp_path):
    assert session_module.discover_sessions(str(tmp_path / "does-not-exist")) == []


def test_discover_sessions_lists_json_files_only(tmp_path):
    (tmp_path / "one.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("not a session")
    (tmp_path / "subdir").mkdir()

    files = session_module.discover_sessions(str(tmp_path))

    assert [f.name for f in files] == ["one.json"]


def test_discover_sessions_orders_most_recently_modified_first(tmp_path):
    import os as _os
    import time as _time

    older = tmp_path / "older.json"
    newer = tmp_path / "newer.json"
    older.write_text("{}")
    newer.write_text("{}")
    now = _time.time()
    _os.utime(older, (now - 100, now - 100))
    _os.utime(newer, (now, now))

    files = session_module.discover_sessions(str(tmp_path))

    assert [f.name for f in files] == ["newer.json", "older.json"]


def test_discover_sessions_reports_size_and_path(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("hello")

    [entry] = session_module.discover_sessions(str(tmp_path))

    assert entry.path == str(path)
    assert entry.size == len("hello")


def test_a_session_without_a_persona_reloads_without_one(tmp_path):
    """Personas are opt-in, so a session file that records none must come
    back with none. This defaulted to "noah" from before that changed, which
    put a costume on a model the user had never asked to dress up."""
    session = Session.from_dict({"turns": []})

    assert session.persona == "none"


def test_session_files_are_written_readable_only_by_their_owner(tmp_path):
    """Encrypted, so this is defence in depth — but a session landing
    world-readable at the default umask on a shared machine is a needless
    invitation, and creating it with the mode costs nothing."""
    import stat

    path = str(tmp_path / "s.json")
    manager = SessionManager.create(path, AesGcmScryptSessionCrypto(), password="pw")
    manager.session.add_text("user", "hello")
    manager.save("pw")

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --------------------------------------------------------------------------- #
# fork_session() — session branching (v0.6.0): trying a different direction
# from a point already on disk, without disturbing what got you there.
# --------------------------------------------------------------------------- #
def _seeded_manager(tmp_path, name="session.json"):
    path = str(tmp_path / name)
    manager = SessionManager.create(path, AesGcmScryptSessionCrypto(), str(tmp_path), "noah", "pw")
    for i in range(4):
        manager.session.add_text("user" if i % 2 == 0 else "assistant", f"turn {i}")
    manager.save("pw")
    return manager, path


def test_fork_session_copies_every_turn_by_default(tmp_path):
    _, path = _seeded_manager(tmp_path)

    branch = fork_session(path, AesGcmScryptSessionCrypto(), "pw")

    assert [t.content for t in branch.session.turns] == [f"turn {i}" for i in range(4)]
    assert branch.session.forked_from == f"{path}@turn3"


def test_fork_session_writes_a_new_file_and_leaves_the_source_untouched(tmp_path):
    original_manager, path = _seeded_manager(tmp_path)
    original_bytes = open(path, "rb").read()

    branch = fork_session(path, AesGcmScryptSessionCrypto(), "pw")

    assert branch.path != path
    assert os.path.exists(branch.path)
    assert open(path, "rb").read() == original_bytes  # the source file is bit-for-bit unchanged

    # And the source is still independently loadable at its original length.
    reloaded = SessionManager.load(path, AesGcmScryptSessionCrypto(), "pw")
    assert len(reloaded.session.turns) == 4


def test_fork_session_up_to_turn_keeps_only_a_prefix(tmp_path):
    _, path = _seeded_manager(tmp_path)

    branch = fork_session(path, AesGcmScryptSessionCrypto(), "pw", up_to_turn=1)

    assert [t.content for t in branch.session.turns] == ["turn 0", "turn 1"]
    assert branch.session.forked_from == f"{path}@turn1"


def test_fork_session_rejects_an_out_of_range_turn(tmp_path):
    _, path = _seeded_manager(tmp_path)

    with pytest.raises(ValueError, match="out of range"):
        fork_session(path, AesGcmScryptSessionCrypto(), "pw", up_to_turn=99)


def test_fork_session_refuses_to_overwrite_an_existing_destination(tmp_path):
    _, path = _seeded_manager(tmp_path)
    destination = str(tmp_path / "already-here.json")
    open(destination, "w").close()

    with pytest.raises(ValueError, match="already exists"):
        fork_session(path, AesGcmScryptSessionCrypto(), "pw", out_path=destination)


def test_fork_session_honours_an_explicit_destination(tmp_path):
    _, path = _seeded_manager(tmp_path)
    destination = str(tmp_path / "my-branch.json")

    branch = fork_session(path, AesGcmScryptSessionCrypto(), "pw", out_path=destination)

    assert branch.path == destination


def test_forking_a_branch_does_not_collide_with_the_original_branch_file(tmp_path):
    """Two branches of the same session, taken back to back, must land in
    two different files — a filename collision here would silently discard
    one branch's turns underneath the other's."""
    _, path = _seeded_manager(tmp_path)

    first = fork_session(path, AesGcmScryptSessionCrypto(), "pw")
    second = fork_session(path, AesGcmScryptSessionCrypto(), "pw")

    assert first.path != second.path
    assert os.path.exists(first.path) and os.path.exists(second.path)


def test_fork_session_wrong_password_is_rejected(tmp_path):
    _, path = _seeded_manager(tmp_path)

    with pytest.raises(Exception):  # noqa: B017 - the crypto library's own tamper/auth error
        fork_session(path, AesGcmScryptSessionCrypto(), "wrong-password")
