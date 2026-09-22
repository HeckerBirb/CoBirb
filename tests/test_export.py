"""Tests for exporting a decrypted session as readable markdown."""
from __future__ import annotations

import os
import stat

from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto
from cobirb.runtime.export import session_to_markdown, write_export
from cobirb.session import Session, SessionManager, Turn


def _session() -> Session:
    session = Session(working_dir="/proj")
    session.add(Turn(role="user", content="add rate limiting"))
    session.add(Turn(role="assistant", content="Reading the router.",
                     tool_use=[{"name": "read_file", "arguments": {"path": "r.py"}}]))
    session.add(Turn(role="tool", content="def route():\n    pass",
                     tool_use=[{"name": "read_file", "arguments": {"path": "r.py"}}]))
    session.summary = "Added a limiter."
    return session


def test_the_conversation_comes_out_readable():
    markdown = session_to_markdown(_session())

    assert "# CoBirb session" in markdown
    assert "## You" in markdown and "add rate limiting" in markdown
    assert "## CoBirb" in markdown
    assert "## Summary" in markdown and "Added a limiter." in markdown


def test_a_tool_result_is_fenced_rather_than_inlined():
    """Tool output is output, not prose — a diff or a listing pasted inline
    reflows into nonsense."""
    markdown = session_to_markdown(_session())

    assert "```" in markdown
    assert "Tool: `read_file`" in markdown


def test_backticks_in_tool_output_cannot_break_out_of_the_fence():
    session = Session()
    session.add(Turn(role="tool", content="here is ``` a fence", tool_use=None))

    markdown = session_to_markdown(session)

    assert "````" in markdown  # the fence grew to contain it


def test_plan_and_validate_turns_are_labelled():
    session = Session()
    session.add(Turn(role="assistant", content="1. do the thing", phase="plan"))
    session.add(Turn(role="assistant", content="it was done", phase="validate"))

    markdown = session_to_markdown(session)

    assert "CoBirb · plan" in markdown
    assert "CoBirb · validate" in markdown


def test_the_exported_file_is_owner_only(tmp_path):
    """Plaintext by design, but that's no reason to hand it to every account
    on the machine as well — sharing it is the user's next decision, not the
    file mode's."""
    destination = write_export(_session(), str(tmp_path / "out" / "s.md"))

    assert os.path.isfile(destination)
    assert stat.S_IMODE(os.stat(destination).st_mode) == 0o600


def test_a_real_encrypted_session_round_trips_to_markdown(tmp_path):
    """End to end through the crypto, since that is the only way to know the
    export path actually reaches a decrypted session."""
    path = str(tmp_path / "s.json")
    manager = SessionManager.create(path, AesGcmScryptSessionCrypto(), str(tmp_path), "pw")
    manager.session.add(Turn(role="user", content="the secret plan"))
    manager.save("pw")

    reloaded = SessionManager.load(path, AesGcmScryptSessionCrypto(), "pw")
    markdown = session_to_markdown(reloaded.session)

    assert "the secret plan" in markdown
    # ...and the session on disk is still opaque.
    with open(path, "rb") as fh:
        assert b"the secret plan" not in fh.read()


def test_an_attached_image_shows_as_a_marker_never_as_bytes():
    session = Session(turns=[Turn(role="user", content="see attached", images=[{"id": "a", "filename": "shot.png"}])])
    markdown = session_to_markdown(session)
    assert "shot.png" in markdown
    assert "📎" in markdown
