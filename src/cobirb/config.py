"""User + repo scoped config, with model/provider settings opt-in.

Config is read-only for the core. Model/provider settings are opt-in and never
default to any networked provider.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import paths


def _load(path: str | None) -> dict[str, Any]:
    """Read one config layer, reporting and skipping anything unreadable.

    A stray comma used to escape as a raw ``JSONDecodeError`` from whichever
    command happened to construct a ``Config`` — which is all of them,
    including ``cobirb help``, which never reads a config key. Everywhere else
    in this codebase a broken input is reported and stepped over; these two
    layers were the exception.

    Reported to stderr and skipped, rather than exiting: the other layer may
    be perfectly good, and losing a run because the *user-scoped* file has a
    typo in it would be a worse trade. The message names the file so the typo
    is findable.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cobirb: ignoring {path} — {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        # Valid JSON, wrong shape: a list or a bare string would break the
        # merge below in a much less obvious place.
        print(f"cobirb: ignoring {path} — expected a JSON object", file=sys.stderr)
        return {}
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Config:
    """A simple, layered configuration reader."""

    def __init__(
        self,
        user_path: str | None = None,
        repo_path: str | None = None,
        cwd: str | None = None,
    ) -> None:
        # The repo-scoped config belongs to the directory CoBirb is working
        # *on* (``--cwd``), not the directory the process happened to be
        # launched from — otherwise `cd ~ && cobirb --cwd /project` silently
        # ignores /project/cobirb.json, the same class of mistake the tools
        # layer already had with relative paths.
        self.user_path = user_path or paths.config_path()
        self.repo_path = repo_path or os.path.join(cwd or ".", "cobirb.json")
        user = _load(self.user_path)
        repo = _load(self.repo_path)
        # Kept separately as well as merged: a few settings are deliberately
        # readable only from the user's own file. See `user_get`.
        self._user_data = user
        # Repo config overrides user config.
        self._data = _merge(user, repo)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, *keys: str, default: Any = None) -> Any:
        """Return a nested value, e.g. ``config.get("models", "default", "name")``."""
        value: Any = self._data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def user_get(self, *keys: str, default: Any = None) -> Any:
        """Read a setting from the **user's own** config file only.

        Repo-scoped ``cobirb.json`` normally overrides the user file, which is
        what you want for "which model does this project use" and emphatically
        not what you want for anything that can execute code. Two settings can:
        ``hooks`` runs shell commands at lifecycle points, and ``mcp_servers``
        launches long-lived subprocesses. Read through ``get()`` those would
        mean that cloning a repository and running CoBirb in it hands that
        repository's author a shell — before the model is ever asked anything,
        and with no approval prompt in the way, because the prompt is a thing
        CoBirb decides to show and this would be code deciding whether to show
        it.

        So those two are read from here instead: a file only the user writes,
        outside any repository. It costs per-project hooks, which is a real
        loss and a deliberate one — the alternative is a permission model that
        the contents of a downloaded directory can rewrite.
        """
        value: Any = self._user_data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, *keys: str, value: Any = None) -> None:
        """Set a nested value, creating intermediate dicts as needed."""
        node: Any = self._data
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value
