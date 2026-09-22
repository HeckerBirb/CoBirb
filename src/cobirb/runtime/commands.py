"""Slash-command handlers shared by every front-end.

These return their message rather than printing it, so both renderers can
use them: the TUI writes the text into its transcript, while anything
text-based can just print it.
"""
from __future__ import annotations

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
