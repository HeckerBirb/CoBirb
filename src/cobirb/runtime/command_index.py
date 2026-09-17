"""Which slash commands exist here, and which one a half-typed ``/`` means.

Paired with ``tui/command_picker.py`` exactly as ``runtime/mentions.py`` is
paired with ``tui/mention_picker.py``: the listing and the ranking know
nothing about terminals, so both can be tested without a running app.

The built-in commands are passed *in* rather than imported. They live in
``tui/slash_commands.py``, and a module under ``runtime/`` reaching into the
TUI would invert the dependency every other module here observes — front-ends
use ``runtime``, never the other way round.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .custom_commands import discover_commands
from .mentions import subsequence_score


@dataclass(frozen=True)
class CommandEntry:
    """One command the picker can offer."""

    name: str  # without the leading slash
    description: str
    # "" for a built-in, "user" or "project" for a custom one — shown so a
    # command that came from a file in the repository is recognisable as such.
    source: str = ""


def _first_line(text: "str | None") -> str:
    """The opening line of a docstring, tidied.

    Built-in descriptions come from the handlers' own docstrings rather than a
    table maintained beside them: the docstrings are already written, already
    accurate, and already sit next to the code, whereas a second list would be
    one more thing to notice had drifted. Tolerates ``None`` because ``python
    -OO`` strips docstrings entirely, and a picker with blank descriptions is
    a great deal better than one that raises.
    """
    if not text:
        return ""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def available_commands(
    builtins: "dict[str, Callable[..., Any]]", cwd: str = "."
) -> "list[CommandEntry]":
    """Every command that would actually run here, built-ins first.

    Built-ins keep their declaration order, which is already curated and
    starts with ``/help`` — that order is what someone who has typed nothing
    but ``/`` is shown.

    A custom command whose name collides with a built-in is left out, because
    it would not run: ``CoBirbApp._dispatch_command`` checks the built-in
    table first and the built-in wins. Offering it anyway would be advertising
    something that does something else when picked.

    Discovery runs per call rather than being cached, the same choice
    ``mentions.candidate_paths`` makes and for the same reason: command files
    can appear while the app is open, and a list captured at startup would be
    wrong from the first one added.
    """
    entries = [
        CommandEntry(name=name.lstrip("/"), description=_first_line(handler.__doc__))
        for name, handler in builtins.items()
    ]
    taken = {entry.name for entry in entries}
    try:
        custom = discover_commands(cwd)
    except Exception:  # noqa: BLE001 - an unreadable commands dir costs the extras, never the picker
        custom = {}
    for name, command in sorted(custom.items()):
        if name not in taken:
            entries.append(
                CommandEntry(name=name, description=command.description, source=command.source)
            )
    return entries


def rank(query: str, entries: "list[CommandEntry]") -> "list[CommandEntry]":
    """Every entry matching ``query``, best first.

    Returns *all* of them rather than a capped slice — unlike
    ``mentions.rank`` — because the picker shows a "5 of 18" counter and
    therefore has to know how many it is not showing. Capping the list here
    would throw away the number it needs.

    An exact prefix outranks a scattered subsequence match unconditionally:
    command names are short and typing ``co`` should offer ``/commands``
    before ``/context`` loses to something that merely contains a c and an o.
    """
    query = query.strip().lstrip("/").lower()
    if not query:
        return list(entries)

    scored: "list[tuple[int, CommandEntry]]" = []
    for entry in entries:
        score = subsequence_score(query, entry.name)
        if score is None:
            continue
        if entry.name.lower().startswith(query):
            score += 1000
        scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name))
    return [entry for _, entry in scored]
