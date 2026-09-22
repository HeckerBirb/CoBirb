"""Tests for the CLI wiring layer, in particular session plumbing.

These exist because ``cli.py``'s wiring is the one layer the rest of the test
suite doesn't exercise directly: a positional-argument mismatch between
``wiring.build_orchestrator`` and ``SessionManager.load`` shipped and passed
every other test, because ``session.py`` was only ever tested by calling
``SessionManager.load`` correctly in isolation.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from conftest import DummyModel, StubSession, StubSessionManager, write_config

from cobirb import cli, help_text, session
from cobirb.runtime import commands, plugins, sessions, system_prompt, wiring
from cobirb.runtime.system_prompt import build_system_prompt
from cobirb.cli import _resolve_plan_mode
from cobirb.runtime.wiring import parse_allow_tools
from cobirb.runtime.plugins import merge_tool_plugins, select_plugin
from cobirb.runtime.wiring import build_orchestrator
from cobirb.config import Config
from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto
from cobirb.plugins.core.io import TerminalIO
from cobirb.plugins.core.tools import ToolRegistry
from cobirb.session import SessionManager
from cobirb.typing import spi as cobirb_typing
from cobirb.typing.spi import Tool, ToolCall, ToolResult


def test_parse_allow_tools_bare_names():
    assert parse_allow_tools("read_file,write_file") == {"read_file": "", "write_file": ""}


def test_parse_allow_tools_narrows_shell_scope_by_argument():
    """entry.partition("(") leaves the closing paren attached to the
    argument ("shell(git)" -> arg "git)", not "git"), which silently breaks
    every documented --allow-tool='shell(git)'-style
    example (README, cli.py's own --help text) — the parsed scope never
    matched a real command's first word, so it never actually allowed
    anything despite looking like it should."""
    assert parse_allow_tools("shell(git)") == {"shell": "git"}
    assert parse_allow_tools("shell(python -m pytest)") == {"shell": "python -m pytest"}


def test_parse_allow_tools_mixed_bare_and_scoped_entries():
    assert parse_allow_tools("shell(git),read_file,shell(ls)") == {"shell": "ls", "read_file": ""}


def test_parse_allow_tools_ignores_blank_entries():
    assert parse_allow_tools("read_file,,write_file,") == {"read_file": "", "write_file": ""}


def test_parse_allow_tools_empty_spec():
    assert parse_allow_tools("") == {}
    assert parse_allow_tools(None) == {}


def test_parse_allow_tools_accepts_a_list():
    """--allow-tool is repeatable and the "allow_tools" config key is a list;
    both land here, and an entry may still carry several comma-separated
    rules of its own."""
    assert parse_allow_tools(["read_file", "shell(git)"]) == {"read_file": "", "shell": "git"}
    assert parse_allow_tools(["read_file,list_dir"]) == {"read_file": "", "list_dir": ""}


def test_config_allow_tools_permits_a_tool_without_a_prompt(tmp_path):
    """The user's own standing rules are the escape hatch from a policy that
    otherwise permits nothing — a tool named in config must run unprompted."""
    # Sandbox off: this is about the allow rules, and a contained command
    # running without asking is a different grant (see test_sandbox.py).
    write_config(tmp_path, json.loads('{"allow_tools": ["read_file", "shell(git)"], "sandbox": "off"}'))
    orchestrator = wiring.build_orchestrator(str(tmp_path), {})

    assert orchestrator.policy.is_allowed("read_file", {"path": "anything.txt"})
    assert orchestrator.policy.is_allowed("shell", {"command": "git status"})
    # ...and nothing else came along for the ride.
    assert not orchestrator.policy.is_allowed("write_file", {"path": "anything.txt"})
    assert not orchestrator.policy.is_allowed("shell", {"command": "rm -rf /"})


def test_resolve_plan_mode_defaults_off(tmp_path):
    assert _resolve_plan_mode(None, Config()) is False


def test_resolve_plan_mode_reads_the_config_key(tmp_path):
    write_config(tmp_path, json.loads('{"plan_mode": true}'))
    assert _resolve_plan_mode(None, Config()) is True


def test_resolve_plan_mode_cli_flag_overrides_config(tmp_path):
    write_config(tmp_path, json.loads('{"plan_mode": true}'))
    config = Config()
    assert _resolve_plan_mode("off", config) is False
    assert _resolve_plan_mode("on", config) is True


def test_build_orchestrator_creates_new_session_on_first_run(tmp_path):
    """A --session path that doesn't exist yet must be created, not loaded."""
    session_path = str(tmp_path / "new-session.json")

    orchestrator = wiring.build_orchestrator(
        str(tmp_path), {}, session_path, "pw"
    )

    assert orchestrator.session is not None
    assert orchestrator.session.session is not None
    assert orchestrator.session.session.turns == []


def test_build_orchestrator_applies_allow_tool_overrides_to_the_policy(tmp_path):
    """The --allow-tool CLI flag has to actually reach the orchestrator's
    live Policy, not just get parsed — this is the glue between
    parse_allow_tools() and Policy.allow() that nothing else exercises."""
    # A tool the default policy would not otherwise permit. Sandbox off: this
    # is about --allow-tool, not about contained commands running unasked.
    write_config(tmp_path, {"sandbox": "off"})
    allow_overrides = {"shell": "curl"}

    orchestrator = wiring.build_orchestrator(str(tmp_path), allow_overrides)

    assert orchestrator.policy.is_allowed("shell", {"command": "curl --version"})
    assert not orchestrator.policy.is_allowed("shell", {"command": "rm -rf /"})


def test_build_orchestrator_audit_log_is_off_by_default(tmp_path):
    """The audit log must stay off unless "audit_log": true is set. Writing
    unconditionally would keep a second, unencrypted, plaintext copy of every
    write_file/edit_file/apply_patch/shell call's full arguments — directly at
    odds with sessions being encrypted at rest."""
    orchestrator = wiring.build_orchestrator(str(tmp_path), {})
    assert orchestrator.policy.audit.enabled is False


def test_build_orchestrator_audit_log_can_be_turned_on_via_config(tmp_path):
    write_config(tmp_path, json.loads('{"audit_log": true}'))
    orchestrator = wiring.build_orchestrator(str(tmp_path), {})
    assert orchestrator.policy.audit.enabled is True


def test_build_orchestrator_loads_existing_session_with_correct_password(tmp_path):
    """The real password must reach SessionManager.load, not the cwd string."""
    session_path = str(tmp_path / "existing-session.json")

    orchestrator = wiring.build_orchestrator(
        str(tmp_path), {}, session_path, "correct-password"
    )
    orchestrator.session.session.add_text("user", "hello")
    orchestrator.session.save("correct-password")

    # Re-open with the same password: must decrypt and recover the turn.
    reopened = wiring.build_orchestrator(
        str(tmp_path), {}, session_path, "correct-password"
    )
    assert [t.content for t in reopened.session.session.turns] == ["hello"]


def test_build_orchestrator_rejects_wrong_password_on_existing_session(tmp_path):
    session_path = str(tmp_path / "existing-session.json")

    orchestrator = wiring.build_orchestrator(
        str(tmp_path), {}, session_path, "correct-password"
    )
    orchestrator.session.save("correct-password")

    try:
        wiring.build_orchestrator(str(tmp_path), {}, session_path, "wrong-password")
    except Exception:
        pass
    else:
        raise AssertionError("expected decryption to fail with the wrong password")


def test_run_with_session_records_user_prompt(tmp_path):
    """Orchestrator._open_session records the user's prompt even when a
    SessionManager was pre-injected (i.e. every --session run). Skipping it
    there loses the user's side of a resumed session's transcript."""
    session_path = str(tmp_path / "session.json")

    orchestrator = wiring.build_orchestrator(
        str(tmp_path), {}, session_path, "pw"
    )
    orchestrator.model = DummyModel(reply="hi there")

    session = orchestrator.run("hello noah", "system prompt", cwd=str(tmp_path))

    assert session.turns[0].role == "user"
    assert session.turns[0].content == "hello noah"
    assert session.turns[-1].role == "assistant"
    assert session.turns[-1].content == "hi there"


def test_run_twice_with_same_session_accumulates_history(tmp_path):
    """A second turn in the same (resumed) session must keep the first
    turn's history, not start over."""
    session_path = str(tmp_path / "session.json")

    orchestrator = wiring.build_orchestrator(
        str(tmp_path), {}, session_path, "pw"
    )
    orchestrator.model = DummyModel(reply="first reply")
    orchestrator.run("first message", "system prompt", cwd=str(tmp_path))

    orchestrator.model = DummyModel(reply="second reply")
    session = orchestrator.run("second message", "system prompt", cwd=str(tmp_path))

    contents = [t.content for t in session.turns]
    assert contents == ["first message", "first reply", "second message", "second reply"]


class _StubOrchestrator:
    last_turn_streamed = False
    last_run_tool_calls: list = []
    last_compaction = None

    def __init__(self, run_result=None, run_raises=None, with_session_manager=False):
        self.session = StubSessionManager() if with_session_manager else None
        self._run_result = run_result if run_result is not None else StubSession()
        self._run_raises = run_raises

    def close(self):
        """Part of the Orchestrator contract since MCP servers became
        child processes it owns. A no-op here; the stub starts nothing."""
        self.closed = True

    def run(self, prompt, system, *, cwd, label=None, session_path=None, plan_mode=False):
        if self._run_raises is not None:
            raise self._run_raises
        return self._run_result


# --------------------------------------------------------------------------- #
# Plan mode, end-to-end: every test above either stubs the whole Orchestrator
# (_StubOrchestrator) or builds one directly with hand-rolled parts — none of
# them actually exercises cli.main() -> _run_one_shot -> the REAL
# _build_orchestrator (real Config, ToolRegistry, Policy, TerminalIO,
# SessionManager) with plan_mode threaded all the way down to a real
# Orchestrator.run() call. These do — only the network-touching model
# provider is swapped for a scripted fake, so a wiring mistake anywhere in
# that chain (the --plan-mode flag never reaching run(), the real policy
# blocking a call the default should allow, phase tags never reaching a
# saved session) would show up here even if it slipped past the more
# targeted tests above.
# --------------------------------------------------------------------------- #
class _ScriptedThreePhaseModel:
    """Drives a real Orchestrator through plan -> act (one real tool call)
    -> validate, without touching the network. Mirrors the shape of
    test_integration_ollama.py's live tests, but deterministic."""

    def __init__(self, tool_name: str, tool_arguments: dict):
        self._step = 0
        self._tool_call = ToolCall(name=tool_name, arguments=tool_arguments)
        self._tool_call_emitted = False

    def name(self):
        return "scripted"

    def chat(self, system, context, tools=None, *, stream=False):
        self._step += 1
        return {
            1: "1. Read note.txt. 2. Report the secret word.",
            2: "Let me check the file.",
            3: "The secret word is banana.",
        }.get(self._step, "Confirmed: note.txt was read and the secret word is banana.")

    def parse_tool_calls(self, reply):
        if self._step == 2 and not self._tool_call_emitted:
            self._tool_call_emitted = True
            return [self._tool_call]
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def test_cli_one_shot_plan_mode_end_to_end_with_real_orchestrator_wiring(monkeypatch, tmp_path, capsys):
    (tmp_path / "note.txt").write_text("the secret word is banana")
    model = _ScriptedThreePhaseModel("read_file", {"path": str(tmp_path / "note.txt")})
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)

    result = cli.main(
        ["-p", "read note.txt and report the secret word", "--cwd", str(tmp_path), "--plan-mode", "on"]
    )

    assert result == 0
    out = capsys.readouterr().out
    # The real ToolRegistry actually read the real file (the default policy
    # pre-approves read_file, so no approval prompt blocks it), and the
    # real TerminalIO rendered all three phases plus the tool call.
    assert "Read note.txt" in out  # plan panel
    assert "note.txt" in out  # tool-call panel (path argument)
    assert "the secret word is banana" in out  # tool result content
    assert "Confirmed" in out  # validation panel


