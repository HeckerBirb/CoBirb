"""Tests for the built-in tools and tool registry."""
from __future__ import annotations

import difflib
import os
import threading
import time
from types import SimpleNamespace

import pytest

from cobirb.policy import AuditLog
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
    _shell_timeout,
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
    """Hunk position is honoured: an added line replaces the removed one in
    place, rather than being appended to the end of the file."""
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


def test_shell_reports_a_nonzero_exit_code():
    result = _mk_tool(ShellTool).execute({"command": "exit 3"})
    assert not result.ok
    assert result.meta["returncode"] == 3


def test_shell_times_out_a_command_that_never_exits():
    result = _mk_tool(ShellTool).execute({"command": "sleep 5", "timeout": 0.2})
    assert not result.ok
    assert result.error == "timeout"
    assert "timed out" in result.content


def test_shell_timeout_also_kills_a_backgrounded_descendant(tmp_path):
    """A command that backgrounds or forks something long-running (a game
    loop, a server — anything that doesn't exit on its own) must not leave
    that descendant orphaned when the immediate shell process is gone.
    Reporting "timed out" while something is still alive leaves it holding
    resources or a stray pipe open."""
    marker = tmp_path / "still-running"
    tool = _mk_tool(ShellTool)

    result = tool.execute(
        {
            # The backgrounded loop keeps touching `marker`'s mtime for a
            # while; if it's still alive after the tool call returns, the
            # process group wasn't actually killed.
            "command": f"(for i in $(seq 1 50); do touch {marker}; sleep 0.1; done) & sleep 0.2",
            "timeout": 0.05,
        }
    )

    assert result.error == "timeout"
    mtime_at_return = marker.stat().st_mtime if marker.exists() else None
    time.sleep(0.5)
    mtime_after_wait = marker.stat().st_mtime if marker.exists() else None
    assert mtime_at_return == mtime_after_wait  # nothing touched it again — the loop is dead


def test_shell_cancel_running_stops_an_in_flight_command():
    tool = _mk_tool(ShellTool)
    result_box: list[ToolResult] = []

    def run_it():
        result_box.append(tool.execute({"command": "sleep 30", "timeout": 300}))

    thread = threading.Thread(target=run_it)
    thread.start()
    # Give execute() a moment to actually start the process before cancelling.
    for _ in range(50):
        if tool._current_process is not None:
            break
        time.sleep(0.05)
    assert tool._current_process is not None

    cancelled = tool.cancel_running()
    thread.join(timeout=5)

    assert cancelled is True
    assert not thread.is_alive()
    [result] = result_box
    assert result.error == "cancelled"
    assert "cancelled" in result.content


def test_shell_cancel_running_is_a_noop_with_nothing_in_flight():
    assert _mk_tool(ShellTool).cancel_running() is False


def test_shell_cancel_running_is_a_noop_once_the_command_already_finished():
    tool = _mk_tool(ShellTool)
    tool.execute({"command": "echo hi"})
    assert tool.cancel_running() is False


def test_shell_reports_a_failure_to_even_start_the_command(monkeypatch):
    def fake_popen(*args, **kwargs):
        raise OSError("no such shell")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    result = _mk_tool(ShellTool).execute({"command": "echo hi"})
    assert not result.ok
    assert "no such shell" in result.content


def test_shell_kill_swallows_a_process_that_is_already_gone():
    """A race between checking a process is still alive and actually
    signalling it — os.killpg/getpgid on a pid that's already gone raises
    ProcessLookupError, which must be swallowed, not surfaced as a crash."""
    ShellTool._kill(SimpleNamespace(pid=2**30))  # not a real pid; must not raise


def test_shell_kill_falls_back_to_a_plain_kill_off_posix(monkeypatch):
    """No process groups outside POSIX — just kill the one process."""
    monkeypatch.setattr("cobirb.plugins.core.tools.os.name", "nt")
    killed = []
    ShellTool._kill(SimpleNamespace(pid=123, kill=lambda: killed.append(1)))
    assert killed == [1]


