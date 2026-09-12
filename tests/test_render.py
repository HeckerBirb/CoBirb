"""Tests for ``plugins/core/render.py`` — the Rich renderable builders shared
by ``TerminalIO`` (scrolling stdout) and the TUI's ``TuiIO`` (a ``RichLog``).

These exist because ``test_io.py`` only ever asserts on what a ``Console``
*printed*. That was fine while ``TerminalIO`` was the one renderer, but the
builders now have a second consumer that never touches a console, so the
shape of what they return is a contract in its own right: a Panel handed to
``RichLog.write()`` has to be a Panel, not something that only happens to
look right once printed.
"""
from __future__ import annotations

import pytest
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from cobirb.plugins.core import render
from cobirb.typing.spi import ToolResult


def _printed(renderable) -> str:
    """Render to a throwaway console, so a test can assert on visible text
    without a real terminal."""
    console = Console(file=None, width=200, record=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


# --------------------------------------------------------------------------- #
# unified_diff
# --------------------------------------------------------------------------- #
def test_unified_diff_shows_the_removed_and_added_lines():
    diff = render.unified_diff("note.txt", "old line\n", "new line\n")

    assert "-old line" in diff
    assert "+new line" in diff
    assert "note.txt" in diff


def test_unified_diff_labels_unnamed_text_before_and_after():
    diff = render.unified_diff("", "a\n", "b\n")

    assert "before" in diff
    assert "after" in diff


def test_unified_diff_is_empty_when_nothing_changed():
    assert render.unified_diff("note.txt", "same\n", "same\n") == ""


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
def test_header_panel_carries_persona_model_and_cwd():
    panel = render.build_header_panel("Noah", "ollama/llama3.1", "/work")

    assert isinstance(panel, Panel)
    printed = _printed(panel)
    assert "Noah" in printed
    assert "ollama/llama3.1" in printed
    assert "/work" in printed
    assert "session:" not in printed


def test_header_panel_adds_the_session_path_only_when_given():
    printed = _printed(render.build_header_panel("Noah", "m", "/work", "/tmp/s.json"))

    assert "session: /tmp/s.json" in printed


def test_header_panel_says_so_when_no_model_is_configured():
    assert "(no model configured)" in _printed(render.build_header_panel("Noah", "", "/work"))


@pytest.mark.parametrize(
    "builder,expected_title",
    [
        (render.build_answer_panel, "Noah"),
        (render.build_plan_panel, "Noah · plan"),
        (render.build_validation_panel, "Noah · validation"),
    ],
)
def test_the_reply_panels_are_titled_markdown(builder, expected_title):
    """All three render the model's own words, so all three go through
    Markdown — a plain Text body would show raw ``**bold**`` and list
    markers instead of formatting them."""
    panel = builder("Noah", "**bold** text")

    assert isinstance(panel, Panel)
    assert isinstance(panel.renderable, Markdown)
    assert panel.title == expected_title
    assert "bold text" in _printed(panel)


# --------------------------------------------------------------------------- #
# Tool calls
# --------------------------------------------------------------------------- #
def test_tool_call_panel_shows_the_name_and_result_content():
    panel = render.build_tool_call_panel(
        "read_file", {"path": "note.txt"}, ToolResult(ok=True, content="banana")
    )

    assert panel.title == "tool: read_file"
    assert isinstance(panel.renderable, Text)
    assert "banana" in _printed(panel)


def test_tool_call_panel_borders_a_failure_in_red_and_a_success_in_green():
    ok = render.build_tool_call_panel("read_file", {}, ToolResult(ok=True, content="fine"))
    failed = render.build_tool_call_panel("read_file", {}, ToolResult(ok=False, content="nope"))

    assert ok.border_style == "green"
    assert failed.border_style == "red"


def test_tool_call_panel_syntax_highlights_an_apply_patch_diff():
    patch = "--- a\n+++ b\n@@\n-old\n+new\n"
    panel = render.build_tool_call_panel(
        "apply_patch", {"patch": patch}, ToolResult(ok=True, content="applied")
    )

    assert isinstance(panel.renderable, Syntax)
    assert panel.renderable.lexer.name.lower() == "diff"


def test_tool_call_panel_builds_a_diff_for_an_edit_file_call():
    panel = render.build_tool_call_panel(
        "edit_file",
        {"path": "note.txt", "old_str": "old line\n", "new_str": "new line\n"},
        ToolResult(ok=True, content="edited"),
    )

    assert isinstance(panel.renderable, Syntax)
    assert "-old line" in panel.renderable.code


def test_tool_call_panel_falls_back_to_plain_content_when_there_is_no_diff():
    """An edit_file call whose old and new text are identical produces an
    empty diff — showing an empty syntax block would be worse than showing
    the tool's own result."""
    panel = render.build_tool_call_panel(
        "edit_file",
        {"path": "note.txt", "old_str": "same\n", "new_str": "same\n"},
        ToolResult(ok=True, content="no change"),
    )

    assert isinstance(panel.renderable, Text)
    assert "no change" in _printed(panel)


def test_tool_call_panel_accepts_a_result_that_is_not_a_ToolResult():
    """The orchestrator passes ToolResults, but this is reached via a
    duck-typed hook — a bare string must not crash the renderer."""
    panel = render.build_tool_call_panel("shell", {}, "raw output")

    assert "raw output" in _printed(panel)
    assert panel.border_style == "green"  # nothing said it failed


# --------------------------------------------------------------------------- #
# Notices and errors
# --------------------------------------------------------------------------- #
def test_error_panel_shows_the_persona_and_the_message():
    panel = render.build_error_panel("Noah", "blocked — nope")

    assert panel.title == "Noah"
    assert panel.border_style == "red"
    assert "blocked" in _printed(panel)


def test_notice_is_plain_text_not_a_panel():
    """Notices are chatter about the session (a /plan toggle, a persona
    switch), not content from the model, so they deliberately don't get the
    framing a reply does."""
    notice = render.build_notice("Plan mode: on.")

    assert isinstance(notice, Text)
    assert notice.plain == "Plan mode: on."


# --------------------------------------------------------------------------- #
# Plugins tab view (the TUI's Plugins tab)
# --------------------------------------------------------------------------- #
def test_plugins_view_lists_the_slots_and_tools():
    from cobirb.cli import PluginsSummary, ToolInfo

    summary = PluginsSummary(
        slots={"model": "core", "io": "core", "crypto": "my-crypto"},
        tools=[
            ToolInfo(name="read_file", description="Read a file", source="core"),
            ToolInfo(name="extra_tool", description="A plugin tool", source="plugin"),
        ],
        issues={},
    )

    printed = _printed(render.build_plugins_view(summary))

    assert "read_file" in printed
    assert "extra_tool" in printed
    assert "my-crypto" in printed
    assert "Plugin issues" not in printed  # nothing to report


def test_plugins_view_shows_issues_only_when_there_are_any():
    from cobirb.cli import PluginsSummary

    summary = PluginsSummary(issues={"tool:broken": "it exploded"})

    printed = _printed(render.build_plugins_view(summary))

    assert "Plugin issues" in printed
    assert "it exploded" in printed
