"""``cobirb remote-worker``: this machine works for a main CoBirb elsewhere.

A pure worker. It listens on one address with a self-signed certificate, pairs
with a main machine by printing an 8-digit code the user types over there, and
runs one Worker Birb at a time — in a process of its own (``job``), so halting
it is absolute. It reads nothing from its own config: the work, the files, the
settings and by default the model all come from the main machine.

It stays loyal to its main machine only while that machine is alive: no
heartbeat for ``HALT_AFTER_SECONDS`` and the job is killed mid-whatever and its
workspace deleted. A dropped connection alone costs nothing — the job keeps
going, its events are kept, and the main machine picks up where it left off
(``resume``).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

from .. import __version__
from ..runtime.verify import DEFAULT_TIMEOUT_SECONDS, run_verification
from . import protocol
from .certs import certificate_fingerprint, ensure_certificate
from .osnames import local_os
from .trust import IssuedTokens


def machine_facts() -> dict[str, Any]:
    """What a main machine's Brainy Birb is told about this one."""
    return {
        "os": local_os(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "shell": "cmd.exe" if os.name == "nt" else "/bin/sh",
        "python": platform.python_version(),
        "cobirb": __version__,
    }


def _safe_path(root: str, relative: str) -> str:
    """``relative`` inside ``root``, or ``ValueError``: a file from the main
    machine is never written outside the workspace."""
    if not relative or os.path.isabs(relative) or relative.replace("\\", "/").startswith("/"):
        raise ValueError(f"not a relative path: {relative!r}")
    target = os.path.realpath(os.path.join(root, relative))
    base = os.path.realpath(root)
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"outside the workspace: {relative!r}")
    return target


