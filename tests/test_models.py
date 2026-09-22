"""Tests for per-role model selection.

The contract is inheritance: a role that says nothing gets the default's
answer, a role that says something gets its own, and a config that predates
roles entirely keeps working unchanged.
"""
from __future__ import annotations

import json

import pytest

from conftest import write_config

from cobirb.config import Config
from cobirb.runtime.models import (
    model_options,
    ROLE_DEFAULT,
    ROLE_ORCHESTRATOR,
    ROLE_WORKER,
    build_for_role,
    describe_roles,
    parse_context_size,
    resolve_role,
)
from cobirb.runtime.wiring import build_model


def _config(tmp_path, data) -> Config:
    write_config(tmp_path, json.loads(json.dumps(data)))
    return Config()


def _advertising(context_length: int):
    """An endpoint whose /api/show says the model can do ``context_length``."""

    class _Response:
        def __init__(self):
            self._body = json.dumps({"model_info": {"qwen2.context_length": context_length}}).encode()

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    return lambda request, timeout=None: _Response()


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


def test_max_num_ctx_caps_the_window_every_role_asks_for(tmp_path, monkeypatch):
    """A global cap, not a per-role one: the flock runs a planner and several
    workers against one endpoint, so a ceiling that only one role honoured
    would not keep the card's VRAM inside itself."""
    monkeypatch.setattr("urllib.request.urlopen", _advertising(262144))
    config = _config(tmp_path, {"model": "m", "max_num_ctx": 32768})

    for role in (ROLE_DEFAULT, ROLE_ORCHESTRATOR, ROLE_WORKER):
        assert build_for_role(role, config).context_window() == 32768


def test_without_max_num_ctx_the_model_still_gets_what_it_advertises(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _advertising(262144))
    config = _config(tmp_path, {"model": "m"})

    assert build_for_role(ROLE_DEFAULT, config).context_window() == 262144


def test_a_context_size_can_be_written_the_way_people_say_it():
    """Windows are powers of two that everyone names in thousands, so a k is
    1024 — "64k" has to come out as exactly the window people mean by it."""
    assert parse_context_size("64k") == 65536
    assert parse_context_size("64K") == 65536
    assert parse_context_size(" 32k ") == 32768
    assert parse_context_size("128k") == 131072


def test_a_plain_number_is_still_a_context_size():
    assert parse_context_size(65536) == 65536
    assert parse_context_size("65536") == 65536


def test_a_size_that_says_nothing_is_no_cap_rather_than_a_crash():
    """This reads a config file at startup: a stray character in it should
    cost the setting, not the run. `cobirb doctor` is what says so."""
    for junk in (None, "", "   ", "sixty-four k", "64kb", 0, -1, True):
        assert parse_context_size(junk) is None


def test_max_num_ctx_caps_from_the_human_spelling_too(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _advertising(262144))
    config = _config(tmp_path, {"model": "m", "max_num_ctx": "64k"})

    assert build_for_role(ROLE_DEFAULT, config).context_window() == 65536


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


# --------------------------------------------------------------------------- #
# Connect and read are two different waits
# --------------------------------------------------------------------------- #
def test_the_two_timeouts_have_separate_defaults(tmp_path):
    """One 120s socket timeout did both jobs and was wrong for both: a socket
    timeout measures silence, not work, so a request queued behind another
    worker's generation timed out having never sent a prompt."""
    from cobirb.plugins.core.model import DEFAULT_CONNECT_TIMEOUT, DEFAULT_REQUEST_TIMEOUT

    provider = build_for_role("worker", _config(tmp_path, {}))

    assert provider._connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert provider._request_timeout == DEFAULT_REQUEST_TIMEOUT
    # Waiting for a busy endpoint is the long one; asking whether anything is
    # listening is the short one, and that ordering is the whole point.
    assert provider._request_timeout > provider._connect_timeout


def test_both_timeouts_can_be_configured(tmp_path):
    provider = build_for_role(
        "worker", _config(tmp_path, {"connect_timeout": 3, "request_timeout": 1200})
    )

    assert provider._connect_timeout == 3
    assert provider._request_timeout == 1200


@pytest.mark.parametrize("value", ["soon", 0, -5, None])
def test_an_unusable_timeout_costs_the_setting_not_the_run(tmp_path, value):
    """It reads a config file at startup. A stray value should cost the
    setting, the same way `max_num_ctx` does."""
    from cobirb.plugins.core.model import DEFAULT_REQUEST_TIMEOUT

    provider = build_for_role("worker", _config(tmp_path, {"request_timeout": value}))

    assert provider._request_timeout == DEFAULT_REQUEST_TIMEOUT


# --------------------------------------------------------------------------- #
# Sampling options
# --------------------------------------------------------------------------- #
def test_a_role_inherits_the_default_options_key_by_key(tmp_path):
    write_config(tmp_path, {"models": {
        "default": {"name": "m", "options": {"temperature": 0.7, "seed": 1}},
        "worker": {"options": {"seed": 7}},
    }})

    assert model_options("worker", Config()) == {"temperature": 0.7, "seed": 7}
    assert model_options("default", Config()) == {"temperature": 0.7, "seed": 1}


def test_num_ctx_is_not_an_option_because_max_num_ctx_owns_the_window(tmp_path):
    write_config(tmp_path, {"models": {"default": {"name": "m", "options": {"num_ctx": 4096, "top_p": 0.9}}}})

    assert model_options("default", Config()) == {"top_p": 0.9}


def test_options_that_are_not_an_object_are_ignored(tmp_path):
    write_config(tmp_path, {"models": {"default": {"name": "m", "options": "hot"}}})

    assert model_options("default", Config()) == {}


def test_models_default_name_wins_over_the_older_spellings(tmp_path):
    write_config(tmp_path, {"model": "old", "default_model": "older", "models": {"default": {"name": "new"}}})

    assert resolve_role("orchestrator", Config()).name == "new"


def test_an_older_spelling_still_works_on_its_own(tmp_path):
    write_config(tmp_path, {"default_model": "older"})

    assert resolve_role("orchestrator", Config()).name == "older"
