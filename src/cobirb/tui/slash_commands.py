"""What each ``/command`` does, and the table that finds it.

Every one of these lived on ``CoBirbApp`` as a ``_cmd_*`` method, which made
the app the home of fourteen unrelated features — exporting markdown, toggling
plan mode, starting a flock — on top of being the Textual application. They
were already written as plain functions of ``(app, argument)``: the dispatch
table held *unbound* methods and called them ``handler(self, argument)``, so
moving them here changes how they are stored, not how they work.

A command that needs the app says so by taking it. Nothing here is a method of
anything, nothing here holds state, and ``COMMANDS`` is the only export the
app needs.

The app keeps ``_dispatch_command`` itself: matching the first word, deciding
that an unknown ``/thing`` is prose rather than a typo, and expanding a custom
command are routing decisions about input, not the behaviour of any one
command.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

from textual.widgets import TabbedContent

from ..flock.supervisor import Canceller
from ..plugins.core import render
from ..runtime import commands as command_helpers
from ..runtime.custom_commands import describe_commands, discover_commands
from ..runtime.export import write_export
from ..session import turns_since_clear
from . import attachments
from .screens import MemoryCataloguesModal, RememberModal
from .widgets import PromptInput, StatusBar, TranscriptLog

if TYPE_CHECKING:
    from .app import CoBirbApp


def cmd_help(app: "CoBirbApp", argument: str) -> None:
    """The help screen. `/help <topic>` for one topic."""
    app.action_help(argument)


def cmd_model(app: "CoBirbApp", argument: str) -> None:
    """Pick a model from what your endpoint offers."""
    if argument:
        app.write_transcript(
            render.build_notice("Usage: /model — lists available models to choose from.")
        )
        return
    app._select_model_worker(auto=False)


def cmd_persona(app: "CoBirbApp", argument: str) -> None:
    """Pick a persona. `/persona <name>` switches directly."""
    # Bare /persona opens the picker, exactly like bare /model; /persona
    # <name> still switches directly, so anything scripted or recalled
    # from history keeps working.
    if argument:
        app._apply_persona(argument)
    else:
        app.pick_persona()


def cmd_context(app: "CoBirbApp", argument: str) -> None:
    """How much of the model's window this session is using.

    Worth surfacing rather than leaving in the log: on a local model the
    window is usually far smaller than people expect, and this is where
    they find that out before it degrades an answer.
    """
    if app.orchestrator is None:
        app.write_transcript(
            render.build_notice("No turns yet — the context budget is measured on the first one.")
        )
        return
    report = app.orchestrator.last_compaction
    app.write_transcript(
        render.build_notice(report.describe() if report else "Nothing sent to the model yet.")
    )


def cmd_undo(app: "CoBirbApp", argument: str) -> None:
    """Put back the files the last changing turn altered.

    Reports what it actually restored rather than saying "done": `shell`
    cannot declare what it writes, so anything a command did is outside
    this, and a user told "undone" who then finds otherwise is worse off
    than one told exactly which files came back.
    """
    checkpoints = getattr(app.orchestrator, "checkpoints", None)
    if checkpoints is None:
        app.write_transcript(
            render.build_notice("Undo is off for this session (\"checkpoints\": false).")
        )
        return
    app.write_transcript(render.build_notice(checkpoints.undo_last().describe()))


def cmd_diff(app: "CoBirbApp", argument: str) -> None:
    """Everything the agent has changed this session, as one diff.

    Built from the undo snapshots, not from git: it works in a directory
    that is not a repository, and it shows *the agent's* changes rather
    than conflating them with whatever the user had already edited.
    """
    checkpoints = getattr(app.orchestrator, "checkpoints", None)
    if checkpoints is None:
        app.write_transcript(
            render.build_notice("No change tracking this session (\"checkpoints\": false).")
        )
        return
    diff = checkpoints.session_diff()
    if not diff.strip():
        app.write_transcript(render.build_notice("No files have been changed this session."))
        return
    app.write_transcript(render.build_preview_panel("this session", diff))


def cmd_clear(app: "CoBirbApp", argument: str) -> None:
    """Start over from here: clear the screen, and the model's context with it.

    **Not a deletion.** The turns before this stay in the session file exactly
    as they were; what changes is where the conversation is read from. Clearing
    is a point in the history — the way committing an emptied file is a new
    commit rather than a rewrite of the ones before it — so the record of what
    happened survives for anyone auditing or debugging it, while the model and
    the screen both start again from here.

    Both halves matter together. Clearing the screen without clearing the
    context leaves the model answering from a conversation the user believes is
    gone; clearing the context without the screen leaves the user reading one
    the model cannot see.
    """
    app.query_one("#transcript", TranscriptLog).clear()
    session_data = getattr(getattr(app.orchestrator, "session", None), "session", None)
    if session_data is None:
        app.write_transcript(render.build_notice("Cleared — nothing had been said yet."))
        return
    dropped = len(turns_since_clear(session_data.turns))
    session_data.add_clear()
    app.write_transcript(
        render.build_notice(
            f"Cleared. The {dropped} turn(s) before this are still in the session file, "
            "but are no longer sent to the model. Your next message starts fresh."
        )
    )


def cmd_export(app: "CoBirbApp", argument: str) -> None:
    """Write this session out as markdown.

    Says plainly that the result is plaintext. The session stays
    encrypted; this is a copy the user asked for, and the whole reason to
    ask for one is to give it to someone.
    """
    manager = getattr(app.orchestrator, "session", None)
    session_data = getattr(manager, "session", None)
    if session_data is None or not session_data.turns:
        app.write_transcript(render.build_notice("Nothing to export yet."))
        return
    name = argument.strip() or f"cobirb-session-{time.strftime('%Y%m%d-%H%M%S')}.md"
    try:
        written = write_export(session_data, name)
    except OSError as exc:
        app.write_transcript(render.build_notice(f"Could not export — {exc}"))
        return
    app.write_transcript(
        render.build_notice(
            f"Exported {len(session_data.turns)} turn(s) to {written}. "
            "That file is plaintext; the session itself stays encrypted."
        )
    )


# --------------------------------------------------------------------------- #
# Memory catalogues
# --------------------------------------------------------------------------- #
def _ensure_public_catalogue(app: "CoBirbApp") -> None:
    """Create the always-present Public catalogue if it isn't there yet.

    "Lazily, on first use" is what ``memory.ensure_public_exists``
    promises, and this is the first use — both commands that show the
    catalogue list call it. Without this the promise was never kept by
    anything but the tests: a fresh install opened ``/memories`` on an
    empty list, and ``/remember`` offered nowhere to put the fact.

    A failure here is reported, not raised: an unwritable memories
    directory should cost the Public catalogue, never the command.
    """
    try:
        app.catalogues.ensure_public()
    except OSError as exc:  # noqa: BLE001 - reported, never fatal
        app.write_transcript(render.build_notice(f"Could not create the public catalogue — {exc}"))


def cmd_memories(app: "CoBirbApp", argument: str) -> None:
    """Load, create, rename or delete memory catalogues."""
    _ensure_public_catalogue(app)
    app.push_screen(MemoryCataloguesModal())


def cmd_remember(app: "CoBirbApp", argument: str) -> None:
    """Save a fact into a catalogue of the user's choosing.

    ``/remember <fact>``.

    Deliberately a plain slash command, not a model tool: a tool would
    either be unreachable for a model that can't call tools at all, or
    get called on every turn with no memory of having already asked —
    this fires exactly once, exactly when typed, regardless of what the
    model can do.
    """
    fact = argument.strip()
    if not fact:
        app.write_transcript(render.build_notice("Usage: /remember <fact to save>"))
        return
    _ensure_public_catalogue(app)
    app.push_screen(RememberModal(fact))


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def cmd_image(app: "CoBirbApp", argument: str) -> None:
    """Attach an image to your next message.

    ``/image <path> [message]``.

    With no trailing text the image is queued and the message you type
    and send next carries it, the way attaching a file works anywhere
    else. With trailing text, that text *is* the message and the turn
    goes now — because typing the path and the question together is what
    people reach for, and reading the whole line as a filename made that
    fail with "no such file", which blames the file for a parsing rule.
    """
    path, message = attachments.split_argument(argument, app.cwd)
    if not path:
        app.write_transcript(render.build_notice("Usage: /image <path> [message]"))
        return
    try:
        filename = app.attachments.queue(path, app.cwd)
    except attachments.AttachmentError as exc:
        app.write_transcript(render.build_notice(str(exc)))
        return

    model = getattr(app.orchestrator, "model", None)
    if model is not None and not model.supports_vision():
        app.write_transcript(
            render.build_notice(
                f"\U0001f4ce {filename} queued — the current model doesn't support vision, so "
                "this will be understood as text only. Switch with /model, or send anyway."
            )
        )
    elif not message:
        app.write_transcript(
            render.build_notice(f"\U0001f4ce {filename} queued — attach more with /image, or send your message.")
        )
    if message:
        app._send_prompt(message)


def cmd_plan(app: "CoBirbApp", argument: str) -> None:
    """Plan, act and validate as three phases. `/plan on|off` toggles it."""
    app.plan_mode, message = command_helpers.apply_plan_toggle(argument, app.plan_mode)
    app.query_one(StatusBar).plan_mode = app.plan_mode
    app.write_transcript(render.build_notice(message))


def cmd_flock(app: "CoBirbApp", argument: str) -> None:
    """Divide a piece of work between several agents.

    ``/flock <objective>``.

    Refused while an ordinary turn is running, and while another flock is:
    both would put two agents into the same working tree with no partition
    between them, which is the one thing the whole design exists to
    prevent.
    """
    if not argument.strip():
        # With a charter already proposed, "/flock" on its own means "get on
        # with that one" far more often than it means "what are the arguments
        # again?" — so it does what /charter does. Usage is still the answer
        # when there is nothing waiting.
        if cmd_charter(app, "", quiet=True):
            return
        app.write_transcript(
            render.build_notice(
                "Usage: /flock <objective>, e.g. /flock add CSV export to the reporting "
                "tool. Brainy Birb plans it and you approve the charter before anything "
                "runs. See /help flock."
            )
        )
        return
    if app._turn_in_progress:
        app.write_transcript(
            render.build_notice("Wait for the current turn to finish before starting a flock.")
        )
        return
    if app._flock_stop is not None:
        app.write_transcript(render.build_notice("A flock is already running."))
        return
    app.query_one(TabbedContent).active = "flock"
    app._turn_in_progress = True
    app.query_one("#prompt-input", PromptInput).disabled = True
    app._flock_stop = threading.Event()
    app._flock_canceller = Canceller()
    app._planning_calls = 0
    app.charter_in_hand = False
    app.set_activity("Brainy Birb is planning…")
    app._run_flock(argument.strip())


def cmd_charter(app: "CoBirbApp", argument: str, *, quiet: bool = False) -> bool:
    """Review the charter Brainy Birb last proposed, and run it if you approve.

    The manual way back to the approval dialog. A charter normally puts itself
    in front of you the moment it is accepted, but the dialog can be dismissed,
    or proposed during a flock that was already running — and without this
    there is no way to ask for it again, which leaves a perfectly good charter
    sitting in memory with nothing able to reach it.

    ``quiet`` suppresses the "nothing to show" notice, for ``/flock`` with no
    argument — which falls through to its own usage message instead. Returns
    whether there was a charter to offer.
    """
    charter = getattr(app, "_pending_charter", None) or _last_proposed_charter(app)
    if charter is None:
        if not quiet:
            app.write_transcript(
                render.build_notice(
                    "No charter has been proposed yet. '/flock <objective>' starts one."
                )
            )
        return False
    if app._turn_in_progress or app._flock_stop is not None:
        if not quiet:
            app.write_transcript(
                render.build_notice("Wait for the current turn to finish first.")
            )
        return True
    app._pending_charter = charter
    app.offer_pending_charter()
    return True


def _last_proposed_charter(app: "CoBirbApp"):
    """The charter still held by the orchestrator's `propose_charter` tool.

    Separate from ``app._pending_charter`` because the two go stale at
    different moments: the app clears its copy once it has put the dialog up,
    while the tool keeps the last charter it accepted until the next planning
    turn resets it. That is what makes a dismissed dialog recoverable.
    """
    from ..flock.brainy import PROPOSE_CHARTER

    tools = getattr(app.orchestrator, "tools", None) or {}
    return getattr(tools.get(PROPOSE_CHARTER), "charter", None)


def cmd_commands(app: "CoBirbApp", argument: str) -> None:
    """List the prompt files that are available as commands here."""
    app.write_transcript(render.build_notice(describe_commands(discover_commands(app.cwd))))


# The built-in commands, by the exact first word that invokes them. Anything
# else starting with "/" is tried as a custom command and then sent to the
# model unchanged — see CoBirbApp._dispatch_command.
COMMANDS: "dict[str, Callable[[CoBirbApp, str], None]]" = {
    "/help": cmd_help,
    "/model": cmd_model,
    "/persona": cmd_persona,
    "/plan": cmd_plan,
    "/context": cmd_context,
    "/clear": cmd_clear,
    "/undo": cmd_undo,
    "/export": cmd_export,
    "/diff": cmd_diff,
    "/commands": cmd_commands,
    "/flock": cmd_flock,
    "/charter": cmd_charter,
    "/memories": cmd_memories,
    "/remember": cmd_remember,
    "/image": cmd_image,
}
