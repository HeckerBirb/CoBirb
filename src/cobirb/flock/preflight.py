"""Checking the endpoint can actually do the run, before anyone starts.

A flock is the first thing in CoBirb that uses *two* model roles at once, and
that turns a quiet misconfiguration into three simultaneous failures. A worker
model that is named in config but not pulled produces one identical error per
Worker Birb, several minutes after approval, with the planning work already
done — and the error is about a worker, so it reads as a Flock problem rather
than as a missing model.

This is one HTTP call that turns that into a sentence before anything runs.
It is a *check*, not a gate: an endpoint that cannot answer it gets the benefit
of the doubt, because being unable to list models is not evidence that a model
is absent.
"""
from __future__ import annotations

import logging

from ..runtime.models import ROLE_ORCHESTRATOR, ROLE_WORKER, build_for_role, resolve_role
from ..config import Config

logger = logging.getLogger("cobirb")


def missing_models(config: Config | None = None) -> str:
    """A warning about roles whose model the endpoint does not have, or "".

    Checks the roles a flock actually uses. ``worker`` is the one that matters
    — it is newly load-bearing, and the one most likely to be configured
    hopefully rather than from what is installed.
    """
    config = config or Config()
    try:
        available = set(build_for_role(ROLE_ORCHESTRATOR, config).list_models())
    except Exception as exc:  # noqa: BLE001 - cannot check is not the same as missing
        logger.info("could not list models for the pre-flight check: %s", exc)
        return ""
    if not available:
        return ""

    problems = []
    for role in (ROLE_ORCHESTRATOR, ROLE_WORKER):
        spec = resolve_role(role, config)
        if not spec.configured:
            continue
        # Ollama reports "llama3.1:latest" for a model pulled as "llama3.1",
        # so an exact match alone would report a false absence on the
        # commonest possible configuration.
        if not any(
            name == spec.name or name.split(":")[0] == spec.name.split(":")[0]
            for name in available
        ):
            problems.append(f"  {role}: {spec.name!r} is not on the endpoint")

    if not problems:
        return ""
    return (
        "The model endpoint does not have every model this flock needs:\n"
        + "\n".join(problems)
        + "\n\nEvery Worker Birb using it would fail the same way, after the planning "
        "work is already done. Check 'cobirb models' and 'ollama list'."
    )
