"""What `cobirb help` prints.

`cobirb help` is a short overview. `cobirb help <topic>` is a page of the
user manual — `docs/manual/<topic>.md`, shipped inside the package as
`cobirb/manual/` (see setup.py) — so the manual is the one place any of this
is written. The help used to be its own copy of the same material, and the
two drifted: the help still said `/undo` could not reverse a shell command
releases after it could. A page changed is now the help changed.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

# Older topic names, and the page each now lives on — so nothing anyone has
# typed, or written down, stops working.
ALIASES = {
    "session": "sessions",
    "plugin": "plugins-and-mcp",
    "plugins": "plugins-and-mcp",
    "model": "config",
    "plan": "commands",
    "permission": "permissions",
    "setup": "first-run",
}


def manual_dir() -> Path | None:
    """Where the manual pages are: inside the installed package, or — in a
    checkout that has not been built — ``docs/manual`` beside the source."""
    packaged = Path(__file__).resolve().parent / "manual"
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "docs" / "manual"
    return checkout if checkout.is_dir() else None


class _ManualTopics(Mapping[str, str]):
    """Topic name → that manual page's text, read when asked for."""

    def _pages(self) -> dict[str, Path]:
        directory = manual_dir()
        if directory is None:
            return {}
        pages = {path.stem: path for path in sorted(directory.glob("*.md"))}
        for alias, page in ALIASES.items():
            if page in pages and alias not in pages:
                pages[alias] = pages[page]
        return pages

    def __getitem__(self, topic: str) -> str:
        return self._pages()[topic].read_text(encoding="utf-8")

    def __iter__(self) -> Iterator[str]:
        return iter(self._pages())

    def __len__(self) -> int:
        return len(self._pages())

    def pages(self) -> list[str]:
        """The page names, without the aliases."""
        return sorted(name for name in self._pages() if name not in ALIASES)


HELP_TOPICS = _ManualTopics()

_OVERVIEW = """\
CoBirb — a privacy-first agentic coding CLI for models you run yourself.

FIRST RUN
  cobirb setup           Choose your model server and model; saved to your
                         config. Only the address you give is contacted.
  cobirb doctor          Check that everything is ready.

MODES
  cobirb                 The full-screen app.
  cobirb -p "task"       One-shot: plain stdout, pipes and scripts.
  cobirb -p "task" --headless --output json
                         For CI: never prompts; exit 0 clean, 1 failed,
                         2 something was refused.
  cobirb -p "task" --autopilot
                         Unattended: works inside the project and the
                         sandbox, refuses anything else instead of asking.
  cobirb -w              An encrypted session you can resume.

PRIVACY BY CONSTRUCTION
  • No telemetry, and no outbound network by default — CoBirb talks only to
    the model server you configure.
  • Shell commands run in a sandbox: no network, nothing writable outside
    your project, credential directories hidden. Anything the sandbox cannot
    contain is put to you first; nothing else is pre-approved.
  • Every turn can be undone (/undo), shell changes included, without
    touching your git history.
  • Sessions are encrypted at rest (AES-256-GCM, keyed via scrypt), and
    credentials are stripped from tool output.
  • Your model's own SYSTEM prompt is left alone.

These are enforced in code, not by asking the model to behave.

In the app: ? or /help for this, /help <topic> for a page, f1 help,
f2 next tab, f3 auto-pilot on/off, ctrl+q quit.
"""


def overview() -> str:
    """The overview, ending with the pages there are to read."""
    pages = HELP_TOPICS.pages()
    listing = ", ".join(pages) if pages else "(the manual is not installed)"
    return f"{_OVERVIEW}\nTOPICS — 'cobirb help <topic>'\n  {listing}\n"


HELP_TEXT = overview()
