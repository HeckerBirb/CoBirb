"""Tests for the built-in tools and tool registry."""
from __future__ import annotations

import difflib


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


def _make_patch(old_text: str, new_text: str) -> str:
    """A real unified diff, generated the way a model actually would."""
    return "".join(
        difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True))
    )


def test_apply_patch_replaces_line_in_place(tmp_path):
    """Regression test: the old implementation ignored hunk position
    entirely, appending added lines to the end of the file instead of
    replacing the removed line in place."""
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    patch = _make_patch("alpha\nbeta\ngamma\n", "alpha\ndelta\ngamma\n")
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": patch})
    assert result.ok
    assert f.read_text() == "alpha\ndelta\ngamma\n"


def test_apply_patch_multiple_hunks(tmp_path):
    original = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    f = tmp_path / "a.txt"
    f.write_text(original)

    lines = original.splitlines()
    lines[1] = "CHANGED-2"
    lines[17] = "CHANGED-18"
    updated = "\n".join(lines) + "\n"

    patch = _make_patch(original, updated)
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": patch})
    assert result.ok
    assert f.read_text() == updated


def test_apply_patch_pure_insertion(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\n")
    patch = _make_patch("alpha\nbeta\n", "alpha\nnew\nbeta\n")
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": patch})
    assert result.ok
    assert f.read_text() == "alpha\nnew\nbeta\n"


def test_apply_patch_context_mismatch_fails_without_corrupting_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    # Patch generated against a different original -> context won't match.
    patch = _make_patch("alpha\nWRONG\ngamma\n", "alpha\ndelta\ngamma\n")
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": patch})
    assert not result.ok
    assert "does not apply" in result.content
    assert f.read_text() == "alpha\nbeta\ngamma\n"  # untouched


def test_apply_patch_preserves_missing_trailing_newline(tmp_path):
    # Hand-written rather than difflib-generated: when neither the old nor
    # new last line ends in "\n", difflib.unified_diff runs the "-"/"+"
    # lines together with no separator (a difflib quirk, not a patch-format
    # one — real diff tools mark this with "\ No newline at end of file"
    # and still newline-terminate each diff line in the patch text itself).
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta")  # no trailing newline
    patch = "@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n"
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": patch})
    assert result.ok
    assert f.read_text() == "alpha\ngamma"


def test_apply_patch_no_valid_hunks(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\n")
    result = _mk_tool(ApplyPatchTool).execute({"path": str(f), "patch": "not a real patch"})
    assert not result.ok
    assert result.error == "no_hunks"
    assert f.read_text() == "alpha\n"  # untouched


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


def test_glob_excludes_ignored_dirs_by_default(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "vendored.py").write_text("x")

    result = _mk_tool(GlobTool).execute({"pattern": str(tmp_path / "**" / "*.py")})
    assert result.ok
    assert "real.py" in result.content
    assert "vendored.py" not in result.content


def test_glob_include_ignored_opts_back_in(tmp_path):
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "vendored.py").write_text("x")

    result = _mk_tool(GlobTool).execute(
        {"pattern": str(tmp_path / "**" / "*.py"), "include_ignored": True}
    )
    assert result.ok
    assert "vendored.py" in result.content


def test_grep_excludes_ignored_dirs_by_default(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.txt").write_text("needle")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "vendored.txt").write_text("needle")

    result = _mk_tool(GrepTool).execute({"pattern": "needle", "path": str(tmp_path)})
    assert result.ok
    assert "real.txt" in result.content
    assert "vendored.txt" not in result.content


def test_grep_include_ignored_opts_back_in(tmp_path):
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "vendored.txt").write_text("needle")

    result = _mk_tool(GrepTool).execute(
        {"pattern": "needle", "path": str(tmp_path), "include_ignored": True}
    )
    assert result.ok
    assert "vendored.txt" in result.content


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




# --------------------------------------------------------------------------- #
# Relative-path resolution against each tool's configured cwd.
#
# Every test above this point passes an *absolute* path, which is exactly
# why this was broken for so long without any test catching it: self._cwd
# was stored on every tool but only ShellTool ever actually used it. A
# model normally emits relative paths ("note.txt", not the full absolute
# path), which used to resolve against the real OS process's cwd instead
# of whatever --cwd the tool was configured with — silently reading/
# writing the wrong location whenever the two differed. Found via a live
# integration test (see test_integration_ollama.py) that runs the
# orchestrator from a different directory than the target cwd, the way a
# real `cobirb --cwd <other dir>` invocation does.
# --------------------------------------------------------------------------- #
def test_read_file_relative_path_resolves_against_configured_cwd(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    result = ReadFileTool(cwd=str(tmp_path)).execute({"path": "a.txt"})
    assert result.ok
    assert result.content == "hello"


def test_write_file_relative_path_resolves_against_configured_cwd(tmp_path):
    result = WriteFileTool(cwd=str(tmp_path)).execute({"path": "new.txt", "content": "data"})
    assert result.ok
    assert (tmp_path / "new.txt").read_text() == "data"


def test_edit_file_relative_path_resolves_against_configured_cwd(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    result = EditFileTool(cwd=str(tmp_path)).execute({"path": "a.txt", "old_str": "old", "new_str": "new"})
    assert result.ok
    assert (tmp_path / "a.txt").read_text() == "new"


def test_apply_patch_relative_path_resolves_against_configured_cwd(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta\n")
    patch = _make_patch("alpha\nbeta\n", "alpha\ngamma\n")
    result = ApplyPatchTool(cwd=str(tmp_path)).execute({"path": "a.txt", "patch": patch})
    assert result.ok
    assert (tmp_path / "a.txt").read_text() == "alpha\ngamma\n"


def test_list_dir_relative_path_resolves_against_configured_cwd(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    result = ListDirTool(cwd=str(tmp_path)).execute({"path": "."})
    assert result.ok
    assert "a.txt" in result.content


def test_glob_relative_pattern_resolves_against_configured_cwd(tmp_path):
    (tmp_path / "a.py").write_text("x")
    result = GlobTool(cwd=str(tmp_path)).execute({"pattern": "*.py"})
    assert result.ok
    assert "a.py" in result.content


def test_grep_relative_path_resolves_against_configured_cwd(tmp_path):
    (tmp_path / "a.txt").write_text("needle")
    result = GrepTool(cwd=str(tmp_path)).execute({"pattern": "needle", "path": "."})
    assert result.ok
    assert "a.txt" in result.content


def test_grep_defaults_to_configured_cwd_when_no_path_given(tmp_path):
    (tmp_path / "a.txt").write_text("needle")
    result = GrepTool(cwd=str(tmp_path)).execute({"pattern": "needle"})
    assert result.ok
    assert "a.txt" in result.content