def test_registry_names_and_get():
    registry = ToolRegistry(".")
    assert "read_file" in registry.names()
    assert registry.is_known("read_file")
    assert registry.get("read_file").name() == "read_file"


def test_registry_custom_tool_extension():
    class MyTool(CobirbTool):
        NAME = "my_tool"

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
# path), which must resolve against whatever --cwd the tool was configured
# with rather than the real OS process's cwd — otherwise it silently reads
# and writes the wrong location whenever the two differ. Also covered live
# (see test_integration_ollama.py), which runs the orchestrator from a
# different directory than the target cwd, the way a real
# `cobirb --cwd <other dir>` invocation does.
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


def test_registry_rejects_a_tool_whose_name_is_not_a_method():
    """The SPI declares `name` as a method. A tool that uses a plain string
    is rejected at the boundary with a message saying so, rather than being
    tolerated and serializing a bound method into a request payload much
    later — which is exactly how that bug reached users once."""

    class StringNamedTool(CobirbTool):
        name = "oops"

        def description(self):
            return "d"

        def parameters(self):
            return {"type": "object", "properties": {}}

        def execute(self, arguments):
            return ToolResult(ok=True, content="ok")

    with pytest.raises(TypeError, match="must be a method"):
        ToolRegistry(".").register(StringNamedTool())


def test_grep_reports_an_invalid_regex_as_such(tmp_path):
    """re.error isn't an OSError, so grep has to catch it explicitly —
    otherwise it reaches the model as a generic "tool failed" with nothing
    actionable in it."""
    result = GrepTool(str(tmp_path)).execute({"pattern": "([unclosed"})

    assert not result.ok
    assert result.error == "bad_pattern"
    assert "valid regex" in result.content


def test_shell_timeout_is_clamped_and_defaults_sensibly():
    """The timeout is model-supplied, so it is bounded rather than trusted:
    an unbounded one would make a hung command outlive any way of stopping it
    short of cancelling the turn."""
    assert _shell_timeout(None) == 300
    assert _shell_timeout("not a number") == 300
    assert _shell_timeout(0) == 300
    assert _shell_timeout(-5) == 300
    assert _shell_timeout(10) == 10
    assert _shell_timeout(10_000) == 600


def test_shell_declares_the_timeout_it_actually_reads():
    """It was read from the arguments but absent from the schema, so the
    model could neither discover it nor know its bounds."""
    schema = ShellTool(".").parameters()
    assert "timeout" in schema["properties"]


