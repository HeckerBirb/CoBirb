"""Hooks: the user's own code, at the points where CoBirb makes a decision.

The permission layer answers "may this run?" by asking a person. That works
until the answer is a rule rather than a judgement — *never* touch anything
under `infra/`, *always* run the formatter after an edit, *tell me* when a turn
finishes because I have gone to make coffee. Those are policies, and asking a
human to re-enact a policy twenty times a session is how people end up
approving things unread.

A hook is a command the user nominates for a lifecycle event:

    {
      "hooks": {
        "before_tool": [
          {"match": "write_file", "command": "~/bin/guard-infra.sh"},
          {"match": "shell",      "command": "~/bin/log-shell.sh"}
        ],
        "after_turn": [{"command": "notify-send 'CoBirb finished'"}]
      }
    }

**A ``before_tool`` hook can refuse.** Non-zero exit blocks the call, and
whatever the hook printed becomes the reason the *model* is given — so a hook
can say "infra/ is off limits, edit the terraform module instead" and the model
adapts, rather than being told a flat no it will simply retry. This is the one
event whose result changes what CoBirb does; every other event is an
observation, where a failing hook is reported and stepped over.

**Hooks come from `~/.cobirb/config.json`, like every other setting**, and a
repository cannot supply one — CoBirb reads no config from a working directory
at all (see ``cobirb.config``). That is worth stating here rather than leaving
implicit: a hook is arbitrary code that runs with no approval prompt in the way,
so a repository able to define one would mean cloning a repository is enough to
execute its author's code.

**The contract.** The hook is run through the shell, with the event as JSON on
stdin:

    {"event": "before_tool", "tool": "write_file",
     "arguments": {"path": "a.py", "content": "..."},
     "cwd": "/home/you/project"}

Exit 0 means proceed. Anything else means "refused" for ``before_tool`` and
"this hook failed" everywhere else. Output is read but never handed to the
model except as that refusal reason: a hook is not a way to inject a prompt.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from ..config import Config

logger = logging.getLogger("cobirb")

# The lifecycle points a hook can attach to. Deliberately few: each one is a
# place the core has to call out to, and an event nobody can act on is a
# maintenance cost with no user.
EVENT_BEFORE_TOOL = "before_tool"
EVENT_AFTER_TOOL = "after_tool"
EVENT_BEFORE_TURN = "before_turn"
EVENT_AFTER_TURN = "after_turn"
EVENTS = (EVENT_BEFORE_TOOL, EVENT_AFTER_TOOL, EVENT_BEFORE_TURN, EVENT_AFTER_TURN)

# A hook sits in front of a tool call the user is waiting on, so it has to be
# quick. Long enough for a script that greps a policy file or shells out to
# git; not long enough to hide a hung command.
DEFAULT_TIMEOUT_SECONDS = 30

# What a refusing hook's own output is trimmed to before the model sees it. A
# hook that accidentally cats a file should cost a truncated message, not a
# turn's worth of window.
_MAX_REASON_CHARS = 2000


@dataclass(frozen=True)
class Hook:
    """One configured command, and what it applies to."""

    event: str
    command: str
    # Glob against the tool name, for the two tool events. Empty matches
    # everything, which is also what the turn events always do.
    match: str = "*"
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    def applies_to(self, tool_name: str | None) -> bool:
        if tool_name is None or not self.match:
            return True
        return fnmatch.fnmatch(tool_name, self.match)


@dataclass
class HookOutcome:
    """What running the hooks for one event came to.

    ``blocked`` is only ever true for ``before_tool``; ``reason`` is the
    refusing hook's own words, which is the whole point of letting it print.
    """

    blocked: bool = False
    reason: str = ""
    failures: list[str] = field(default_factory=list)


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_REASON_CHARS:
        return text
    return text[:_MAX_REASON_CHARS] + f"\n[…{len(text) - _MAX_REASON_CHARS} more characters]"


def load_hooks(config: Config) -> list[Hook]:
    """Read the ``hooks`` block from the configuration.

    Malformed entries are skipped with a log line rather than raising: this
    runs during wiring, and a stray key in one hook should not cost the
    session. An entry with no ``command`` is the common typo and is silently
    the same as not writing it.
    """
    block = config.get("hooks", default={}) or {}
    if not isinstance(block, dict):
        logger.warning("ignoring \"hooks\": expected an object of event -> list")
        return []

    hooks: list[Hook] = []
    for event, entries in block.items():
        if event not in EVENTS:
            logger.warning("ignoring hook event %r; known events: %s", event, ", ".join(EVENTS))
            continue
        if isinstance(entries, (str, dict)):
            entries = [entries]
        if not isinstance(entries, list):
            logger.warning("ignoring hooks for %r: expected a list", event)
            continue
        for entry in entries:
            # A bare string is the common case — a command with no filter —
            # and making people write {"command": "..."} for it is friction
            # with nothing on the other side of it.
            if isinstance(entry, str):
                entry = {"command": entry}
            if not isinstance(entry, dict) or not entry.get("command"):
                logger.warning("ignoring a %s hook with no command", event)
                continue
            try:
                timeout = int(entry.get("timeout", DEFAULT_TIMEOUT_SECONDS))
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT_SECONDS
            hooks.append(
                Hook(
                    event=event,
                    command=str(entry["command"]),
                    match=str(entry.get("match", "*")) or "*",
                    timeout=max(1, timeout),
                )
            )
    return hooks


class HookRunner:
    """Runs the configured hooks for an event. Cheap and safe when empty.

    Held by the orchestrator, which calls ``fire()`` at each lifecycle point.
    ``None`` is not needed as a "hooks are off" signal — a runner with no
    hooks returns immediately, which keeps the call sites free of ``if
    self.hooks is not None``.
    """

    def __init__(self, hooks: list[Hook] | None = None, cwd: str = ".") -> None:
        self.hooks = hooks or []
        self.cwd = cwd

    @classmethod
    def from_config(cls, config: Config, cwd: str = ".") -> "HookRunner":
        return cls(load_hooks(config), cwd)

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def has(self, event: str) -> bool:
        return any(hook.event == event for hook in self.hooks)

    def fire(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HookOutcome:
        """Run every hook attached to ``event`` that matches, in order.

        Stops at the first refusal for ``before_tool`` — there is nothing to
        gain from asking the remaining hooks whether they also object to
        something that is not going to happen. Every other event runs them all
        and collects failures, because those are observations and one failing
        observer should not silence the rest.
        """
        outcome = HookOutcome()
        if not self.hooks:
            return outcome

        event_json = json.dumps(
            {
                "event": event,
                "tool": tool_name,
                "arguments": arguments or {},
                "cwd": self.cwd,
                **(payload or {}),
            }
        )
        for hook in self.hooks:
            if hook.event != event or not hook.applies_to(tool_name):
                continue
            code, output = self._run(hook, event_json)
            if code == 0:
                continue
            if event == EVENT_BEFORE_TOOL:
                outcome.blocked = True
                outcome.reason = output or f"a {event} hook refused it (exit {code})"
                return outcome
            outcome.failures.append(f"{hook.command} exited {code}")
        return outcome

    def _run(self, hook: Hook, event_json: str) -> tuple[int, str]:
        """Execute one hook. Returns ``(exit code, combined output)``.

        A hook that cannot be run at all is treated as a refusal (a non-zero
        code), not as silence. The failure modes here — a missing script, a
        timeout — are exactly the ones where a hook that was meant to be
        guarding something is not guarding it, and failing open there would
        make the guard worthless precisely when it broke.
        """
        try:
            completed = subprocess.run(
                os.path.expanduser(hook.command),
                shell=True,
                cwd=self.cwd,
                input=event_json,
                capture_output=True,
                text=True,
                timeout=hook.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("hook timed out after %ss: %s", hook.timeout, hook.command)
            return 124, f"the hook timed out after {hook.timeout}s"
        except OSError as exc:
            logger.warning("hook could not be run: %s — %s", hook.command, exc)
            return 126, f"the hook could not be run: {exc}"
        return completed.returncode, _clip(f"{completed.stdout}{completed.stderr}")
