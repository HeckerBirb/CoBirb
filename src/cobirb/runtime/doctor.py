"""``cobirb doctor`` — the checks that answer "am I ready to go?".

Every one of these already fails somewhere today. What they lack is a place to
fail *early*, together, in one voice: a mistyped config key fails by silently
doing nothing, a model that was never pulled fails mid-turn, and a checkout
left on a detached ``HEAD`` fails by swallowing the next commit made in it.

Three groups, cheapest first, because the cheap ones are also the ones most
likely to be wrong:

1. **Config** — parses, every key is real, values have the right shape, and
   the things it points at exist.
2. **Environment** — the endpoint answers, the named models are actually
   there, and they can do what this configuration asks of them.
3. **Install** — which of the three install shapes this is (so you know
   whether ``--upgrade`` can work at all), the version, and for a checkout
   whether it is in a state that can receive a commit.

Nothing here changes anything. A check that repaired what it found would be a
different command with a different name, and a much larger promise.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import paths
from ..config import Config
from ..policy import READ_TOOLS, WRITE_TOOLS
from . import models as model_roles

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Every key CoBirb reads from config.json. A key that is not here is either a
# typo or a setting that no longer exists — both worth saying out loud, since
# the alternative is that it silently does nothing (`Config.get` is a plain
# lookup with no validation, so `redact_secret` for `redact_secrets` reads as
# "off" while redaction stays on).
KNOWN_KEYS = frozenset({
    "models", "system_prompt", "plan_mode",
    "allow_tools", "allow_read_dirs", "allow_write_dirs",
    "checkpoints", "redact_secrets", "audit_log",
    "instructions", "instructions_max_chars",
    "repo_map", "repo_map_max_chars", "context_tokens", "max_num_ctx",
    "verify_command", "verify_timeout", "verify_fix_attempts",
    "hooks", "mcp_servers", "plugins", "max_turns", "sandbox",
    "connect_timeout", "request_timeout", "flock_planning",
})

# What each key should look like, for the shape check. Only the keys whose
# type being wrong would misbehave quietly rather than raise.
_EXPECTED_TYPES: dict[str, tuple[type, ...]] = {
    "models": (dict,), "plugins": (dict,), "hooks": (dict,), "mcp_servers": (dict,),
    "allow_tools": (list,), "allow_read_dirs": (list,), "allow_write_dirs": (list,),
    "checkpoints": (bool,), "redact_secrets": (bool,), "audit_log": (bool,),
    "instructions": (bool,), "repo_map": (bool,), "plan_mode": (bool,),
    "instructions_max_chars": (int,), "repo_map_max_chars": (int,),
    "context_tokens": (int,),
    # Also a string, for the "64k" spelling — see models.parse_context_size.
    # Whether that string *says* anything is checked below, since a type is
    # all this table can ask about.
    "max_num_ctx": (int, str),
    "sandbox": (str, dict),
    "flock_planning": (str,),
    "verify_timeout": (int,), "verify_fix_attempts": (int,), "max_turns": (int,),
    "system_prompt": (str,), "verify_command": (str,),
}

# Settings CoBirb used to read and no longer does, with what happened to each.
# Reported apart from unknown keys: "not a setting CoBirb reads" invites
# someone to hunt for the typo in a key that was spelled correctly.
RETIRED_KEYS = {
    "persona": "personas were removed in 0.22.0; delete this key",
}

# Settings still read, under a name that is on its way out.
DEPRECATED_KEYS = {
    "model": "an older name for models.default.name — move it there",
    "default_model": "an older name for models.default.name — move it there",
}

_TOOL_NAMES = READ_TOOLS | WRITE_TOOLS | {"shell"}


@dataclass
class Check:
    """One question, its answer, and what to do about it."""

    name: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    """Everything the checks found."""

    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        """Whether anything outright failed. A warning is not a failure — it
        is something worth knowing that does not stop a turn working."""
        return not any(check.failed for check in self.checks)

    def describe(self) -> str:
        marks = {OK: "✓", WARN: "!", FAIL: "✗"}
        lines = [
            f"  {marks.get(check.status, '?')} {check.name}"
            + (f" — {check.detail}" if check.detail else "")
            for check in self.checks
        ]
        failures = sum(1 for check in self.checks if check.failed)
        warnings = sum(1 for check in self.checks if check.status == WARN)
        if failures:
            summary = f"{failures} problem(s) to fix"
        elif warnings:
            summary = f"ready, with {warnings} thing(s) worth knowing"
        else:
            summary = "ready to go"
        return "\n".join(lines) + f"\n\n{summary}."


# --------------------------------------------------------------------------- #
# 1. Config
# --------------------------------------------------------------------------- #
def _check_config(report: Report, config: Config, raw: "dict[str, Any] | None") -> None:
    path = paths.config_path()
    if raw is None:
        report.add("config file", WARN, f"none at {path} — defaults apply")
        return
    report.add("config file", OK, path)

    retired = sorted(set(raw) & set(RETIRED_KEYS))
    if retired:
        report.add(
            "retired config keys",
            WARN,
            "; ".join(f"{key}: {RETIRED_KEYS[key]}" for key in retired),
        )
    deprecated = sorted(set(raw) & set(DEPRECATED_KEYS))
    if deprecated:
        report.add(
            "deprecated config keys",
            WARN,
            "; ".join(f"{key}: {DEPRECATED_KEYS[key]}" for key in deprecated),
        )
    unknown = sorted(set(raw) - KNOWN_KEYS - set(RETIRED_KEYS) - set(DEPRECATED_KEYS))
    if unknown:
        report.add(
            "config keys",
            FAIL,
            f"not settings CoBirb reads: {', '.join(unknown)}. A key it does not know is "
            "ignored in silence, so this is doing nothing at all",
        )
    else:
        report.add("config keys", OK, f"{len(raw)} recognised")

    wrong = [
        f"{key} should be {' or '.join(t.__name__ for t in expected)}"
        for key, expected in _EXPECTED_TYPES.items()
        if key in raw and not isinstance(raw[key], expected)
        # bool is an int in Python; an int key given True is still wrong.
        or (key in raw and expected == (int,) and isinstance(raw[key], bool))
    ]
    if wrong:
        report.add("config value types", FAIL, "; ".join(wrong))
    else:
        report.add("config value types", OK)

    _check_referenced_things(report, raw)


def _check_referenced_things(report: Report, raw: dict[str, Any]) -> None:
    """Config can be perfectly well-formed and still point at nothing."""
    problems: list[str] = []

    for rule in raw.get("allow_tools") or []:
        name = str(rule).split("(")[0].strip()
        if name and name not in _TOOL_NAMES:
            problems.append(f"allow_tools names '{name}', which is not a built-in tool")

    for key in ("allow_read_dirs", "allow_write_dirs"):
        for directory in raw.get(key) or []:
            if not os.path.isdir(os.path.expanduser(str(directory))):
                problems.append(f"{key} names '{directory}', which is not a directory")

    # A string here is allowed so that "64k" can be written, which means a
    # string that isn't a size is the one way this key can be well-typed and
    # still say nothing. Uncapped-in-silence is exactly the outcome someone
    # setting it was trying to avoid.
    if "max_num_ctx" in raw and model_roles.parse_context_size(raw["max_num_ctx"]) is None:
        problems.append(
            f"max_num_ctx is '{raw['max_num_ctx']}', which is not a size — "
            "write 65536 or 64k. No cap is being applied"
        )

    if problems:
        report.add("config references", WARN, "; ".join(problems))
    else:
        report.add("config references", OK)


# --------------------------------------------------------------------------- #
# 2. Environment
# --------------------------------------------------------------------------- #
def canonical_model(name: str) -> str:
    """A model name as the endpoint lists it.

    Ollama implies ``:latest`` when a tag is omitted — ``gemma4`` and
    ``gemma4:latest`` are the same model, and both are accepted — but
    ``list_models`` reports the fully-qualified form. Comparing the two as raw
    strings therefore reports a perfectly working configuration as missing,
    and tells the user to pull a model they already have.

    Only the last path segment is examined for the tag separator, so a model
    served from a registry with a port (``registry.example.com:5000/thing``)
    is not mistaken for one that already carries a tag.
    """
    name = name.strip()
    if not name:
        return name
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _check_models(report: Report, config: Config, build_provider: Callable[[str], Any]) -> None:
    """The endpoint answers, and the models named are actually pulled.

    These are the checks that otherwise fail mid-turn, which is the worst
    moment to learn a model was never downloaded.
    """
    try:
        provider = build_provider(model_roles.ROLE_ORCHESTRATOR)
        available = provider.list_models()
    except Exception as exc:  # noqa: BLE001 - an unreachable endpoint is an answer
        report.add("model endpoint", FAIL, f"not reachable — {exc}")
        return
    report.add("model endpoint", OK, f"{len(available)} model(s) available")

    for role in model_roles.ROLES:
        # The configured name comes from `resolve_role`, which is what
        # `cobirb models` already reports — the provider keeps its own name
        # private, and asking it would be reaching past the front door.
        try:
            spec = model_roles.resolve_role(role, config)
            role_provider = build_provider(role)
        except Exception as exc:  # noqa: BLE001
            report.add(f"model ({role})", WARN, str(exc))
            continue
        name = spec.name or ""
        if not name:
            report.add(f"model ({role})", WARN, "none configured — run 'cobirb setup' to choose one")
            continue
        if available and canonical_model(name) not in {canonical_model(m) for m in available}:
            report.add(
                f"model ({role})", FAIL,
                f"'{name}' is configured but not on the endpoint — 'ollama pull {name}'",
            )
            continue
        detail = name
        try:
            if not role_provider.supports_vision():
                detail += " (no vision — /image will be text only)"
        except Exception:  # noqa: BLE001 - a capability we could not ask about is not a failure
            pass
        report.add(f"model ({role})", OK, detail)


# --------------------------------------------------------------------------- #
# 3. Install
# --------------------------------------------------------------------------- #
def _check_install(report: Report) -> None:
    from . import upgrade as upgrade_module

    install = upgrade_module.detect_install()

    if install.kind == upgrade_module.MANAGED:
        report.add("install", OK, f"managed — {install.venv}")
        # Deliberately no "is there a newer release?" for this shape. Asking
        # means a request to GitHub, and doctor talks to the endpoint you
        # configured and nothing else. That question belongs to
        # `cobirb --upgrade`, where somebody typed the command that asks it.
        try:
            report.add("version", OK, f"v{upgrade_module._running_version()}")
        except Exception as exc:  # noqa: BLE001 - a version it can't read is worth saying, not fatal
            report.add("version", WARN, f"could not be read — {exc}")
        return

    if install.kind == upgrade_module.UNMANAGED:
        report.add(
            "install",
            WARN,
            "neither a managed install nor a git checkout, so 'cobirb --upgrade' "
            "cannot work here. Upgrade it however you installed it, or switch to a "
            f"self-upgrading install with:\n      {upgrade_module._INSTALL_COMMAND}",
        )
        return

    root = install.root
    report.add("install", OK, root)

    branch = upgrade_module._current_branch(cwd=root)
    if branch:
        report.add("checkout", OK, f"on {branch}")
    else:
        report.add(
            "checkout", WARN,
            "not on a branch (detached HEAD) — a commit made here belongs to no branch "
            "and 'git push' will not send it. 'git checkout <branch>' fixes it",
        )

    try:
        running = upgrade_module._running_version()
        latest = upgrade_module._latest_tag(cwd=root)
    except Exception as exc:  # noqa: BLE001 - version currency is nice to know, never fatal
        report.add("version", WARN, f"could not be compared — {exc}")
        return
    if upgrade_module._parse_version(latest) > upgrade_module._parse_version(running):
        report.add("version", WARN, f"v{running} installed, {latest} released — 'cobirb --upgrade'")
    else:
        report.add("version", OK, f"v{running}")


# --------------------------------------------------------------------------- #
def _check_sandbox(report: Report, config: Config) -> None:
    """Whether shell commands are contained, and how."""
    from .. import sandbox

    box = sandbox.from_config(config.get("sandbox"), os.getcwd())
    if box.mode == sandbox.MODE_OFF:
        report.add("sandbox", WARN, "off — approved shell commands run with your full access")
    elif not box.active:
        installed = shutil.which("bwrap")
        why = ("bubblewrap is installed but cannot create namespaces here (unprivileged user "
               "namespaces may be disabled)" if installed else
               "bubblewrap is not installed — e.g. 'sudo apt install bubblewrap', "
               "'sudo dnf install bubblewrap' or 'sudo pacman -S bubblewrap'")
        report.add("sandbox", WARN, f"{why}; shell commands run unsandboxed and are always asked about")
    elif box.mode == sandbox.MODE_AUTO and not box.explicit and not shutil.which("git"):
        report.add("sandbox", OK, "bubblewrap, asks first — commands would run without asking, "
                   "but git is missing so their changes could not be undone")
    else:
        report.add("sandbox", OK, box.describe())


def run(
    *,
    config: Config | None = None,
    build_provider: Callable[[str], Any] | None = None,
    check_environment: bool = True,
    check_install: bool = True,
) -> Report:
    """Run every check and report. Never raises, and never changes anything."""
    config = config or Config()
    report = Report()

    raw: dict[str, Any] | None = None
    path = paths.config_path()
    if os.path.isfile(path):
        import json

        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            raw = loaded if isinstance(loaded, dict) else {}
            if not isinstance(loaded, dict):
                report.add("config file", FAIL, f"{path} is not a JSON object")
                raw = None
        except (OSError, ValueError) as exc:
            report.add("config file", FAIL, f"{path} could not be read — {exc}")
            raw = None
    _check_config(report, config, raw)

    if check_environment:
        if build_provider is None:
            def build_provider(role: str) -> Any:  # noqa: F811 - the default, resolved late
                return model_roles.build_for_role(role, config)
        _check_models(report, config, build_provider)
        _check_sandbox(report, config)

    if check_install:
        _check_install(report)
    return report
