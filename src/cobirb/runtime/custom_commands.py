"""Custom commands: a prompt you have written down, invoked by name.

Everyone who uses an agent for real work ends up with a handful of prompts they
retype — "review this diff the way our team reviews diffs", "write the release
notes from the log since the last tag", "explain this module to someone joining
the project". Retyping them means they drift, and the good version of a prompt
is usually the fifth one.

A custom command is one of those prompts in a markdown file:

    ~/.cobirb/commands/review.md          available everywhere
    <project>/.cobirb/commands/review.md  available in that project

``/review`` then sends the file's body as the prompt. ``$ARGUMENTS`` in the body
is replaced by whatever followed the command, and ``$1``…``$9`` by individual
words, so ``/review src/parser.py --strict`` can put the path and the flag in
different places. A body with no placeholder simply gets the arguments appended
as a line, because the alternative — silently dropping what the user typed — is
the sort of thing that looks like the command is broken.

An optional frontmatter block gives it a description for the listing:

    ---
    description: Review a diff the way this team reviews diffs
    ---
    Read the staged diff and check it against $ARGUMENTS...

**A project command is data, not code.** It expands to a prompt and nothing
else, which is why these — unlike hooks and MCP servers — are read from the
project directory as well as the user's home. The prompt still comes from a
file in a repository, so the usual caution about running an agent inside code
you have not read applies; what it cannot do is act on its own. Every tool call
it leads to still goes through the permission layer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .. import paths

# Where commands live, under each root. One name, two places, project last so
# it wins — a project that ships its own /review means the one for this
# codebase, which is more specific than the one you use everywhere.
_SUBDIR = os.path.join(".cobirb", "commands")

# `$1`..`$9`, and `$ARGUMENTS` for the lot. Deliberately not a general template
# language: a prompt file is prose with two holes in it, and anything more
# would be a scripting language nobody asked for living inside markdown.
_POSITIONAL = re.compile(r"\$([1-9])")
_ALL_ARGUMENTS = "$ARGUMENTS"

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


@dataclass(frozen=True)
class CustomCommand:
    """One command file, ready to expand."""

    name: str
    body: str
    description: str
    path: str
    source: str  # "user" or "project"

    def expand(self, argument: str = "") -> str:
        """The prompt to send, with the user's arguments filled in."""
        words = argument.split()
        text = self.body
        # "Has a placeholder" means *any* of them: a body using only `$1` and
        # `$2` has already said where the arguments go, and appending them
        # again underneath would repeat them.
        placed = _ALL_ARGUMENTS in text or bool(_POSITIONAL.search(text))
        text = text.replace(_ALL_ARGUMENTS, argument)
        text = _POSITIONAL.sub(lambda m: _nth(words, int(m.group(1))), text)
        if argument and not placed:
            # No placeholder, but the user typed something. Appending it is the
            # only reading that doesn't throw away what they said.
            text = f"{text}\n\n{argument}"
        return text.strip()

    def describe(self) -> str:
        """One line for the ``/commands`` listing."""
        summary = self.description or _first_line(self.body)
        return f"/{self.name:<16} {summary}  ({self.source})"


def _nth(words: list[str], index: int) -> str:
    """``$3`` when only two words were given is empty, not an error — a command
    with optional trailing arguments is a normal thing to write."""
    return words[index - 1] if 0 < index <= len(words) else ""


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:70]
    return "(no description)"


def _parse(text: str) -> tuple[str, str]:
    """Split an optional frontmatter block off the front. Returns
    ``(description, body)``.

    A hand-rolled reader for two or three ``key: value`` lines rather than a
    YAML dependency: this project ships no parser it doesn't need, and the
    entire vocabulary here is one key.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return "", text.strip()
    description = ""
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "description":
            description = value.strip()
    return description, text[match.end():].strip()


def _load_dir(directory: str, source: str) -> dict[str, CustomCommand]:
    found: dict[str, CustomCommand] = {}
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        description, body = _parse(text)
        if not body:
            continue
        name = entry.name[: -len(".md")]
        found[name] = CustomCommand(
            name=name, body=body, description=description, path=entry.path, source=source
        )
    return found


def discover_commands(cwd: str = ".") -> dict[str, CustomCommand]:
    """Every custom command available here, keyed by name (no leading slash).

    User commands first, then the project's, so a project that defines a name
    the user also has wins — the more specific definition of ``/review`` is the
    one belonging to the code being reviewed.
    """
    commands = _load_dir(os.path.join(paths.cobirb_dir(), "commands"), "user")
    commands.update(_load_dir(os.path.join(cwd, _SUBDIR), "project"))
    return commands


def expand_custom_command(prompt: str, cwd: str = ".") -> str:
    """Expand ``prompt`` if it invokes a custom command; otherwise return it.

    Used by one-shot mode, so ``cobirb -p "/review src/parser.py"`` means the
    same thing as typing it interactively. A ``/word`` that names no command is
    left exactly as it is — the same rule the TUI applies, and for the same
    reason: an unrecognised slash-word is far more likely to be prose than a
    typo, and rewriting it would be worse than passing it through.
    """
    if not prompt.startswith("/"):
        return prompt
    name, _, argument = prompt[1:].partition(" ")
    command = discover_commands(cwd).get(name)
    return command.expand(argument.strip()) if command else prompt


def describe_commands(commands: dict[str, CustomCommand]) -> str:
    """The text ``/commands`` prints."""
    if not commands:
        return (
            "No custom commands. Put a markdown file in ~/.cobirb/commands/ or "
            f"./{_SUBDIR}/ and its name becomes a command — see 'cobirb help commands'."
        )
    lines = [command.describe() for _, command in sorted(commands.items())]
    return "Custom commands:\n  " + "\n  ".join(lines)
