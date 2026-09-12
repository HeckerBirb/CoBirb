"""Persona resolution and system-prompt composition.

Personas are opt-in data describing how CoBirb speaks. This module owns
finding one by name, listing what is available, working out the name that
reloads a given persona, and turning any of it into CoBirb's own
contribution to the system prompt.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from ..plugins.core import (
    PLAIN_PERSONA_NAME,
    build_default_persona,
    build_plain_persona,
    persona_shapes_voice,
)
from ..typing import spi as cobirb_typing


def _article(word: str) -> str:
    """"a" or "an" for ``word`` — personas are user-authored data, so the
    species can start with anything."""
    return "an" if word[:1].lower() in "aeiou" else "a"


_HARNESS_PROMPT = (
    "This is CoBirb, a local agent harness running on the user's own machine. "
    "Tool calls are gated by a permission prompt the user answers, so a denied "
    "call is the user's decision, not an error to retry. Nothing leaves this "
    "machine: no telemetry, no outbound network by default, and session files "
    "are encrypted at rest."
)


def build_system_prompt(persona: cobirb_typing.Persona, *, harness: bool = False) -> str:
    """Compose CoBirb's *own* contribution to the system prompt, if any.

    Returns ``""`` by default — no persona, no harness block — and an empty
    string here means the provider sends no system message whatsoever, so the
    model's Modelfile ``SYSTEM`` applies exactly as it does when talking to
    Ollama directly. That is the point: an explicit system message *replaces*
    the model's own for that request, so a client that always sends one
    silently overrides a configuration its user built on purpose.

    Whatever this does return is a supplement, not a replacement: the
    provider reads the model's own prompt back and places it first (see
    ``LocalModelProvider.compose_system``).

    With a persona active (``--persona``, ``/persona``, or the ``"persona"``
    config key), every field the persona file defines is rendered. Supplying
    only the name and species (as this once did) left tone, phrasings, emoji
    density and squawks as inert data the model never saw — so a persona
    declaring ``"emoji_density": "none"`` had no way to be honoured.
    ``greeting`` is the one exception: the CLI speaks it directly when a
    persona is adopted, so the model needn't reproduce it.
    """
    p = persona
    blocks = [_HARNESS_PROMPT] if harness else []
    if not persona_shapes_voice(p):
        return "\n".join(blocks)

    voice = []
    if p.species:
        voice.append(f"You are {p.name}, {_article(p.species)} {p.species}.")
    else:
        voice.append(f"You are {p.name}.")
    if p.tone:
        voice.append(f"Your tone is {p.tone}.")
    if p.phrasings:
        voice.append("Phrases that come naturally to you: " + "; ".join(p.phrasings))
    if p.known_squawks:
        voice.append("Interjections you use sparingly: " + "; ".join(p.known_squawks))
    if p.emoji_density:
        voice.append(f"Emoji use: {p.emoji_density}.")
    voice.append("This describes how you speak, and nothing else.")
    return "\n".join([*blocks, *voice])


# Relative to the *package*, not to this module: the bundled personas live at
# cobirb/personas/ and this file moved down into cobirb/runtime/.
_PERSONAS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "personas")


NO_PERSONA = "none"


def load_persona(persona_name: str | None) -> cobirb_typing.Persona:
    """Return the persona for ``persona_name``, or no persona at all.

    ``None`` (nothing configured) and the explicit name ``"none"`` both give
    the plain, voice-less default — personas are opt-in, so an unconfigured
    run sends the model no instructions about how to sound.

    Resolution order for a real name: the bundled personas shipped with
    CoBirb (see ``cobirb/personas/``), then a project-local ``<name>.json``,
    then a ``<name>.json`` under the user's CoBirb home.
    """
    key = (persona_name or "").strip()
    lowered = key.lower()
    # PLAIN_PERSONA_NAME is accepted alongside "none" so a session saved with
    # no persona reloads cleanly: sessions record a persona name, and the
    # plain default's name is "CoBirb".
    if not lowered or lowered in (NO_PERSONA, PLAIN_PERSONA_NAME.lower()):
        return build_plain_persona()
    # Case-insensitive so "Noah" (as stored in a session) resolves to the
    # same persona "noah" (as typed on the command line) does.
    if lowered == "noah":
        return build_default_persona()
    persona_name = key
    candidates = [
        os.path.join(_PERSONAS_DIR, f"{persona_name}.json"),
        os.path.join(os.getcwd(), f"{persona_name}.json"),
        os.path.join(os.environ.get("COBIRB_HOME", os.path.expanduser("~")), "cobirb", f"{persona_name}.json"),
    ]
    data = None
    for path in candidates:
        if os.path.isfile(path):
            data = load_json(path)
            break
    if data is None:
        # Falls back to *no* persona rather than to Noah: a typo in
        # --persona shouldn't quietly dress the model up in a character the
        # user never asked for.
        print(f"unknown persona '{persona_name}'. Continuing without one.", file=sys.stderr)
        return build_plain_persona()
    from ..typing.spi import Persona

    return Persona.from_dict(data)


def available_personas() -> list[str]:
    """Every persona CoBirb can resolve out of the box, for the ``/persona``
    picker and listing.

    ``"none"`` leads because it is the default and the way back to it: the
    picker has to be able to *remove* a persona, not only swap one for
    another. After it come the built-in Noah and every ``*.json`` bundled
    under ``cobirb/personas/``.
    """
    names = {"noah"}
    if os.path.isdir(_PERSONAS_DIR):
        for fname in os.listdir(_PERSONAS_DIR):
            if fname.endswith(".json"):
                names.add(fname[: -len(".json")])
    return [NO_PERSONA, *sorted(names)]


def persona_key(persona: cobirb_typing.Persona) -> str:
    """The name that reloads ``persona`` — what ``--persona`` takes, which is
    not always what the persona calls itself.

    ``kawaii.json`` introduces itself as "Imouto", so a session that recorded
    the display name could not be reopened: ``load_persona("Imouto")`` finds
    no such file. Sessions therefore store this key instead.
    """
    if not persona_shapes_voice(persona):
        return NO_PERSONA
    for name in available_personas():
        if name != NO_PERSONA and load_persona(name).name == persona.name:
            return name
    # A persona the user wrote themselves: the file name is the best guess
    # available, and matches the common case of naming the file after it.
    return persona.name


def load_json(path: str) -> dict[str, Any] | None:
    """Read a persona file, or ``None`` if it can't be read.

    A malformed persona file used to traceback out of the whole run. It is
    reported and treated as a persona that couldn't be found — which lands
    on the same "continuing without one" path a typo already takes, and for
    the same reason: a broken costume is not worth losing the conversation
    over.
    """
    import json

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cobirb: could not read persona {path} — {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"cobirb: could not read persona {path} — expected a JSON object", file=sys.stderr)
        return None
    return data
