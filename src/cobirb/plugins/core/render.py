"""Pure Rich renderable builders shared by every renderer CoBirb ships.

Nothing here prints, owns a ``Console``, or touches a terminal: each function
just *builds* a Rich renderable and returns it. That split exists because
CoBirb now has two renderers that must look identical — ``TerminalIO`` (the
scrolling, one-shot/programmatic path, which prints these to a ``Console``)
and the Textual TUI's ``TuiIO`` (interactive mode, which writes the very same
objects into a ``RichLog``). Keeping the panel/diff construction here means
the chrome is defined once instead of drifting between the two.
"""
from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a cli.py import cycle
    from ...cli import PluginsSummary
from rich.text import Text

# The "Noah" palette (v0.9.0) — the parrot's own colours. Defined once here
# rather than in the TUI, because this module's whole point is that
# TerminalIO and the TUI's TuiIO look identical (see the module docstring);
# a one-shot CLI reply gets the same marker and notice colours a full-screen
# session does. The TUI's own chrome (title bar, tabs, status bar, input
# border) is a separate concern, styled through a Textual Theme in
# tui/app.py — which imports these same five constants rather than repeating
# the hex.
TAIL_RED = "#E43A3A"  # the one accent: the user's own `>`, and (in the TUI) the active-tab
# underline, the input's focus border/cursor, and the footer's keybind letters.
CREST_GRAY = "#D3D6D9"  # what's meant to be read: all chat text, yours and the assistant's alike.
FEATHER_GRAY = "#83878C"  # the secondary layer: notices, dividers, the assistant's own `>`.
WING_GRAY = "#3D4045"  # structural chrome, in the TUI: title bar, status bar, tab-row divider.
BEAK_BLACK = "#1A1B1D"  # the canvas, in the TUI: scrollback background, input interior.

# Tail Red blended 14% into Beak Black — enough to read as "this line is
# different" without competing with the tail-red text sitting on top of it.
# R: .14*228 + .86*26 ≈ 54 (0x36); G: .14*58 + .86*27 ≈ 31 (0x1f);
# B: .14*58 + .86*29 ≈ 33 (0x21).
_ERROR_TINT = "#361F21"


def unified_diff(path: str, old_str: str, new_str: str) -> str:
    """A small unified diff between an ``edit_file`` call's old/new text, for
    display only — the tool itself still matches/replaces on ``old_str``
    verbatim; this is purely a rendering convenience."""
    diff = difflib.unified_diff(
        old_str.splitlines(keepends=True),
        new_str.splitlines(keepends=True),
        fromfile=path or "before",
        tofile=path or "after",
    )
    return "".join(diff)


def build_plan_panel(persona_name: str, text: str) -> Panel:
    """Plan mode's planning-phase reply — the plan the model is about to
    follow, shown before any tool runs."""
    return Panel(
        Markdown(text), title=f"{persona_name} · plan", title_align="left", border_style="cyan"
    )


def build_validation_panel(persona_name: str, text: str) -> Panel:
    """Plan mode's validate-phase report: whether/how the request was
    actually fulfilled, with references."""
    return Panel(
        Markdown(text), title=f"{persona_name} · validation", title_align="left", border_style="yellow"
    )


def build_history_divider(text: str) -> Rule:
    """A dim rule marking where a restored conversation ends and the live one
    begins, so replayed turns can't be mistaken for something that just
    happened."""
    return Rule(Text(text, style=FEATHER_GRAY), style=FEATHER_GRAY)


def build_tool_call_panel(
    tool_name: str, arguments: dict[str, Any], result: Any, *, replayed: bool = False
) -> Panel:
    """A tool call and its result, set apart from the conversation so the
    flow of "assistant reasons → calls a tool → sees the result" is visually
    distinct rather than buried in a single wall of text.

    ``apply_patch``/``edit_file`` calls get their diff syntax-highlighted;
    everything else shows the tool's own result text.

    ``replayed`` marks a call read back from a saved session. Sessions record
    a tool call's *output* but not whether it succeeded, so a replayed panel
    is drawn in a neutral colour rather than the green a live success gets —
    claiming success CoBirb never recorded would be worse than saying
    nothing.
    """
    ok = bool(getattr(result, "ok", True))
    content = str(getattr(result, "content", result))
    style = "dim" if replayed else ("green" if ok else "red")

    body: Any = Text(content)
    if tool_name == "apply_patch" and arguments.get("patch"):
        body = Syntax(arguments["patch"], "diff", theme="ansi_dark", background_color="default")
    elif tool_name == "edit_file" and "old_str" in arguments and "new_str" in arguments:
        diff_text = unified_diff(arguments.get("path", ""), arguments["old_str"], arguments["new_str"])
        if diff_text:
            body = Syntax(diff_text, "diff", theme="ansi_dark", background_color="default")

    return Panel(body, title=f"tool: {tool_name}", title_align="left", border_style=style)


