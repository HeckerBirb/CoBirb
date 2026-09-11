"""Tests for the terminal I/O adapter, in particular the approval prompt."""
from __future__ import annotations

import pytest

from cobirb.plugins.core.io import TerminalIO


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
