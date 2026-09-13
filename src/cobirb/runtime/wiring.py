"""Composition: turn config, plugins and CLI arguments into an Orchestrator.

This is the application's composition root. It lived in cli.py, which meant
interactive mode had to import a dozen of its private functions to build the
same object; it is a module in its own right so every front-end can wire a
run the same way.
"""
from __future__ import annotations

import os
from typing import Callable

from ..checkpoints import Checkpoints
from ..config import Config
from ..orchestrator import Orchestrator, build_default_policy
from ..plugins.core import LocalModelProvider, TerminalIO
from ..session import SessionManager
from ..typing import spi as cobirb_typing
from .instructions import DEFAULT_MAX_CHARS, load_instructions
from .personas import persona_key
from .plugins import build_crypto, discover_plugins, report_plugin_issues, resolve_slot


def build_model(model_name: str | None, cwd: str | None = None, config: Config | None = None) -> LocalModelProvider:
    """Build the local model provider from CLI arg, config, or env.

    Resolution order: the explicit ``model_name`` argument (``--model``),
    then config's ``"model"``, then ``models.default.name``, then
    ``"default_model"`` — the newest and simplest of the three config keys,
    checked last so it never overrides a more specific existing setting.
    All three name the same thing; keeping all of them working is just
    backward compatibility, not three different behaviors.

    No models are embedded by default; the provider talks to a local Ollama
    (or other OpenAI-compatible) server once a model name is supplied.
    Interactive mode additionally *validates* whatever name this resolves
    to against the live server at startup and offers ``/model`` to pick a
    working one if it can't — see ``tui.app.CoBirbApp``. This function
    itself does no such validation; one-shot mode uses it exactly as before.
    """
    config = config or Config(cwd=cwd)
    name = (
        model_name
        or config.get("model")
        or config.get("models", "default", "name")
        or config.get("default_model")
        or ""
    )
    base_url = config.get("models", "default", "base_url")
    return LocalModelProvider(model=name, base_url=base_url)


def parse_allow_tools(specs: "str | list[str] | None") -> dict[str, str]:
    """Parse permission rules written by the user.

    Used for both ``--allow-tool`` (repeatable) and the ``allow_tools`` config
    key (a list), which take the same syntax: each entry is ``name`` or
    ``name(arg)``, where ``arg`` narrows the ``shell`` scope to an invocation
    (e.g. ``shell(git)``, ``shell(python -m pytest)``). A single entry may
    also carry several rules separated by commas.

    This is the user's own escape hatch from the default-deny policy, which
    now pre-approves nothing at all (see ``build_default_policy``) — anything
    they want to run unattended, they say so here once.
    """
    if specs is None:
        specs = []
    if isinstance(specs, str):
        specs = [specs]
    allowed: dict[str, str] = {}
    for spec in specs:
        for entry in str(spec).split(","):
            entry = entry.strip()
            if not entry:
                continue
            if "(" in entry:
                name, _, arg = entry.partition("(")
                allowed[name.strip()] = arg.strip().rstrip(")").strip()
            else:
                allowed[entry] = ""
    return allowed


def _project_instructions(cwd: str, config: Config) -> str:
    """This project's instructions, unless config turned them off.

    Opt-out rather than opt-in: a file the user put in their own repo saying
    how they want an agent to behave is about as clear a signal of intent as
    there is, and making them ask for it twice would be silly.
    """
    if config.get("instructions") is False:
        return ""
    return load_instructions(cwd, int(config.get("instructions_max_chars", default=DEFAULT_MAX_CHARS)))


