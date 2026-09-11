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
from cobirb.cli import _available_personas, _build_orchestrator, _load_persona, _parse_allow_tools
from cobirb.typing.spi import Persona


def test_parse_allow_tools_bare_names():
    assert _parse_allow_tools("read_file,write_file") == {"read_file": "", "write_file": ""}


def test_parse_allow_tools_narrows_shell_scope_by_argument():
    """Regression test: entry.partition("(") leaves the closing paren
    attached to the argument ("shell(git)" -> arg "git)", not "git"),
    which silently broke every documented --allow-tool='shell(git)'-style
    example (README, cli.py's own --help text) — the parsed scope never
    matched a real command's first word, so it never actually allowed
    anything despite looking like it should."""
    assert _parse_allow_tools("shell(git)") == {"shell": "git"}
    assert _parse_allow_tools("shell(python -m pytest)") == {"shell": "python -m pytest"}


def test_parse_allow_tools_mixed_bare_and_scoped_entries():
    assert _parse_allow_tools("shell(git),read_file,shell(ls)") == {"shell": "ls", "read_file": ""}


def test_parse_allow_tools_ignores_blank_entries():
    assert _parse_allow_tools("read_file,,write_file,") == {"read_file": "", "write_file": ""}


def test_parse_allow_tools_empty_spec():
    assert _parse_allow_tools("") == {}


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


def test_build_orchestrator_applies_allow_tool_overrides_to_the_policy(tmp_path):
    """The --allow-tool CLI flag has to actually reach the orchestrator's
    live Policy, not just get parsed — this is the glue between
    _parse_allow_tools() and Policy.allow() that nothing else exercises."""
    persona = Persona(name="Noah")
    # A tool the default policy would not otherwise permit.
    allow_overrides = {"shell": "curl"}

    orchestrator, _, _ = _build_orchestrator(str(tmp_path), persona, allow_overrides, "system prompt")

    assert orchestrator.policy.is_allowed("shell", {"command": "curl --version"})
    assert not orchestrator.policy.is_allowed("shell", {"command": "rm -rf /"})


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


class _StubSessionManager:
    def __init__(self):
        self.saved_with = []

    def save(self, password):
        self.saved_with.append(password)


class _StubOrchestrator:
    last_turn_streamed = False

    def __init__(self, run_result=None, run_raises=None, with_session_manager=False):
        self.session = _StubSessionManager() if with_session_manager else None
        self._run_result = run_result if run_result is not None else _StubSession()
        self._run_raises = run_raises

    def run(self, prompt, system, *, cwd, persona, session_path=None):
        if self._run_raises is not None:
            raise self._run_raises
        return self._run_result


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


