"""``cobirb --upgrade [tag] [--force]`` — move this install to another release.

**There are three shapes an install can have, and this routes between them.**

- ``managed`` — put there by ``install.sh``: a venv under
  ``~/.local/share/cobirb`` with a ``cobirb`` symlink in ``~/.local/bin``.
  Upgrading re-runs that same script with a different ``--version``.
- ``checkout`` — a git clone that was ``pip install -e``'d (directly, or via
  ``pipx install --editable .``). The checkout *is* the installation, so
  upgrading means moving it onto another tag and re-running the install step.
- ``unmanaged`` — anything else: someone's own venv, a distro package, a
  plain ``pip install .`` into site-packages. Refused, with the command that
  would actually work named in the refusal.

**Why the managed path shells out instead of reimplementing the work.**
``install.sh`` already resolves a release, downloads a wheel, verifies its
checksum, installs it into the venv and rewrites the marker — and it has to,
because it is also what a first-time user runs. Doing any of that a second
time in Python would mean two implementations of "move to version X" that
agree only as long as someone keeps them in step. So ``--upgrade`` on a
managed install is a thin call into the script that installed it, and the
downgrade guard lives there rather than here.

For the checkout path, the only thing that ever needs to change to move to a
different release is which commit that checkout has on disk: fetch tags, pick
one, move the checkout onto it, and re-run the same install step so any
dependency or entry-point change in the new tag actually takes — the same
metadata-refresh ``pip install -e .`` caveat documented in the README's
update section.

**Moving onto a tag without leaving the branch.** Checking a tag out directly
detaches ``HEAD``, which is fine for someone only running CoBirb and a trap
for anyone who also commits to it: the next commit belongs to no branch and
``git push`` silently has nothing to send. Upgrading is not a request to leave
your branch, so the branch is fast-forwarded onto the tagged commit where it
can be, and detaching is the reported fallback — see ``_move_to``.

**Why tags, not branches or commits.** A release is a tag (see
``AGENTS.md`` for the convention: every version bump gets a matching
``vX.Y.Z``), so "the latest release" and "a specific release" both resolve to
one. Moving to an arbitrary commit or branch is a different, riskier request
this deliberately doesn't offer — nothing here needs to guess what "latest"
means beyond "the highest released version number".

**Why this refuses a downgrade by default.** Moving to an older release can
silently undo a session-schema or SPI change you are relying on, which is
exactly the kind of mistake ``--force`` exists to require a person say out
loud rather than fall into. Comparing tags as semver, not as git history
(:func:`_parse_version`), is what makes "older" a well-defined question
before anything is touched.

**Why a dirty working tree is refused outright rather than stashed.** A
``git stash`` a person didn't ask for is a surprise they'd have to go find;
refusing and naming the reason costs one command (``git stash`` or
``git commit``, then try again) and loses nothing.

**Not a new trust boundary.** ``--upgrade`` is the one command in CoBirb that
talks to a network by default, but only because a person typed it — the same
justification ``cobirb plugin install`` already has for running arbitrary
code. See AGENTS.md §2 for why that is still consistent with "no outbound
network by default": the default is what happens without being asked, and
this is never that. Which host it talks to depends on the install shape — a
git remote for a checkout, GitHub's release assets plus PyPI for the
dependencies on a managed one — but not whether it talks at all.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata as importlib_metadata

# A release tag, with or without the "v" — "v0.8.0" and "0.8.0" both parse.
_TAG_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# git and pip are both one-off, sat-and-watched actions here, not part of a
# turn loop with a person waiting on a model reply — generous compared to a
# tool call's own timeout, the same reasoning `plugin_install._PIP_TIMEOUT_SECONDS`
# uses.
_GIT_TIMEOUT_SECONDS = 60
_PIP_TIMEOUT_SECONDS = 300
# The installer's budget is larger than the bare pip one because it is doing
# strictly more: resolving a release, downloading and checksumming a wheel,
# building the venv on a first run, and only then installing CoBirb and its
# three dependencies.
_INSTALL_TIMEOUT_SECONDS = 600

_DEFAULT_REMOTE = "origin"

# The three install shapes — see the module docstring.
MANAGED = "managed"
CHECKOUT = "checkout"
UNMANAGED = "unmanaged"

# Where install.sh puts the venv it manages, and the marker it writes beside
# it. Both honour the same environment overrides the script does, so pointing
# one at a throwaway directory points the other there too.
_DEFAULT_INSTALL_DIR = "~/.local/share/cobirb"
_MARKER_NAME = "install.json"

# Spelled once. It appears in a refusal, in `doctor`, and in the docs, and the
# three disagreeing about how to install CoBirb would be its own small bug.
_INSTALL_COMMAND = (
    "curl -fsSL https://github.com/HeckerBirb/CoBirb/releases/latest/download/install.sh | bash"
)


class UpgradeError(Exception):
    """CoBirb could not upgrade itself, with a reason a person can act on."""


@dataclass
class UpgradeResult:
    from_version: str
    to_version: str
    tag: str
    already_current: bool = False
    # The branch left checked out, or "" for a detached HEAD. Reported rather
    # than assumed: a detached checkout silently swallows the next commit
    # someone makes, so it has to be said out loud when it happens.
    branch: str = ""
    kind: str = CHECKOUT

    def describe(self) -> str:
        # A managed upgrade has already narrated itself: install.sh streams
        # what it resolved, downloaded, verified and installed straight to the
        # terminal as it happens, which is what you want during a download.
        # Summarising it again here would say the same thing twice.
        if self.kind == MANAGED:
            return ""
        if self.already_current:
            return f"Already at {self.tag} — nothing to do."
        moved = f"Upgraded v{self.from_version} → v{self.to_version} ({self.tag})."
        if self.branch:
            return f"{moved} Still on {self.branch}."
        return (
            f"{moved} The checkout is not on a branch — it is detached at {self.tag}. "
            "Run 'git checkout <branch>' before committing anything, or a commit made "
            "here will belong to no branch."
        )


@dataclass
class Install:
    """Which of the three shapes this running CoBirb has, and where it lives."""

    kind: str
    # The git checkout for CHECKOUT, the install directory for MANAGED, "" for
    # UNMANAGED.
    root: str = ""
    venv: str = ""
    version: str = ""
    source: str = ""


def _parse_version(tag: str) -> tuple[int, int, int]:
    match = _TAG_PATTERN.match(tag.strip())
    if not match:
        raise UpgradeError(
            f"{tag!r} doesn't look like a release tag — expected e.g. 'v0.8.0' or '0.8.0'."
        )
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def _run_git(*args: str, cwd: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise UpgradeError("git is not on PATH — self-upgrade needs it to fetch and switch tags") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(f"git {args[0]} timed out after {_GIT_TIMEOUT_SECONDS}s") from exc
    output = f"{completed.stdout}{completed.stderr}"
    if completed.returncode != 0:
        raise UpgradeError(f"git {args[0]} failed:\n{output.strip()}")
    return output


def _run_pip(*args: str) -> str:
    """``sys.executable -m pip`` for the same reason ``plugin_install`` uses it:
    this must land in the interpreter CoBirb itself is running under, editable
    or pipx-managed, not whatever bare ``pip`` happens to resolve to on PATH.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", *args],
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(f"pip {args[0]} timed out after {_PIP_TIMEOUT_SECONDS}s") from exc
    output = f"{completed.stdout}{completed.stderr}"
    if completed.returncode != 0:
        raise UpgradeError(f"pip {args[0]} failed:\n{output.strip()}")
    return output


