"""Tests for ``cobirb doctor`` — the checks that answer "am I ready to go?".

The environment and install checks are kept off by default here and exercised
with stubs: this suite must not depend on a running model server, and must not
report on whatever state CoBirb's own checkout happens to be in.
"""
from __future__ import annotations

import pytest

from conftest import write_config

from cobirb.config import Config
from cobirb.runtime import doctor


def _run(tmp_path, data=None, **kwargs):
    if data is not None:
        write_config(tmp_path, data)
    kwargs.setdefault("check_environment", False)
    kwargs.setdefault("check_install", False)
    return doctor.run(config=Config(), **kwargs)


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


# --------------------------------------------------------------------------- #
# Config: the half that fails silently today
# --------------------------------------------------------------------------- #
def test_a_clean_config_is_ready_to_go(tmp_path):
    report = _run(tmp_path, {"persona": "none", "redact_secrets": True})

    assert report.ok
    assert "ready to go" in report.describe()


def test_an_unknown_key_is_a_failure_not_a_shrug(tmp_path):
    """`Config.get` is a plain lookup, so a typo is accepted in silence:
    `redact_secret` reads as redaction off while it stays on. That is exactly
    what this command exists to catch."""
    report = _run(tmp_path, {"redact_secret": False})

    assert not report.ok
    detail = _check(report, "config keys").detail
    assert "redact_secret" in detail
    assert "silence" in detail


def test_several_unknown_keys_are_all_named(tmp_path):
    report = _run(tmp_path, {"modles": {}, "persona": "none", "reddact": 1})

    detail = _check(report, "config keys").detail
    assert "modles" in detail and "reddact" in detail


def test_a_value_of_the_wrong_type_is_reported(tmp_path):
    report = _run(tmp_path, {"allow_tools": "read_file"})   # should be a list

    assert not report.ok
    assert "allow_tools" in _check(report, "config value types").detail


def test_a_boolean_where_a_number_belongs_is_reported(tmp_path):
    """bool is an int in Python, so this is the one that slips through a
    naive isinstance check."""
    report = _run(tmp_path, {"context_tokens": True})

    assert not report.ok
    assert "context_tokens" in _check(report, "config value types").detail


def test_allow_tools_naming_a_tool_that_does_not_exist_is_a_warning(tmp_path):
    report = _run(tmp_path, {"allow_tools": ["read_file", "reed_file"]})

    check = _check(report, "config references")
    assert check.status == doctor.WARN
    assert "reed_file" in check.detail
    assert report.ok        # worth knowing, but it does not stop a turn working


def test_a_scoped_shell_rule_is_understood(tmp_path):
    report = _run(tmp_path, {"allow_tools": ["shell(git status)"]})
    assert _check(report, "config references").status == doctor.OK


def test_an_approved_directory_that_is_not_there_is_a_warning(tmp_path):
    report = _run(tmp_path, {"allow_read_dirs": [str(tmp_path / "nope")]})

    check = _check(report, "config references")
    assert check.status == doctor.WARN
    assert "not a directory" in check.detail


def test_a_directory_that_is_there_passes(tmp_path):
    report = _run(tmp_path, {"allow_read_dirs": [str(tmp_path)]})
    assert _check(report, "config references").status == doctor.OK


def test_a_max_num_ctx_that_is_not_a_size_is_a_warning(tmp_path):
    """A string is allowed there so "64k" can be written, which makes a
    string that says nothing the one way the key can be well-typed and still
    do nothing — uncapped in silence, which is what setting it was avoiding."""
    report = _run(tmp_path, {"max_num_ctx": "64kb"})

    check = _check(report, "config references")
    assert check.status == doctor.WARN
    assert "64kb" in check.detail


def test_both_spellings_of_max_num_ctx_pass(tmp_path):
    for value in ("64k", 65536):
        report = _run(tmp_path, {"max_num_ctx": value})
        assert _check(report, "config value types").status == doctor.OK
        assert _check(report, "config references").status == doctor.OK


def test_no_config_file_at_all_is_fine(tmp_path):
    """Defaults apply, and that is a legitimate way to run."""
    report = _run(tmp_path)

    assert report.ok
    assert _check(report, "config file").status == doctor.WARN


def test_unparseable_json_is_a_failure_that_names_the_file(tmp_path):
    import os

    directory = os.path.join(str(tmp_path), ".cobirb")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as handle:
        handle.write("{ not json,")

    report = _run(tmp_path)

    assert not report.ok
    assert "could not be read" in _check(report, "config file").detail


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
class _Provider:
    def __init__(self, available=("a-model",), vision=True):
        self._available = list(available)
        self._vision = vision

    def list_models(self):
        return self._available

    def supports_vision(self):
        return self._vision


def test_an_unreachable_endpoint_fails_rather_than_waiting_for_a_turn(tmp_path):
    def build(role):
        raise RuntimeError("Could not reach the model provider")

    report = _run(tmp_path, {}, check_environment=True, build_provider=build)

    assert not report.ok
    assert "not reachable" in _check(report, "model endpoint").detail


def test_a_model_named_but_never_pulled_is_a_failure_with_the_fix_in_it(tmp_path):
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "never-pulled"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["something-else"]),
    )

    assert not report.ok
    detail = _check(report, "model (default)").detail
    assert "never-pulled" in detail
    assert "ollama pull never-pulled" in detail


