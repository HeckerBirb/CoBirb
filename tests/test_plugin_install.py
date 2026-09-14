"""Tests for ``cobirb plugin install/list/remove``.

Most of this mocks ``_run_pip`` — the validation and rollback logic is the
part worth testing cheaply and often. But the actual point of this module is
that a real ``pip install -e`` genuinely becomes discoverable afterward, and a
mock can't prove that; one test runs the whole thing for real, the same way
the force-stop mechanism earlier in this project was proven against a real
socket rather than trusted from its mock. It costs a couple of seconds
(pip building an editable wheel) and is fully offline — the fixture plugin
declares no dependencies.
"""
from __future__ import annotations

import textwrap

import pytest

from cobirb.plugins.loader import load_plugins
from cobirb.runtime.plugin_install import (
    PluginInstallError,
    install_plugin,
    list_installed,
    remove_plugin,
)


def _fixture_plugin(tmp_path, name="cobirb_plugins_greeter", entry_points=True):
    """A minimal, real, installable plugin source directory."""
    source = tmp_path / "greeter-source"
    source.mkdir()
    entry_point_block = (
        '[project.entry-points."cobirb.plugins"]\ntool = "greeter_plugin:GreeterTool"\n'
        if entry_points
        else ""
    )
    (source / "pyproject.toml").write_text(textwrap.dedent(f"""\
        [build-system]
        requires = ["setuptools>=61"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "{name}"
        version = "0.0.1"

        {entry_point_block}
        [tool.setuptools]
        py-modules = ["greeter_plugin"]
    """))
    (source / "greeter_plugin.py").write_text(textwrap.dedent("""\
        from cobirb.typing.spi import Tool, ToolResult

        class GreeterTool(Tool):
            def name(self):
                return "greet"
            def description(self):
                return "says hello"
            def parameters(self):
                return {"type": "object", "properties": {}}
            def execute(self, arguments):
                return ToolResult(ok=True, content="hello")
    """))
    return source


def _isolate(monkeypatch, tmp_path):
    """No real local plugins or entry points should leak into these tests."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COBIRB_PROJECT_DIR", str(tmp_path / "empty-project"))


# --------------------------------------------------------------------------- #
# The real thing: install, discover, remove — no mocks.
# --------------------------------------------------------------------------- #
def test_a_plugin_installed_this_way_is_actually_discoverable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    source = _fixture_plugin(tmp_path)

    try:
        result = install_plugin(str(source))
        assert result.name == "greeter"
        assert result.discovered_as == ("tool:greeter",)

        discovered, errors = load_plugins()
        assert "tool:greeter" in discovered
        assert discovered["tool:greeter"]().name() == "greet"
        assert list_installed() == ["greeter"]
    finally:
        remove_plugin("greeter")

    # Gone from disk, and gone from discovery.
    assert list_installed() == []
    discovered, _ = load_plugins()
    assert "tool:greeter" not in discovered


def test_removing_uninstalls_the_pip_package_too(monkeypatch, tmp_path):
    """Not just the directory — the underlying distribution, so a later
    `pip install some-published-plugin` of the same name doesn't collide with
    a stale editable install nobody can see anymore."""
    _isolate(monkeypatch, tmp_path)
    install_plugin(str(_fixture_plugin(tmp_path)))

    result = remove_plugin("greeter")

    assert result.pip_uninstalled
    import importlib.metadata as im

    with pytest.raises(im.PackageNotFoundError):
        im.distribution("cobirb_plugins_greeter")


# --------------------------------------------------------------------------- #
# Validation — cheap, no real pip round trip.
# --------------------------------------------------------------------------- #
def test_a_source_with_no_pyproject_is_refused(tmp_path):
    with pytest.raises(PluginInstallError, match="pyproject.toml"):
        install_plugin(str(tmp_path))


def test_a_pyproject_with_no_project_name_is_refused(tmp_path):
    source = tmp_path / "bad"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')

    with pytest.raises(PluginInstallError, match=r"\[project\].name"):
        install_plugin(str(source))


def test_a_pyproject_declaring_no_cobirb_entry_point_is_refused(tmp_path):
    """Nothing here is a CoBirb plugin as far as the loader is concerned —
    refusing before touching pip is the whole value over discovering that
    later from a confusing "not found" after the fact."""
    source = tmp_path / "bad"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "cobirb_plugins_bad"\nversion = "0.0.1"\n'
    )

    with pytest.raises(PluginInstallError, match="entry-points"):
        install_plugin(str(source))


def test_a_name_not_matching_the_loaders_convention_is_refused(tmp_path):
    """`_load_local_plugin` derives the distribution name it looks up from
    the installed directory's own basename, not from anything in the
    package — so a name that doesn't start with the expected prefix would
    silently never be found, which must be caught here instead."""
    source = tmp_path / "bad"
    source.mkdir()
    (source / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "totally-unrelated-name"
        [project.entry-points."cobirb.plugins"]
        tool = "x:Y"
    """))

    with pytest.raises(PluginInstallError, match="cobirb_plugins_"):
        install_plugin(str(source))