def build_preview_panel(tool_name: str, preview: str) -> Panel:
    """What a tool call is about to do, shown while it is still a question.

    Diffs are syntax-highlighted, because approving a change you can read is
    the entire point of showing it — a wall of unhighlighted +/- lines is
    something people learn to click past.
    """
    looks_like_a_diff = preview.lstrip().startswith(("---", "@@", "diff "))
    body: Any = (
        Syntax(preview, "diff", theme="ansi_dark", background_color="default")
        if looks_like_a_diff
        else Text(preview)
    )
    return Panel(body, title=f"{tool_name} would change", title_align="left", border_style="yellow")


def build_error_panel(persona_name: str, text: str) -> Panel:
    """An error notice (a blocked tool call, a provider that fell over, a
    failed command or connection) shown in the conversation flow rather than
    on stderr.

    Interactive mode has nowhere to print a stray stderr line to — a
    full-screen app owns the whole terminal — so errors that the scrolling
    CLI writes with ``print()`` become a panel in the transcript instead.

    Bold text on a tinted background strip, not just tail-red text on the
    ordinary background: tail red is also the app's one accent (the active
    tab, the input focus border), so an error rendered the same way as those
    would read as more of the same chrome rather than as something wrong.
    """
    return Panel(
        Text(text, style=f"bold {TAIL_RED}"),
        title=persona_name,
        title_align="left",
        border_style=TAIL_RED,
        style=f"on {_ERROR_TINT}",
    )


def build_plugins_view(summary: "PluginsSummary") -> Group:
    """The TUI's Plugins tab content: active providers, registered tools,
    and any discovery/merge problems — a live view over the same
    ``cli.describe_plugins()`` snapshot ``_build_orchestrator`` itself uses
    to wire a real run, so what's shown here is exactly what a turn would
    actually use.
    """
    slots_table = Table(title="Active providers", show_header=True, header_style="bold", expand=True)
    slots_table.add_column("Slot", style="bold")
    slots_table.add_column("Provider")
    for kind in ("model", "io", "crypto"):
        slots_table.add_row(kind, summary.slots.get(kind, "core"))

    tools_table = Table(title="Registered tools", show_header=True, header_style="bold", expand=True)
    tools_table.add_column("Tool", style="bold")
    tools_table.add_column("Source")
    tools_table.add_column("Description")
    for tool in summary.tools:
        tools_table.add_row(tool.name, tool.source, tool.description)

    parts: list[Any] = [slots_table, tools_table]
    if summary.mcp_servers:
        # Named, not listed as tools: this snapshot deliberately starts no
        # servers, so their tools only appear in the table above once a turn
        # has built a real orchestrator.
        parts.append(
            Panel(
                Text(
                    ", ".join(summary.mcp_servers)
                    + "\n\nStarted with your first message; their tools then join the table "
                    "above as mcp__<server>__<tool>. Each one is a separate program that can "
                    "make its own network calls — see 'cobirb help mcp'."
                ),
                title="MCP servers configured",
                border_style="yellow",
            )
        )
    if summary.issues:
        lines = "\n".join(f"• {ident}: {message}" for ident, message in summary.issues.items())
        parts.append(Panel(Text(lines), title="Plugin issues", border_style="red"))
    return Group(*parts)


USER_MARKER_STYLE = f"bold {TAIL_RED}"
ASSISTANT_MARKER_STYLE = f"bold {FEATHER_GRAY}"
_MARKER = "> "
_INDENT = "  "


class MarkerPrefixed:
    """A renderable carrying a ``>`` marker on its first line only.

    Both halves of the conversation are marked the same way and told apart by
    the marker's *colour* rather than by a name or a box. That keeps the
    transcript a single readable column: a bordered panel around every reply
    turned each exchange into a framed block, which is heavy to read and
    (since the border is drawn per line) noisy to copy out of.

    The marker appears exactly once however many lines the content has —
    continuation lines are indented to sit under it — so a long reply reads
    as one message rather than as a stack of separate ones.

    The content is rendered first and then prefixed, rather than having the
    marker glued onto a string, so it can be any Rich renderable: an
    assistant reply stays real markdown, with its lists, emphasis and
    syntax-highlighted code blocks intact.
    """

    def __init__(self, renderable: Any, marker_style: str) -> None:
        self.renderable = renderable
        self.marker_style = marker_style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Render into a narrowed width so the indent doesn't push long lines
        # past the right edge and wrap them into a ragged extra column.
        width = max(options.max_width - len(_INDENT), 1)
        lines = console.render_lines(self.renderable, options.update_width(width), pad=False)
        marker_style = console.get_style(self.marker_style, default="none")
        for index, line in enumerate(lines):
            yield Segment(_MARKER, marker_style) if index == 0 else Segment(_INDENT)
            yield from line
            yield Segment("\n")


