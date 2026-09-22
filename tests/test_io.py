"""Tests for the terminal I/O adapter, in particular the approval prompt."""
from __future__ import annotations

import io as iomod

import pytest
from rich.console import Console

from cobirb.plugins.core.io import TerminalIO
from cobirb.typing.spi import ToolResult


def _make_terminal_io(width: int = 100) -> tuple[TerminalIO, iomod.StringIO]:
    """A TerminalIO wired to a captured, color-free Console so its rendered
    text (box-drawing chars aside) can be asserted on directly."""
    buf = iomod.StringIO()
    console = Console(file=buf, width=width, force_terminal=False, no_color=True)
    return TerminalIO(console=console), buf


@pytest.mark.parametrize("answer,expected", [("y", "once"), ("yes", "once"), ("Y", "once")])
def test_confirm_yes_variants_mean_once(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert TerminalIO().confirm("shell", {"command": "git status"}) == expected


@pytest.mark.parametrize("answer", ["a", "always", "ALWAYS"])
def test_confirm_always_variants(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert TerminalIO().confirm("shell", {"command": "git status"}) == "always"


@pytest.mark.parametrize("answer", ["n", "no", "", "anything else"])
def test_confirm_everything_else_denies(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert TerminalIO().confirm("shell", {"command": "git status"}) == "deny"


def test_confirm_fails_closed_on_eof(monkeypatch):
    def raise_eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)
    assert TerminalIO().confirm("shell", {"command": "git status"}) == "deny"


def test_confirm_fails_closed_on_keyboard_interrupt(monkeypatch):
    def raise_interrupt(prompt=""):
        raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", raise_interrupt)
    assert TerminalIO().confirm("shell", {"command": "git status"}) == "deny"


def test_confirm_prompt_mentions_tool_and_detail(monkeypatch):
    captured = {}

    def fake_input(prompt=""):
        captured["prompt"] = prompt
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)
    TerminalIO().confirm("shell", {"command": "rm -rf /"})
    assert "shell" in captured["prompt"]
    assert "rm -rf /" in captured["prompt"]


# --------------------------------------------------------------------------- #
# Phase D: rich-powered chrome beyond the I_OAdapter contract. These are
# duck-typed extras (see cli.py/orchestrator.py's getattr(..., None) call
# sites) — TerminalIO is the one concrete adapter that implements them.
# --------------------------------------------------------------------------- #
def test_render_writes_plain_text_without_interpreting_markup():
    """Streamed model output is arbitrary text, not authored rich markup —
    a stray '[' from the model must never be parsed as (or crash on) a
    markup tag."""
    term, buf = _make_terminal_io()
    term.render("this has [brackets] and [[double]] ones\n")
    assert "this has [brackets] and [[double]] ones" in buf.getvalue()


def test_render_answer_renders_markdown_content():
    term, buf = _make_terminal_io()
    term.render_answer("Noah", "**bold** and a list:\n\n- one\n- two")
    out = buf.getvalue()
    assert "bold" in out
    assert "one" in out and "two" in out


def test_render_answer_marks_the_reply_and_does_not_label_it():
    """Replies carry the same ``>`` the prompt does, told apart by colour —
    not by a label printed above every one of them."""
    term, buf = _make_terminal_io()
    term.render_answer("Noah", "Hello there.")
    out = buf.getvalue()
    assert out.startswith("> Hello there.")
    assert "Noah" not in out


def test_render_answer_is_a_noop_for_empty_text():
    term, buf = _make_terminal_io()
    term.render_answer("Noah", "")
    assert buf.getvalue() == ""


def test_render_tool_call_shows_the_tool_name_and_result():
    term, buf = _make_terminal_io()
    term.render_tool_call("read_file", {"path": "a.txt"}, ToolResult(ok=True, content="file contents"))
    out = buf.getvalue()
    assert "read_file" in out
    assert "file contents" in out


def test_render_tool_call_highlights_an_apply_patch_diff():
    term, buf = _make_terminal_io()
    patch = "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n"
    term.render_tool_call("apply_patch", {"path": "a.py", "patch": patch}, ToolResult(ok=True, content="Applied"))
    out = buf.getvalue()
    assert "apply_patch" in out
    assert "-old" in out
    assert "+new" in out


def test_render_tool_call_diffs_an_edit_file_call():
    term, buf = _make_terminal_io()
    term.render_tool_call(
        "edit_file",
        {"path": "a.py", "old_str": "foo = 1\n", "new_str": "foo = 2\n"},
        ToolResult(ok=True, content="Edited a.py"),
    )
    out = buf.getvalue()
    assert "-foo = 1" in out
    assert "+foo = 2" in out


def test_render_tool_call_falls_back_to_plain_content_without_a_diffable_call():
    term, buf = _make_terminal_io()
    term.render_tool_call("list_dir", {"path": "."}, ToolResult(ok=True, content="a.txt\nb.txt"))
    out = buf.getvalue()
    assert "a.txt" in out and "b.txt" in out


def test_spinner_is_a_usable_context_manager():
    term, _ = _make_terminal_io()
    with term.spinner("thinking…"):
        pass  # must not raise; the spinner's own animation is cosmetic


def test_render_plan_shows_the_label_and_plan_text():
    term, buf = _make_terminal_io()
    term.render_plan("Noah", "1. Read the file.\n2. Report back.")
    out = buf.getvalue()
    assert "Noah" in out
    assert "plan" in out
    assert "Read the file" in out


def test_render_plan_is_a_noop_for_empty_text():
    term, buf = _make_terminal_io()
    term.render_plan("Noah", "")
    assert buf.getvalue() == ""


def test_render_validation_shows_the_label_and_validation_text():
    term, buf = _make_terminal_io()
    term.render_validation("Noah", "Confirmed: the file was edited as intended, see line 12.")
    out = buf.getvalue()
    assert "Noah" in out
    assert "validation" in out
    assert "Confirmed" in out


def test_render_validation_is_a_noop_for_empty_text():
    term, buf = _make_terminal_io()
    term.render_validation("Noah", "")
    assert buf.getvalue() == ""


def test_begin_stream_writes_the_reply_marker():
    """A scrolling renderer can't wrap a reply that hasn't arrived yet, so it
    writes the marker when the first token shows up."""
    term, buf = _make_terminal_io()
    term.begin_stream("Noah")
    term.render("Hello")

    assert buf.getvalue() == "> Hello"


def test_begin_stream_does_not_print_the_label():
    term, buf = _make_terminal_io()
    term.begin_stream("Noah")

    assert "Noah" not in buf.getvalue()
