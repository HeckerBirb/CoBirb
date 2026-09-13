"""Keeping credentials out of prompts, sessions and the audit log.

A tool result does not stop at the screen. It becomes a turn in the session,
a message in the next model request, and — if the audit log is on — a line in
a plaintext file. So one `read_file` on a `.env` puts a live credential in
three places at once, in a tool whose first line is that privacy is the
foundation. That is the gap this closes.

**Only high-confidence patterns.** Every rule here matches a credential format
with a distinctive prefix or structure: `AKIA…`, `ghp_…`, `sk-…`, a PEM block.
Nothing matches on a *name* — no `password=`, no `SECRET=`, no entropy
heuristics — because those fire on documentation, test fixtures, variable
declarations and prose, and a redactor that eats real content is worse than
none. The trade is deliberate: this will miss a bespoke credential format, and
it will very rarely destroy something that wasn't one.

**Redaction is visible.** A removed secret leaves `[redacted: github token]`
rather than vanishing, so the model knows something was there and can say so
instead of quietly working from a value it never received.

**It has a real cost, so it can be turned off.** An agent asked to *edit* a
credentials file cannot do it through a redacted read. `"redact_secrets":
false` is the way out, and the README says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Each rule is (name, pattern). Names appear in the replacement marker, so
# they are written for a person reading a transcript.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(
        r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----", re.S)),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("api key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("json web token", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private key material", re.compile(r"\b(?:ssh-rsa|ssh-ed25519)\s+[A-Za-z0-9+/]{60,}={0,3}")),
]


@dataclass
class Redaction:
    """What was removed from a piece of text."""

    text: str
    counts: dict[str, int]

    @property
    def changed(self) -> bool:
        return bool(self.counts)

    def describe(self) -> str:
        parts = [f"{count} {name}" + ("s" if count > 1 else "") for name, count in sorted(self.counts.items())]
        return "redacted " + ", ".join(parts)


def redact(text: str) -> Redaction:
    """Replace anything that looks unmistakably like a credential."""
    if not text:
        return Redaction(text, {})
    counts: dict[str, int] = {}
    for name, pattern in _RULES:
        def _replace(match: re.Match[str], _name: str = name) -> str:
            counts[_name] = counts.get(_name, 0) + 1
            return f"[redacted: {_name}]"

        text = pattern.sub(_replace, text)
    return Redaction(text, counts)


def redact_arguments(arguments: dict) -> dict:
    """Redact the string values of a tool call's arguments.

    For the audit log, whose whole problem is that it records arguments
    verbatim — ``write_file``'s full content, ``shell``'s full command — into
    a plaintext file that outlives the session.
    """
    cleaned = {}
    for key, value in (arguments or {}).items():
        cleaned[key] = redact(value).text if isinstance(value, str) else value
    return cleaned
