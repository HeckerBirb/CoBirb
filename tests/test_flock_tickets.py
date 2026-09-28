"""Tests for reading and checking ticket blocks (``flock.tickets``)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from cobirb import sandbox
from cobirb.flock import tickets
from cobirb.flock.tickets import TicketSpec, check_tickets, parse_tickets

_BLOCKS = """
### ticket: a
- writes: a.py, test_a.py
- tests: test_a.py
- accept: "{py}" -m pytest test_a.py -q
- needs: none
- builds: doubles the number
- done when: test_a passes

### ticket: b
- writes: `b.py`, `test_b.py`
- tests: test_b.py
- accept: "{py}" -m pytest test_b.py -q
- needs: none
- builds: doubles the number too
- done when: test_b passes
""".replace("{py}", sys.executable)


def _requiring(*checks):
    return [
        TicketSpec(
            id="a",
            writes=("a.c",),
            tests=("ta.c",),
            accept="true",
            requires=tuple((f"dep{i}", c, f"install dep{i}") for i, c in enumerate(checks)),
        )
    ]


# --------------------------------------------------------------------------- #
# The ticket blocks
# --------------------------------------------------------------------------- #
def test_ticket_blocks_are_read_whatever_their_formatting():
    tickets = parse_tickets(_BLOCKS)

    assert [t.id for t in tickets] == ["a", "b"]
    assert tickets[1].writes == ("b.py", "test_b.py")
    assert tickets[0].tests == ("test_a.py",) and tickets[0].needs == ()


@pytest.mark.parametrize(
    "blocks, problem",
    [
        ("no blocks here", "no ticket blocks"),
        ("### ticket: a\n- writes: a.py\n- accept: x", "has no test files"),
        ("### ticket: a\n- writes: a.py\n- tests: t.py", "no `accept`"),
        (
            "### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: x\n"
            "### ticket: b\n- writes: a.py\n- tests: tb.py\n- accept: x",
            "listed by more than one ticket",
        ),
        # Another ticket's test file under `tests` — "the tests I must pass".
        (
            "### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: x\n"
            "### ticket: b\n- writes: b.py\n- tests: tb.py, ta.py\n- accept: x",
            "never another ticket's",
        ),
        ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true\n- needs: ghost", "ghost"),
        # The language's name where the program goes — a real flock's `accept`.
        ("### ticket: a\n- writes: a.c\n- tests: ta.c\n- accept: c a.c ta.c", "`c`, which is not a program"),
        (
            "### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true && no-such-program-here",
            "no-such-program-here",
        ),
        ("### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true $(x)", "cannot be read"),
    ],
)
def test_ticket_blocks_that_cannot_become_a_charter_say_why(blocks, problem):
    assert problem in check_tickets(parse_tickets(blocks))


@pytest.mark.parametrize(
    "accept",
    [
        "true",
        "cd sub && FLAG=1 true",
        # A program given as a path may be built by the command itself.
        "./build.sh && /tmp/test_a",
    ],
)
def test_an_accept_command_naming_real_programs_passes_the_check(accept):
    assert (
        check_tickets(parse_tickets(f"### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: {accept}"))
        == ""
    )


@pytest.mark.parametrize(
    "line",
    [
        "- writes: a.py",
        "- **writes**: a.py",
        "- **writes:** a.py",
        "* __writes__ : a.py",
    ],
)
def test_a_field_is_read_whether_or_not_its_name_is_bold(line):
    assert parse_tickets(f"### ticket: a\n{line}\n")[0].writes == ("a.py",)


@pytest.mark.parametrize(
    "block, tests",
    [
        ("### ticket: a\n- writes: a.py, tests/test_a.py\n- tests: tests/test_a.py", ("tests/test_a.py",)),
        # No `tests` line: the test files it writes are its tests.
        ("### ticket: a\n- writes: a.py, tests/test_a.py", ("tests/test_a.py",)),
        ("### ticket: a\n- writes: a.py, a_test.py", ("a_test.py",)),
        ("### ticket: a\n- writes: a.py", ()),
    ],
)
def test_a_tickets_tests_are_read_from_its_block(block, tests):
    assert parse_tickets(block)[0].tests == tests


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            "- requires: zlib headers — check: pkg-config --exists zlib",
            ("zlib headers", "pkg-config --exists zlib", ""),
        ),
        (
            '- **requires**: requests, check: `python3 -c "import requests"`',
            ("requests", 'python3 -c "import requests"', ""),
        ),
        ("- requires: zlib", ("zlib", "", "")),
        # A check ending in `-` keeps it: only a long dash, comma or space comes before `install:`.
        (
            "- requires: zlib for MinGW — check: x86_64-w64-mingw32-gcc -E -x c - — install: sudo apt install libz-mingw-w64-dev",
            ("zlib for MinGW", "x86_64-w64-mingw32-gcc -E -x c -", "sudo apt install libz-mingw-w64-dev"),
        ),
    ],
)
def test_a_requirement_is_read_with_its_check(line, expected):
    ticket = parse_tickets(f"### ticket: a\n- writes: a.c\n{line}\n")[0]

    assert ticket.requires == (expected,)
    assert parse_tickets(ticket.block())[0].requires == (expected,)


@pytest.mark.parametrize(
    "line, problem",
    [
        ("- requires: zlib", "has no check"),
        ("- requires: zlib — check: no-such-probe --exists zlib", "`no-such-probe`, which is not a program"),
    ],
)
def test_a_requirement_needs_a_check_that_can_run(line, problem):
    blocks = f"### ticket: a\n- writes: a.py\n- tests: ta.py\n- accept: true\n{line}"

    assert problem in check_tickets(parse_tickets(blocks))


def test_requirements_are_not_run_where_commands_would_be_asked_about(tmp_path):
    """Brainy Birb's checks run only where contained commands already run unasked."""
    main = SimpleNamespace(tools={}, policy=SimpleNamespace(sandbox_auto=False))

    results = tickets.check_requirements(main, _requiring("true"), str(tmp_path))

    assert results == [("a", "dep0", tickets.UNCHECKED, "install dep0")]
    assert "not checked" in tickets.describe_requirements(results)


