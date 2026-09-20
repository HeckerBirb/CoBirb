"""Core type definitions and the Plugin SPI interfaces.

This module defines the *contract* that plugins implement. The core depends on
these interfaces but never imports plugin internals.

**This contract is frozen as of v0.7.0.** What that means, precisely, because
"frozen" is a word people read optimistically:

- Within an SPI version, changes are **additive only**. A new optional,
  duck-typed hook (the way ``cancel()`` and ``interrupt_current_reply()``
  arrived) is fine; a plugin that has never heard of it keeps working. New
  *abstract* methods, renamed methods, and changed signatures are not fine,
  because every existing plugin breaks the moment core calls them.
- A change that cannot be made additively bumps ``SPI_VERSION``. That is a
  deliberate, visible event, not something a refactor does by accident.
- ``MIN_SUPPORTED_SPI_VERSION`` is how long old plugins keep working. Raising
  it is how support is eventually dropped, and is equally deliberate.

A plugin declares the version it was written against with a class attribute:

    class MyTool(Tool):
        COBIRB_SPI = 1

Declaring nothing means 1 — everything written before the freeze existed, and
the overwhelming majority of plugins that will never care. The loader checks
the number and refuses, *non-fatally*, anything it cannot honestly support:
a plugin from the future would be calling into methods this core does not
have, and loading it to fail later at a worse moment helps nobody.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# The SPI revision this core implements, and the oldest it still accepts.
# Both are 1: the interface is being frozen at its first stable shape rather
# than renumbered to mark the occasion.
SPI_VERSION = 1
MIN_SUPPORTED_SPI_VERSION = 1

# What a plugin class sets to declare which revision it was written against.
# A plain class attribute rather than metadata or an extra entry point: it is
# visible in the plugin's own source next to the class it describes, needs no
# packaging ceremony, and costs a plugin author who doesn't care exactly
# nothing, since its absence means 1.
SPI_VERSION_ATTRIBUTE = "COBIRB_SPI"


class IncompatiblePlugin(Exception):
    """A plugin declares an SPI version this core cannot honour.

    Separate from ``plugins.loader.PluginError`` (something went wrong while
    loading) because nothing went wrong here: the plugin is intact and the
    core is intact, and they simply do not have a contract in common. The
    distinction matters for what the message should say — "this plugin needs
    a newer CoBirb" is a different instruction to its user than "this plugin
    is broken".
    """


def check_spi_version(plugin: Any) -> None:
    """Raise ``IncompatiblePlugin`` unless ``plugin`` declares a usable SPI.

    A missing declaration is version 1, as documented above. A declaration
    that isn't an integer is treated as a mistake rather than quietly ignored:
    ``COBIRB_SPI = "1"`` is exactly the sort of thing that would otherwise
    compare unequal to every supported version forever, or worse, be silently
    skipped and let a genuinely incompatible plugin through.
    """
    declared = getattr(plugin, SPI_VERSION_ATTRIBUTE, MIN_SUPPORTED_SPI_VERSION)
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise IncompatiblePlugin(
            f"{SPI_VERSION_ATTRIBUTE} must be an integer, not {declared!r}"
        )
    if declared > SPI_VERSION:
        raise IncompatiblePlugin(
            f"needs CoBirb SPI v{declared}, but this CoBirb implements v{SPI_VERSION} — "
            "upgrade CoBirb, or use a build of the plugin written for this version"
        )
    if declared < MIN_SUPPORTED_SPI_VERSION:
        raise IncompatiblePlugin(
            f"was written for CoBirb SPI v{declared}, which this CoBirb does not support "
            f"(oldest accepted: v{MIN_SUPPORTED_SPI_VERSION})"
        )


# --------------------------------------------------------------------------- #
# Shared data types
# --------------------------------------------------------------------------- #
# The three answers `I_OAdapter.confirm` may give. A three-value protocol
# compared by string literal at four boundaries (the orchestrator, both
# shipped adapters, the approval modal) is one typo away from silently
# meaning "deny", which is safe but wrong.
DECISION_ONCE = "once"
DECISION_ALWAYS = "always"
DECISION_DENY = "deny"
# Allow this call and every matching one for the rest of the CoBirb session,
# across every agent in it — the main conversation and every Worker Birb,
# including ones that have not started yet (see `policy.SessionGrants`).
# Distinct from ALWAYS, which only ever widens the one policy it was asked
# about: a Worker Birb's policy is built per worker and dies with it, so
# "always" answered in a flock would re-ask on the next ticket and the next
# round. A session grant lives in memory for the life of the process and is
# never written to config — a permission that survives a restart is a
# different decision from one made live in a dialog.
DECISION_SESSION = "session"
DECISIONS = frozenset({DECISION_ONCE, DECISION_ALWAYS, DECISION_SESSION, DECISION_DENY})


@dataclass
class ApprovalRequest:
    """Everything an adapter needs to ask "may I run this?" well.

    A dataclass rather than more positional arguments: what a user needs to
    see before approving has grown twice already (the scope an "always" would
    grant, then a preview of the change), and each growth would otherwise be
    another incompatible signature for the optional ``confirm_scoped`` hook.
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Plain-language description of what "always" would permit, from
    # Policy.describe_grant — approving a read widens to a whole directory.
    scope: str = ""
    # What the call would actually do, where that can be shown before doing
    # it: a unified diff for an edit, the patch for apply_patch. Empty for
    # tools whose arguments already say everything (read_file, shell).
    preview: str = ""
    # Which agent is asking, where that is not "the one you are talking to" —
    # a Worker Birb's id. Empty for the main conversation. An adapter that
    # shows this can say *who* wants the capability, which in a flock is half
    # the question: several agents are working at once and they are not
    # interchangeable.
    asked_by: str = ""


