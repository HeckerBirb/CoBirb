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

# How to work, for a model that otherwise knows only what its Modelfile says —
# usually "a helpful assistant", which is a chat partner, not an agent. Kept
# short: every line is paid for on every request, and each one answers a
# failure seen in practice (editing blind, claiming success unchecked,
# retrying a refusal, stopping to describe the next step instead of taking it).
_HARNESS_PROMPT = (
    "You are working as a coding agent in the user's project on their own machine, "
    "through the tools you have been given.\n"
    "- Look before you change anything: find the relevant code with grep, glob, list_dir "
    "or repo_map, and read it with read_file.\n"
    "- Change files with edit_file (one region, copied exactly from what read_file showed) "
    "or write_file (new or short files).\n"
    "- After changing code, check it: run the tests or the command the task names, if you "
    "can, and fix what fails.\n"
    "- Keep going until the task is done — take the next step rather than describing it. "
    "Then answer briefly with what you changed. If you cannot finish, say what is left and "
    "why; never claim success you have not checked.\n"
    "- Tool calls are gated by a permission prompt the user answers. A denied call is their "
    "decision, not an error to retry: do something else, or ask.\n"
    "Nothing leaves this machine: no telemetry, no outbound network by default, and session "
    "files are encrypted at rest."
)


def build_system_prompt(*, harness: bool = False) -> str:
    """The system prompt CoBirb adds: the harness block when asked for
    (``--system-prompt harness`` or ``"system_prompt": "harness"``), else ``""``.
    """
    return _HARNESS_PROMPT if harness else ""