def build_assistant_message(text: str) -> MarkerPrefixed:
    """The model's finished reply: the same ``>`` the user's prompt gets, in a
    different colour, with the body still rendered as markdown."""
    return MarkerPrefixed(Markdown(text, style=CREST_GRAY), ASSISTANT_MARKER_STYLE)


def stream_marker() -> Text:
    """Just the marker, for a renderer that prints a stream token by token.

    A scrolling renderer can't wrap a reply that hasn't finished arriving, so
    it writes this once when the first token shows up and then the raw tokens
    after it. The result matches a finished reply's first line.
    """
    return Text(_MARKER, style=ASSISTANT_MARKER_STYLE)


def build_streamed_message(text: str) -> MarkerPrefixed:
    """A reply that arrived token by token.

    Deliberately *not* markdown: this is the text the user already watched
    appear in the streaming preview, so re-rendering it as markdown on
    completion would reflow and restyle what they just read. Same marker and
    colour as a finished reply, so the transcript still reads uniformly.
    """
    return MarkerPrefixed(Text(text, style=CREST_GRAY), ASSISTANT_MARKER_STYLE)


def build_user_message(text: str) -> Text:
    """The user's own prompt, marked so it can't be mistaken for the model's.

    Every other thing in the transcript is either a bordered panel (the
    assistant, tools, errors) or dim grey chatter (notices). A plain "You:"
    prefix read as neither, which made a long transcript hard to scan for
    where each exchange started. A bright ``>`` marker does the job the way a
    shell prompt does.

    The marker appears exactly **once** per message however many lines the
    message has: continuation lines are indented to sit under the first one
    instead of repeating it, so a pasted multi-line prompt reads as one block
    rather than as several separate turns.
    """
    lines = text.splitlines() or [""]
    body = Text()
    body.append(_MARKER, style=USER_MARKER_STYLE)
    body.append(lines[0], style=f"bold {CREST_GRAY}")
    for line in lines[1:]:
        body.append("\n" + _INDENT)
        body.append(line, style=f"bold {CREST_GRAY}")
    # A rule under the prompt, not between exchanges. Both markers are a `>`
    # in different colours, which is enough to tell whose turn a line is and
    # not enough to find the seam when scrolling — the complaint that prompted
    # this was "it's hard to tell where my message ends and the reply begins".
    # Closing the prompt answers exactly that: whatever follows the rule is
    # the answer to what precedes it.
    return Group(body, ExchangeRule())


STEER_MARKER_STYLE = "bold magenta"
_STEER_MARKER = "» "


def build_steer_message(text: str) -> Text:
    """A message sent to redirect a turn that is already running.

    Distinct from ``build_user_message``'s ``> `` marker (bold cyan, closed
    by a rule): a ``»`` in a different colour makes clear at a glance that
    this interjected into something already in progress, mid-transcript,
    rather than opening a fresh exchange — and it deliberately isn't followed
    by an ``ExchangeRule``, since it doesn't close one; whatever the model
    was already saying continues right below it.
    """
    lines = text.splitlines() or [""]
    body = Text()
    body.append(_STEER_MARKER, style=STEER_MARKER_STYLE)
    body.append(lines[0], style="italic")
    for line in lines[1:]:
        body.append("\n" + _INDENT)
        body.append(line, style="italic")
    return body


class ExchangeRule:
    """A dim rule at 80% width, indented to sit under the message text.

    Proportional rather than fixed, so it still reads as a divider in a narrow
    pane and does not run the whole way across a wide terminal — a full-width
    rule reads as a section break between exchanges, which is a different and
    louder thing than "the answer follows".

    A renderable rather than a pre-built string because the width is not known
    until it is drawn, and the transcript is resizable.
    """

    # Aligned with the text after the "> " marker, so the rule starts where
    # the words start rather than under the marker.
    INDENT = len(_INDENT)
    FRACTION = 0.8
    STYLE = FEATHER_GRAY

    def __rich_console__(self, console: "Console", options: "ConsoleOptions") -> "RenderResult":
        available = max(options.max_width - self.INDENT, 1)
        width = max(int(available * self.FRACTION), 8)
        yield Segment(" " * self.INDENT)
        yield Segment("─" * width, console.get_style(self.STYLE))
        yield Segment("\n")


def build_notice(text: str) -> Text:
    """A plain one-line notice (``/persona`` and ``/plan`` confirmations,
    the greeting). Not a panel: these are chatter about the session, not
    content from the model."""
    return Text(text, style=FEATHER_GRAY)
