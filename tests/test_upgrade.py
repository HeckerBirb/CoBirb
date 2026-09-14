"""Tests for ``cobirb --upgrade`` (``runtime/upgrade.py``).

Most of this mocks ``_run_pip`` and ``_running_version`` — the tag/version
comparison logic and the dirty-tree/downgrade refusals are the part worth
testing cheaply and often, and they don't need a real package install to
prove. One test runs the git side for real (fetch, checkout, tag resolution
against an actual remote) — the part a mock can't prove — while still mocking
``pip install -e`` to keep it fast and fully offline, the same split
``test_plugin_install.py`` uses for its own subprocess boundary.
"""
from __future__ import annotations

import subprocess

import pytest

from cobirb.runtime import upgrade as upgrade_module
from cobirb.runtime.upgrade import UpgradeError, UpgradeResult, upgrade


# --------------------------------------------------------------------------- #
# Pure logic: version parsing and repo-root discovery
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag,expected", [("v1.2.3", (1, 2, 3)), ("0.8.0", (0, 8, 0))])
def test_parse_version_accepts_with_or_without_v(tag, expected):
    assert upgrade_module._parse_version(tag) == expected


def test_parse_version_rejects_garbage():
    with pytest.raises(UpgradeError, match="doesn't look like a release tag"):
        upgrade_module._parse_version("latest")


def test_find_repo_root_walks_up_to_the_checkout(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = \"fake\"\n")
    nested = repo / "src" / "cobirb" / "runtime"
    nested.mkdir(parents=True)

    assert upgrade_module._find_repo_root(start=str(nested)) == str(repo)


def test_find_repo_root_refuses_a_non_git_install(tmp_path):
    nested = tmp_path / "site-packages" / "cobirb" / "runtime"
    nested.mkdir(parents=True)

    with pytest.raises(UpgradeError, match="could not find a git checkout"):
        upgrade_module._find_repo_root(start=str(nested))


# --------------------------------------------------------------------------- #
# upgrade()'s own contract, with git/pip mocked out
# --------------------------------------------------------------------------- #
def _stub_git(monkeypatch, *, tags: list[str] = ()):
    """Replace every git-touching call `upgrade()` makes with fakes that
    report a clean tree and the given tags already fetched."""
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: "/fake/repo")
    monkeypatch.setattr(
        upgrade_module,
        "_run_git",
        lambda *args, cwd: "" if args[0] == "status" else "\n".join(tags),
    )
    monkeypatch.setattr(upgrade_module, "_tag_exists", lambda tag, *, cwd: tag in tags)
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")


def test_upgrade_with_no_tag_picks_the_highest_by_version_not_by_listing_order(monkeypatch):
    _stub_git(monkeypatch, tags=["v0.9.0", "v0.10.0", "v0.8.0"])
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.8.0")

    result = upgrade()

    assert result == UpgradeResult(from_version="0.8.0", to_version="0.10.0", tag="v0.10.0")


def test_upgrade_to_the_version_already_running_is_a_no_op(monkeypatch):
    _stub_git(monkeypatch, tags=["v0.8.0"])
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.8.0")

    result = upgrade("v0.8.0")

    assert result.already_current
    assert result.tag == "v0.8.0"


def test_upgrade_refuses_a_downgrade_without_force(monkeypatch):
    _stub_git(monkeypatch, tags=["v0.7.0"])
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.8.0")

    with pytest.raises(UpgradeError, match="downgrade"):
        upgrade("v0.7.0")


def test_upgrade_allows_a_downgrade_with_force(monkeypatch):
    _stub_git(monkeypatch, tags=["v0.7.0"])
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.8.0")

    result = upgrade("v0.7.0", force=True)

    assert result.tag == "v0.7.0"
    assert not result.already_current


def test_upgrade_refuses_a_dirty_working_tree(monkeypatch):
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: "/fake/repo")
    monkeypatch.setattr(
        upgrade_module,
        "_run_git",
        lambda *args, cwd: " M src/cobirb/cli.py\n" if args[0] == "status" else "",
    )

    with pytest.raises(UpgradeError, match="uncommitted changes"):
        upgrade()


def test_upgrade_reports_no_matching_tag_by_name(monkeypatch):
    _stub_git(monkeypatch, tags=["v0.8.0"])
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.8.0")

    with pytest.raises(UpgradeError, match="no tag named"):
        upgrade("v9.9.9")


def test_upgrade_reports_no_release_tags_at_all(monkeypatch):
    _stub_git(monkeypatch, tags=[])
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.8.0")

    with pytest.raises(UpgradeError, match="no release tags found"):
        upgrade()


# --------------------------------------------------------------------------- #
# The real thing: a genuine git fetch + tag resolution + checkout against an
# actual local remote. pip stays mocked — offline and fast, like the module's
# own docstring says a metadata refresh should be, not something this test
# needs to prove on top of the git side.
# --------------------------------------------------------------------------- #
def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _real_repo(tmp_path):
    """A bare 'origin' plus a clone of it, with two tagged commits — the
    shape a real CoBirb checkout has (a git clone tracking a real remote)."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "-q", str(origin), cwd=tmp_path)
    _git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)

    (work / "pyproject.toml").write_text('[project]\nname = "fake"\n')
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "v0.7.0", cwd=work)
    _git("tag", "v0.7.0", cwd=work)

    (work / "pyproject.toml").write_text('[project]\nname = "fake"\nversion = "0.8.0"\n')
    _git("commit", "-q", "-am", "v0.8.0", cwd=work)
    _git("tag", "v0.8.0", cwd=work)

    _git("push", "-q", "--all", "origin", cwd=work)
    _git("push", "-q", "--tags", "origin", cwd=work)
    # Land back on the older release, as if this checkout had never upgraded.
    _git("checkout", "-q", "v0.7.0", cwd=work)
    return work


def test_upgrade_against_a_real_git_remote_moves_the_checkout(monkeypatch, tmp_path):
    work = _real_repo(tmp_path)
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    result = upgrade()

    assert result == UpgradeResult(from_version="0.7.0", to_version="0.8.0", tag="v0.8.0")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.strip()
    tagged = subprocess.run(
        ["git", "rev-parse", "v0.8.0"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == tagged


def test_upgrade_against_a_real_remote_resolves_a_tag_without_its_v_prefix(monkeypatch, tmp_path):
    work = _real_repo(tmp_path)
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    result = upgrade("0.8.0")

    assert result.tag == "v0.8.0"


def test_upgrade_against_a_real_remote_refuses_a_dirty_tree(monkeypatch, tmp_path):
    work = _real_repo(tmp_path)
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    (work / "pyproject.toml").write_text("not committed")

    with pytest.raises(UpgradeError, match="uncommitted changes"):
        upgrade()