def test_cli_one_shot_plan_mode_persists_phase_tags_to_a_real_encrypted_session(monkeypatch, tmp_path):
    """The same end-to-end wiring as above, but through --session — proving
    plan mode's Turn.phase/Session.validation additions actually survive a
    real save via the CLI, not just SessionManager called directly (see
    test_session.py) or Orchestrator wired up by hand (see
    test_orchestrator.py)."""
    (tmp_path / "note.txt").write_text("the secret word is banana")
    model = _ScriptedThreePhaseModel("read_file", {"path": str(tmp_path / "note.txt")})
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: model)
    monkeypatch.setattr(sessions, "read_password", lambda: "pw")
    session_path = str(tmp_path / "s.json")

    result = cli.main(
        [
            "-p",
            "read note.txt and report the secret word",
            "--cwd",
            str(tmp_path),
            "--plan-mode",
            "on",
            "--session",
            session_path,
            # Nothing is permitted by default; this test is about phase tags
            # surviving a save, not about the permission prompt.
            "--allow-tool",
            "read_file",
        ]
    )

    assert result == 0
    reloaded = SessionManager.load(session_path, AesGcmScryptSessionCrypto(), "pw", str(tmp_path))
    assistant_phases = [t.phase for t in reloaded.session.turns if t.role == "assistant"]
    # plan, then the act phase's tool-call-announcing turn, then its final
    # answer turn (both "act"), then validate.
    assert assistant_phases == ["plan", "act", "act", "validate"]
    tool_turn = next(t for t in reloaded.session.turns if t.role == "tool")
    assert tool_turn.phase == "act"
    assert tool_turn.content == "the secret word is banana"
    assert reloaded.session.validation
    assert reloaded.session.summary == "The secret word is banana."


# --------------------------------------------------------------------------- #
# main(argv): the actual entry point every invocation goes through. Routes
# to _run_one_shot (one-shot) or _run_tui (interactive), which are patched
# out here so these tests check *argument parsing and routing* (main's actual
# job) without needing a real model or a real terminal — that behavior is
# covered by the _run_one_shot tests below, tests/test_tui.py, and the live
# integration tests.
# --------------------------------------------------------------------------- #
def test_main_help_prints_help_and_exits_zero(capsys):
    assert cli.main(["help"]) == 0
    assert "CoBirb" in capsys.readouterr().out


@pytest.mark.parametrize("topic", ["session", "plugins", "tools", "config"])
def test_main_help_topic_prints_that_topic_only(topic, capsys):
    assert cli.main(["help", topic]) == 0
    out = capsys.readouterr().out
    assert out.strip() == help_text.HELP_TOPICS[topic].strip()
    # A topic page is not just the generic overview repeated.
    assert out.strip() != help_text.HELP_TEXT.strip()


