"""Tests for the plugin loader."""
from __future__ import annotations

import types

from cobirb.plugins.loader import _INTERFACES, _is_subclass, load_plugins
from cobirb.typing.spi import Tool


class _FakeTool(Tool):
    def name(self):
        return "fake_tool"

    def description(self):
        return "a fake tool for tests"

    def parameters(self):
        return {"type": "object", "properties": {}}

    def execute(self, arguments):
        return None


class _FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


class _FakeDistribution:
    def __init__(self, entry_points):
        self.entry_points = entry_points


def _fake_importlib(distribution_fn):
    """A stand-in for the real `importlib` module, exposing just the one
    attribute path (`.metadata.distribution`) the loader actually calls."""
    return types.SimpleNamespace(metadata=types.SimpleNamespace(distribution=distribution_fn))


def _make_local_plugin_dir(tmp_path, name="my-tool"):
    project_dir = tmp_path / "project"
    plugin_dir = project_dir / "cobirb" / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pyproject.toml").write_text('[project]\nname = "my-tool"\n')
    return project_dir, plugin_dir


def _isolate_plugin_dirs(monkeypatch, project_dir, tmp_path):
    monkeypatch.setenv("COBIRB_PROJECT_DIR", str(project_dir))
    # No real user-level plugin directory should leak into these tests.
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr("cobirb.plugins.loader.im.entry_points", lambda **kwargs: [])


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

    ``kind`` must not be referenced in the except-block before the inner loop
    binds it, or a failing ``ep.load()`` raises NameError instead of being
    recorded as an error.
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


# --------------------------------------------------------------------------- #
# SPI versioning (frozen at v0.7.0). A plugin declares COBIRB_SPI; declaring
# nothing means 1. The check happens at the loader boundary so an unusable
# plugin never reaches the registry — and, like every other plugin failure
# here, it is reported rather than raised.
# --------------------------------------------------------------------------- #
def _entry_point_serving(plugin_class, name="versioned-plugin"):
    class _EntryPoint:
        def __init__(self):
            self.name = name

        def load(self):
            return plugin_class

    return _EntryPoint()