def test_run_interactive_builds_orchestrator_once_and_reuses_it(monkeypatch):
    """Regression test: _build_orchestrator used to be called fresh on
    every turn, discarding its Policy object each time — so an "always
    allow this" approval from one turn would silently stop applying on the
    next. It must now be built once (lazily, on the first real turn) and
    reused for the rest of the interactive session."""
    build_calls = []

    def fake_build_orchestrator(cwd, persona, allow_overrides, system, session_path, password, model_name=None):
        build_calls.append(1)
        return _StubOrchestrator(), None, None

    monkeypatch.setattr(cli, "_build_orchestrator", fake_build_orchestrator)
    monkeypatch.setattr("builtins.input", _scripted_input(["first", "y", "second", "n"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    cli._run_interactive(persona, system, {}, None, None, "/tmp")

    assert len(build_calls) == 1


# --------------------------------------------------------------------------- #
# main(argv): the actual entry point every invocation goes through. Routes
# to _run_one_shot/_run_interactive, which are patched out here so these
# tests check *argument parsing and routing* (main's actual job) without
# needing a real model — that behavior is already covered by the
# _run_one_shot/_run_interactive tests above and the live integration tests.
# --------------------------------------------------------------------------- #
def test_main_help_prints_help_and_exits_zero(capsys):
    assert cli.main(["help"]) == 0
    assert "CoBirb" in capsys.readouterr().out


def test_main_one_shot_mode_routes_prompt_and_options(monkeypatch):
    captured = {}

    def fake_run_one_shot(prompt, persona, system, allow_overrides, session_path, password, cwd, model_name=None):
        captured.update(
            prompt=prompt,
            allow_overrides=allow_overrides,
            session_path=session_path,
            password=password,
            cwd=cwd,
            model_name=model_name,
        )
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    result = cli.main(["-p", "do the thing", "--cwd", "/tmp", "--model", "llama3.1", "--allow-tool", "shell(git)"])

    assert result == 0
    assert captured["prompt"] == "do the thing"
    assert captured["cwd"] == "/tmp"
    assert captured["model_name"] == "llama3.1"
    assert captured["allow_overrides"] == {"shell": "git"}
    assert captured["session_path"] is None
    assert captured["password"] is None


def test_main_defaults_to_interactive_mode_without_a_prompt(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_run_interactive", lambda *a, **k: called.append(1) or 0)

    assert cli.main(["--cwd", "/tmp"]) == 0
    assert called


def test_main_persona_flag_selects_the_requested_persona(monkeypatch):
    captured = {}

    def fake_run_one_shot(prompt, persona, system, allow_overrides, session_path, password, cwd, model_name=None):
        captured["persona_name"] = persona.name
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi", "--persona", "professional"])

    assert captured["persona_name"] == "Professional"


def test_main_defaults_cwd_to_current_directory(monkeypatch):
    captured = {}

    def fake_run_one_shot(prompt, persona, system, allow_overrides, session_path, password, cwd, model_name=None):
        captured["cwd"] = cwd
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi"])

    assert captured["cwd"] == "."


def test_main_one_shot_without_session_never_prompts_for_a_password(monkeypatch):
    def fail_if_called():
        raise AssertionError("must not prompt for a password with no --session and no --password")

    monkeypatch.setattr(cli, "_read_password", fail_if_called)
    monkeypatch.setattr(cli, "_run_one_shot", lambda *a, **k: 0)

    cli.main(["-p", "hi"])  # must not raise


def test_main_one_shot_with_session_prompts_for_a_password(monkeypatch, tmp_path):
    """Regression test: one-shot mode used to only prompt for a password
    when --password was *also* given, so `cobirb -p ... --session foo.json`
    (a documented, reasonable invocation) would run the whole task and then
    crash with an unhandled AttributeError trying to encrypt with a None
    password when saving. A session path must always mean a password is
    requested, matching interactive mode's existing behavior below."""
    monkeypatch.setattr(cli, "_read_password", lambda: "typed-secretly")
    captured = {}

    def fake_run_one_shot(prompt, persona, system, allow_overrides, session_path, password, cwd, model_name=None):
        captured["password"] = password
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi", "--session", str(tmp_path / "s.json")])

    assert captured["password"] == "typed-secretly"


def test_main_interactive_with_session_prompts_for_a_password(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_read_password", lambda: "typed-secretly")
    captured = {}

    def fake_run_interactive(persona, system, allow_overrides, session_path, password, cwd, model_name=None):
        captured["password"] = password
        return 0

    monkeypatch.setattr(cli, "_run_interactive", fake_run_interactive)
    cli.main(["--session", str(tmp_path / "s.json")])

    assert captured["password"] == "typed-secretly"


# --------------------------------------------------------------------------- #
# _run_one_shot / _run_interactive's own bodies: error handling and session
# persistence. Everything above patches these functions OUT (to test main's
# routing in isolation); these tests patch _build_orchestrator instead, so
# _run_one_shot/_run_interactive's actual logic runs for real.
# --------------------------------------------------------------------------- #
def test_run_one_shot_reports_the_final_answer(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (_StubOrchestrator(), None, None))
    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)

    result = cli._run_one_shot("do something", persona, system, {}, None, None, "/tmp")

    assert result == 0
    out = capsys.readouterr().out
    assert "do something" in out
    assert "ok" in out  # the stub session's summary


def test_run_one_shot_returns_1_and_reports_a_permission_error(monkeypatch, capsys):
    orchestrator = _StubOrchestrator(run_raises=cli.PermissionError("nope"))
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)

    result = cli._run_one_shot("do something", persona, system, {}, None, None, "/tmp")

    assert result == 1
    assert "blocked" in capsys.readouterr().out


def test_run_one_shot_returns_1_and_reports_unexpected_errors_without_crashing(monkeypatch, capsys):
    """A provider/tool exception must produce a clean error message and
    exit code, not an unhandled traceback."""
    orchestrator = _StubOrchestrator(run_raises=RuntimeError("model exploded"))
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)

    result = cli._run_one_shot("do something", persona, system, {}, None, None, "/tmp")

    assert result == 1
    out = capsys.readouterr().out
    assert "could not complete" in out
    assert "model exploded" in out


def test_run_one_shot_saves_the_session_when_a_session_path_was_given(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)

    cli._run_one_shot("do something", persona, system, {}, "/tmp/s.json", "pw", "/tmp")

    assert orchestrator.session.saved_with == ["pw"]


def test_run_one_shot_does_not_save_when_no_session_path_was_given(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)

    cli._run_one_shot("do something", persona, system, {}, None, None, "/tmp")

    assert orchestrator.session.saved_with == []


def test_run_interactive_reports_a_permission_error_and_keeps_going(monkeypatch, capsys):
    orchestrator = _StubOrchestrator(run_raises=cli.PermissionError("nope"))
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    monkeypatch.setattr("builtins.input", _scripted_input(["hello"]))  # then EOF -> exits cleanly

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    result = cli._run_interactive(persona, system, {}, None, None, "/tmp")

    assert result == 0
    assert "blocked" in capsys.readouterr().out


def test_run_interactive_reports_unexpected_errors_and_keeps_going(monkeypatch, capsys):
    orchestrator = _StubOrchestrator(run_raises=RuntimeError("boom"))
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    monkeypatch.setattr("builtins.input", _scripted_input(["hello"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    result = cli._run_interactive(persona, system, {}, None, None, "/tmp")

    assert result == 0
    assert "could not complete" in capsys.readouterr().out


def test_run_interactive_saves_the_session_after_each_successful_turn(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: (orchestrator, None, None))
    monkeypatch.setattr("builtins.input", _scripted_input(["first", "y", "second", "n"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    cli._run_interactive(persona, system, {}, "/tmp/s.json", "pw", "/tmp")

    assert orchestrator.session.saved_with == ["pw", "pw"]


def test_run_interactive_skips_blank_input_without_building_an_orchestrator(monkeypatch):
    build_calls = []

    def fake_build_orchestrator(*a, **k):
        build_calls.append(1)
        return _StubOrchestrator(), None, None

    monkeypatch.setattr(cli, "_build_orchestrator", fake_build_orchestrator)
    monkeypatch.setattr("builtins.input", _scripted_input(["", "  ", "real prompt", "n"]))

    persona = cli._load_persona(None)
    system = cli._build_system_prompt(persona)
    cli._run_interactive(persona, system, {}, None, None, "/tmp")

    assert len(build_calls) == 1  # only the real prompt triggered a build
