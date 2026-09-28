"""Remote Worker Birbs over a real TLS WebSocket, on loopback.

A real ``RemoteWorkerServer`` in a thread and a real ``RemoteClient``: pairing,
certificate pinning, a Worker Birb job with its model relayed to the main side,
checks run on the remote, the hard halt, resuming after a drop, and a flock
round with one remote ticket.
"""

from __future__ import annotations

import os
import re
import threading
import time

import pytest

from cobirb.config import Config
from cobirb.flock.charter import Charter, WorkerBrief
from cobirb.flock.supervisor import run_flock
from cobirb.policy import SessionGrants
from cobirb.remote import protocol
from cobirb.remote.client import NotPaired, RemoteClient, Untrusted
from cobirb.remote.pool import RemotePool
from cobirb.remote.runner import RemoteRun
from cobirb.remote.server import RemoteWorkerServer
from cobirb.remote.settings import RemoteSpec
from cobirb.remote.trust import TrustStore
from cobirb.typing.spi import ToolCall

pytestmark = pytest.mark.xdist_group("remote_loopback")


class _Server:
    def __init__(self, directory: str, **kwargs) -> None:
        self.said: list[str] = []
        self.server = RemoteWorkerServer("127.0.0.1", 0, directory=directory, say=self.said.append, **kwargs)
        ready = threading.Event()
        threading.Thread(target=self.server.serve_forever, args=(ready,), daemon=True).start()
        assert ready.wait(10)
        self.spec = RemoteSpec("Windows", f"https://127.0.0.1:{self.server.port}/api")

    def code(self, _spec=None) -> str:
        for _ in range(100):
            found = [m for m in (re.search(r"(\d{4}) (\d{4})", s) for s in self.said) if m]
            if found:
                return found[-1].group(1) + found[-1].group(2)
            time.sleep(0.05)
        raise AssertionError("the remote never printed a pairing code")

    def client(self, **kwargs) -> RemoteClient:
        kwargs.setdefault("ask_trust", lambda spec, fp: True)
        kwargs.setdefault("ask_code", self.code)
        return RemoteClient(self.spec, TrustStore(), **kwargs)


@pytest.fixture
def remote(tmp_path):
    server = _Server(str(tmp_path / "remote-worker"))
    yield server
    server.server.shutdown()


class _ScriptedModel:
    """The main session's model, as the remote worker's relay reaches it:
    write the file, report, then answer."""

    def __init__(self, content: str = "done") -> None:
        self.calls = 0
        self._content = content
        self._last: list[ToolCall] = []

    def name(self):
        return "scripted"

    def supports_streaming(self):
        return True

    def supports_tool_calling(self):
        return True

    def supports_vision(self):
        return False

    def context_window(self):
        return 32768

    def chat(self, system, context, tools=None, *, stream=False):
        self.calls += 1
        names = [t.name() for t in tools or []]
        if self.calls == 1:
            self._last, text = (
                [ToolCall("write_file", {"path": "a.txt", "content": self._content})],
                "writing",
            )
        elif self.calls == 2 and "report" in names:
            self._last = [ToolCall("report", {"tests_pass": True, "contract_kept": True, "missing": []})]
            text = "reporting"
        else:
            self._last, text = [], "Done."
        return iter([text]) if stream else text

    def parse_tool_calls(self, raw):
        return list(self._last)


_CHECK = """python3 -c "import sys; sys.exit(0 if open('a.txt').read()=='done' else 1)\""""


# --------------------------------------------------------------------------- #
# Trust and pairing
# --------------------------------------------------------------------------- #
def test_pairing_once_lets_later_connections_in_without_asking(remote):
    first = remote.client()
    assert first.connect()["os"]
    first.close()

    asked = []
    again = remote.client(
        ask_trust=lambda s, f: asked.append("trust"), ask_code=lambda s: asked.append("code")
    )
    again.connect()
    again.close()

    assert asked == []


def test_a_declined_certificate_is_refused(remote):
    with pytest.raises(Untrusted):
        remote.client(ask_trust=lambda spec, fp: False).connect()


def test_a_different_certificate_at_a_trusted_address_is_refused(remote):
    remote.client().connect().clear()
    TrustStore().trust(remote.spec.url, "00:11:22")  # as if the remote had been replaced

    with pytest.raises(Untrusted, match="different certificate"):
        remote.client().connect()


def test_wrong_codes_end_pairing(remote):
    with pytest.raises(NotPaired, match="too many wrong codes"):
        remote.client(ask_code=lambda spec: "00000000").connect()


def test_without_anyone_to_type_a_code_an_unpaired_remote_is_refused(remote):
    with pytest.raises(NotPaired):
        RemoteClient(remote.spec, TrustStore(), ask_trust=lambda s, f: True).connect()


