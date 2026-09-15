"""Tests for the Flock tab: panes, live progress, and interrupting a run.

The two that matter are the ones with teeth. A charter must not be approvable
by a dialog that was dismissed rather than answered — it is the only place a
person sees what the Worker Birbs will be allowed to touch. And ctrl+c has to
ask, because a flock is several agents deep in somebody's working tree.
"""
from __future__ import annotations

import threading

import pytest
from textual.widgets import Input, TabbedContent

from cobirb.flock.charter import parse_charter
from cobirb.flock.run import FlockRun
from cobirb.flock.review import RedCheck, Review
from cobirb.flock.supervisor import Canceller, FlockOutcome
from cobirb.flock.worker import WorkerReport
from cobirb.tui.app import CoBirbApp
from cobirb.tui.widgets import PromptInput
from cobirb.tui.flock_bridge import TuiAsker, WorkerPaneIO
from cobirb.tui.panes import FlockPane, WorkerPane
from cobirb.tui.screens import ConfirmModal
from cobirb.runtime.personas import load_persona

_CHARTER = parse_charter("""
objective = "two things"
[[workers]]
id = "a"
writes = ["a.py"]
reads = ["types.py"]
brief = "Implement a."
[[workers]]
id = "b"
writes = ["b.py"]
brief = "Implement b."
""")


@pytest.fixture(autouse=True)
def _skip_startup_model_check(monkeypatch):
    """``CoBirbApp`` validates its model against a live endpoint at startup,
    which hits a real network port and, when nothing resolves, opens a picker
    modal over everything these tests want to look at."""
    monkeypatch.setattr(CoBirbApp, "_select_model_worker", lambda self, **kwargs: None)


def _text(app, selector: str) -> str:
    """The visible text of a Static, matching test_tui.py's own helper."""
    return " ".join(str(node.content) for node in app.screen.query(selector))


def _make_app(**kwargs) -> CoBirbApp:
    return CoBirbApp(
        persona=load_persona(None), system="", allow_overrides={},
        session_path=None, password=None, cwd=".", **kwargs
    )


async def _flock_tab(pilot, app):
    """Show the Flock tab and wait until it is actually laid out.

    The initial pause matters: ``TabbedContent(initial="current")`` sets its
    own active tab while mounting, so an assignment made before that lands is
    silently overwritten. Waiting for a real height afterwards is what makes
    these tests about a tab a person can *see*, rather than about widgets that
    merely exist in the tree.
    """
    await pilot.pause()
    app.query_one(TabbedContent).active = "flock"
    for _ in range(60):
        pane = app.query_one(FlockPane)
        if pane.region.height > 0:
            return pane
        await pilot.pause()
    raise AssertionError("the Flock tab never became visible")


# --------------------------------------------------------------------------- #
# The panes
# --------------------------------------------------------------------------- #
async def test_the_flock_tab_starts_empty_and_says_how_to_use_it():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)

        assert not app.query(WorkerPane)
        assert "/flock" in _text(app, "#flock-status")
        assert pane.region.height > 0  # actually on screen, not merely mounted


async def test_approving_a_charter_lays_out_a_pane_per_worker():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)

        await pane.begin(_CHARTER)
        await pilot.pause()

        panes = list(app.query(WorkerPane))
        assert [p._worker_id for p in panes] == ["a", "b"]
        # Side by side and both visible — a pane each is the whole point.
        assert all(p.region.width > 0 and p.region.height > 0 for p in panes)
        assert panes[0].region.x != panes[1].region.x


async def test_a_pane_shows_what_its_worker_is_allowed_to_touch():
    """The scope is the whole of a worker's isolation, and it stays on screen
    rather than scrolling away with the log."""
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        scope = _text(app, "#worker-a .worker-scope")

        assert "a.py" in scope
        assert "types.py" in scope


async def test_a_new_flock_replaces_the_last_ones_panes():
    """A pane left over from the last flock, showing a ticket that no longer
    exists, is worse than an empty tab."""
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        await pane.begin(parse_charter(
            'objective = "x"\n[[workers]]\nid = "z"\nwrites = ["z.py"]\nbrief = "go"\n'
        ))
        await pilot.pause()

        assert [p._worker_id for p in app.query(WorkerPane)] == ["z"]


# --------------------------------------------------------------------------- #
# Live progress
# --------------------------------------------------------------------------- #
async def test_a_worker_starting_and_finishing_moves_its_pane_through_states():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        app._apply_flock_event("started", _CHARTER.workers[0])
        await pilot.pause()
        running = _text(app, "#worker-a .worker-title")

        app._apply_flock_event(
            "finished", WorkerReport(worker_id="a", ok=True, accepted=True, summary="did it")
        )
        await pilot.pause()
        done = _text(app, "#worker-a .worker-title")

        assert "running" in running
        assert "done" in done


