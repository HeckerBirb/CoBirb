"""MCP: tools that live in someone else's process.

CoBirb's own tools are the ones a coding agent always needs — read, write,
grep, run. Everything else is somebody's specific world: their issue tracker,
their database, their build system, their house style guide. The Model Context
Protocol is the interface that world can be handed over, and supporting it as a
*client* means CoBirb gains those tools without CoBirb having to know anything
about them.

Only **stdio** transport is implemented, and deliberately so. An MCP server over
stdio is a subprocess on this machine, talking over a pipe — which is the only
shape of this that keeps CoBirb's founding promise intact. HTTP/SSE transports
point at a URL, and a URL is a network call CoBirb did not make and cannot see
inside. See ``cobirb help mcp`` for what a configured server can and cannot do.
"""
from __future__ import annotations

from .client import McpError, StdioClient
from .tools import McpTool, connect_servers, tool_name_for

__all__ = ["McpError", "StdioClient", "McpTool", "connect_servers", "tool_name_for"]