# --------------------------------------------------------------------------- #
# A job
# --------------------------------------------------------------------------- #
def test_a_remote_worker_writes_its_file_and_its_checks_run_on_the_remote(remote, tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "a.txt").write_text("stub")
    client = remote.client(heartbeat_seconds=1)
    client.connect()
    shown = []

    class _Pane:
        def render_tool_call(self, tool, arguments, result):
            shown.append(tool)

    worker = WorkerBrief(id="win", brief="Write done into a.txt", writes=("a.txt",), accept=_CHECK)
    run = RemoteRun(
        client,
        worker,
        str(cwd),
        config=Config(),
        io=_Pane(),
        grants=SessionGrants(),
        provider=_ScriptedModel(),
    )
    report = run.execute()

    assert report.ok and report.accepted is True
    assert "write_file" in shown
    assert (cwd / "a.txt").read_text() == "done"  # brought home
    assert run.check(_CHECK).ok
    assert not run.check(_CHECK, overrides={"a.txt": "stub"}).ok  # the stub pass, on the remote
    assert run.check(_CHECK).ok  # and put back afterwards
    run.end()
    assert client.heartbeat() == protocol.STATE_IDLE
    client.close()


def test_a_finished_colleagues_file_pushed_mid_round_is_what_the_remote_runs_against(remote, tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    client = remote.client()
    client.connect()
    worker = WorkerBrief(id="win", brief="b", writes=("a.txt",), reads=("lib.txt",), accept=_CHECK)
    run = RemoteRun(
        client, worker, str(cwd), config=Config(), grants=SessionGrants(), provider=_ScriptedModel()
    )
    run.execute()

    (cwd / "lib.txt").write_text("from the colleague")
    run.push(["lib.txt"])

    assert run.check(
        """python3 -c "import sys; sys.exit(0 if open('lib.txt').read()=='from the colleague' else 1)\""""
    ).ok
    run.end()
    client.close()


def test_a_remote_that_hears_nothing_halts_its_job_and_discards_the_work(tmp_path):
    remote = _Server(str(tmp_path / "rw"), halt_after=2)
    client = remote.client(heartbeat_seconds=60)  # no heartbeat in time
    client.connect()
    order = {"worker": {"id": "t", "brief": "b", "writes": ["a.txt"]}, "model": {"mode": "relay"}}
    job = client.request("job_start", order=order, files={}, timeout=30)
    assert job["ok"]
    workspace = remote.server._job.workspace

    for _ in range(100):
        if remote.server._job is None:
            break
        time.sleep(0.1)

    assert remote.server._job is None and not os.path.exists(workspace)
    assert any("halting" in line for line in remote.said)
    client.close()
    remote.server.shutdown()


def test_a_dropped_connection_resumes_without_losing_or_repeating_events(remote):
    client = remote.client(heartbeat_seconds=1)
    client.connect()
    events = []
    client.on_event(events.append)
    order = {"worker": {"id": "t", "brief": "b", "writes": ["a.txt"]}, "model": {"mode": "relay"}}
    client.job_id = client.request("job_start", order=order, files={}, timeout=30)["job_id"]
    for _ in range(100):  # the relayed model call is the job's first event
        if events:
            break
        time.sleep(0.05)

    client._connection.close()  # the network drops
    for _ in range(100):
        if client._connected.is_set() and client.heartbeat() == protocol.STATE_YOURS:
            break
        time.sleep(0.1)

    assert client.heartbeat() == protocol.STATE_YOURS
    assert [e.get("kind") for e in events] == ["model"]
    client.request("job_end", timeout=30)
    client.close()


# --------------------------------------------------------------------------- #
# The pool and a flock round
# --------------------------------------------------------------------------- #
def test_a_busy_remote_makes_the_next_ticket_wait_until_it_is_free(remote):
    remote.client().connect()  # pair first; the pool never prompts
    pool = RemotePool([remote.spec], TrustStore(), poll_seconds=0.2).open()
    stop = threading.Event()
    first = pool.acquire("Windows", stop)
    waited, got = [], []

    thread = threading.Thread(
        target=lambda: got.append(pool.acquire("Windows", stop, on_wait=lambda: waited.append(1)))
    )
    thread.start()
    time.sleep(0.5)
    assert waited == [1] and got == []

    pool.release(first)
    thread.join(5)
    assert got == [first]
    assert pool.acquire("Darwin", stop) is None  # no remote of that OS at all
    pool.close()


def test_a_flock_round_runs_a_remote_ticket_and_judges_it_on_the_remote(remote, tmp_path, monkeypatch):
    from cobirb.remote import runner

    monkeypatch.setattr(runner, "build_for_role", lambda role, config: _ScriptedModel())
    remote.client().connect()
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "a.txt").write_text("stub")
    charter = Charter(
        objective="o",
        workers=(
            WorkerBrief(
                id="win", brief="Write done into a.txt", writes=("a.txt",), accept=_CHECK, runs_on="Windows"
            ),
        ),
    )
    pool = RemotePool([remote.spec], TrustStore()).open()
    events = []

    outcome = run_flock(
        charter, str(cwd), config=Config(), remotes=pool, on_event=lambda kind, payload: events.append(kind)
    )
    pool.close()

    report = outcome.reports[0]
    assert report.complete and report.rechecked
    assert outcome.reviews[0].stub is not None and outcome.reviews[0].stub.caught
    assert (cwd / "a.txt").read_text() == "done"
    assert events[:2] == ["started", "finished"]
