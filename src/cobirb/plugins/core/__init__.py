"""Built-in (core) plugins.

These plugins are always available. Third-party plugins extend the same registry
without touching this module.
"""
from __future__ import annotations

from .crypto import AesGcmScryptSessionCrypto
from .io import TerminalIO
from .model import LocalModelProvider
from .tools import ToolRegistry

__all__ = [
    "ToolRegistry",
    "LocalModelProvider",
    "TerminalIO",
    "AesGcmScryptSessionCrypto",
]