def test_main_help_rejects_an_unknown_topic(capsys):
    """A plain nonzero return, not argparse's own SystemExit(2) — `topic`'s
    static choices had to be dropped so `plugin install/list/remove` could
    reuse the same positional slot for its verb, so this is validated by hand
    now. Exit 1 rather than argparse's default 2 is also the better fit here:
    this project's own exit codes give 2 a specific, different meaning
    (headless and something was refused), which argparse's default silently
    collided with."""
    status = cli.main(["help", "does-not-exist"])

    assert status != 0
    assert "does-not-exist" in capsys.readouterr().err


def test_main_one_shot_mode_routes_prompt_and_options(monkeypatch):
    captured = {}

    def fake_run_one_shot(
        prompt, system, allow_overrides, session_path, password, cwd, model_name=None, plan_mode=False, **kwargs
    ):
        captured.update(
            prompt=prompt,
            allow_overrides=allow_overrides,
            session_path=session_path,
            password=password,
            cwd=cwd,
            model_name=model_name,
            plan_mode=plan_mode,
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
    assert captured["plan_mode"] is False


def test_main_plan_mode_flag_routes_through_to_run_one_shot(monkeypatch):
    captured = {}

    def fake_run_one_shot(
        prompt, system, allow_overrides, session_path, password, cwd, model_name=None, plan_mode=False, **kwargs
    ):
        captured["plan_mode"] = plan_mode
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi", "--plan-mode", "on"])

    assert captured["plan_mode"] is True


def test_main_rejects_an_invalid_plan_mode_value():
    with pytest.raises(SystemExit):
        cli.main(["-p", "hi", "--plan-mode", "sideways"])


def test_main_defaults_to_interactive_mode_without_a_prompt(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_run_tui", lambda *a, **k: called.append(1) or 0)

    assert cli.main(["--cwd", "/tmp"]) == 0
    assert called


def test_main_defaults_cwd_to_current_directory(monkeypatch):
    """Resolved to the real, absolute directory — not the literal string
    ".", which reaches the status bar verbatim as "CoBirb · model · ."."""
    captured = {}

    def fake_run_one_shot(
        prompt, system, allow_overrides, session_path, password, cwd, model_name=None, plan_mode=False, **kwargs
    ):
        captured["cwd"] = cwd
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi"])

    assert captured["cwd"] == os.getcwd()


def test_main_one_shot_without_session_never_prompts_for_a_password(monkeypatch):
    def fail_if_called():
        raise AssertionError("must not prompt for a password with no --session and no --password")

    monkeypatch.setattr(sessions, "read_password", fail_if_called)
    monkeypatch.setattr(cli, "_run_one_shot", lambda *a, **k: 0)

    cli.main(["-p", "hi"])  # must not raise


def test_main_one_shot_with_session_prompts_for_a_password(monkeypatch, tmp_path):
    """A session path always means a password is requested, matching
    interactive mode below. Prompting only when --password was *also* given
    leaves `cobirb -p ... --session foo.json` running the whole task and then
    failing to encrypt with a None password when it saves."""
    monkeypatch.setattr(sessions, "read_password", lambda: "typed-secretly")
    captured = {}

    def fake_run_one_shot(
        prompt, system, allow_overrides, session_path, password, cwd, model_name=None, plan_mode=False, **kwargs
    ):
        captured["password"] = password
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi", "--session", str(tmp_path / "s.json")])

    assert captured["password"] == "typed-secretly"


def test_main_interactive_with_session_prompts_for_a_password(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "read_password", lambda: "typed-secretly")
    captured = {}

    def fake_run_tui(
        system, allow_overrides, session_path, password, cwd,
        model_name=None, plan_mode=False,
    ):
        captured["password"] = password
        return 0

    monkeypatch.setattr(cli, "_run_tui", fake_run_tui)
    cli.main(["--session", str(tmp_path / "s.json")])

    assert captured["password"] == "typed-secretly"


# --------------------------------------------------------------------------- #
# -w / --password: asking for a password is asking for a session.
#
# --password is not inert on its own. Reading it only when --session also
# names a path would make `cobirb -w` an ordinary throwaway conversation that
# saves nothing at all.
# --------------------------------------------------------------------------- #
def test_password_alone_starts_a_session_under_the_sessions_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    captured = {}

    def fake_run_tui(
        system, allow_overrides, session_path, password, cwd,
        model_name=None, plan_mode=False,
    ):
        captured.update(session_path=session_path, password=password)
        return 0

    monkeypatch.setattr(cli, "_run_tui", fake_run_tui)
    cli.main(["-w", "1234"])

    assert captured["password"] == "1234"
    assert captured["session_path"].startswith(session.default_sessions_dir())
    assert captured["session_path"].endswith(".json")


def test_password_given_inline_is_used_verbatim_without_prompting(monkeypatch, tmp_path):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))

    def fail_if_called():
        raise AssertionError("-w with a value must not also prompt")

    monkeypatch.setattr(sessions, "read_password", fail_if_called)
    path, password = sessions.resolve_session(None, "1234")

    assert password == "1234"
    assert path is not None


def test_bare_password_flag_still_prompts_without_echo(monkeypatch, tmp_path):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    monkeypatch.setattr(sessions, "read_password", lambda: "typed-secretly")

    path, password = sessions.resolve_session(None, True)

    assert password == "typed-secretly"
    assert path.startswith(session.default_sessions_dir())


def test_a_session_path_is_kept_when_one_was_given(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "read_password", lambda: "pw")

    path, password = sessions.resolve_session("/somewhere/mine.json", True)

    assert path == "/somewhere/mine.json"
    assert password == "pw"


def test_neither_flag_means_no_session_at_all(monkeypatch):
    def fail_if_called():
        raise AssertionError("must not prompt with neither flag given")

    monkeypatch.setattr(sessions, "read_password", fail_if_called)

    assert sessions.resolve_session(None, None) == (None, None)


def test_sessions_live_under_the_cobirb_home(monkeypatch, tmp_path):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))

    assert session.default_sessions_dir() == str(tmp_path / ".cobirb" / "sessions")


def test_password_alone_starts_a_session_in_one_shot_mode_too(monkeypatch, tmp_path):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    captured = {}

    def fake_run_one_shot(
        prompt, system, allow_overrides, session_path, password, cwd, model_name=None, plan_mode=False, **kwargs
    ):
        captured.update(session_path=session_path, password=password)
        return 0

    monkeypatch.setattr(cli, "_run_one_shot", fake_run_one_shot)
    cli.main(["-p", "hi", "-w", "1234"])

    assert captured["password"] == "1234"
    assert captured["session_path"].startswith(session.default_sessions_dir())


# --------------------------------------------------------------------------- #
# The resume hint printed on the way out of a session.
# --------------------------------------------------------------------------- #
def test_resume_hint_names_the_session_and_never_the_password():
    hint = sessions.resume_hint("/home/somebody/.cobirb/sessions/s.json")

    assert "cobirb --session" in hint
    assert "s.json" in hint
    assert hint.rstrip().endswith("-w")  # prompts on resume; no password printed


def test_resume_hint_abbreviates_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "expanduser", lambda p: "/home/somebody" if p == "~" else p)

    hint = sessions.resume_hint("/home/somebody/.cobirb/sessions/s.json")

    assert "~/.cobirb/sessions/s.json" in hint


