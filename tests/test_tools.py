"""Tests for the built-in tools and tool registry."""
from __future__ import annotations

import pytest

from cobirb.plugins.core.tools import (
    ApplyPatchTool,
    CobirbTool,
    EditFileTool,
    GrepTool,
    GlobTool,
    ListDirTool,
    ReadFileTool,
    ShellTool,
    ToolRegistry,
    ToolResult,
    WriteFileTool,
)
from cobirb.plugins.core.tools import _first_word


def _mk_tool(cls):
    return cls()


def test_read_file_success(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    result = _mk_tool(ReadFileTool).execute({"path": str(f)})
    assert result.ok
    assert result.content == "hello"


def test_read_file_error(tmp_path):
    result = _mk_tool(ReadFileTool).execute({"path": str(tmp_path / "missing")})
    assert not result.ok
    assert "Could not read" in result.content


def test_write_file(tmp_path):
    result = _mk_tool(WriteFileTool).execute(
        {"path": str(tmp_path / "sub" / "new.txt"), "content": "data"}
    )
    assert result.ok
    assert (tmp_path / "sub" / "new.txt").read_text() == "data"


def test_edit_file_success(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("old\nkeep")
    result = _mk_tool(EditFileTool).execute({"path": str(f), "old_str": "old", "new_str": "new"})
    assert result.ok
    assert f.read_text() == "new\nkeep"


def test_edit_file_no_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("keep")
    result = _mk_tool(EditFileTool).execute(
        {"path": str(f), "old_str": "missing", "new_str": "x"}
    )
    assert not result.ok
    assert "not found" in result.content


def test_apply_patch(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\n")
    patch = "-beta\n+gamma\n"
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": patch})
    assert result.ok
    assert f.read_text() == "alpha\nbeta\ngamma"


def test_glob(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "c.txt").write_text("x")
    result = _mk_tool(GlobTool).execute({"pattern": str(tmp_path / "*.py")})
    assert result.ok
    assert "a.py" in result.content and "b.py" in result.content and "c.txt" not in result.content


def test_grep(tmp_path):
    (tmp_path / "a.txt").write_text("foo bar\nbaz")
    result = _mk_tool(GrepTool).execute({"pattern": "foo", "path": str(tmp_path)})
    assert result.ok
    assert "a.txt" in result.content


def test_list_dir(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    result = _mk_tool(ListDirTool).execute({"path": str(tmp_path)})
    assert result.ok
    assert "a.txt" in result.content
    assert "sub" in result.content


def test_shell_executes(tmp_path):
    result = _mk_tool(ShellTool).execute({"command": "echo hi"})
    assert result.ok
    assert "hi" in result.content


def test_registry_names_and_get():
    registry = ToolRegistry(".")
    assert "read_file" in registry.names()
    assert registry.is_known("read_file")
    assert registry.get("read_file").name == "read_file"


def test_registry_custom_tool_extension():
    class MyTool(CobirbTool):
        name = "my_tool"

        def description(self):
            return "custom"

        def parameters(self):
            return {"type": "object", "properties": {}}

        def execute(self, arguments):
            return ToolResult(ok=True, content="ok")

    registry = ToolRegistry(".")
    registry.register(MyTool())
    assert registry.is_known("my_tool")
    assert "my_tool" in registry.names()


def test_first_word():
    assert _first_word("git status") == "git"
    assert _first_word("bash -n") == "bash"
    assert _first_word("a; b") == "a"