def test_audit_log_failure_does_not_take_down_the_tool_call(tmp_path, capsys):
    """The trail is worth having, but not more than the work it is a trail
    of — an unwritable path is reported, not raised."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    log = AuditLog(str(blocker / "audit.jsonl"), enabled=True)

    log.append({"tool": "read_file"})

    assert "could not write the audit log" in capsys.readouterr().err


def test_a_large_file_is_paged_rather_than_merely_cut_off(tmp_path):
    """One *call* is bounded, because a single read taking half the context
    window is rarely what anyone wanted. The *file* is not: a call that stops
    early says what offset continues from, so an arbitrarily large file can be
    read in full. Truncating alone would leave a large file readable from the
    top and never finishable."""
    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {n}\n" for n in range(60_000)))
    tool = ReadFileTool(str(tmp_path))

    first = tool.execute({"path": "big.txt"})

    assert first.ok
    assert "line 0" in first.content
    assert "more follow" in first.content
    assert first.meta["next_offset"] > 1

    # ...and the offset it gave actually continues from where it stopped.
    second = tool.execute({"path": "big.txt", "offset": first.meta["next_offset"]})
    assert second.ok
    assert f"line {first.meta['next_offset'] - 1}\n" in second.content
    assert "line 0\n" not in second.content


def test_paging_reaches_the_end_of_a_large_file(tmp_path):
    """The whole point: not just "you can ask for more", but "you can finish"."""
    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {n}\n" for n in range(60_000)))
    tool = ReadFileTool(str(tmp_path))

    offset, calls, saw_last = 1, 0, False
    while calls < 20:
        calls += 1
        result = tool.execute({"path": "big.txt", "offset": offset})
        if "line 59999" in result.content:
            saw_last = True
        if result.meta.get("at_end", True):
            break
        offset = result.meta["next_offset"]

    assert saw_last
    assert calls < 20  # it terminates rather than paging forever


def test_an_explicit_line_range_is_honoured(tmp_path):
    (tmp_path / "a.py").write_text("".join(f"line {n}\n" for n in range(100)))

    result = ReadFileTool(str(tmp_path)).execute({"path": "a.py", "offset": 10, "limit": 3})

    assert "line 9\n" in result.content and "line 11\n" in result.content
    assert "line 12\n" not in result.content
    assert "lines 10-12" in result.content


def test_a_file_of_one_enormous_line_is_cut_and_says_why(tmp_path):
    """A minified bundle or single-line JSON cannot be paged by line offset,
    so it is cut with an explanation rather than silently returned whole —
    which would blow the budget the cap exists to protect."""
    (tmp_path / "bundle.js").write_text("x" * (300 * 1024))

    result = ReadFileTool(str(tmp_path)).execute({"path": "bundle.js"})

    assert result.ok
    assert len(result.content) < 300 * 1024
    assert "cannot page within a single line" in result.content


def test_read_file_leaves_an_ordinary_file_alone(tmp_path):
    (tmp_path / "small.py").write_text("print('hi')\n")

    result = ReadFileTool(str(tmp_path)).execute({"path": "small.py"})

    assert result.content == "print('hi')\n"


def test_grep_stops_at_the_match_cap_and_says_so(tmp_path):
    (tmp_path / "many.txt").write_text("needle\n" * 900)

    result = GrepTool(str(tmp_path)).execute({"pattern": "needle", "path": "."})

    assert result.ok
    assert len(result.content.splitlines()) < 900
    assert "stopped at" in result.content


# --------------------------------------------------------------------------- #
# Change previews.
#
# Approving a write you have not seen is approving the *tool*, not the
# change — and the change is the thing that matters. These run before
# approval, so by contract they must never raise and never alter anything.
# --------------------------------------------------------------------------- #
def test_edit_file_previews_the_diff_it_would_make(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")

    preview = EditFileTool(str(tmp_path)).preview(
        {"path": "a.py", "old_str": "return 1", "new_str": "return 2"}
    )

    assert "-    return 1" in preview
    assert "+    return 2" in preview


def test_write_file_previews_a_diff_against_what_is_there(tmp_path):
    (tmp_path / "a.txt").write_text("old\n")

    preview = WriteFileTool(str(tmp_path)).preview({"path": "a.txt", "content": "new\n"})

    assert "-old" in preview and "+new" in preview


def test_write_file_previews_a_new_file_as_a_new_file(tmp_path):
    preview = WriteFileTool(str(tmp_path)).preview({"path": "fresh.txt", "content": "a\nb\n"})

    assert "new file" in preview


def test_a_preview_never_touches_the_file(tmp_path):
    """It runs while the answer is still 'maybe'."""
    target = tmp_path / "a.txt"
    target.write_text("original\n")

    WriteFileTool(str(tmp_path)).preview({"path": "a.txt", "content": "replaced\n"})

    assert target.read_text() == "original\n"


def test_edit_preview_says_so_when_the_edit_would_not_apply(tmp_path):
    """Better to learn the old_str doesn't match while deciding than after."""
    (tmp_path / "a.py").write_text("something else\n")

    preview = EditFileTool(str(tmp_path)).preview(
        {"path": "a.py", "old_str": "not here", "new_str": "x"}
    )

    assert "will fail" in preview


def test_previews_do_not_raise_on_nonsense_arguments(tmp_path):
    """Contract: a broken preview costs the user a diff, never the ability to
    answer the approval question."""
    for tool in (WriteFileTool(str(tmp_path)), EditFileTool(str(tmp_path)), ApplyPatchTool(str(tmp_path))):
        assert isinstance(tool.preview({}), str)


