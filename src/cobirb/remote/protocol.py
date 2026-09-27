"""The conversation between the main CoBirb and a Remote Worker Birb.

One TLS WebSocket, opened by the main machine; the main machine never listens.
Every message is a JSON object with a ``type``. A request carries an ``id`` and
is answered by a ``reply`` with the same ``id``.

**Connecting** (main → remote, then the answer):

- ``hello`` {protocol, token?} → ``hello`` {protocol, facts, state, authenticated}
- without a valid token, the remote prints an 8-digit code and waits for
  ``pair`` {code} → ``paired`` {token} or ``pair_failed`` {attempts_left}

**Requests** (main → remote, answered by ``reply``):

- ``which`` {programs} → {found}: which of these programs exist there
- ``run`` {command, seconds} → a check run in a scratch directory (``requires``)
- ``job_start`` {order, files} → {job_id}: start the Worker Birb
- ``push`` {files}: write files into the workspace (a colleague finished, or the
  round's final tree before a re-check)
- ``check`` {command, seconds, overrides} → a verification result: run a
  ticket's check in the workspace, with ``overrides`` swapped in and put back
  afterwards (the review's put-the-stub-back pass)
- ``fetch`` → {files}: the worker's own files, as they are now
- ``job_end``: discard the workspace
- ``resume`` {job_id, since}: replay the job's events after a reconnect

**The job** (remote → main, each numbered with ``seq`` so a reconnect loses
nothing): ``event`` {seq, event} where event is one of ``tool_call``,
``notice``, ``approval``, ``model``, ``grant``, ``done``. The main machine
answers ``approval`` with ``answer``, streams a relayed ``model`` call back
with ``model_chunk`` / ``model_end`` / ``model_error``, and may send ``grant``,
``autopilot`` and ``cancel`` at any time.

**Staying alive**: ``heartbeat`` {id} → ``heartbeat`` {id, state} at least
every ``HEARTBEAT_SECONDS``. A remote that hears nothing for
``HALT_AFTER_SECONDS`` halts its job hard — the worker is killed mid-call and
its workspace discarded — rather than working for a main machine that is gone.
"""
from __future__ import annotations

import base64
import json
from typing import Any

PROTOCOL_VERSION = 1
HEARTBEAT_SECONDS = 30
HALT_AFTER_SECONDS = 300
PAIRING_ATTEMPTS = 5
# Large enough for a project's source files in one push; a remote is sent only
# the files its ticket needs.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

STATE_IDLE = "idle"
STATE_BUSY = "busy"  # another main machine's job
STATE_YOURS = "yours"  # this connection's job is running or awaiting its end


class ProtocolError(Exception):
    """A message that does not belong in this conversation."""


def encode(kind: str, **fields: Any) -> str:
    return json.dumps({"type": kind, **fields})


def decode(text: str | bytes) -> dict[str, Any]:
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("a message must be an object with a type")
    return message


def pack_files(files: "dict[str, bytes]") -> dict[str, str]:
    """Files as base64, so any content survives JSON."""
    return {path: base64.b64encode(data).decode("ascii") for path, data in files.items()}


def unpack_files(files: object) -> "dict[str, bytes]":
    if not isinstance(files, dict):
        raise ProtocolError("files must be an object of path → base64")
    try:
        return {str(path): base64.b64decode(data) for path, data in files.items()}
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"a file is not valid base64: {exc}") from exc
