"""Plugin discovery and loading.

Discovers plugins from (in order): installed entry points, then local plugin
directories. Loading is lazy, cached per run, and **fail-closed**: a broken plugin
never bricks the core — the error is reported and the core continues.
"""
from __future__ import annotations

import importlib
import importlib.metadata as im
import os
from typing import Any

from .. import paths
from ..typing import spi as cobirb_typing


# Maps an SPI interface name to the base class it implements.
_INTERFACES = {
    "model": cobirb_typing.ModelProvider,
    "tool": cobirb_typing.Tool,
    "io": cobirb_typing.I_OAdapter,
    "crypto": cobirb_typing.SessionCrypto,
}


class PluginError(Exception):
    """Raised when a plugin fails to load."""


def _is_subclass(obj: Any, base: type) -> bool:
    return isinstance(obj, type) and issubclass(obj, base) and obj is not base


def load_plugins(entry_points: im.EntryPoints | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Discover and load all available plugins.

    Returns ``(discovered, errors)`` where ``discovered`` maps ``"kind:name"``
    to a plugin class and ``errors`` maps a plugin identifier to its failure
    message. Never raises for a single failing plugin.
    """
    discovered: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for ep in im.entry_points(group="cobirb.plugins"):
        try:
            obj = ep.load()
            for kind, base in _INTERFACES.items():
                if _is_subclass(obj, base):
                    # Checked here, at the boundary, rather than at every call
                    # site later: a plugin that cannot be honoured should never
                    # reach the registry at all. Raises IncompatiblePlugin,
                    # which the same handler reports as any other failure —
                    # the run continues without it, as it does for a plugin
                    # that simply fails to import.
                    cobirb_typing.check_spi_version(obj)
                    discovered[f"{kind}:{ep.name}"] = obj
                    break
        except Exception as exc:  # noqa: BLE001 - fail-closed per plugin
            errors[f"entry-point:{ep.name}"] = str(exc)

    # Local plugin directories (project + user), if present.
    for search_dir in _plugin_dirs():
        if not os.path.isdir(search_dir):
            continue
        for name in os.listdir(search_dir):
            path = os.path.join(search_dir, name)
            if not os.path.isdir(path):
                continue
            # The try sits inside the interface loop so the failing kind is
            # actually known. Wrapping the loop instead would key the error on
            # `kind` after the fact — a variable that leaks out of a for
            # statement, filing every failure under whichever interface
            # happened to be tried first regardless of the real cause.
            for kind, base in _INTERFACES.items():
                try:
                    obj = _load_local_plugin(path, kind)
                except Exception as exc:  # noqa: BLE001 - fail-closed per plugin
                    errors[f"local:{name}"] = str(exc)
                    break
                if obj is not None:
                    discovered[f"{kind}:{name}"] = obj
                    break

    return discovered, errors


def _plugin_dirs() -> list[str]:
    """Directories where local plugins may live."""
    project = os.environ.get("COBIRB_PROJECT_DIR", ".")
    return [
        os.path.join(project, "cobirb", "plugins"),
        paths.user_plugins_dir(),
    ]


def _load_local_plugin(path: str, kind: str) -> Any | None:
    """Load a single local plugin, resolving the entry point declared in its
    pyproject.toml. Returns None if the plugin does not implement ``kind``,
    or its entry point resolves to something that doesn't actually subclass
    the SPI interface ``kind`` names — matching the same check the
    installed-entry-points path already applies, so a mismatched entry
    point name can't be trusted just because it happens to match.
    """
    mod_name = f"cobirb_plugins_{os.path.basename(path).replace('-', '_')}"
    if not os.path.exists(os.path.join(path, "pyproject.toml")):
        return None
    dist = importlib.metadata.distribution(mod_name)
    for ep in dist.entry_points:
        if ep.name != kind:
            continue
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            raise PluginError(f"failed loading plugin {os.path.basename(path)}: {exc}") from exc
        if not _is_subclass(obj, _INTERFACES[kind]):
            return None
        # Deliberately *not* wrapped in PluginError: an incompatible plugin is
        # not a broken one, and its own message already says what to do about
        # it. load_plugins() files it under the same per-plugin error key
        # either way, so the run continues without it.
        cobirb_typing.check_spi_version(obj)
        return obj
    return None
