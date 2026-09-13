"""Project instructions: what this repo wants an agent to know.

Every agentic CLI grows one of these — `AGENTS.md`, `CLAUDE.md`, `.cursorrules`
— because the alternative is re-typing the same conventions at the start of
every session. CoBirb has shipped an `AGENTS.md` since before it could read
one, which was an easy thing to miss and a cheap thing to fix.

Two decisions worth knowing about:

**It is capped, hard.** This text rides on *every* request for the whole
session. Against a 4096-token window — which is what a local model is usually
actually served — a 3,000-word contributing guide would eat the conversation
alive. So it is truncated at a budget the user can raise, and the truncation
is announced rather than silent.

**It does not walk up the tree.** Only the working directory is read. Walking
up to a git root sounds helpful right up until a monorepo's top-level
instructions silently override the ones next to the code you asked about.
"""
from __future__ import annotations

import os

# Checked in order; the first that exists wins. AGENTS.md leads because it is
# the convention this project itself follows.
INSTRUCTION_FILENAMES = ("AGENTS.md", "CoBirb.md", "COBIRB.md")

# Roughly 500 tokens at four characters each. Enough for the conventions that
# matter, small enough that it doesn't crowd out the conversation on a small
# window. `"instructions_max_chars"` raises it.
DEFAULT_MAX_CHARS = 2000


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
