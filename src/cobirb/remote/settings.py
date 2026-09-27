"""The ``remote_workers`` config block: which remotes exist and how they think.

Each entry names a remote by its OS and its address, and says whose model its
Worker Birb uses::

    "remote_workers": [
      {"remote_os": "Windows", "remote_url": "https://10.10.10.10:8443/api",
       "run_llms_locally": true, "openai_endpoint": "http://gpu-box:11434",
       "model_name": "qwen3-coder"}
    ]

``run_llms_locally`` defaults to false: the remote's model calls come back over
the connection to the main session's one endpoint. When true, the remote uses
``openai_endpoint`` — an address as *the remote* sees it; the main machine never
contacts it — with ``model_name``, defaulting to the worker model the main
config names.

A remote with the same OS as this machine is ignored: telling apart work that
should run in isolation from work that should run here is not decided yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import Config
from .osnames import ACCEPTED, canonical_os, local_os

KEY = "remote_workers"


@dataclass(frozen=True)
class RemoteSpec:
    """One configured remote, validated."""

    os: str  # the family, as platform.system() names it
    url: str
    run_llms_locally: bool = False
    openai_endpoint: str = ""
    model_name: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""

    @property
    def port(self) -> int:
        return urlparse(self.url).port or 443

    @property
    def websocket_url(self) -> str:
        """``https://h:p/api`` → ``wss://h:p/api``: the same address, spoken as
        a WebSocket."""
        parsed = urlparse(self.url)
        return parsed._replace(scheme="wss").geturl()

    def label(self) -> str:
        return f"{self.os} ({self.host}:{self.port})"


def parse_entry(entry: object) -> "tuple[RemoteSpec | None, str]":
    """One config entry as a ``RemoteSpec``, or ``None`` and why not."""
    if not isinstance(entry, dict):
        return None, "each entry must be an object"
    family = canonical_os(entry.get("remote_os"))
    if family is None:
        accepted = ", ".join(name for names in ACCEPTED.values() for name in names)
        return None, (f"remote_os {entry.get('remote_os')!r} is not an OS name CoBirb knows "
                      f"(accepted, in any case: {accepted}; a BSD release number may follow)")
    url = entry.get("remote_url")
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme != "https" or not parsed.hostname:
        return None, f"remote_url {url!r} must be an https:// address"
    local = entry.get("run_llms_locally", False)
    if not isinstance(local, bool):
        return None, "run_llms_locally must be true or false"
    endpoint = entry.get("openai_endpoint", "")
    if local and not (isinstance(endpoint, str) and endpoint.startswith(("http://", "https://"))):
        return None, "run_llms_locally is true, so openai_endpoint must be the remote's http(s):// model server"
    model = entry.get("model_name", "")
    if not isinstance(model, str):
        return None, "model_name must be a string"
    unknown = set(entry) - {"remote_os", "remote_url", "run_llms_locally", "openai_endpoint", "model_name"}
    if unknown:
        return None, f"unknown key(s): {', '.join(sorted(unknown))}"
    return RemoteSpec(family, url, local, endpoint if isinstance(endpoint, str) else "", model), ""


def configured(config: Config) -> "tuple[list[RemoteSpec], list[str]]":
    """Every usable remote, and a line for each entry that is not.

    Same-OS remotes are left out, with a line saying so: an entry that silently
    does nothing is a setting the user cannot see is ignored.
    """
    block = config.get(KEY)
    if block is None:
        return [], []
    if not isinstance(block, list):
        return [], [f"{KEY} must be a list of remotes"]
    remotes, problems = [], []
    here = local_os()
    for index, entry in enumerate(block):
        spec, problem = parse_entry(entry)
        if spec is None:
            problems.append(f"{KEY}[{index}]: {problem}")
        elif spec.os == here:
            problems.append(f"{KEY}[{index}]: {spec.label()} has this machine's OS ({here}) and is "
                            "ignored — only remotes with another OS are used")
        else:
            remotes.append(spec)
    return remotes, problems
