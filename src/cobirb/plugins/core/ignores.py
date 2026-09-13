"""Honouring `.gitignore` when searching a project.

`glob` and `grep` used to skip a hardcoded list of eight directory names.
That covered `.venv` and `node_modules` and nothing else, so every search of a
real project crawled `dist/`, `build/`, `target/`, `coverage/` and whatever
else that project happens to generate — wasting time, and worse, spending
context on generated files. It also read anything a user had deliberately kept
out of version control, `.env` included.

**A documented subset, not a git clone.** Supported: comments, blank lines,
negation with `!`, directory-only patterns with a trailing `/`, anchoring with
a leading or embedded `/`, `*` and `?` within a path segment, `**` across
segments, and character classes. Not supported: nested `.gitignore` files
below the root, `.git/info/exclude`, the global excludes file, or
`git check-ignore`'s precedence subtleties. Those matter for correctness of
*version control*; here the cost of getting one wrong is a file searched that
needn't have been, so the simpler thing that covers essentially every real
repository is the right trade.

No new dependency for this. `pathspec` does it properly, but a tool with three
carefully chosen runtime dependencies should not gain a fourth for one that
fits in a page.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Always skipped, whatever `.gitignore` says. `.git` is never listed in one —
# git has no need to ignore itself — and the rest are near-universal and
# expensive enough to crawl that a project forgetting to list them shouldn't
# cost the user a slow search.
ALWAYS_IGNORED = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def _translate(pattern: str) -> str:
    """A gitignore path pattern as a regular expression fragment.

    Written out rather than handed to ``fnmatch`` because the difference that
    matters here is exactly the one ``fnmatch`` gets wrong: ``*`` must not
    cross a ``/`` and ``**`` must.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        i += 1
        if char == "*":
            if i < n and pattern[i] == "*":
                i += 1
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")  # `**/` is zero or more directories
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = i
            if close < n and pattern[close] in "!^":
                close += 1
            if close < n and pattern[close] == "]":
                close += 1
            while close < n and pattern[close] != "]":
                close += 1
            if close >= n:
                out.append(r"\[")  # unterminated class: a literal bracket
            else:
                body = pattern[i:close].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = close + 1
        else:
            out.append(re.escape(char))
    return "".join(out)


@dataclass(frozen=True)
class _Rule:
    matcher: re.Pattern[str]
    negated: bool
    directory_only: bool


def _compile(line: str) -> _Rule | None:
    """One `.gitignore` line as a rule, or ``None`` if it isn't one."""
    stripped = line.rstrip("\n").rstrip()
    if not stripped or stripped.lstrip().startswith("#"):
        return None

    negated = stripped.startswith("!")
    if negated:
        stripped = stripped[1:]
    directory_only = stripped.endswith("/")
    stripped = stripped.rstrip("/")
    if not stripped:
        return None

    # A slash anywhere but the end anchors the pattern to the repository root;
    # without one it matches a basename at any depth. That asymmetry is the
    # part of the format people most often forget, and getting it wrong is the
    # difference between ignoring one `build/` and ignoring all of them.
    anchored = "/" in stripped
    body = _translate(stripped.lstrip("/"))
    # The trailing group is what makes ignoring a directory ignore everything
    # inside it, which is how the format is expected to behave.
    prefix = "" if anchored else "(?:.*/)?"
    return _Rule(re.compile(f"^{prefix}{body}(?:/.*)?$"), negated, directory_only)


class IgnoreRules:
    """The ignore rules in force for one project root."""

    def __init__(self, root: str, rules: list[_Rule] | None = None) -> None:
        self.root = os.path.abspath(root)
        self._rules = rules or []

    @classmethod
    def for_directory(cls, root: str) -> "IgnoreRules":
        """Read ``root/.gitignore``, if there is one.

        A missing or unreadable file is not an error: the built-in list below
        still applies, and a search that skips slightly less than it could is
        a much better failure than one that refuses to run.
        """
        rules: list[_Rule] = []
        path = os.path.join(root, ".gitignore")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    rule = _compile(line)
                    if rule is not None:
                        rules.append(rule)
        except OSError:
            pass
        return cls(root, rules)

    def is_ignored(self, path: str, is_dir: bool | None = None) -> bool:
        """Whether ``path`` should be skipped when searching."""
        absolute = os.path.abspath(path)

        # The built-in list matches on basenames, so it needs no project root
        # and applies wherever the path happens to be — searching someone
        # else's `node_modules` is no more useful than searching your own.
        # `.egg-info` is a suffix rather than a name, hence the second check.
        parts = absolute.replace(os.sep, "/").split("/")
        if any(part in ALWAYS_IGNORED or part.endswith(".egg-info") for part in parts):
            return True

        # `.gitignore` rules, by contrast, only mean anything relative to the
        # project they came from.
        try:
            relative = os.path.relpath(absolute, self.root)
        except ValueError:  # different drive on Windows
            return False
        if relative.startswith(".."):
            return False  # outside the project; not ours to judge
        relative = relative.replace(os.sep, "/")

        if is_dir is None:
            is_dir = os.path.isdir(absolute)

        # Last matching rule wins, which is what makes `!` able to rescue
        # something an earlier line swept up.
        ignored = False
        for rule in self._rules:
            if not rule.matcher.match(relative):
                continue
            if rule.directory_only and not is_dir and "/" not in relative:
                continue
            ignored = not rule.negated
        return ignored
