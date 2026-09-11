"""Built-in (core) plugins.

These plugins are always available. Third-party plugins extend the same registry
without touching this module. See PLUGIN_SPEC.md §5.2.
"""
from __future__ import annotations

from .crypto import HybridPQCSessionCrypto
from .io import TerminalIO
from .model import LocalModelProvider
from .persona import build_default_persona, persona_to_json
from .tools import ToolRegistry

__all__ = [
    "ToolRegistry",
    "LocalModelProvider",
    "TerminalIO",
    "HybridPQCSessionCrypto",
    "build_default_persona",
    "persona_to_json",
]
