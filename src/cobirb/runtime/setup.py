"""``cobirb setup``: from nothing to a working model without editing JSON.

The first-run path used to be "write a config file by hand, then find out
whether you spelled the model right." Setup asks the two things only the user
knows — where their model server is, and which protocol it speaks — lists what
that server actually has, and writes the choice.

It **asks, and never probes**. CoBirb connects only to what its config names,
so setup suggests Ollama's default address and contacts exactly the address the
user confirms; it does not scan ports or look for servers on its own.

Writing the config is the one thing here that could do damage, so it is
careful: the existing file is read and every other key kept, a file that does
not parse is refused rather than replaced, and the write is atomic and 0600.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse
from typing import Any, Callable

from .. import paths
from .bootstrap import ensure_home
from .models import API_OLLAMA, API_OPENAI

DEFAULT_ADDRESS = "http://localhost:11434"


class ConfigUnreadable(RuntimeError):
    """The config exists but is not valid JSON; setup will not overwrite it."""


def save_default_model(name: str, base_url: str | None = None, api: str | None = None) -> str:
    """Make ``name`` the default model in the user's config; return its path.

    Only ``models.default.name`` (and ``base_url``/``api`` when given) change.
    Older spellings of the same setting (``model``, ``default_model``) are
    removed, since they would otherwise outrank what was just chosen.
    """
    path = paths.config_path()
    ensure_home()
    data: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigUnreadable(f"{path} could not be read ({exc}); fix or move it first") from exc
        if not isinstance(data, dict):
            raise ConfigUnreadable(f"{path} is not a JSON object; fix or move it first")
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        raise ConfigUnreadable(f"'models' in {path} is not an object; fix it first")
    default = models.setdefault("default", {})
    default["name"] = name
    if base_url:
        default["base_url"] = base_url
    if api:
        default["api"] = api
    for older in ("model", "default_model"):
        data.pop(older, None)

    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return path


def _guess_api(address: str) -> str:
    """Ollama on its own port, the OpenAI protocol anywhere else — a default
    for the question, never a decision made for the user."""
    return API_OLLAMA if urllib.parse.urlsplit(address).port in (None, 11434) else API_OPENAI


def run(ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
        interactive: bool | None = None) -> int:
    """The interactive setup. Returns a process exit code."""
    from ..plugins.core.model import LocalModelProvider
    from ..plugins.core.openai import OpenAICompatibleProvider
    from . import doctor

    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        say("cobirb setup asks questions, so it needs a terminal. Set models.default in "
            f"{paths.config_path()} instead (see 'cobirb help config').")
        return 1

    say("CoBirb talks to one model server that you run. Nothing else is contacted.")
    address = (ask(f"Where is it? [{DEFAULT_ADDRESS}] ").strip() or DEFAULT_ADDRESS).rstrip("/")
    if not address.startswith(("http://", "https://")):
        address = f"http://{address}"
    suggested = _guess_api(address)
    answer = ask(f"Does it speak Ollama's API or the OpenAI one (llama.cpp, LM Studio, vLLM)? "
                 f"[{suggested}] ").strip().lower()
    if answer.startswith("ol"):
        api = API_OLLAMA
    elif answer.startswith(("op", "llama", "lm", "vllm")):
        api = API_OPENAI
    else:
        api = suggested

    provider_class = OpenAICompatibleProvider if api == API_OPENAI else LocalModelProvider
    try:
        models = provider_class(base_url=address).list_models()
    except Exception as exc:  # noqa: BLE001 - reported, and nothing is written
        say(f"Could not list models at {address}: {exc}")
        say("Nothing was changed. Start the server, or check the address, and run 'cobirb setup' again.")
        return 1
    if not models:
        say(f"{address} answered but has no models. Pull or load one, then run setup again.")
        return 1

    say("")
    for number, name in enumerate(models, 1):
        say(f"  {number:>2}. {name}")
    choice = ask("Which one? (number or name) ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(models):
        model = models[int(choice) - 1]
    elif choice in models:
        model = choice
    else:
        say(f"'{choice}' is not one of those. Nothing was changed.")
        return 1

    try:
        path = save_default_model(model, base_url=address, api=api)
    except ConfigUnreadable as exc:
        say(f"Not saved: {exc}")
        return 1
    say(f"\nSaved: {model} at {address} ({api}) as your default, in {path}.")
    from ..config import Config

    override = Config().get("models", "orchestrator", "name")
    if override and override != model:
        say(f"Note: models.orchestrator.name is set to {override!r}, and that still decides the "
            "model you talk to. Remove it to use the default.")
    say("")
    report = doctor.run()
    say(report.describe())
    return 0 if report.ok else 1
