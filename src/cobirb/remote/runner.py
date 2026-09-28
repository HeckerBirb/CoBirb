"""One ticket, worked on by a Remote Worker Birb, driven from the main machine.

``RemoteRun`` is the remote counterpart of ``flock.worker.run_worker``: it
returns the same ``WorkerReport``, and between start and finish the worker
behaves as a local one would — its tool calls appear in its pane, its questions
are asked there, a session grant reaches every agent, and auto-pilot switched on
mid-round reaches it too. What differs is where the process runs and what it can
see: only its ticket's files and the files it reads.

After the round, ``check`` runs the ticket's check on the remote against the
round's final files — the re-check, and with ``overrides`` the review's
put-the-stub-back pass — so a Windows test is judged on Windows.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import threading
from collections.abc import Callable
from typing import Any

from ..config import Config
from ..flock.charter import WorkerBrief
from ..flock.worker import WorkerReport
from ..runtime.models import ROLE_WORKER, build_for_role, resolve_role
from ..runtime.verify import DEFAULT_TIMEOUT_SECONDS, VerifyResult
from ..typing.spi import DECISION_DENY, ApprovalRequest, ToolResult
from . import protocol
from .client import RemoteClient, RemoteError
from .relay import answer_model_request, model_info

# The main machine's settings a remote worker runs under. Everything comes from
# here; hooks do not, since they name commands on this machine.
_SETTINGS = (
    "verify_timeout",
    "verify_fix_attempts",
    "redact_secrets",
    "checkpoints",
    "sandbox",
    "context_tokens",
    "max_num_ctx",
    "connect_timeout",
    "request_timeout",
)


def _read_files(cwd: str, paths: tuple[str, ...] | list[str]) -> dict[str, bytes]:
    files = {}
    for relative in paths:
        try:
            with open(os.path.join(cwd, relative), "rb") as fh:
                files[relative] = fh.read()
        except OSError:
            continue  # not written yet: a stub the skeleton did not make, or a new file
    return files


class _GrantRelay:
    """Registered with the session's grants, so one made by any local agent
    reaches the remote worker too."""

    def __init__(self, client: RemoteClient) -> None:
        self._client = client

    def grant(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        self._client.send("grant", tool=tool_name, arguments=dict(arguments or {}))


class RemoteRun:
    def __init__(
        self,
        client: RemoteClient,
        worker: WorkerBrief,
        cwd: str,
        *,
        config: Config,
        io: Any = None,
        grants: Any = None,
        refuse: Callable[[], bool] = lambda: False,
        max_turns: int = 30,
        provider: Any = None,
    ) -> None:
        self.client = client
        self.worker = worker
        self._cwd = cwd
        self._config = config
        self._io = io
        self._grants = grants
        self._refuse = refuse
        self._max_turns = max_turns
        self._provider = provider
        self._done = threading.Event()
        self._report: WorkerReport | None = None
        self._error = ""
        self._relay = _GrantRelay(client)

    # ------------------------------------------------------------------ #
    def _order(self) -> dict[str, Any]:
        worker = self.worker
        spec = self.client.spec
        if spec.run_llms_locally:
            name = spec.model_name or resolve_role(ROLE_WORKER, self._config).name
            model = {"mode": "local", "endpoint": spec.openai_endpoint, "name": name}
        else:
            if self._provider is None:
                self._provider = build_for_role(ROLE_WORKER, self._config)
            model = {"mode": "relay", "info": model_info(self._provider)}
        return {
            "worker": {
                "id": worker.id,
                "brief": worker.brief,
                "writes": list(worker.writes),
                "reads": list(worker.reads),
                "accept": worker.accept,
                "tests": list(worker.tests),
            },
            "config": {key: self._config.get(key) for key in _SETTINGS if self._config.get(key) is not None},
            "grants": [list(g) for g in (getattr(self._grants, "granted", ()) or ())],
            "refuse": bool(self._refuse()),
            "max_turns": self._max_turns,
            "model": model,
        }

    def execute(self, stop: threading.Event | None = None) -> WorkerReport:
        """Start the job, see it through, and bring its files home."""
        stop = stop or threading.Event()
        self.client.on_event(self._on_event)
        files = _read_files(self._cwd, list(self.worker.writes) + list(self.worker.reads))
        # Before the request, not after: the job's first events can arrive
        # ahead of the reply, and counting from zero again would replay them.
        self.client.last_seq = 0
        try:
            answer = self.client.request(
                "job_start", order=self._order(), files=protocol.pack_files(files), timeout=120
            )
        except RemoteError as exc:
            return self._failed(str(exc))
        if not answer.get("ok"):
            return self._failed(str(answer.get("error") or "the remote refused the job"))
        self.client.job_id = str(answer.get("job_id"))
        if self._grants is not None:
            self._grants.register(self._relay)
        autopilot = bool(self._refuse())
        cancelled = False
        while not self._done.wait(0.5):
            if stop.is_set() and not cancelled:
                cancelled = True
                self.client.send("cancel")
            now = bool(self._refuse())
            if now != autopilot:
                autopilot = now
                self.client.send("autopilot", on=now)
        if self._report is None:
            return self._failed(self._error or "the remote job ended without a report")
        self._bring_home()
        return self._report

    def _failed(self, error: str) -> WorkerReport:
        return WorkerReport(
            worker_id=self.worker.id, ok=False, error=f"on {self.client.spec.label()}: {error}"
        )

    def _bring_home(self) -> None:
        """The worker's own files, written here — and only those."""
        try:
            answer = self.client.request("fetch", timeout=120)
            files = protocol.unpack_files(answer.get("files") or {})
        except (RemoteError, protocol.ProtocolError) as exc:
            if self._report is not None:
                self._report.ok = False
                self._report.error = f"its files could not be brought back: {exc}"
            return
        owned = set(self.worker.writes)
        for relative, data in files.items():
            if relative not in owned:
                continue
            target = os.path.join(self._cwd, relative)
            os.makedirs(os.path.dirname(target) or self._cwd, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)

    # ------------------------------------------------------------------ #
    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "tool_call":
            render = getattr(self._io, "render_tool_call", None)
            if callable(render):
                render(
                    str(event.get("tool", "")),
                    dict(event.get("arguments") or {}),
                    ToolResult(ok=bool(event.get("ok")), content=str(event.get("content", ""))),
                )
        elif kind == "notice":
            render = getattr(self._io, "render_notice", None)
            if callable(render):
                render(str(event.get("text", "")))
        elif kind == "approval":
            threading.Thread(target=self._answer, args=(event,), daemon=True).start()
        elif kind == "model":
            if self._provider is None:
                self._provider = build_for_role(ROLE_WORKER, self._config)
            threading.Thread(
                target=answer_model_request, args=(self._provider, event, self.client.send), daemon=True
            ).start()
        elif kind == "grant":
            if self._grants is not None:
                self._grants.grant(str(event.get("tool", "")), dict(event.get("arguments") or {}))
        elif kind == "done":
            self._report = _report_from(self.worker.id, event.get("report") or {})
            self._done.set()
        elif kind == "lost":
            self._error = str(event.get("error") or "the connection to the remote was lost")
            self._done.set()

    def _answer(self, event: dict[str, Any]) -> None:
        """A remote worker's question, asked where a local one's would be."""
        decision, instruction = DECISION_DENY, ""
        ask = getattr(self._io, "confirm_request", None)
        if not self._refuse() and callable(ask):
            try:
                outcome = ask(
                    ApprovalRequest(
                        tool_name=str(event.get("tool", "")),
                        arguments=dict(event.get("arguments") or {}),
                        scope=str(event.get("scope", "")),
                        preview=str(event.get("preview", "")),
                        asked_by=self.worker.id,
                    )
                )
                decision = str(getattr(outcome, "decision", DECISION_DENY))
                instruction = str(getattr(outcome, "instruction", "") or "")
            except Exception:  # noqa: BLE001 - nobody could answer: no, as for a local worker
                decision = DECISION_DENY
        self.client.send("answer", req_id=event.get("req_id"), decision=decision, instruction=instruction)

    # ------------------------------------------------------------------ #
    def push(self, paths: list[str] | tuple[str, ...]) -> None:
        """A colleague finished: send its files, silently, as a shared tree would."""
        files = _read_files(self._cwd, paths)
        if files:
            with contextlib.suppress(RemoteError):  # the re-check pushes the final tree again anyway
                self.client.request("push", files=protocol.pack_files(files), timeout=120)

    def check(
        self, command: str, timeout: int = DEFAULT_TIMEOUT_SECONDS, overrides: dict[str, str] | None = None
    ) -> VerifyResult:
        """Run ``command`` on the remote against the round's final files, with
        ``overrides`` swapped in for the run."""
        try:
            self.client.request(
                "push",
                timeout=120,
                files=protocol.pack_files(
                    _read_files(self._cwd, list(self.worker.writes) + list(self.worker.reads))
                ),
            )
            # `seconds` is the check's own limit; `timeout` is how long to wait
            # for the answer, a minute longer so the remote can report a timeout.
            answer = self.client.request(
                "check",
                command=command,
                seconds=timeout,
                timeout=timeout + 60,
                overrides=protocol.pack_files({p: c.encode() for p, c in (overrides or {}).items()}),
            )
        except RemoteError as exc:
            return VerifyResult(command, ok=False, output="", error=str(exc))
        return VerifyResult(
            command,
            ok=bool(answer.get("ok")),
            output=str(answer.get("output", "")),
            timed_out=bool(answer.get("timed_out")),
            error=answer.get("error") or None,
        )

    def end(self) -> None:
        unregister = getattr(self._grants, "unregister", None)
        if callable(unregister):
            unregister(self._relay)
        with contextlib.suppress(RemoteError):
            self.client.request("job_end", timeout=60)
        self.client.job_id = ""


def _report_from(worker_id: str, data: dict[str, Any]) -> WorkerReport:
    known = {f.name for f in dataclasses.fields(WorkerReport)}
    fields = {k: v for k, v in data.items() if k in known}
    fields["worker_id"] = worker_id
    if "denied" in fields:
        fields["denied"] = tuple(fields["denied"] or ())
    return WorkerReport(**fields)