def _find_repo_root(start: str | None = None) -> str:
    """Where CoBirb's own source lives on disk — the checkout ``--upgrade`` moves.

    Walks up from *this file* by default, not from the current working
    directory: a person can run ``cobirb --upgrade`` from anywhere. For an
    editable install (the only kind this supports — see the module
    docstring) this file's real path is the git checkout itself, never a
    copy, which is what makes the walk reliable. A plain, non-editable ``pip
    install .`` copies this file into site-packages with no ``.git`` above it
    anywhere, and that case is refused rather than guessed at.

    ``start`` overrides the walk's origin — for tests only, which need to
    point this at a throwaway repository rather than CoBirb's own real
    checkout.
    """
    here = start or os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(here, ".git")) and os.path.isfile(
            os.path.join(here, "pyproject.toml")
        ):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    raise UpgradeError(
        "could not find a git checkout above CoBirb's own installed location — "
        "self-upgrade only works for the ordinary install (a git clone, then "
        "'pip install -e .' or 'pipx install --editable .'). Re-clone and reinstall "
        "that way, then try again."
    )


def _running_version() -> str:
    try:
        return importlib_metadata.version("cobirb")
    except importlib_metadata.PackageNotFoundError as exc:
        raise UpgradeError("CoBirb's own package metadata is missing — reinstall it first") from exc


def _install_dir() -> str:
    """The directory ``install.sh`` manages, resolved at call time.

    ``COBIRB_INSTALL_DIR`` is the same override the script itself reads, so
    moving one half of a managed install moves the other with it.
    """
    return os.path.expanduser(os.environ.get("COBIRB_INSTALL_DIR", _DEFAULT_INSTALL_DIR))