@pytest.mark.skipif(sandbox.find_bwrap() is None, reason="bubblewrap not usable here")
def test_requirements_are_checked_inside_the_sandbox(tmp_path):
    box = sandbox.from_config("auto", str(tmp_path))
    main = SimpleNamespace(
        tools={"shell": SimpleNamespace(sandbox=box)}, policy=SimpleNamespace(sandbox_auto=True)
    )

    results = tickets.check_requirements(main, _requiring("true", "false"), str(tmp_path))

    assert [status for _, _, status, _ in results] == [tickets.INSTALLED, tickets.MISSING]
    described = tickets.describe_requirements(results)
    assert "dep1 — MISSING" in described and "install dep1" in described
    assert "dep0" not in described


@pytest.mark.parametrize(
    "line, tests",
    [
        ("tests/test_a.py, tests/test_b.py", ("tests/test_a.py", "tests/test_b.py")),
        # The command that runs the tests, where the files belong.
        ("python -m pytest tests/test_a.py -q", ("tests/test_a.py",)),
        ("`python -m pytest tests/test_a.py`", ("tests/test_a.py",)),
        ("cd sub && pytest tests/test_a.py tests/test_b.py", ("tests/test_a.py", "tests/test_b.py")),
        # A command naming its build output and the code under test keeps only the test.
        ("cc -Wall -o /tmp/t a.c tests/test_a.c && /tmp/t", ("tests/test_a.c",)),
        ("cc -o /tmp/t latest.c tests/test_a.c && /tmp/t", ("tests/test_a.c",)),
        ("tests/test_a.py (unit tests for a)", ("tests/test_a.py",)),
        # Test files of any language, not only pytest's names.
        ("src/test/java/CaptureTest.java", ("src/test/java/CaptureTest.java",)),
        # Nothing that is a file: the test files it writes, as with no line at all.
        ("python -m pytest", ("tests/test_a.py",)),
    ],
)
def test_a_tests_line_yields_the_test_files_it_names(line, tests):
    ticket = parse_tickets(f"### ticket: a\n- writes: a.py, tests/test_a.py\n- tests: {line}\n")[0]

    assert ticket.tests == tests