def test_tools_that_have_nothing_to_preview_say_nothing(tmp_path):
    """read_file and shell already say everything in their arguments."""
    assert ReadFileTool(str(tmp_path)).preview({"path": "a"}) == ""
    assert ShellTool(str(tmp_path)).preview({"command": "ls"}) == ""


def test_an_ordinary_file_comes_back_whole_with_no_decoration(tmp_path):
    """The common case must be exactly what it was: no header, no footer, no
    line-range note to confuse an exact-match edit_file later."""
    (tmp_path / "small.py").write_text("print('hi')\n")

    result = ReadFileTool(str(tmp_path)).execute({"path": "small.py"})

    assert result.content == "print('hi')\n"


def test_reading_past_the_end_returns_nothing_rather_than_failing(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\n")

    result = ReadFileTool(str(tmp_path)).execute({"path": "a.py", "offset": 99})

    assert result.ok
    assert "end of file" in result.content


def test_a_nonsense_offset_falls_back_to_the_start(tmp_path):
    """Models supply the wrong type constantly; that shouldn't cost a turn."""
    (tmp_path / "a.py").write_text("one\ntwo\n")

    result = ReadFileTool(str(tmp_path)).execute({"path": "a.py", "offset": "somewhere"})

    assert result.ok
    assert "one" in result.content


# --------------------------------------------------------------------------- #
# Bounded results.
#
# Every tool that can produce an arbitrarily large answer has to bound it: the
# result becomes a session turn and a message in the next request, and an
# unbounded one costs memory, window and session size at once. read_file pages
# through a big file; these can't sensibly page, so they cap and say so.
# --------------------------------------------------------------------------- #
def test_list_dir_pages_a_huge_directory(tmp_path):
    """node_modules, a build output, a mail spool. Not rare."""
    big = tmp_path / "many"
    big.mkdir()
    for n in range(2500):
        (big / f"f{n}.txt").touch()

    result = ListDirTool(str(tmp_path)).execute({"path": "many"})

    assert result.ok
    assert result.meta["total"] == 2500
    assert not result.meta["at_end"]
    assert "more follow" in result.content
    assert len(result.content.splitlines()) < 1100

    rest = ListDirTool(str(tmp_path)).execute(
        {"path": "many", "offset": result.meta["next_offset"]}
    )
    assert rest.ok and rest.meta["total"] == 2500


def test_list_dir_marks_directories(tmp_path):
    (tmp_path / "adir").mkdir()
    (tmp_path / "afile.txt").touch()

    content = ListDirTool(str(tmp_path)).execute({"path": "."}).content

    assert "adir/" in content
    assert "afile.txt" in content and "afile.txt/" not in content


def test_a_small_directory_is_listed_plainly(tmp_path):
    """No footer, no counts — the common case reads as it always did."""
    (tmp_path / "a.txt").touch()
    (tmp_path / "b.txt").touch()

    assert ListDirTool(str(tmp_path)).execute({"path": "."}).content == "a.txt\nb.txt"


def test_an_empty_directory_says_so(tmp_path):
    (tmp_path / "empty").mkdir()

    assert "is empty" in ListDirTool(str(tmp_path)).execute({"path": "empty"}).content


def test_glob_pages_a_huge_match_set(tmp_path):
    for n in range(2500):
        (tmp_path / f"f{n}.py").touch()

    result = GlobTool(str(tmp_path)).execute({"pattern": "*.py"})

    assert result.ok
    assert result.meta["total"] == 2500
    assert "more follow" in result.content


def test_a_grep_match_in_a_minified_file_is_clipped(tmp_path):
    """A hit in a single-line bundle is a match of megabytes. Storing five
    hundred of those to truncate afterwards is how a grep turns into
    gigabytes of memory."""
    (tmp_path / "bundle.min.js").write_text("var x=1;" * 200_000)

    result = GrepTool(str(tmp_path)).execute({"pattern": "var x"})

    assert result.ok
    assert len(result.content) < 2000
    assert "chars]" in result.content  # the clip is announced


def test_grep_ignores_a_pruned_directory_without_descending_into_it(tmp_path):
    (tmp_path / ".gitignore").write_text("vendor/\n")
    vendor = tmp_path / "vendor" / "deep"
    vendor.mkdir(parents=True)
    (vendor / "x.txt").write_text("needle")
    (tmp_path / "real.txt").write_text("needle")

    content = GrepTool(str(tmp_path)).execute({"pattern": "needle"}).content

    assert "real.txt" in content
    assert "vendor" not in content


def test_shell_output_keeps_both_ends_when_it_overflows(tmp_path):
    """The head is what the command set out to say and the tail is usually
    where it went wrong; a build log cut to its first half hides the error."""
    import sys

    script = (
        'print("THE VERY FIRST LINE")\n'
        'for i in range(200000): print("noise", i)\n'
        'print("THE VERY LAST LINE")\n'
    )
    result = ShellTool(str(tmp_path)).execute(
        {"command": f'"{sys.executable}" -c \'{script}\''}
    )

    assert len(result.content) < 200_000
    assert "THE VERY FIRST LINE" in result.content
    assert "THE VERY LAST LINE" in result.content
    assert "omitted" in result.content


# --------------------------------------------------------------------------- #
# `~` expansion.
#
# `~` is neither absolute nor meaningfully relative, so joining it to the
# configured cwd made a directory *literally named* `~`: "write it to
# ~/notes/x.md" silently produced `<cwd>/~/notes/x.md` and reported success.
# `HOME` is redirected per test so none of this can touch a real one.
# --------------------------------------------------------------------------- #
def test_write_file_expands_a_leading_tilde_to_the_home_directory(tmp_path, monkeypatch):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = WriteFileTool(cwd=str(work)).execute({"path": "~/notes/x.md", "content": "hi"})

    assert result.ok
    assert (home / "notes" / "x.md").read_text() == "hi"
    assert not (work / "~").exists()


def test_read_file_expands_a_leading_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / "a.txt").write_text("from home")

    result = ReadFileTool(cwd=str(tmp_path / "elsewhere")).execute({"path": "~/a.txt"})

    assert result.ok
    assert result.content == "from home"


