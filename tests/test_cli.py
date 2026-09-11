"""Tests for the CLI wiring layer, in particular session plumbing.

These exist because ``cli.py``'s wiring is the one layer the rest of the test
suite doesn't exercise directly: a positional-argument mismatch between
``cli._build_orchestrator`` and ``SessionManager.load`` shipped and passed
every other test, because ``session.py`` was only ever tested by calling
``SessionManager.load`` correctly in isolation.
"""
from __future__ import annotations

import pytest

from cobirb.cli import _build_orchestrator, _load_persona
from cobirb.typing.spi import Persona


class _DummyModel:
    """A stub model that returns a fixed reply and never calls tools."""

    def __init__(self, reply="hello"):
        self.reply = reply

    def chat(self, *args, **kwargs):
        return self.reply

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return False


def test_build_orchestrator_creates_new_session_on_first_run(tmp_path):
    """A --session path that doesn't exist yet must be created, not loaded."""
    session_path = str(tmp_path / "new-session.json")
    persona = Persona(name="Noah")

    orchestrator, _, _ = _build_orchestrator(
        str(tmp_path), persona, {}, "system prompt", session_path, "pw"
    )

    assert orchestrator.session is not None
    assert orchestrator.session.session is not None
    assert orchestrator.session.session.turns == []


def test_build_orchestrator_loads_existing_session_with_correct_password(tmp_path):
    """The real password must reach SessionManager.load, not the cwd string."""
    session_path = str(tmp_path / "existing-session.json")
    persona = Persona(name="Noah")

    orchestrator, _, _ = _build_orchestrator(
        str(tmp_path), persona, {}, "system prompt", session_path, "correct-password"
    )
    orchestrator.session.session.add_text("user", "hello")
    orchestrator.session.save("correct-password")

    # Re-open with the same password: must decrypt and recover the turn.
    reopened, _, _ = _build_orchestrator(
        str(tmp_path), persona, {}, "system prompt", session_path, "correct-password"
    )
    assert [t.content for t in reopened.session.session.turns] == ["hello"]


def test_build_orchestrator_rejects_wrong_password_on_existing_session(tmp_path):
    session_path = str(tmp_path / "existing-session.json")
    persona = Persona(name="Noah")

    orchestrator, _, _ = _build_orchestrator(
        str(tmp_path), persona, {}, "system prompt", session_path, "correct-password"
    )
    orchestrator.session.save("correct-password")

    try:
        _build_orchestrator(str(tmp_path), persona, {}, "system prompt", session_path, "wrong-password")
    except Exception:
        pass
    else:
        raise AssertionError("expected decryption to fail with the wrong password")


def test_run_with_session_records_user_prompt(tmp_path):
    """Regression test: Orchestrator._open_session used to skip recording the
    user's prompt whenever a SessionManager was pre-injected (i.e. every
    --session run built via _build_orchestrator), so a resumed session's
    transcript silently lost the user's side of the conversation."""
    session_path = str(tmp_path / "session.json")
    persona = Persona(name="Noah")

    orchestrator, _, _ = _build_orchestrator(
        str(tmp_path), persona, {}, "system prompt", session_path, "pw"
    )
    orchestrator.model = _DummyModel(reply="hi there")

    session = orchestrator.run("hello noah", "system prompt", cwd=str(tmp_path))

    assert session.turns[0].role == "user"
    assert session.turns[0].content == "hello noah"
    assert session.turns[-1].role == "assistant"
    assert session.turns[-1].content == "hi there"


def test_run_twice_with_same_session_accumulates_history(tmp_path):
    """A second turn in the same (resumed) session must keep the first
    turn's history, not start over."""
    session_path = str(tmp_path / "session.json")
    persona = Persona(name="Noah")

    orchestrator, _, _ = _build_orchestrator(
        str(tmp_path), persona, {}, "system prompt", session_path, "pw"
    )
    orchestrator.model = _DummyModel(reply="first reply")
    orchestrator.run("first message", "system prompt", cwd=str(tmp_path))

    orchestrator.model = _DummyModel(reply="second reply")
    session = orchestrator.run("second message", "system prompt", cwd=str(tmp_path))

    contents = [t.content for t in session.turns]
    assert contents == ["first message", "first reply", "second message", "second reply"]


@pytest.mark.parametrize("name", ["professional", "neighbor", "kawaii"])
def test_bundled_personas_load(name):
    persona = _load_persona(name)
    assert persona.name  # every bundled persona must have a non-empty name
    assert persona.tone


def test_load_persona_defaults_to_noah():
    assert _load_persona(None).name == "Noah"
    assert _load_persona("noah").name == "Noah"


def test_load_persona_falls_back_to_noah_when_unknown(capsys):
    persona = _load_persona("does-not-exist")
    assert persona.name == "Noah"
    assert "unknown persona" in capsys.readouterr().err
