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

import json
import os
import subprocess
import sys

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


# --------------------------------------------------------------------------- #
# Which of the three install shapes is running (`detect_install`)
# --------------------------------------------------------------------------- #
def _managed_marker(tmp_path, monkeypatch, *, venv=None, version="0.13.1", raw=None):
    """Point `COBIRB_INSTALL_DIR` at a throwaway directory holding a marker.

    `venv` defaults to `sys.prefix` — i.e. "the interpreter running this test
    *is* the managed one", which is the state a real managed install is in.
    """
    monkeypatch.setenv("COBIRB_INSTALL_DIR", str(tmp_path))
    marker = tmp_path / "install.json"
    if raw is not None:
        marker.write_text(raw)
        return marker
    marker.write_text(json.dumps({
        "kind": "managed",
        "venv": venv if venv is not None else sys.prefix,
        "version": version,
        "tag": f"v{version}",
        "source": "https://example.invalid/releases/tag/v" + version,
    }))
    return marker


def test_detect_install_reports_managed_when_this_interpreter_is_the_managed_one(
    tmp_path, monkeypatch
):
    _managed_marker(tmp_path, monkeypatch)

    install = upgrade_module.detect_install()

    assert install.kind == upgrade_module.MANAGED
    assert install.version == "0.13.1"


def test_detect_install_ignores_a_marker_for_some_other_venv(tmp_path, monkeypatch):
    """Somebody can have a managed install *and* a clone they hack on. The
    question is which one is executing, so a marker pointing somewhere other
    than this interpreter is not an answer about this interpreter."""
    _managed_marker(tmp_path, monkeypatch, venv=str(tmp_path / "some" / "other" / "venv"))
    monkeypatch.setattr(upgrade_module, "_find_repo_root", lambda: "/fake/repo")

    assert upgrade_module.detect_install().kind == upgrade_module.CHECKOUT


def test_detect_install_treats_an_unreadable_marker_as_not_managed(tmp_path, monkeypatch):
    """A hand-edited or truncated marker should land on a refusal that names
    the fix, not a JSON parse error raised from inside an upgrade."""
    _managed_marker(tmp_path, monkeypatch, raw="{ this is not json")
    monkeypatch.setattr(
        upgrade_module, "_find_repo_root", lambda: (_ for _ in ()).throw(UpgradeError("nope"))
    )

    assert upgrade_module.detect_install().kind == upgrade_module.UNMANAGED


def test_detect_install_reports_unmanaged_with_no_marker_and_no_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_INSTALL_DIR", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(
        upgrade_module, "_find_repo_root", lambda: (_ for _ in ()).throw(UpgradeError("nope"))
    )

    assert upgrade_module.detect_install().kind == upgrade_module.UNMANAGED


