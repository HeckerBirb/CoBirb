"""Tests for custom commands — a prompt you wrote down, invoked by name."""
from __future__ import annotations

import os

from cobirb.runtime.custom_commands import (
    describe_commands,
    discover_commands,
    expand_custom_command,
)


def _user_command(tmp_path, name, text):
    directory = tmp_path / ".cobirb" / "commands"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text)


def _project_command(tmp_path, name, text):
    directory = tmp_path / ".cobirb" / "commands"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text)


def test_no_commands_is_the_normal_case(tmp_path):
    assert discover_commands(str(tmp_path)) == {}
    assert "No custom commands" in describe_commands({})


def test_a_markdown_file_becomes_a_command_named_after_it(tmp_path):
    _user_command(tmp_path, "review", "Review the staged diff carefully.")

    commands = discover_commands(str(tmp_path / "elsewhere"))

    assert commands["review"].expand() == "Review the staged diff carefully."


def test_arguments_land_where_the_body_puts_them(tmp_path):
    _user_command(tmp_path, "explain", "Explain $ARGUMENTS to a new joiner.")

    commands = discover_commands(str(tmp_path / "elsewhere"))

    assert commands["explain"].expand("the parser") == "Explain the parser to a new joiner."


def test_positional_arguments_can_be_placed_separately(tmp_path):
    _user_command(tmp_path, "port", "Move $1 to $2, keeping its tests.")

    command = discover_commands(str(tmp_path / "elsewhere"))["port"]

    assert command.expand("a.py b.py") == "Move a.py to b.py, keeping its tests."


def test_a_placeholder_with_nothing_to_fill_it_is_empty_not_an_error(tmp_path):
    """A command with optional trailing arguments is a normal thing to write."""
    _user_command(tmp_path, "port", "Move $1 to $2.")

    assert discover_commands(str(tmp_path / "x"))["port"].expand("a.py") == "Move a.py to ."


def test_arguments_are_never_silently_dropped(tmp_path):
    """A body with no placeholder still has to do something with what the user
    typed — losing it looks exactly like the command being broken."""
    _user_command(tmp_path, "review", "Review the diff.")

    expanded = discover_commands(str(tmp_path / "x"))["review"].expand("src/parser.py")

    assert "src/parser.py" in expanded


def test_frontmatter_gives_the_command_a_description(tmp_path):
    _user_command(
        tmp_path,
        "review",
        "---\ndescription: Review a diff our way\n---\nRead the staged diff.",
    )

    command = discover_commands(str(tmp_path / "x"))["review"]

    assert command.description == "Review a diff our way"
    assert command.expand() == "Read the staged diff."  # the block itself is not sent


def test_a_command_without_frontmatter_still_lists_readably(tmp_path):
    _user_command(tmp_path, "notes", "# Release notes\nWrite them from the log.")

    assert "Release notes" in discover_commands(str(tmp_path / "x"))["notes"].describe()


def test_a_project_command_wins_over_a_users_command_of_the_same_name(tmp_path):
    """The more specific definition of /review is the one belonging to the
    code being reviewed."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    os.environ["COBIRB_HOME"] = str(home)
    _user_command(home, "review", "the generic review")
    _project_command(project, "review", "the house review")

    assert discover_commands(str(project))["review"].expand() == "the house review"


def test_both_sources_are_available_at_once(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    os.environ["COBIRB_HOME"] = str(home)
    _user_command(home, "explain", "explain it")
    _project_command(project, "release", "cut a release")

    commands = discover_commands(str(project))

    assert set(commands) == {"explain", "release"}
    assert commands["explain"].source == "user"
    assert commands["release"].source == "project"


def test_an_empty_command_file_is_not_a_command(tmp_path):
    _user_command(tmp_path, "blank", "---\ndescription: nothing\n---\n   \n")

    assert "blank" not in discover_commands(str(tmp_path / "x"))


def test_a_non_markdown_file_is_ignored(tmp_path):
    directory = tmp_path / ".cobirb" / "commands"
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("not a command")

    assert discover_commands(str(tmp_path / "x")) == {}


# --------------------------------------------------------------------------- #
# The expansion entry point the front-ends call
# --------------------------------------------------------------------------- #
def test_an_ordinary_prompt_passes_straight_through(tmp_path):
    assert expand_custom_command("fix the parser", str(tmp_path)) == "fix the parser"


def test_a_slash_word_that_names_no_command_is_left_alone(tmp_path):
    """Far more likely to be prose than a typo'd command, and rewriting it
    would be worse than passing it through."""
    assert expand_custom_command("/etc is mounted read-only", str(tmp_path)).startswith("/etc")


def test_an_invoked_command_expands_to_its_body(tmp_path):
    _user_command(tmp_path, "review", "Review $ARGUMENTS.")

    assert expand_custom_command("/review a.py", str(tmp_path / "x")) == "Review a.py."
