"""Working out which session file to use, and which password unlocks it.

Also the small pieces of text a session run produces on the way in and out:
the prompt for a password, the reason a file could not be opened, and the
command that reopens it.
"""
from __future__ import annotations

import os
import time
from typing import Any

from .. import session as session_module


def abbreviate_home(path: str) -> str:
    """``/home/you/.cobirb/x`` as ``~/.cobirb/x``, for text a user might type
    back. Paths outside the home directory are returned unchanged."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def read_password() -> str:
    """Read a password from stdin without echoing it."""
    import getpass

    return getpass.getpass("CoBirb password: ")


def sessions_dir_display() -> str:
    """``~/.cobirb/sessions`` with the home directory abbreviated, for help
    text and the resume hint — the literal path is long and the ``~`` form is
    what a user would type back."""
    return abbreviate_home(session_module.default_sessions_dir())


class NoSessionToContinue(Exception):
    """``--continue`` was asked for and there is nothing to continue."""


def most_recent_session() -> str | None:
    """The session touched most recently, or ``None`` if there are none.

    ``discover_sessions`` already sorts by modification time, so "the one I
    was just in" is the first entry — the information was always there, it
    simply had no command in front of it.
    """
    found = session_module.discover_sessions(session_module.default_sessions_dir())
    return found[0].path if found else None


def resolve_session(
    session_arg: str | None, password_arg: Any, *, continue_last: bool = False
) -> tuple[str | None, str | None]:
    """Work out which session file to use and which password unlocks it.

    Returns ``(session_path, password)``, both ``None`` when this run isn't a
    session at all.

    ``--password``/``-w`` on its own is not inert: asking for a password is
    asking for an encrypted session, so one is created under
    ``~/.cobirb/sessions`` named for the current time. Reading the password
    only when ``--session`` also named a path would make ``cobirb -w`` an
    ordinary throwaway conversation that saves nothing.

    ``-w <password>`` takes the password from the command line, and ``-w``
    alone prompts for it without echo. The command-line form is what makes
    ``cobirb -w 1234`` work in one go; it is also visible in shell history
    and in ``ps``, which is why the bare form still exists and why the help
    text says so.

    ``continue_last`` picks the most recently touched session and **implies a
    password**: continuing a session means unlocking one, so asking for
    ``--continue -w`` as two flags would be asking the same question twice.
    An explicit ``-w <password>`` is still honoured, so the non-interactive
    form keeps working.
    """
    if continue_last:
        path = most_recent_session()
        if path is None:
            raise NoSessionToContinue(
                f"there are no sessions in {sessions_dir_display()} to continue — "
                "start one with 'cobirb -w'."
            )
        password = str(password_arg) if password_arg not in (None, True, False) else read_password()
        return path, password
    if not session_arg and not password_arg:
        return None, None
    if password_arg is True or password_arg is None:
        password = read_password()
    else:
        password = str(password_arg)
    path = session_arg or os.path.join(
        session_module.default_sessions_dir(), f"session-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    return path, password


def session_open_error(exc: Exception) -> str:
    """A readable reason a session file could not be opened.

    A wrong password surfaces as the crypto library's ``InvalidTag``, which
    carries **no message at all** — AES-GCM can only report that the
    authentication tag didn't match, never why. Printed raw that produced a
    dangling "could not open that session —" with nothing after the dash, so
    an empty message is spelled out here instead. Tamper detection
    (``SessionManager._verify_hashes``) does raise with a real message, and
    that one is passed through unchanged.
    """
    message = str(exc).strip()
    return message or "wrong password, or the file has been modified"


def resume_hint(session_path: str) -> str:
    """The exact command that reopens ``session_path``.

    Printed on the way out of a session so the file isn't something the user
    has to go hunting for. Deliberately ``-w`` with no value: the password
    would otherwise be printed to the terminal and into whatever scrollback
    or log is capturing it.
    """
    return (
        "Session saved. Resume it with:\n"
        f"  cobirb --session {abbreviate_home(session_path)} -w"
    )
