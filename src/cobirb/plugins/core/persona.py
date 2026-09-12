"""Core persona plugin: the "no persona" default, and the Noah parrot persona.

Personas are *pure data* — they shape how CoBirb speaks but must never affect
behavior or permissions. The core instructs the agent that persona data does not
grant permission to skip security, encryption, or network controls.

**Personas are off unless asked for.** A persona is a costume put on the model:
a name, a species, a tone, stock phrases. Wearing one by default means every
CoBirb install silently overrides whatever voice the local model already has,
which is not CoBirb's call to make — the model is the user's, running on the
user's machine. ``build_plain_persona()`` is therefore what an unconfigured run
gets: a label to print next to replies, and no voice instructions at all.
``--persona noah`` (or ``/persona``) opts back in.
"""
from __future__ import annotations

from ...typing.spi import Persona

# What replies are labelled with when no persona is active. Not a persona
# name the model is ever told to adopt — just the name of the program.
PLAIN_PERSONA_NAME = "CoBirb"


def build_plain_persona() -> Persona:
    """The default: no voice, no character, nothing sent to shape the model.

    Every voice-bearing field is deliberately empty. ``_build_system_prompt``
    checks exactly that (see ``persona_shapes_voice``) and emits no persona
    block at all, so an unconfigured run leaves the model's own voice alone.
    """
    return Persona(
        name=PLAIN_PERSONA_NAME,
        species="",
        tone="",
        greeting="",
        phrasings=[],
        emoji_density="",
        known_squawks=[],
    )


def persona_shapes_voice(persona: Persona) -> bool:
    """Whether ``persona`` actually asks the model to speak a certain way.

    False for the plain default (and for a user-authored persona file that
    happens to fill in nothing), which is the signal to send no persona
    instructions whatsoever rather than an empty, pointless block.
    """
    return bool(
        persona.species
        or persona.tone
        or persona.phrasings
        or persona.known_squawks
        or persona.emoji_density
    )


def build_default_persona() -> Persona:
    """Return the Noah persona as a Persona object.

    Opt-in since personas stopped being on by default: ``--persona noah``,
    ``/persona``, or ``"persona": "noah"`` in config.
    """
    return Persona(
        name="Noah",
        species="African Grey Parrot",
        tone="friendly, playful, not saccharine",
        greeting="Squawk! Noah here — what shall we build today?",
        phrasings=["Great idea!", "I'll take a look.", "Consider that done."],
        emoji_density="light",
        known_squawks=["Ha-ha!", "Done-done!", "Not today."],
    )


def persona_to_json(persona: Persona) -> str:
    """Serialize a persona to a JSON string."""
    import json

    return json.dumps(persona.to_dict(), indent=2)
