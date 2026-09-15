"""The memory catalogues a session has open.

``memory.py`` owns catalogue *files*; this owns the part that only makes
sense while something is running — which catalogues are unlocked, the crypto
backend they share, and the block of text they contribute to the system
prompt.

Split out of ``CoBirbApp``, which had grown ten methods of catalogue
bookkeeping mixed in among tabs, modals and worker threads. None of them
touched a widget, so none of them belonged to a screen: an app has plenty of
reasons to change, and "how a catalogue is unlocked" should not be one of
them. Nothing here imports Textual, so the same store serves any surface
that grows one later.

Every method that can fail returns an error *string* rather than raising:
these are all user-driven actions whose failures (wrong password, name
already taken) are ordinary and belong in front of the user as a sentence,
not as a traceback. ``""`` means it worked.
"""
from __future__ import annotations

from typing import Any

from .. import memory, paths
from ..config import Config
from .plugins import build_crypto, discover_plugins


class CatalogueStore:
    """Catalogues unlocked for the life of one session.

    Held in memory only, and never auto-loaded just because a catalogue
    exists on disk: existing and feeding the model are separate things.
    """

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self._crypto: Any | None = None
        self.loaded: dict[str, memory.MemoryCatalogue] = {}

    # ------------------------------------------------------------------ #
    # Reading what is on disk
    # ------------------------------------------------------------------ #
    def rows(self) -> list[memory.CatalogueFile]:
        """Every catalogue on disk, without opening any of them."""
        return memory.discover_catalogues()

    def ensure_public(self) -> None:
        """Create the always-present public catalogue if it isn't there yet.

        Raises ``OSError`` if the memories directory can't be written; the
        caller decides whether that is worth a message, because it should
        never cost the command that triggered it.
        """
        memory.ensure_public_exists()

    def _row_for(self, name: str) -> "memory.CatalogueFile | None":
        return next((row for row in self.rows() if row.name == name), None)

    @property
    def crypto(self) -> Any:
        """The crypto backend catalogues use — resolved once and reused, the
        same object sessions already resolve via ``plugins.build_crypto``."""
        if self._crypto is None:
            config = Config()
            _, discovered, _ = discover_plugins(self._cwd, config)
            self._crypto, _ = build_crypto(config, discovered)
        return self._crypto

    # ------------------------------------------------------------------ #
    # Changing what is open
    # ------------------------------------------------------------------ #
    def load(self, row: memory.CatalogueFile, password: "str | None") -> str:
        try:
            catalogue = memory.load(row.path, self.crypto, password)
        except memory.CatalogueError as exc:
            return str(exc)
        self.loaded[catalogue.name] = catalogue
        return ""

    def unload(self, name: str) -> None:
        self.loaded.pop(name, None)

    def create(self, name: str, password: str) -> str:
        try:
            catalogue = memory.create(paths.memories_dir(), name, self.crypto, password)
        except memory.CatalogueError as exc:
            return str(exc)
        self.loaded[catalogue.name] = catalogue
        return ""

    def delete(self, name: str) -> str:
        row = self._row_for(name)
        if row is None:
            return f"No catalogue named '{name}'."
        memory.delete(row.path)
        self.unload(name)
        return ""

    def rename(self, name: str, new_name: str) -> str:
        row = self._row_for(name)
        if row is None:
            return f"No catalogue named '{name}'."
        try:
            new_path = memory.rename(row.path, new_name)
        except memory.CatalogueError as exc:
            return str(exc)
        catalogue = self.loaded.pop(name, None)
        if catalogue is not None:
            catalogue.name = new_name
            catalogue.path = new_path
            self.loaded[new_name] = catalogue
        return ""

    def remember(self, name: str, fact: str) -> str:
        """Append ``fact`` to an already-loaded catalogue and save it."""
        catalogue = self.loaded.get(name)
        if catalogue is None:
            return f"'{name}' is not loaded."
        memory.append_fact(catalogue, fact)
        memory.save(catalogue, self.crypto)
        return ""

    # ------------------------------------------------------------------ #
    # What the model is told
    # ------------------------------------------------------------------ #
    def system_prompt(self) -> str:
        """This turn's contribution to the system prompt from every loaded
        catalogue — Brainy Birb's own context, never a Worker Birb's brief
        (see ``wiring.build_subagent``, which gets no project context at all
        for the same reason).

        Built fresh on every call rather than cached: a catalogue loaded or
        appended to mid-session must be reflected on the very next turn, not
        only on the one after the orchestrator happens to be rebuilt.
        """
        blocks = [catalogue.render() for catalogue in self.loaded.values()]
        return "\n\n".join(block for block in blocks if block)
