"""Configuration, read from the user's home directory and nowhere else.

**There is exactly one config file: `~/.cobirb/config.json`.** CoBirb does not
read a `cobirb.json` from the working directory, does not merge one over this
one, and does not look for one. A repository cannot configure CoBirb at all.

That used to be a two-layer merge with repo-overrides-user precedence, which is
the conventional shape and was the wrong one here. Configuration is not
preference in this tool — it decides what is pre-approved, which directories
may be read or written, which commands run at lifecycle points, and which
subprocesses start. A repository able to contribute any of that means cloning a
repository and running CoBirb inside it lets its author influence the
permission model, before the model is asked anything and with no prompt able to
intervene. That was not hypothetical: a committed `cobirb.json` naming
``allow_tools`` pre-approved those tools silently.

The narrower fixes — exempting the dangerous keys, or prompting once to trust a
directory — both leave the same shape in place and rely on the list of
dangerous keys staying correct forever. Removing the layer is the version with
no ongoing obligation attached.

**What a repository may still do is describe itself.** `AGENTS.md` project
instructions, the repo map, and prompt files under `<project>/.cobirb/commands/`
are all still read, because those are content for the model rather than
capability granted to it, and every tool call they lead to still goes through
the permission layer. The line is: a project may tell CoBirb about itself, never
tell CoBirb what it is allowed to do.

Model/provider settings remain opt-in and never default to anything networked.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import paths


def _load(path: str | None) -> dict[str, Any]:
    """Read the config file, reporting and skipping anything unreadable.

    A stray comma used to escape as a raw ``JSONDecodeError`` from whichever
    command happened to construct a ``Config`` — which is all of them,
    including ``cobirb help``, which never reads a config key. Everywhere else
    in this codebase a broken input is reported and stepped over; this was the
    exception.

    Reported to stderr and skipped, rather than exiting: losing a run because
    the config has a typo in it would be a worse trade than running with
    defaults, and the message names the file so the typo is findable.
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
        # Valid JSON, wrong shape: a list or a bare string would break every
        # `get` below in a much less obvious place.
        print(f"cobirb: ignoring {path} — expected a JSON object", file=sys.stderr)
        return {}
    return data


class Config:
    """The user's configuration. One file, one layer, no project overrides."""

    def __init__(self, user_path: str | None = None) -> None:
        self.path = user_path or paths.config_path()
        self._data = _load(self.path)

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

    def set(self, *keys: str, value: Any = None) -> None:
        """Set a nested value, creating intermediate dicts as needed."""
        node: Any = self._data
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value
