"""The main machine's end of the connection to one Remote Worker Birb.

Opens the TLS WebSocket (the main machine never listens), checks the remote's
certificate against the one the user trusted, pairs with the 8-digit code when
there is no valid token, and then keeps the connection alive: a heartbeat at
least every ``HEARTBEAT_SECONDS``, and after a drop a reconnect that resumes the
job's events where they left off. A remote that cannot be reached again within
``HALT_AFTER_SECONDS`` will have halted its job by then, so the client stops
trying and says the job is lost.

Thread-based, like the rest of CoBirb: a receiver thread, a heartbeat thread
and an event thread, so a slow event handler never delays a heartbeat.
"""

from __future__ import annotations

import contextlib
import itertools
import queue
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

from . import protocol
from .certs import fingerprint
from .settings import RemoteSpec
from .trust import TrustStore


class RemoteError(Exception):
    """The remote could not be used; the message says why, for the user."""


class Untrusted(RemoteError):
    """The certificate is not one the user trusts (or they declined it)."""


class NotPaired(RemoteError):
    """No valid pairing, and nobody here to type a code."""


class RemoteClient:
    """One connection to one remote.

    ``ask_trust(spec, fingerprint) -> bool`` and ``ask_code(spec) -> str | None``
    are how pairing talks to the user; without them an unpaired or untrusted
    remote is refused rather than guessed at (fail closed).
    """

    def __init__(
        self,
        spec: RemoteSpec,
        store: TrustStore | None = None,
        *,
        ask_trust: Callable[[RemoteSpec, str], bool] | None = None,
        ask_code: Callable[[RemoteSpec], str | None] | None = None,
        heartbeat_seconds: float = protocol.HEARTBEAT_SECONDS,
        reconnect_for: float = protocol.HALT_AFTER_SECONDS,
        open_timeout: float = 10.0,
    ) -> None:
        self.spec = spec
        self._store = store or TrustStore()
        self._ask_trust = ask_trust
        self._ask_code = ask_code
        self._heartbeat_seconds = heartbeat_seconds
        self._reconnect_for = reconnect_for
        self._open_timeout = open_timeout
        self._ids = itertools.count(1)
        self._pending: dict[Any, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._connection = None
        self._closing = threading.Event()
        self._connected = threading.Event()
        self.facts: dict[str, Any] = {}
        self.state = protocol.STATE_IDLE
        self.job_id = ""
        self.last_seq = 0
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._handler: Callable[[dict[str, Any]], None] | None = None

    # ------------------------------------------------------------------ #
    # Connecting
    # ------------------------------------------------------------------ #
    def connect(self) -> dict[str, Any]:
        """Open the connection, trusting and pairing as needed; the remote's facts."""
        self._open(interactive=True)
        threading.Thread(target=self._heartbeats, daemon=True, name="remote-heartbeat").start()
        threading.Thread(target=self._dispatch_events, daemon=True, name="remote-events").start()
        return self.facts

    def _open(self, *, interactive: bool) -> None:
        from websockets.sync.client import connect

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Verified by pinning below, not by a certificate authority: a remote on
        # a home network has a self-signed certificate by design.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            connection = connect(
                self.spec.websocket_url,
                ssl=context,
                open_timeout=self._open_timeout,
                max_size=protocol.MAX_MESSAGE_BYTES,
                close_timeout=5,
            )
        except Exception as exc:  # every way of not connecting is "unreachable" to the user
            raise RemoteError(f"could not reach the remote at {self.spec.url}: {exc}") from exc
        # The connection outlives any `with` block here (it is held for hours and
        # closed by close() or a drop), so it is entered directly — which is what
        # websockets asks for, on every version this supports.
        connection.__enter__()
        try:
            self._verify(connection, interactive)
            connection.send(
                protocol.encode(
                    "hello", protocol=protocol.PROTOCOL_VERSION, token=self._store.token(self.spec.url)
                )
            )
            hello = protocol.decode(connection.recv(timeout=30))
            if hello.get("type") == "error":
                raise RemoteError(str(hello.get("error")))
            if hello.get("type") != "hello" or hello.get("protocol") != protocol.PROTOCOL_VERSION:
                raise RemoteError("the remote speaks a different protocol; update CoBirb on one side")
            if not hello.get("authenticated"):
                self._pair(connection, interactive)
            self._store.used(self.spec.url)
            self.facts = dict(hello.get("facts") or {})
            self.state = str(hello.get("state") or protocol.STATE_IDLE)
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._connected.set()
        threading.Thread(
            target=self._receive, args=(connection,), daemon=True, name="remote-receiver"
        ).start()

    def _verify(self, connection, interactive: bool) -> None:
        der = connection.socket.getpeercert(binary_form=True)
        seen = fingerprint(der)
        pinned = self._store.fingerprint(self.spec.url)
        if pinned == seen:
            return
        if pinned:
            raise Untrusted(
                f"the remote at {self.spec.url} presented a different certificate ({seen}) from the "
                f"one you trusted ({pinned}); it is refused until you trust it again"
            )
        if not interactive or self._ask_trust is None or not self._ask_trust(self.spec, seen):
            raise Untrusted(f"the certificate of {self.spec.url} is not trusted")
        self._store.trust(self.spec.url, seen)

    def _pair(self, connection, interactive: bool) -> None:
        if not interactive or self._ask_code is None:
            raise NotPaired(f"{self.spec.label()} is not paired — pair it when CoBirb starts")
        while True:
            code = self._ask_code(self.spec)
            if not code:
                raise NotPaired(f"pairing with {self.spec.label()} was cancelled")
            connection.send(protocol.encode("pair", code="".join(code.split())))
            answer = protocol.decode(connection.recv(timeout=60))
            if answer.get("type") == "paired":
                self._store.paired(self.spec.url, str(answer.get("token", "")))
                return
            if not answer.get("attempts_left"):
                raise NotPaired(f"pairing with {self.spec.label()} failed: too many wrong codes")

    # ------------------------------------------------------------------ #
    # Staying connected
    # ------------------------------------------------------------------ #
    def _receive(self, connection) -> None:
        try:
            for raw in connection:
                try:
                    message = protocol.decode(raw)
                except protocol.ProtocolError:
                    continue
                kind = message.get("type")
                if kind == "heartbeat":
                    self.state = str(message.get("state") or self.state)
                if kind in ("reply", "heartbeat"):
                    with self._pending_lock:
                        waiting = self._pending.pop(message.get("id"), None)
                    if waiting is not None:
                        waiting.put(message)
                elif kind == "event":
                    seq = int(message.get("seq") or 0)
                    if seq > self.last_seq:
                        self.last_seq = seq
                        self._events.put(message.get("event") or {})
        except Exception:  # noqa: BLE001 - the connection dropped; reconnecting below
            pass
        if connection is not self._connection:
            return
        self._connected.clear()
        self._fail_pending("the connection to the remote dropped")
        if not self._closing.is_set():
            threading.Thread(target=self._reconnect, daemon=True, name="remote-reconnect").start()

    def _reconnect(self) -> None:
        """Keep trying until the remote would have halted anyway."""
        deadline = time.monotonic() + self._reconnect_for
        delay = 1.0
        while not self._closing.is_set() and time.monotonic() < deadline:
            try:
                self._open(interactive=False)
            except RemoteError:
                self._closing.wait(delay)
                delay = min(delay * 2, 15.0)
                continue
            if self.job_id:
                try:
                    answer = self.request("resume", job_id=self.job_id, since=self.last_seq)
                except RemoteError:
                    answer = {"ok": False}
                if not answer.get("ok"):
                    self._events.put({"kind": "lost", "error": "the remote no longer has this job"})
            return
        if self.job_id and not self._closing.is_set():
            self._events.put(
                {
                    "kind": "lost",
                    "error": f"the remote at {self.spec.url} could not be "
                    "reached again before it halted the job",
                }
            )

    def _heartbeats(self) -> None:
        while not self._closing.wait(self._heartbeat_seconds):
            if self._connected.is_set():
                self.heartbeat(wait=False)

    def heartbeat(self, *, wait: bool = True, timeout: float = 10.0) -> str:
        """Send a heartbeat; with ``wait``, return the remote's state."""
        beat = f"hb-{next(self._ids)}"
        waiting: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._pending_lock:
            self._pending[beat] = waiting
        try:
            self._send("heartbeat", id=beat)
        except RemoteError:
            with self._pending_lock:
                self._pending.pop(beat, None)
            return self.state
        if not wait:
            with self._pending_lock:
                self._pending.pop(beat, None)
            return self.state
        try:
            return str(waiting.get(timeout=timeout).get("state") or self.state)
        except queue.Empty:
            return self.state
        finally:
            with self._pending_lock:
                self._pending.pop(beat, None)

    def _fail_pending(self, why: str) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for waiting in pending.values():
            waiting.put({"type": "reply", "ok": False, "error": why, "lost": True})

    # ------------------------------------------------------------------ #
    # Talking
    # ------------------------------------------------------------------ #
    def _send(self, kind: str, **fields: Any) -> None:
        connection = self._connection
        if connection is None or not self._connected.is_set():
            raise RemoteError("not connected to the remote")
        try:
            with self._send_lock:
                connection.send(protocol.encode(kind, **fields))
        except Exception as exc:  # a send on a dropped connection
            raise RemoteError(f"the connection to the remote dropped: {exc}") from exc

    def send(self, kind: str, **fields: Any) -> None:
        """Fire and forget; a message lost to a drop is the job's to notice."""
        with contextlib.suppress(RemoteError):
            self._send(kind, **fields)

    def request(self, kind: str, *, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        """Send a request and wait for its reply. Raises ``RemoteError`` when the
        connection drops first."""
        request_id = next(self._ids)
        waiting: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._pending_lock:
            self._pending[request_id] = waiting
        try:
            self._send(kind, id=request_id, **fields)
            answer = waiting.get(timeout=timeout)
        except queue.Empty as exc:
            raise RemoteError(f"the remote did not answer '{kind}' in time") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if answer.get("lost"):
            raise RemoteError(str(answer.get("error")))
        return answer

    def on_event(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handler = handler

    def _dispatch_events(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            handler = self._handler
            if handler is not None:
                with contextlib.suppress(Exception):  # a broken handler must not stop the heartbeats
                    handler(event)

    def close(self) -> None:
        self._closing.set()
        self._events.put(None)
        connection = self._connection
        if connection is not None:
            with contextlib.suppress(Exception):  # already gone is closed enough
                connection.close()
