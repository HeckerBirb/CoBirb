"""Core type definitions and the Plugin SPI interfaces.

This module defines the *contract* that plugins implement. The core depends on
these interfaces but never imports plugin internals. See PLUGIN_SPEC.md for the
formal specification.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# --------------------------------------------------------------------------- #
# Shared data types
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """A structured tool invocation emitted by the model.

    Attributes:
        name:       machine name of the tool (e.g. ``read_file``).
        arguments:  keyword arguments passed to the tool.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #
class Tool(abc.ABC):
    """A capability exposed to the model. Implement the actual logic in ``execute``.

    Implementing plugins OR core. The core ships a registry of built-in tools
    (read_file, write_file, shell, ...); third parties extend it.
    """

    @abc.abstractmethod
    def name(self) -> str:
        """Short machine name, e.g. ``read_file``. Used for allow/deny matching."""

    @abc.abstractmethod
    def description(self) -> str:
        """Natural-language description of what this tool does."""

    @abc.abstractmethod
    def parameters(self) -> dict[str, Any]:
        """Parameter schema in a simple JSON-schema-like shape."""

    @abc.abstractmethod
    def execute(self, arguments: dict[str, Any]) -> "ToolResult":
        """Run the tool. Implement the actual logic here."""


@dataclass
class ToolResult:
    """Result of a tool execution."""

    ok: bool
    content: str
    error: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Model provider
# --------------------------------------------------------------------------- #
class ModelProvider(abc.ABC):
    """Turns a system prompt + context into agent turns. Local or remote.

    Remote providers (network) are NOT part of core. They are registered as
    plugins and gated behind the permission layer. See DESIGN.md §5.
    """

    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable identifier, e.g. ``ollama/llama3.1``."""

    @abc.abstractmethod
    def chat(
        self,
        system: str,
        context: str,
        tools: Optional[list[Tool]] = None,
        *,
        stream: bool = False,
    ) -> "Iterable[str] | str":
        """Return the assistant's reply.

        When ``stream=True`` yield tokens incrementally; when ``stream=False``
        return the full string.
        """

    @abc.abstractmethod
    def parse_tool_calls(self, raw: str) -> list[ToolCall]:
        """Extract structured tool calls from a raw assistant message, if any."""

    @abc.abstractmethod
    def supports_tool_calling(self) -> bool:
        """Whether the model can emit structured tool invocations."""

    @abc.abstractmethod
    def supports_streaming(self) -> bool:
        """Whether incremental streaming is supported."""

    @abc.abstractmethod
    def supports_vision(self) -> bool:
        """Whether the model can consume image bytes (relevant to vision adapters)."""


# --------------------------------------------------------------------------- #
# I/O adapter (future: speech & vision)
# --------------------------------------------------------------------------- #
class I_OAdapter(abc.ABC):
    """Renders output and optionally captures input for the user.

    Out-of-scope for v0.1.0 (core ships a plain-text terminal renderer).
    Implementations may be added in v0.2.0. See DESIGN.md §5.3.
    """

    @abc.abstractmethod
    def name(self) -> str:
        """Adapter identifier, e.g. ``speech`` or ``vision``."""

    @abc.abstractmethod
    def render(self, text: str) -> None:
        """Render text to the user (override the default terminal print)."""

    @abc.abstractmethod
    def listen(self) -> Optional[str]:
        """Return user input captured from a non-text channel. None if none."""

    @abc.abstractmethod
    def view(self, data: bytes, mime: str | None = None) -> None:
        """Present binary data (e.g. an image)."""

    @abc.abstractmethod
    def confirm(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Ask the user whether to allow a tool call the policy hasn't
        already permitted. Must return one of:

        - ``"once"``: allow this one call, don't change the policy.
        - ``"always"``: allow this call and update the policy so matching
          calls skip the prompt for the rest of this run.
        - ``"deny"``: refuse the call.

        Adapters with no way to ask (e.g. a future non-interactive one)
        should return ``"deny"`` — permission is fail-closed, not fail-open.
        """


# --------------------------------------------------------------------------- #
# Crypto backend (future: swapable encryption)
# --------------------------------------------------------------------------- #
class SessionCrypto(abc.ABC):
    """Encrypts/decrypts session files. Default backend: AES-256-GCM + scrypt.

    The core ships no hard crypto dependency. See DESIGN.md §7.2 (including
    why this deliberately has no post-quantum KEM).
    """

    @abc.abstractmethod
    def name(self) -> str:
        """Identifier of the scheme, e.g. ``aes256gcm-scrypt``."""

    @abc.abstractmethod
    def encrypt(self, plaintext_json: str, password: str) -> bytes:
        """Return the encrypted session blob. Must not return the password."""

    @abc.abstractmethod
    def decrypt(self, blob: bytes, password: str) -> str:
        """Return the decrypted JSON string, or raise on wrong password/corruption."""


# --------------------------------------------------------------------------- #
# Persona
# --------------------------------------------------------------------------- #
@dataclass
class Persona:
    """Pure data describing how CoBirb speaks. Never affects behavior/permissions."""

    name: str
    species: str = "Parrot"
    tone: str = "friendly, playful, not saccharine"
    greeting: str = ""
    phrasings: list[str] = field(default_factory=list)
    emoji_density: str = "light"
    known_squawks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a persona data dict."""
        return {
            "name": self.name,
            "species": self.species,
            "tone": self.tone,
            "greeting": self.greeting,
            "phrasings": self.phrasings,
            "emoji_density": self.emoji_density,
            "known_squawks": self.known_squawks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Persona":
        """Deserialize from a persona data dict."""
        return cls(
            name=data.get("name", "Noah"),
            species=data.get("species", "Parrot"),
            tone=data.get("tone", "friendly, playful, not saccharine"),
            greeting=data.get("greeting", ""),
            phrasings=list(data.get("phrasings", [])),
            emoji_density=data.get("emoji_density", "light"),
            known_squawks=list(data.get("known_squawks", [])),
        )