def test_a_tilde_inside_a_name_is_not_a_home_reference(tmp_path):
    """Only a *leading* `~` names a home directory. A file with one in the
    middle of its name is an ordinary relative path."""
    nested = tmp_path / "a~b"
    nested.mkdir()

    result = WriteFileTool(cwd=str(tmp_path)).execute({"path": "a~b/note.txt", "content": "x"})

    assert result.ok
    assert (nested / "note.txt").read_text() == "x"


# --------------------------------------------------------------------------- #
# `cd` does not survive a shell call, and says so.
#
# Each call is its own process, so `cd somewhere` moves a shell that exits
# immediately afterwards. Reported as a bare `exit=0` that is indistinguishable
# from success, it reads as a working directory change and the mistake only
# surfaces when something later runs in the wrong place.
# --------------------------------------------------------------------------- #
def test_a_directory_change_does_not_carry_into_the_next_call(tmp_path):
    """The reported behaviour: `cd` then `pwd` reports the original directory."""
    (tmp_path / "sub").mkdir()
    tool = ShellTool(cwd=str(tmp_path))

    tool.execute({"command": "cd sub"})
    after = tool.execute({"command": "pwd"})

    assert os.path.realpath(after.content.splitlines()[1]) == os.path.realpath(str(tmp_path))


def test_a_cd_only_command_says_it_did_not_persist(tmp_path):
    (tmp_path / "sub").mkdir()

    result = ShellTool(cwd=str(tmp_path)).execute({"command": "cd sub"})

    assert result.ok
    assert "next call starts in" in result.content


def test_a_cd_chained_with_real_work_is_not_flagged(tmp_path):
    """`cd build && make` is the thing that *does* work, so it must not be
    told it doesn't."""
    (tmp_path / "sub").mkdir()

    result = ShellTool(cwd=str(tmp_path)).execute({"command": "cd sub && pwd"})

    assert result.ok
    assert "next call starts in" not in result.content
    assert os.path.realpath(result.content.splitlines()[1]) == os.path.realpath(str(tmp_path / "sub"))


