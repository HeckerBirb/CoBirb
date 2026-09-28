"""Remote Worker Birbs: OS names, config entries, trust, and what planning makes of them."""
from __future__ import annotations

import pytest

from cobirb.flock import run as flock_run
from cobirb.flock.brainy import AddWorkerTool, CharterDesk
from cobirb.flock.charter import Charter, CharterError, WorkerBrief, runs_on_os
from cobirb.flock.run import Asker
from cobirb.flock.stages import UNCHECKABLE, TicketSpec, check_tickets, parse_tickets
from cobirb.flock.worker import WorkerReport
from cobirb.remote import settings
from cobirb.remote.osnames import canonical_os, local_os
from cobirb.remote.server import summarise
from cobirb.remote.settings import parse_entry
from cobirb.remote.trust import IssuedTokens, TrustStore


@pytest.mark.parametrize("name, family", [
    ("Windows", "Windows"), ("win32", "Windows"), ("wINdOwS", "Windows"),
    ("Linux", "Linux"), ("linux", "Linux"),
    ("Darwin", "Darwin"), ("darwin", "Darwin"),
    ("FreeBSD", "FreeBSD"), ("freebsd14", "FreeBSD"), ("openbsd7", "OpenBSD"), ("netbsd10", "NetBSD"),
    ("win", None), ("cygwin", None), ("", None), (None, None),
])
def test_an_os_name_from_either_python_call_means_its_family(name, family):
    assert canonical_os(name) == family


@pytest.mark.parametrize("entry, problem", [
    ({"remote_os": "win32", "remote_url": "https://10.0.0.5:8443/api"}, ""),
    ({"remote_os": "Windows", "remote_url": "https://h/api", "run_llms_locally": True,
      "openai_endpoint": "http://gpu:11434", "model_name": "m"}, ""),
    ({"remote_os": "plan9", "remote_url": "https://h/api"}, "not an OS name"),
    ({"remote_os": "Windows", "remote_url": "http://h/api"}, "https://"),
    ({"remote_os": "Windows", "remote_url": "https://h", "run_llms_locally": True}, "openai_endpoint"),
    ({"remote_os": "Windows", "remote_url": "https://h", "surprise": 1}, "unknown key"),
])
def test_a_config_entry_is_validated(entry, problem):
    spec, why = parse_entry(entry)

    assert (spec is None) == bool(problem) and problem in why


def test_a_remote_with_this_machines_os_is_ignored_and_said(monkeypatch):
    monkeypatch.setattr(settings, "local_os", lambda: "Linux")

    class _Config:
        def get(self, key):
            return [{"remote_os": "linux", "remote_url": "https://a/api"},
                    {"remote_os": "win32", "remote_url": "https://b/api"}]

    remotes, problems = settings.configured(_Config())

    assert [r.os for r in remotes] == ["Windows"]
    assert "ignored" in problems[0]


def test_a_pairing_lapses_after_thirty_idle_days_and_use_renews_it(tmp_path):
    clock = [1_000_000.0]
    store = TrustStore(str(tmp_path / "remotes.json"), now=lambda: clock[0])
    store.trust("https://h/api", "AA:BB")
    store.paired("https://h/api", "tok")

    clock[0] += 29 * 86400
    assert store.token("https://h/api") == "tok"
    store.used("https://h/api")
    clock[0] += 29 * 86400
    assert store.token("https://h/api") == "tok"  # renewed by the use
    clock[0] += 2 * 86400
    assert store.token("https://h/api") == ""  # 31 idle days


def test_trusting_a_new_certificate_drops_the_old_pairing(tmp_path):
    store = TrustStore(str(tmp_path / "remotes.json"))
    store.trust("https://h/api", "AA")
    store.paired("https://h/api", "tok")

    store.trust("https://h/api", "BB")

    assert store.token("https://h/api") == "" and store.fingerprint("https://h/api") == "BB"


def test_the_remote_keeps_only_hashes_and_lets_idle_tokens_lapse(tmp_path):
    clock = [0.0]
    tokens = IssuedTokens(str(tmp_path / "tokens.json"), now=lambda: clock[0])
    token = tokens.issue()

    assert token not in (tmp_path / "tokens.json").read_text()
    assert tokens.valid(token) and not tokens.valid("forged")
    clock[0] += 31 * 86400
    assert not tokens.valid(token)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
_WIN = "### ticket: win\n- writes: cap.c, test_cap.c\n- tests: test_cap.c\n- accept: {accept}\n- runs on: {os}\n"


def test_runs_on_is_read_as_its_family_and_kept_in_the_block():
    ticket = parse_tickets(_WIN.format(accept="true", os="win32"))[0]

    assert ticket.runs_on == "Windows"
    assert parse_tickets(ticket.block())[0].runs_on == "Windows"


def test_an_unknown_runs_on_is_refused():
    assert "not an OS name" in check_tickets(parse_tickets(_WIN.format(accept="true", os="Plan9")))


def test_a_remote_tickets_check_is_looked_up_on_its_remote_the_windows_way():
    asked = []

    def which_for(os_family):
        return (lambda program: asked.append(program) or program == "cl"), True

    accept = r"cl /W4 cap.c test_cap.c && .\test_cap.exe"
    assert check_tickets(parse_tickets(_WIN.format(accept=accept, os="Windows")), which_for) == ""
    assert asked == ["cl"]  # `.\test_cap.exe` is a path the command builds, not looked up
    assert "not a program installed" in check_tickets(
        parse_tickets(_WIN.format(accept="gcc cap.c", os="Windows")), which_for)


