"""Tests for the CLI wiring layer, in particular session plumbing.

These exist because ``cli.py``'s wiring is the one layer the rest of the test
suite doesn't exercise directly: a positional-argument mismatch between
``cli._build_orchestrator`` and ``SessionManager.load`` shipped and passed
every other test, because ``session.py`` was only ever tested by calling
``SessionManager.load`` correctly in isolation.
"""
from __future__ import annotations

import pytest

from cobirb import cli
from cobirb.cli import _available_personas, _build_orchestrator, _load_persona
from cobirb.typing.spi import Persona


def _scripted_input(responses):
    """Return a fake ``input()`` that yields ``responses`` in order, then
    raises EOFError (like a closed stdin/Ctrl-D) once exhausted."""
    it = iter(responses)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError()

    return fake_input


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


def test_available_personas_includes_bundled_and_noah():
    names = _available_personas()
    assert names == sorted({"noah", "professional", "neighbor", "kawaii"})


class _StubSession:
    summary = "ok"
    turns = ()


class _StubOrchestrator:
    session = None
    last_turn_streamed = False

    def run(self, prompt, system, *, cwd, persona, session_path=None):
        return _StubSession()


def test_interactive_persona_list_command(monkeypatch, capsys):
    """`/persona` with no argument lists the available personas and never
    reaches the model (no orchestrator should be built)."""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for /persona")

    monkeypatch.setattr(cli, "_build_orchestrator", fail_if_called)
    monkeypatch.setattr("builtins.input", _scripted_input(["/persona"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    cli._run_interactive(persona, system, {}, None, None, "/tmp")

    out = capsys.readouterr().out
    assert "Available personas:" in out
    for name in ("noah", "professional", "neighbor", "kawaii"):
        assert name in out


def test_interactive_persona_switch_prints_confirmation(monkeypatch, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator should not be built for /persona")

    monkeypatch.setattr(cli, "_build_orchestrator", fail_if_called)
    monkeypatch.setattr("builtins.input", _scripted_input(["/persona professional"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    cli._run_interactive(persona, system, {}, None, None, "/tmp")

    out = capsys.readouterr().out
    assert "Professional:" in out


def test_interactive_persona_switch_affects_subsequent_turns(monkeypatch):
    """Switching persona mid-conversation must change the system prompt (and
    therefore the persona name) used for every turn afterward."""
    captured = []

    def fake_build_orchestrator(cwd, persona, allow_overrides, system, session_path, password, model_name=None):
        captured.append((persona.name, system))
        return _StubOrchestrator(), None, None

    monkeypatch.setattr(cli, "_build_orchestrator", fake_build_orchestrator)
    monkeypatch.setattr("builtins.input", _scripted_input(["/persona professional", "hello", "n"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    cli._run_interactive(persona, system, {}, None, None, "/tmp")

    # /persona itself never builds an orchestrator; only the "hello" turn does.
    assert len(captured) == 1
    used_name, used_system = captured[0]
    assert used_name == "Professional"
    assert "Professional" in used_system


def test_interactive_exits_gracefully_on_eof_at_continue_prompt(monkeypatch, capsys):
    """Regression test: closed stdin right at the "continue?" prompt used to
    propagate an unhandled EOFError instead of exiting like Ctrl-D does
    everywhere else in the loop."""
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (_StubOrchestrator(), None, None))
    # No third scripted response for "continue?" -> the fake input raises EOFError there.
    monkeypatch.setattr("builtins.input", _scripted_input(["hello"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    result = cli._run_interactive(persona, system, {}, None, None, "/tmp")

    assert result == 0
    assert "goodnight" in capsys.readouterr().out
