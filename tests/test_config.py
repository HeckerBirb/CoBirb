"""Tests for the config reader.

Tests go through Config's public interface (construction + get/set/data) with
real temp JSON files, not the private ``_load`` helper — that is an
implementation detail Config could refactor away without its observable
behaviour changing at all.

**There is one config file, in the user's home directory.** No user layer, no
repo layer, nothing merged. A repository cannot configure CoBirb, and the tests immediately
below assert that as a property rather than an absence — a repo layer coming
back by accident is exactly the kind of regression that would otherwise pass
unnoticed until it granted something.
"""
from __future__ import annotations

import json
import os

from conftest import write_config

from cobirb.config import Config


def _write_json(path, data):
    path.write_text(json.dumps(data))


# --------------------------------------------------------------------------- #
# One file, in the user's home. Nothing else is read.
# --------------------------------------------------------------------------- #
def test_the_config_read_is_the_one_in_the_users_home(tmp_path):
    write_config(tmp_path, {"model": "llama3.1"})

    assert Config().get("model") == "llama3.1"


def test_a_cobirb_json_in_the_working_directory_is_ignored_entirely(tmp_path, monkeypatch):
    """The property this whole design turns on. Configuration decides what is
    pre-approved and what runs at lifecycle points, so a repository able to
    contribute any of it means cloning a repository is enough to influence the
    permission model — before the model is asked anything, with no prompt able
    to intervene."""
    write_config(tmp_path, {"model": "mine", "allow_tools": []})
    project = tmp_path / "project"
    project.mkdir()
    _write_json(project / "cobirb.json", {"model": "theirs", "allow_tools": ["shell(curl)"]})
    monkeypatch.chdir(project)

    config = Config()

    assert config.get("model") == "mine"
    assert config.get("allow_tools") == []


def test_a_cobirb_json_is_not_even_opened(tmp_path, monkeypatch):
    """"Ignored" is not quite the promise; "never attempted" is. A file that is
    unreadable, enormous, or a named pipe must cost nothing at all, which is
    only true if CoBirb never goes near it."""
    write_config(tmp_path, {"model": "mine"})
    project = tmp_path / "project"
    project.mkdir()
    (project / "cobirb.json").write_text("{ this would be reported if it were read")
    monkeypatch.chdir(project)

    opened = []
    real_open = open

    def watching(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", watching)
    Config()

    assert not any(name.endswith("cobirb.json") for name in opened)


def test_config_takes_no_working_directory(tmp_path):
    """Not a stylistic point: a `cwd` parameter that selects nothing is an
    invitation to assume it does."""
    import inspect

    assert list(inspect.signature(Config.__init__).parameters) == ["self", "user_path"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def test_a_missing_file_yields_an_empty_config(tmp_path):
    config = Config(user_path=str(tmp_path / "no-user.json"))

    assert config.data == {}
    assert config.get("anything") is None
    assert config.get("anything", default="fallback") == "fallback"


def test_get_returns_default_for_a_path_that_does_not_exist(tmp_path):
    write_config(tmp_path, {"model": "llama3.1"})
    config = Config()

    assert config.get("does", "not", "exist") is None
    assert config.get("does", "not", "exist", default="x") == "x"


def test_get_stops_cleanly_when_descending_through_a_non_dict_value(tmp_path):
    """"model" is a plain string; asking for a key underneath it must return
    the default, not raise."""
    write_config(tmp_path, {"model": "llama3.1"})

    assert Config().get("model", "nested", "deeper") is None


def test_nested_values_are_reachable_by_path(tmp_path):
    write_config(tmp_path, {"models": {"default": {"name": "llama3.1", "base_url": "http://x"}}})
    config = Config()

    assert config.get("models", "default", "name") == "llama3.1"
    assert config.get("models", "default", "base_url") == "http://x"


# --------------------------------------------------------------------------- #
# Writing (in memory)
# --------------------------------------------------------------------------- #
def test_set_creates_intermediate_dicts_as_needed():
    config = Config(user_path="/no/such/file.json")
    config.set("models", "default", "name", value="llama3.1")

    assert config.get("models", "default", "name") == "llama3.1"


def test_set_then_get_round_trips_a_top_level_value():
    config = Config(user_path="/no/such/file.json")
    config.set("system_prompt", value="harness")

    assert config.get("system_prompt") == "harness"


def test_set_overwrites_a_non_dict_value_that_is_in_the_way():
    config = Config(user_path="/no/such/file.json")
    config.set("a", value="just a string")
    config.set("a", "b", value="x")

    assert config.get("a", "b") == "x"


# --------------------------------------------------------------------------- #
# Broken input
# --------------------------------------------------------------------------- #
def test_malformed_json_is_reported_but_not_silently_ignored(tmp_path, capsys):
    """A config file that exists but fails to parse must not be treated as if
    it were missing — that would hide a real mistake. It is reported by name,
    with the parse error, and then skipped: raising instead took down every
    command including ones that read no config at all."""
    user_path = tmp_path / "user.json"
    user_path.write_text("{not valid json")

    config = Config(user_path=str(user_path))

    assert config.data == {}
    err = capsys.readouterr().err
    assert str(user_path) in err
    assert "ignoring" in err


def test_a_stray_comma_does_not_take_down_a_command_that_reads_no_config(tmp_path, capsys):
    write_config(tmp_path, {})
    (tmp_path / ".cobirb" / "config.json").write_text('{"default_model": "m",}')

    assert Config().get("default_model") is None
    assert "ignoring" in capsys.readouterr().err


def test_a_config_that_is_valid_json_but_not_an_object_is_skipped(tmp_path, capsys):
    write_config(tmp_path, {})
    (tmp_path / ".cobirb" / "config.json").write_text('["not", "an", "object"]')

    assert Config().data == {}
    assert "expected a JSON object" in capsys.readouterr().err


def test_the_bundled_example_config_is_valid_and_loadable(tmp_path):
    """config.json.example (the starting point the README points users at) must
    stay valid, loadable JSON that Config can actually read — a stale or broken
    example is worse than no example at all."""
    example_path = os.path.join(os.path.dirname(__file__), "..", "config.json.example")
    config = Config(user_path=example_path)

    assert config.get("default_model")
    assert config.get("models", "default", "name")


# --------------------------------------------------------------------------- #
# The home tree
# --------------------------------------------------------------------------- #
def test_every_cobirb_path_sits_under_one_home(tmp_path, monkeypatch):
    """One derivation, one tree. Modules resolving COBIRB_HOME independently
    and joining their own subpath onto it is how one kind of file ends up in
    ~/cobirb/ while everything else uses ~/.cobirb/."""
    from cobirb import paths

    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    root = str(tmp_path / ".cobirb")

    assert paths.cobirb_dir() == root
    for path in (
        paths.config_path(),
        paths.sessions_dir(),
        paths.audit_path(),
        paths.user_plugins_dir(),
    ):
        assert path.startswith(root + os.sep), path


