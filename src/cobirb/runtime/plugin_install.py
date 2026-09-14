"""``cobirb plugin install/list/remove`` — a registry that is a plain file.

Discovery (``plugins/loader.py``) already finds two kinds of plugin: an
installed Python package advertising a ``cobirb.plugins`` entry point, and a
local directory under ``~/.cobirb/plugins/<name>/`` that has *also* been
``pip install -e``'d under a matching distribution name. The first kind needs
nothing from this module — ``pip install some-published-plugin`` is already
the whole story, and CoBirb should not wrap a package manager it doesn't need
to. This module exists for the second kind: turning "I have a plugin's source
on disk" into "CoBirb finds it," which today means a person doing that by hand
correctly, a step ``_load_local_plugin`` is unforgiving about.

**Deliberately local-only.** ``install`` takes a path already on disk, never a
URL, a package name to resolve, or a version to fetch — there is no index to
query and nothing this module ever reaches out for. That is not a missing
feature; a registry service, even a curated list of known-good plugins, is a
new trust and networking question for a project that has repeatedly settled
on "local models only, forever," and it is not this module's decision to make.
Getting a plugin's source onto disk (cloning it, downloading a release) is the
user's own action, exactly as choosing which model to run is.

**Why this still shells out to ``pip``.** The loader resolves a local plugin's
entry point through ``importlib.metadata.distribution()`` — real package
metadata, not a hand-parsed ``pyproject.toml`` — so a plugin genuinely has to
be installed as a Python distribution to be found, editable or not. Wrapping
that in one command is automating a sequence a plugin's own README would
otherwise have to spell out by hand (copy here, ``cd`` there, ``pip install
-e .``, hope the name matches); it is not a new capability CoBirb has invented,
and it runs only on an explicit ``cobirb plugin install <path>`` a person
typed, never as a side effect of anything else.

No checksum or signature verification: the source a person names is already on
their own disk, under their own control, before this module ever sees it — no
new trust boundary is crossed by copying it into ``~/.cobirb/plugins/``. That
verification earns its cost only once a plugin can be *fetched* by this
module rather than merely relocated, which is exactly the capability this
module deliberately does not have.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field

from .. import paths
from ..plugins.loader import load_plugins

# A local plugin's package name must match this exactly, because
# `_load_local_plugin` derives the distribution name it looks up from the
# *directory's own* basename — `cobirb_plugins_<installed-dir-name>` — not
# from anything declared inside the package. Enforced at install time so a
# mismatch is a clear refusal here rather than a silent "never discovered"
# later, which is a much worse place to learn about a naming convention.
_NAME_PREFIX = "cobirb_plugins_"

# A plugin's own install step is local (copy a directory) but its `pip
# install -e` may need to build something, so this is generous compared to a
# tool call's own timeout — an install is a one-off, sat-and-watched action,
# not part of a turn loop with a person waiting on a reply.
_PIP_TIMEOUT_SECONDS = 300


class PluginInstallError(Exception):
    """A plugin could not be installed, with a reason a person can act on."""


@dataclass
class InstallResult:
    name: str
    path: str
    discovered_as: tuple[str, ...] = field(default_factory=tuple)
    pip_output: str = ""

    def describe(self) -> str:
        kinds = ", ".join(self.discovered_as) or "nothing"
        return f"Installed {self.name!r} at {self.path} — discovered as: {kinds}"


@dataclass
class RemoveResult:
    name: str
    removed_directory: bool
    pip_uninstalled: bool
    pip_output: str = ""

    def describe(self) -> str:
        if not self.removed_directory:
            return f"{self.name!r} is not an installed local plugin — nothing to remove."
        note = "" if self.pip_uninstalled else " (its pip package could not be uninstalled cleanly; the directory is gone, which is what matters for discovery)"
        return f"Removed {self.name!r}.{note}"


def _read_project_name(pyproject_path: str) -> str:
    try:
        with open(pyproject_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginInstallError(f"could not read {pyproject_path}: {exc}") from exc
    name = data.get("project", {}).get("name")
    if not name:
        raise PluginInstallError(f"{pyproject_path} has no [project].name")
    entry_points = data.get("project", {}).get("entry-points", {}).get("cobirb.plugins")
    if not entry_points:
        raise PluginInstallError(
            f"{pyproject_path} declares no [project.entry-points.\"cobirb.plugins\"] table — "
            "nothing here is a CoBirb plugin as far as the loader is concerned"
        )
    return str(name)


def _run_pip(*args: str) -> str:
    """Run pip and return its combined output, raising with that output on failure.

    ``sys.executable -m pip`` rather than a bare ``pip`` on PATH: this must
    install into the same interpreter CoBirb itself is running under, or
    ``importlib.metadata`` — which looks at *this* interpreter's installed
    distributions — would never see the result.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", *args],
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginInstallError(f"pip {args[0]} timed out after {_PIP_TIMEOUT_SECONDS}s") from exc
    output = f"{completed.stdout}{completed.stderr}"
    if completed.returncode != 0:
        raise PluginInstallError(f"pip {args[0]} failed:\n{output}")
    return output


