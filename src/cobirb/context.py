"""Fitting a conversation into the model's context window.

Sending every turn of a session on every model call is what this exists to
prevent. Read a few files and the request outgrows the window; the server then
silently truncates from the front, so the model loses the task it was given and
re-reads files it has already read. From the user's side that reads as "it got
dumb halfway through" — the worst failure an agent has, because it looks like
the model's fault rather than the harness's.

This module decides what to keep. Two passes, in this order, because they
lose increasing amounts:

1. **Elide old tool results.** A file you read eight turns ago is the
   cheapest thing in the history to forget: it is usually the largest, and
   the model has already extracted what it needed. The call that produced it
   stays, so the model still knows the read happened.
2. **Drop the oldest turns.** Only if eliding wasn't enough, and always
   leaving the opening request (the objective) and a recent working window.

Deliberately no model call. Summarising the dropped turns would preserve more,
but it costs a round trip on every compaction and can itself fail. A
deterministic, testable rule that never makes things worse is what belongs
underneath; summarisation can sit on top of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Used only when the provider cannot say what window it has. Not Ollama's own
# 4096 default: CoBirb *states* `num_ctx` on every request (see
# `LocalModelProvider.context_window`), so the server serves what is asked for
# and there is nothing to be timid about. Guessing low would re-create the
# silent truncation this module exists to prevent, one conversation at a time.
# 32k is a floor for the rare case where nothing is discoverable, not an
# expectation — the target hardware runs 128k comfortably.
DEFAULT_CONTEXT_TOKENS = 32768

# What the history must leave room for: the system prompt and project context,
# the tool schemas, and the model's reply. An *absolute* allowance rather than
# a fraction: 40% of a 128k window would hold back 51k tokens, which is absurd,
# while on a small window a fraction reserves too little to answer from.
_RESERVE_MIN_TOKENS = 2048
_RESERVE_MAX_TOKENS = 16384
_RESERVE_FRACTION = 0.2

# Tokens per character, near enough. A real tokenizer would mean a new
# dependency and a per-model vocabulary, and would still only sharpen an
# estimate that is deliberately used with headroom. Code and English both sit
# close to four characters per token; the ratio is wrong for CJK, which is why
# the fraction above is not 0.9.
_CHARS_PER_TOKEN = 4

# Turns at the end of the history that are never touched: the immediate
# working set, where the model is actually operating.
_KEEP_RECENT = 6

# Below this, eliding a tool result saves less than the note replacing it.
_ELIDE_MIN_CHARS = 400


def estimate_tokens(text: str) -> int:
    """Roughly how many tokens ``text`` will cost. An estimate, used with
    headroom — never a promise."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def history_budget(context_tokens: int) -> int:
    """How many tokens of conversation history a window of this size allows.

    The reserve scales but is clamped at both ends, so a large window is not
    made to waste a fifth of itself and a small one still keeps enough back to
    hold a reply.
    """
    reserve = min(max(int(context_tokens * _RESERVE_FRACTION), _RESERVE_MIN_TOKENS), _RESERVE_MAX_TOKENS)
    return max(512, context_tokens - reserve)


@dataclass
class CompactionReport:
    """What compaction did, for ``/context`` and the log."""

    estimated_tokens: int
    budget_tokens: int
    total_turns: int
    kept_turns: int
    elided_results: int
    dropped_turns: int

    @property
    def changed(self) -> bool:
        return bool(self.elided_results or self.dropped_turns)

    def describe(self) -> str:
        """One line a person can read."""
        used = f"~{self.estimated_tokens} of {self.budget_tokens} history tokens"
        if not self.changed:
            return f"{used}; {self.total_turns} turns, nothing compacted."
        parts = []
        if self.elided_results:
            parts.append(f"{self.elided_results} tool result(s) elided")
        if self.dropped_turns:
            parts.append(f"{self.dropped_turns} early turn(s) dropped")
        return f"{used}; {self.kept_turns} of {self.total_turns} turns kept, " + ", ".join(parts) + "."


# What one attached image costs the model, nominally. Deliberately *not*
# `estimate_tokens(base64)`: a 1 MB screenshot is ~1.4 M base64 characters,
# which the character rule would price at ~350 000 tokens, and a single
# screenshot would then look like it had blown a 128k window on its own. A
# vision encoder charges a bounded amount per image regardless of file size,
# and this is that order of magnitude. Better a stable approximation than a
# number that is wrong by two orders and drives compaction from panic.
_IMAGE_TOKENS = 1500


def _size(turns: list[dict[str, Any]]) -> int:
    """Estimated token cost of a turn list, counting the structural overhead
    of the roles and tool-call records rather than just the prose."""
    total = 0
    for turn in turns:
        total += estimate_tokens(str(turn.get("content") or ""))
        total += estimate_tokens(str(turn.get("tool_use") or ""))
        total += _IMAGE_TOKENS * len(turn.get("images") or [])
        total += 4  # role and message framing
    return total


def _elide(turn: dict[str, Any]) -> dict[str, Any]:
    """Replace a tool result's body with a note that it existed.

    The tool name is kept, so the model can still see *that* it read a file
    and which one — only the contents go. That distinction matters: a model
    that thinks a read never happened will simply do it again.
    """
    name = ""
    if turn.get("tool_use"):
        name = str(turn["tool_use"][0].get("name", "")) or ""
    size = len(str(turn.get("content") or ""))
    label = f"{name} result" if name else "tool result"
    return {
        **turn,
        "content": f"[earlier {label} elided to fit the context window — {size} characters]",
    }


