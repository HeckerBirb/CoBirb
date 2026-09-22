"""The ``*** Begin Patch`` format, read once for the tool and the policy.

gpt-oss (and other models trained against OpenAI's tooling) write patches in
this format rather than as unified diffs:

    *** Begin Patch
    *** Update File: handlers.py
    @@ def handle_delete(request):
    -    return {"status": "ok", "code": 200}
    +    return {"status": "ok", "code": 204}
    *** End Patch

Hunks carry no line numbers; they are located by their context, optionally
narrowed by the text after ``@@``. ``apply_patch`` refused all of these as "no
valid hunks", so the model's edit never landed.

This module lives outside ``plugins`` because the **policy** reads it too. The
file a patch changes is named inside the patch, not in a ``path`` argument, and
a permission check has to be made against the file the tool will actually
write — so both must parse the header the same way, the same rule that keeps
``Policy._resolve`` and ``CobirbTool._resolve`` in step. Only single-file
Update and Add sections are accepted: a patch touching several files, or moving
or deleting one, names targets the one-path permission check cannot vouch for,
so it is refused rather than half-checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

BEGIN = "*** Begin Patch"
_SECTION = re.compile(r"^\*\*\* (Update|Add|Delete) File:\s*(.+?)\s*$")
_MOVE = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$")


@dataclass
class Section:
    """One file's part of a patch."""

    op: str  # "update" | "add" | "delete"
    path: str
    move_to: str | None = None
    # For "update": (anchor, [(marker, text), ...]); for "add": lines to write.
    hunks: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def is_begin_patch(text: str) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(BEGIN)


def parse(text: str) -> list[Section]:
    """The sections of a ``*** Begin Patch`` text, in order."""
    sections: list[Section] = []
    current: Section | None = None
    anchor, body = "", []  # the hunk being collected

    def close_hunk() -> None:
        nonlocal anchor, body
        if current is not None and current.op == "update" and body:
            current.hunks.append((anchor, body))
        anchor, body = "", []

    for line in text.splitlines():
        if line.startswith(("*** Begin Patch", "*** End Patch", "*** End of File")):
            continue
        section = _SECTION.match(line)
        if section:
            close_hunk()
            current = Section(op=section.group(1).lower(), path=section.group(2))
            sections.append(current)
            continue
        if current is None:
            continue
        move = _MOVE.match(line)
        if move:
            current.move_to = move.group(1)
            continue
        if current.op == "add":
            current.lines.append(line[1:] if line.startswith("+") else line)
            continue
        if line.startswith("@@"):
            close_hunk()
            anchor = line[2:].strip()
            continue
        if line[:1] in ("+", "-", " "):
            body.append((line[0], line[1:]))
        elif line == "":
            body.append((" ", ""))
    close_hunk()
    return sections


def single_target(text: str) -> str | None:
    """The one file a ``*** Begin Patch`` text changes, or ``None`` when it
    names none, several, or asks for a move or a delete."""
    sections = parse(text)
    if len(sections) != 1:
        return None
    section = sections[0]
    if section.op not in ("update", "add") or section.move_to:
        return None
    return section.path
