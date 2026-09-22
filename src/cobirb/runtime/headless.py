"""Running CoBirb unattended: no prompts, machine-readable output.

Default-deny made unattended runs impossible, which was the right trade for
an interactive tool and the wrong one for a pipeline. This is the sanctioned
way back: state up front what is permitted (``allow_tools`` in config, or
``--allow-tool``), and anything outside that is refused immediately rather
than blocking on a prompt nobody is there to answer.

Deliberately **not** a flag that turns the safety model off. There is no
``--yes``. The difference matters: a policy file is a decision someone made
once, reviewably, in a file their colleagues can read; a blanket approval flag
is a decision nobody made at all.

Exit codes are the part CI actually consumes, so they distinguish the three
outcomes that call for different responses:

===  ==========================================================
0    Completed. Everything the model tried was permitted.
1    Failed — the provider was unreachable, a session would not
     open, the run could not finish.
2    Completed, but something was refused. The answer may still
     be useful; the permissions probably need widening. Only
     ever returned by ``--headless``: a person who answered "no"
     at a prompt got what they asked for, and reporting that as
     a non-zero exit would make an ordinary decision look like a
     broken script.
===  ==========================================================
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..typing.spi import DECISION_DENY, I_OAdapter

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DENIED = 2


class HeadlessIO(I_OAdapter):
    """An adapter for when there is nobody to ask and nothing to draw.

    Refuses every approval outright rather than reaching for ``input()``. In
    a pipeline the terminal prompt would either block forever or read EOF and
    deny anyway — this makes that explicit, and does it without printing a
    question no one will see.
    """

    def __init__(self) -> None:
        self.denied: list[str] = []

    def name(self) -> str:
        return "headless"

    def render(self, text: str) -> None:
        return None  # streamed tokens have nowhere useful to go

    def listen(self) -> str | None:
        return None

    def view(self, data: bytes, mime: str | None = None) -> None:
        return None

    def confirm(self, tool_name: str, arguments: dict[str, Any]) -> str:
        self.denied.append(tool_name)
        return DECISION_DENY


@dataclass
class HeadlessResult:
    """The outcome of an unattended run, in a shape a script can read."""

    ok: bool
    summary: str
    validation: str | None = None
    turns: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    error: str | None = None
    session_path: str | None = None
    context: dict[str, Any] | None = None
    # Why the run ended: "answered", or a STOP_* reason from the orchestrator.
    stop_reason: str | None = None

    def exit_code(self, *, unattended: bool = True) -> int:
        """The process exit code for this run.

        ``unattended`` is what decides whether a refusal is worth a non-zero
        code. In a pipeline it is a signal that the permissions need widening,
        which is the whole reason for a distinct code. When a person is
        sitting there and answered "no" themselves, it is not a failure — they
        got exactly what they asked for, and returning 2 would make an
        ordinary interactive decision look like a broken script.
        """
        if not self.ok:
            return EXIT_ERROR
        if unattended and self.denied:
            return EXIT_DENIED
        return EXIT_OK

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "summary": self.summary,
                "validation": self.validation,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "denied": sorted(set(self.denied)),
                "error": self.error,
                "session": self.session_path,
                "context": self.context,
                "stop_reason": self.stop_reason,
            },
            indent=2,
            ensure_ascii=False,
        )


def describe_context(orchestrator: Any) -> dict[str, Any] | None:
    """The context budget as plain data, for the JSON report.

    Worth including: a headless run that quietly compacted half its history
    away is something whoever reads the log needs to be able to see.
    """
    report = getattr(orchestrator, "last_compaction", None)
    if report is None:
        return None
    return {
        "estimated_tokens": report.estimated_tokens,
        "budget_tokens": report.budget_tokens,
        "compacted": report.changed,
        "turns_kept": report.kept_turns,
        "turns_total": report.total_turns,
    }
