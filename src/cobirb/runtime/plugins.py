"""Plugin discovery, slot resolution, and the snapshot the Plugins tab shows.

Tools are additive and every discovered one is registered; model, I/O and
crypto are singleton slots a config selection must name explicitly. Problems
are reported and skipped — a broken plugin never brings the core down.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..mcp import McpTool
from ..plugins.core import AesGcmScryptSessionCrypto, ToolRegistry
from ..plugins.core.tools import BUILTIN_TOOLS
from ..plugins.loader import load_plugins


def report_plugin_issues(issues: dict[str, str]) -> None:
    """Print discovery/merge problems for third-party plugins to stderr.

    Never fatal: a broken or colliding plugin is reported and skipped, the
    core keeps running on its built-ins.
    """
    for ident, message in issues.items():
        print(f"cobirb: plugin problem — {ident}: {message}", file=sys.stderr)


def merge_tool_plugins(registry: ToolRegistry, discovered: dict[str, Any]) -> dict[str, str]:
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


def discover_plugins(cwd: str, config: Config) -> tuple[ToolRegistry, dict[str, Any], dict[str, str]]:
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
    tool_issues = merge_tool_plugins(registry, discovered)
    return registry, discovered, {**plugin_errors, **tool_issues}


def select_plugin(kind: str, discovered: dict[str, Any], config: Config) -> tuple[Any | None, str | None]:
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


def resolve_slot(
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
    cls, issue = select_plugin(kind, discovered, config)
    if issue:
        return default(), issue
    if cls is None:
        return default(), None
    try:
        return cls(), None
    except Exception as exc:  # noqa: BLE001 - fail closed to the core default
        return default(), f"failed to instantiate: {exc}"


def build_crypto(config: Config, discovered: dict[str, Any]) -> tuple[Any, str | None]:
    """Resolve the session crypto backend: the core AES-256-GCM+scrypt
    default, or a config-selected ``plugins.crypto`` plugin."""
    return resolve_slot("crypto", discovered, config, AesGcmScryptSessionCrypto)


@dataclass
class ToolInfo:
    """One registered tool, for display (the TUI's Plugins tab)."""

    name: str
    description: str
    # "core", "plugin", or "mcp". MCP is called out separately rather than
    # lumped in with plugins because the distinction is the one a person
    # actually cares about here: a plugin is Python running in this process, an
    # MCP tool is a separate program that can do things CoBirb cannot see.
    source: str


@dataclass
class PluginsSummary:
    """A live snapshot of what's discovered/active, for display."""

    slots: dict[str, str] = field(default_factory=dict)
    tools: list[ToolInfo] = field(default_factory=list)
    issues: dict[str, str] = field(default_factory=dict)
    # Configured MCP servers, by name — *not* started. This snapshot exists to
    # populate a tab, and starting somebody's database proxy to draw a list
    # would be an absurd side effect of opening it. Their tools therefore
    # appear here only once a turn has built a real orchestrator; until then
    # the names are what can honestly be shown.
    mcp_servers: list[str] = field(default_factory=list)


def describe_plugins(cwd: str) -> PluginsSummary:
    """Gather a live snapshot of discovered plugins and registered tools.

    Needs no model and builds no orchestrator, so it's safe and cheap to
    call just to populate a display (the TUI's Plugins tab) — it never
    reports discovery/merge issues to stderr itself (unlike
    ``build_orchestrator``'s own call to plugin discovery); the caller
    decides how to show them.
    """
    config = Config(cwd=cwd)
    registry, discovered, issues = discover_plugins(cwd, config)

    slots: dict[str, str] = {}
    for kind in ("model", "io", "crypto"):
        cls, issue = select_plugin(kind, discovered, config)
        if issue:
            slots[kind] = f"core (config error: {issue})"
        elif cls is not None:
            slots[kind] = str(config.get("plugins", kind))
        else:
            slots[kind] = "core"

    tools = sorted(
        (
            ToolInfo(name=tool.name(), description=tool.description(), source=_source_of(tool))
            for tool in registry.values()
        ),
        key=lambda info: info.name,
    )
    return PluginsSummary(
        slots=slots, tools=tools, issues=issues, mcp_servers=configured_mcp_servers(config)
    )


def configured_mcp_servers(config: Config) -> list[str]:
    """Names of the MCP servers the user has configured, without starting any."""
    block = config.user_get("mcp_servers", default={}) or {}
    if not isinstance(block, dict):
        return []
    return sorted(
        str(name) for name, spec in block.items()
        if isinstance(spec, dict) and spec.get("enabled") is not False
    )


def _source_of(tool: Any) -> str:
    if type(tool) in BUILTIN_TOOLS:
        return "core"
    return "mcp" if isinstance(tool, McpTool) else "plugin"