def test_installing_over_an_existing_plugin_is_refused_without_replace(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from cobirb import paths

    existing = tmp_path / "home" / ".cobirb" / "plugins" / "greeter"
    existing.mkdir(parents=True)
    assert paths.user_plugins_dir() == str(tmp_path / "home" / ".cobirb" / "plugins")

    with pytest.raises(PluginInstallError, match="already exists"):
        install_plugin(str(_fixture_plugin(tmp_path)))


def test_a_failed_replace_puts_the_working_plugin_back(monkeypatch, tmp_path):
    """--replace must not cost you the plugin you already had. A reinstall
    whose new source doesn't work has to leave the old one installed and
    discoverable, not leave you with neither — so this runs the real pip
    round trip both ways rather than mocking it: the restore is only real if
    the *distribution* comes back too, not just the directory."""
    _isolate(monkeypatch, tmp_path)
    install_plugin(str(_fixture_plugin(tmp_path)))

    broken = tmp_path / "broken-source"
    broken.mkdir()
    (broken / "pyproject.toml").write_text(textwrap.dedent("""\
        [build-system]
        requires = ["setuptools>=61"]
        build-backend = "setuptools.build_meta"
        [project]
        name = "cobirb_plugins_greeter"
        version = "0.0.2"
        [project.entry-points."cobirb.plugins"]
        tool = "greeter_plugin:NOT_A_TOOL"
        [tool.setuptools]
        py-modules = ["greeter_plugin"]
    """))
    (broken / "greeter_plugin.py").write_text("NOT_A_TOOL = object()\n")

    try:
        with pytest.raises(PluginInstallError, match="not discovered"):
            install_plugin(str(broken), replace=True)

        assert list_installed() == ["greeter"]  # and nothing parked alongside it
        discovered, _ = load_plugins()
        assert discovered["tool:greeter"]().name() == "greet"
    finally:
        remove_plugin("greeter")


def test_a_failed_install_leaves_no_directory_behind(monkeypatch, tmp_path):
    """An install that copies files but never becomes discoverable is worse
    than no install at all — it looks like it worked."""
    _isolate(monkeypatch, tmp_path)
    source = _fixture_plugin(tmp_path, entry_points=False)
    # This source passes the pyproject checks structurally... except it must
    # declare an entry point to get this far in the first place, so instead
    # force pip itself to "succeed" while discovery still fails, by pointing
    # the entry point at something that isn't a Tool subclass.
    (source / "pyproject.toml").write_text(textwrap.dedent("""\
        [build-system]
        requires = ["setuptools>=61"]
        build-backend = "setuptools.build_meta"
        [project]
        name = "cobirb_plugins_greeter"
        version = "0.0.1"
        [project.entry-points."cobirb.plugins"]
        tool = "greeter_plugin:NOT_A_TOOL"
        [tool.setuptools]
        py-modules = ["greeter_plugin"]
    """))
    (source / "greeter_plugin.py").write_text("NOT_A_TOOL = object()\n")

    with pytest.raises(PluginInstallError, match="not discovered"):
        install_plugin(str(source))

    assert list_installed() == []
    import importlib.metadata as im

    with pytest.raises(im.PackageNotFoundError):
        im.distribution("cobirb_plugins_greeter")


def test_removing_a_plugin_that_was_never_installed_says_so_rather_than_raising(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    result = remove_plugin("never-installed")

    assert not result.removed_directory
    assert "not an installed local plugin" in result.describe()


def test_list_installed_is_empty_with_no_plugins_directory_at_all(monkeypatch, tmp_path):
    """The directory may not exist yet on a fresh install — must not raise."""
    _isolate(monkeypatch, tmp_path)

    assert list_installed() == []
