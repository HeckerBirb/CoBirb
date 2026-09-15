"""Composition: turn config, plugins and CLI arguments into an Orchestrator.

This is the application's composition root, and a module in its own right so
that every front-end wires a run the same way rather than reaching into
another's internals to build the same object.
"""
from __future__ import annotations

import os
from typing import Callable

from ..checkpoints import Checkpoints
from ..config import Config
from ..mcp import connect_servers
from ..orchestrator import Orchestrator, build_default_policy
from ..plugins.core import LocalModelProvider, TerminalIO, ToolRegistry
from ..policy import Policy
from ..session import SessionManager
from ..typing import spi as cobirb_typing
from ..plugins.core.repomap import DEFAULT_BUDGET_CHARS, render_map
from .headless import HeadlessIO
from .hooks import HookRunner
from .instructions import DEFAULT_MAX_CHARS, load_instructions
from .models import ROLE_ORCHESTRATOR, ROLE_WORKER, build_for_role
from .personas import persona_key
from .plugins import build_crypto, discover_plugins, report_plugin_issues, resolve_slot
from .verify import DEFAULT_MAX_FIX_ATTEMPTS, DEFAULT_TIMEOUT_SECONDS, VerifySettings


def build_model(model_name: str | None, cwd: str | None = None, config: Config | None = None) -> LocalModelProvider:
    """Build the provider for the agent the user is talking to.

    A thin front for ``models.build_for_role(ROLE_ORCHESTRATOR, ...)``, kept
    because every front-end calls it and the role it wants is always the same
    one. The orchestrator role resolves through its own key and then the
    default — see ``runtime.models``, which also explains why a role rather
    than a name.

    ``cwd`` is accepted and unused: configuration comes from the user's home
    directory and nothing else (see ``cobirb.config``), so the working
    directory selects nothing here. The parameter stays because every
    front-end passes it and removing it buys nothing.

    No models are embedded by default; the provider talks to a local Ollama
    (or other OpenAI-compatible) server once a model name is supplied.
    Interactive mode additionally *validates* whatever name this resolves
    to against the live server at startup and offers ``/model`` to pick a
    working one if it can't — see ``tui.app.CoBirbApp``. This function
    itself does no such validation; one-shot mode uses it exactly as before.
    """
    return build_for_role(ROLE_ORCHESTRATOR, config or Config(), override=model_name)


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


def _project_context(cwd: str, config: Config) -> str:
    """What the model is told about this project before it is asked anything.

    Two parts, both opt-out rather than opt-in. The project's own
    instructions, because a file the user put in their repo saying how they
    want an agent to behave is about as clear a signal of intent as there is
    and making them ask twice would be silly. And an outline of the codebase,
    because a model that knows where things live stops guessing at filenames.

    The map is injected rather than left as a tool the model must think to
    call. A permanent map would crowd a small context window, but that does
    not apply to the hardware CoBirb targets: a few thousand tokens of orientation
    against a 128k window is cheap, and having it there from the first turn is
    most of why this kind of grounding works. The tool stays, for a subtree or
    a refresh after the layout changes.
    """
    parts = []
    if config.get("instructions") is not False:
        parts.append(
            load_instructions(cwd, int(config.get("instructions_max_chars", default=DEFAULT_MAX_CHARS)))
        )
    if config.get("repo_map") is not False:
        parts.append(_startup_map(cwd, config))
    return "\n\n".join(part for part in parts if part)


def _startup_map(cwd: str, config: Config) -> str:
    """An outline of the codebase, or nothing if it can't be produced.

    Guarded: an unreadable project costs the model its orientation, never the
    session. Walking a very large tree is the one slow thing that happens
    before the first turn, which is why the budget is bounded here too.
    """
    budget = int(config.get("repo_map_max_chars", default=DEFAULT_BUDGET_CHARS))
    if budget <= 0:
        return ""
    try:
        return render_map(cwd, budget)
    except Exception:  # noqa: BLE001 - orientation is worth nothing at this price
        return ""