# --------------------------------------------------------------------------- #
# The managed path hands the work to install.sh rather than redoing it
# --------------------------------------------------------------------------- #
def test_managed_upgrade_forwards_the_release_and_force_to_the_installer(tmp_path, monkeypatch):
    _managed_marker(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(upgrade_module, "_run_install_script", lambda args: calls.append(args))

    upgrade("v0.0.3", force=True)

    assert calls == [["--version", "v0.0.3", "--force"]]


def test_managed_upgrade_with_no_release_lets_the_installer_pick_latest(tmp_path, monkeypatch):
    _managed_marker(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(upgrade_module, "_run_install_script", lambda args: calls.append(args))

    upgrade()

    assert calls == [[]]


def test_managed_upgrade_reads_the_outcome_back_off_the_marker(tmp_path, monkeypatch):
    """What landed is whatever the installer wrote, not what was asked for —
    it exits successfully having done nothing when the version is already
    there, and the result has to be able to say so."""
    marker = _managed_marker(tmp_path, monkeypatch, version="0.13.1")

    def _installer(args):
        marker.write_text(json.dumps({"venv": sys.prefix, "version": "0.14.0", "tag": "v0.14.0"}))

    monkeypatch.setattr(upgrade_module, "_run_install_script", _installer)

    result = upgrade()

    assert (result.from_version, result.to_version) == ("0.13.1", "0.14.0")
    assert not result.already_current


def test_a_managed_result_says_nothing_because_the_installer_already_did(tmp_path, monkeypatch):
    """install.sh streams its own progress and summary straight to the
    terminal. A second summary here would just say it all again."""
    _managed_marker(tmp_path, monkeypatch)
    monkeypatch.setattr(upgrade_module, "_run_install_script", lambda args: None)

    assert upgrade().describe() == ""


def test_a_failing_installer_is_an_upgrade_error(tmp_path, monkeypatch):
    _managed_marker(tmp_path, monkeypatch)

    def _boom(args):
        raise UpgradeError("the installer did not finish — its output above says why.")

    monkeypatch.setattr(upgrade_module, "_run_install_script", _boom)

    with pytest.raises(UpgradeError, match="did not finish"):
        upgrade()


def test_an_unmanaged_install_is_refused_and_told_what_would_work(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_INSTALL_DIR", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(
        upgrade_module, "_find_repo_root", lambda: (_ for _ in ()).throw(UpgradeError("nope"))
    )

    with pytest.raises(UpgradeError, match="install.sh"):
        upgrade()


# --------------------------------------------------------------------------- #
# The real install.sh — the decisions a mock of it could not prove
# --------------------------------------------------------------------------- #
def _run_installer(*args, install_dir, extra_env=None):
    """The bundled install.sh, for real, against a throwaway install dir.

    None of these reach the network: every path exercised here is a refusal or
    an early exit that happens before the first download.
    """
    env = {**os.environ, "COBIRB_INSTALL_DIR": str(install_dir)}
    env.update(extra_env or {})
    return subprocess.run(
        ["sh", upgrade_module._bundled_install_script(), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_the_installer_ships_inside_the_package():
    """`--upgrade` on a managed install runs the copy that came with the
    running version, so the wheel has to actually contain one."""
    assert os.path.isfile(upgrade_module._bundled_install_script())


def test_the_installer_is_valid_posix_shell():
    """It is run with `sh`, not bash, and is piped to a shell by people who
    have not read it. A syntax error is not something to find in production."""
    checked = subprocess.run(
        ["sh", "-n", upgrade_module._bundled_install_script()],
        capture_output=True, text=True, timeout=30,
    )
    assert checked.returncode == 0, checked.stderr


def test_the_installer_refuses_a_version_that_is_not_a_release(tmp_path):
    result = _run_installer("--version", "latest", install_dir=tmp_path)

    assert result.returncode != 0
    assert "not a release version" in result.stderr


def test_the_installer_refuses_a_downgrade_without_force(tmp_path):
    (tmp_path / "install.json").write_text(json.dumps({"venv": "/x", "version": "0.13.1"}))

    result = _run_installer("--version", "v0.0.3", install_dir=tmp_path)

    assert result.returncode != 0
    assert "downgrade" in result.stderr


def test_the_installer_does_nothing_when_the_version_is_already_there(tmp_path):
    (tmp_path / "install.json").write_text(json.dumps({"venv": "/x", "version": "0.13.1"}))

    result = _run_installer("--version", "v0.13.1", install_dir=tmp_path)

    assert result.returncode == 0
    assert "nothing to do" in result.stdout


def test_the_installer_leaves_a_cobirb_home_alone_when_uninstalling(tmp_path):
    """Config, sessions and memory outlive any install. The one thing an
    uninstaller must not do is take them with it."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "install.json").write_text(json.dumps({"venv": "/x", "version": "0.13.1"}))
    home = tmp_path / ".cobirb"
    home.mkdir()
    (home / "config.json").write_text("{}")

    result = _run_installer("--uninstall", install_dir=install_dir)

    assert result.returncode == 0
    assert not install_dir.exists()
    assert (home / "config.json").exists()


# --------------------------------------------------------------------------- #
# The end of an install: doctor, and the system dependencies worth having
# --------------------------------------------------------------------------- #
def _post_install_report(tmp_path, *, have):
    """Run install.sh's closing report against a fake `cobirb` and a PATH that
    holds only the commands named in ``have`` (plus what the report needs)."""
    import shutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("uname", "cat"):
        os.symlink(shutil.which(tool), bin_dir / tool)
    for tool in have:
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "apt-get").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "apt-get").chmod(0o755)
    fake = tmp_path / "cobirb"
    fake.write_text("#!/bin/sh\necho '  ✗ model endpoint — not reachable'\nexit 1\n")
    fake.chmod(0o755)
    script = upgrade_module._bundled_install_script()
    return subprocess.run(
        ["/bin/sh", "-c", f'. "{script}"; post_install_report "{fake}"; echo "exit=$?"'],
        capture_output=True, text=True, timeout=30,
        env={"PATH": str(bin_dir), "COBIRB_INSTALL_SOURCED": "1", "HOME": str(tmp_path)},
    )


def test_an_install_ends_with_doctor_and_what_its_marks_mean(tmp_path):
    result = _post_install_report(tmp_path, have=["bwrap", "git"])

    assert "✗ model endpoint" in result.stdout  # doctor's own output, shown
    assert "✓ is ready" in result.stdout
    assert "exit=0" in result.stdout  # a failing check never fails the install
    assert "system-level dependencies" not in result.stdout  # nothing missing, nothing said


def test_missing_system_dependencies_are_named_with_reasons_and_a_command(tmp_path):
    result = _post_install_report(tmp_path, have=[])

    out = result.stdout
    assert "should strongly consider installing the missing system-level dependencies because" in out
    assert "  1. " in out and "  2. " in out
    if os.uname().sysname == "Linux":
        assert "sudo apt install bubblewrap git" in out
    else:
        assert "sudo apt install git" in out