def test_run_tui_prints_the_resume_hint_for_a_session_that_was_written(monkeypatch, tmp_path, capsys):
    written = tmp_path / "s.json"
    written.write_bytes(b"encrypted")
    monkeypatch.setattr(wiring, "build_orchestrator", lambda *a, **k: object())

    class _FakeApp:
        def __init__(self, **kwargs):
            self.session_path = kwargs["session_path"]

        def run(self):
            pass

    import cobirb.tui.app as tui_app

    monkeypatch.setattr(tui_app, "CoBirbApp", _FakeApp)
    cli._run_tui("system", {}, str(written), "pw", "/work")

    out = capsys.readouterr().out
    assert "Resume it with:" in out
    assert str(written) in out


def test_run_tui_prints_no_hint_when_the_session_was_never_written(monkeypatch, tmp_path, capsys):
    """Quitting before sending anything leaves no file — and nothing to
    resume, so promising a resume command would be a lie."""
    class _FakeApp:
        def __init__(self, **kwargs):
            self.session_path = kwargs["session_path"]

        def run(self):
            pass

    import cobirb.tui.app as tui_app

    monkeypatch.setattr(tui_app, "CoBirbApp", _FakeApp)
    cli._run_tui("system", {}, str(tmp_path / "never.json"), "pw", "/work")

    assert "Resume it with:" not in capsys.readouterr().out


def test_run_tui_reports_the_session_the_app_ended_on_not_the_one_it_started_with(
    monkeypatch, tmp_path, capsys
):
    """The Sessions tab can switch sessions mid-run; the hint has to name the
    file that was actually written."""
    switched = tmp_path / "switched-to.json"
    switched.write_bytes(b"encrypted")

    class _FakeApp:
        def __init__(self, **kwargs):
            self.session_path = kwargs["session_path"]

        def run(self):
            self.session_path = str(switched)

    import cobirb.tui.app as tui_app

    monkeypatch.setattr(tui_app, "CoBirbApp", _FakeApp)
    cli._run_tui("system", {}, None, None, "/work")

    assert str(switched) in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# _run_one_shot's own body: error handling and session persistence.
# Everything above patches this function OUT (to test main's routing in
# isolation); these tests patch _build_orchestrator instead, so
# _run_one_shot's actual logic runs for real. Interactive mode's equivalents
# live in tests/test_tui.py, which drives the real app the same way.
# --------------------------------------------------------------------------- #
def test_run_one_shot_reports_the_final_answer(monkeypatch, capsys):
    monkeypatch.setattr(wiring, "build_orchestrator", lambda *a, **k: _StubOrchestrator())
    system = build_system_prompt()

    result = cli._run_one_shot("do something", system, {}, None, None, "/tmp")

    assert result == 0
    out = capsys.readouterr().out
    assert "do something" in out
    assert "ok" in out  # the stub session's summary


def test_run_one_shot_returns_1_and_reports_a_permission_error(monkeypatch, capsys):
    orchestrator = _StubOrchestrator(run_raises=cli.PermissionError("nope"))
    monkeypatch.setattr(wiring, "build_orchestrator", lambda *a, **k: orchestrator)
    system = build_system_prompt()

    result = cli._run_one_shot("do something", system, {}, None, None, "/tmp")

    assert result == 1
    assert "blocked" in capsys.readouterr().out


def test_run_one_shot_returns_1_and_reports_unexpected_errors_without_crashing(monkeypatch, capsys):
    """A provider/tool exception must produce a clean error message and
    exit code, not an unhandled traceback."""
    orchestrator = _StubOrchestrator(run_raises=RuntimeError("model exploded"))
    monkeypatch.setattr(wiring, "build_orchestrator", lambda *a, **k: orchestrator)
    system = build_system_prompt()

    result = cli._run_one_shot("do something", system, {}, None, None, "/tmp")

    assert result == 1
    out = capsys.readouterr().out
    assert "could not complete" in out
    assert "model exploded" in out


