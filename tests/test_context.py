"""Tests for fitting a conversation into the model's context window.

The behaviour that matters here is a contract, not an implementation: a short
history comes back untouched, a long one comes back inside budget, and what
survives is still a coherent conversation the model can act on. The specific
order of elision is an implementation detail these deliberately don't pin
down beyond "tool results go before turns do".
"""
from __future__ import annotations

from cobirb.context import (
    DEFAULT_CONTEXT_TOKENS,
    compact,
    estimate_tokens,
    history_budget,
)


def _turn(role, content, tool_use=None):
    return {"role": role, "content": content, "tool_use": tool_use}


def _exchange(n: int, size: int = 4000) -> list[dict]:
    """One read_file round trip: the model announcing it, and the result."""
    call = [{"name": "read_file", "arguments": {"path": f"file{n}.py"}}]
    return [
        _turn("assistant", "", call),
        _turn("tool", f"contents of file{n} " + "x" * size, call),
    ]


def test_a_short_history_is_returned_untouched():
    """The overwhelming majority of sessions. Byte-for-byte identical, so
    nothing about the common case changes."""
    turns = [_turn("user", "hello"), _turn("assistant", "hi")]

    kept, report = compact(turns, budget_tokens=10_000)

    assert kept == turns
    assert not report.changed
    assert report.kept_turns == 2


def test_a_long_history_is_brought_inside_the_budget():
    turns = [_turn("user", "refactor the parser")]
    for n in range(12):
        turns += _exchange(n)

    kept, report = compact(turns, budget_tokens=1500)

    assert report.changed
    assert report.estimated_tokens <= 1500
    assert len(kept) < len(turns)


def test_the_original_request_always_survives():
    """Losing the objective is the one failure that makes everything after it
    pointless — the model would carry on with no idea what it was asked."""
    turns = [_turn("user", "THE OBJECTIVE")]
    for n in range(20):
        turns += _exchange(n)

    kept, _ = compact(turns, budget_tokens=800)

    assert kept[0]["content"] == "THE OBJECTIVE"


def test_the_recent_working_set_survives_verbatim():
    """Whatever else goes, the turns the model is currently operating on stay
    intact — compacting those would be sawing off the branch it stands on."""
    turns = [_turn("user", "go")]
    for n in range(15):
        turns += _exchange(n)
    turns.append(_turn("tool", "THE LATEST RESULT " + "y" * 5000, [{"name": "read_file"}]))

    kept, _ = compact(turns, budget_tokens=2000)

    assert "THE LATEST RESULT" in kept[-1]["content"]


def test_a_modest_overflow_is_absorbed_by_eliding_results_not_dropping_turns():
    """Ordered by how much they cost to lose. A file read eight turns ago is
    the cheapest thing in the history to forget; a turn is not.

    The budget is derived from the history's own measured size rather than
    hardcoded, so this states the contract — "a modest overflow costs you
    results, not turns" — instead of pinning down arithmetic that would break
    the moment the estimator is tuned.
    """
    turns = [_turn("user", "go")]
    for n in range(6):
        turns += _exchange(n, size=3000)
    _, uncompacted = compact(turns, budget_tokens=10**9)

    _, report = compact(turns, budget_tokens=int(uncompacted.estimated_tokens * 0.7))

    assert report.elided_results > 0
    assert report.dropped_turns == 0


def test_an_elided_result_still_says_the_call_happened():
    """A model that thinks a read never happened simply does it again."""
    turns = [_turn("user", "go")]
    for n in range(6):
        turns += _exchange(n, size=3000)

    kept, _ = compact(turns, budget_tokens=2500)

    elided = [t for t in kept if "elided" in str(t["content"])]
    assert elided
    assert "read_file" in elided[0]["content"]
    assert elided[0]["tool_use"] is not None


def test_the_kept_history_never_opens_on_an_orphaned_tool_result():
    """An assistant turn announcing a call and the tool turn answering it have
    to survive together. A result with nothing requesting it is the exact
    confusion the JSON turn history was introduced to prevent."""
    turns = [_turn("user", "go")]
    for n in range(25):
        turns += _exchange(n, size=6000)

    kept, report = compact(turns, budget_tokens=600)

    assert report.dropped_turns > 0
    after_the_note = kept[2:]  # [0] objective, [1] the dropped-turns note
    if after_the_note:
        assert after_the_note[0]["role"] != "tool"


def test_dropping_turns_leaves_a_note_saying_so():
    turns = [_turn("user", "go")]
    for n in range(25):
        turns += _exchange(n, size=6000)

    kept, report = compact(turns, budget_tokens=600)

    assert any("dropped to fit the context window" in str(t["content"]) for t in kept)
    assert report.dropped_turns > 0


def test_one_enormous_turn_is_trimmed_rather_than_blowing_the_window():
    """A single turn bigger than the whole budget is not an edge case — one
    large file read causes it. Returning it unchanged would hand the server
    something it silently truncates, which is the bug this module exists for."""
    turns = [_turn("user", "go"), _turn("tool", "x" * 100_000, [{"name": "read_file"}])]

    kept, report = compact(turns, budget_tokens=500)

    assert report.estimated_tokens <= 500
    assert len(str(kept[-1]["content"])) < 100_000


def test_the_result_fits_across_a_range_of_shapes_and_budgets():
    """The one guarantee worth stating as a property: whatever went in, what
    comes out is inside the budget."""
    for exchanges in (1, 5, 20):
        for size in (100, 5_000, 50_000):
            for budget in (600, 2_000, 8_000):
                turns = [_turn("user", "the objective")]
                for n in range(exchanges):
                    turns += _exchange(n, size=size)
                _, report = compact(turns, budget_tokens=budget)
                assert report.estimated_tokens <= budget, (exchanges, size, budget)


def test_history_budget_leaves_room_for_the_reply():
    """A history allowed to fill the whole window leaves nowhere to answer
    from — the tool schemas and the reply both have to fit too."""
    assert history_budget(4096) < 4096
    assert history_budget(100) >= 512  # a floor, so tiny windows stay usable


def test_estimate_tokens_is_roughly_four_characters_each():
    assert 20 <= estimate_tokens("a" * 100) <= 30
    assert estimate_tokens("") >= 1


def test_the_default_window_is_conservative():
    """Guessing high re-creates the bug: the server truncates and nobody is
    told. Guessing low only costs some avoidable compaction."""
    assert DEFAULT_CONTEXT_TOKENS <= 4096
