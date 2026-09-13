"""Which model plays which part.

CoBirb used to have exactly one model: whatever ``--model`` or ``models.default
.name`` resolved to, used for everything. That is the right shape for a single
agent and the wrong shape for the flock (§12), where an orchestrator holds the
plan and workers do the legwork — two jobs with genuinely different demands.
Holding a plan across a long conversation wants the largest model that fits;
writing one function against a stated interface does not, and running several
of those at once is the whole point of fanning out.

So a *role* is what gets resolved here, not a model name:

    {
      "models": {
        "default":      {"name": "qwen2.5-coder:32b",
                         "base_url": "http://localhost:11434"},
        "orchestrator": {"name": "qwen2.5-coder:32b"},
        "worker":       {"name": "qwen2.5-coder:7b"}
      }
    }

**Every role inherits from ``default``**, field by field — a role that names
only a model still gets the default's ``base_url``, and a config that names no
roles at all behaves exactly as it did before this existed. That inheritance is
the reason this is worth a module: the alternative is every role needing a full
copy of the endpoint settings, and endpoints that drift apart by omission.

**Roles resolve even when nothing consumes them yet.** ``worker`` has no caller
until the flock ships; it is defined, resolvable and listed by ``cobirb models``
anyway, so a config written today can be checked today rather than discovered
to be wrong on the first fan-out. ``describe_roles`` exists precisely so that
"which model would actually be used for what" is a question with an answer you
can read, instead of one you infer from three layers of fallback.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..plugins.core import LocalModelProvider

# The role a plain, single-agent run uses, and the one every other role falls
# back to.
ROLE_DEFAULT = "default"
# The agent you talk to: holds the objective, decides what happens next.
ROLE_ORCHESTRATOR = "orchestrator"
# A subagent given one bounded piece of work. Reserved for the flock (v0.5.0);
# resolvable now so the config can be written and checked before then.
ROLE_WORKER = "worker"

ROLES = (ROLE_DEFAULT, ROLE_ORCHESTRATOR, ROLE_WORKER)


@dataclass(frozen=True)
class ModelSpec:
    """How one role resolved, and where each part of it came from.

    ``source`` is prose rather than a key path because the point of it is the
    line ``cobirb models`` prints: someone reading that wants to know why the
    worker is running the big model, and "inherited from models.default"
    answers that where a bare model name does not.
    """

    role: str
    name: str
    base_url: str | None
    source: str

    @property
    def configured(self) -> bool:
        """Whether this role resolves to an actual model at all."""
        return bool(self.name)

    def describe(self) -> str:
        """One line for ``cobirb models``."""
        name = self.name or "(none configured)"
        endpoint = f" @ {self.base_url}" if self.base_url else ""
        return f"{self.role:<13} {name}{endpoint}  [{self.source}]"


def _default_name(config: Config) -> tuple[str, str]:
    """The model for the ``default`` role, and which key supplied it.

    Three keys name the same thing and all three still work: ``"model"``,
    ``models.default.name``, and ``"default_model"``. That is backward
    compatibility rather than three behaviours — the order below is the one
    ``build_model`` has always used, and changing it would silently move which
    model somebody's existing config resolves to.
    """
    for keys, label in (
        (("model",), "model"),
        (("models", "default", "name"), "models.default.name"),
        (("default_model",), "default_model"),
    ):
        value = config.get(*keys)
        if value:
            return str(value), label
    return "", "unset"


def resolve_role(role: str, config: Config, override: str | None = None) -> ModelSpec:
    """Work out which model, at which endpoint, plays ``role``.

    Resolution, most specific first: an explicit ``override`` (that is
    ``--model``, which names the model for the run and therefore beats config),
    then ``models.<role>.name``, then whatever the ``default`` role resolves
    to. The endpoint resolves the same way independently, so a role may name a
    model without repeating the URL it is served from.

    An unknown role is not an error. It resolves to the default, which is the
    behaviour a caller wants from something whose whole job is "give me a model
    for this job" — a typo costs the specialisation, never the run.
    """
    if override:
        base_url = config.get("models", role, "base_url") or config.get(
            "models", ROLE_DEFAULT, "base_url"
        )
        return ModelSpec(role, str(override), base_url, "--model")

    name = ""
    source = ""
    if role != ROLE_DEFAULT:
        configured = config.get("models", role, "name")
        if configured:
            name, source = str(configured), f"models.{role}.name"
    if not name:
        name, default_source = _default_name(config)
        # "inherited from unset" is a sentence about nothing. A role with no
        # model anywhere to inherit is simply unset, like the default is.
        source = (
            default_source
            if role == ROLE_DEFAULT or not name
            else f"inherited from {default_source}"
        )

    base_url = config.get("models", role, "base_url") or config.get(
        "models", ROLE_DEFAULT, "base_url"
    )
    return ModelSpec(role, name, base_url, source)


def build_for_role(
    role: str, config: Config | None = None, override: str | None = None
) -> LocalModelProvider:
    """A provider for ``role``.

    Deliberately returns a provider even when nothing is configured: the
    "no model configured" error belongs to the first turn that actually needs
    one, where it can say what to do about it, not to wiring.
    """
    config = config or Config()
    spec = resolve_role(role, config, override)
    return LocalModelProvider(model=spec.name, base_url=spec.base_url)


def describe_roles(config: Config, override: str | None = None) -> list[ModelSpec]:
    """How every known role resolves, for ``cobirb models``.

    Roles the user has named that CoBirb doesn't know about are included too.
    A ``models`` block with a typo'd role currently resolves to the default and
    says nothing about it, which is the sort of quiet no-op that costs an hour;
    listing it makes the typo visible next to the roles that work.
    """
    named = config.get("models", default={})
    extra = sorted(k for k in named if isinstance(named, dict) and k not in ROLES)
    return [resolve_role(role, config, override) for role in (*ROLES, *extra)]