async def test_a_worker_that_failed_its_check_shows_as_failed():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        app._apply_flock_event("finished", WorkerReport(worker_id="a", ok=True, accepted=False))
        await pilot.pause()

        title = _text(app, "#worker-a .worker-title")
        assert "failed" in title


async def test_an_unclean_review_demotes_a_worker_that_had_passed():
    """An acceptance check passing is not the same as the work standing up to
    review — that is the whole point of reviewing."""
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        app._apply_flock_event("finished", WorkerReport(worker_id="a", ok=True, accepted=True))
        await pilot.pause()
        app._apply_flock_event(
            "reviewed",
            Review(worker_id="a", stub=RedCheck(label="stub reversion", caught=False)),
        )
        await pilot.pause()

        title = _text(app, "#worker-a .worker-title")
        assert "flagged" in title


async def test_an_event_for_a_worker_with_no_pane_is_not_a_crash():
    """Panes are laid out from the charter; an event naming something else
    means the display and the run disagreed, which must not take the app
    down mid-flock."""
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)

        app._apply_flock_event("finished", WorkerReport(worker_id="ghost", ok=True))
        await pilot.pause()


# --------------------------------------------------------------------------- #
# The charter approval
# --------------------------------------------------------------------------- #
async def test_a_confirmation_dialog_dismissed_with_escape_means_no():
    """The charter approval is the only place a person sees what the Worker
    Birbs will be allowed to touch. A dialog that could be dismissed into a
    *yes* is the one bug worth avoiding above all others here."""
    app = _make_app()
    answers = []
    async with app.run_test() as pilot:
        await pilot.pause()
        # Through a Textual *worker*, which is what the real flock runs on.
        # A plain thread cannot do this: push_screen_wait raises NoActiveWorker
        # outside a worker context, TuiAsker catches it and fails closed — so a
        # test driven from a raw thread would pass for the wrong reason,
        # answering False without the keypress mattering at all.
        app.run_worker(
            lambda: answers.append(TuiAsker(app).confirm("Approve this charter?")),
            thread=True,
        )
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("escape")
        await _settle(pilot, lambda: bool(answers))

    assert answers == [False]


async def test_answering_yes_approves():
    app = _make_app()
    answers = []
    async with app.run_test() as pilot:
        await pilot.pause()
        # Through a Textual *worker*, which is what the real flock runs on.
        # A plain thread cannot do this: push_screen_wait raises NoActiveWorker
        # outside a worker context, TuiAsker catches it and fails closed — so a
        # test driven from a raw thread would pass for the wrong reason,
        # answering False without the keypress mattering at all.
        app.run_worker(
            lambda: answers.append(TuiAsker(app).confirm("Approve this charter?")),
            thread=True,
        )
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _settle(pilot, lambda: bool(answers))

    assert answers == [True]


def test_an_asker_with_no_app_to_ask_refuses():
    """Fails closed, like every other unanswerable question in CoBirb."""
    class _Dead:
        ui_thread_id = -1

        def call_from_thread(self, *args, **kwargs):
            raise RuntimeError("the app is shutting down")

    assert TuiAsker(_Dead()).confirm("Approve this charter?") is False


def test_a_worker_pane_io_still_refuses_every_tool_call():
    """It is a HeadlessIO that happens to draw. Watching a worker work and
    being asked to approve its calls are different things, and only the first
    is wanted here."""
    io = WorkerPaneIO(None, "a")

    assert io.confirm("write_file", {"path": "anything"}) == "deny"


# --------------------------------------------------------------------------- #
# Interrupting
# --------------------------------------------------------------------------- #
async def test_ctrl_c_during_a_flock_asks_before_stopping_anything():
    """A flock is several agents deep in a working tree. An accidental ctrl+c
    that silently abandoned a run halfway would leave it in a state nobody
    chose."""
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        app._flock_stop = threading.Event()

        app.action_cancel_turn()
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))

        assert not app._flock_stop.is_set()  # nothing stopped just by asking


async def test_confirming_the_interrupt_stops_further_workers():
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        stop = app._flock_stop = threading.Event()

        app.action_cancel_turn()
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _settle(pilot, stop.is_set)

        assert stop.is_set()


async def test_declining_the_interrupt_leaves_the_flock_running():
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        stop = app._flock_stop = threading.Event()

        app.action_cancel_turn()
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("n")
        await _settle(pilot, lambda: not isinstance(app.screen, ConfirmModal))

        assert not stop.is_set()


async def test_quitting_mid_flock_stops_it_rather_than_waiting_it_out():
    """Without this, quitting waits for every remaining Worker Birb to run."""
    app = _make_app()
    async with app.run_test() as pilot:
        stop = app._flock_stop = threading.Event()

        await app.action_quit()

        assert stop.is_set()


