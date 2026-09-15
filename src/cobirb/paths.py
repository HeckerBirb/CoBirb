"""Where CoBirb keeps things on disk.

Every path under the user's CoBirb home is derived here, so ``COBIRB_HOME`` is
read in exactly one place and config, sessions, personas, the audit log and
local plugins can never disagree about where the tree is. A module that spells
the lookup out for itself is one typo away from writing into a directory
nothing else ever looks in — a file silently absent rather than reported
missing.

Everything here is a function, not a module constant: ``COBIRB_HOME`` is read
at call time so relocating it takes effect immediately, which is also how the
test suite gives every test its own isolated tree.
"""
from __future__ import annotations

import os

# The one directory name. Dotted, like every other tool's home.
_DIR = ".cobirb"


def cobirb_home() -> str:
    """The directory CoBirb's own tree lives *inside* — ``COBIRB_HOME`` when
    set (the tests set it per-test), otherwise the real home directory."""
    return os.environ.get("COBIRB_HOME", os.path.expanduser("~"))


def cobirb_dir() -> str:
    """``<home>/.cobirb`` — the tree itself."""
    return os.path.join(cobirb_home(), _DIR)


def config_path() -> str:
    """The config file — the only one there is.

    Configuration comes from here and nowhere else. No ``cobirb.json`` in the
    working directory is read, merged, or even looked for: see ``cobirb.config``
    for why a repository must not be able to configure the tool that is about
    to run inside it.
    """
    return os.path.join(cobirb_dir(), "config.json")


def sessions_dir() -> str:
    """Where interactive mode looks for and offers to save session files.

    A session opened with an explicit ``--session <path>`` elsewhere on disk
    is unaffected: this is where the Sessions tab looks, not a requirement.
    """
    return os.path.join(cobirb_dir(), "sessions")


def audit_path() -> str:
    """The opt-in audit log (see ``policy.AuditLog`` for why it is opt-in)."""
    return os.path.join(cobirb_dir(), "audit.jsonl")


def user_personas_dir() -> str:
    """Where a user's own persona files live.

    ``~/.cobirb/personas/``. This was ``~/cobirb/`` — no dot, no subdirectory
    — which matched nothing else CoBirb writes and meant a persona placed in
    the obvious spot never resolved.
    """
    return os.path.join(cobirb_dir(), "personas")


def user_plugins_dir() -> str:
    """Where a user's own local plugin directories live."""
    return os.path.join(cobirb_dir(), "plugins")


def memories_dir() -> str:
    """Where memory catalogues live — one file per catalogue: ``<name>.md``
    (plaintext, chmod 0600) or ``<name>.md.enc`` (password-protected)."""
    return os.path.join(cobirb_dir(), "memories")