def test_run_one_shot_saves_the_session_when_a_session_path_was_given(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(wiring, "build_orchestrator", lambda *a, **k: orchestrator)
    system = build_system_prompt()

    cli._run_one_shot("do something", system, {}, "/tmp/s.json", "pw", "/tmp")

    assert orchestrator.session.saved_with == ["pw"]


def test_run_one_shot_does_not_save_when_no_session_path_was_given(monkeypatch):
    orchestrator = _StubOrchestrator(with_session_manager=True)
    monkeypatch.setattr(wiring, "build_orchestrator", lambda *a, **k: orchestrator)
    system = build_system_prompt()

    cli._run_one_shot("do something", system, {}, None, None, "/tmp")

    assert orchestrator.session.saved_with == []


# --------------------------------------------------------------------------- #
# System prompt: what CoBirb adds of its own, which is nothing by default.
# --------------------------------------------------------------------------- #


def test_system_prompt_is_empty_by_default():
    """The whole of Modelfile-respecting starts here: with nothing configured
    CoBirb contributes no system prompt, and an empty one means the provider
    sends no system message at all (see test_model.py), leaving the SYSTEM
    directive the model was built with in force."""
    assert build_system_prompt() == ""


def test_the_harness_block_is_opt_in():
    prompt = build_system_prompt(harness=True)

    assert prompt == system_prompt._HARNESS_PROMPT
    assert "A denied call is their decision" in prompt
    assert "check it" in prompt
    assert "no outbound network by default" in prompt


@pytest.mark.parametrize(
    "cli_value,config_value,expected",
    [
        (None, None, False),
        (None, "harness", True),
        (None, "off", False),
        ("harness", None, True),
        ("off", "harness", False),
        (None, "HARNESS", True),
    ],
)
def test_resolve_harness_prompt_prefers_the_flag_then_config_then_off(
    cli_value, config_value, expected, tmp_path
):
    config = Config(user_path=str(tmp_path / "none.json"))
    if config_value is not None:
        config._data = {"system_prompt": config_value}

    assert cli._resolve_harness_prompt(cli_value, config) is expected


def test_main_passes_the_system_prompt_choice_through_to_the_tui(monkeypatch):
    captured = {}

    def fake_run_tui(
        system, allow_overrides, session_path, password, cwd,
        model_name=None, plan_mode=False,
    ):
        captured.update(system=system)
        return 0

    monkeypatch.setattr(cli, "_run_tui", fake_run_tui)
    cli.main(["--system-prompt", "harness"])

    assert captured["system"] == system_prompt._HARNESS_PROMPT


def test_main_sends_no_system_prompt_by_default(monkeypatch):
    captured = {}

    def fake_run_tui(
        system, allow_overrides, session_path, password, cwd,
        model_name=None, plan_mode=False,
    ):
        captured.update(system=system)
        return 0

    monkeypatch.setattr(cli, "_run_tui", fake_run_tui)
    cli.main([])

    assert captured["system"] == ""


# --------------------------------------------------------------------------- #
# Phase C: wiring load_plugins() into the CLI. Tool plugins are merged
# directly into the registry; model/io/crypto plugins are singleton slots
# that only replace the core default when explicitly selected via config.
# --------------------------------------------------------------------------- #
class _FakeToolPlugin(Tool):
    """A minimal third-party tool implementing ``name`` as the SPI's
    documented method (not a plain class attribute like the built-ins)."""

    def __init__(self, cwd=None):
        self.cwd = cwd

    def name(self):
        return "fake_plugin_tool"

    def description(self):
        return "a fake plugin tool"

    def parameters(self):
        return {"type": "object", "properties": {}}

    def execute(self, arguments):
        return ToolResult(ok=True, content="ran")


class _NoArgToolPlugin(Tool):
    """A tool plugin whose constructor doesn't accept a cwd argument."""

    def name(self):
        return "no_arg_tool"

    def description(self):
        return "takes no constructor args"

    def parameters(self):
        return {"type": "object", "properties": {}}

    def execute(self, arguments):
        return ToolResult(ok=True, content="ran")


class _CollidingToolPlugin(Tool):
    """Declares the same name as a built-in tool."""

    def name(self):
        return "read_file"

    def description(self):
        return "shadows a built-in"

    def parameters(self):
        return {"type": "object", "properties": {}}

    def execute(self, arguments):
        return ToolResult(ok=True, content="ran")


def test_merge_tool_plugins_registers_a_discovered_tool(tmp_path):
    registry = ToolRegistry(str(tmp_path))
    problems = plugins.merge_tool_plugins(registry, {"tool:fake": _FakeToolPlugin})

    assert problems == {}
    assert registry.is_known("fake_plugin_tool")


def test_merge_tool_plugins_falls_back_when_constructor_takes_no_cwd(tmp_path):
    registry = ToolRegistry(str(tmp_path))
    problems = plugins.merge_tool_plugins(registry, {"tool:no-arg": _NoArgToolPlugin})

    assert problems == {}
    assert registry.is_known("no_arg_tool")


def test_merge_tool_plugins_skips_and_reports_a_name_collision(tmp_path):
    """A plugin must never be able to shadow a built-in tool."""
    registry = ToolRegistry(str(tmp_path))
    original = registry.get("read_file")

    problems = plugins.merge_tool_plugins(registry, {"tool:evil": _CollidingToolPlugin})

    assert "tool:evil" in problems
    assert "collides" in problems["tool:evil"]
    assert registry.get("read_file") is original


def test_merge_tool_plugins_deduplicates_the_same_plugin_discovered_twice(tmp_path):
    """A single plugin can legitimately show up under two discovery keys
    (e.g. both as an installed entry point and as a local plugin directory
    when it happens to be both) — that must not be reported as a collision."""
    registry = ToolRegistry(str(tmp_path))

    problems = plugins.merge_tool_plugins(
        registry,
        {"tool:entry-point-path": _FakeToolPlugin, "tool:local-dir-path": _FakeToolPlugin},
    )

    assert problems == {}
    assert registry.is_known("fake_plugin_tool")


def test_merge_tool_plugins_reports_instantiation_failures(tmp_path):
    class _BrokenToolPlugin(Tool):
        def __init__(self, cwd=None):
            raise RuntimeError("boom")

        def name(self):
            return "broken"

        def description(self):
            return ""

        def parameters(self):
            return {"type": "object", "properties": {}}

        def execute(self, arguments):
            return ToolResult(ok=True, content="")

    registry = ToolRegistry(str(tmp_path))
    problems = plugins.merge_tool_plugins(registry, {"tool:broken": _BrokenToolPlugin})

    assert "tool:broken" in problems
    assert "boom" in problems["tool:broken"]
    assert not registry.is_known("broken")


def test_merge_tool_plugins_ignores_non_tool_kinds(tmp_path):
    """model:/io:/crypto: entries are handled separately (see _select_plugin)."""
    registry = ToolRegistry(str(tmp_path))
    before = set(registry.names())

    problems = plugins.merge_tool_plugins(registry, {"model:some-model": object})

    assert problems == {}
    assert set(registry.names()) == before


def test_select_plugin_returns_none_when_unconfigured(tmp_path):
    config = Config()
    cls, issue = plugins.select_plugin("model", {"model:custom": object}, config)
    assert cls is None
    assert issue is None


def test_select_plugin_treats_the_core_default_name_as_unconfigured(tmp_path):
    """Selecting 'core-<kind>' explicitly must not try to bare-construct the
    core class (which needs its normal constructor arguments elsewhere)."""
    write_config(tmp_path, json.loads('{"plugins": {"model": "core-model"}}'))
    config = Config()

    cls, issue = plugins.select_plugin("model", {"model:core-model": object}, config)

    assert cls is None
    assert issue is None


def test_select_plugin_reports_an_unknown_selection(tmp_path):
    write_config(tmp_path, json.loads('{"plugins": {"model": "does-not-exist"}}'))
    config = Config()

    cls, issue = plugins.select_plugin("model", {}, config)

    assert cls is None
    assert "unknown model plugin" in issue


def test_select_plugin_resolves_a_configured_selection(tmp_path):
    write_config(tmp_path, json.loads('{"plugins": {"io": "my-io"}}'))
    config = Config()

    cls, issue = plugins.select_plugin("io", {"io:my-io": TerminalIO}, config)

    assert cls is TerminalIO
    assert issue is None


def test_build_orchestrator_merges_discovered_tool_plugins(monkeypatch, tmp_path):
    """The end-to-end wiring: a plugin discovered by load_plugins() must
    actually reach the orchestrator's live tool registry."""
    monkeypatch.setattr(
        plugins, "load_plugins", lambda: ({"tool:fake": _FakeToolPlugin}, {})
    )

    orchestrator = wiring.build_orchestrator(str(tmp_path), {})

    assert "fake_plugin_tool" in orchestrator.tools
    assert "fake_plugin_tool" in orchestrator.tools


def test_build_orchestrator_reports_plugin_errors_to_stderr(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        plugins, "load_plugins", lambda: ({}, {"entry-point:broken": "boom"})
    )

    wiring.build_orchestrator(str(tmp_path), {})

    assert "boom" in capsys.readouterr().err


def test_build_orchestrator_uses_a_configured_io_plugin(monkeypatch, tmp_path):
    class _CustomIO(TerminalIO):
        pass

    write_config(tmp_path, json.loads('{"plugins": {"io": "custom-io"}}'))
    monkeypatch.setattr(plugins, "load_plugins", lambda: ({"io:custom-io": _CustomIO}, {}))

    orchestrator = wiring.build_orchestrator(str(tmp_path), {})

    assert isinstance(orchestrator.io, _CustomIO)


def test_build_orchestrator_reports_an_unknown_configured_model_plugin(monkeypatch, tmp_path, capsys):
    write_config(tmp_path, json.loads('{"plugins": {"model": "ghost"}}'))
    monkeypatch.setattr(plugins, "load_plugins", lambda: ({}, {}))

    wiring.build_orchestrator(str(tmp_path), {})

    assert "unknown model plugin 'ghost'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _build_model's resolution chain, including "default_model" — the newest
# and simplest of its three equivalent config keys (see cobirb help config).
# --------------------------------------------------------------------------- #
def test_build_model_prefers_the_explicit_argument_over_everything(tmp_path):
    write_config(tmp_path, json.loads('{"model": "from-model-key", "default_model": "from-default-model"}'))
    provider = wiring.build_model("from-argument", str(tmp_path))
    assert provider.name() == "ollama/from-argument"


def test_build_model_falls_back_to_the_model_key(tmp_path):
    write_config(tmp_path, json.loads('{"model": "from-model-key"}'))
    assert wiring.build_model(None, str(tmp_path)).name() == "ollama/from-model-key"


def test_build_model_falls_back_to_models_default_name(tmp_path):
    write_config(tmp_path, json.loads('{"models": {"default": {"name": "from-models-default"}}}'))
    assert wiring.build_model(None, str(tmp_path)).name() == "ollama/from-models-default"


def test_build_model_falls_back_to_default_model_key(tmp_path):
    write_config(tmp_path, json.loads('{"default_model": "from-default-model"}'))
    assert wiring.build_model(None, str(tmp_path)).name() == "ollama/from-default-model"


def test_build_model_default_model_is_the_lowest_priority_fallback(tmp_path):
    """All three name the same setting; "default_model" only kicks in once
    the older, more specific keys have nothing to say."""
    write_config(tmp_path, json.loads(
        '{"model": "from-model-key", "default_model": "from-default-model"}'
    ))
    assert wiring.build_model(None, str(tmp_path)).name() == "ollama/from-model-key"


def test_build_model_with_nothing_configured_is_unconfigured(tmp_path):
    assert wiring.build_model(None, str(tmp_path)).name() == "(unconfigured)"


# --------------------------------------------------------------------------- #
# describe_plugins: the live snapshot behind the TUI's Plugins tab. Needs no
# model and builds no orchestrator.
# --------------------------------------------------------------------------- #
def test_describe_plugins_lists_every_built_in_tool_as_core(tmp_path):
    summary = plugins.describe_plugins(str(tmp_path))

    names = {tool.name for tool in summary.tools}
    assert {"read_file", "write_file", "shell", "grep"} <= names
    assert all(tool.source == "core" for tool in summary.tools)
    assert all(tool.description for tool in summary.tools)


def test_describe_plugins_reports_core_for_every_unconfigured_slot(tmp_path):
    summary = plugins.describe_plugins(str(tmp_path))
    assert summary.slots == {"model": "core", "io": "core", "crypto": "core"}


def test_describe_plugins_names_configured_mcp_servers_without_starting_them(tmp_path):
    """This snapshot exists to populate a tab. Starting somebody's database
    proxy to draw a list would be an absurd side effect of opening it, so the
    names are what can honestly be shown until a turn builds a real one."""
    home = tmp_path / ".cobirb"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "notes": {"command": "false"},
                    "off": {"command": "false", "enabled": False},
                }
            }
        )
    )

    summary = plugins.describe_plugins(str(tmp_path))

    assert summary.mcp_servers == ["notes"]
    assert summary.issues == {}  # nothing was started, so nothing failed


