"""Tests for project instructions — what a repo tells an agent about itself."""
from __future__ import annotations

from cobirb.config import Config
from cobirb.runtime.instructions import (
    DEFAULT_MAX_CHARS,
    find_instructions_file,
    load_instructions,
)
from cobirb.runtime.wiring import _project_instructions


def test_agents_md_in_the_working_directory_is_found(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always run pytest before committing.")

    text = load_instructions(str(tmp_path))

    assert "Always run pytest before committing." in text
    assert "AGENTS.md" in text  # the model is told where this came from


def test_no_instructions_file_is_the_normal_case(tmp_path):
    assert load_instructions(str(tmp_path)) == ""
    assert find_instructions_file(str(tmp_path)) is None


def test_agents_md_wins_over_the_other_names(tmp_path):
    (tmp_path / "AGENTS.md").write_text("from agents")
    (tmp_path / "CoBirb.md").write_text("from cobirb")

    assert "from agents" in load_instructions(str(tmp_path))


def test_an_empty_instructions_file_sends_nothing(tmp_path):
    """An empty file must not start CoBirb sending a system message it has
    nothing to put in — that would override the model's own for no reason."""
    (tmp_path / "AGENTS.md").write_text("   \n\n  ")

    assert load_instructions(str(tmp_path)) == ""


def test_a_long_instructions_file_is_truncated_and_says_so(tmp_path):
    """This text rides on every request for the whole session. A long
    contributing guide would eat a small window alive."""
    (tmp_path / "AGENTS.md").write_text("x" * 10_000)

    text = load_instructions(str(tmp_path), max_chars=500)

    assert len(text) < 1200
    assert "truncated" in text


def test_an_unreadable_instructions_file_is_not_fatal(tmp_path):
    """Fail-closed like everything else: a broken file costs you the
    instructions, not the session."""
    path = tmp_path / "AGENTS.md"
    path.mkdir()  # a directory where a file was expected

    assert load_instructions(str(tmp_path)) == ""


def test_instructions_are_on_by_default(tmp_path):
    """Opt-out, not opt-in. A file the user put in their own repo saying how
    they want an agent to behave is about as clear an intent signal as there
    is; making them ask for it twice would be silly."""
    (tmp_path / "AGENTS.md").write_text("the house style")

    assert "the house style" in _project_instructions(str(tmp_path), Config(cwd=str(tmp_path)))


def test_config_can_turn_instructions_off(tmp_path):
    (tmp_path / "AGENTS.md").write_text("the house style")
    (tmp_path / "cobirb.json").write_text('{"instructions": false}')

    assert _project_instructions(str(tmp_path), Config(cwd=str(tmp_path))) == ""


def test_config_can_raise_the_budget(tmp_path):
    (tmp_path / "AGENTS.md").write_text("y" * (DEFAULT_MAX_CHARS + 500))
    (tmp_path / "cobirb.json").write_text('{"instructions_max_chars": 99999}')

    text = _project_instructions(str(tmp_path), Config(cwd=str(tmp_path)))

    assert "truncated" not in text
