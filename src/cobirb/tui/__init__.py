"""CoBirb's interactive mode: a full-screen Textual application.

Imported lazily by ``cli._run_tui`` — ``textual`` is only needed for this one
mode, so one-shot/programmatic mode (``cobirb -p "..."``) never pays for it
and never breaks if it is missing.
"""
from __future__ import annotations

from .app import CoBirbApp

__all__ = ["CoBirbApp"]