def test_a_model_that_is_there_passes(tmp_path):
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "a-model"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["a-model"]),
    )

    assert _check(report, "model (default)").status == doctor.OK


def test_a_model_without_vision_says_so_without_failing(tmp_path):
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "a-model"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["a-model"], vision=False),
    )

    check = _check(report, "model (default)")
    assert check.status == doctor.OK
    assert "no vision" in check.detail


# --------------------------------------------------------------------------- #
# The report itself
# --------------------------------------------------------------------------- #
def test_a_warning_is_not_a_failure():
    report = doctor.Report()
    report.add("something", doctor.WARN, "worth knowing")

    assert report.ok
    assert "worth knowing" in report.describe()


def test_a_failure_makes_the_whole_report_not_ok():
    report = doctor.Report()
    report.add("fine", doctor.OK)
    report.add("broken", doctor.FAIL, "this one")

    assert not report.ok
    assert "1 problem(s)" in report.describe()


@pytest.mark.parametrize("status,mark", [(doctor.OK, "✓"), (doctor.WARN, "!"), (doctor.FAIL, "✗")])
def test_each_status_is_marked_distinctly(status, mark):
    report = doctor.Report()
    report.add("a check", status)
    assert mark in report.describe()


# --------------------------------------------------------------------------- #
# Model names and the implied ":latest" tag.
#
# Ollama treats `gemma4` and `gemma4:latest` as the same model and serves
# either, but `list_models` only ever reports the qualified form. Comparing
# them as raw strings tells someone to pull a model they already have.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,expected",
    [
        ("gemma4", "gemma4:latest"),
        ("gemma4:latest", "gemma4:latest"),
        ("ornith-1.5:9b", "ornith-1.5:9b"),
        ("registry.example.com:5000/thing", "registry.example.com:5000/thing:latest"),
        ("", ""),
    ],
)
def test_a_missing_tag_means_latest(name, expected):
    assert doctor.canonical_model(name) == expected


def test_a_bare_name_matches_the_endpoints_latest_tag(tmp_path):
    """The reported bug: `gemma4-unchained` configured, `gemma4-unchained:latest`
    on the endpoint, and doctor calling it missing."""
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "gemma4-unchained"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["gemma4-unchained:latest"]),
    )

    assert report.ok
    assert _check(report, "model (default)").status == doctor.OK


def test_a_latest_tag_matches_an_endpoint_listing_without_one(tmp_path):
    """The same confusion in the other direction."""
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "thing:latest"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["thing"]),
    )

    assert _check(report, "model (default)").status == doctor.OK


def test_a_genuinely_absent_model_is_still_a_failure(tmp_path):
    """The fix must not turn the check into one that always passes."""
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "never-pulled"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["gemma4:latest"]),
    )

    assert not report.ok
    assert "never-pulled" in _check(report, "model (default)").detail


def test_a_different_tag_of_a_present_model_is_still_a_failure(tmp_path):
    """`thing:9b` is not `thing:latest` — only the *absent* tag is implied."""
    report = _run(
        tmp_path,
        {"models": {"default": {"name": "thing:70b"}}},
        check_environment=True,
        build_provider=lambda role: _Provider(available=["thing:9b"]),
    )

    assert not report.ok


# --------------------------------------------------------------------------- #
# Install: which of the three shapes, and whether --upgrade can work here
# --------------------------------------------------------------------------- #
def _install_report(tmp_path, monkeypatch, install):
    from cobirb.runtime import upgrade as upgrade_module

    monkeypatch.setattr(upgrade_module, "detect_install", lambda: install)
    return _run(tmp_path, {}, check_install=True)


def test_a_managed_install_is_fine_rather_than_a_missing_checkout(tmp_path, monkeypatch):
    """Installing with install.sh is an ordinary, supported way to have
    CoBirb. Reporting it as "no git checkout found" would tell most users
    something is wrong with a perfectly good install."""
    from cobirb.runtime import upgrade as upgrade_module

    report = _install_report(tmp_path, monkeypatch, upgrade_module.Install(
        kind=upgrade_module.MANAGED, root="/x", venv="/x/venv", version="0.13.1",
    ))

    assert _check(report, "install").status == doctor.OK
    assert "/x/venv" in _check(report, "install").detail
    assert report.ok


def test_a_managed_install_is_not_checked_against_the_latest_release(tmp_path, monkeypatch):
    """Answering "is there a newer one?" means asking GitHub, and doctor talks
    to the endpoint you configured and nothing else. `--upgrade` is where that
    question gets asked, by someone who typed it."""
    from cobirb.runtime import upgrade as upgrade_module

    def _boom(**kwargs):
        raise AssertionError("doctor reached for a release listing")

    monkeypatch.setattr(upgrade_module, "_latest_tag", _boom)
    report = _install_report(tmp_path, monkeypatch, upgrade_module.Install(
        kind=upgrade_module.MANAGED, root="/x", venv="/x/venv", version="0.13.1",
    ))

    assert _check(report, "version").status == doctor.OK


def test_an_unmanaged_install_warns_and_names_the_installer(tmp_path, monkeypatch):
    from cobirb.runtime import upgrade as upgrade_module

    report = _install_report(
        tmp_path, monkeypatch, upgrade_module.Install(kind=upgrade_module.UNMANAGED)
    )

    check = _check(report, "install")
    assert check.status == doctor.WARN
    assert "install.sh" in check.detail
    assert report.ok  # worth knowing, but nothing here is broken