def _write_files(root: str, files: "dict[str, bytes]") -> None:
    for relative, data in files.items():
        target = _safe_path(root, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop the job and everything it started, now."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, check=False)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.kill()
    except OSError:
        pass


class _Job:
    """The one job this remote may hold, and everything it said."""

    def __init__(self, owner: str, order: dict[str, Any], workspace: str) -> None:
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.order = order
        self.workspace = workspace
        self.process: subprocess.Popen | None = None
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.last_heartbeat = time.monotonic()
        self.sink: Callable[[str], None] | None = None
        self.lock = threading.Lock()


class RemoteWorkerServer:
    """The server. ``say`` is where it talks to the person at this machine."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8443, *, directory: str | None = None,
                 say: Callable[[str], None] = print, halt_after: float = protocol.HALT_AFTER_SECONDS,
                 python: str = sys.executable) -> None:
        self.host, self.port = host, port
        self._say = say
        self._halt_after = halt_after
        self._python = python
        self._tokens = IssuedTokens(os.path.join(directory, "tokens.json") if directory else None)
        self._cert, self._key = ensure_certificate(directory, (host,) if host not in ("0.0.0.0", "::") else ())
        self.fingerprint = certificate_fingerprint(self._cert)
        self._job: _Job | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._server = None

    # ------------------------------------------------------------------ #
    def serve_forever(self, ready: threading.Event | None = None) -> None:
        from websockets.sync.server import serve

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._cert, self._key)
        threading.Thread(target=self._watchdog, daemon=True, name="remote-watchdog").start()
        with serve(self._handle, self.host, self.port, ssl=context,
                   max_size=protocol.MAX_MESSAGE_BYTES) as server:
            self._server = server
            self.port = server.socket.getsockname()[1]
            self._say(f"CoBirb remote worker ({local_os()}) listening on {self.host}:{self.port}")
            self._say(f"Certificate fingerprint: {self.fingerprint}")
            self._say("Compare it with the one your main CoBirb shows before trusting it.")
            if ready is not None:
                ready.set()
            server.serve_forever()

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            if self._job is not None:
                self._end(self._job)
        if self._server is not None:
            self._server.shutdown()

    # ------------------------------------------------------------------ #
    def _state_for(self, owner: str) -> str:
        with self._lock:
            job = self._job
        if job is None:
            return protocol.STATE_IDLE
        return protocol.STATE_YOURS if job.owner == owner else protocol.STATE_BUSY

    def _handle(self, connection) -> None:
        send_lock = threading.Lock()

        def send(kind: str, **fields: Any) -> None:
            with send_lock:
                connection.send(protocol.encode(kind, **fields))

        def reply(request: dict[str, Any], **fields: Any) -> None:
            send("reply", id=request.get("id"), **fields)

        try:
            hello = protocol.decode(connection.recv(timeout=30))
        except Exception:  # noqa: BLE001 - a peer that cannot say hello is simply not served
            return
        if hello.get("type") != "hello" or hello.get("protocol") != protocol.PROTOCOL_VERSION:
            send("error", error=f"this remote speaks protocol {protocol.PROTOCOL_VERSION}; "
                                "update CoBirb on one side")
            return
        token = hello.get("token")
        authenticated = self._tokens.valid(token)
        send("hello", protocol=protocol.PROTOCOL_VERSION, facts=machine_facts(),
             authenticated=authenticated,
             state=self._state_for(hashlib.sha256(str(token).encode()).hexdigest()))
        if not authenticated:
            token = self._pair(connection, send)
            if not token:
                return
        owner = hashlib.sha256(token.encode()).hexdigest()
        attached: list[_Job] = []

        def sink(line: str) -> None:
            with send_lock:
                connection.send(line)

        try:
            for raw in connection:
                try:
                    message = protocol.decode(raw)
                except protocol.ProtocolError as exc:
                    send("error", error=str(exc))
                    continue
                self._dispatch(message, owner, send, reply, sink, attached)
        except Exception:  # noqa: BLE001 - a dropped connection; the job carries on until the watchdog decides
            pass
        finally:
            for job in attached:
                with job.lock:
                    if job.sink is sink:
                        job.sink = None

    def _pair(self, connection, send) -> str:
        code = f"{secrets.randbelow(10**8):08d}"
        self._say(f"Pairing request. Enter this code in your main CoBirb: {code[:4]} {code[4:]}")
        for attempt in range(protocol.PAIRING_ATTEMPTS):
            try:
                message = protocol.decode(connection.recv(timeout=600))
            except Exception:  # noqa: BLE001 - the main side went away mid-pairing
                return ""
            if message.get("type") == "pair" and secrets.compare_digest(str(message.get("code", "")), code):
                token = self._tokens.issue()
                send("paired", token=token)
                self._say("Paired.")
                return token
            send("pair_failed", attempts_left=protocol.PAIRING_ATTEMPTS - attempt - 1)
        self._say("Pairing failed: too many wrong codes.")
        return ""

    # ------------------------------------------------------------------ #
    def _dispatch(self, message, owner, send, reply, sink, attached) -> None:
        kind = message.get("type")
        with self._lock:
            job = self._job
        mine = job is not None and job.owner == owner
        if kind == "heartbeat":
            if mine:
                job.last_heartbeat = time.monotonic()
            send("heartbeat", id=message.get("id"), state=self._state_for(owner))
        elif kind == "which":
            programs = [str(p) for p in message.get("programs") or []]
            reply(message, found=[p for p in programs if shutil.which(p)])
        elif kind == "run":
            scratch = tempfile.mkdtemp(prefix="cobirb-check-")
            try:
                result = run_verification(str(message.get("command", "")), scratch,
                                          int(message.get("seconds") or DEFAULT_TIMEOUT_SECONDS))
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            reply(message, **_result(result))
        elif kind == "job_start":
            reply(message, **self._start(message, owner, sink, attached))
        elif kind == "resume":
            if mine and job.id == message.get("job_id"):
                self._attach(job, sink, attached, int(message.get("since") or 0))
                reply(message, ok=True)
            else:
                reply(message, ok=False, error="no such job here any more")
        elif not mine:
            reply(message, ok=False, error="this connection holds no job here")
        elif kind == "push":
            try:
                _write_files(job.workspace, protocol.unpack_files(message.get("files")))
                reply(message, ok=True)
            except (ValueError, OSError, protocol.ProtocolError) as exc:
                reply(message, ok=False, error=str(exc))
        elif kind == "check":
            reply(message, **self._check(job, message))
        elif kind == "fetch":
            files = {}
            for relative in job.order.get("worker", {}).get("writes") or []:
                try:
                    with open(_safe_path(job.workspace, relative), "rb") as fh:
                        files[relative] = fh.read()
                except (OSError, ValueError):
                    continue
            reply(message, ok=True, files=protocol.pack_files(files))
        elif kind == "job_end":
            self._end(job)
            reply(message, ok=True)
        elif kind in ("answer", "model_chunk", "model_end", "model_error", "grant", "autopilot", "cancel"):
            self._forward(job, message)

    def _start(self, message, owner, sink, attached) -> dict[str, Any]:
        with self._lock:
            if self._job is not None:
                return {"ok": False, "error": "this remote is busy with another job"}
            workspace = tempfile.mkdtemp(prefix="cobirb-remote-")
            job = _Job(owner, message.get("order") or {}, workspace)
            self._job = job
        try:
            _write_files(workspace, protocol.unpack_files(message.get("files") or {}))
        except (ValueError, OSError, protocol.ProtocolError) as exc:
            self._end(job)
            return {"ok": False, "error": str(exc)}
        home = tempfile.mkdtemp(prefix="cobirb-remote-home-")
        env = dict(os.environ, COBIRB_HOME=home, PYTHONUNBUFFERED="1")
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        job.process = subprocess.Popen(
            [self._python, "-m", "cobirb.remote.job", workspace],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            env=env, cwd=workspace, **kwargs,
        )
        job.home = home  # type: ignore[attr-defined]
        assert job.process.stdin is not None
        job.process.stdin.write(json.dumps(job.order) + "\n")
        job.process.stdin.flush()
        self._attach(job, sink, attached, 0)
        threading.Thread(target=self._pump, args=(job,), daemon=True, name="remote-job-pump").start()
        worker = job.order.get("worker", {}).get("id", "?")
        self._say(f"Working on ticket '{worker}'.")
        return {"ok": True, "job_id": job.id}

    def _attach(self, job: _Job, sink, attached, since: int) -> None:
        with job.lock:
            job.sink = sink
            backlog = [e for e in job.events if e["seq"] > since]
        if job not in attached:
            attached.append(job)
        for event in backlog:
            try:
                sink(protocol.encode("event", **event))
            except Exception:  # noqa: BLE001 - dropped again; the next resume replays it
                return

    def _pump(self, job: _Job) -> None:
        """The job's stdout, numbered, kept, and passed on while someone listens."""
        assert job.process is not None and job.process.stdout is not None
        for line in job.process.stdout:
            try:
                message = protocol.decode(line)
            except protocol.ProtocolError:
                continue
            if message.get("type") != "event":
                continue
            event = message.get("event") or {}
            with job.lock:
                record = {"seq": len(job.events) + 1, "job_id": job.id, "event": event}
                job.events.append(record)
                sink = job.sink
                if event.get("kind") == "done":
                    job.done = True
            if sink is not None:
                try:
                    sink(protocol.encode("event", **record))
                except Exception:  # noqa: BLE001 - the connection dropped; kept for resume
                    pass
            if event.get("kind") == "done":
                self._say(f"Ticket '{job.order.get('worker', {}).get('id', '?')}' finished.")

    def _forward(self, job: _Job, message: dict[str, Any]) -> None:
        process = job.process
        if process is None or process.poll() is not None or process.stdin is None:
            return
        try:
            process.stdin.write(protocol.encode(message["type"], **{k: v for k, v in message.items() if k != "type"}) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            pass

    def _check(self, job: _Job, message: dict[str, Any]) -> dict[str, Any]:
        """A ticket's check in the workspace, with ``overrides`` swapped in and
        put back afterwards — the re-check, and the review's stub pass."""
        try:
            overrides = protocol.unpack_files(message.get("overrides") or {})
            saved: dict[str, bytes | None] = {}
            for relative in overrides:
                target = _safe_path(job.workspace, relative)
                saved[target] = None
                if os.path.exists(target):
                    with open(target, "rb") as fh:
                        saved[target] = fh.read()
        except (ValueError, OSError, protocol.ProtocolError) as exc:
            return {"ok": False, "output": "", "error": str(exc)}
        try:
            _write_files(job.workspace, overrides)
            result = run_verification(str(message.get("command", "")), job.workspace,
                                      int(message.get("seconds") or DEFAULT_TIMEOUT_SECONDS))
        finally:
            for target, content in saved.items():
                try:
                    if content is None:
                        os.unlink(target)
                    else:
                        with open(target, "wb") as fh:
                            fh.write(content)
                except OSError:
                    pass
        return _result(result)

    def _end(self, job: _Job) -> None:
        if job.process is not None:
            _kill_tree(job.process)
            try:
                job.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(job.workspace, ignore_errors=True)
        home = getattr(job, "home", "")
        if home:
            shutil.rmtree(home, ignore_errors=True)
        with self._lock:
            if self._job is job:
                self._job = None

    def _watchdog(self) -> None:
        """Halt a job whose main machine has gone quiet for too long."""
        while not self._stopping.wait(1.0):
            with self._lock:
                job = self._job
            if job is not None and time.monotonic() - job.last_heartbeat > self._halt_after:
                self._say(f"No heartbeat from the main CoBirb for {int(self._halt_after)} s: "
                          "halting the job and discarding its work.")
                self._end(job)


def _result(result) -> dict[str, Any]:
    return {"ok": bool(result.ok), "output": result.output, "timed_out": bool(result.timed_out),
            "error": result.error or ""}


def main(listen: str = "0.0.0.0:8443") -> int:
    """``cobirb remote-worker [--listen HOST:PORT]``."""
    host, _, port = listen.rpartition(":")
    # Flushed line by line: the pairing code has to appear the moment it is
    # made, also when this runs under a service manager or a pipe.
    server = RemoteWorkerServer(host or "0.0.0.0", int(port or 8443),
                                say=lambda line: print(line, flush=True))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0
