"""``cobirb --upgrade [tag] [--force]`` — move a git checkout to another release.

CoBirb is distributed as a git clone that gets ``pip install -e``'d (directly,
or via ``pipx install --editable .`` for a global binary — see the README's
install section). That means the checkout *is* the installation, and the only
thing that ever needs to change to move to a different release is which
commit that checkout has on disk. This module does exactly that and nothing
more: fetch tags, pick one, ``git checkout`` it, and re-run the same install
step so any dependency or entry-point change in the new tag actually takes —
the same metadata-refresh ``pip install -e .`` caveat documented in the
README's update section.

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
this is never that.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
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

_DEFAULT_REMOTE = "origin"


class UpgradeError(Exception):
    """CoBirb could not upgrade itself, with a reason a person can act on."""


@dataclass
class UpgradeResult:
    from_version: str
    to_version: str
    tag: str
    already_current: bool = False

    def describe(self) -> str:
        if self.already_current:
            return f"Already at {self.tag} — nothing to do."
        return f"Upgraded v{self.from_version} → v{self.to_version} ({self.tag})."


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
    """Move this checkout to ``tag``, or the latest release tag if none is given.

    Refuses on a dirty working tree (see the module docstring for why) and on
    a downgrade unless ``force`` is set. Leaves the checkout in a detached
    ``HEAD`` at the target tag — the ordinary, well-understood result of
    checking out a tag in any git project, not something CoBirb invents.
    """
    repo_root = _find_repo_root()

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

    _run_git("checkout", target_tag, cwd=repo_root)
    _run_pip("install", "-e", repo_root)

    return UpgradeResult(from_version=running_version, to_version=target_version, tag=target_tag)