# --------------------------------------------------------------------------- #
# Force-stopping — the second Ctrl+C, for a worker blocked on the model.
#
# The graceful stop above only keeps *new* workers from starting; it cannot
# reach one already blocked waiting on the model, because that thread is not
# checking anything. This is the escalation the user actually needed: "the
# worker is stuck waiting for Ollama... is there a way to force stop it?"
# --------------------------------------------------------------------------- #
async def test_a_second_ctrl_c_once_already_stopping_offers_the_force_stop():
    """The first Ctrl+C is graceful and asks its own question; a second one,
    once already stopping, has to ask a *different* question rather than
    showing the same dialog again or doing nothing."""
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        app._flock_stop = threading.Event()
        app._flock_stop.set()  # already stopping, from a first Ctrl+C
        app._flock_canceller = Canceller()

        app.action_cancel_turn()
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))

        assert "Force-stop" in _text(app, "#confirm-question")


async def test_confirming_the_force_stop_cancels_every_live_worker():
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        app._flock_stop = threading.Event()
        app._flock_stop.set()
        canceller = app._flock_canceller = Canceller()

        class _FakeOrchestrator:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        live = _FakeOrchestrator()
        canceller.register(live)

        app.action_cancel_turn()
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _settle(pilot, lambda: canceller.forced)

        assert live.cancelled  # the actual model connection was told to drop


async def test_declining_the_force_stop_leaves_workers_running():
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        app._flock_stop = threading.Event()
        app._flock_stop.set()
        canceller = app._flock_canceller = Canceller()

        app.action_cancel_turn()
        await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("n")
        await _settle(pilot, lambda: not isinstance(app.screen, ConfirmModal))

        assert not canceller.forced


async def test_quitting_mid_flock_force_stops_rather_than_asking():
    """Quitting is unambiguous — there is no dialog to answer on the way out
    — so it goes straight to the hard stop rather than leaving a worker
    blocked on the model to hold the whole app open."""
    app = _make_app()
    async with app.run_test() as pilot:
        app._flock_stop = threading.Event()
        canceller = app._flock_canceller = Canceller()

        class _FakeOrchestrator:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        live = _FakeOrchestrator()
        canceller.register(live)

        await app.action_quit()

        assert live.cancelled


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
async def test_flock_with_no_objective_explains_itself():
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()

        app._dispatch_command("/flock")
        await pilot.pause()

        assert app._flock_stop is None  # nothing started


async def test_flock_is_refused_while_another_one_is_running():
    """Two flocks in one working tree is two sets of agents with no partition
    between them — the one thing the whole design exists to prevent."""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        existing = app._flock_stop = threading.Event()

        app._dispatch_command("/flock do something else")
        await pilot.pause()

        assert app._flock_stop is existing


async def test_the_finished_report_re_enables_the_prompt():
    app = _make_app()
    async with app.run_test() as pilot:
        await _flock_tab(pilot, app)
        app._flock_stop = threading.Event()
        app._turn_in_progress = True
        app.query_one("#prompt-input", PromptInput).disabled = True

        app._on_flock_finished(
            FlockRun(charter=_CHARTER, outcome=FlockOutcome(charter=_CHARTER), report="done")
        )
        await pilot.pause()

        assert not app.query_one("#prompt-input", PromptInput).disabled
        assert app._flock_stop is None


async def _settle(pilot, predicate, tries: int = 60) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError("condition never became true")


# --------------------------------------------------------------------------- #
# The activity roll-up, read off the panes themselves
# --------------------------------------------------------------------------- #
async def test_the_activity_line_rolls_up_every_worker_not_just_the_last_event():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        app._apply_flock_event("started", _CHARTER.workers[0])
        await pilot.pause()

        summary = pane.activity_summary()
        assert "a running" in summary
        # The others are still listed, waiting — during a fan-out the question
        # is "is anything still going", which needs all of them visible.
        assert summary.count("·") == len(_CHARTER.workers) - 1


async def test_a_worker_with_no_pane_left_is_not_in_the_roll_up():
    """The app used to keep its own dict of worker states alongside the panes,
    so an event for a worker whose pane had gone still counted in the activity
    line. Read off the panes, a ghost simply isn't there."""
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        app._apply_flock_event("finished", WorkerReport(worker_id="ghost", ok=True))
        await pilot.pause()

        assert "ghost" not in pane.activity_summary()


async def test_the_roll_up_is_empty_before_a_flock_lays_out_any_panes():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        assert pane.activity_summary() == ""


async def test_a_pane_remembers_the_state_it_is_showing():
    app = _make_app()
    async with app.run_test() as pilot:
        pane = await _flock_tab(pilot, app)
        await pane.begin(_CHARTER)
        await pilot.pause()

        worker = pane.pane("a")
        assert worker.state == "waiting"
        worker.set_state("running")
        assert worker.state == "running"
