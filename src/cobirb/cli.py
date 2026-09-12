"""Command-line interface for CoBirb.

Three modes:

- **Interactive** (default): a full-screen Textual app (see ``cobirb.tui``)
  with a tab bar, a live status footer and a boxed input.
- **One-shot** (``--prompt``/``-p``): run a single task then exit. Stays a
  plain-stdout, pipeable CLI — a full-screen app can't be scripted.
- **Session** (``--session`` with ``--password``/``-w``): resume/continue an
  encrypted session on disk.

The CLI wires the thin core together: config -> plugin discovery -> tool
registry -> model provider -> policy -> orchestrator (optionally with
encrypted session storage). It renders results and reads input; the core
drives the loop.

Privacy by construction: no telemetry, no outbound network by default, sessions
encrypted at rest, and a hard default-deny permission boundary.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .orchestrator import Orchestrator, build_default_policy, render_through
from .plugins.loader import load_plugins
from .policy import PermissionError
from . import session as session_module
from .session import SessionManager
from .typing import spi as cobirb_typing

# Core plugins are always available and are imported directly here (never via
# entry points) — they must work even if plugin discovery finds nothing or
# fails entirely. Third-party plugins are discovered via load_plugins() each
# time an orchestrator is built (entry points + local cobirb/plugins/<name>/
# dirs) and merged in fail-closed: tool plugins are additive (registered
# alongside the built-ins, gated by the same permission policy at call time);
# model/io/crypto plugins are singleton slots so swapping the core default
# out requires an explicit "plugins.<slot>" config selection. A plugin that
# fails to load or collides with a built-in is reported to stderr and
# skipped — it never brings the core down.
from .plugins.core import (  # noqa: E402
    PLAIN_PERSONA_NAME,
    AesGcmScryptSessionCrypto,
    LocalModelProvider,
    TerminalIO,
    ToolRegistry,
    build_default_persona,
    build_plain_persona,
    persona_shapes_voice,
)
from .plugins.core import render  # noqa: E402
from .plugins.core.tools import BUILTIN_TOOLS  # noqa: E402


def _article(word: str) -> str:
    """"a" or "an" for ``word`` — personas are user-authored data, so the
    species can start with anything."""
    return "an" if word[:1].lower() in "aeiou" else "a"


# An optional description of the environment the model is running inside,
# off unless asked for with --system-prompt harness. Deliberately operational
# rather than editorial: it says nothing about what the model should or
# shouldn't discuss, only that tool calls are gated behind a prompt a human
# answers — a model that doesn't know a denial is a decision will retry a
# blocked tool until the loop gives up. That is the one concrete thing it
# buys, and the reason it is still offered at all.
#
# It is off by default because it is still an override: any system message
# CoBirb sends replaces the model's own Modelfile SYSTEM directive for that
# request (see LocalModelProvider.compose_system). CoBirb's actual guarantees
# never depended on it — they are enforced in policy.py and the session
# crypto, not by asking the model to honour them.
_HARNESS_PROMPT = (
    "This is CoBirb, a local agent harness running on the user's own machine. "
    "Tool calls are gated by a permission prompt the user answers, so a denied "
    "call is the user's decision, not an error to retry. Nothing leaves this "
    "machine: no telemetry, no outbound network by default, and session files "
    "are encrypted at rest."
)


def _build_system_prompt(persona: cobirb_typing.Persona, *, harness: bool = False) -> str:
    """Compose CoBirb's *own* contribution to the system prompt, if any.

    Returns ``""`` by default — no persona, no harness block — and an empty
    string here means the provider sends no system message whatsoever, so the
    model's Modelfile ``SYSTEM`` applies exactly as it does when talking to
    Ollama directly. That is the point: an explicit system message *replaces*
    the model's own for that request, so a client that always sends one
    silently overrides a configuration its user built on purpose.

    Whatever this does return is a supplement, not a replacement: the
    provider reads the model's own prompt back and places it first (see
    ``LocalModelProvider.compose_system``).

    With a persona active (``--persona``, ``/persona``, or the ``"persona"``
    config key), every field the persona file defines is rendered. Supplying
    only the name and species (as this once did) left tone, phrasings, emoji
    density and squawks as inert data the model never saw — so a persona
    declaring ``"emoji_density": "none"`` had no way to be honoured.
    ``greeting`` is the one exception: the CLI speaks it directly when a
    persona is adopted, so the model needn't reproduce it.
    """
    p = persona
    blocks = [_HARNESS_PROMPT] if harness else []
    if not persona_shapes_voice(p):
        return "\n".join(blocks)

    voice = []
    if p.species:
        voice.append(f"You are {p.name}, {_article(p.species)} {p.species}.")
    else:
        voice.append(f"You are {p.name}.")
    if p.tone:
        voice.append(f"Your tone is {p.tone}.")
    if p.phrasings:
        voice.append("Phrases that come naturally to you: " + "; ".join(p.phrasings))
    if p.known_squawks:
        voice.append("Interjections you use sparingly: " + "; ".join(p.known_squawks))
    if p.emoji_density:
        voice.append(f"Emoji use: {p.emoji_density}.")
    voice.append("This describes how you speak, and nothing else.")
    return "\n".join([*blocks, *voice])


def _parse_allow_tools(specs: "str | list[str] | None") -> dict[str, str]:
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


_PERSONAS_DIR = os.path.join(os.path.dirname(__file__), "personas")


NO_PERSONA = "none"


def _load_persona(persona_name: str | None) -> cobirb_typing.Persona:
    """Return the persona for ``persona_name``, or no persona at all.

    ``None`` (nothing configured) and the explicit name ``"none"`` both give
    the plain, voice-less default — personas are opt-in, so an unconfigured
    run sends the model no instructions about how to sound.

    Resolution order for a real name: the bundled personas shipped with
    CoBirb (see ``cobirb/personas/``), then a project-local ``<name>.json``,
    then a ``<name>.json`` under the user's CoBirb home.
    """
    key = (persona_name or "").strip()
    lowered = key.lower()
    # PLAIN_PERSONA_NAME is accepted alongside "none" so a session saved with
    # no persona reloads cleanly: sessions record a persona name, and the
    # plain default's name is "CoBirb".
    if not lowered or lowered in (NO_PERSONA, PLAIN_PERSONA_NAME.lower()):
        return build_plain_persona()
    # Case-insensitive so "Noah" (as stored in a session) resolves to the
    # same persona "noah" (as typed on the command line) does.
    if lowered == "noah":
        return build_default_persona()
    persona_name = key
    candidates = [
        os.path.join(_PERSONAS_DIR, f"{persona_name}.json"),
        os.path.join(os.getcwd(), f"{persona_name}.json"),
        os.path.join(os.environ.get("COBIRB_HOME", os.path.expanduser("~")), "cobirb", f"{persona_name}.json"),
    ]
    data = None
    for path in candidates:
        if os.path.isfile(path):
            data = _load_json(path)
            break
    if data is None:
        # Falls back to *no* persona rather than to Noah: a typo in
        # --persona shouldn't quietly dress the model up in a character the
        # user never asked for.
        print(f"unknown persona '{persona_name}'. Continuing without one.", file=sys.stderr)
        return build_plain_persona()
    from .typing.spi import Persona

    return Persona.from_dict(data)


def _available_personas() -> list[str]:
    """Every persona CoBirb can resolve out of the box, for the ``/persona``
    picker and listing.

    ``"none"`` leads because it is the default and the way back to it: the
    picker has to be able to *remove* a persona, not only swap one for
    another. After it come the built-in Noah and every ``*.json`` bundled
    under ``cobirb/personas/``.
    """
    names = {"noah"}
    if os.path.isdir(_PERSONAS_DIR):
        for fname in os.listdir(_PERSONAS_DIR):
            if fname.endswith(".json"):
                names.add(fname[: -len(".json")])
    return [NO_PERSONA, *sorted(names)]


def _persona_key(persona: cobirb_typing.Persona) -> str:
    """The name that reloads ``persona`` — what ``--persona`` takes, which is
    not always what the persona calls itself.

    ``kawaii.json`` introduces itself as "Imouto", so a session that recorded
    the display name could not be reopened: ``_load_persona("Imouto")`` finds
    no such file. Sessions therefore store this key instead.
    """
    if not persona_shapes_voice(persona):
        return NO_PERSONA
    for name in _available_personas():
        if name != NO_PERSONA and _load_persona(name).name == persona.name:
            return name
    # A persona the user wrote themselves: the file name is the best guess
    # available, and matches the common case of naming the file after it.
    return persona.name


def _load_json(path: str) -> dict[str, Any]:
    import json

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_harness_prompt(cli_value: str | None, config: Config) -> bool:
    """Resolve whether CoBirb sends its own harness block:
    ``--system-prompt {off,harness}`` overrides the ``"system_prompt"`` config
    key, and with neither given it is **off** — CoBirb sends no system message
    of its own and the model's Modelfile ``SYSTEM`` applies untouched.
    """
    value = cli_value if cli_value is not None else config.get("system_prompt", default="off")
    return str(value).strip().lower() == "harness"


def _resolve_plan_mode(cli_value: str | None, config: Config) -> bool:
    """Resolve whether plan mode starts on: ``--plan-mode {on,off}`` (CLI)
    overrides the ``"plan_mode"`` boolean in config; with neither given, it
    defaults off. Either way it can still be flipped mid-conversation with
    ``/plan on``/``/plan off`` (see ``_apply_plan_toggle``).
    """
    if cli_value is not None:
        return cli_value == "on"
    return bool(config.get("plan_mode", default=False))


# --------------------------------------------------------------------------- #
# Slash commands. These return their message rather than printing it, so both
# renderers can use them: the TUI writes the text into its transcript, while
# anything text-based can just print it. The wording is the wording the
# scrolling interactive loop used before the TUI replaced it.
# --------------------------------------------------------------------------- #
def _apply_persona_switch(
    arg: str, persona: cobirb_typing.Persona, system: str, *, harness: bool = False
) -> tuple[cobirb_typing.Persona, str, str]:
    """Handle ``/persona [name]``.

    With no argument it lists the available personas and leaves the current
    one alone. With a name it loads that persona and rebuilds the system
    prompt around it, so every turn afterward actually speaks as the new
    persona instead of just announcing that it will.

    Returns ``(persona, system, message)``.
    """
    if not arg:
        return persona, system, f"Available personas: {', '.join(_available_personas())}"
    new_persona = _load_persona(arg)
    new_system = _build_system_prompt(new_persona, harness=harness)
    if not persona_shapes_voice(new_persona):
        return new_persona, new_system, "Persona off — the model speaks in its own voice."
    greeting = new_persona.greeting or "switched personas."
    return new_persona, new_system, f"{new_persona.name}: {greeting}"


def _apply_plan_toggle(arg: str, plan_mode: bool) -> tuple[bool, str]:
    """Handle ``/plan [on|off]``: toggle plan mode, report its state, or
    reject an argument that is neither. Returns ``(plan_mode, message)``."""
    arg = arg.strip().lower()
    if arg in ("on", "off"):
        plan_mode = arg == "on"
        return plan_mode, f"Plan mode: {'on' if plan_mode else 'off'}."
    if not arg:
        return plan_mode, f"Plan mode is {'on' if plan_mode else 'off'}. Usage: /plan on|off"
    return plan_mode, "Usage: /plan on|off"


def _build_model(model_name: str | None, cwd: str | None = None, config: Config | None = None) -> LocalModelProvider:
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


def _report_plugin_issues(issues: dict[str, str]) -> None:
    """Print discovery/merge problems for third-party plugins to stderr.

    Never fatal: a broken or colliding plugin is reported and skipped, the
    core keeps running on its built-ins.
    """
    for ident, message in issues.items():
        print(f"cobirb: plugin problem — {ident}: {message}", file=sys.stderr)


def _merge_tool_plugins(registry: ToolRegistry, discovered: dict[str, Any]) -> dict[str, str]:
    """Register every discovered ``tool:`` plugin into ``registry``.

    Tools are additive (unlike the model/io/crypto singleton slots below):
    every discovered tool plugin is registered, because the permission
    policy still gates whether it can actually run. A
    plugin can never shadow a built-in (or a different plugin's) tool name,
    though — if its declared name collides with one already registered
    under a different implementation, it's skipped and reported rather than
    silently replacing (or losing to) the existing one.

    One real plugin can legitimately be *discovered twice* under different
    keys — ``load_plugins()`` finds it both as an installed entry point and
    as a local ``cobirb/plugins/<name>/`` directory when it happens to be
    both (e.g. installed editable from that same directory). That's a
    harmless duplicate, not a collision, so it's recognized by comparing
    the *class* already registered under that name, not just the name.
    """
    problems: dict[str, str] = {}
    for key, cls in discovered.items():
        kind, _, plugin_name = key.partition(":")
        if kind != "tool":
            continue
        try:
            try:
                instance = cls(registry.cwd)
            except TypeError:
                instance = cls()
        except Exception as exc:  # noqa: BLE001 - fail-closed per plugin
            problems[f"tool:{plugin_name}"] = f"failed to instantiate: {exc}"
            continue
        if not callable(instance.name):
            problems[f"tool:{plugin_name}"] = (
                "name must be a method returning a string, as the Tool interface "
                "declares — not a plain attribute; skipped"
            )
            continue
        tool_name = instance.name()
        existing = registry.get(tool_name)
        if existing is not None:
            if type(existing) is cls:
                continue  # the same plugin, discovered twice; not a collision
            problems[f"tool:{plugin_name}"] = (
                f"tool name '{tool_name}' collides with an existing tool; skipped"
            )
            continue
        registry.register(instance)
    return problems


def _discover_plugins(cwd: str, config: Config) -> tuple[ToolRegistry, dict[str, Any], dict[str, str]]:
    """Discover third-party plugins and register every ``tool:`` one into a
    fresh ``ToolRegistry``.

    Returns ``(registry, discovered, issues)``: ``discovered`` is every
    plugin class ``load_plugins()`` found (also used to resolve the
    model/io/crypto singleton slots below), ``issues`` collects discovery/
    merge problems for the caller to report. Needs no model, so it can run
    standalone — e.g. to populate the TUI's Plugins tab — without building a
    whole ``Orchestrator``.
    """
    registry = ToolRegistry(cwd)
    discovered, plugin_errors = load_plugins()
    tool_issues = _merge_tool_plugins(registry, discovered)
    return registry, discovered, {**plugin_errors, **tool_issues}


def _resolve_slot(
    kind: str,
    discovered: dict[str, Any],
    config: Config,
    default: Callable[[], Any],
) -> tuple[Any, str | None]:
    """Build the implementation for a singleton slot (model, io, or crypto).

    Select by config name, instantiate, and fall back to the core default if
    either step fails. Returns ``(instance, issue)``; the instance is always
    usable, and ``issue`` is set whenever a selection couldn't be honored so
    the caller can report why. ``default`` is a factory rather than an
    instance because the core defaults take real constructor arguments (a
    model name, a base URL) that a bare no-argument construction would lose.

    Written once and parameterized because all three slots resolve the same
    way; only the name and the fallback differ.
    """
    cls, issue = _select_plugin(kind, discovered, config)
    if issue:
        return default(), issue
    if cls is None:
        return default(), None
    try:
        return cls(), None
    except Exception as exc:  # noqa: BLE001 - fail closed to the core default
        return default(), f"failed to instantiate: {exc}"


def _build_crypto(config: Config, discovered: dict[str, Any]) -> tuple[Any, str | None]:
    """Resolve the session crypto backend: the core AES-256-GCM+scrypt
    default, or a config-selected ``plugins.crypto`` plugin."""
    return _resolve_slot("crypto", discovered, config, AesGcmScryptSessionCrypto)


@dataclass
class ToolInfo:
    """One registered tool, for display (the TUI's Plugins tab)."""

    name: str
    description: str
    source: str  # "core" or "plugin"


@dataclass
class PluginsSummary:
    """A live snapshot of what's discovered/active, for display."""

    slots: dict[str, str] = field(default_factory=dict)
    tools: list[ToolInfo] = field(default_factory=list)
    issues: dict[str, str] = field(default_factory=dict)


def describe_plugins(cwd: str) -> PluginsSummary:
    """Gather a live snapshot of discovered plugins and registered tools.

    Needs no model and builds no orchestrator, so it's safe and cheap to
    call just to populate a display (the TUI's Plugins tab) — it never
    reports discovery/merge issues to stderr itself (unlike
    ``_build_orchestrator``'s own call to plugin discovery); the caller
    decides how to show them.
    """
    config = Config(cwd=cwd)
    registry, discovered, issues = _discover_plugins(cwd, config)

    slots: dict[str, str] = {}
    for kind in ("model", "io", "crypto"):
        cls, issue = _select_plugin(kind, discovered, config)
        if issue:
            slots[kind] = f"core (config error: {issue})"
        elif cls is not None:
            slots[kind] = str(config.get("plugins", kind))
        else:
            slots[kind] = "core"

    tools = sorted(
        (
            ToolInfo(
                name=tool.name(),
                description=tool.description(),
                source="core" if type(tool) in BUILTIN_TOOLS else "plugin",
            )
            for tool in registry.values()
        ),
        key=lambda info: info.name,
    )
    return PluginsSummary(slots=slots, tools=tools, issues=issues)


def _select_plugin(kind: str, discovered: dict[str, Any], config: Config) -> tuple[Any | None, str | None]:
    """Resolve a config-selected model/io/crypto plugin for slot ``kind``.

    Unlike tools, these are singleton slots, so swapping the core default
    out requires an explicit ``plugins.<kind>`` choice in config rather than
    a first-discovered guess. Returns ``(None, None)`` when nothing is
    selected, or the selection just names the core default (already built
    with its normal constructor arguments elsewhere) — the caller keeps its
    existing default in both cases. Returns ``(None, message)`` when a name
    is selected but no matching plugin was discovered.
    """
    name = config.get("plugins", kind)
    if not name or name == f"core-{kind}":
        return None, None
    cls = discovered.get(f"{kind}:{name}")
    if cls is None:
        return None, f"unknown {kind} plugin '{name}'"
    return cls, None


def _build_orchestrator(
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
    registry, discovered, discovery_issues = _discover_plugins(cwd, config)
    _report_plugin_issues(discovery_issues)

    provider, model_issue = _resolve_slot(
        "model", discovered, config, lambda: _build_model(model_name, cwd, config)
    )
    if model_issue:
        _report_plugin_issues({"model": model_issue})

    io_adapter, io_issue = _resolve_slot("io", discovered, config, io_factory)
    if io_issue:
        _report_plugin_issues({"io": io_issue})

    # Nothing is permitted until the user says so. Config's `allow_tools`
    # comes first and `--allow-tool` after it; both only ever add, so the
    # order is about readability rather than precedence.
    policy = build_default_policy(
        audit_log_enabled=bool(config.get("audit_log")), cwd=registry.cwd
    )
    for rules in (_parse_allow_tools(config.get("allow_tools")), allow_overrides):
        for name, arg in rules.items():
            policy.allow(name, arg)

    crypto = None
    if session_path is not None:
        crypto, crypto_issue = _build_crypto(config, discovered)
        if crypto_issue:
            _report_plugin_issues({"crypto": crypto_issue})
        if os.path.isfile(session_path):
            manager = SessionManager.load(session_path, crypto, password, cwd, _persona_key(persona))
        else:
            manager = SessionManager.create(session_path, crypto, cwd, _persona_key(persona), password)
    else:
        manager = None

    return Orchestrator(
        model=provider,
        tools=registry.tools,
        policy=policy,
        io=io_adapter,
        session=manager,
        crypto=crypto,
    )


def _render(text: str) -> None:
    print(text)


def _render_user_prompt(prompt: str) -> None:
    """Echo what the user asked, marked the way interactive mode marks it.

    Goes through a Rich ``Console`` rather than ``_render``'s bare ``print``
    so the ``>`` is coloured and a multi-line prompt is indented under it —
    the two renderers are supposed to look identical (see
    ``plugins.core.render``), and this was the last place still printing a
    bare "You:" label.
    """
    from rich.console import Console

    Console().print(render.build_user_message(prompt))
    print()


def _render_final_answer(orchestrator: Any, persona_name: str, text: str) -> None:
    """Show the model's finished reply.

    Prefers the orchestrator's ``io`` adapter's ``render_answer`` hook when
    it has one (marked, markdown-rendered — see
    ``TerminalIO.render_answer``), falling back to a plain "name: text"
    line for an adapter without it (including test doubles and a bare
    stub orchestrator with no ``io`` attribute at all).
    """
    answer = text or "Completed."

    def plain() -> None:
        _render(f"{persona_name}: {answer}")

    io_adapter = getattr(orchestrator, "io", None)
    if not render_through(io_adapter, "render_answer", persona_name, answer, fallback=plain):
        plain()  # no adapter at all (a bare stub orchestrator)


def _resolve_model_name(model_name: str | None, cwd: str) -> str:
    """The model's display name, for the session banner and the TUI's status
    bar.

    Resolved before any orchestrator exists (one is built lazily, on the
    first real turn), so it goes through ``_build_model`` directly. Purely
    cosmetic, so a broken config here is swallowed rather than blocking
    startup — the real error still surfaces once a turn actually needs the
    model.
    """
    try:
        return _build_model(model_name, cwd).name()
    except Exception:  # noqa: BLE001 - cosmetic only; never block startup on it
        return model_name or "(no model configured)"


def _run_one_shot(
    prompt: str,
    persona: cobirb_typing.Persona,
    system: str,
    allow_overrides: dict[str, str],
    session_path: str | None,
    password: str | None,
    cwd: str,
    model_name: str | None = None,
    plan_mode: bool = False,
) -> int:
    # Wiring first, echo second: a prompt echoed before the session failed to
    # open reads as though the task was attempted, when nothing ran at all.
    try:
        orchestrator = _build_orchestrator(
            cwd, persona, allow_overrides, session_path, password, model_name
        )
    except Exception as exc:  # noqa: BLE001 - a bad password must not traceback
        # Chiefly a session that wouldn't decrypt. Wiring failures used to
        # escape here as an unhandled traceback, which for the commonest
        # cause (a mistyped password) is a terrible way to be told.
        print(f"cobirb: could not open {session_path} — {_session_open_error(exc)}", file=sys.stderr)
        return 1

    _render_user_prompt(prompt)
    try:
        session = orchestrator.run(
            prompt, system, cwd=cwd, persona=persona.name, session_path=session_path, plan_mode=plan_mode
        )
    except PermissionError as exc:
        _render(f"{persona.name}: blocked — {exc}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - surface provider/tool errors cleanly
        _render(f"{persona.name}: could not complete — {exc}\n")
        return 1

    # If the final answer already streamed live via the I/O adapter, printing
    # session.summary again here would just show it a second time.
    if not orchestrator.last_turn_streamed:
        _render_final_answer(orchestrator, persona.name, session.summary)
    if session_path is not None and orchestrator.session is not None:
        orchestrator.session.save(password)
    return 0


def _run_tui(
    persona: cobirb_typing.Persona,
    system: str,
    allow_overrides: dict[str, str],
    session_path: str | None,
    password: str | None,
    cwd: str,
    model_name: str | None = None,
    plan_mode: bool = False,
    harness: bool = False,
) -> int:
    """Interactive mode: hand off to the full-screen Textual app.

    ``cobirb.tui`` is imported lazily, here, rather than at module scope.
    Textual is only needed for this one mode, so a missing or broken install
    must not take down ``cobirb -p`` or ``cobirb help`` — modes that never
    touch it — with an import error at the top of this file.
    """
    try:
        from .tui.app import CoBirbApp
    except Exception as exc:  # noqa: BLE001 - a missing optional dep is not a crash
        print(
            f"cobirb: interactive mode needs textual, which could not be loaded ({exc}).\n"
            "Install it with 'pip install textual', or use one-shot mode: cobirb -p \"...\"",
            file=sys.stderr,
        )
        return 1

    app = CoBirbApp(
        persona=persona,
        system=system,
        allow_overrides=allow_overrides,
        session_path=session_path,
        password=password,
        cwd=cwd,
        model_name=model_name,
        plan_mode=plan_mode,
        harness=harness,
    )

    # Resuming an existing session file: open it *before* the app starts.
    #
    # Doing this inside the running app meant a wrong password took you all
    # the way in — full-screen UI, a model to pick — only to report the
    # failure into a transcript belonging to a session that had never
    # opened. If a session was asked for and can't be unlocked, there is
    # nothing to interact with, so nothing should start: the error belongs
    # on the terminal you typed the command into, with a non-zero exit.
    #
    # The app object exists by now but hasn't run, so its I/O bridge can
    # already be handed to the orchestrator — which is what keeps this the
    # same single code path that opens a session everywhere else, and means
    # the file is decrypted exactly once.
    if session_path is not None and os.path.isfile(session_path):
        try:
            app.orchestrator = _build_orchestrator(
                cwd,
                persona,
                allow_overrides,
                session_path,
                password,
                model_name,
                io_factory=lambda: app.io_bridge,
            )
        except Exception as exc:  # noqa: BLE001 - a bad password is routine, not a crash
            print(
                f"cobirb: could not open {session_path} — {_session_open_error(exc)}",
                file=sys.stderr,
            )
            return 1

    app.run()

    # After app.run() returns, never from inside the app: Textual owns the
    # whole screen until then, so anything printed earlier would be painted
    # over and lost. Read back off the app rather than using the argument —
    # the Sessions tab can start or resume a different session mid-run, and
    # the hint has to name the file that was actually written.
    if app.session_path is not None and os.path.isfile(app.session_path):
        print(_resume_hint(app.session_path))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cobirb",
        description="CoBirb — a privacy-first, local agentic CLI. No telemetry, no "
        "outbound network by default, sessions encrypted at rest.",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["help"],
        help="Show help (or 'cobirb help' for this overview).",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        choices=sorted(_HELP_TOPICS),
        help=f"With 'help': a specific topic ({', '.join(sorted(_HELP_TOPICS))}).",
    )

    mode = parser.add_argument_group("modes")
    mode.add_argument("-p", "--prompt", help="One-shot prompt: run once then exit.")
    mode.add_argument("--session", metavar="PATH", help="Resume/continue an encrypted session at PATH.")
    mode.add_argument(
        "--password",
        "-w",
        nargs="?",
        const=True,
        metavar="PASSWORD",
        help="Run in an encrypted session, starting a new one under "
        f"{_sessions_dir_display()} if --session names none. Give the password "
        "here ('-w hunter2') or leave it off ('-w') to be prompted without "
        "echo — a password on the command line is visible in shell history "
        "and process listings.",
    )

    opts = parser.add_argument_group("options")
    opts.add_argument(
        "--model", help="Ollama model name, e.g. 'llama3.1' (or use config/COBIRB_MODEL_NAME)."
    )
    opts.add_argument(
        "--persona",
        "--agent",
        dest="persona",
        help="Adopt a persona (default: none — the model keeps its own voice). "
        "Bundled: noah, professional, neighbor, kawaii; or point at your own "
        "<name>.json. Pick one interactively any time with /persona.",
    )
    opts.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        metavar="SPEC",
        help="Permit a tool up front: 'name' or 'name(arg)' (e.g. "
        "'shell(git),read_file'). Repeatable, and equivalent to the "
        "'allow_tools' config key. Without one, every tool call asks.",
    )
    opts.add_argument(
        "--plan-mode",
        choices=["on", "off"],
        default=None,
        help="Plan, then act, then validate with references, as 3 separate model "
        "phases (default: off, or the 'plan_mode' config key). Toggle mid-session "
        "with /plan on|off.",
    )
    opts.add_argument(
        "--system-prompt",
        choices=["off", "harness"],
        default=None,
        metavar="{off,harness}",
        help="Whether CoBirb sends a system prompt of its own (default: off, "
        "or the 'system_prompt' config key). Off means no system message is "
        "sent at all, so the SYSTEM directive your model was built with "
        "applies exactly as it does in Ollama. 'harness' adds a short "
        "description of the tool-permission model, which stops some models "
        "retrying a denied tool call. Either way, a persona (if you adopt "
        "one) is added after your model's own prompt, never instead of it.",
    )
    opts.add_argument("--cwd", help="Working directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "help":
        print(_HELP_TOPICS[args.topic] if args.topic else _HELP_TEXT)
        return 0

    config = Config(cwd=args.cwd)
    allow_overrides = _parse_allow_tools(args.allow_tool)
    persona_name = args.persona or config.get("persona")
    persona = _load_persona(persona_name)
    harness = _resolve_harness_prompt(args.system_prompt, config)
    system = _build_system_prompt(persona, harness=harness)
    plan_mode = _resolve_plan_mode(args.plan_mode, config)

    # Either flag alone is enough to mean "this is a session": a path always
    # needs a password to encrypt to, and a password with no path gets a new
    # session under ~/.cobirb/sessions rather than being silently ignored.
    session_path, password = _resolve_session(args.session, args.password)

    if args.prompt is not None:
        status = _run_one_shot(
            args.prompt,
            persona,
            system,
            allow_overrides,
            session_path,
            password,
            args.cwd or ".",
            args.model,
            plan_mode,
        )
        # Only on success: the hint is about a session this run actually
        # wrote to. Printing it after a failure ("could not open …" followed
        # by "Session saved") claims something that didn't happen.
        if status == 0 and session_path is not None and os.path.isfile(session_path):
            # stderr, not stdout: one-shot mode is meant to pipe, and this
            # hint is for the human, not for whatever is reading the output.
            print(_resume_hint(session_path), file=sys.stderr)
        return status

    return _run_tui(
        persona,
        system,
        allow_overrides,
        session_path,
        password,
        args.cwd or ".",
        args.model,
        plan_mode,
        harness,
    )


def _abbreviate_home(path: str) -> str:
    """``/home/you/.cobirb/x`` as ``~/.cobirb/x``, for text a user might type
    back. Paths outside the home directory are returned unchanged."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def _read_password() -> str:
    """Read a password from stdin without echoing it."""
    import getpass

    return getpass.getpass("CoBirb password: ")


def _sessions_dir_display() -> str:
    """``~/.cobirb/sessions`` with the home directory abbreviated, for help
    text and the resume hint — the literal path is long and the ``~`` form is
    what a user would type back."""
    return _abbreviate_home(session_module.default_sessions_dir())


def _resolve_session(session_arg: str | None, password_arg: Any) -> tuple[str | None, str | None]:
    """Work out which session file to use and which password unlocks it.

    Returns ``(session_path, password)``, both ``None`` when this run isn't a
    session at all.

    ``--password``/``-w`` on its own used to be inert in interactive mode:
    the password was only ever read when ``--session`` also named a path, so
    ``cobirb -w`` ran an ordinary throwaway conversation and nothing was
    saved. Asking for a password is asking for an encrypted session, so one
    is now created under ``~/.cobirb/sessions`` named for the current time.

    ``-w <password>`` takes the password from the command line, and ``-w``
    alone prompts for it without echo. The command-line form is what makes
    ``cobirb -w 1234`` work in one go; it is also visible in shell history
    and in ``ps``, which is why the bare form still exists and why the help
    text says so.
    """
    if not session_arg and not password_arg:
        return None, None
    if password_arg is True or password_arg is None:
        password = _read_password()
    else:
        password = str(password_arg)
    path = session_arg or os.path.join(
        session_module.default_sessions_dir(), f"session-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    return path, password


def _session_open_error(exc: Exception) -> str:
    """A readable reason a session file could not be opened.

    A wrong password surfaces as the crypto library's ``InvalidTag``, which
    carries **no message at all** — AES-GCM can only report that the
    authentication tag didn't match, never why. Printed raw that produced a
    dangling "could not open that session —" with nothing after the dash, so
    an empty message is spelled out here instead. Tamper detection
    (``SessionManager._verify_hashes``) does raise with a real message, and
    that one is passed through unchanged.
    """
    message = str(exc).strip()
    return message or "wrong password, or the file has been modified"


def _resume_hint(session_path: str) -> str:
    """The exact command that reopens ``session_path``.

    Printed on the way out of a session so the file isn't something the user
    has to go hunting for. Deliberately ``-w`` with no value: the password
    would otherwise be printed to the terminal and into whatever scrollback
    or log is capturing it.
    """
    return (
        "Session saved. Resume it with:\n"
        f"  cobirb --session {_abbreviate_home(session_path)} -w"
    )


_HELP_TEXT = """\
CoBirb — a privacy-first, local agentic CLI.

MODES
  Interactive (default): a full-screen terminal app — tabs, a live status
                         line, a boxed input, tool approval as a dialog.
  One-shot:              cobirb -p "your task" --allow-tool='shell(git)'
                         Plain stdout, so it pipes and scripts like any CLI.
  Session:               cobirb -w                 (new encrypted session)
                         cobirb --session <path> -w

PRIVACY BY CONSTRUCTION
  • Zero telemetry. • No outbound network by default. • Sessions encrypted
    at rest (AES-256-GCM, keyed via scrypt). • Nothing is pre-approved: no
    tool reads, writes, or runs anything until you say so. Approving a read
    covers that directory and below; writing and running are asked every
    time unless you allow them yourself. • No local audit log unless you
    opt in ("audit_log" in config — see 'cobirb help config'), since one
    would otherwise be a second, unencrypted copy of what you write and
    run. • Everything stays on this machine.

These are enforced in code — the permission layer and the session crypto —
not by asking the model to behave. CoBirb sends no system prompt of its own
by default, so your model's own SYSTEM directive is what shapes it.

OPTIONS
  --model NAME        Model name (config/COBIRB_MODEL_NAME if omitted); pick
                      one interactively any time with /model.
  --persona NAME      Adopt a persona (default: none — the model's own voice
                      is left alone). Bundled: noah, professional, neighbor,
                      kawaii. Or set "persona" in config, or point at your
                      own <name>.json. Pick one with /persona.
  -w [PASSWORD]       Run in an encrypted session, starting one under
                      ~/.cobirb/sessions if --session names none. '-w' alone
                      prompts without echo; '-w hunter2' is visible in shell
                      history and process listings.
  --session PATH      Resume/continue the encrypted session at PATH.
  --system-prompt     off (default) or harness. Off sends NO system message
                      at all, so the SYSTEM directive your model was built
                      with applies exactly as it does in Ollama. See
                      'cobirb help model'.
  --allow-tool SPEC   Override permission: 'name' or 'name(arg)'. Repeatable.
  --plan-mode on|off  Plan, then act, then validate with references, as 3
                      separate model phases (default: off, or "plan_mode"
                      in config). See 'cobirb help plan'.
  --cwd DIR           Working directory.

INTERACTIVE COMMANDS
  /model             List models available from the configured endpoint
                     and pick one for this session.
  /persona           Pick a persona from a list (including "none", the
                     default, which hands the voice back to the model).
  /persona <name>    Switch to a named persona directly.
  /plan on|off       Toggle plan mode mid-conversation.
  /plan              Show whether plan mode is currently on.
  ? or /help         Open this help. '/help <topic>' opens one topic.

INTERACTIVE KEYS
  f1 help · f2 next tab · ctrl+q quit · up/down recall earlier prompts (the
  last 100, in memory only — nothing you type is written to disk).
  ctrl+c copies the transcript selection if you have dragged one out with
  the mouse, and otherwise cancels a running turn (a stuck or slow shell
  command, most usefully — quitting mid-turn tries this first too, so it
  never sits waiting on one either). In a tool-approval dialog: y allow
  once · a allow for the rest of the session · n (or escape) deny.

TOPICS
  Run 'cobirb help <topic>' for more: session, persona, plan, model,
  plugins, tools, config.
"""

_HELP_TOPICS: dict[str, str] = {
    "session": """\
SESSION — encrypted, resumable conversations

  cobirb -w                            New session under ~/.cobirb/sessions.
  cobirb -w hunter2                    Same, with the password given inline.
  cobirb --session <path> -w           Resumed/created at <path>.
  cobirb -p "task" -w                  One-shot, saved to a new session.

Asking for a password is asking for a session: -w on its own starts one named
for the current time under ~/.cobirb/sessions, so there is no path to invent
up front. The same password unlocks an existing session or sets one for a
session being created.

'-w' with no value prompts for the password without echoing it. '-w hunter2'
takes it straight from the command line, which is quicker and is also visible
in your shell history and to anyone who can list processes — your call which
trade you want.

Resuming shows you the conversation you are rejoining: interactive mode
replays the saved turns into the transcript, between two dim rules, before
you type anything. The file is unlocked before anything starts, so a wrong
password reports on the terminal and exits non-zero — a session that can't
be decrypted never opens the app at all.

On exit, the command that reopens the session is printed to the terminal, so
the file is never something you have to go hunting for. In interactive mode
the Sessions tab also lists and starts sessions without needing --session at
all — see its own screen for details.

  • Encrypted at rest: AES-256-GCM, keyed via scrypt over the password. The
    plaintext session file never exists on disk.
  • Tamper-evident: each turn carries a content hash, checked on reload.
  • Saved after every turn (interactive) or once at the end (one-shot).
""",
    "persona": """\
PERSONA — how CoBirb speaks

  cobirb --persona <name>       Adopt a persona for this run.
  /persona                      Pick one from a list, "none" included.
  /persona <name>               Switch to a named persona directly.

Personas are OFF by default. A persona is a costume for the model — a name, a
species, a tone, stock phrases — and CoBirb sends it as a system message,
which replaces whatever SYSTEM directive the local model's own Modelfile
sets. Wearing one by default would silently override your model's own
configuration on every turn, so an unconfigured run sends no voice
instructions at all and the model sounds like itself.

Bundled: noah, professional, neighbor, kawaii. Set "persona" in config to
adopt one by default, or point --persona at your own <name>.json (same shape
as the bundled files — see cobirb/personas/*.json). "none" turns it back
off.

Personas are pure data: name, tone, greeting, phrasings, emoji density,
squawks. They shape tone only and can never grant permission to skip
encryption, network, or permission controls — the CLI's system prompt says
so explicitly, and no persona field can override it.
""",
    "plan": """\
PLAN MODE — explicit plan → act → validate

Off by default: the model plans, acts, and checks its own work implicitly
in one pass, which is how CoBirb behaves normally. Turned on, a run becomes
three separate model phases instead, each recorded as its own turn:

  1. plan      One reply, no tools available — a short, numbered plan for
               how the request will be fulfilled. Shown to you immediately.
  2. act       The normal tool-using loop, following that plan.
  3. validate  A bounded follow-up loop (tools available) that checks the
               work — re-reading files, re-running tests/commands — and
               reports, with concrete references, whether and how the
               request was actually fulfilled. Stored as the session's
               "validation" field alongside its usual summary.

Turn on/off:
  cobirb --plan-mode on|off     For this run (overrides config below).
  /plan on|off                  Mid-conversation, interactively.
  /plan                         Show whether it's currently on.
  "plan_mode": true             In config, as the default when neither
                                 --plan-mode nor /plan has been used yet.

Plan mode costs at least one extra model call per turn (the plan), and up
to a few more (the validate phase, bounded like the act phase); expect
slower turns in exchange for the explicit checkpoints.
""",
    "model": """\
MODEL — choosing what CoBirb talks to

CoBirb ships no model of its own; it talks to whatever OpenAI-compatible
endpoint you point it at (a local Ollama server by default). No model is
selected until you name one:

  --model NAME          For this run.
  "default_model"       In config, as the default for every run — tried at
                         interactive startup and silently ignored (not an
                         error) if that model can't be found there.
  /model                 Interactively: fetches the list of models the
                         configured endpoint currently has and lets you
                         pick one, for this session only.

If interactive mode starts with no working model — nothing configured, or
"default_model" named one that isn't there — it fetches the list itself and
opens the same picker /model would, so you're never left staring at a
session with nothing to talk to.

YOUR MODEL'S OWN SYSTEM PROMPT

Ollama accepts one system message per request, and sending one REPLACES the
SYSTEM directive the model was built with. A model you made with
'ollama create' around a custom SYSTEM is a configuration you chose, so
CoBirb does not overwrite it:

  • By default CoBirb sends no system message at all. Your model behaves
    inside CoBirb exactly as it does in 'ollama run' — same SYSTEM, same
    voice, same everything.
  • When CoBirb does have something to add (a persona, plan-mode phase
    instructions, or --system-prompt harness), it reads your model's own
    SYSTEM back via /api/show and puts it FIRST, then appends its own part.
    Yours is supplemented, never discarded.

  --system-prompt off       The default. Nothing of CoBirb's is sent.
  --system-prompt harness   Add a short description of the tool-permission
                            model. Worth trying if a model keeps retrying a
                            tool call you denied; it has no other effect.
  "system_prompt"           The same choice in config.

Nothing about CoBirb's actual guarantees depends on any of this: permissions
are enforced in policy.py and sessions are encrypted by the crypto backend,
not by asking a model to cooperate.
""",
    "plugins": """\
PLUGINS — bolt-on capabilities

Discovered from installed entry points and local cobirb/plugins/<name>/
directories each time an orchestrator is built. Never fatal: a broken
plugin, or one whose declared name collides with an existing tool, is
reported and skipped. In interactive mode, the Plugins tab shows exactly
what was discovered, what's active, and any such problems live.

  • Tool plugins are additive: every discovered one is registered
    alongside the built-ins (the permission policy still gates whether it
    can actually run).
  • Model/I/O/crypto plugins are singleton slots: a discovered one replaces
    the matching core default only when explicitly selected in config —
    "plugins": {"model": "<name>", "io": "<name>", "crypto": "<name>"}.
""",
    "tools": """\
TOOLS — what the agent can do

  read_file      Read a file's contents.
  write_file     Create/overwrite a file.
  edit_file      Replace an exact old_str with new_str in a file.
  apply_patch    Apply a unified-diff patch to a file.
  glob           Find files matching a glob pattern.
  grep           Search file contents by regex.
  list_dir       List a directory's contents.
  shell          Run a shell command. Highest privilege; gated.

Nothing is permitted up front. Every tool call you haven't already allowed
prompts you to permit it once, always, or not at all — and what "always"
covers depends on the tool:

  read_file, list_dir,   The directory the call names, and everything under
  glob, grep             it. Say yes once for a project and CoBirb can read
                         it without asking again.
  write_file, edit_file, The tool itself, for the rest of the run. There is
  apply_patch            no directory shortcut for changing files.
  shell                  Exactly the invocation you approved — 'git' if you
                         approved a bare binary, 'python -m pytest' if you
                         approved that. Never more.

To skip the prompts for things you always want, list them yourself with
--allow-tool='name' or --allow-tool='name(arg)' (e.g. 'shell(git)'), repeat
the flag, or set "allow_tools" in config. A command CoBirb cannot fully
read — command substitution, a subshell, or find's -exec — is refused
rather than guessed at. Third-party tool plugins extend this list and are
gated identically — see 'cobirb help plugins'.
""",
    "config": """\
CONFIG — user + repo scoped settings

Read from (repo overrides user): ~/.cobirb/config.json, then ./cobirb.json
(relative to --cwd). See cobirb.json.example for a starting point. Keys:

  "default_model"                Model to use by default (or --model/
                                 COBIRB_MODEL_NAME). Validated at
                                 interactive startup — see 'cobirb help
                                 model'.
  "model"                        Older, equivalent name for the same thing.
  "models": {"default": {
      "name": "...", "base_url": "..."}}  Model name/endpoint (local Ollama
                                 by default; any OpenAI-compatible server
                                 works).
  "allow_tools"                  Permission rules you always want, as a list
                                 in --allow-tool's syntax, e.g.
                                 ["read_file", "shell(git)",
                                  "shell(python -m pytest)"].
                                 Nothing is permitted without a rule here or
                                 an approval at the prompt.
  "persona"                      Persona to adopt by default (or --persona).
                                 Unset means none: the model keeps its voice.
  "system_prompt"                "off" (default) or "harness" — whether
                                 CoBirb sends a system prompt of its own.
                                 See 'cobirb help model'.
  "plugins": {"model"|"io"|"crypto": "<name>"}
                                 Select a discovered plugin for that slot
                                 (see 'cobirb help plugins'); the core
                                 default is kept when unset.
  "plan_mode"                    Start with plan mode on (default: false).
                                 See 'cobirb help plan'.
  "audit_log"                    Keep a local record of every tool call —
                                 name, arguments, cwd, timestamp — at
                                 ~/.cobirb/audit.jsonl (default: false).
                                 Off by default because the arguments
                                 logged are whatever a tool call actually
                                 carried, unredacted: write_file's full
                                 content, edit_file's full old/new text,
                                 apply_patch's full diff, shell's full
                                 command. Unlike sessions, this log is
                                 plain text, not encrypted — only turn it
                                 on if you want that trail and understand
                                 what ends up in it.

Nothing here ever defaults to a networked provider — model/provider
settings are opt-in, matching CoBirb's no-network-by-default rule.
""",
}


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nNoah: goodnight! 🐦")
        sys.exit(130)
