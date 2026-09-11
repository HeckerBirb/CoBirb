"""Tests for the encrypted session manager (tamper detection + round-trip)."""
from __future__ import annotations

import json

import pytest

from cobirb.plugins.core.crypto import HybridPQCSessionCrypto
from cobirb.session import SessionManager, Turn


@pytest.fixture
def manager(tmp_path):
    path = str(tmp_path / "session.json")
    return SessionManager.create(path, HybridPQCSessionCrypto(), persona="noah", password="pw")


def test_create_adds_opening_turn(manager):
    manager.session.add_text("user", "hello")
    assert len(manager.session.turns) == 1
    assert manager.session.turns[0].role == "user"


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
    blob = manager.save("pw")

    manager2 = SessionManager.load(manager.path, HybridPQCSessionCrypto(), "pw")
    assert [t.content for t in manager2.session.turns] == ["hello", "world"]
    # The hash is preserved for tamper detection.
    assert manager2.session.turns[0].hash == manager.session.turns[0].hash


def test_wrong_password_rejected(tmp_path):
    path = str(tmp_path / "session.json")
    m = SessionManager.create(path, HybridPQCSessionCrypto(), password="correct")
    m.session.add_text("user", "hello")
    m.save("correct")
    with pytest.raises(Exception):
        SessionManager.load(path, HybridPQCSessionCrypto(), "wrong")


def test_tamper_detected_on_load(tmp_path):
    path = str(tmp_path / "session.json")
    crypto = HybridPQCSessionCrypto()
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
    m = SessionManager(path, HybridPQCSessionCrypto(), password="pw")
    assert m.session is None
    assert m.save("pw") is None


def test_turn_digest_is_stable():
    t = Turn(role="user", content="hello")
    assert t.digest() == Turn(role="user", content="hello").digest()
