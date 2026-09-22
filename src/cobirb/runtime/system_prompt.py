"""CoBirb's own contribution to the system prompt.

By default that contribution is nothing at all, and an empty string here means
the provider sends no system message whatsoever — so the model's Modelfile
``SYSTEM`` applies exactly as it does when talking to Ollama directly. An
explicit system message *replaces* the model's own for that request, so a
client that always sends one silently overrides a configuration its user built
on purpose.

Whatever this does return is a supplement, not a replacement: the provider
reads the model's own prompt back and places it first (see
``LocalModelProvider.compose_system``).
"""
from __future__ import annotations

_HARNESS_PROMPT = (
    "This is CoBirb, a local agent harness running on the user's own machine. "
    "Tool calls are gated by a permission prompt the user answers, so a denied "
    "call is the user's decision, not an error to retry. Nothing leaves this "
    "machine: no telemetry, no outbound network by default, and session files "
    "are encrypted at rest."
)


def build_system_prompt(*, harness: bool = False) -> str:
    """The system prompt CoBirb adds: the harness block when asked for
    (``--system-prompt harness`` or ``"system_prompt": "harness"``), else ``""``.
    """
    return _HARNESS_PROMPT if harness else ""
