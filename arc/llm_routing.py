"""Per-persona LLM routing (PLAN §2.4, D8).

``config/llm_routing.yaml`` maps each persona to a model tier and each tier
to a ``<provider>/<model>`` id. It is the single place a persona's or a
tier's model is changed; call sites ask :func:`resolve` (or
:meth:`LLMRouting.route`) and never name a model themselves.

No fallback provider is configured (D8); see docs/OPS.md §1.2.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from arc.config import ArcSettings

__all__ = [
    "DEFAULT_ROUTING_PATH",
    "LLMRouting",
    "ModelRoute",
    "Persona",
    "TierSpec",
    "load_routing",
    "resolve",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROUTING_PATH = REPO_ROOT / "config" / "llm_routing.yaml"


class Persona(StrEnum):
    """The LLM personas of PLAN §2.4 (D56: Broker and Ops are deterministic, not here)."""

    SCALP = "scalp"
    RESEARCH = "research"
    QUANT = "quant"
    RISK = "risk"


class TierSpec(BaseModel):
    """One model tier: a ``<provider>/<model>`` id.

    ``local: true`` marks an on-device model (E8.4, Mac Studio). D39: only jobs whose
    persona resolves to a local tier take the dispatcher's global LLM lock, so local
    runs happen one at a time while remote API routes run concurrently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(description="Qualified model id, e.g. 'anthropic/claude-opus-5.5'.")
    local: bool = Field(default=False, description="On-device model: serialise via the LLM lock.")

    @field_validator("model")
    @classmethod
    def _qualified(cls, v: str) -> str:
        provider, sep, name = v.strip().partition("/")
        if not sep or not provider or not name:
            msg = f"model {v!r} must be '<provider>/<model>'"
            raise ValueError(msg)
        return f"{provider}/{name}"


class ModelRoute(BaseModel):
    """What a persona resolves to: tier, provider and model for ``hermes -z``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona: Persona
    tier: str
    model: str  # qualified '<provider>/<model>'
    local: bool = False  # D39: an on-device model; its jobs take the global LLM lock

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]


class LLMRouting(BaseModel):
    """Validated ``config/llm_routing.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: dict[str, TierSpec]
    personas: dict[Persona, str]

    @model_validator(mode="after")
    def _complete(self) -> LLMRouting:
        missing = sorted(p.value for p in Persona if p not in self.personas)
        if missing:
            msg = f"personas without a tier: {', '.join(missing)}"
            raise ValueError(msg)
        unknown = sorted({t for t in self.personas.values() if t not in self.tiers})
        if unknown:
            msg = f"unknown tier(s): {', '.join(unknown)}; defined: {', '.join(sorted(self.tiers))}"
            raise ValueError(msg)
        return self

    def route(self, persona: Persona | str) -> ModelRoute:
        """The model route for *persona* (raises ``ValueError`` if unknown)."""
        p = Persona(persona)
        tier = self.personas[p]
        spec = self.tiers[tier]
        return ModelRoute(persona=p, tier=tier, model=spec.model, local=spec.local)

    def is_local(self, persona: Persona | str) -> bool:
        """D39: True when *persona* runs on a local model (its jobs take the LLM lock)."""
        return self.route(persona).local


def load_routing(path: Path | str | None = None) -> LLMRouting:
    """Load and validate a routing YAML file (default: ``config/llm_routing.yaml``)."""
    p = Path(path) if path is not None else DEFAULT_ROUTING_PATH
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{p}: top level must be a mapping"
        raise ValueError(msg)
    return LLMRouting.model_validate(data)


@lru_cache(maxsize=8)
def _cached(path: str) -> LLMRouting:
    return load_routing(path)


def resolve(persona: Persona | str, settings: ArcSettings | None = None) -> ModelRoute:
    """Resolve *persona* using the routing file named by *settings* (or the default)."""
    path = settings.llm_routing_file if settings is not None else None
    return _cached(str(path or DEFAULT_ROUTING_PATH)).route(persona)
