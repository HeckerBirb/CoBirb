"""Tests for the plugin loader."""
from __future__ import annotations

import types

from cobirb.plugins.loader import _INTERFACES, _is_subclass, load_plugins


def test_interfaces_cover_all_spi_contracts():
    # Keys are lowercase interface identifiers, not class names.
    assert "model" in _INTERFACES
    assert "tool" in _INTERFACES
    assert "io" in _INTERFACES
    assert "crypto" in _INTERFACES


def test_is_subclass_true_for_concrete():
    from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto

    assert _is_subclass(AesGcmScryptSessionCrypto, object)


def test_is_subclass_false_for_identical_class():
    from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto

    assert not _is_subclass(AesGcmScryptSessionCrypto, AesGcmScryptSessionCrypto)


def test_is_subclass_false_for_instances():
    from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto
    assert not _is_subclass(AesGcmScryptSessionCrypto(), object)


def test_load_plugins_returns_tuple(monkeypatch, tmp_path):
    import cobirb

    monkeypatch.setattr(cobirb, "__path__", [str(tmp_path)])
    discovered, errors = load_plugins()
    assert isinstance(discovered, dict)
    assert isinstance(errors, dict)


def test_load_plugins_empty(monkeypatch, tmp_path):
    # No installed entry points and no local plugin directories => nothing found.
    import cobirb.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod.im, "entry_points", lambda **kwargs: [])
    monkeypatch.setenv("COBIRB_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    discovered, errors = load_plugins()
    assert discovered == {}
    assert errors == {}


def test_load_plugins_entry_point_failure_is_captured_not_raised(monkeypatch, tmp_path):
    """A broken entry point must be reported, never crash discovery (fail-closed).

    Regression test: ``kind`` used to be referenced in the except-block before
    the inner loop ever bound it, so a failing ``ep.load()`` raised NameError
    instead of being recorded as an error.
    """
    import cobirb.plugins.loader as loader_mod

    class _BrokenEntryPoint:
        name = "broken-plugin"

        def load(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(loader_mod.im, "entry_points", lambda **kwargs: [_BrokenEntryPoint()])
    monkeypatch.setenv("COBIRB_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))

    discovered, errors = load_plugins()
    assert discovered == {}
    assert "entry-point:broken-plugin" in errors
    assert "boom" in errors["entry-point:broken-plugin"]
