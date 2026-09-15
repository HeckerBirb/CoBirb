"""Images queued for the next message.

``/image`` attaches; the next prompt carries them. That is a small amount of
state with a clear lifetime — queued, then taken exactly once by the turn that
sends them. Kept here rather than as a bare list on the app, with the
reading, sniffing and payload-shaping in one place rather than spread across
the methods that happen to need them.

Nothing here writes to disk. The bytes go to the orchestrator, which files
them in ``Session.images`` and saves them encrypted with the rest of the
session (see ``session.Session.images`` for why they live there and not
beside it).
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

# Magic-byte prefixes for the image formats CoBirb recognizes. A sniff, not a
# size cap: the question is "is this actually an image", not "is this small
# enough" — CoBirb's hardware baseline has no business picking a byte limit,
# and nothing here does.
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")


class AttachmentError(ValueError):
    """An attachment couldn't be queued, phrased for the user to read.

    A missing file and a file that isn't an image are both ordinary
    mistakes, so they carry a sentence rather than a traceback.
    """


def looks_like_an_image(data: bytes) -> bool:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(magic) for magic in _IMAGE_MAGIC)


def resolve_path(path: str, cwd: str) -> str:
    """Resolve ``/image``'s path against the session's working directory.

    Not the process's — those are the same only when CoBirb was started from
    the directory it is working in, and ``--cwd`` exists precisely so they
    need not be.
    """
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)


def split_argument(argument: str, cwd: str) -> tuple[str, str]:
    """Split ``/image``'s argument into a path and whatever follows it.

    Typing the path and the question on one line — ``/image shot.png what is
    this?`` — is what people actually do, and taking the whole argument as a
    filename turned that into "no such file: 'shot.png what is this?'", which
    reads like the *file* is missing rather than like the command wanted only
    a path. So the trailing text is the message sent with the image.

    Three shapes, in the order that avoids guessing wrong:

    1. A quoted path (``/image "my screenshot.png" what is this?``) — explicit,
       so it wins outright.
    2. The whole argument naming a file that exists — which keeps an unquoted
       path containing spaces working exactly as it did before.
    3. Otherwise the first word is the path and the rest is the message.
    """
    argument = argument.strip()
    if not argument:
        return "", ""
    if argument[0] in "\"'":
        closing = argument.find(argument[0], 1)
        if closing != -1:
            return argument[1:closing], argument[closing + 1 :].strip()
    if os.path.isfile(resolve_path(argument, cwd)):
        return argument, ""
    path, _, message = argument.partition(" ")
    return path, message.strip()


@dataclass
class Attachment:
    """One queued image: what to call it, and its bytes as read off disk."""

    filename: str
    data: bytes


@dataclass
class PendingAttachments:
    """What ``/image`` has queued for the next message to carry."""

    pending: list[Attachment] = field(default_factory=list)

    def queue(self, path: str, cwd: str) -> str:
        """Read and check ``path``, queue it, and return its filename.

        Raises ``AttachmentError`` for anything the user should see as a
        sentence — which is every failure this can have.
        """
        resolved = resolve_path(path, cwd)
        try:
            with open(resolved, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise AttachmentError(f"Could not read '{path}' — {exc}") from exc
        if not looks_like_an_image(data):
            raise AttachmentError(f"'{path}' doesn't look like an image CoBirb recognizes.")
        filename = os.path.basename(resolved)
        self.pending.append(Attachment(filename=filename, data=data))
        return filename

    def take(self) -> "list[dict[str, Any]] | None":
        """Empty the queue and shape it for ``Orchestrator.run(images=...)``:
        content-hash id, filename, base64.

        Taken rather than read, because an attachment belongs to exactly one
        message — leaving it queued would silently re-send it with the next
        one too.
        """
        queued, self.pending = self.pending, []
        if not queued:
            return None
        return [
            {
                "id": hashlib.sha256(item.data).hexdigest(),
                "filename": item.filename,
                "data": base64.b64encode(item.data).decode("ascii"),
            }
            for item in queued
        ]
