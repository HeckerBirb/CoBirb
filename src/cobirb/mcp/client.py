"""A minimal MCP client over stdio: JSON-RPC 2.0, one line per message.

Written by hand rather than pulled in as a dependency. The wire format is
JSON-RPC over a pipe and the handshake is three messages; the official SDK
brings asyncio, pydantic and a transport layer for HTTP variants this client
will never speak. For a project whose whole pitch is "no outbound network and
nothing you didn't ask for", a hundred lines of `json` and `subprocess` is a
better trade than a dependency tree.

**Environment is not inherited by default.** A server gets ``PATH``, ``HOME``,
``LANG`` and whatever the user listed under ``env``, and nothing else. This is
the one place where a subprocess CoBirb spawned could read the user's whole
environment — cloud credentials, API keys, tokens for services CoBirb has
nothing to do with — and hand them anywhere it likes. Servers that genuinely
need more can be given exactly what they need, or ``"inherit_env": true`` for
someone who has decided they trust it.

**Threading.** stdout is drained by a reader thread into a queue, because a
response has to be waited for with a timeout and a blocking ``readline`` has
none. stderr is drained by another, keeping only the last few lines, because a
chatty server that nobody reads will otherwise fill the pipe buffer and hang
mid-sentence — a failure that looks exactly like the server being slow.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
from collections import deque
from typing import Any

logger = logging.getLogger("cobirb")

# The revision of MCP this client implements. Servers negotiate: one that
# speaks a different version answers with its own, and this client's subset —
# initialize, tools/list, tools/call — has been stable across all of them.
PROTOCOL_VERSION = "2025-06-18"

CLIENT_NAME = "cobirb"

# Starting a server should be quick; it is a local process. Long enough for a
# `uvx`/`npx` server to unpack itself the first time, short enough that a
# server which will never answer doesn't hold up the session indefinitely.
DEFAULT_STARTUP_TIMEOUT = 30

# A tool call is the model waiting on a person's database or build system, so
# this is more generous than the handshake.
DEFAULT_CALL_TIMEOUT = 120

# Environment passed to a server unless it asks for more. Enough to find an
# interpreter and behave sanely in a locale; nothing that identifies the user
# or authenticates them anywhere.
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")

_MAX_STDERR_LINES = 20


class McpError(RuntimeError):
    """Anything that went wrong talking to a server."""


def _child_env(configured: dict[str, str] | None, inherit: bool) -> dict[str, str]:
    if inherit:
        env = dict(os.environ)
    else:
        env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    env.update({str(k): str(v) for k, v in (configured or {}).items()})
    return env


class StdioClient:
    """One MCP server, running as a child process.

    Constructed but not started; call ``start()``, which performs the handshake
    and raises ``McpError`` if the server cannot be reached or does not answer.
    Safe to ``close()`` more than once.
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        inherit_env: bool = False,
        startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
        call_timeout: int = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.cwd = cwd
        self.inherit_env = inherit_env
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout

        self._process: subprocess.Popen[str] | None = None
        # One mailbox per in-flight request, not one shared queue. A shared
        # queue works only while a single thread is ever waiting: with two,
        # whichever wakes first consumes whatever arrived and discards it if
        # the id does not match, so a concurrent caller loses its reply and
        # waits out its whole timeout. Nothing called this concurrently until
        # the Flock did.
        self._mailboxes: "dict[int, queue.Queue[dict[str, Any]]]" = {}
        # Set when the server's stdout closes, so everyone waiting gives up
        # rather than each sitting out its own timeout.
        self._eof = threading.Event()
        self._stderr: deque[str] = deque(maxlen=_MAX_STDERR_LINES)
        self._next_id = 0
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn the server and complete the MCP handshake."""
        try:
            self._process = subprocess.Popen(  # noqa: S603 - the command is the user's own
                [os.path.expanduser(self.command), *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_child_env(self.env, self.inherit_env),
                cwd=self.cwd,
                text=True,
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"could not start {self.command!r}: {exc}") from exc

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": _version()},
            },
            timeout=self.startup_timeout,
        )
        self.server_info = result.get("serverInfo") or {}
        # Required by the protocol: until this arrives the server is entitled
        # to reject everything else.
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        """Stop the server, politely then not.

        **End the process before closing its pipes**, in that order. A reader
        thread sitting in ``for line in process.stdout`` holds that stream's
        lock, and closing the stream from here waits for the read in flight to
        finish — which, for a server that has stopped answering but not
        exited, means waiting as long as that server happens to live. Ending
        the process first makes the blocked read return EOF immediately, so
        the close that follows is instant.
        """
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------ #
    # The three methods CoBirb actually uses
    # ------------------------------------------------------------------ #
    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool this server offers, following ``nextCursor`` pages.

        Bounded: a server that returns a cursor pointing at itself would
        otherwise page forever, and this runs during startup where a hang is
        indistinguishable from a slow machine.
        """
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=self.startup_timeout)
            tools.extend(t for t in (result.get("tools") or []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Invoke a tool. Returns ``(ok, text)``.

        A protocol-level ``isError`` is a *tool* failure, not a client one:
        the model should see the message and adapt, exactly as it does when a
        built-in tool returns ``ok=False``. Only a broken connection raises.
        """
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=self.call_timeout
        )
        return not bool(result.get("isError")), _flatten_content(result)

    # ------------------------------------------------------------------ #
    # JSON-RPC plumbing
    # ------------------------------------------------------------------ #
    def _request(self, method: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            # Registered before the request goes out, so a reply that arrives
            # before this thread reaches _await still has somewhere to land.
            self._mailboxes[request_id] = queue.Queue()
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            message = self._await(request_id, timeout, method)
        finally:
            with self._lock:
                self._mailboxes.pop(request_id, None)
        if "error" in message:
            error = message["error"] or {}
            raise McpError(f"{method} failed: {error.get('message', error)}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError(f"{self.name} is not running")
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise McpError(f"{self.name} stopped listening: {exc}{self._stderr_tail()}") from exc

    def _await(self, request_id: int, timeout: int, method: str) -> dict[str, Any]:
        """Wait for the response to ``request_id``, and only that one.

        The reader thread routes by id, so this waits on its own mailbox and
        cannot consume somebody else's reply. Anything unroutable is dropped
        there rather than here: notifications (progress, log lines) are
        advisory, and a request *from* the server — sampling, elicitation — is
        a capability this client never advertised, so a well-behaved server
        will not send one and a badly-behaved one is not owed an answer.
        """
        mailbox = self._mailboxes[request_id]
        try:
            message = mailbox.get(timeout=timeout)
        except queue.Empty:
            if self._eof.is_set():
                raise McpError(f"{self.name} exited{self._stderr_tail()}") from None
            raise McpError(
                f"{self.name} did not answer {method} within {timeout}s{self._stderr_tail()}"
            ) from None
        if message.get("__eof__"):
            raise McpError(f"{self.name} exited{self._stderr_tail()}")
        return message

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # Servers that print banners to stdout are a known
                    # nuisance. Log it and keep reading rather than treating
                    # the connection as broken.
                    logger.debug("mcp/%s: non-JSON on stdout: %.200s", self.name, line)
                    continue
                if isinstance(message, dict):
                    self._deliver(message)
        except (OSError, ValueError):
            pass
        finally:
            # Unblocks everyone waiting on a server that died, not just
            # whoever happens to be first in a queue.
            self._eof.set()
            with self._lock:
                waiting = list(self._mailboxes.values())
            for mailbox in waiting:
                mailbox.put({"__eof__": True})

    def _deliver(self, message: dict[str, Any]) -> None:
        """Route one message to the caller waiting for it, if any."""
        with self._lock:
            mailbox = self._mailboxes.get(message.get("id"))  # type: ignore[arg-type]
        if mailbox is not None:
            mailbox.put(message)
        else:
            logger.debug("mcp/%s: unrouted message %.120s", self.name, message)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                self._stderr.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def _stderr_tail(self) -> str:
        """The server's own last words, for an error message.

        Without this, a server that fails to start reports as a bare timeout
        and the actual cause — a missing package, a bad path — is sitting
        unread in a pipe.
        """
        if not self._stderr:
            return ""
        return "\n  " + "\n  ".join(self._stderr)


def _flatten_content(result: dict[str, Any]) -> str:
    """Turn an MCP result's content blocks into text for the model.

    Text blocks come through as themselves. Anything else — images, audio,
    embedded resources — is named rather than dropped, so a model that
    receives "[image content, 41kB]" knows something was returned that it
    cannot see, instead of concluding the call produced nothing.
    """
    blocks = result.get("content")
    if not isinstance(blocks, list):
        # Structured-only results are legal; showing the JSON is better than
        # showing nothing.
        structured = result.get("structuredContent")
        return json.dumps(structured) if structured is not None else ""

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            resource = block.get("resource") or {}
            text = resource.get("text")
            parts.append(str(text) if text else f"[resource {resource.get('uri', '?')}]")
        else:
            size = len(str(block.get("data", "")))
            parts.append(f"[{kind} content, {size} bytes, not shown]")
    return "\n".join(part for part in parts if part)


def _version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - a version string is not worth a failure
        return "0"