def _make_importable_in_this_process(target: str) -> None:
    """Let the interpreter that just ran ``pip install -e`` actually import it.

    Found the hard way, by a test that ran the real install rather than
    mocking it: ``pip`` runs as a subprocess, and a fresh editable install
    normally becomes importable through a ``.pth`` file that Python's ``site``
    module only *processes at interpreter startup*. The metadata is visible
    immediately — ``importlib.metadata.distribution()`` finds it right away,
    because that just reads the dist-info pip wrote — but the actual module
    file is not on this already-running process's import path, so ``ep.load()``
    fails with a plain ``ModuleNotFoundError`` even though the install
    genuinely succeeded and would work perfectly the next time ``cobirb``
    starts fresh.

    That would make this function's own verification step spuriously fail a
    good install. Since the plugin's importable root is exactly ``target``
    (the directory pip was told to install with ``-e``, using the ``py-modules``
    flat layout this loader's naming convention assumes), adding it to
    ``sys.path`` directly sidesteps needing to know which editable-install
    mechanism a given setuptools version chose. It also means a plugin
    installed while CoBirb is already running becomes usable immediately
    rather than needing a restart — a genuine improvement, not only a fix for
    the verification check.
    """
    importlib.invalidate_caches()
    if target not in sys.path:
        sys.path.insert(0, target)


def install_plugin(source_dir: str, *, replace: bool = False) -> InstallResult:
    """Install the plugin at ``source_dir`` into ``~/.cobirb/plugins/``.

    ``source_dir`` must contain a ``pyproject.toml`` declaring a
    ``cobirb.plugins`` entry point, and its ``[project].name`` must be
    ``cobirb_plugins_<name>`` — the exact name the loader will look for once
    the directory lands under ``~/.cobirb/plugins/<name>/``. Refusing a
    mismatch here, before anything is copied, is the whole value of this
    function over doing the same steps by hand.

    Verified rather than assumed: after installing, this re-runs discovery
    and confirms the plugin actually appears. A plugin that fails that check
    is uninstalled and its copied directory removed — an install that leaves
    behind a directory the loader will never find is worse than no install at
    all, because it looks like it worked.
    """
    pyproject = os.path.join(source_dir, "pyproject.toml")
    if not os.path.isfile(pyproject):
        raise PluginInstallError(f"{source_dir} has no pyproject.toml")

    declared_name = _read_project_name(pyproject)
    if not declared_name.replace("-", "_").startswith(_NAME_PREFIX):
        raise PluginInstallError(
            f"[project].name is {declared_name!r}, but the loader only finds local plugins "
            f"whose package name starts with {_NAME_PREFIX!r} — see plugins/loader.py. "
            f"Rename it to {_NAME_PREFIX}<something> and try again."
        )
    name = declared_name.replace("-", "_")[len(_NAME_PREFIX):]
    if not name:
        raise PluginInstallError(f"[project].name {declared_name!r} has nothing after the prefix")

    target = os.path.join(paths.user_plugins_dir(), name)
    if os.path.exists(target):
        if not replace:
            raise PluginInstallError(
                f"{target} already exists — pass replace=True (or --replace) to reinstall it"
            )
        shutil.rmtree(target)

    os.makedirs(paths.user_plugins_dir(), exist_ok=True)
    shutil.copytree(source_dir, target)

    try:
        output = _run_pip("install", "-e", target)
        _make_importable_in_this_process(target)
        discovered, errors = load_plugins()
        found = tuple(sorted(key for key, cls in discovered.items() if key.endswith(f":{name}")))
        if not found:
            reason = errors.get(f"local:{name}", "the loader did not report why")
            raise PluginInstallError(f"installed, but not discovered as a plugin: {reason}")
    except PluginInstallError:
        # Never leave a directory behind that "worked" only by copying files —
        # a failed install should leave no more trace than an install that was
        # never attempted.
        _run_pip_uninstall_quietly(declared_name)
        shutil.rmtree(target, ignore_errors=True)
        raise

    return InstallResult(name=name, path=target, discovered_as=found, pip_output=output)


def _run_pip_uninstall_quietly(distribution_name: str) -> None:
    try:
        _run_pip("uninstall", "-y", distribution_name)
    except PluginInstallError:
        pass  # best-effort cleanup on a path that is already reporting a different failure


def remove_plugin(name: str) -> RemoveResult:
    """Remove a plugin ``cobirb plugin install`` put in ``~/.cobirb/plugins/``.

    Only ever touches what CoBirb's own local-plugin mechanism owns — a
    plugin installed by running plain ``pip install`` yourself is yours to
    ``pip uninstall``, the same way CoBirb never touches a project's own
    ``cobirb/plugins/`` directory. Removing the directory is what actually
    controls discovery going forward (``_load_local_plugin`` returns
    immediately if the directory or its ``pyproject.toml`` is gone); the pip
    uninstall alongside it is best-effort tidiness, not the source of truth.
    """
    target = os.path.join(paths.user_plugins_dir(), name)
    if not os.path.isdir(target):
        return RemoveResult(name=name, removed_directory=False, pip_uninstalled=False)

    distribution_name = f"{_NAME_PREFIX}{name}"
    pip_output = ""
    pip_ok = True
    try:
        pip_output = _run_pip("uninstall", "-y", distribution_name)
    except PluginInstallError as exc:
        pip_ok = False
        pip_output = str(exc)

    shutil.rmtree(target)
    return RemoveResult(name=name, removed_directory=True, pip_uninstalled=pip_ok, pip_output=pip_output)


def list_installed() -> list[str]:
    """Plugin names installed via ``cobirb plugin install`` — the contents of
    ``~/.cobirb/plugins/``, regardless of whether they currently load cleanly.

    Separate from ``runtime.plugins.describe_plugins()``, which shows every
    *active* plugin (these plus anything installed by plain ``pip install``
    of a published package). This answers the narrower question "what did
    `cobirb plugin install` put here", which matters for `remove` naming a
    target that actually exists to remove.
    """
    directory = paths.user_plugins_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(entry for entry in os.listdir(directory) if os.path.isdir(os.path.join(directory, entry)))
