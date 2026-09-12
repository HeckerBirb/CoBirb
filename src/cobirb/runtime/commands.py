"""Slash-command handlers shared by every front-end.

These return their message rather than printing it, so both renderers can
use them: the TUI writes the text into its transcript, while anything
text-based can just print it.
"""
from __future__ import annotations

from ..plugins.core import persona_shapes_voice
from ..typing import spi as cobirb_typing
from .personas import available_personas, build_system_prompt, load_persona


def apply_persona_switch(
    arg: str, persona: cobirb_typing.Persona, system: str, *, harness: bool = False
) -> tuple[cobirb_typing.Persona, str, str]:
    """Handle ``/persona [name]``.

    With no argument it lists the available personas and leaves the current
    one alone. With a name it loads that persona and rebuilds the system
    prompt around it, so every turn afterward actually speaks as the new
    persona instead of just announcing that it will.

    Returns ``(persona, system, message)``.
    """
    if not arg:
        return persona, system, f"Available personas: {', '.join(available_personas())}"
    new_persona = load_persona(arg)
    new_system = build_system_prompt(new_persona, harness=harness)
    if not persona_shapes_voice(new_persona):
        return new_persona, new_system, "Persona off — the model speaks in its own voice."
    greeting = new_persona.greeting or "switched personas."
    return new_persona, new_system, f"{new_persona.name}: {greeting}"


def apply_plan_toggle(arg: str, plan_mode: bool) -> tuple[bool, str]:
    """Handle ``/plan [on|off]``: toggle plan mode, report its state, or
    reject an argument that is neither. Returns ``(plan_mode, message)``."""
    arg = arg.strip().lower()
    if arg in ("on", "off"):
        plan_mode = arg == "on"
        return plan_mode, f"Plan mode: {'on' if plan_mode else 'off'}."
    if not arg:
        return plan_mode, f"Plan mode is {'on' if plan_mode else 'off'}. Usage: /plan on|off"
    return plan_mode, "Usage: /plan on|off"