def _elide_image(turn: dict[str, Any]) -> dict[str, Any]:
    """Drop a turn's attached images, leaving a note that they were there.

    The counterpart of ``_elide`` for attachments, and it exists for the same
    reason: an old image is the single most expensive thing in a long
    session's history, and the model needs to know an image *was* attached
    far more than it needs to see it again ten turns later. The recent ones
    (inside ``_KEEP_RECENT``) are never touched, so the image you are
    actually discussing stays visible.
    """
    names = " ".join(f"[image: {img.get('filename') or 'attachment'} elided to fit the context window]"
                     for img in turn.get("images") or [])
    content = str(turn.get("content") or "")
    return {**turn, "content": f"{content}\n{names}".strip() if content else names, "images": None}


_TRIM_MARK = "\n[…"

# Below this a body has nothing useful left to give, and trimming further only
# costs the note that says we did.
_MIN_BODY_CHARS = 80


def _truncate(turn: dict[str, Any], keep_chars: int) -> dict[str, Any]:
    """Cut a turn's body down, saying by how much.

    Any note from a previous trim is stripped first, so a body trimmed twice
    ends with one note rather than a stack of them.
    """
    body = str(turn.get("content") or "").partition(_TRIM_MARK)[0]
    if len(body) <= keep_chars:
        return turn
    cut = len(body) - keep_chars
    return {**turn, "content": body[:keep_chars] + f"{_TRIM_MARK}{cut} characters trimmed to fit]"}


def compact(
    turns: list[dict[str, Any]], budget_tokens: int
) -> tuple[list[dict[str, Any]], CompactionReport]:
    """Return ``turns`` trimmed to fit ``budget_tokens``, and what that cost.

    Short sessions — the overwhelming majority — take the fast path and come
    back byte-for-byte unchanged, so nothing about the common case changes.

    Longer ones go through the passes in order of how much they cost to lose:
    elide old tool results, then drop the oldest turns, then — only if the
    recent working set is *itself* bigger than the whole budget, which happens
    the moment a model reads one enormous file — elide and trim inside it too.
    Coming back still over budget is the one outcome that defeats the point,
    so the last pass has no floor except the final turn.
    """
    total = len(turns)
    estimated = _size(turns)
    if estimated <= budget_tokens or total <= 1:
        return turns, CompactionReport(estimated, budget_tokens, total, total, 0, 0)

    working = [dict(turn) for turn in turns]
    elided = 0

    def fits() -> bool:
        return _size(working) <= budget_tokens

    def elide_at(index: int) -> bool:
        """Elide the tool result — or the attached image — at ``index``."""
        nonlocal elided
        turn = working[index]
        # Images first: one of them outweighs most tool results, and unlike a
        # tool result there is no size threshold worth checking.
        if turn.get("images"):
            working[index] = _elide_image(turn)
            elided += 1
            return True
        if turn.get("role") != "tool" or "elided to fit" in str(turn.get("content") or ""):
            return False
        if len(str(turn.get("content") or "")) < _ELIDE_MIN_CHARS:
            return False
        working[index] = _elide(turn)
        elided += 1
        return True

    # Pass 1 — the oldest tool results, outside the working set.
    # Index 0 is the objective the user asked for; the tail is where the model
    # is currently operating. Neither is touched here.
    protected_tail = max(1, total - _KEEP_RECENT)
    for index in range(1, protected_tail):
        if fits():
            break
        elide_at(index)

    # Pass 2 — drop a contiguous run from the front, after the opening
    # request. Contiguous and from the front specifically, because an
    # assistant turn announcing a tool call and the tool turn answering it
    # have to survive together: a result with nothing requesting it is the
    # very confusion the JSON turn history was introduced to prevent.
    dropped = 0
    if not fits():
        drop_to = 1
        while drop_to < protected_tail and _size([working[0], *working[drop_to:]]) > budget_tokens:
            drop_to += 1
        # Never begin the kept remainder on an orphaned tool result.
        while drop_to < len(working) and working[drop_to].get("role") == "tool":
            drop_to += 1
        dropped = drop_to - 1
        if dropped > 0:
            note = {
                "role": "user",
                "content": (
                    f"[{dropped} earlier turn(s) from this session were dropped to fit the "
                    "context window. The original request above still stands.]"
                ),
                "tool_use": None,
            }
            working = [working[0], note, *working[drop_to:]]

    # Pass 3 — the working set is itself over budget. One large file read is
    # enough to cause this, so it is not an edge case worth failing on.
    for index in range(len(working) - 1):
        if fits():
            break
        elide_at(index)

    # Pass 4 — nothing structural left to shed, so trim bodies. Repeatedly
    # take the largest and cut it back toward the size of the rest, which
    # converges quickly and spreads the loss instead of gutting one turn.
    # A single arithmetic pass isn't enough: the note each trim adds, and the
    # tool-call records that can't be trimmed at all, both push the total back
    # over. Bounded so an impossible budget ends rather than spins.
    for _ in range(64):
        if fits():
            break
        index = max(
            range(len(working)), key=lambda i: len(str(working[i].get("content") or ""))
        )
        body = str(working[index].get("content") or "").partition(_TRIM_MARK)[0]
        if len(body) <= _MIN_BODY_CHARS:
            break  # every body is already at the floor; the rest is structure
        working[index] = _truncate(working[index], max(_MIN_BODY_CHARS, int(len(body) * 0.6)))

    kept_turns = len(working) - (1 if dropped else 0)
    return working, CompactionReport(
        _size(working), budget_tokens, total, kept_turns, elided, dropped
    )
