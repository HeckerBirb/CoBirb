"""Tests for project instructions — what a repo tells an agent about itself."""
from __future__ import annotations

import json
from conftest import write_config

from cobirb.config import Config
from cobirb.runtime.instructions import (
    DEFAULT_MAX_CHARS,
    find_instructions_file,
    load_instructions,
)
from cobirb.runtime.wiring import _project_context


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


def _instructions_only(tmp_path):
    """Config that leaves the instructions on and the repo map off, so these
    tests assert on the half they are about."""
    write_config(tmp_path, json.loads('{"repo_map": false}'))
    return Config()


def test_instructions_are_on_by_default(tmp_path):
    """Opt-out, not opt-in. A file the user put in their own repo saying how
    they want an agent to behave is about as clear an intent signal as there
    is; making them ask for it twice would be silly."""
    (tmp_path / "AGENTS.md").write_text("the house style")

    assert "the house style" in _project_context(str(tmp_path), _instructions_only(tmp_path))


def test_config_can_turn_instructions_off(tmp_path):
    (tmp_path / "AGENTS.md").write_text("the house style")
    write_config(tmp_path, json.loads('{"instructions": false, "repo_map": false}'))

    assert _project_context(str(tmp_path), Config()) == ""


def test_config_can_raise_the_budget(tmp_path):
    (tmp_path / "AGENTS.md").write_text("y" * (DEFAULT_MAX_CHARS + 500))
    write_config(tmp_path, json.loads('{"instructions_max_chars": 999999, "repo_map": false}'))

    text = _project_context(str(tmp_path), Config())

    assert "truncated" not in text


# --------------------------------------------------------------------------- #
# The repo map in the project context.
#
# Injected rather than tool-only: a permanent map would crowd a
# small window — which assumes a 4,096-token budget the target hardware does
# not have.
# --------------------------------------------------------------------------- #
def test_the_codebase_outline_is_in_the_project_context_by_default(tmp_path):
    (tmp_path / "engine.py").write_text("class Engine:\n    def start(self): pass\n")

    context = _project_context(str(tmp_path), Config())

    assert "engine.py" in context
    assert "class Engine" in context


def test_instructions_and_the_map_are_both_present(tmp_path):
    (tmp_path / "AGENTS.md").write_text("run pytest before committing")
    (tmp_path / "engine.py").write_text("def start(): pass\n")

    context = _project_context(str(tmp_path), Config())

    assert "run pytest before committing" in context
    assert "engine.py" in context


def test_the_map_can_be_turned_off(tmp_path):
    (tmp_path / "engine.py").write_text("def start(): pass\n")
    write_config(tmp_path, json.loads('{"repo_map": false}'))

    assert "engine.py" not in _project_context(str(tmp_path), Config())


def test_a_zero_budget_also_turns_the_map_off(tmp_path):
    (tmp_path / "engine.py").write_text("def start(): pass\n")
    write_config(tmp_path, json.loads('{"repo_map_max_chars": 0}'))

    assert "engine.py" not in _project_context(str(tmp_path), Config())


def test_an_unmappable_project_costs_orientation_not_the_session(tmp_path, monkeypatch):
    """Guarded: walking a project that cannot be walked must not stop CoBirb
    starting."""
    from cobirb.runtime import wiring

    def boom(*args, **kwargs):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(wiring, "render_map", boom)
    (tmp_path / "AGENTS.md").write_text("still here")

    assert "still here" in _project_context(str(tmp_path), Config())
