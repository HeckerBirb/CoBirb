"""Pure Rich renderable builders shared by every renderer CoBirb ships.

Nothing here prints, owns a ``Console``, or touches a terminal: each function
just *builds* a Rich renderable and returns it. That split exists because
CoBirb now has two renderers that must look identical — ``TerminalIO`` (the
scrolling, one-shot/programmatic path, which prints these to a ``Console``)
and the Textual TUI's ``TuiIO`` (interactive mode, which writes the very same
objects into a ``RichLog``). Keeping the panel/diff construction here means
the chrome is defined once instead of drifting between the two.

See DESIGN.md §11 and PLUGIN_SPEC.md's I/O adapter section.
"""
from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a cli.py import cycle
    from ...cli import PluginsSummary
from rich.text import Text


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


def build_header_panel(
    persona_name: str, model_name: str, cwd: str, session_path: str | None = None
) -> Panel:
    """A compact banner: who's speaking, on what model, where.

    Shown once at the top of a session (and again after a ``/persona``
    switch) by ``TerminalIO``; written into the transcript at mount time by
    the TUI.
    """
    lines = [f"[bold]{persona_name}[/] · {model_name or '(no model configured)'} · {cwd}"]
    if session_path:
        lines.append(f"session: {session_path}")
    return Panel("\n".join(lines), expand=False, border_style="cyan")


def build_answer_panel(persona_name: str, text: str) -> Panel:
    """The assistant's finished reply, as rendered markdown in a panel."""
    return Panel(Markdown(text), title=persona_name, title_align="left", border_style="magenta")


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


def build_tool_call_panel(tool_name: str, arguments: dict[str, Any], result: Any) -> Panel:
    """A tool call and its result, set apart from the conversation so the
    flow of "assistant reasons → calls a tool → sees the result" is visually
    distinct rather than buried in a single wall of text.

    ``apply_patch``/``edit_file`` calls get their diff syntax-highlighted;
    everything else shows the tool's own result text.
    """
    ok = bool(getattr(result, "ok", True))
    content = str(getattr(result, "content", result))
    style = "green" if ok else "red"

    body: Any = Text(content)
    if tool_name == "apply_patch" and arguments.get("patch"):
        body = Syntax(arguments["patch"], "diff", theme="ansi_dark", background_color="default")
    elif tool_name == "edit_file" and "old_str" in arguments and "new_str" in arguments:
        diff_text = unified_diff(arguments.get("path", ""), arguments["old_str"], arguments["new_str"])
        if diff_text:
            body = Syntax(diff_text, "diff", theme="ansi_dark", background_color="default")

    return Panel(body, title=f"tool: {tool_name}", title_align="left", border_style=style)


def build_error_panel(persona_name: str, text: str) -> Panel:
    """An error notice (a blocked tool call, a provider that fell over)
    shown in the conversation flow rather than on stderr.

    Interactive mode has nowhere to print a stray stderr line to — a
    full-screen app owns the whole terminal — so errors that the scrolling
    CLI writes with ``print()`` become a panel in the transcript instead.
    """
    return Panel(Text(text), title=persona_name, title_align="left", border_style="red")


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
    if summary.issues:
        lines = "\n".join(f"• {ident}: {message}" for ident, message in summary.issues.items())
        parts.append(Panel(Text(lines), title="Plugin issues", border_style="red"))
    return Group(*parts)


def build_notice(text: str) -> Text:
    """A plain one-line notice (``/persona`` and ``/plan`` confirmations,
    the greeting). Not a panel: these are chatter about the session, not
    content from the model."""
    return Text(text, style="dim")
