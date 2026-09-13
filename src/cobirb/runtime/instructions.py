"""Project instructions: what this repo wants an agent to know.

Every agentic CLI grows one of these — `AGENTS.md`, `CLAUDE.md`, `.cursorrules`
— because the alternative is re-typing the same conventions at the start of
every session. CoBirb has shipped an `AGENTS.md` since before it could read
one, which was an easy thing to miss and a cheap thing to fix.

Two decisions worth knowing about:

**It is capped, but generously.** This text rides on *every* request for the
whole session, so an unbounded file is still worth guarding against — a
100 KB contributing guide is a quarter of a 128k window spent before the
conversation starts. The cap was originally 2,000 characters, sized for a
4,096-token window that CoBirb's users do not have; at 32k it comfortably
sends a real project's conventions whole, which is the point of reading them
at all. Truncation is announced rather than silent, and the budget is
configurable.

**It does not walk up the tree.** Only the working directory is read. Walking
up to a git root sounds helpful right up until a monorepo's top-level
instructions silently override the ones next to the code you asked about.
"""
from __future__ import annotations

import os

# Checked in order; the first that exists wins. AGENTS.md leads because it is
# the convention this project itself follows.
INSTRUCTION_FILENAMES = ("AGENTS.md", "CoBirb.md", "COBIRB.md")

# Roughly 8,000 tokens at four characters each — a few percent of the window
# the target hardware runs, and enough to send most projects' conventions
# whole rather than cutting them off mid-section.
# `"instructions_max_chars"` raises or lowers it.
DEFAULT_MAX_CHARS = 32000


def find_instructions_file(cwd: str) -> str | None:
    """The project instructions file in ``cwd``, if there is one."""
    for name in INSTRUCTION_FILENAMES:
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            return path
    return None


def load_instructions(cwd: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The project's instructions, ready to append to a system prompt.

    Returns ``""`` when there is no file, when it is empty, or when it can't
    be read — a broken instructions file is not worth losing the session
    over, and a missing one is the normal case.
    """
    path = find_instructions_file(cwd)
    if path is None:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return ""
    if not text:
        return ""

    name = os.path.basename(path)
    if len(text) > max_chars:
        text = (
            text[:max_chars]
            + f"\n[…truncated at {max_chars} characters; raise \"instructions_max_chars\" in "
            "config to send more]"
        )
    return f"Project instructions, from {name} in the working directory:\n\n{text}"