def test_describe_plugins_reports_a_misconfigured_slot_without_crashing(tmp_path):
    write_config(tmp_path, json.loads('{"plugins": {"model": "ghost"}}'))

    summary = plugins.describe_plugins(str(tmp_path))

    assert "unknown model plugin 'ghost'" in summary.slots["model"]


def test_describe_plugins_reports_a_discovered_tool_plugin_and_its_issues(monkeypatch, tmp_path):
    monkeypatch.setattr(
        plugins, "load_plugins", lambda: ({"tool:extra": _FakeToolPlugin}, {"tool:broken": "it exploded"})
    )

    summary = plugins.describe_plugins(str(tmp_path))

    plugin_tools = {tool.name: tool.source for tool in summary.tools}
    assert plugin_tools["fake_plugin_tool"] == "plugin"
    assert summary.issues == {"tool:broken": "it exploded"}


def test_describe_plugins_reports_a_configured_model_plugin_by_name(monkeypatch, tmp_path):
    write_config(tmp_path, json.loads('{"plugins": {"model": "custom"}}'))

    class _FakeModelPlugin(cobirb_typing.ModelProvider):
        def name(self):
            return "custom"

        def chat(self, *a, **k):
            return ""

        def parse_tool_calls(self, raw):
            return []

        def supports_tool_calling(self):
            return False

        def supports_streaming(self):
            return False

        def supports_vision(self):
            return False

    monkeypatch.setattr(plugins, "load_plugins", lambda: ({"model:custom": _FakeModelPlugin}, {}))

    summary = plugins.describe_plugins(str(tmp_path))

    assert summary.slots["model"] == "custom"


def test_describe_plugins_never_reports_to_stderr_itself(monkeypatch, tmp_path, capsys):
    """Unlike _build_orchestrator, describe_plugins hands issues back for the
    caller (the Plugins tab) to display — it must not also duplicate them to
    stderr, which a full-screen TUI user would never see anyway."""
    monkeypatch.setattr(plugins, "load_plugins", lambda: ({}, {"tool:broken": "it exploded"}))

    plugins.describe_plugins(str(tmp_path))

    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# Phase D: rendering the final answer through io's rich chrome hook (see
# TerminalIO.render_answer) when it has one, falling back to plain text
# otherwise.
# --------------------------------------------------------------------------- #
class _RecordingAnswerIO:
    def __init__(self):
        self.answers = []

    def render_answer(self, label, text):
        self.answers.append((label, text))


def test_render_final_answer_uses_the_io_hook_when_present():
    io_adapter = _RecordingAnswerIO()
    orchestrator = _StubOrchestrator()
    orchestrator.io = io_adapter

    cli._render_final_answer(orchestrator, "Noah", "the answer")

    assert io_adapter.answers == [("Noah", "the answer")]


def test_render_final_answer_falls_back_to_plain_text_without_the_hook(capsys):
    """A stub orchestrator with no ``io`` attribute at all (as used
    throughout this file's routing tests) must not crash."""
    cli._render_final_answer(_StubOrchestrator(), "Noah", "the answer")

    assert "Noah: the answer" in capsys.readouterr().out


def test_render_final_answer_falls_back_to_completed_for_empty_text(capsys):
    cli._render_final_answer(_StubOrchestrator(), "Noah", "")

    assert "Noah: Completed." in capsys.readouterr().out


def test_resolve_model_name_returns_the_provider_display_name(monkeypatch):
    monkeypatch.setattr(wiring, "build_model", lambda model_name, cwd: type("M", (), {"name": lambda self: "fake/model"})()
    )

    assert wiring.resolve_model_name(None, "/tmp") == "fake/model"


def test_resolve_model_name_swallows_a_broken_model_build(monkeypatch):
    """Resolving the model's display name is cosmetic — it feeds the session
    banner and the TUI's status bar — so a broken config must not block
    startup. The real error still surfaces once a turn actually needs the
    model."""

    def _broken_build_model(model_name, cwd):
        raise RuntimeError("bad config")

    monkeypatch.setattr(wiring, "build_model", _broken_build_model)

    assert wiring.resolve_model_name("my-model", "/tmp") == "my-model"
    assert wiring.resolve_model_name(None, "/tmp") == "(no model configured)"


# --------------------------------------------------------------------------- #
# Slash-command helpers. They return their message rather than printing it,
# so both front-ends share the exact same logic and wording instead of each
# reimplementing it. See cobirb/tui/app.py's on_input_submitted.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "arg,expected_mode,expected_message",
    [
        ("on", True, "Plan mode: on."),
        ("off", False, "Plan mode: off."),
        (" ON ", True, "Plan mode: on."),
    ],
)
def test_apply_plan_toggle_turns_plan_mode_on_and_off(arg, expected_mode, expected_message):
    assert commands.apply_plan_toggle(arg, not expected_mode) == (expected_mode, expected_message)


@pytest.mark.parametrize("plan_mode,expected", [(True, "on"), (False, "off")])
def test_apply_plan_toggle_with_no_argument_reports_the_current_state(plan_mode, expected):
    result_mode, message = commands.apply_plan_toggle("", plan_mode)

    assert result_mode is plan_mode
    assert f"Plan mode is {expected}" in message
    assert "Usage: /plan on|off" in message