def _verify_settings(cwd: str, config: Config) -> VerifySettings | None:
    """The project's check, if the user nominated one.

    No guessing from the project layout: guessing wrong means running an
    arbitrary command the user never asked for, after every turn.
    """
    command = config.get("verify_command")
    if not command or not str(command).strip():
        return None
    return VerifySettings(
        command=str(command),
        cwd=cwd,
        timeout=int(config.get("verify_timeout", default=DEFAULT_TIMEOUT_SECONDS)),
        max_fix_attempts=int(config.get("verify_fix_attempts", default=DEFAULT_MAX_FIX_ATTEMPTS)),
    )


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
    not to the wiring, and travels through ``Orchestrator.run()``.

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
    config = Config()
    registry, discovered, discovery_issues = discover_plugins(cwd, config)
    report_plugin_issues(discovery_issues)

    # MCP servers, if the user configured any. Registered into the same
    # registry as the built-ins and the plugins, so they reach the model, the
    # approval prompt and the policy by exactly the same path.
    mcp_tools, mcp_clients, mcp_issues = connect_servers(config, cwd)
    report_plugin_issues(mcp_issues)
    for tool in mcp_tools:
        registry.register(tool)

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
        project_context=_project_context(cwd, config),
        # On by default. The cost is that an agent asked to edit a
        # credentials file can't do it through a redacted read; the benefit is
        # that reading one doesn't put a live key in three places at once.
        redact_secrets=config.get("redact_secrets") is not False,
        verify=_verify_settings(cwd, config),
        # On by default: an agent that edits real files without an undo is a
        # worse deal than one that spends a little disk.
        checkpoints=None if config.get("checkpoints") is False else Checkpoints(cwd),
        # The user's own commands at the lifecycle points.
        hooks=HookRunner.from_config(config, cwd),
        mcp_clients=mcp_clients,
    )


def build_subagent(
    cwd: str,
    policy: Policy,
    *,
    accept: str = "",
    config: Config | None = None,
    io: cobirb_typing.I_OAdapter | None = None,
) -> Orchestrator:
    """Wire an agent that takes its instructions from another agent.

    A subagent — a Worker Birb, in the Flock — is **not a special kind of
    run**. It is an ordinary CoBirb agent that received its prompt from
    Brainy Birb instead of from a person, so this composes the same parts
    ``build_orchestrator`` does and differs in exactly four places, each for a
    reason:

    - **Its policy is handed in, not read from config.** The charter's scopes
      *are* the isolation; config's ``allow_tools`` would widen them behind the
      user's back, and the user approved the charter rather than the config.
    - **It gets no project context at all.** No ``AGENTS.md``, no repo map.
      Need-to-know is the whole design: a worker that can read the codebase
      outline knows the shape of everyone else's work. Conventions reach it
      instead through the skeleton it is filling in, which was already written
      in house style.
    - **Its I/O is headless.** There is nobody to ask — the single approval
      already happened, at the charter. ``HeadlessIO`` refuses anything not
      pre-permitted rather than reaching for a prompt no one will answer, which
      is also what stops concurrent workers racing for the same modal.
    - **Its verification is its own scoped check**, so it never runs the full
      suite and therefore never meets a colleague's failing test to helpfully
      fix.

    Everything else it inherits *because it is an ordinary run*: checkpoints,
    secret redaction, and the user's own lifecycle hooks. Those rules should
    apply to every agent working in someone's tree, not only the ones they
    prompted themselves.

    No plugin discovery and no MCP servers. Not a restriction on principle —
    the policy denies those tools anyway, since the charter grants file paths
    and nothing else — but starting a user's database proxy once per worker
    would be actively wrong, and the built-ins are what a brief can actually
    use.
    """
    config = config or Config()
    registry = ToolRegistry(cwd)
    return Orchestrator(
        model=build_for_role(ROLE_WORKER, config),
        tools=registry.tools,
        policy=policy,
        io=io or HeadlessIO(),
        session=None,
        context_tokens=config.get("context_tokens"),
        # No project context, and — separately — no memory catalogues either:
        # those are composed into `system` per turn by the TUI itself
        # (CoBirbApp._memory_system_prompt), which a Worker Birb's run never
        # goes through. Consistent with "nothing but its brief" above.
        project_context="",
        redact_secrets=config.get("redact_secrets") is not False,
        verify=(
            VerifySettings(
                command=accept,
                cwd=cwd,
                timeout=int(config.get("verify_timeout", default=DEFAULT_TIMEOUT_SECONDS)),
                max_fix_attempts=int(
                    config.get("verify_fix_attempts", default=DEFAULT_MAX_FIX_ATTEMPTS)
                ),
                # Always, even if the worker changed nothing: this is the
                # ticket's definition of done, not a regression guard.
                only_after_changes=False,
            )
            if accept.strip()
            else None
        ),
        checkpoints=None if config.get("checkpoints") is False else Checkpoints(cwd),
        hooks=HookRunner.from_config(config, cwd),
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
