"""Tests for `cobirb setup` and saving a default model."""
from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import write_config

from cobirb import paths
from cobirb.runtime import setup


def _answers(*values):
    queue = list(values)
    return lambda prompt: queue.pop(0)


def _fake_models(monkeypatch, models=("a:1", "b:2"), fail=None):
    def list_models(self):
        if fail:
            raise RuntimeError(fail)
        return list(models)

    for cls in ("cobirb.plugins.core.model.LocalModelProvider",
                "cobirb.plugins.core.openai.OpenAICompatibleProvider"):
        monkeypatch.setattr(f"{cls}.list_models", list_models)
    monkeypatch.setattr("cobirb.runtime.doctor.run", lambda: type("R", (), {"ok": True, "describe": lambda s: "ready"})())


def test_saving_keeps_every_other_setting(tmp_path):
    write_config(tmp_path, {"allow_tools": ["read_file"], "model": "old", "models": {"worker": {"name": "w"}}})

    setup.save_default_model("new:1", base_url="http://localhost:11434", api="ollama")

    data = json.load(open(paths.config_path()))
    assert data["models"]["default"] == {"name": "new:1", "base_url": "http://localhost:11434", "api": "ollama"}
    assert data["models"]["worker"] == {"name": "w"}
    assert data["allow_tools"] == ["read_file"]
    assert "model" not in data  # it would outrank the choice just made
    assert stat.S_IMODE(os.stat(paths.config_path()).st_mode) == 0o600


def test_a_config_that_does_not_parse_is_never_overwritten(tmp_path):
    path = write_config(tmp_path, {})
    open(path, "w").write("{ not json")

    with pytest.raises(setup.ConfigUnreadable):
        setup.save_default_model("x")
    assert open(path).read() == "{ not json"


def test_setup_asks_lists_and_saves(monkeypatch):
    _fake_models(monkeypatch)
    said = []

    code = setup.run(ask=_answers("", "", "2"), say=said.append, interactive=True)

    assert code == 0
    assert json.load(open(paths.config_path()))["models"]["default"]["name"] == "b:2"
    assert any("b:2" in line for line in said)


def test_a_server_on_another_port_is_offered_the_openai_protocol(monkeypatch):
    _fake_models(monkeypatch)

    setup.run(ask=_answers("localhost:8080", "", "a:1"), say=lambda s: None, interactive=True)

    default = json.load(open(paths.config_path()))["models"]["default"]
    assert default == {"name": "a:1", "base_url": "http://localhost:8080", "api": "openai"}


def test_an_unreachable_server_changes_nothing(monkeypatch):
    _fake_models(monkeypatch, fail="connection refused")
    said = []

    code = setup.run(ask=_answers("", ""), say=said.append, interactive=True)

    assert code == 1
    assert "Nothing was changed" in " ".join(said)
    assert not os.path.exists(paths.config_path()) or "name\": \"a" not in open(paths.config_path()).read()


def test_setup_refuses_without_a_terminal():
    assert setup.run(ask=_answers(), say=lambda s: None, interactive=False) == 1
