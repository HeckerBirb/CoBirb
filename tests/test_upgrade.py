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
    # Detached by default here; the branch-keeping path has its own tests
    # against a real repository below, where git can actually answer.
    monkeypatch.setattr(upgrade_module, "_current_branch", lambda *, cwd: "")
    monkeypatch.setattr(upgrade_module, "_can_fast_forward_to", lambda tag, *, cwd: False)


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
# Named explicitly when each test repository is created, never inherited from
# whoever is running the suite: `git init`'s default branch is `master` unless
# a machine's own config says otherwise, so a test that assumes `main` passes
# for the author and fails in CI.
_BRANCH = "main"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_out(*args, cwd) -> str:
    """Same, but for the answer rather than the side effect."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _real_repo(tmp_path):
    """A bare 'origin' plus a clone of it, with two tagged commits — the
    shape a real CoBirb checkout has (a git clone tracking a real remote)."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "-q", "-b", _BRANCH, str(origin), cwd=tmp_path)
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


# --------------------------------------------------------------------------- #
# Staying on the branch.
#
# Checking a tag out directly detaches HEAD, which silently swallows the next
# commit anyone makes in that checkout — it belongs to no branch, so `git push`
# has nothing to send. These run against a real repository because the whole
# question is what git actually does to HEAD.
# --------------------------------------------------------------------------- #
def _branch_of(work) -> str:
    """The branch checked out in ``work``, or "" when HEAD is detached."""
    done = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=work, capture_output=True, text=True,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def _on_branch_behind_the_tag(tmp_path):
    """A checkout sitting on its branch, one tagged release behind — what a
    person upgrading normally has."""
    work = _real_repo(tmp_path)
    _git("checkout", "-q", _BRANCH, cwd=work)
    _git("reset", "-q", "--hard", "v0.7.0", cwd=work)
    return work


def test_upgrading_from_a_branch_stays_on_that_branch(monkeypatch, tmp_path):
    work = _on_branch_behind_the_tag(tmp_path)
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    result = upgrade()

    assert _branch_of(work) == _BRANCH
    assert result.branch == _BRANCH
    assert f"Still on {_BRANCH}" in result.describe()


def test_the_branch_actually_moved_to_the_tagged_commit(monkeypatch, tmp_path):
    """Staying on the branch is only useful if the branch arrived."""
    work = _on_branch_behind_the_tag(tmp_path)
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    upgrade()

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True, check=True).stdout.strip()
    tagged = subprocess.run(["git", "rev-parse", "v0.8.0"], cwd=work,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert head == tagged


def test_an_already_detached_checkout_is_left_detached_and_says_so(monkeypatch, tmp_path):
    """Nothing to guess at: there is no branch to keep, so the result names
    the state rather than leaving someone to discover it."""
    work = _real_repo(tmp_path)  # this fixture lands on a tag, detached
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    result = upgrade()

    assert _branch_of(work) == ""
    assert result.branch == ""
    assert "not on a branch" in result.describe()


def test_a_branch_with_its_own_commits_detaches_rather_than_moving_them(monkeypatch, tmp_path):
    """A fast-forward would be a lie here — the branch has work the tag does
    not. Detach, and say so, rather than rewriting where the branch points."""
    work = _on_branch_behind_the_tag(tmp_path)
    (work / "mine.txt").write_text("local work")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "local work", cwd=work)
    mine = subprocess.run(["git", "rev-parse", _BRANCH], cwd=work,
                          capture_output=True, text=True, check=True).stdout.strip()

    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    result = upgrade()

    assert result.branch == ""
    assert "not on a branch" in result.describe()
    still = subprocess.run(["git", "rev-parse", _BRANCH], cwd=work,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert still == mine  # the local commit is still on main, untouched


def test_upgrading_works_from_a_clone_that_has_never_fetched(monkeypatch, tmp_path):
    """The fast-forward needs the tagged commit to exist locally, and a user
    who has not touched git since cloning does not have it. `upgrade()` fetches
    before it resolves anything, and fetching a tag brings the objects it
    points to — so the release is reachable without the user syncing first.

    Worth pinning: narrowing or reordering that fetch would break this
    silently, leaving the branch behind and the checkout detached.
    """
    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    _git("init", "--bare", "-q", "-b", _BRANCH, str(origin), cwd=tmp_path)
    _git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=other)
    _git("config", "user.name", "Test", cwd=other)
    (other / "pyproject.toml").write_text('[project]\nname = "fake"\n')
    _git("add", ".", cwd=other)
    _git("commit", "-q", "-m", "v0.7.0", cwd=other)
    _git("tag", "v0.7.0", cwd=other)
    _git("push", "-q", "--all", "origin", cwd=other)
    _git("push", "-q", "--tags", "origin", cwd=other)

    # The user clones here, at v0.7.0, and never runs git again.
    _git("clone", "-q", str(origin), str(work), cwd=tmp_path)

    # A newer release is cut and pushed by someone else.
    (other / "pyproject.toml").write_text('[project]\nname = "fake"\nversion = "0.8.0"\n')
    _git("commit", "-q", "-am", "v0.8.0", cwd=other)
    _git("tag", "v0.8.0", cwd=other)
    _git("push", "-q", "origin", _BRANCH, cwd=other)
    _git("push", "-q", "--tags", "origin", cwd=other)

    assert "v0.8.0" not in _git_out("tag", cwd=work)  # the user has never seen it

    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: str(work))
    monkeypatch.setattr(upgrade_module, "_running_version", lambda: "0.7.0")
    monkeypatch.setattr(upgrade_module, "_run_pip", lambda *args: "")

    result = upgrade()

    assert result.branch == _BRANCH
    assert _branch_of(work) == _BRANCH
    assert _git_out("rev-parse", "HEAD", cwd=work) == _git_out("rev-parse", "v0.8.0", cwd=work)
