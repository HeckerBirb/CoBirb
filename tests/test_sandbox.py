"""Tests for the shell sandbox."""
from __future__ import annotations

import os
import socket

import pytest

from cobirb import sandbox
from cobirb.plugins.core.tools import ShellTool
from cobirb.policy import Policy

needs_bwrap = pytest.mark.skipif(sandbox.find_bwrap() is None, reason="bubblewrap not usable here")


def test_mode_parsing_is_forgiving():
    assert sandbox.from_config(None, ".").mode == sandbox.DEFAULT_MODE
    assert sandbox.from_config("AUTO", ".").mode == "auto"
    assert sandbox.from_config({"mode": "off"}, ".").mode == "off"
    assert sandbox.from_config("sideways", ".").mode == sandbox.DEFAULT_MODE


def test_a_hidden_path_that_contains_the_project_is_not_masked(tmp_path):
    (tmp_path / "proj").mkdir()
    box = sandbox.Sandbox(mode="ask", project=str(tmp_path / "proj"), hidden=[str(tmp_path)], bwrap="bwrap")

    argv = box.argv("true", str(tmp_path / "proj"))
    masked = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--tmpfs"]
    assert os.path.realpath(tmp_path) not in masked


def test_without_bubblewrap_nothing_is_contained_or_auto_approved(tmp_path):
    box = sandbox.Sandbox(mode="auto", project=str(tmp_path), bwrap=None)

    assert not box.active and not box.auto_approve


def test_auto_mode_approves_contained_commands_only():
    policy = Policy()
    policy.sandbox_auto = True

    assert policy.is_allowed("shell", {"command": "pytest -q $(ls)"})
    assert not policy.is_allowed("shell", {"command": "curl example.com", "unsandboxed": True})


def test_ask_mode_changes_nothing_about_approval():
    assert not Policy().is_allowed("shell", {"command": "pytest -q"})


def _shell(tmp_path, mode="auto"):
    tool = ShellTool(str(tmp_path))
    tool.sandbox = sandbox.from_config(mode, str(tmp_path))
    return tool


@needs_bwrap
def test_a_contained_command_can_write_the_project_but_nothing_else(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    tool = _shell(project)

    inside = tool.execute({"command": "echo hi > made.txt && cat made.txt"})
    outside = tool.execute({"command": f"echo x > {tmp_path / 'escaped.txt'}"})

    assert inside.content.startswith("exit=0") and (project / "made.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert "sandboxed" in inside.content


@needs_bwrap
def test_a_contained_command_has_no_network(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        result = _shell(tmp_path).execute(
            {"command": f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2)\""}
        )
    finally:
        listener.close()

    assert not result.content.startswith("exit=0")


@needs_bwrap
def test_credentials_are_hidden_from_a_contained_command(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("SECRET")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()

    result = _shell(project).execute({"command": f"cat {home}/.ssh/id_ed25519"})

    assert "SECRET" not in result.content


_CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"


@needs_bwrap
@pytest.mark.skipif(not (os.path.isdir(sandbox.WSL_INTEROP_DIR) and os.path.exists(_CMD_EXE)),
                    reason="not WSL with Windows interop")
def test_a_contained_command_cannot_start_a_windows_program(tmp_path):
    """On WSL a Windows program started from the sandbox ran outside it."""
    result = _shell(tmp_path).execute({"command": f"{_CMD_EXE} /c ver"})

    assert "Microsoft Windows" not in result.content


@needs_bwrap
def test_unsandboxed_runs_outside(tmp_path):
    target = tmp_path.parent / f"outside-{os.getpid()}.txt"
    try:
        _shell(tmp_path).execute({"command": f"echo x > {target}", "unsandboxed": True})
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_cobirbs_own_home_is_hidden_wherever_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path / "elsewhere"))
    from cobirb import paths

    assert paths.cobirb_dir() in sandbox.from_config("auto", str(tmp_path)).hidden


@needs_bwrap
def test_the_projects_history_cannot_be_changed_from_inside(tmp_path):
    import subprocess

    project = tmp_path / "proj"
    project.mkdir()
    git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (project / "a.txt").write_text("1")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True)
    (project / "a.txt").write_text("2")
    tool = _shell(project)

    status = tool.execute({"command": "git status --porcelain"})
    commit = tool.execute({"command": "git -c user.name=x -c user.email=x@x commit -qam two"})

    assert status.content.startswith("exit=0") and "a.txt" in status.content
    assert not commit.content.startswith("exit=0")
    log = subprocess.run([*git, "log", "--oneline"], capture_output=True, text=True).stdout
    assert len(log.strip().splitlines()) == 1
