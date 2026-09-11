"""Tests for the default-deny permission layer and audit log."""
from __future__ import annotations

import pytest

from cobirb.policy import AuditLog, Policy, _first_word, PermissionError


def test_default_deny_unknown_tool():
    policy = Policy()
    assert not policy.is_allowed("shell")  # not explicitly allowed


def test_explicit_allow():
    policy = Policy(allowed={"read_file", "shell"})
    assert policy.is_allowed("read_file")
    assert policy.is_allowed("shell")  # no command => allowed as-is
    # With a command, shell scope is narrowed by the first word.
    assert not policy.is_allowed("shell", {"command": "git status"})


def test_denied_always_wins():
    policy = Policy(allowed={"shell"}, denied={"shell"})
    assert not policy.is_allowed("shell", {"command": "ls"})


def test_shell_narrowed_by_first_word():
    policy = Policy(allowed={"git"})
    # 'shell' itself isn't allowed, but its first-word scope is.
    assert policy.is_allowed("shell", {"command": "git status"})
    # A different first word is not covered by the 'git' scope.
    assert not policy.is_allowed("shell", {"command": "python script.py"})


def test_allow_narrows_shell_to_exact_prefix_not_bare_binary():
    """Allowing a multi-word command must not implicitly trust the bare
    binary for any other arguments — that would defeat the point of
    narrowing (see todo-list.md's python/bash motivation)."""
    policy = Policy()
    policy.allow("shell", "python -m cobirb")
    assert policy.is_allowed("shell", {"command": "python -m cobirb"})
    assert not policy.is_allowed("shell", {"command": "rm -rf /"})
    # The bare binary, or a different narrow use of it, must stay denied.
    assert not policy.is_allowed("shell", {"command": "python -c 'import os; os.system(\"evil\")'"})
    assert not policy.is_allowed("shell", {"command": "python"})


def test_allow_without_command_adds_full_name():
    policy = Policy()
    policy.allow("read_file")
    assert policy.is_allowed("read_file")


def test_is_denied_explicit():
    policy = Policy()
    policy.deny("shell")
    assert policy.is_denied("shell")
    assert not policy.is_allowed("shell", {"command": "ls"})


def test_audit_log_append_only(tmp_path):
    log_path = str(tmp_path / "audit.jsonl")
    audit = AuditLog(log_path)
    policy = Policy(audit=audit)
    policy.log("read_file", {"path": "x.py"}, cwd="/tmp")
    with open(log_path, "r", encoding="utf-8") as fh:
        line = fh.read().strip()
    assert '"tool": "read_file"' in line
    assert '"cwd": "/tmp"' in line


def test_first_word_splitting():
    assert _first_word("git status") == "git"
    assert _first_word("python -m cobirb") == "python"
    assert _first_word("bash -n") == "bash"
    assert _first_word("a; b") == "a"
    assert _first_word("   ") == ""


def test_allow_all_core_tools(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    policy = Policy(audit=audit)
    policy.allow_all_core_tools()
    for name in ("read_file", "write_file", "edit_file", "apply_patch", "glob", "grep", "list_dir"):
        assert policy.is_allowed(name)
    # 'shell' is allowed, but its scope is narrowed to the safe default set.
    assert policy.is_allowed("shell", {"command": "git status"})
    assert policy.is_allowed("shell", {"command": "python --version"})
    assert policy.is_allowed("shell", {"command": "python -m pytest tests/"})
    assert not policy.is_allowed("shell", {"command": "rm -rf /"})


def test_allow_all_core_tools_does_not_trust_bare_python(tmp_path):
    """Regression test: bare `python` must never be globally allowed by the
    default policy, since `python -c '...'`/`python -m pip install ...` can
    run or fetch arbitrary code — only specific narrow invocations are
    trusted by default (see todo-list.md)."""
    policy = Policy(audit=AuditLog(str(tmp_path / "audit.jsonl")))
    policy.allow_all_core_tools()
    assert not policy.is_allowed("shell", {"command": "python"})
    assert not policy.is_allowed("shell", {"command": "python -c 'print(1)'"})
    assert not policy.is_allowed("shell", {"command": "python -m pip install anything"})
