"""Tests for `.gitignore` handling in glob and grep.

A documented subset of the format, so these pin down the subset rather than
git's full behaviour: the cost of getting a rule wrong here is a file searched
that needn't have been, not a file wrongly committed.
"""
from __future__ import annotations

from cobirb.plugins.core.ignores import IgnoreRules


def _rules(tmp_path, gitignore: str) -> IgnoreRules:
    (tmp_path / ".gitignore").write_text(gitignore)
    return IgnoreRules.for_directory(str(tmp_path))


def test_a_plain_name_matches_at_any_depth(tmp_path):
    """No slash in the pattern means "this basename, anywhere" — the part of
    the format people most often forget."""
    rules = _rules(tmp_path, "build\n")

    assert rules.is_ignored(tmp_path / "build", is_dir=True)
    assert rules.is_ignored(tmp_path / "src" / "deep" / "build", is_dir=True)
    assert not rules.is_ignored(tmp_path / "src" / "rebuild.py", is_dir=False)


def test_a_leading_slash_anchors_to_the_project_root(tmp_path):
    rules = _rules(tmp_path, "/build\n")

    assert rules.is_ignored(tmp_path / "build", is_dir=True)
    assert not rules.is_ignored(tmp_path / "src" / "build", is_dir=True)


def test_ignoring_a_directory_ignores_everything_under_it(tmp_path):
    rules = _rules(tmp_path, "dist/\n")

    assert rules.is_ignored(tmp_path / "dist" / "app" / "bundle.js", is_dir=False)


def test_a_directory_only_pattern_does_not_match_a_file_of_that_name(tmp_path):
    rules = _rules(tmp_path, "cache/\n")

    assert rules.is_ignored(tmp_path / "cache", is_dir=True)
    assert not rules.is_ignored(tmp_path / "cache", is_dir=False)


def test_a_star_does_not_cross_a_directory_separator(tmp_path):
    """The one thing fnmatch gets wrong, and the reason the matcher is
    hand-written rather than delegated."""
    rules = _rules(tmp_path, "src/*.py\n")

    assert rules.is_ignored(tmp_path / "src" / "a.py", is_dir=False)
    assert not rules.is_ignored(tmp_path / "src" / "deep" / "a.py", is_dir=False)


def test_a_double_star_does_cross_separators(tmp_path):
    rules = _rules(tmp_path, "src/**/*.py\n")

    assert rules.is_ignored(tmp_path / "src" / "deep" / "down" / "a.py", is_dir=False)


def test_negation_rescues_something_an_earlier_rule_swept_up(tmp_path):
    rules = _rules(tmp_path, "*.log\n!keep.log\n")

    assert rules.is_ignored(tmp_path / "noise.log", is_dir=False)
    assert not rules.is_ignored(tmp_path / "keep.log", is_dir=False)


def test_the_last_matching_rule_wins(tmp_path):
    """Order matters, and re-ignoring after a negation has to work too."""
    rules = _rules(tmp_path, "*.log\n!keep.log\nkeep.log\n")

    assert rules.is_ignored(tmp_path / "keep.log", is_dir=False)


def test_comments_and_blank_lines_are_skipped(tmp_path):
    rules = _rules(tmp_path, "# a comment\n\n   \nbuild\n")

    assert rules.is_ignored(tmp_path / "build", is_dir=True)
    assert not rules.is_ignored(tmp_path / "a comment", is_dir=False)


def test_character_classes_work(tmp_path):
    rules = _rules(tmp_path, "tmp[0-9].txt\n")

    assert rules.is_ignored(tmp_path / "tmp3.txt", is_dir=False)
    assert not rules.is_ignored(tmp_path / "tmpX.txt", is_dir=False)


def test_the_builtin_list_applies_with_no_gitignore_at_all(tmp_path):
    """`.git` is never listed in a .gitignore — git has no need to ignore
    itself — and the rest are near-universal enough that a project forgetting
    them shouldn't cost the user a slow search."""
    rules = IgnoreRules.for_directory(str(tmp_path))

    for name in (".git", "node_modules", "__pycache__", ".venv"):
        assert rules.is_ignored(tmp_path / name / "thing", is_dir=False), name
    assert rules.is_ignored(tmp_path / "cobirb.egg-info" / "PKG-INFO", is_dir=False)


def test_the_builtin_list_applies_outside_the_project_too(tmp_path):
    """Searching someone else's node_modules is no more useful than searching
    your own, and an absolute path outside the root is a normal thing for a
    model to pass."""
    rules = IgnoreRules.for_directory(str(tmp_path / "project"))

    assert rules.is_ignored("/somewhere/else/node_modules/x.js", is_dir=False)


def test_gitignore_rules_do_not_apply_outside_their_project(tmp_path):
    """A pattern is only meaningful relative to the repo it came from."""
    rules = _rules(tmp_path, "notes.txt\n")

    assert rules.is_ignored(tmp_path / "notes.txt", is_dir=False)
    assert not rules.is_ignored("/elsewhere/notes.txt", is_dir=False)


def test_a_missing_or_unreadable_gitignore_is_not_an_error(tmp_path):
    """A search that skips slightly less than it could is a far better
    failure than one that refuses to run."""
    rules = IgnoreRules.for_directory(str(tmp_path / "does-not-exist"))

    assert not rules.is_ignored(tmp_path / "anything.py", is_dir=False)
