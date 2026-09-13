"""CoBirb — a privacy-first, Copilot-like agentic CLI.

The core is intentionally thin: it wires together pluggable providers
(model, tools, I/O, crypto, persona) and enforces a privacy-first,
default-deny policy layer.
"""
from __future__ import annotations

__version__ = "0.5.1"

from .typing.spi import (  # noqa: F401  (public SPI)
    I_OAdapter,
    ModelProvider,
    Persona,
    SessionCrypto,
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
    "Persona",
    "__version__",
]
