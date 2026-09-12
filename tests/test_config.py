"""Tests for the layered user/repo config reader.

Tests go through Config's public interface (construction + get/set/data)
with real temp JSON files, not the private _load/_merge helpers directly —
those are implementation details Config could refactor away without its
observable behavior (what a caller actually sees) changing at all.
"""
from __future__ import annotations

import json
import os

import pytest

from cobirb.config import Config


def _write_json(path, data):
    path.write_text(json.dumps(data))


def test_missing_files_yield_an_empty_config(tmp_path):
    config = Config(user_path=str(tmp_path / "no-user.json"), repo_path=str(tmp_path / "no-repo.json"))
    assert config.data == {}
    assert config.get("anything") is None
    assert config.get("anything", default="fallback") == "fallback"


def test_user_config_is_loaded_when_repo_config_is_absent(tmp_path):
    user_path = tmp_path / "user.json"
    _write_json(user_path, {"model": "llama3.1"})
    config = Config(user_path=str(user_path), repo_path=str(tmp_path / "no-repo.json"))
    assert config.get("model") == "llama3.1"


def test_repo_config_overrides_user_config_for_the_same_key(tmp_path):
    user_path, repo_path = tmp_path / "user.json", tmp_path / "repo.json"
    _write_json(user_path, {"model": "llama3.1", "persona": "noah"})
    _write_json(repo_path, {"model": "qwen2.5"})

    config = Config(user_path=str(user_path), repo_path=str(repo_path))

    assert config.get("model") == "qwen2.5"  # repo wins on conflict
    assert config.get("persona") == "noah"  # untouched sibling key survives


def test_nested_config_merges_rather_than_replacing_whole_subtree(tmp_path):
    """A repo config narrowly overriding one nested key must not wipe out
    sibling keys the user set at the same nesting level — a naive
    dict.update() at the top level would silently drop them."""
    user_path, repo_path = tmp_path / "user.json", tmp_path / "repo.json"
    _write_json(user_path, {"models": {"default": {"name": "llama3.1", "base_url": "http://x"}}})
    _write_json(repo_path, {"models": {"default": {"name": "qwen2.5"}}})

    config = Config(user_path=str(user_path), repo_path=str(repo_path))

    assert config.get("models", "default", "name") == "qwen2.5"
    assert config.get("models", "default", "base_url") == "http://x"


def test_get_returns_default_for_a_path_that_does_not_exist(tmp_path):
    user_path = tmp_path / "user.json"
    _write_json(user_path, {"model": "llama3.1"})
    config = Config(user_path=str(user_path), repo_path=str(tmp_path / "no-repo.json"))

    assert config.get("does", "not", "exist") is None
    assert config.get("does", "not", "exist", default="x") == "x"


def test_get_stops_cleanly_when_descending_through_a_non_dict_value(tmp_path):
    """"model" is a plain string; asking for a key underneath it must
    return the default, not raise."""
    user_path = tmp_path / "user.json"
    _write_json(user_path, {"model": "llama3.1"})
    config = Config(user_path=str(user_path), repo_path=str(tmp_path / "no-repo.json"))

    assert config.get("model", "nested", "deeper") is None


def test_set_creates_intermediate_dicts_as_needed():
    config = Config(user_path="/no/such/file.json", repo_path="/no/such/other.json")
    config.set("models", "default", "name", value="llama3.1")
    assert config.get("models", "default", "name") == "llama3.1"


def test_set_then_get_round_trips_a_top_level_value():
    config = Config(user_path="/no/such/file.json", repo_path="/no/such/other.json")
    config.set("persona", value="professional")
    assert config.get("persona") == "professional"


def test_set_overwrites_a_non_dict_value_that_is_in_the_way():
    config = Config(user_path="/no/such/file.json", repo_path="/no/such/other.json")
    config.set("a", value="just a string")
    config.set("a", "b", value="x")
    assert config.get("a", "b") == "x"


def test_malformed_json_config_file_is_reported_but_not_silently_ignored(tmp_path, capsys):
    """A config file that exists but fails to parse must not be treated as
    if it were missing — that would hide a real mistake. It is reported by
    name, with the parse error, and then skipped: raising instead took down
    every command including ones that read no config at all."""
    user_path = tmp_path / "user.json"
    user_path.write_text("{not valid json")

    config = Config(user_path=str(user_path), repo_path=str(tmp_path / "no-repo.json"))

    assert config.data == {}
    err = capsys.readouterr().err
    assert str(user_path) in err
    assert "ignoring" in err


def test_bundled_example_config_is_valid_and_loadable(tmp_path):
    """cobirb.json.example (the starting point the README points users at) must
    stay valid, loadable JSON that Config can actually read — a stale or
    broken example is worse than no example at all."""
    example_path = os.path.join(os.path.dirname(__file__), "..", "cobirb.json.example")
    config = Config(user_path=example_path, repo_path=str(tmp_path / "no-repo.json"))

    assert config.get("default_model")
    assert config.get("models", "default", "name")
    assert config.get("persona")


def test_a_malformed_config_is_reported_and_skipped_not_fatal(tmp_path, capsys):
    """A stray comma used to traceback out of every command, `cobirb help`
    included — it constructs a Config too, and never reads a key from it."""
    (tmp_path / "cobirb.json").write_text('{"default_model": "m",}')

    config = Config(cwd=str(tmp_path))

    assert config.get("default_model") is None
    assert "ignoring" in capsys.readouterr().err


def test_a_config_that_is_valid_json_but_not_an_object_is_skipped(tmp_path, capsys):
    (tmp_path / "cobirb.json").write_text('["not", "an", "object"]')

    assert Config(cwd=str(tmp_path)).data == {}
    assert "expected a JSON object" in capsys.readouterr().err


def test_a_broken_repo_config_does_not_discard_the_user_config(tmp_path, monkeypatch):
    """The layers are independent: one being unreadable must not take the
    other down with it."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    user = tmp_path / ".cobirb"
    user.mkdir()
    (user / "config.json").write_text('{"default_model": "from-user"}')
    (tmp_path / "cobirb.json").write_text("{oops")

    assert Config(cwd=str(tmp_path)).get("default_model") == "from-user"