def test_a_quoted_cd_is_not_a_directory_change(tmp_path):
    """Only a command that really is a `cd`. `echo 'cd x'` prints a string."""
    result = ShellTool(cwd=str(tmp_path)).execute({"command": "echo 'cd x'"})

    assert result.ok
    assert "next call starts in" not in result.content


def test_shell_runs_in_the_cwd_argument_when_given(tmp_path):
    """The stateless alternative to `cd`: say where, per call."""
    (tmp_path / "sub").mkdir()

    result = ShellTool(cwd=str(tmp_path)).execute({"command": "pwd", "cwd": "sub"})

    assert result.ok
    assert os.path.realpath(result.content.splitlines()[1]) == os.path.realpath(str(tmp_path / "sub"))


def test_the_shell_cwd_argument_expands_a_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = ShellTool(cwd=str(tmp_path)).execute({"command": "pwd", "cwd": "~"})

    assert result.ok
    assert os.path.realpath(result.content.splitlines()[1]) == os.path.realpath(str(home))


def test_a_missing_shell_cwd_is_reported_as_such(tmp_path):
    """Rather than as whatever confusing error the shell would raise."""
    result = ShellTool(cwd=str(tmp_path)).execute({"command": "pwd", "cwd": "nope"})

    assert not result.ok
    assert "No such directory" in result.content


# --------------------------------------------------------------------------- #
# edit_file: one region, or a refusal that says why
# --------------------------------------------------------------------------- #
def _edit(tmp_path, text, **arguments):
    f = tmp_path / "a.py"
    f.write_text(text)
    result = EditFileTool(str(tmp_path)).execute({"path": "a.py", **arguments})
    return result, f.read_text()


def test_an_ambiguous_match_is_refused_and_names_every_line(tmp_path):
    """It used to edit the first match and report success — in a file of
    similar functions, a different function from the one meant."""
    text = "def a():\n    return 200\n\ndef b():\n    return 200\n"
    result, after = _edit(tmp_path, text, old_str="    return 200", new_str="    return 204")

    assert not result.ok
    assert "2 places" in result.content and "lines 2, 5" in result.content
    assert after == text


def test_replace_all_changes_every_occurrence(tmp_path):
    result, after = _edit(tmp_path, "x = 1\ny = 1\n", old_str="1", new_str="2", replace_all=True)

    assert result.ok and after == "x = 2\ny = 2\n"


def test_enough_context_makes_an_ambiguous_edit_unique(tmp_path):
    text = "def a():\n    return 200\n\ndef b():\n    return 200\n"
    result, after = _edit(tmp_path, text, old_str="def b():\n    return 200",
                          new_str="def b():\n    return 204")

    assert result.ok
    assert after == "def a():\n    return 200\n\ndef b():\n    return 204\n"
    assert "Lines 4–5 now read" in result.content


def test_trailing_whitespace_and_crlf_differences_still_match(tmp_path):
    result, after = _edit(tmp_path, "a = 1   \r\nb = 2\r\n", old_str="a = 1\nb = 2", new_str="a = 3\nb = 4")

    assert result.ok and "ignoring whitespace" in result.content
    assert after.startswith("a = 3\nb = 4")


def test_a_dropped_indent_is_matched_and_put_back(tmp_path):
    text = "class C:\n    def f(self):\n        return 1\n"
    result, after = _edit(tmp_path, text, old_str="def f(self):\n    return 1",
                          new_str="def f(self):\n    return 2")

    assert result.ok
    assert after == "class C:\n    def f(self):\n        return 2\n"


def test_a_miss_quotes_the_closest_region(tmp_path):
    text = "def total(items):\n    return sum(i.price for i in items)\n"
    result, after = _edit(tmp_path, text, old_str="return sum(i.cost for i in items)", new_str="x")

    assert not result.ok
    assert "not found" in result.content
    assert "line 2" in result.content and "i.price" in result.content
    assert after == text


def test_the_preview_says_an_ambiguous_edit_will_fail(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n")

    preview = EditFileTool(str(tmp_path)).preview({"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"})

    assert "will fail" in preview and "2 places" in preview
