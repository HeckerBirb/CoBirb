"""CoBirb — a privacy-first, agentic coding CLI.

The core is intentionally thin: it wires together pluggable providers
(model, tools, I/O, crypto, persona) and enforces a privacy-first,
default-deny policy layer.
"""
from __future__ import annotations

__version__ = "0.12.4"

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
    "Persona",
    "SteeringInterrupted",
    # The frozen SPI's own version surface (v0.7.0) — a plugin author checking
    # compatibility at runtime should not have to import from a private path.
    "SPI_VERSION",
    "MIN_SUPPORTED_SPI_VERSION",
    "IncompatiblePlugin",
    "__version__",
]
