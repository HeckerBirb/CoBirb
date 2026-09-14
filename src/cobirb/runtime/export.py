"""Turning an encrypted session into something you can hand to someone.

A session you can't read is most of the reason not to keep one. Export is the
sanctioned way out: the user asks, gives the password, and gets markdown.

**Deliberately an explicit act.** Nothing exports on its own, and the output
is plaintext — the whole point is to produce something shareable, and a
"shareable" file that stayed encrypted would be neither. That makes export
the one place CoBirb writes a conversation to disk unprotected, so it says so
when it does it, and never picks the destination itself.
"""
from __future__ import annotations

import os

from ..session import PHASE_PLAN, PHASE_VALIDATE, Session

_ROLE_HEADINGS = {
    "user": "You",
    "assistant": "CoBirb",
    "tool": "Tool",
}


def _tool_heading(turn) -> str:
    """"Tool: read_file" where the call is known, plain "Tool" otherwise."""
    if turn.tool_use:
        name = str(turn.tool_use[0].get("name", "")).strip()
        if name:
            return f"Tool: `{name}`"
    return "Tool"


def session_to_markdown(session: Session, *, title: str = "CoBirb session") -> str:
    """Render a decrypted session as markdown.

    Tool results go in fenced blocks rather than as prose: they are output,
    not conversation, and a diff or a file listing pasted inline would reflow
    into nonsense.
    """
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- Created: {session.created_at}")
    lines.append(f"- Working directory: `{session.working_dir}`")
    lines.append(f"- Persona: {session.persona}")
    lines.append(f"- Turns: {len(session.turns)}")
    lines.append("")

    for turn in session.turns:
        heading = _tool_heading(turn) if turn.role == "tool" else _ROLE_HEADINGS.get(turn.role, turn.role)
        if turn.phase in (PHASE_PLAN, PHASE_VALIDATE):
            heading = f"{heading} · {turn.phase}"
        lines.append(f"## {heading}")
        lines.append("")
        content = (turn.content or "").rstrip()
        if turn.role == "tool":
            # A fence, and one that can't be broken by backticks in the output.
            fence = "`" * max(3, _longest_backtick_run(content) + 1)
            lines.extend([fence, content or "(no output)", fence])
        else:
            lines.append(content or "_(empty)_")
        if turn.images:
            # A marker only, never the bytes: an export is plaintext by
            # design (see this module's docstring) and an attached image
            # riding along unasked in it is not the same "explicit act".
            names = ", ".join(img.get("filename", "attachment") for img in turn.images)
            lines.append(f"\n📎 {names}")
        lines.append("")

    if session.summary:
        lines.extend(["## Summary", "", session.summary.rstrip(), ""])
    if session.validation:
        lines.extend(["## Validation", "", session.validation.rstrip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest


def write_export(session: Session, destination: str, *, title: str = "CoBirb session") -> str:
    """Write ``session`` to ``destination`` as markdown, returning the path.

    0600, like the session file it came from. The content is plaintext by
    design, but that is no reason to hand it to every account on the machine
    as well — sharing it is a decision the user makes afterwards, deliberately,
    not one the file mode makes for them.
    """
    destination = os.path.abspath(os.path.expanduser(destination))
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(session_to_markdown(session, title=title))
    return destination
