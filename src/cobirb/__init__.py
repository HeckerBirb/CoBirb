"""CoBirb — a privacy-first, agentic coding CLI.

The core is intentionally thin: it wires together pluggable providers
(model, tools, I/O, crypto) and enforces a privacy-first,
default-deny policy layer.
"""
from __future__ import annotations

from importlib import metadata as _metadata

# Read from the installed distribution, which `scripts/release.sh` keeps in
# step with pyproject.toml. A literal here was bumped by nobody: it said 0.13.1
# for eight releases, and the MCP client reports it to every server it starts.
try:
    __version__ = _metadata.version("cobirb")
except _metadata.PackageNotFoundError:  # running from a tree that was never installed
    __version__ = "0+unknown"

from .typing.spi import (  # noqa: F401  (public SPI)
    MIN_SUPPORTED_SPI_VERSION,
    SPI_VERSION,
    I_OAdapter,
    IncompatiblePlugin,
    ModelProvider,
    Persona,
    SessionCrypto,
    SteeringInterrupted,
    Tool,
    ToolCall,
    ToolResult,
)

__all__ = [
    "ModelProvider",
    "Tool",
    "ToolResult",
    "ToolCall",
    "I_OAdapter",
    "SessionCrypto",
    # Unused since personas were removed; kept so v1 plugins importing it load.
    "Persona",
    "SteeringInterrupted",
    # The frozen SPI's own version surface (v0.7.0) — a plugin author checking
    # compatibility at runtime should not have to import from a private path.
    "SPI_VERSION",
    "MIN_SUPPORTED_SPI_VERSION",
    "IncompatiblePlugin",
    "__version__",
]
