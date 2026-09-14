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

import io

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
    without a real terminal.

    ``force_terminal=False, no_color=True`` (matching ``test_io.py``'s own
    console helper) makes this deterministic regardless of how the test
    itself is invoked: left to auto-detect, Rich enables ANSI styling
    whenever the real stdout it's attached to is an actual tty — e.g. under
    ``pytest -s``, which disables output capturing — and a styled run wraps
    "**bold**" as ``\x1b[1mbold\x1b[0m``, splitting it away from the plain
    text right after it and breaking a plain substring assertion like
    ``"bold text" in printed`` even though nothing about the renderable
    itself changed.
    """
    console = Console(file=None, width=200, record=True, force_terminal=False, no_color=True)
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
@pytest.mark.parametrize(
    "builder,expected_title",
    [
        (render.build_plan_panel, "Noah · plan"),
        (render.build_validation_panel, "Noah · validation"),
    ],
)
def test_the_plan_mode_panels_are_titled_markdown(builder, expected_title):
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
    assert panel.border_style == render.TAIL_RED
    assert "blocked" in _printed(panel)


def test_error_panel_gets_a_tinted_background_not_just_coloured_text():
    """An error must read as visually distinct from the ordinary tail-red
    accent used elsewhere (the active tab, the input focus border) — a
    background fill, not just coloured text on the usual surface."""
    panel = render.build_error_panel("Noah", "blocked — nope")

    assert panel.style not in (None, "", "none")
    assert "on " in str(panel.style)


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


# --------------------------------------------------------------------------- #
# The conversation as one marked column, rather than replies in titled panels.
# --------------------------------------------------------------------------- #
def _plain(renderable, width: int = 70) -> str:
    """Render to plain text, the way it would look on screen."""
    console = Console(width=width, file=io.StringIO(), force_terminal=False)
    console.print(renderable)
    return console.file.getvalue()


def test_a_reply_carries_the_same_marker_the_prompt_does():
    assert _plain(render.build_assistant_message("Hello there.")).startswith("> Hello there.")


def test_the_reply_marker_is_a_different_colour_from_the_users():
    """The two halves of the conversation look alike on purpose and are told
    apart by colour, so the colours must not be the same."""
    assert render.USER_MARKER_STYLE != render.ASSISTANT_MARKER_STYLE


def test_a_reply_is_marked_once_and_its_later_lines_are_indented_under_it():
    lines = _plain(render.build_assistant_message("First line.\n\nSecond line.")).splitlines()

    assert lines[0].startswith("> First line.")
    assert sum(1 for line in lines if line.startswith(">")) == 1
    assert [line for line in lines if line.strip() == "Second line."][0].startswith("  ")


def test_a_reply_is_still_rendered_as_markdown():
    """Marking the reply must not cost the formatting a panel used to give
    it — code blocks and lists are the reason markdown is rendered at all."""
    out = _plain(render.build_assistant_message("Try:\n\n- **one**\n- two"))

    assert "•" in out  # a rendered bullet, not a literal "-"
    assert "**" not in out  # emphasis consumed, not printed


def test_a_streamed_reply_is_marked_but_not_rerendered_as_markdown():
    """It is the text the user already watched appear; re-rendering it on
    completion would reflow what they just read."""
    out = _plain(render.build_streamed_message("- literal **text**"))

    assert out.startswith("> - literal **text**")


def test_replies_do_not_carry_a_persona_label():
    out = _plain(render.build_assistant_message("Hello."))

    assert "Noah" not in out and "CoBirb" not in out


def test_a_marked_message_never_runs_past_the_console_width():
    """The indent has to come out of the content's width, not be added on
    top of it, or long lines wrap into a ragged extra column."""
    long_text = "word " * 200
    for line in _plain(render.build_assistant_message(long_text), width=40).splitlines():
        assert len(line) <= 40


def test_a_replayed_tool_panel_does_not_claim_success():
    """Sessions record a tool call's output but not whether it worked, so a
    replayed panel must not be drawn in the green a live success gets."""
    live = render.build_tool_call_panel("shell", {}, ToolResult(ok=True, content="done"))
    replayed = render.build_tool_call_panel("shell", {}, "done", replayed=True)

    assert live.border_style == "green"
    assert replayed.border_style == "dim"
    assert "done" in _printed(replayed)


def test_a_replayed_tool_panel_still_shows_its_diff():
    panel = render.build_tool_call_panel(
        "edit_file",
        {"path": "a.py", "old_str": "foo = 1\n", "new_str": "foo = 2\n"},
        "Edited",
        replayed=True,
    )
    printed = _printed(panel)

    assert "-foo = 1" in printed and "+foo = 2" in printed


def test_the_history_divider_names_what_was_restored():
    assert "4 earlier turns" in _printed(render.build_history_divider("4 earlier turns"))
