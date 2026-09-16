"""``@path`` mentions: finding a file by typing at it, and sending it along.

Putting a file in front of the model is the commonest thing anyone does, and
without this it costs a ``read_file`` call, an approval prompt and a second
round trip before the work starts. A mention is the user handing over a file
directly — so it needs no approval, the same way ``/image`` needs none.

Two halves, both here because they are the same feature and neither is about
the terminal:

- **Finding it.** ``rank`` scores candidate paths against what has been typed
  so far, so a picker can show the five best. Matching is by *subsequence*:
  the typed letters must appear in order but need not be adjacent, so ``gba``
  finds ``global.py`` and ``general_batch.py``. Order alone is not enough to
  be useful, though — the ranking is what decides whether it feels right, so
  an exact name wins outright, then a prefix, then letters landing on word
  boundaries, then everything else.
- **Sending it.** ``expand`` turns the text the user submitted into the text
  the model receives, with each mentioned file appended in full.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..redaction import redact

# A mention is "@" followed by a path-ish run: no whitespace, and stopping
# before trailing sentence punctuation so "look at @main.py, then..." names
# `main.py` rather than `main.py,`.
_MENTION = re.compile(r"(?<![^\s(\[{])@([^\s@]+?)(?=[,.;:!?)\]}]*(?:\s|$))")

# One mentioned file, capped the same way `read_file` caps a read. A mention
# is a read like any other; being user-initiated makes it un-prompted, not
# unbounded.
MAX_MENTION_BYTES = 256 * 1024

# Characters that start a new "word" inside a filename, for scoring.
_BOUNDARY = "_-./ "


@dataclass(frozen=True)
class Match:
    """One candidate path and how well it answers what was typed."""

    path: str
    score: int


def _subsequence_score(query: str, candidate: str) -> int | None:
    """Score ``candidate`` against ``query``, or ``None`` if it doesn't match.

    Matching is a subsequence test — every character of the query appears in
    the candidate, in order. The score rewards the things that make a match
    feel deliberate rather than coincidental: letters that land at the start
    of a word (``general_batch`` for ``gb``) beat letters buried mid-word
    (``global`` for ``gb``), and runs of adjacent letters beat scattered ones.
    """
    if not query:
        return 0
    lowered = candidate.lower()
    position = 0
    score = 0
    previous_index = -2
    for char in query.lower():
        found = lowered.find(char, position)
        if found == -1:
            return None
        if found == previous_index + 1:
            score += 6  # adjacent to the last match: a real run
        if found == 0 or lowered[found - 1] in _BOUNDARY:
            score += 10  # start of the name or of a word inside it
        previous_index = found
        position = found + 1
    # A short name containing the query is a better answer than a long one.
    return score - len(candidate) // 8


def rank(query: str, paths: list[str], limit: int = 5) -> list[Match]:
    """The best ``limit`` matches for ``query``, best first.

    Ranked on the *basename* first and the whole path second, because people
    type a filename and only reach for directories to disambiguate.

    An exact basename match is promoted above everything else unconditionally:
    typing ``gba`` when ``gba.py`` exists must put ``gba.py`` first, however
    well some longer name happens to score.
    """
    query = query.strip()
    matches: list[Match] = []
    for path in paths:
        name = os.path.basename(path)
        by_name = _subsequence_score(query, name)
        by_path = _subsequence_score(query, path)
        if by_name is None and by_path is None:
            continue
        score = max(by_name if by_name is not None else -10**6,
                    (by_path - 5) if by_path is not None else -10**6)
        stem = name.rsplit(".", 1)[0].lower()
        if query and stem == query.lower():
            score += 10_000  # the name they actually typed
        elif query and name.lower().startswith(query.lower()):
            score += 100
        matches.append(Match(path=path, score=score))
    # Sorted by score, then by path so equal scores come out in a stable,
    # predictable order rather than however the directory walk happened to go.
    matches.sort(key=lambda m: (-m.score, len(m.path), m.path))
    return matches[:limit]


# Enough of a tree to pick from without walking a monorepo on every keystroke.
# The picker shows five rows; the list only has to be big enough that the right
# file is in it.
MAX_CANDIDATES = 4000


def candidate_paths(cwd: str, limit: int = MAX_CANDIDATES) -> list[str]:
    """Files under ``cwd`` worth offering, as paths relative to it.

    Honours the same ignore rules ``glob`` and ``grep`` use, so a mention
    never offers ``node_modules`` or anything the user kept out of version
    control. Walked per call rather than cached: the agent writes files while
    the app is open, and a list captured at startup would go stale the first
    time it did.
    """
    from ..plugins.core.ignores import IgnoreRules

    rules = IgnoreRules.for_directory(cwd)
    found: list[str] = []
    for directory, subdirectories, filenames in os.walk(cwd):
        subdirectories[:] = [
            name for name in subdirectories
            if not name.startswith(".")
            and not rules.is_ignored(os.path.join(directory, name), is_dir=True)
        ]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(directory, name)
            if rules.is_ignored(full, is_dir=False):
                continue
            found.append(os.path.relpath(full, cwd))
            if len(found) >= limit:
                return found
    return found


def find(text: str) -> list[str]:
    """Every ``@path`` mentioned in ``text``, in the order they appear."""
    seen: list[str] = []
    for match in _MENTION.finditer(text):
        path = match.group(1)
        if path not in seen:
            seen.append(path)
    return seen


def _read(path: str, cwd: str, *, redact_secrets: bool) -> str:
    """One mentioned file as a labelled block, or a line saying why not.

    A mention that can't be read is reported inline rather than raising: the
    rest of the message is still worth sending, and the model being told
    "that file isn't there" is more useful than the turn failing.
    """
    resolved = path if os.path.isabs(path) else os.path.join(cwd, os.path.expanduser(path))
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_MENTION_BYTES + 1)
    except OSError as exc:
        return f"[@{path} could not be read — {exc}]"
    truncated = len(content) > MAX_MENTION_BYTES
    if truncated:
        content = content[:MAX_MENTION_BYTES]
    if redact_secrets:
        content = redact(content).text
    note = f"\n[truncated at {MAX_MENTION_BYTES} bytes]" if truncated else ""
    return f"--- {path} ---\n{content.rstrip()}{note}"


def expand(text: str, cwd: str, *, redact_secrets: bool = True) -> str:
    """The message the model receives: what was typed, plus what was mentioned.

    The mentions are left in the text rather than replaced by the file, so the
    sentence still reads as written ("compare @a.py and @b.py") and the model
    can tell which block answers which name. Files are appended after the
    message, in the order mentioned.

    Returns ``text`` unchanged when nothing was mentioned, so this costs a
    regex scan on an ordinary prompt and nothing else.
    """
    paths = find(text)
    if not paths:
        return text
    blocks = [_read(path, cwd, redact_secrets=redact_secrets) for path in paths]
    return text + "\n\n" + "\n\n".join(blocks)
