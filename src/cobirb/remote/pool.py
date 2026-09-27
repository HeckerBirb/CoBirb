"""The remotes a flock may use, and who is free.

Opened at the start of a flock with the pairings the user made when CoBirb
started — never prompting here, since a flock may be headless and a question in
the middle of planning is the wrong moment. A remote that cannot be reached, or
is not paired, is simply not offered to Brainy Birb, and ``problems`` says why.

One task per remote, outside the local concurrency limit: two local workers and
one remote worker run at once under a limit of two. A ticket goes to the first
idle remote of its OS; with none idle it waits, polling on the heartbeat until
one reports itself idle.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from . import protocol
from .client import RemoteClient, RemoteError
from .settings import RemoteSpec
from .trust import TrustStore


class RemotePool:
    def __init__(self, specs: "list[RemoteSpec]", store: TrustStore | None = None, *,
                 client_factory: Callable[..., RemoteClient] = RemoteClient,
                 poll_seconds: float = protocol.HEARTBEAT_SECONDS) -> None:
        self._specs = list(specs)
        self._store = store or TrustStore()
        self._factory = client_factory
        self._poll = poll_seconds
        self._clients: list[RemoteClient] = []
        self._busy: set[int] = set()
        self._lock = threading.Lock()
        self._freed = threading.Condition(self._lock)
        self.problems: list[str] = []

    def open(self) -> "RemotePool":
        for spec in self._specs:
            client = self._factory(spec, self._store)
            try:
                client.connect()
            except RemoteError as exc:
                self.problems.append(f"{spec.label()} is not available: {exc}")
                continue
            self._clients.append(client)
        return self

    def machines(self) -> list[dict[str, Any]]:
        """Each reachable remote's facts, for Brainy Birb."""
        return [{"label": c.spec.label(), **c.facts, "os": c.spec.os} for c in self._clients]

    def oses(self) -> set[str]:
        return {c.spec.os for c in self._clients}

    def any_for(self, os_family: str) -> RemoteClient | None:
        """A remote of that OS for a question that does not need it idle
        (``which``, a requirement check)."""
        return next((c for c in self._clients if c.spec.os == os_family), None)

    def acquire(self, os_family: str, stop: threading.Event,
                on_wait: "Callable[[], None] | None" = None) -> RemoteClient | None:
        """The first idle remote of ``os_family``; waits while none is.

        Idle means both "not running one of ours" and "the remote says idle" —
        another main machine may hold it. ``None`` if there is no remote of that
        OS at all, or the flock was stopped while waiting.
        """
        candidates = [i for i, c in enumerate(self._clients) if c.spec.os == os_family]
        if not candidates:
            return None
        waited = False
        while not stop.is_set():
            with self._lock:
                free = [i for i in candidates if i not in self._busy]
            for index in free:
                client = self._clients[index]
                if client.heartbeat(wait=True) == protocol.STATE_IDLE:
                    with self._lock:
                        if index not in self._busy:
                            self._busy.add(index)
                            return client
            if not waited and on_wait is not None:
                waited = True
                on_wait()
            with self._freed:
                self._freed.wait(timeout=self._poll)
        return None

    def release(self, client: RemoteClient) -> None:
        with self._freed:
            for index, known in enumerate(self._clients):
                if known is client:
                    self._busy.discard(index)
            self._freed.notify_all()

    def close(self) -> None:
        for client in self._clients:
            client.close()
        self._clients = []