def test_two_tickets_with_the_command_on_their_tests_line_pass_the_check():
    """A user's flock stopped here: both tickets "owned" a file called `python`."""
    blocks = "".join(
        f"### ticket: {t}\n- writes: {t}.py, tests/test_{t}.py\n- tests: python -m pytest tests/test_{t}.py\n"
        f"- accept: true\n\n"
        for t in ("net", "parser")
    )

    assert check_tickets(parse_tickets(blocks)) == ""


@pytest.mark.parametrize(
    "writes, word",
    [
        ("a.py, python -m pytest tests/test_a.py", "`python`"),
        ("a.py (new), tests/test_a.py", "`(new)`"),
        ("a.py && tests/test_a.py", "`&&`"),
    ],
)
def test_a_writes_line_holding_something_other_than_files_is_refused(writes, word):
    problem = check_tickets(
        parse_tickets(f"### ticket: a\n- writes: {writes}\n- tests: tests/test_a.py\n- accept: true")
    )

    assert word in problem and "which is not a file" in problem


def test_a_writes_line_of_files_without_extensions_passes():
    blocks = "### ticket: a\n- writes: Makefile, Dockerfile, a.c, tests/test_a.c\n- tests: tests/test_a.c\n- accept: true"

    assert check_tickets(parse_tickets(blocks)) == ""


@pytest.mark.parametrize(
    "line, needs",
    [
        ("a", ("a",)),
        ("a (for the socket)", ("a",)),
        ("ticket a", ("a",)),
        ("`a` and b", ("a", "b")),
        ("a — the socket it opens", ("a",)),
        ("b, c.", ("b", "c")),
        ("None.", ()),
    ],
)
def test_a_needs_line_yields_the_ticket_ids_it_names(line, needs):
    assert parse_tickets(f"### ticket: x\n- writes: x.py\n- needs: {line}\n")[0].needs == needs


@pytest.mark.parametrize(
    "writes, tests",
    [
        ("a.py, tests/test_a.py", ("tests/test_a.py",)),
        ("Capture.java, src/test/java/CaptureTest.java", ("src/test/java/CaptureTest.java",)),
        ("capture.go, capture_test.go", ("capture_test.go",)),
        ("src/Capture.hs, test/CaptureSpec.hs", ("test/CaptureSpec.hs",)),
        # Only a directory says so: Rust's integration tests.
        ("src/lib.rs, tests/integration.rs", ("tests/integration.rs",)),
        # A helper beside a real test file is not taken for one.
        ("tests/conftest.py, tests/test_a.py", ("tests/test_a.py",)),
        # A word inside a name is not a test: latest, contest, inspect.
        ("latest.py, contest.py, inspect.py", ()),
    ],
)
def test_without_a_tests_line_the_test_files_are_found_in_writes_in_any_language(writes, tests):
    assert parse_tickets(f"### ticket: a\n- writes: {writes}\n")[0].tests == tests


def test_good_ticket_blocks_pass_the_check():
    assert check_tickets(parse_tickets(_BLOCKS)) == ""


# --------------------------------------------------------------------------- #
# The gate: a write outside the stage's files is refused before anyone is asked
# --------------------------------------------------------------------------- #


def test_ticket_blocks_round_trip_through_their_own_format():
    ticket = TicketSpec(
        id="a",
        writes=("a.py", "t.py"),
        tests=("t.py",),
        accept="pytest t.py",
        needs=("b",),
        builds="x",
        done="y",
    )

    assert parse_tickets(ticket.block())[0] == ticket
