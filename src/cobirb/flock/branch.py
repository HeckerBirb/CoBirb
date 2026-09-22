"""Where a flock lives in a session, and how to find it again.

Engaging flock mode mints a **GUID** and writes it into two places: the main
session, where the conversation branched, and the head of a new flock session
file in the same folder. The pair is what makes the history legible months
later — you can see where the conversation handed off, follow the token to
read what the Worker Birbs actually did, and see where it came back.

**One engagement is one flock session. A new round is not a new session.** A
second round continues the active one, appended like any other turn, so a
flock that took three rounds reads as one continuous piece of work rather than
three disconnected files. Engage flock mode again later and a fresh token and a
fresh file are minted.

The file format is exactly the session format — same encryption, same schema,
same reader. A flock session is a session that happens to carry a ``flock``
token and to have been written by agents rather than typed by a person.
"""
from __future__ import annotations

import logging
import os
import time
import uuid

from ..session import Session, SessionManager, Turn

logger = logging.getLogger("cobirb")

# Turn roles that mark the two ends of a branch in the main session. Recorded
# as turns rather than as session fields because a session can branch more than
# once, and *when* it branched is part of the answer.
ROLE_ENGAGED = "flock_engaged"
ROLE_RETURNED = "flock_returned"


def mint() -> str:
    """A token for one flock engagement."""
    return str(uuid.uuid4())


def flock_path(main_path: str, token: str) -> str:
    """Where this flock's session file goes: beside the main one.

    The same folder deliberately. A flock session is a session, and putting it
    somewhere else would mean the Sessions tab lists half a history.
    """
    directory = os.path.dirname(os.path.abspath(main_path))
    stem = os.path.splitext(os.path.basename(main_path))[0]
    return os.path.join(directory, f"{stem}-flock-{token[:8]}.json")


def engage(session: Session, objective: str, token: str | None = None) -> str:
    """Mark in the main session that the conversation branched, and say where to.

    Returns the token, which the caller carries into the flock session. The
    objective is recorded alongside it so the main transcript still reads as a
    conversation — "here is where we handed this off, and what for" — without
    needing the other file open.
    """
    token = token or mint()
    session.add(
        Turn(
            role=ROLE_ENGAGED,
            content=f"Flock mode engaged for: {objective.strip()}",
            tool_use=[{"name": "flock", "arguments": {"token": token}}],
        )
    )
    logger.info("flock %s engaged", token)
    return token


def rejoin(session: Session, token: str, report: str) -> None:
    """Mark where the conversation came back, and what it came back with.

    The report lands in the *main* session as well as the flock one, because
    the outcome is part of the conversation the user is having, while the
    worker transcripts are detail they can go and read if they want to.
    """
    session.add(
        Turn(
            role=ROLE_RETURNED,
            content=report,
            tool_use=[{"name": "flock", "arguments": {"token": token}}],
        )
    )
    logger.info("flock %s returned", token)


def open_flock_session(
    main: SessionManager, token: str, objective: str, password: str | None
) -> SessionManager | None:
    """Create the flock's own session file beside the main one.

    Returns ``None`` when the main run is not persisting anything — an
    unencrypted, unsaved session has nowhere to put a branch, and inventing a
    file for one would be writing to disk on behalf of someone who asked for
    nothing to be written. Traceability is worth a lot; it is not worth
    breaking the promise that an ephemeral session leaves no trace.
    """
    if main is None or not getattr(main, "path", None):
        return None
    path = flock_path(main.path, token)
    manager = SessionManager.create(
        path, main.crypto, main.working_dir, password
    )
    manager.session.flock = token
    manager.session.add(
        Turn(
            role=ROLE_ENGAGED,
            content=(
                f"Flock {token}\n"
                f"Branched from {os.path.basename(main.path)} at "
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.\n"
                f"Objective: {objective.strip()}"
            ),
            tool_use=[{"name": "flock", "arguments": {"token": token}}],
        )
    )
    return manager


def describe_branches(session: Session) -> str:
    """Every flock this session has engaged, for ``/context`` and the log."""
    engagements = [
        turn for turn in session.turns if turn.role in (ROLE_ENGAGED, ROLE_RETURNED)
    ]
    if not engagements:
        return "No flock has been engaged in this session."
    lines = []
    for turn in engagements:
        token = (turn.tool_use or [{}])[0].get("arguments", {}).get("token", "?")
        verb = "engaged" if turn.role == ROLE_ENGAGED else "returned"
        lines.append(f"  {token[:8]} {verb}")
    return "Flock activity in this session:\n" + "\n".join(lines)