def _read_marker(path: str) -> "dict | None":
    """``install.json`` as a dict, or ``None`` if absent or unreadable.

    Unreadable counts as absent rather than as an error. A truncated or
    hand-edited marker should degrade to "this looks unmanaged" — which ends
    in a refusal naming the fix — instead of failing the upgrade with a JSON
    parse error nobody can act on.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def detect_install() -> Install:
    """Which of the three shapes the CoBirb executing this code has.

    Managed is checked first, and confirmed against ``sys.prefix`` rather than
    taken on the marker's word: the marker only says a managed install exists
    somewhere, not that it is the one currently running. Having both a managed
    install and a clone to hack on is a perfectly ordinary thing to do, and
    the question here is about this interpreter, not about what is on disk.
    """
    marker = _read_marker(os.path.join(_install_dir(), _MARKER_NAME))
    if marker:
        venv = str(marker.get("venv", ""))
        if venv and os.path.realpath(venv) == os.path.realpath(sys.prefix):
            return Install(
                kind=MANAGED,
                root=_install_dir(),
                venv=venv,
                version=str(marker.get("version", "")),
                source=str(marker.get("source", "")),
            )
    try:
        return Install(kind=CHECKOUT, root=_find_repo_root())
    except UpgradeError:
        return Install(kind=UNMANAGED)


def _bundled_install_script() -> str:
    """The ``install.sh`` that shipped inside *this* version's package.

    Deliberately the bundled copy rather than a freshly downloaded one, so a
    release carries the procedure for managing itself. A version that needs to
    change how upgrading works can then do it, and have the change take effect
    on the way out of that version rather than only for people who never
    installed it.
    """
    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh"
    )
    if not os.path.isfile(script):
        raise UpgradeError(
            "this install is missing its own install.sh, which the managed upgrade path "
            "runs. Reinstalling repairs it:\n"
            f"  {_INSTALL_COMMAND}"
        )
    return script


def _run_install_script(args: "list[str]") -> None:
    """Run the bundled installer from a copy, with its output left streaming.

    **From a copy** because pip is about to rewrite the original underneath
    it. A shell reads a script as it executes rather than all at once, so
    replacing the file mid-run can hand it the tail of a different file.

    **Streaming** — no ``capture_output`` — because this downloads and
    installs, and progress that only appears once the work has finished is not
    progress.
    """
    script = _bundled_install_script()
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "install.sh")
        shutil.copyfile(script, copy)
        try:
            completed = subprocess.run(["sh", copy, *args], timeout=_INSTALL_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise UpgradeError("no 'sh' on PATH — the installer needs a POSIX shell") from exc
        except subprocess.TimeoutExpired as exc:
            raise UpgradeError(
                f"the installer timed out after {_INSTALL_TIMEOUT_SECONDS}s"
            ) from exc
    if completed.returncode != 0:
        raise UpgradeError("the installer did not finish — its output above says why.")


def _managed_upgrade(install: Install, tag: "str | None", *, force: bool) -> UpgradeResult:
    """Hand the whole job to ``install.sh``, then report what it ended up doing.

    Every decision — resolving "latest", refusing a downgrade without
    ``--force``, verifying the download — belongs to the script, which is also
    what a first-time user runs. Re-deciding any of it here would be a second
    implementation to keep in step with the first.
    """
    args: "list[str]" = []
    if tag:
        args += ["--version", tag]
    if force:
        args.append("--force")

    _run_install_script(args)

    # Read the outcome back off the marker rather than assuming the requested
    # version is the installed one: the script exits successfully without
    # changing anything when it was already there.
    marker = _read_marker(os.path.join(install.root, _MARKER_NAME)) or {}
    landed = str(marker.get("version", ""))
    return UpgradeResult(
        from_version=install.version,
        to_version=landed,
        tag=str(marker.get("tag", "")),
        already_current=bool(landed) and landed == install.version,
        kind=MANAGED,
    )


def _tag_exists(tag: str, *, cwd: str) -> bool:
    """Whether ``tag`` is an actual ref, tolerating "no" as a normal answer
    rather than an error — separate from ``_run_git``, which treats a
    non-zero exit as a failure to report rather than a question to ask."""
    check = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return check.returncode == 0


def _current_branch(*, cwd: str) -> str:
    """The branch checked out, or ``""`` for a detached ``HEAD``.

    Like ``_tag_exists``, this treats a non-zero exit as an answer rather than
    a failure: "not on a branch" is a normal state to be in, not an error to
    report.
    """
    check = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return check.stdout.strip() if check.returncode == 0 else ""


def _can_fast_forward_to(tag: str, *, cwd: str) -> bool:
    """Whether ``HEAD`` can reach ``tag`` by moving forward only — i.e. the
    commit checked out is an ancestor of the tagged one."""
    check = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", tag],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return check.returncode == 0


def _move_to(tag: str, *, cwd: str) -> str:
    """Put the checkout at ``tag``, staying on the current branch when that is
    possible. Returns the branch still checked out, or ``""`` if detached.

    **Checking a tag out directly detaches ``HEAD``**, and a detached checkout
    is a trap for anyone who also works on CoBirb itself: the next commit they
    make belongs to no branch, so it is invisible to ``git push`` and easy to
    lose. Upgrading is not a request to leave the branch you are on.

    So when the branch can simply move forward onto the tagged commit — the
    ordinary case of upgrading while sitting on an up-to-date branch — it is
    fast-forwarded and stays checked out. Detaching is the fallback for the
    cases where there is nothing else honest to do: already detached, a
    downgrade, or a branch carrying commits the tag does not have. The result
    says which happened, because a detached ``HEAD`` the user was not told
    about is the whole problem.
    """
    branch = _current_branch(cwd=cwd)
    if branch and _can_fast_forward_to(tag, cwd=cwd):
        _run_git("merge", "--ff-only", tag, cwd=cwd)
        return branch
    _run_git("checkout", tag, cwd=cwd)
    return ""


def _resolve_tag(tag: str, *, cwd: str) -> str:
    """The exact, existing tag ref a person's ``--upgrade <tag>`` argument means.

    Tried as given first, then with a leading ``v`` added — so ``--upgrade
    0.8.0`` finds a tag actually named ``v0.8.0`` without the release
    convention having to be memorised.
    """
    candidates = [tag] if tag.startswith("v") else [tag, f"v{tag}"]
    for candidate in candidates:
        if _tag_exists(candidate, cwd=cwd):
            return candidate
    raise UpgradeError(
        f"no tag named {tag!r} (tried {', '.join(candidates)}) after fetching — "
        "run 'git tag' in the checkout to see what's actually there."
    )


def _latest_tag(*, cwd: str) -> str:
    """The highest release tag by version number, not by git history order —
    a tag can be created out of order, and "latest" should survive that."""
    output = _run_git("tag", "-l", "v*.*.*", cwd=cwd)
    candidates = [line.strip() for line in output.splitlines() if _TAG_PATTERN.match(line.strip())]
    if not candidates:
        raise UpgradeError(
            "no release tags found after fetching (expected 'vX.Y.Z') — "
            "name one explicitly with 'cobirb --upgrade <tag>' instead."
        )
    return max(candidates, key=_parse_version)


def upgrade(tag: str | None = None, *, force: bool = False, remote: str = _DEFAULT_REMOTE) -> UpgradeResult:
    """Move this install to ``tag``, or to the latest release if none is given.

    Routes on the install's shape — see the module docstring for the three and
    why the managed one delegates rather than reimplements.
    """
    install = detect_install()

    if install.kind == MANAGED:
        return _managed_upgrade(install, tag, force=force)
    if install.kind == CHECKOUT:
        return _checkout_upgrade(install.root, tag, force=force, remote=remote)

    raise UpgradeError(
        "this CoBirb cannot upgrade itself: it is not the installer's managed install, "
        "and there is no git checkout above it. If you installed it into a virtualenv "
        "of your own, upgrade it the same way you installed it. To move to an install "
        "that does upgrade itself:\n"
        f"  {_INSTALL_COMMAND}"
    )


def _checkout_upgrade(
    repo_root: str, tag: str | None, *, force: bool, remote: str
) -> UpgradeResult:
    """Move a git checkout onto ``tag``, or the latest release tag.

    Refuses on a dirty working tree (see the module docstring for why) and on
    a downgrade unless ``force`` is set. Stays on the branch you are on
    wherever that is possible, and says so when it cannot — see ``_move_to``.
    """
    status = _run_git("status", "--porcelain", cwd=repo_root)
    if status.strip():
        raise UpgradeError(
            "this checkout has uncommitted changes — commit or stash them first. "
            "An upgrade switches tags outright rather than guessing what to do with "
            "work that was never saved."
        )

    _run_git("fetch", "--tags", remote, cwd=repo_root)

    target_tag = _resolve_tag(tag, cwd=repo_root) if tag else _latest_tag(cwd=repo_root)
    target_parts = _parse_version(target_tag)
    target_version = ".".join(str(part) for part in target_parts)

    running_version = _running_version()
    running_parts = _parse_version(running_version)

    if target_parts == running_parts:
        return UpgradeResult(
            from_version=running_version, to_version=target_version, tag=target_tag, already_current=True
        )

    if target_parts < running_parts and not force:
        raise UpgradeError(
            f"{target_tag} (v{target_version}) is older than the running v{running_version} — "
            "that's a downgrade. Pass --force if that's actually what you want."
        )

    branch = _move_to(target_tag, cwd=repo_root)
    _run_pip("install", "-e", repo_root)

    return UpgradeResult(
        from_version=running_version, to_version=target_version, tag=target_tag, branch=branch
    )
