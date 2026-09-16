"""Tests for ``@path`` mentions: finding a file, and sending it along."""
from __future__ import annotations

import os

import pytest

from cobirb.runtime import mentions


# --------------------------------------------------------------------------- #
# Ranking. The matching rule is "letters in order"; the ranking is what decides
# whether the picker feels right, so most of these are about order.
# --------------------------------------------------------------------------- #
def _paths(*names: str) -> list[str]:
    return list(names)


def test_letters_must_appear_in_order_but_need_not_be_adjacent():
    """The whole point of subsequence matching: 'gba' finds both of these."""
    found = [m.path for m in mentions.rank("gba", _paths("global.py", "general_batch.py"))]
    assert set(found) == {"global.py", "general_batch.py"}


def test_letters_out_of_order_do_not_match():
    assert mentions.rank("bag", _paths("gba.py")) == []


def test_an_exact_name_wins_outright():
    """Typing 'gba' when gba.py exists puts gba.py first, however well some
    longer name happens to score."""
    ranked = mentions.rank("gba", _paths("global.py", "general_batch.py", "gba.py"))
    assert ranked[0].path == "gba.py"


def test_an_exact_name_wins_even_when_listed_last():
    ranked = mentions.rank("gba", _paths("a/b/general_batch_processor.py", "gba.py"))
    assert ranked[0].path == "gba.py"


def test_word_boundary_hits_outrank_letters_buried_mid_word():
    """general_batch matches g+b at the start of two words; global matches
    them inside one."""
    ranked = mentions.rank("gb", _paths("global.py", "general_batch.py"))
    assert ranked[0].path == "general_batch.py"


def test_a_prefix_outranks_a_scattered_match():
    ranked = mentions.rank("conf", _paths("src/c_o_n_f.py", "config.py"))
    assert ranked[0].path == "config.py"


def test_the_shorter_of_two_equally_good_names_comes_first():
    ranked = mentions.rank("app", _paths("tui/application_builder.py", "tui/app.py"))
    assert ranked[0].path == "tui/app.py"


def test_a_directory_in_the_path_still_matches():
    ranked = mentions.rank("tuiapp", _paths("src/tui/app.py"))
    assert [m.path for m in ranked] == ["src/tui/app.py"]


def test_matching_is_case_insensitive():
    assert [m.path for m in mentions.rank("RM", _paths("README.md"))] == ["README.md"]


def test_the_list_is_capped():
    many = [f"file{n}.py" for n in range(50)]
    assert len(mentions.rank("f", many)) == 5
    assert len(mentions.rank("f", many, limit=3)) == 3


def test_an_empty_query_returns_the_first_candidates_rather_than_nothing():
    """Typing '@' alone should show something to pick from."""
    assert len(mentions.rank("", _paths("a.py", "b.py", "c.py"))) == 3


def test_ranking_is_stable_for_equally_scored_paths():
    once = mentions.rank("x", _paths("b/x.py", "a/x.py"))
    twice = mentions.rank("x", _paths("b/x.py", "a/x.py"))
    assert [m.path for m in once] == [m.path for m in twice]


# --------------------------------------------------------------------------- #
# Finding mentions in what was typed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("explain @src/main.py", ["src/main.py"]),
        ("compare @a.py and @b.py", ["a.py", "b.py"]),
        ("look at @main.py, then stop", ["main.py"]),          # trailing comma not part of it
        ("is @main.py right?", ["main.py"]),                    # nor a question mark
        ("(see @main.py)", ["main.py"]),                        # nor a closing bracket
        ("@main.py", ["main.py"]),
        ("no mentions here", []),
        ("email me at foo@example.com", []),                    # not a mention
        ("@a.py and @a.py again", ["a.py"]),                    # de-duplicated
    ],
)
def test_find_pulls_out_the_paths(text, expected):
    assert mentions.find(text) == expected


# --------------------------------------------------------------------------- #
# Expansion — what the model actually receives
# --------------------------------------------------------------------------- #
def test_expand_appends_the_file_and_keeps_the_sentence(tmp_path):
    (tmp_path / "a.py").write_text("print('hello')\n")

    out = mentions.expand("explain @a.py please", str(tmp_path))

    assert "explain @a.py please" in out      # the sentence still reads as written
    assert "--- a.py ---" in out              # labelled, so the model knows which is which
    assert "print('hello')" in out


def test_expand_leaves_a_prompt_with_no_mentions_untouched(tmp_path):
    assert mentions.expand("just a question", str(tmp_path)) == "just a question"


def test_expand_handles_several_files_in_order(tmp_path):
    (tmp_path / "a.py").write_text("AAA")
    (tmp_path / "b.py").write_text("BBB")

    out = mentions.expand("compare @a.py and @b.py", str(tmp_path))

    assert out.index("--- a.py ---") < out.index("--- b.py ---")
    assert "AAA" in out and "BBB" in out


def test_a_missing_file_is_reported_inline_rather_than_raising(tmp_path):
    """The rest of the message is still worth sending, and the model being
    told the file isn't there beats the turn failing."""
    out = mentions.expand("explain @nope.py", str(tmp_path))
    assert "could not be read" in out


def test_a_relative_mention_resolves_against_the_given_cwd(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("content here")

    out = mentions.expand("see @docs/x.md", str(tmp_path))

    assert "content here" in out


def test_a_mentioned_file_is_capped_like_any_other_read(tmp_path, monkeypatch):
    monkeypatch.setattr(mentions, "MAX_MENTION_BYTES", 100)
    (tmp_path / "big.txt").write_text("x" * 5000)

    out = mentions.expand("read @big.txt", str(tmp_path))

    assert "truncated at 100 bytes" in out
    assert len(out) < 1000


def test_credentials_in_a_mentioned_file_are_redacted(tmp_path):
    """A mention is a read like any other, so it goes through the same
    redaction tool output does — sharing a file deliberately is not the same
    as deciding a key should reach the model."""
    (tmp_path / ".env").write_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")

    out = mentions.expand("check @.env", str(tmp_path))

    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_redaction_can_be_turned_off_the_way_it_is_everywhere_else(tmp_path):
    (tmp_path / ".env").write_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")

    out = mentions.expand("check @.env", str(tmp_path), redact_secrets=False)

    assert "AKIAIOSFODNN7EXAMPLE" in out