def test_apply_plan_toggle_rejects_an_unrecognized_argument():
    assert commands.apply_plan_toggle("sideways", True) == (True, "Usage: /plan on|off")


# --------------------------------------------------------------------------- #
# _run_tui: interactive mode's entry point. Its job is to construct the app
# and run it; the app's own behavior is covered by tests/test_tui.py.
# --------------------------------------------------------------------------- #
def test_run_tui_builds_the_app_with_everything_it_was_given(monkeypatch):
    captured = {}

    class _FakeApp:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            # Mirrors the real app, which keeps session_path as an attribute
            # _run_tui reads back after run() to print the resume hint.
            self.session_path = kwargs["session_path"]

        def run(self):
            captured["ran"] = True

    import cobirb.tui.app as tui_app

    monkeypatch.setattr(tui_app, "CoBirbApp", _FakeApp)

    result = cli._run_tui(
        "system", {"shell": "git"}, "/tmp/s.json", "pw", "/work", "llama3.1", True
    )

    assert result == 0
    assert captured["ran"] is True
    assert captured["allow_overrides"] == {"shell": "git"}
    assert captured["session_path"] == "/tmp/s.json"
    assert captured["password"] == "pw"
    assert captured["cwd"] == "/work"
    assert captured["model_name"] == "llama3.1"
    assert captured["plan_mode"] is True


def test_run_tui_reports_a_missing_textual_instead_of_crashing(monkeypatch, capsys):
    """Textual is imported lazily, inside _run_tui, precisely so a missing or
    broken install can't take down `cobirb -p` or `cobirb help` — modes that
    never touch it. Interactive mode itself should then fail with one clear
    line, not an import traceback."""
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if "tui" in name:
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    result = cli._run_tui("system", {}, None, None, "/tmp")

    assert result == 1
    err = capsys.readouterr().err
    assert "needs textual" in err
    assert "cobirb -p" in err


# --------------------------------------------------------------------------- #
# Resuming a session that won't unlock.
#
# The failure must happen before the app starts. A wrong password that takes
# you all the way in — full-screen UI, a model to pick — then reports itself
# into the transcript of a session that never opened. If a session was asked
# for and can't be unlocked there is nothing to interact with, so nothing
# should start.
# --------------------------------------------------------------------------- #
class _NeverRunsApp:
    """Records that it was constructed, and fails the test if it is run."""

    started = False

    def __init__(self, **kwargs):
        self.session_path = kwargs["session_path"]
        self.orchestrator = None
        self.io_bridge = object()

    def run(self):
        type(self).started = True


@pytest.fixture
def _never_runs_app(monkeypatch):
    import cobirb.tui.app as tui_app

    _NeverRunsApp.started = False
    monkeypatch.setattr(tui_app, "CoBirbApp", _NeverRunsApp)
    return _NeverRunsApp


def test_a_session_that_will_not_unlock_never_starts_the_app(monkeypatch, tmp_path, capsys, _never_runs_app):
    locked = tmp_path / "s.json"
    locked.write_bytes(b"encrypted")

    def wrong_password(*args, **kwargs):
        from cryptography.exceptions import InvalidTag

        raise InvalidTag()

    monkeypatch.setattr(wiring, "build_orchestrator", wrong_password)

    status = cli._run_tui("", {}, str(locked), "nope", "/work")

    assert status == 1
    assert _never_runs_app.started is False
    assert "could not open" in capsys.readouterr().err


def test_a_wrong_password_is_explained_rather_than_printed_blank(monkeypatch, tmp_path, capsys, _never_runs_app):
    """A wrong password surfaces as the crypto library's InvalidTag, which
    carries no message at all — printed raw it left a dangling dash."""
    locked = tmp_path / "s.json"
    locked.write_bytes(b"encrypted")

    def wrong_password(*args, **kwargs):
        from cryptography.exceptions import InvalidTag

        raise InvalidTag()

    monkeypatch.setattr(wiring, "build_orchestrator", wrong_password)
    cli._run_tui("", {}, str(locked), "nope", "/work")

    err = capsys.readouterr().err
    assert "wrong password, or the file has been modified" in err
    assert not err.rstrip().endswith("—")


def test_a_tampered_session_keeps_its_own_explanation(monkeypatch, tmp_path, capsys, _never_runs_app):
    """Hash verification raises with a real message; only an empty one gets
    the generic wording."""
    locked = tmp_path / "s.json"
    locked.write_bytes(b"encrypted")

    def tampered(*args, **kwargs):
        raise ValueError("tampered session detected in s.json (turn user corrupted)")

    monkeypatch.setattr(wiring, "build_orchestrator", tampered)
    cli._run_tui("", {}, str(locked), "nope", "/work")

    assert "tampered session detected" in capsys.readouterr().err


def test_a_session_that_unlocks_is_handed_to_the_app_already_open(monkeypatch, tmp_path, _never_runs_app):
    """Opened once, before the app starts — so the app has nothing left to
    decrypt and the file is read exactly one time."""
    unlocked = tmp_path / "s.json"
    unlocked.write_bytes(b"encrypted")
    opened = object()
    builds = []

    def build(*args, **kwargs):
        builds.append(kwargs.get("io_factory"))
        return opened

    monkeypatch.setattr(wiring, "build_orchestrator", build)
    captured = {}
    original_init = _NeverRunsApp.__init__

    def record_init(self, **kwargs):
        original_init(self, **kwargs)
        captured["app"] = self

    monkeypatch.setattr(_NeverRunsApp, "__init__", record_init)

    status = cli._run_tui("", {}, str(unlocked), "pw", "/work")

    assert status == 0
    assert _never_runs_app.started is True
    assert captured["app"].orchestrator is opened
    assert len(builds) == 1  # decrypted once, not again inside the app
    assert callable(builds[0])  # the app's own I/O bridge was passed in


def test_a_new_session_path_does_not_try_to_unlock_anything(monkeypatch, tmp_path, _never_runs_app):
    """--session naming a file that doesn't exist yet is a new session; there
    is nothing to open and no password to be wrong."""
    def fail_if_called(*args, **kwargs):
        raise AssertionError("nothing to open for a file that isn't there")

    monkeypatch.setattr(wiring, "build_orchestrator", fail_if_called)

    status = cli._run_tui("", {}, str(tmp_path / "new.json"), "pw", "/work")

    assert status == 0
    assert _never_runs_app.started is True


def test_one_shot_reports_a_session_that_will_not_unlock_instead_of_tracebacking(
    monkeypatch, tmp_path, capsys
):
    def wrong_password(*args, **kwargs):
        from cryptography.exceptions import InvalidTag

        raise InvalidTag()

    monkeypatch.setattr(wiring, "build_orchestrator", wrong_password)

    status = cli._run_one_shot(
        "hi", "", {}, str(tmp_path / "s.json"), "nope", "/work"
    )

    assert status == 1
    assert "wrong password, or the file has been modified" in capsys.readouterr().err


def test_one_shot_does_not_echo_a_prompt_it_never_ran(monkeypatch, tmp_path, capsys):
    """Echoing before the session failed to open reads as though the task was
    attempted, when nothing ran at all."""
    def wrong_password(*args, **kwargs):
        from cryptography.exceptions import InvalidTag

        raise InvalidTag()

    monkeypatch.setattr(wiring, "build_orchestrator", wrong_password)
    cli._run_one_shot("do the thing", "", {}, str(tmp_path / "s.json"), "no", "/w")

    assert "do the thing" not in capsys.readouterr().out


