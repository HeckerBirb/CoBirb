"""Operating-system names, as Python reports them, reduced to one family each.

A remote is configured by the name its own Python would give: either
``platform.system()`` (``Windows``, ``Linux``, ``Darwin``) or ``sys.platform``
(``win32``, ``linux``, ``darwin``, ``freebsd14``), in any case. Both mean the
same machine to CoBirb, so each is mapped to one family, named as
``platform.system()`` names it. ``ACCEPTED`` is the documented list.
"""
from __future__ import annotations

import platform
import re

# family (as platform.system() names it) -> every accepted spelling, lower case.
# sys.platform appends a release number on the BSDs (freebsd14), matched below.
ACCEPTED: dict[str, tuple[str, ...]] = {
    "Linux": ("linux",),
    "Windows": ("windows", "win32"),
    "Darwin": ("darwin",),
    "FreeBSD": ("freebsd",),
    "OpenBSD": ("openbsd",),
    "NetBSD": ("netbsd",),
    "SunOS": ("sunos",),
    "AIX": ("aix",),
}

_BY_NAME = {name: family for family, names in ACCEPTED.items() for name in names}


def canonical_os(name: object) -> str | None:
    """The family ``name`` means, or ``None`` if it is not one CoBirb knows.

    ``"wINdOwS"``, ``"win32"`` and ``"Windows"`` are all ``"Windows"``;
    ``"freebsd14"`` is ``"FreeBSD"``.
    """
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    if key in _BY_NAME:
        return _BY_NAME[key]
    return _BY_NAME.get(re.sub(r"\d+$", "", key))


def local_os() -> str:
    """This machine's family, or ``platform.system()`` itself if unknown."""
    system = platform.system()
    return canonical_os(system) or system
