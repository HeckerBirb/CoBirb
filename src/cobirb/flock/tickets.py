"""Tickets: the blocks the overview and each evaluation write, read and checked.

A ticket arrives as Markdown a model wrote — a ``### ticket: <id>`` heading
and a list of fields — and leaves as a ``TicketSpec`` the charter is built
from. Everything between is here: reading the fields the way models actually
write them (bold keys, a command on the ``tests`` line, remarks in
parentheses), checking that the programs a ticket's ``accept`` names exist on
the machine it runs on, and running the checks for what it needs installed.

Split from ``stages`` because none of it runs a stage: it is parsing and
checking, testable without an orchestrator, and the stages call it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..orchestrator import Orchestrator
from ..policy import command_segments
from ..remote.osnames import canonical_os, local_os
from .charter import CharterError
from .plan import PlanDraft

_TICKET_HEADING = re.compile(r"^\s{0,4}#{2,4}\s*ticket\s*[:：]\s*`?([A-Za-z0-9_.-]+)`?\s*$", re.I | re.M)
# The key may be bold, as models often write it in Markdown — `**accept**:` or
# `**accept:**`. Without that the whole line was skipped, and a ticket written
# that way was refused for having no files and no check.
_FIELD = re.compile(r"^\s*[-*]\s*(?:\*\*|__)?([a-z ]+?)(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$", re.I)


@dataclass
class TicketSpec:
    """One ticket as the overview (or an evaluation) describes it."""

    id: str
    writes: tuple[str, ...]
    tests: tuple[str, ...]
    accept: str
    needs: tuple[str, ...] = ()
    builds: str = ""
    done: str = ""
    # The OS family this ticket is built and tested on, when not this machine's
    # (a Remote Worker Birb). A name CoBirb does not know is kept as written, so
    # check_tickets can say so.
    runs_on: str = ""
    # No machine here can build or test it and the user chose to go on: written
    # only, with no check (see WorkerBrief.static).
    static: bool = False
    # What must be installed on this machine: (what, check, install) — the
    # command that says whether it is, and the one that would install it.
    # CoBirb runs the check (see check_requirements) and only shows the install.
    requires: tuple[tuple[str, str, str], ...] = ()

    def block(self) -> str:
        """The ticket in the same form the overview uses."""
        return "\n".join(
            [
                f"### ticket: {self.id}",
                f"- writes: {', '.join(self.writes)}",
                f"- tests: {', '.join(self.tests)}",
                f"- accept: {self.accept}",
                *([f"- runs on: {self.runs_on}"] if self.runs_on else []),
                *(
                    f"- requires: {what} — check: {check}" + (f" — install: {install}" if install else "")
                    for what, check, install in self.requires
                ),
                f"- needs: {', '.join(self.needs) or 'none'}",
                f"- builds: {self.builds}",
                f"- done when: {self.done}",
            ]
        )


def _paths(value: str) -> tuple[str, ...]:
    parts = [p.strip().strip("`").strip() for p in re.split(r"[,\s]+", value) if p.strip()]
    return tuple(os.path.normpath(p) for p in parts if p and p.lower() != "none")


_SHELL_PUNCTUATION = frozenset("&|;<>()")


def _command_word(word: str) -> bool:
    """A word on a file list that is part of a command, not a file: an option,
    shell punctuation, or a bare word naming an installed program."""
    return (
        word.startswith("-")
        or set(word) <= _SHELL_PUNCTUATION
        or ("/" not in word and "." not in word and shutil.which(word) is not None)
    )


def _test_paths(value: str) -> tuple[str, ...]:
    """The test files a `tests` line names — even when it holds the command
    that runs them, or a remark in parentheses.

    **Models put the command here.** `- tests: python -m pytest tests/test_x.py`
    was split into four "files", and two tickets written that way both owned
    one called `python`: the flock stopped on a file-ownership refusal that
    sent Brainy Birb reshuffling files three times over the wrong problem.
    What was meant is plain, so the words that are not files are dropped: a
    command word, or anything with neither a `.` nor a `/` (a test file has an
    extension or a directory, in every language).

    A command also names files that are not tests: `cc -o /tmp/t capture.c
    tests/test_capture.c && /tmp/t` names the build output and the code under
    test. So from a command only project files are kept, and of those the ones
    named as tests (``_named_as_tests``) when there are any.
    """
    words = _paths(re.sub(r"\([^)]*\)", " ", value))
    files = [w for w in words if not _command_word(w) and ("." in w or "/" in w)]
    if len(files) == len(words):
        return tuple(files)  # a plain list of files
    files = [w for w in dict.fromkeys(files) if not os.path.isabs(w)]
    return tuple(_named_as_tests(files) or files)


_NEED_ID = re.compile(r"^(?:ticket(?:\s*[:：]\s*|\s+))?[`'\"]*([A-Za-z0-9_.-]+)", re.I)


def _need_ids(value: str) -> tuple[str, ...]:
    """The ticket ids a `needs` line names.

    Models add to them — `a (for the socket)`, `ticket a`, `a and b` — and
    each extra word used to become part of an id no ticket had, refused as
    unknown. The id is the leading name of each entry (the same characters a
    `### ticket:` heading allows); a remark in parentheses is dropped.
    """
    ids = []
    for entry in re.split(r"[,;]|\band\b", re.sub(r"\([^)]*\)", " ", value)):
        match = _NEED_ID.match(entry.strip())
        need = match.group(1).rstrip(".") if match else ""  # "b." ends a sentence, not an id
        if need and need.lower() != "none":
            ids.append(need)
    return tuple(dict.fromkeys(ids))


_TEST_WORD = re.compile(r"^(tests?|specs?)$", re.I)


def _name_words(name: str) -> list[str]:
    """``CaptureTest.java`` → Capture, Test, java; ``test_x.py`` → test, x, py."""
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)


def _named_as_tests(paths: list[str] | tuple[str, ...]) -> list[str]:
    """The paths named as tests, in any language's convention.

    A whole word of the file name — `test_x.py`, `x_test.go`, `CaptureTest.java`,
    `CaptureSpec.hs` — and never a substring, which would take `latest.c` or
    `inspect.py`. Only when no file name says so, a file under a `test`,
    `tests` or `spec` directory (Rust's `tests/integration.rs`), so a helper
    beside the tests (`tests/conftest.py`) is not taken for one when a real
    test file is there.
    """
    by_name = [p for p in paths if any(_TEST_WORD.match(w) for w in _name_words(os.path.basename(p)))]
    if by_name:
        return by_name
    return [p for p in paths if any(_TEST_WORD.match(part) for part in p.replace("\\", "/").split("/")[:-1])]


def parse_tickets(text: str) -> list[TicketSpec]:
    """Every ticket block in ``text``; tolerant of spacing, backticks and case."""
    heads = list(_TICKET_HEADING.finditer(text))
    tickets = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        fields: dict[str, str] = {}
        requires: list[tuple[str, str, str]] = []
        for line in text[head.end() : end].splitlines():
            match = _FIELD.match(line)
            if match and match.group(1).strip().lower() == "requires":
                requires.append(_requirement(match.group(2)))
            elif match:
                fields[match.group(1).strip().lower()] = match.group(2).strip()
        needs = _need_ids(fields.get("needs", ""))
        writes = _paths(fields.get("writes", ""))
        # A block with no `tests` line whose `writes` holds its test files
        # means them: the checklist says a ticket writes its own tests. A
        # user's flock stopped on a ticket listing
        # `tests/test_communication_protocol.py` under `writes` only.
        tests = _test_paths(fields.get("tests", "")) or tuple(_named_as_tests(writes))
        tickets.append(
            TicketSpec(
                id=head.group(1),
                writes=writes,
                tests=tests,
                accept=fields.get("accept", "").strip().strip("`"),
                needs=needs,
                builds=fields.get("builds", ""),
                done=fields.get("done when", fields.get("done", "")),
                requires=tuple(requires),
                runs_on=_runs_on(fields.get("runs on", "")),
            )
        )
    return tickets


_CHECK = re.compile(r"^(.*?)[\s,;—–-]*\bcheck\s*:\s*(.*)$", re.I)
# Only whitespace, a comma or semicolon, or a long dash before `install:` —
# never a plain hyphen, which a check command can end with (`gcc -x c -`).
_INSTALL = re.compile(r"\s*[,;]?\s*(?:[—–]\s*)?\binstall\s*:\s*", re.I)


def _requirement(value: str) -> tuple[str, str, str]:
    """``zlib — check: pkg-config --exists zlib — install: sudo apt install zlib1g-dev``
    → (what, check, install)."""
    match = _CHECK.match(value.strip())
    if not match:
        return value.strip().strip("`"), "", ""
    parts = _INSTALL.split(match.group(2), maxsplit=1)
    check = parts[0].strip().strip("`")
    install = parts[1].strip().strip("`") if len(parts) > 1 else ""
    return match.group(1).strip().strip("`"), check, install


# Shell builtins with no program of their own on PATH. `cd` is the one an
# acceptance command really uses (`cd sub && pytest`); the rest are here so a
# plausible command is not refused for a word the shell answers itself.
_BUILTINS = frozenset(
    {
        "cd",
        "export",
        "set",
        "source",
        ".",
        "exit",
        # cmd.exe's own, for a Windows remote's checks
        "echo",
        "dir",
        "del",
        "copy",
        "type",
        "mkdir",
        "md",
        "rmdir",
        "rd",
        "move",
        "ren",
        "call",
        "start",
        "cls",
        "pushd",
        "popd",
    }
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Named in a refusal when they are on the ticket's machine. Told only that its
# cross-compiler was missing on a Windows remote, a model guessed a different
# missing program on every retry and the overview failed; told what is there,
# it has something real to choose from. A hint, not a list of what may be used.
TOOLCHAINS = (
    "cc",
    "gcc",
    "g++",
    "clang",
    "clang++",
    "cl",
    "zig",
    "make",
    "cmake",
    "cargo",
    "go",
    "dotnet",
    "javac",
    "python",
    "python3",
    "py",
    "node",
    "npm",
)


def _accept_problem(command: str, which: Callable[[str], Any] = shutil.which, windows: bool = False) -> str:
    """Why ``command`` cannot run here, or "" if every program it names exists.

    **Checked when the overview is written, because nothing later would.** A
    user's flock went to approval with `c bacon_main.c tests/test_bacon_main.c`
    — the ticket template's `python …` example with the language's name put
    where the program goes. Its worker could run only the programs its check
    names, so it spent 43 turns refused and never wrote a line.

    Read the way the worker's grant is (``policy.command_segments``): a command that
    scan cannot read grants the worker nothing, so it could never be run. A
    program given as a path is not looked for — the command may build it
    (`cc -o /tmp/t … && /tmp/t`).

    For a Remote Worker Birb's ticket, ``which`` asks its remote and
    ``windows`` reads the command Windows' way.
    """
    segments = command_segments(command, windows=windows)
    if not segments:
        return (
            "cannot be read (command substitution, a subshell or unbalanced quotes), "
            "so the Worker Birb could not be allowed to run it. Write it as plain commands "
            "joined with `&&`"
        )
    for words in segments:
        program = next((w for w in words if not _ASSIGNMENT.match(w)), "")
        if not program or "/" in program or "\\" in program or program.lower() in _BUILTINS:
            continue
        if not which(program):
            present = [t for t in TOOLCHAINS if t != program and which(t)]
            there = (
                f" Programs for building and testing that are installed there: {', '.join(present)}."
                if present
                else ""
            )
            return (
                f"names `{program}`, which is not a program installed on the machine this "
                f"ticket runs on.{there} Name the real program that runs this ticket's tests"
            )
    return ""


REQUIREMENT_TIMEOUT = 30
INSTALLED, MISSING, UNCHECKED = "installed", "MISSING", "not checked"


def check_requirements(
    main: Orchestrator,
    tickets: list[TicketSpec],
    cwd: str,
    cache: dict[str, str] | None = None,
    remotes: Any = None,
) -> list[tuple[str, str, str, str]]:
    """Each ticket's requirements, run: ``(ticket id, what, status, install)``.

    **The checks are Brainy Birb's commands, so they run only where a
    contained command already runs without asking** — inside the sandbox, on
    the main agent's policy with ``sandbox_auto``. Anywhere else nothing is
    run, and each is reported as not checked: guessing "installed" would hide
    the one thing this exists to show. A check that fails, times out or cannot
    start counts as missing only when it ran; anything that stopped it running
    at all is not checked. Only a check that passed is remembered in
    ``cache``: something missing is looked for again next round, since the
    user may have installed it in between.
    """
    box = getattr(main.tools.get("shell"), "sandbox", None)
    runnable = requirements_checkable(main)
    cache = {} if cache is None else cache
    results = []
    for ticket in tickets:
        if ticket.static:
            continue  # nothing will build it, so nothing it needs is looked for
        if ticket.runs_on and ticket.runs_on != local_os():
            results += _remote_requirements(ticket, remotes, cache)
            continue
        for what, check, install in ticket.requires:
            status = cache.get(check, UNCHECKED)
            if status != INSTALLED and runnable:
                try:
                    done = subprocess.run(
                        box.argv(check, cwd),
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=REQUIREMENT_TIMEOUT,
                    )
                    status = INSTALLED if done.returncode == 0 else MISSING
                except subprocess.TimeoutExpired:
                    status = MISSING
                except OSError:  # the sandbox itself would not start: nothing was checked
                    status = UNCHECKED
                if status == INSTALLED:
                    cache[check] = status
            results.append((ticket.id, what, status, install))
    return results


def _remote_requirements(
    ticket: TicketSpec, remotes: Any, cache: dict[str, str]
) -> list[tuple[str, str, str, str]]:
    """A Remote Worker Birb's ticket's requirements, checked on its remote.

    The remote runs the check in a scratch directory of its own; it is the
    machine the ticket will be built on, so it is the one that counts."""
    client = remotes.any_for(ticket.runs_on) if remotes is not None else None
    results = []
    for what, check, install in ticket.requires:
        key = f"{ticket.runs_on}:{check}"
        status = cache.get(key, UNCHECKED)
        if status != INSTALLED and client is not None:
            try:
                answer = client.request(
                    "run", command=check, seconds=REQUIREMENT_TIMEOUT, timeout=REQUIREMENT_TIMEOUT + 30
                )
                status = INSTALLED if answer.get("ok") else MISSING
            except Exception:  # noqa: BLE001 - a remote that cannot answer leaves it unchecked
                status = UNCHECKED
            if status == INSTALLED:
                cache[key] = status
        results.append((ticket.id, what, status, install))
    return results


def requirements_checkable(main: Orchestrator) -> bool:
    """Whether ``check_requirements`` can run anything here (see its docstring)."""
    box = getattr(main.tools.get("shell"), "sandbox", None)
    return bool(box is not None and box.active and getattr(main.policy, "sandbox_auto", False))


def describe_requirements(results: list[tuple[str, str, str, str]]) -> str:
    """The requirements that are not known to be installed, for the user; "" if none.

    With the command that would install each, as Brainy Birb wrote it for this
    machine. CoBirb never runs it: installing is the user's decision, often
    needs `sudo`, and reaches the network.
    """
    open_ = [row for row in results if row[2] != INSTALLED]
    if not open_:
        return ""
    lines = ["Needed on this machine, and not found installed:"]
    for tid, what, status, _ in open_:
        lines.append(f"  {tid}: {what} — {status}")
    installs = list(dict.fromkeys(install for *_, install in open_ if install))
    if installs:
        lines += ["", "To install them (CoBirb does not run these):"] + [f"  {cmd}" for cmd in installs]
    if any(status == UNCHECKED for _, _, status, _ in open_):
        lines.append(
            "\n  (not checked: requirement checks run only inside the sandbox, where commands "
            "run without asking)"
        )
    return "\n".join(lines)


# What `which_for` answers for an OS no remote can speak for: its programs
# cannot be looked up anywhere, so they are not — the user decides about such a
# ticket before anything is built (run._static_or_stop).
UNCHECKABLE = object()


def _runs_on(value: str) -> str:
    """A `runs on` value as its OS family, or as written if CoBirb does not
    know it (check_tickets then says so); "" for none."""
    value = value.strip().strip("`").strip()
    if not value or value.lower() == "none":
        return ""
    return canonical_os(value) or value


def check_tickets(tickets: list[TicketSpec], which_for: Callable[[str], Any] | None = None) -> str:
    """Why these tickets cannot become a charter, or "" if they can.

    Run through a scratch ``PlanDraft`` — the same checks the charter moves
    apply — so a ticket table that overlaps is caught while the overview is
    still being written, with one path named, rather than after the skeleton.

    ``which_for(os)`` says how to look up programs for a ticket that runs on
    another OS: ``(which, windows)`` for a remote that can answer, or
    ``UNCHECKABLE``. Without it, every ticket is checked against this machine.
    """
    if not tickets:
        return "no ticket blocks were found — each needs a `### ticket: <id>` heading"
    # Before ownership: a command on the `writes` line splits into words that
    # every such ticket "owns", and the ownership refusal would send the model
    # after the wrong problem. Refused rather than salvaged, unlike `tests`:
    # a wrong guess here would give a worker the wrong files to write.
    for ticket in tickets:
        word = next((w for w in ticket.writes if _command_word(w) or "(" in w or ")" in w), "")
        if word:
            return (
                f"ticket {ticket.id!r}: its `writes` line has `{word}`, which is not a file. "
                "`writes` lists only the files this ticket creates or changes, comma-separated, "
                "e.g. `- writes: src/x.py, tests/test_x.py`; the command that runs its tests "
                "goes on `accept`"
            )
    # A file in two tickets, said in the overview's own terms. The charter
    # moves' refusal ("call drop_worker…") names a tool no overview stage has,
    # and every model in the first overnight run that met it failed the same
    # way twice — most often by listing another ticket's test file under its
    # own `tests`, meaning "the tests I must pass".
    owners: dict[str, list[str]] = {}
    for ticket in tickets:
        for path in dict.fromkeys((*ticket.writes, *ticket.tests)):
            owners.setdefault(path, []).append(ticket.id)
    for path, who in owners.items():
        if len(who) > 1:
            return (
                f"`{path}` is listed by more than one ticket ({', '.join(repr(w) for w in who)}). "
                "Every file belongs to exactly one ticket: keep it only in the ticket that writes it. "
                "A ticket's `tests` are its own test files, never another ticket's"
            )
    draft = PlanDraft()
    for ticket in tickets:
        if not ticket.tests:
            return (
                f"ticket {ticket.id!r} has no test files: add a `- tests:` line naming them "
                "(e.g. `- tests: tests/test_x.py`), and list them in `writes` too"
            )
        if ticket.runs_on and canonical_os(ticket.runs_on) is None:
            return (
                f"ticket {ticket.id!r}: `runs on: {ticket.runs_on}` is not an OS name CoBirb knows "
                "— use the OS exactly as the machine facts name it, or leave the line out"
            )
        if ticket.static:
            continue  # the user chose to go on without building or testing it
        if not ticket.accept:
            return f"ticket {ticket.id!r} has no `accept` command"
        which: Callable[[str], Any] | None = shutil.which
        windows = False
        if which_for is not None and ticket.runs_on and ticket.runs_on != local_os():
            target = which_for(ticket.runs_on)
            if target is UNCHECKABLE:
                which = None  # nothing can look it up; the user decides about this ticket
            else:
                which, windows = target
        if which is not None:
            problem = _accept_problem(ticket.accept, which, windows)
            if problem:
                return f"ticket {ticket.id!r}: its `accept` command `{ticket.accept}` {problem}"
        for what, check, _install in ticket.requires:
            if not check:
                return (
                    f"ticket {ticket.id!r}: its requirement `{what}` has no check — write it as "
                    f"`- requires: {what} — check: <a command that succeeds only if it is installed>`"
                )
            if which is None:
                continue
            problem = _accept_problem(check, which, windows)
            if problem:
                return f"ticket {ticket.id!r}: the check for `{what}`, `{check}`, {problem}"
        try:
            draft.add_worker(
                ticket.id,
                brief="-",
                writes=list(ticket.writes),
                accept=ticket.accept,
                tests=list(ticket.tests),
                needs=list(ticket.needs),
            )
        except CharterError as exc:
            return str(exc)
    try:
        draft.seal("check")
    except CharterError as exc:
        return str(exc)
    return ""


_LEFT_TO_DO = re.compile(
    r"^\s*[-*]\s*(?:\*\*|__)?left to do(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.+?)\s*$", re.I | re.M
)


def left_to_do(text: str) -> list[str]:
    """The `- left to do:` lines of an evaluation: what the user must still do."""
    return [match.group(1) for match in _LEFT_TO_DO.finditer(text)]


def evaluation_why(text: str) -> dict[str, str]:
    """The `- why:` line under each ticket block, by ticket id."""
    whys: dict[str, str] = {}
    heads = list(_TICKET_HEADING.finditer(text))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        for line in text[head.end() : end].splitlines():
            match = _FIELD.match(line)
            if match and match.group(1).strip().lower() == "why":
                whys[head.group(1)] = match.group(2).strip()
    return whys