def test_no_resume_hint_is_printed_after_a_failed_run(monkeypatch, tmp_path, capsys):
    """"could not open …" followed by "Session saved" claimed something that
    never happened — the file existed, it just wasn't this run's doing."""
    existing = tmp_path / "s.json"
    existing.write_bytes(b"encrypted")

    def wrong_password(*args, **kwargs):
        from cryptography.exceptions import InvalidTag

        raise InvalidTag()

    monkeypatch.setattr(wiring, "build_orchestrator", wrong_password)
    monkeypatch.setattr(sessions, "read_password", lambda: "nope")
    cli.main(["-p", "hi", "--session", str(existing), "-w"])


# --------------------------------------------------------------------------- #
# --branch — session branching's CLI surface (v0.6.0). Forks the session
# named by --session into a new file; see fork_session() (session.py) for
# the actual branching logic, exercised there in more depth.
# --------------------------------------------------------------------------- #
def _seed_session(path: str, *, turns: int = 3) -> None:
    manager = SessionManager.create(path, AesGcmScryptSessionCrypto(), "/work", "pw")
    for i in range(turns):
        manager.session.add_text("user" if i % 2 == 0 else "assistant", f"turn {i}")
    manager.save("pw")


def test_branch_forks_the_named_session_and_reports_the_new_path(tmp_path, capsys):
    source = str(tmp_path / "s.json")
    _seed_session(source)
    destination = str(tmp_path / "branch.json")

    status = cli.main(["--session", source, "-w", "pw", "--branch", destination])

    assert status == 0
    assert os.path.exists(destination)
    out = capsys.readouterr().out
    assert "Branched 3 turn(s)" in out
    assert destination in out
    branch = SessionManager.load(destination, AesGcmScryptSessionCrypto(), "pw")
    assert len(branch.session.turns) == 3


def test_branch_at_keeps_only_a_prefix(tmp_path):
    source = str(tmp_path / "s.json")
    _seed_session(source, turns=4)
    destination = str(tmp_path / "branch.json")

    status = cli.main(["--session", source, "-w", "pw", "--branch", destination, "--branch-at", "1"])

    assert status == 0
    branch = SessionManager.load(destination, AesGcmScryptSessionCrypto(), "pw")
    assert [t.content for t in branch.session.turns] == ["turn 0", "turn 1"]


def test_branch_without_a_session_is_refused(capsys):
    status = cli.main(["--branch", "/tmp/wherever.json"])

    assert status == 1
    assert "--branch needs --session" in capsys.readouterr().err


def test_upgrade_flag_dispatches_to_run_upgrade(monkeypatch):
    """Thin dispatch test — the actual upgrade logic is tested against
    ``runtime.upgrade`` directly in ``test_upgrade.py``."""
    seen = {}

    def fake_run_upgrade(tag, *, force):
        seen["tag"] = tag
        seen["force"] = force
        return 0

    monkeypatch.setattr(cli, "_run_upgrade", fake_run_upgrade)

    assert cli.main(["--upgrade", "v0.8.0", "--force"]) == 0
    assert seen == {"tag": "v0.8.0", "force": True}


def test_upgrade_flag_with_no_tag_passes_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli, "_run_upgrade", lambda tag, *, force: seen.update(tag=tag, force=force) or 0
    )

    assert cli.main(["--upgrade"]) == 0
    assert seen == {"tag": None, "force": False}


def test_force_without_upgrade_says_so_rather_than_doing_nothing(capsys):
    status = cli.main(["--force"])

    assert status == 1
    assert "--force only means something with --upgrade" in capsys.readouterr().err


def test_branch_at_without_branch_says_so_rather_than_doing_nothing(capsys):
    """A typed flag that quietly does nothing is the same failure as `-w`
    once being inert without --session."""
    status = cli.main(["--branch-at", "2"])

    assert status == 1
    assert "--branch-at only means something with --branch" in capsys.readouterr().err


def test_branch_reports_an_out_of_range_turn_plainly(tmp_path, capsys):
    source = str(tmp_path / "s.json")
    _seed_session(source, turns=2)

    status = cli.main(
        ["--session", source, "-w", "pw", "--branch", str(tmp_path / "b.json"), "--branch-at", "99"]
    )

    assert status == 1
    assert "out of range" in capsys.readouterr().err


def test_branch_reports_a_wrong_password_the_same_way_export_does(tmp_path, capsys):
    source = str(tmp_path / "s.json")
    _seed_session(source)

    status = cli.main(["--session", source, "-w", "nope", "--branch", str(tmp_path / "b.json")])

    assert status == 1
    assert "could not open" in capsys.readouterr().err

    assert "Session saved" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# --continue: reopen the session you were last in.
#
# The information was always on disk — discover_sessions sorts by modification
# time — it simply had no command in front of it, so getting back to yesterday
# meant reading timestamps out of filenames.
# --------------------------------------------------------------------------- #
def _session_file(tmp_path, name, *, age_seconds=0):
    """A file in the sessions directory, optionally backdated."""
    directory = os.path.join(str(tmp_path), ".cobirb", "sessions")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def test_most_recent_session_is_the_one_last_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    _session_file(tmp_path, "old.json", age_seconds=9000)
    newest = _session_file(tmp_path, "new.json")

    assert sessions.most_recent_session() == newest


def test_continue_picks_the_most_recent_and_asks_for_a_password(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    _session_file(tmp_path, "old.json", age_seconds=9000)
    newest = _session_file(tmp_path, "new.json")
    monkeypatch.setattr(sessions, "read_password", lambda: "typed-secretly")

    path, password = sessions.resolve_session(None, None, continue_last=True)

    assert path == newest
    assert password == "typed-secretly"


def test_continue_honours_a_password_given_on_the_command_line(tmp_path, monkeypatch):
    """The non-interactive form still works — otherwise --continue could
    never be scripted."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    newest = _session_file(tmp_path, "new.json")

    def fail():
        raise AssertionError("should not prompt when -w gave the password")

    monkeypatch.setattr(sessions, "read_password", fail)

    path, password = sessions.resolve_session(None, "hunter2", continue_last=True)

    assert (path, password) == (newest, "hunter2")


def test_continue_with_nothing_to_continue_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    with pytest.raises(sessions.NoSessionToContinue, match="no sessions"):
        sessions.resolve_session(None, None, continue_last=True)


def test_main_reports_nothing_to_continue_rather_than_tracebacking(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))

    code = cli.main(["--continue", "-p", "hello"])

    assert code == 1
    assert "no sessions" in capsys.readouterr().err


def test_continue_reaches_the_orchestrator_with_that_session(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    newest = _session_file(tmp_path, "new.json")
    captured = {}

    def fake_build(cwd, allow_overrides, session_path=None, password=None,
                   model_name=None, io_factory=None):
        captured["session_path"] = session_path
        captured["password"] = password
        return _StubOrchestrator()

    monkeypatch.setattr(wiring, "build_orchestrator", fake_build)
    monkeypatch.setattr(sessions, "read_password", lambda: "pw")

    cli.main(["--continue", "-p", "hello"])

    assert captured["session_path"] == newest
    assert captured["password"] == "pw"