def _discover_only(monkeypatch, tmp_path, entry_point):
    """load_plugins() with nothing but ``entry_point`` in play."""
    import cobirb.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod.im, "entry_points", lambda **kwargs: [entry_point])
    monkeypatch.setenv("COBIRB_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    return load_plugins()


class _PlainTool(Tool):
    def name(self):
        return "plain"

    def description(self):
        return ""

    def parameters(self):
        return {}

    def execute(self, arguments):
        return None


def test_a_plugin_declaring_no_spi_version_is_taken_as_version_one(monkeypatch, tmp_path):
    """Everything written before the freeze existed, and every plugin whose
    author never thinks about this, has to keep working."""
    discovered, errors = _discover_only(
        monkeypatch, tmp_path, _entry_point_serving(_PlainTool)
    )

    assert discovered == {"tool:versioned-plugin": _PlainTool}
    assert errors == {}


def test_a_plugin_from_the_future_is_refused_with_an_actionable_message(monkeypatch, tmp_path):
    """It would be calling into methods this core doesn't have. Loading it to
    fail later, at a worse moment, helps nobody — and the message has to tell
    its user which side needs upgrading."""

    class _FromTheFuture(_PlainTool):
        COBIRB_SPI = 99

    discovered, errors = _discover_only(
        monkeypatch, tmp_path, _entry_point_serving(_FromTheFuture)
    )

    assert discovered == {}
    assert "upgrade CoBirb" in errors["entry-point:versioned-plugin"]


def test_a_non_integer_spi_declaration_is_a_mistake_not_a_shrug(monkeypatch, tmp_path):
    """`COBIRB_SPI = "1"` would otherwise compare unequal to every supported
    version forever, or be skipped and let a real mismatch through."""

    class _Stringly(_PlainTool):
        COBIRB_SPI = "1"

    discovered, errors = _discover_only(
        monkeypatch, tmp_path, _entry_point_serving(_Stringly)
    )

    assert discovered == {}
    assert "must be an integer" in errors["entry-point:versioned-plugin"]


def test_one_incompatible_plugin_does_not_stop_the_others(monkeypatch, tmp_path):
    """The whole point of reporting rather than raising."""

    class _FromTheFuture(_PlainTool):
        COBIRB_SPI = 99

    import cobirb.plugins.loader as loader_mod

    monkeypatch.setattr(
        loader_mod.im,
        "entry_points",
        lambda **kwargs: [
            _entry_point_serving(_FromTheFuture, "too-new"),
            _entry_point_serving(_PlainTool, "fine"),
        ],
    )
    monkeypatch.setenv("COBIRB_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))

    discovered, errors = load_plugins()

    assert "tool:fine" in discovered
    assert "entry-point:too-new" in errors


# --------------------------------------------------------------------------- #
# Local plugin directories (cobirb/plugins/<name>/). This mechanism is more
# subtle than it looks: a dropped-in directory is only actually usable once
# it's *also* installed as a real Python distribution named
# cobirb_plugins_<name> (dashes -> underscores) — the pyproject.toml is
# only used as a "does this look like a plugin" marker, not parsed for its
# own entry points. These tests document and verify that real mechanism.
# --------------------------------------------------------------------------- #
def test_local_plugin_dir_without_pyproject_toml_is_silently_skipped(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "cobirb" / "plugins" / "not-a-plugin").mkdir(parents=True)  # no pyproject.toml

    _isolate_plugin_dirs(monkeypatch, project_dir, tmp_path)
    discovered, errors = load_plugins()

    assert discovered == {}
    assert errors == {}


def test_local_plugin_dir_without_installed_distribution_fails_closed(monkeypatch, tmp_path):
    """The common real-world case: someone drops a directory into
    cobirb/plugins/ without also `pip install -e`-ing it as a distribution
    under the derived name. Must be recorded as an error, never raised —
    a broken/incomplete plugin must not crash discovery for everything else.
    """
    project_dir, _ = _make_local_plugin_dir(tmp_path)
    _isolate_plugin_dirs(monkeypatch, project_dir, tmp_path)

    # No monkeypatched distribution() here: the real importlib.metadata
    # genuinely won't find "cobirb_plugins_my_tool" installed, which is
    # exactly the failure mode being tested.
    discovered, errors = load_plugins()

    assert discovered == {}
    assert errors  # captured, not left to propagate


def test_local_plugin_dir_loads_when_backed_by_an_installed_distribution(monkeypatch, tmp_path):
    project_dir, _ = _make_local_plugin_dir(tmp_path)
    _isolate_plugin_dirs(monkeypatch, project_dir, tmp_path)

    def fake_distribution(name):
        assert name == "cobirb_plugins_my_tool"
        return _FakeDistribution([_FakeEntryPoint("tool", _FakeTool)])

    monkeypatch.setattr("cobirb.plugins.loader.importlib", _fake_importlib(fake_distribution))

    discovered, errors = load_plugins()

    assert discovered == {"tool:my-tool": _FakeTool}
    assert errors == {}


def test_local_plugin_entry_point_not_matching_declared_interface_is_rejected(monkeypatch, tmp_path):
    """An entry point named "tool" whose target doesn't actually subclass
    Tool must not be trusted just because the name matches — the same
    standard the installed-entry-points path already holds itself to
    (see load_plugins()'s _is_subclass check on that path)."""
    project_dir, _ = _make_local_plugin_dir(tmp_path)
    _isolate_plugin_dirs(monkeypatch, project_dir, tmp_path)

    class _NotATool:
        pass

    monkeypatch.setattr(
        "cobirb.plugins.loader.importlib",
        _fake_importlib(lambda name: _FakeDistribution([_FakeEntryPoint("tool", _NotATool)])),
    )

    discovered, errors = load_plugins()

    assert discovered == {}
