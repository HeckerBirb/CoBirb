"""One Remote Worker Birb, running in a process of its own on the remote.

Started by ``server`` as ``python -m cobirb.remote.job <workspace>`` with a
fresh, temporary ``COBIRB_HOME``, so nothing on the remote machine — its own
config, its sessions, its memories — reaches the worker: everything comes from
the main machine. A process of its own so that halting is absolute: the server
kills it, mid model call or mid test run, and nothing of it is left running.

The job speaks JSON lines: the work order arrives as the first line on stdin,
and afterwards stdin carries what the main machine sends (approval answers,
relayed model replies, grants, auto-pilot, cancel). Every line on the real
stdout is an event for the main machine; anything else that writes to stdout
is sent to stderr instead, so it can never corrupt the conversation.

It runs the ordinary ``run_worker`` — the same brief, the same policy built
from the ticket's scope, the same checkpoints and checks as a local Worker Birb.
"""
from __future__ import annotations

import dataclasses
import json
import os
import queue
import sys
import threading
from typing import Any

# Bounded like any result a person reads: a pane shows the start of a tool's
# output, and a remote must not flood the connection with a huge one.
_MAX_CONTENT = 8 * 1024


class _Channel:
    """The job's two directions: events out, messages in."""

    def __init__(self, out) -> None:
        self._out = out
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._out.write(json.dumps({"type": "event", "event": event}) + "\n")
            self._out.flush()


def _io(channel: _Channel, answers: "dict[int, queue.Queue]", answers_lock: threading.Lock):
    from ..runtime.headless import HeadlessIO
    from ..typing.spi import DECISION_DENY, ApprovalOutcome

    class JobIO(HeadlessIO):
        """What the worker shows, sent to its pane on the main machine; what it
        asks, asked there."""

        def __init__(self) -> None:
            super().__init__()
            self._next = 0

        def name(self) -> str:
            return "remote-job"

        def render_tool_call(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
            content = str(getattr(result, "content", result))
            channel.emit({"kind": "tool_call", "tool": tool_name, "arguments": arguments,
                          "ok": bool(getattr(result, "ok", True)), "content": content[:_MAX_CONTENT]})

        def render_notice(self, text: str) -> None:
            channel.emit({"kind": "notice", "text": text})

        def confirm_request(self, request: Any) -> ApprovalOutcome:
            with answers_lock:
                self._next += 1
                req_id = self._next
                waiting: "queue.Queue" = queue.Queue()
                answers[req_id] = waiting
            channel.emit({"kind": "approval", "req_id": req_id, "tool": request.tool_name,
                          "arguments": request.arguments, "scope": request.scope,
                          "preview": request.preview})
            try:
                answer = waiting.get()
            finally:
                with answers_lock:
                    answers.pop(req_id, None)
            if not isinstance(answer, dict):
                return ApprovalOutcome(decision=DECISION_DENY)
            return ApprovalOutcome(decision=str(answer.get("decision") or DECISION_DENY),
                                   instruction=str(answer.get("instruction") or ""))

    return JobIO()


def _grants(channel: _Channel):
    from ..policy import SessionGrants

    class ReportingGrants(SessionGrants):
        """A session grant made here is reported home, where it reaches every
        other agent; one arriving from home is applied without echoing it."""

        def grant(self, tool_name: str, arguments: "dict[str, Any] | None" = None) -> None:
            super().grant(tool_name, arguments)
            channel.emit({"kind": "grant", "tool": tool_name, "arguments": dict(arguments or {})})

        def apply(self, tool_name: str, arguments: "dict[str, Any] | None" = None) -> None:
            SessionGrants.grant(self, tool_name, arguments)

    return ReportingGrants()


def _write_config(settings: dict[str, Any]) -> None:
    """The main machine's settings, as this job's only config."""
    from .. import paths

    os.makedirs(paths.cobirb_dir(), mode=0o700, exist_ok=True)
    with open(paths.config_path(), "w", encoding="utf-8") as fh:
        json.dump(settings, fh)


def _model(order: dict[str, Any], channel: _Channel):
    model = order.get("model") or {}
    if model.get("mode") == "local":
        from ..plugins.core.openai import OpenAICompatibleProvider

        return None, OpenAICompatibleProvider(model=str(model.get("name", "")),
                                              base_url=str(model.get("endpoint", "")))
    from .relay import RelayProvider

    relay = RelayProvider(channel.emit, model.get("info") or {})
    return relay, relay


def run(order: dict[str, Any], workspace: str, messages, out) -> None:
    """Run the work order in ``workspace``: the job's whole life."""
    from ..config import Config
    from ..flock.charter import WorkerBrief
    from ..flock.supervisor import Canceller
    from ..flock.worker import run_worker
    from .osnames import local_os

    channel = _Channel(out)
    answers: "dict[int, queue.Queue]" = {}
    answers_lock = threading.Lock()
    autopilot = threading.Event()
    if order.get("refuse"):
        autopilot.set()
    grants = _grants(channel)
    for tool_name, arguments in order.get("grants") or []:
        grants.apply(tool_name, arguments)
    relay, model = _model(order, channel)
    canceller = Canceller()

    def read() -> None:
        for line in messages:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "answer":
                with answers_lock:
                    waiting = answers.get(message.get("req_id"))
                if waiting is not None:
                    waiting.put(message)
            elif kind in ("model_chunk", "model_end", "model_error") and relay is not None:
                relay.deliver(message)
            elif kind == "autopilot":
                autopilot.set() if message.get("on") else autopilot.clear()
            elif kind == "grant":
                grants.apply(str(message.get("tool", "")), message.get("arguments") or {})
            elif kind == "cancel":
                canceller.force()
        # The server closed our input: nobody is left to answer, so stop.
        canceller.force()
        with answers_lock:
            for waiting in answers.values():
                waiting.put(None)

    threading.Thread(target=read, daemon=True, name="remote-job-reader").start()

    worker_order = order.get("worker") or {}
    worker = WorkerBrief(
        id=str(worker_order.get("id", "remote")),
        brief=str(worker_order.get("brief", "")),
        writes=tuple(worker_order.get("writes") or ()),
        reads=tuple(worker_order.get("reads") or ()),
        accept=str(worker_order.get("accept", "")),
        tests=tuple(worker_order.get("tests") or ()),
        # Says in its brief which OS it is on and that only its files are here.
        runs_on=local_os(),
    )
    _write_config(order.get("config") or {})
    report = run_worker(
        worker, workspace, config=Config(), io=_io(channel, answers, answers_lock),
        canceller=canceller, grants=grants, refuse=autopilot.is_set, model=model,
        max_turns=int(order.get("max_turns") or 30),
    )
    channel.emit({"kind": "done", "report": dataclasses.asdict(report)})


def main() -> int:
    workspace = sys.argv[1]
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    # Anything else that prints goes to stderr: stdout is the conversation.
    sys.stdout = sys.stderr
    order_line = sys.stdin.readline()
    if not order_line:
        return 1
    run(json.loads(order_line), workspace, sys.stdin, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
