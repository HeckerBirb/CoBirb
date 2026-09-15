"""Tests for unattended runs: no prompts, machine-readable output, exit codes."""
from __future__ import annotations

import json

from cobirb import cli
from cobirb.runtime import wiring
from cobirb.runtime.headless import EXIT_DENIED, EXIT_ERROR, EXIT_OK, HeadlessIO, HeadlessResult
from cobirb.typing.spi import ToolCall


class _ScriptedModel:
    """Reads a file, then reaches for something it hasn't been allowed."""

    def __init__(self, calls):
        self._calls = list(calls)
        self._index = 0

    def chat(self, *args, **kwargs):
        return "" if self._index < len(self._calls) else "done"

    def parse_tool_calls(self, raw):
        if self._index >= len(self._calls):
            return []
        call = self._calls[self._index]
        self._index += 1
        return [call]

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False

    def context_window(self):
        return 8192


def _run(monkeypatch, tmp_path, argv, calls):
    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _ScriptedModel(calls))
    return cli.main([*argv, "--cwd", str(tmp_path)])


def test_headless_refuses_rather_than_prompting(tmp_path, monkeypatch, capsys):
    """In a pipeline the terminal prompt would block on a question nobody
    will answer, so it is never asked."""
    (tmp_path / "a.py").write_text("x = 1\n")

    code = _run(
        monkeypatch, tmp_path,
        ["-p", "read it", "--headless", "--output", "json"],
        [ToolCall("read_file", {"path": "a.py"})],
    )

    report = json.loads(capsys.readouterr().out)
    assert report["denied"] == ["read_file"]
    assert code == EXIT_DENIED


def test_a_permitted_tool_runs_unattended_and_exits_clean(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.py").write_text("x = 1\n")

    code = _run(
        monkeypatch, tmp_path,
        ["-p", "read it", "--headless", "--output", "json", "--allow-tool", "read_file"],
        [ToolCall("read_file", {"path": "a.py"})],
    )

    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and not report["denied"]
    assert [c["name"] for c in report["tool_calls"]] == ["read_file"]
    assert code == EXIT_OK


def test_json_output_is_the_only_thing_on_stdout(tmp_path, monkeypatch, capsys):
    """A consumer parses stdout. Anything else there is a bug, including the
    echoed prompt and the resume hint."""
    code = _run(monkeypatch, tmp_path, ["-p", "say hi", "--headless", "--output", "json"], [])

    out = capsys.readouterr().out
    assert json.loads(out)  # parses whole, with nothing bracketing it
    assert code == EXIT_OK


def test_a_failure_is_reported_as_json_rather_than_a_traceback(tmp_path, monkeypatch, capsys):
    class _Broken(_ScriptedModel):
        def chat(self, *args, **kwargs):
            raise RuntimeError("the endpoint is down")

    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _Broken([]))

    code = cli.main(["-p", "hi", "--headless", "--output", "json", "--cwd", str(tmp_path)])

    report = json.loads(capsys.readouterr().out)
    assert not report["ok"]
    assert "the endpoint is down" in report["error"]
    assert code == EXIT_ERROR


def test_the_context_budget_is_in_the_report(tmp_path, monkeypatch, capsys):
    """A headless run that quietly compacted half its history away is
    something whoever reads the log needs to be able to see."""
    _run(monkeypatch, tmp_path, ["-p", "hi", "--headless", "--output", "json"], [])

    context = json.loads(capsys.readouterr().out)["context"]
    assert context["budget_tokens"] > 0
    assert context["compacted"] is False


def test_an_interactive_denial_is_not_an_error(tmp_path, monkeypatch, capsys):
    """A person who answered "no" got exactly what they asked for. Returning
    2 there would make an ordinary decision look like a broken script."""
    result = HeadlessResult(ok=True, summary="fine", denied=["shell"])

    assert result.exit_code(unattended=True) == EXIT_DENIED
    assert result.exit_code(unattended=False) == EXIT_OK


def test_the_headless_adapter_denies_and_remembers_what_it_denied():
    io = HeadlessIO()

    assert io.confirm("shell", {"command": "rm -rf /"}) == "deny"
    assert io.denied == ["shell"]


def test_there_is_no_flag_that_approves_everything():
    """Deliberate. A policy file is a decision someone made once, reviewably,
    in a file their colleagues can read; a blanket approval flag is a decision
    nobody made at all. If this ever fails, that argument was lost by
    accident rather than on purpose."""
    parser = cli._build_parser()
    flags = {action.dest for action in parser._actions}

    assert "yes" not in flags
    assert "dangerously_allow_all" not in flags