@dataclass
class ApprovalOutcome:
    """An answer to an ``ApprovalRequest``, for adapters that can say more
    than one of the three decision strings.

    Exists because "no" is the least useful thing a person can tell an agent
    that has just asked for something. A refusal carrying *what to do
    instead* turns a dead end into a redirection: the text reaches the model
    as part of the tool result, so the next turn acts on it rather than
    retrying the same call or giving up.

    Returned by the optional ``confirm_request`` hook. Adapters implementing
    only ``confirm``/``confirm_scoped`` keep returning a plain string and lose
    nothing but the instruction.
    """

    decision: str
    # Only meaningful alongside DECISION_DENY. Empty means a plain refusal.
    instruction: str = ""
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


class SteeringInterrupted(Exception):
    """A model reply was deliberately cut off mid-stream to steer the turn.

    Raised by a ``ModelProvider`` (from inside ``chat()``'s stream, when it
    implements the optional, duck-typed ``interrupt_current_reply()`` — see
    ``ModelProvider`` below) to tell the orchestrator "this wasn't a failure,
    a person redirected the turn while I was still answering." The
    orchestrator catches it by type to keep whatever content streamed so far
    as a genuine (if incomplete) assistant turn, rather than reporting the
    turn as broken — the same distinction ``cancel()``'s one-way stop makes
    for ending a run outright, but resumable: the provider is expected to
    serve the very next request normally.
    """


# --------------------------------------------------------------------------- #
# Model provider
# --------------------------------------------------------------------------- #
class ModelProvider(abc.ABC):
    """Turns a system prompt + context into agent turns. Local or remote.

    Remote providers (network) are NOT part of core. They are registered as
    plugins and gated behind the permission layer.
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

    # Optional, duck-typed, not part of this ABC (the same way `cancel()`
    # isn't): `cancel()` stops a provider for good; `interrupt_current_reply()
    # -> bool` cuts off only the reply in progress — raising
    # `SteeringInterrupted` out of `chat()`'s stream — and leaves the
    # provider ready to serve the very next request normally. Returns
    # whether there was actually something in flight to interrupt. A
    # provider that implements neither simply can't be steered mid-stream;
    # the orchestrator still applies a queued steering message at the next
    # turn boundary either way.


# --------------------------------------------------------------------------- #
# I/O adapter (future: speech & vision)
# --------------------------------------------------------------------------- #
class I_OAdapter(abc.ABC):
    """Renders output and optionally captures input for the user.

    Out-of-scope for v0.1.0 (core ships a plain-text terminal renderer).
    Implementations may be added in v0.2.0.
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
        - ``"session"``: allow this call for every agent in the CoBirb
          session, now and for any that start later. Wider than ``"always"``,
          which only widens the single policy it was asked about.
        - ``"deny"``: refuse the call.

        Adapters with no way to ask (e.g. a future non-interactive one)
        should return ``"deny"`` — permission is fail-closed, not fail-open.

        Two **optional** hooks extend this without changing the signature, and
        are probed with ``getattr`` rather than declared here, which is what
        keeps the SPI freeze honest. Implement either, both, or neither:

        - ``confirm_scoped(ApprovalRequest) -> str`` — the same question, with
          what ``"always"`` would grant and a preview of the change.
        - ``confirm_request(ApprovalRequest) -> ApprovalOutcome`` — that, plus
          the ability to refuse *with an instruction* for the model.
        """


# --------------------------------------------------------------------------- #
# Crypto backend (future: swapable encryption)
# --------------------------------------------------------------------------- #
class SessionCrypto(abc.ABC):
    """Encrypts/decrypts session files. Default backend: AES-256-GCM + scrypt.

    The core ships no hard crypto dependency, and this deliberately has no
    post-quantum KEM: a password-protected local file has no key exchange for
    one to protect.
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
