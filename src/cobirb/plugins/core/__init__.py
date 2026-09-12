"""Built-in (core) plugins.

These plugins are always available. Third-party plugins extend the same registry
without touching this module.
"""
from __future__ import annotations

from .crypto import AesGcmScryptSessionCrypto
from .io import TerminalIO
from .model import LocalModelProvider
from .persona import (
    PLAIN_PERSONA_NAME,
    build_default_persona,
    build_plain_persona,
    persona_shapes_voice,
    persona_to_json,
)
from .tools import ToolRegistry

__all__ = [
    "ToolRegistry",
    "LocalModelProvider",
    "TerminalIO",
    "AesGcmScryptSessionCrypto",
    "PLAIN_PERSONA_NAME",
    "build_default_persona",
    "build_plain_persona",
    "persona_shapes_voice",
    "persona_to_json",
]
