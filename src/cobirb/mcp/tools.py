"""Presenting an MCP server's tools as CoBirb tools.

Each remote tool becomes one ``Tool`` in the registry, so everything downstream
— the schema sent to the model, the approval prompt, the permission policy, the
audit log, the redaction pass — treats it exactly like a built-in. That is the
point: MCP is a way of *acquiring* tools, not a second, parallel set of rules
about what tools may do.

**Naming.** ``mcp__<server>__<tool>`` — long, and deliberately legible. The
model sees where a tool came from, the approval prompt does too, and a
permission rule like ``"allow_tools": ["mcp__postgres__query"]`` says which
server it trusts rather than trusting a bare ``query`` that could be anything.

**Nothing is pre-approved.** MCP tools land in the same default-deny policy as
everything else, and they are *not* members of ``READ_TOOLS``: CoBirb cannot
know whether a remote ``fetch_issue`` reads, writes or bills someone, so the
directory-scoped read grant — which exists because reading a project's files is
one decision, not a thousand — would be an unsafe generalisation. Approving an
MCP tool with "always" grants that one tool, and only for the session.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from ..typing.spi import Tool, ToolResult
from .client import DEFAULT_CALL_TIMEOUT, DEFAULT_STARTUP_TIMEOUT, McpError, StdioClient

logger = logging.getLogger("cobirb")

PREFIX = "mcp"
_SEPARATOR = "__"


def tool_name_for(server: str, tool: str) -> str:
    """The CoBirb-side name for a tool offered by ``server``."""
    return f"{PREFIX}{_SEPARATOR}{server}{_SEPARATOR}{tool}"


class McpTool(Tool):
    """One tool on one MCP server.

    Holds the client rather than a connection factory: the server is a running
    process for the life of the session, and reconnecting per call would pay a
    process start for every invocation of something that is often called in a
    tight sequence.
    """

    def __init__(self, client: StdioClient, server: str, spec: dict[str, Any]) -> None:
        self._client = client
        self._server = server
        self._remote_name = str(spec.get("name", ""))
        self._description = str(spec.get("description") or "").strip()
        self._schema = spec.get("inputSchema") or spec.get("input_schema") or {}

    def name(self) -> str:
        return tool_name_for(self._server, self._remote_name)

    def description(self) -> str:
        """The server's own description, said to be the server's.

        Attribution matters here in a way it does not for a built-in: a model
        choosing between ``grep`` and ``mcp__notes__search`` should know that
        the second one is somebody else's code, and a user reading an approval
        prompt certainly should.
        """
        body = self._description or f"The '{self._remote_name}' tool."
        return f"{body} (via the '{self._server}' MCP server)"

    def parameters(self) -> dict[str, Any]:
        """The server's JSON Schema, passed through.

        MCP and Ollama both speak JSON Schema for tool parameters, so there is
        nothing to translate. A server that declares nothing gets an empty
        object schema rather than ``{}`` — some models refuse a tool whose
        parameters have no ``type``.
        """
        if isinstance(self._schema, dict) and self._schema:
            return self._schema
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Call the remote tool.

        A dead or unreachable server is reported as a failed tool result, not
        raised: the orchestrator's contract is that a tool failing is routine
        and the model gets to react to it. A message naming the server is far
        more use to the model than a traceback, because "the notes server is
        not running" is something it can work around.
        """
        try:
            ok, text = self._client.call_tool(self._remote_name, arguments or {})
        except McpError as exc:
            return ToolResult(ok=False, content=f"MCP call failed: {exc}", error="mcp_error")
        return ToolResult(
            ok=ok,
            content=text or ("(no output)" if ok else "the server reported an error"),
            error=None if ok else "tool_error",
            meta={"mcp_server": self._server, "mcp_tool": self._remote_name},
        )


def _server_specs(config: Config) -> dict[str, dict[str, Any]]:
    """The configured servers, from the **user's** config only.

    Not from a repository's ``cobirb.json``: a server entry is a command line,
    so honouring one from a cloned directory would make cloning it sufficient
    to run its author's code. See ``Config.user_get``.
    """
    block = config.user_get("mcp_servers", default={}) or {}
    if not isinstance(block, dict):
        logger.warning('ignoring "mcp_servers": expected an object of name -> server')
        return {}
    return {str(name): spec for name, spec in block.items() if isinstance(spec, dict)}


def connect_servers(
    config: Config, cwd: str | None = None
) -> tuple[list[McpTool], list[StdioClient], dict[str, str]]:
    """Start every configured MCP server and collect the tools they offer.

    Returns ``(tools, clients, issues)``. ``clients`` is handed back so the
    caller can close them at the end of the run — this module deliberately
    does not register an ``atexit`` handler, because a process that has
    several orchestrators over its lifetime (the TUI, rebuilding one after a
    ``/model`` switch) would accumulate them.

    A server that fails to start is an ``issue`` and never an exception. The
    session is worth more than any one server, and the report says which one
    and why so it can be fixed — a silent absence would present as the model
    inexplicably not having a tool the user knows they configured.
    """
    tools: list[McpTool] = []
    clients: list[StdioClient] = []
    issues: dict[str, str] = {}

    for name, spec in _server_specs(config).items():
        if spec.get("enabled") is False:
            continue
        command = spec.get("command")
        if not command:
            issues[f"mcp:{name}"] = "no command; skipped"
            continue
        client = StdioClient(
            name=name,
            command=str(command),
            args=[str(a) for a in (spec.get("args") or [])],
            env=spec.get("env") or {},
            cwd=str(spec.get("cwd") or cwd or "."),
            inherit_env=bool(spec.get("inherit_env")),
            startup_timeout=_int(spec.get("startup_timeout"), DEFAULT_STARTUP_TIMEOUT),
            call_timeout=_int(spec.get("timeout"), DEFAULT_CALL_TIMEOUT),
        )
        try:
            client.start()
            offered = client.list_tools()
        except McpError as exc:
            issues[f"mcp:{name}"] = str(exc)
            client.close()
            continue
        except Exception as exc:  # noqa: BLE001 - a third-party process, fail closed
            issues[f"mcp:{name}"] = f"unexpected failure: {exc}"
            client.close()
            continue

        clients.append(client)
        for entry in offered:
            if not entry.get("name"):
                continue
            tools.append(McpTool(client, name, entry))
        logger.info("mcp/%s: %d tool(s)", name, len(offered))

    return tools, clients, issues


def _int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