def test_a_ticket_for_an_os_with_no_remote_is_not_refused_over_its_programs():
    tickets = parse_tickets(_WIN.format(accept="xcrun clang cap.c", os="Darwin"))

    assert check_tickets(tickets, lambda os_family: UNCHECKABLE) == ""


@pytest.mark.parametrize("answer, static, stops", [(True, True, False), (False, False, True)])
def test_the_user_decides_about_tickets_no_remote_can_run(answer, static, stops):
    tickets = [TicketSpec(id="mac", writes=("a.m",), tests=("t.m",), accept="xcrun", runs_on="Darwin"),
               TicketSpec(id="here", writes=("b.py",), tests=("t.py",), accept="true")]
    questions = []

    kept, stopping = flock_run._static_or_stop(
        tickets, None, Asker(confirm=lambda q, d="": questions.append(q) or answer))

    assert "'mac'" in questions[0] and "Darwin" in questions[0]
    assert stopping is stops
    assert kept[0].static is static and (kept[0].runs_on == "") is static
    assert kept[1].static is False


def test_a_static_ticket_is_done_when_written_and_says_it_was_not_verified():
    report = WorkerReport(worker_id="mac", ok=True, static=True)

    assert report.complete
    assert "not verified" in report.describe()


def test_the_charter_says_where_each_worker_runs():
    charter = Charter(objective="o", workers=(
        WorkerBrief(id="win", brief="b", writes=("cap.c",), accept="cl cap.c", runs_on="Windows"),
        WorkerBrief(id="mac", brief="b", writes=("a.m",), static=True),
    ))

    text = charter.describe()

    assert "runs on Windows" in text and "files are sent there" in text
    assert "static" in text


@pytest.mark.parametrize("value, expected", [
    ("windows", "Windows"), ("win32", "Windows"), (None, ""), ("None", ""), ("", ""),
    (local_os(), ""),
])
def test_a_tickets_runs_on_is_its_family_and_empty_for_here(value, expected):
    assert runs_on_os(value, "t") == expected


def test_a_runs_on_naming_no_known_os_is_refused():
    with pytest.raises(CharterError):
        runs_on_os("Plan9", "t")


def test_a_ticket_added_in_chat_can_run_on_another_os(tmp_path):
    desk = CharterDesk(str(tmp_path))
    AddWorkerTool(desk).execute({"id": "win", "brief": "b", "writes": ["cap.c"],
                                 "accept": "cl cap.c", "runs_on": "win32", "reads": "None"})

    worker = desk.draft.seal("o").workers[0]

    assert worker.runs_on == "Windows"
    assert worker.reads == ()


class _QuietBrainy:
    def name(self):
        return "quiet"

    def chat(self, system, context, tools=None, *, stream=False):
        return "Done."

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return True

    def supports_streaming(self):
        return False


def _orchestrator_for(monkeypatch, tmp_path):
    from cobirb.runtime import wiring

    monkeypatch.setattr(wiring, "build_model", lambda *a, **k: _QuietBrainy())
    return wiring.build_orchestrator(str(tmp_path), {})


@pytest.mark.parametrize("answer, stopped_at, ran", [(False, "remote", []), (True, "", [("win", True, "")])])
def test_a_charter_from_chat_puts_an_os_with_no_remote_to_the_user(monkeypatch, tmp_path, answer, stopped_at, ran):
    from cobirb.flock import supervisor

    monkeypatch.setattr("cobirb.flock.run.missing_models", lambda *a, **k: "")
    monkeypatch.setattr("cobirb.flock.run.DEFAULT_PLANNING", "single")
    started = []

    def run_worker(worker, cwd, **kwargs):
        started.append((worker.id, worker.static, worker.accept))
        return WorkerReport(worker_id=worker.id, ok=True, accepted=None)

    monkeypatch.setattr(supervisor, "run_worker", run_worker)
    charter = Charter(objective="o", workers=(
        WorkerBrief(id="win", brief="b", writes=("cap.c",), accept="cl cap.c", runs_on="Windows"),))
    questions = []

    def confirm(question, detail=""):
        questions.append(question)
        return answer if "Windows" in question else question.startswith("Approve")

    run = flock_run.run_flock_session(
        _orchestrator_for(monkeypatch, tmp_path), "o", str(tmp_path),
        ask=Asker(confirm=confirm), charter=charter, probe=False)

    assert "Windows" in questions[0]
    assert run.stopped_at == stopped_at
    assert started == ran


@pytest.mark.parametrize("message, line", [
    ({"type": "heartbeat", "id": 1}, "heartbeat"),
    ({"type": "which", "programs": ["cl", "link"]}, "which cl link"),
    ({"type": "job_start", "order": {"worker": {"id": "win"}}}, "job_start: ticket 'win'"),
    ({"type": "event", "event": {"kind": "tool_call", "tool": "shell", "arguments": {"command": "cl a.c"}}},
     "tool_call shell: cl a.c"),
    ({"type": "event", "event": {"kind": "done"}}, "done"),
    ({"type": "reply", "ok": False, "error": "busy"}, "reply: failed busy"),
    ({"type": "model_chunk", "text": "x"}, None),
])
def test_verbose_says_what_each_message_is_in_one_line(message, line):
    assert summarise(message) == line


def test_verbose_lines_are_bounded():
    line = summarise({"type": "run", "command": "x" * 500})

    assert len(line) <= 100 and line.endswith("…")
