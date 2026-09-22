"""Tests for what the ``/`` picker offers and in what order.

No app here on purpose — the listing and the ranking are deliberately free of
terminal knowledge (see ``runtime/command_index.py``), which is what lets them
be checked this cheaply.
"""
from __future__ import annotations

import os

from cobirb.runtime import command_index
from cobirb.runtime.command_index import CommandEntry, available_commands, rank


def _builtins():
    def cmd_help(app, argument):
        """The help screen.

        A second paragraph the picker has no room for.
        """

    def cmd_clear(app, argument):
        """Start over from here."""

    def cmd_context(app, argument):
        """How much of the model's window this session is using."""

    def cmd_commands(app, argument):
        """List your own prompt files."""

    return {"/help": cmd_help, "/clear": cmd_clear, "/context": cmd_context,
            "/commands": cmd_commands}


def _write_command(directory, name, description, body="do the thing"):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.md"), "w", encoding="utf-8") as handle:
        handle.write(f"---\ndescription: {description}\n---\n{body}\n")


# --------------------------------------------------------------------------- #
# What is listed
# --------------------------------------------------------------------------- #
def test_a_builtins_description_is_the_first_line_of_its_docstring(tmp_path):
    """Rather than a table kept beside them, which would be a second thing to
    notice had drifted."""
    entries = {entry.name: entry for entry in available_commands(_builtins(), str(tmp_path))}

    assert entries["help"].description == "The help screen."
    assert "second paragraph" not in entries["help"].description


def test_a_command_with_no_docstring_still_appears(tmp_path):
    """`python -OO` strips docstrings. A picker with blank descriptions beats
    one that raises."""
    def cmd_bare(app, argument):
        pass

    entries = available_commands({"/bare": cmd_bare}, str(tmp_path))

    assert [entry.name for entry in entries] == ["bare"]
    assert entries[0].description == ""


def test_custom_commands_are_offered_and_say_where_they_came_from(tmp_path):
    """The discoverability the picker is for: a command defined in a file is
    otherwise invisible until you already know it exists."""
    _write_command(str(tmp_path / ".cobirb" / "commands"), "review", "Review a diff")

    entries = {entry.name: entry for entry in available_commands(_builtins(), str(tmp_path))}

    assert entries["review"].description == "Review a diff"
    assert entries["review"].source == "project"


def test_a_custom_command_shadowed_by_a_builtin_is_not_offered(tmp_path):
    """The built-in wins at dispatch, so offering the custom one would be
    advertising something that does something else when picked."""
    _write_command(str(tmp_path / ".cobirb" / "commands"), "clear", "Not the real one")

    entries = [entry for entry in available_commands(_builtins(), str(tmp_path))
               if entry.name == "clear"]

    assert len(entries) == 1
    assert entries[0].description == "Start over from here."
    assert entries[0].source == ""


def test_builtins_keep_their_declaration_order(tmp_path):
    """It is already curated, and it is what someone who typed only "/" sees."""
    entries = available_commands(_builtins(), str(tmp_path))

    assert [entry.name for entry in entries][:4] == ["help", "clear", "context", "commands"]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def _entries(*names):
    return [CommandEntry(name=name, description="") for name in names]


def test_an_empty_query_offers_everything_in_order():
    entries = _entries("help", "model", "clear")

    assert [entry.name for entry in rank("", entries)] == ["help", "model", "clear"]


def test_a_prefix_beats_a_scattered_match():
    """Typing "co" should offer /context and /commands before "clone", which
    merely happens to contain a c and then an o."""
    entries = _entries("clone", "context", "commands")

    ranked = [entry.name for entry in rank("co", entries)]

    assert ranked[:2] == ["context", "commands"]
    assert ranked[-1] == "clone"


def test_a_query_the_name_does_not_contain_at_all_is_dropped():
    """Subsequence, not "close enough": /model has no c, so "co" is not a
    worse match for it — it is not a match."""
    assert rank("co", _entries("model")) == []


def test_a_leading_slash_in_the_query_is_ignored():
    """The query arrives without its slash, but tolerating one costs nothing
    and a caller passing the whole word is the obvious mistake to survive."""
    entries = _entries("clear", "context")

    assert [entry.name for entry in rank("/cle", entries)] == ["clear"]


def test_nothing_matching_ranks_to_nothing():
    assert rank("zzz", _entries("clear", "context")) == []


def test_rank_returns_every_match_not_a_capped_slice():
    """The picker shows five rows and a "5 of N" counter, so it has to be told
    how many it is not showing."""
    entries = _entries(*[f"command{n}" for n in range(12)])

    assert len(rank("command", entries)) == 12


def test_an_unreadable_commands_directory_costs_the_extras_not_the_picker(tmp_path, monkeypatch):
    def _boom(cwd="."):
        raise OSError("nope")

    monkeypatch.setattr(command_index, "discover_commands", _boom)

    entries = available_commands(_builtins(), str(tmp_path))

    assert [entry.name for entry in entries] == ["help", "clear", "context", "commands"]
