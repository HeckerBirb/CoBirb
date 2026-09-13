"""Tests for per-role model selection.

The contract is inheritance: a role that says nothing gets the default's
answer, a role that says something gets its own, and a config that predates
roles entirely keeps working unchanged.
"""
from __future__ import annotations

import json

from conftest import write_config

from cobirb.config import Config
from cobirb.runtime.models import (
    ROLE_DEFAULT,
    ROLE_ORCHESTRATOR,
    ROLE_WORKER,
    build_for_role,
    describe_roles,
    resolve_role,
)
from cobirb.runtime.wiring import build_model


def _config(tmp_path, data) -> Config:
    write_config(tmp_path, json.loads(json.dumps(data)))
    return Config()


def test_a_config_with_no_roles_gives_every_role_the_same_model(tmp_path):
    """The overwhelmingly common case, and the one that must not have changed:
    one model, used for everything."""
    config = _config(tmp_path, {"model": "qwen2.5-coder:32b"})

    for role in (ROLE_DEFAULT, ROLE_ORCHESTRATOR, ROLE_WORKER):
        assert resolve_role(role, config).name == "qwen2.5-coder:32b"


def test_a_role_can_name_its_own_model(tmp_path):
    config = _config(
        tmp_path,
        {"models": {"default": {"name": "big"}, "worker": {"name": "small"}}},
    )

    assert resolve_role(ROLE_WORKER, config).name == "small"
    assert resolve_role(ROLE_ORCHESTRATOR, config).name == "big"


def test_a_role_inherits_the_endpoint_it_does_not_state(tmp_path):
    """The reason roles are more than a dict of names: repeating the endpoint
    per role is how two roles end up pointed at different servers by
    omission."""
    config = _config(
        tmp_path,
        {
            "models": {
                "default": {"name": "big", "base_url": "http://gpu-box:11434"},
                "worker": {"name": "small"},
            }
        },
    )

    assert resolve_role(ROLE_WORKER, config).base_url == "http://gpu-box:11434"


def test_a_role_can_also_use_its_own_endpoint(tmp_path):
    """Two machines is a normal shape for this: the big model on the box with
    the GPUs, the small one locally."""
    config = _config(
        tmp_path,
        {
            "models": {
                "default": {"name": "big", "base_url": "http://gpu-box:11434"},
                "worker": {"name": "small", "base_url": "http://localhost:11434"},
            }
        },
    )

    assert resolve_role(ROLE_WORKER, config).base_url == "http://localhost:11434"


def test_the_model_flag_outranks_every_configured_role(tmp_path):
    """--model names the model for this run. A run that silently used a
    different one because a role was configured would be indefensible."""
    config = _config(tmp_path, {"models": {"orchestrator": {"name": "configured"}}})

    assert resolve_role(ROLE_ORCHESTRATOR, config, override="asked-for").name == "asked-for"


def test_an_unknown_role_falls_back_rather_than_failing(tmp_path):
    """A typo should cost the specialisation, never the run."""
    config = _config(tmp_path, {"model": "the-one-model"})

    assert resolve_role("reviewr", config).name == "the-one-model"


def test_nothing_configured_resolves_to_no_model(tmp_path):
    """Reported by the first turn that needs one, where it can say what to do
    about it — not raised here, where it would take down `cobirb models`."""
    spec = resolve_role(ROLE_DEFAULT, Config())

    assert not spec.configured
    assert "none configured" in spec.describe()


def test_all_three_legacy_keys_still_name_the_default_model(tmp_path):
    for key in ("model", "default_model"):
        assert resolve_role(ROLE_DEFAULT, _config(tmp_path, {key: "legacy"})).name == "legacy"
    nested = _config(tmp_path, {"models": {"default": {"name": "nested"}}})
    assert resolve_role(ROLE_DEFAULT, nested).name == "nested"


def test_build_model_still_resolves_the_model_every_front_end_asks_for(tmp_path):
    """The public entry point the CLI and TUI both use. It now goes through
    the orchestrator role, and must still answer the same for a plain config."""
    config = _config(tmp_path, {"model": "llama3.1"})

    assert build_model(None, str(tmp_path), config).name() == "ollama/llama3.1"
    assert build_model("override", str(tmp_path), config).name() == "ollama/override"


def test_build_for_role_produces_a_usable_provider(tmp_path):
    config = _config(tmp_path, {"models": {"worker": {"name": "small"}}})

    assert build_for_role(ROLE_WORKER, config).name() == "ollama/small"


def test_the_listing_says_where_each_answer_came_from(tmp_path):
    """`cobirb models` exists to answer "so which one actually runs?" — a list
    of names without their provenance answers half of that."""
    config = _config(
        tmp_path, {"models": {"default": {"name": "big"}, "worker": {"name": "small"}}}
    )

    listed = {spec.role: spec for spec in describe_roles(config)}

    assert "inherited" in listed[ROLE_ORCHESTRATOR].source
    assert "inherited" not in listed[ROLE_WORKER].source


def test_a_role_nobody_recognises_is_still_listed(tmp_path):
    """So a typo is visible next to the roles that work, rather than being a
    silent no-op."""
    config = _config(tmp_path, {"models": {"default": {"name": "big"}, "reviewr": {"name": "x"}}})

    assert any(spec.role == "reviewr" for spec in describe_roles(config))