def build_orchestrator(
    cwd: str,
    persona: cobirb_typing.Persona,
    allow_overrides: dict[str, str],
    session_path: str | None = None,
    password: str | None = None,
    model_name: str | None = None,
    io_factory: Callable[[], cobirb_typing.I_OAdapter] = TerminalIO,
) -> Orchestrator:
    """Wire the core: registry -> provider -> policy -> orchestrator.

    The system prompt is deliberately not a parameter: it belongs to a turn,
    not to the wiring, and travels through ``Orchestrator.run()``. One used
    to be accepted here and silently ignored.

    ``io_factory`` builds the default I/O adapter, and defaults to the
    scrolling ``TerminalIO`` every text-mode caller wants. Interactive mode
    passes its own factory instead, so the orchestrator's output lands in
    the TUI's transcript rather than being printed over the top of a
    full-screen app. A config-selected ``plugins.io`` plugin still overrides
    whatever this produces, exactly as it overrode the hardcoded default.

    Session crypto (AES-256-GCM + scrypt) is only instantiated when a session
    path is supplied; otherwise the core runs with no crypto (nothing persisted).

    Third-party plugins are discovered here (see ``load_plugins()``) and
    merged in: tool plugins are registered into ``registry`` alongside the
    built-ins; model/io/crypto plugins replace the core default only when
    explicitly selected via ``plugins.<slot>`` in config. Discovery/merge
    problems are reported to stderr and never fatal.
    """
    config = Config(cwd=cwd)
    registry, discovered, discovery_issues = discover_plugins(cwd, config)
    report_plugin_issues(discovery_issues)

    provider, model_issue = resolve_slot(
        "model", discovered, config, lambda: build_model(model_name, cwd, config)
    )
    if model_issue:
        report_plugin_issues({"model": model_issue})

    io_adapter, io_issue = resolve_slot("io", discovered, config, io_factory)
    if io_issue:
        report_plugin_issues({"io": io_issue})

    # Nothing is permitted until the user says so. Config's `allow_tools`
    # comes first and `--allow-tool` after it; both only ever add, so the
    # order is about readability rather than precedence.
    policy = build_default_policy(
        audit_log_enabled=bool(config.get("audit_log")), cwd=registry.cwd
    )
    for rules in (parse_allow_tools(config.get("allow_tools")), allow_overrides):
        for name, arg in rules.items():
            policy.allow(name, arg)
    # Standing directory scopes, for a project the user has already decided
    # CoBirb may work in. Separate keys because reading and writing are
    # separate decisions.
    for directory in config.get("allow_read_dirs", default=[]) or []:
        policy.allow_read_dir(os.path.expanduser(str(directory)))
    for directory in config.get("allow_write_dirs", default=[]) or []:
        policy.allow_write_dir(os.path.expanduser(str(directory)))

    crypto = None
    if session_path is not None:
        crypto, crypto_issue = build_crypto(config, discovered)
        if crypto_issue:
            report_plugin_issues({"crypto": crypto_issue})
        if os.path.isfile(session_path):
            manager = SessionManager.load(session_path, crypto, password, cwd, persona_key(persona))
        else:
            manager = SessionManager.create(session_path, crypto, cwd, persona_key(persona), password)
    else:
        manager = None

    return Orchestrator(
        model=provider,
        tools=registry.tools,
        policy=policy,
        io=io_adapter,
        session=manager,
        crypto=crypto,
        # Only trusted if the user set it: Ollama serves its own default
        # num_ctx regardless of what a model advertises, so this is how
        # someone who has raised the window tells CoBirb about it.
        context_tokens=config.get("context_tokens"),
        project_instructions=_project_instructions(cwd, config),
        # On by default. The cost is that an agent asked to edit a
        # credentials file can't do it through a redacted read; the benefit is
        # that reading one doesn't put a live key in three places at once.
        redact_secrets=config.get("redact_secrets") is not False,
        # On by default: an agent that edits real files without an undo is a
        # worse deal than one that spends a little disk.
        checkpoints=None if config.get("checkpoints") is False else Checkpoints(cwd),
    )


def resolve_model_name(model_name: str | None, cwd: str) -> str:
    """The model's display name, for the session banner and the TUI's status
    bar.

    Resolved before any orchestrator exists (one is built lazily, on the
    first real turn), so it goes through ``build_model`` directly. Purely
    cosmetic, so a broken config here is swallowed rather than blocking
    startup — the real error still surfaces once a turn actually needs the
    model.
    """
    try:
        return build_model(model_name, cwd).name()
    except Exception:  # noqa: BLE001 - cosmetic only; never block startup on it
        return model_name or "(no model configured)"
