"""Built-in tools and the tool registry.

Tools are the "hands" of the agent. The core ships a registry of built-in tools;
third-party plugins extend it. The registry enforces a default-deny policy:
every tool must be explicitly allowed before it runs.

One module per family — ``files`` (read, write, delete), ``edit``, ``patch``,
``search`` (glob, grep, list_dir, repo_map), ``shell``, ``todo`` — over
``base``, which holds ``CobirbTool`` and the limits more than one family uses.
The registry is here, and every tool class is importable from here.
"""

from __future__ import annotations

import os

from ....typing.spi import Tool, ToolResult
from .base import CobirbTool
from .edit import EditFileTool
from .files import DeleteFileTool, ReadFileTool, WriteFileTool
from .patch import ApplyPatchTool
from .search import GlobTool, GrepTool, ListDirTool, RepoMapTool
from .shell import ShellTool
from .todo import TodoTool

__all__ = [
    "BUILTIN_TOOLS",
    "ApplyPatchTool",
    "CobirbTool",
    "DeleteFileTool",
    "EditFileTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "ReadFileTool",
    "RepoMapTool",
    "ShellTool",
    "TodoTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "WriteFileTool",
]

BUILTIN_TOOLS: list[type[Tool]] = [
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ApplyPatchTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    RepoMapTool,
    ShellTool,
    TodoTool,
    DeleteFileTool,
]


class ToolRegistry:
    """Holds the built-in tools and supports plugin extension."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self._tools: dict[str, Tool] = {}
        for tool_cls in BUILTIN_TOOLS:
            self.register(tool_cls(self.cwd))

    def register(self, tool: Tool) -> None:
        """Register ``tool`` under the name it reports.

        Validates here rather than tolerating a non-conformant tool, because
        tolerance is what let a bound method reach a JSON payload once
        already: a tool whose ``name`` isn't the method the SPI documents is
        rejected at the boundary with a message that says so, and the caller
        (see ``runtime.plugins.discover_plugins``) reports and skips it rather than
        letting it break a turn much later.
        """
        if not callable(tool.name):
            raise TypeError(
                f"{type(tool).__name__}.name must be a method returning a string, "
                "as the Tool interface declares — not a plain attribute"
            )
        self._tools[tool.name()] = tool

    @property
    def tools(self) -> dict[str, Tool]:
        """The live name -> tool mapping the orchestrator dispatches against.

        Public because the orchestrator is constructed with it; callers had
        been reaching into ``registry._tools`` to get the same object.
        """
        return self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def values(self) -> list[Tool]:
        """Return the registered tools in registration order."""
        return list(self._tools.values())

    def is_known(self, name: str) -> bool:
        return name in self._tools
