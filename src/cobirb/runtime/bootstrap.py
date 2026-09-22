"""Making ``~/.cobirb`` exist, with something in it.

A new user runs `cobirb`, goes looking for the config the docs talk about, and
finds nothing — not an empty file, not a directory, nothing. They then have to
know both where it goes and what belongs in it, which is exactly the knowledge
a starter file exists to save them.

**On first run, not on install.** Wheels have no reliable post-install hook —
``pip install`` unpacks files and runs nothing, so an installer step would work
from source and silently not from a wheel, which is worse than not having one.
Seeding at startup gets the same result the first time the tool is used, works
however it was installed, and costs one `os.path.exists` on every later run.

**It is a starter, not a copy of ``config.json.example``.** The example
documents every key, including `hooks` and `mcp_servers` with illustrative
entries — a hook pointing at a script that does not exist, a server pointing at
an interpreter and a file that do not exist. Copied verbatim into a live config
those are not documentation, they are a broken hook on every tool call and a
failed server start on every run. What gets written is the small safe subset,
with the rest pointed at rather than pasted.
"""
from __future__ import annotations

import logging
import os

from .. import paths

logger = logging.getLogger("cobirb")

# Deliberately short. Everything here is either something a person will
# certainly want to change (the model) or a default worth seeing stated so it
# can be found and flipped. Nothing here does anything on its own.
STARTER_CONFIG = """\
{
  "// CoBirb configuration": "This is the only config file CoBirb reads. A cobirb.json in a project directory is ignored — see 'cobirb help config'.",

  "// model": "The model to use. Include the tag: a bare name means ':latest' to Ollama, so 'mistral' will not find 'mistral:7b'. Check with 'ollama list'.",
  "default_model": "",

  "// models": "One model per role, for the Flock ('cobirb help flock'). Roles inherit from 'default', so a role naming only a model still gets the default's endpoint.",
  "models": {
    "default": {
      "name": "",
      "base_url": "http://localhost:11434"
    }
  },

  "// system_prompt": "'off' sends no system message at all, so your model's own SYSTEM directive applies exactly as it does in Ollama.",
  "system_prompt": "off",

  "// allow_tools": "Tools permitted without asking, e.g. [\\"read_file\\", \\"shell(git status)\\"]. Empty means everything asks first.",
  "allow_tools": [],

  "// more": "hooks, mcp_servers, verify_command, repo_map and the rest are documented in config.json.example and 'cobirb help config'."
}
"""


def ensure_home() -> str | None:
    """Create ``~/.cobirb`` and seed a config, if there isn't one already.

    Returns the path written, or ``None`` when there was nothing to do — which
    is every run after the first.

    Never raises and never overwrites. A home directory that cannot be created
    (a read-only volume, an odd container) costs the starter file, not the
    session; and a config that already exists is the user's, whatever state it
    is in. Silently replacing somebody's settings because a file looked wrong
    would be the worst possible read of "make sure the config is installed".
    """
    path = paths.config_path()
    if os.path.exists(path):
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # x-mode: two CoBirbs starting at once must not have one truncate the
        # other's file between the check above and the write.
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(STARTER_CONFIG)
    except OSError as exc:
        logger.info("could not seed %s: %s", path, exc)
        return None
    logger.info("seeded a starter config at %s", path)
    return path
