"""Context kinds: every context entry's payload is validated by one of these models.

The kind registry is an immutable mapping built at import time. Cards that add a
kind later (e.g. E6.4 ``position_review``) add a line to :data:`KINDS`; unknown
kinds are rejected on write so nothing untyped reaches the store.

All payload models use ``extra="forbid"`` at the top level (D16).
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.exits.model import ExitModelResult  # noqa: TC001 - pydantic field
from arc.features.snapshot import FeatureSnapshot
from arc.models import Candidate, ChannelBrief, Proposal, Stance
from arc.personas.schemas import AuditorOutput, DirectorOutput, QuantOutput, RiskOutput

if TYPE_CHECKING:
    from collections.abc import Mapping

_FORBID = ConfigDict(extra="forbid")


class RawDocRefPayload(BaseModel):
    """Pointer to a stored ``raw_docs`` row (the text itself stays in raw_docs)."""

    model_config = _FORBID

    doc_id: str
    source: str
    url: str
    published_at: str = Field(..., description="ISO-8601, as stored in raw_docs")


class ChannelBriefPayload(ChannelBrief):
    """Channel brief (E4.4 ``ChannelBrief``, D14); keeps its strict/frozen config.

    TTL and supersede policy come from the channel profile (D14), passed by the
    producer on write.
    """


class CandidatePayload(Candidate):
    """Scout candidate (E4.2)."""

    model_config = _FORBID


class RegimePayload(FeatureSnapshot):
    """Regime + vol features for one underlying (E4.3)."""

    model_config = _FORBID


class ShortlistPayload(DirectorOutput):
    """Director ranked shortlist."""

    model_config = _FORBID


class StructuresPayload(QuantOutput):
    """Quant structures with analytics."""

    model_config = _FORBID


class RiskReviewPayload(RiskOutput):
    """Risk persona advisory review."""

    model_config = _FORBID


class ProposalPayload(Proposal):
    """Full trade proposal (pre-gate), plus the E2.4 exit model for the card.

    ``exit_model`` is context only: it is not part of :class:`Proposal`, so it never
    enters the gate's proposal hash.
    """

    model_config = _FORBID

    exit_model: ExitModelResult | None = None


class JournalPayload(AuditorOutput):
    """Auditor daily journal."""

    model_config = _FORBID


class NoteTopic(enum.StrEnum):
    """What a :class:`NotePayload` is about (D27)."""

    THESIS = "thesis"  # why a trade/ticker/idea (Director, Quant)
    REGIME_VIEW = "regime_view"  # market/sector regime read (Director, Scout)
    OBSERVATION = "observation"  # informational: news theme, scan summary (Scout)
    RISK_FLAG = "risk_flag"  # portfolio/calendar concern (Risk)
    LESSON = "lesson"  # post-trade learning (Auditor)
    EXECUTION = "execution"  # fill/market-conditions note (Investor)


class NoteHorizon(enum.StrEnum):
    INTRADAY = "intraday"
    SESSION = "session"
    SWING = "swing"  # days-weeks
    MACRO = "macro"  # weeks+


class Evidence(BaseModel):
    """One piece of support for a note."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(..., min_length=1, description="URL, context entry id, or 'metric:<name>'")
    quote: str | None = Field(None, max_length=400, description="Verbatim excerpt, if textual")


class NotePayload(BaseModel):
    """Free-form but structured persona text (D27): thesis, regime view, observation, ...

    Notes are context that other personas may read back. They are never a gate
    input and never widen :class:`~arc.models.Candidate`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona: Literal["scout", "director", "quant", "risk", "investor", "auditor"]
    topic: NoteTopic
    horizon: NoteHorizon = NoteHorizon.SESSION
    stance: Stance | None = None
    title: str = Field(..., min_length=1, max_length=120)
    body: str = Field(..., min_length=1, max_length=4000)
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list, max_length=12)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    about: list[str] = Field(
        default_factory=list, description="Context entry ids this note comments on"
    )


@dataclass(frozen=True)
class KindSpec:
    """A context kind: its payload model and current schema version."""

    name: str
    model: type[BaseModel]
    schema_version: int = 1


def _registry(*specs: KindSpec) -> Mapping[str, KindSpec]:
    return MappingProxyType({s.name: s for s in specs})


KINDS: Mapping[str, KindSpec] = _registry(
    KindSpec("raw_doc_ref", RawDocRefPayload),
    KindSpec("channel_brief", ChannelBriefPayload),
    KindSpec("candidate", CandidatePayload),
    KindSpec("regime", RegimePayload),
    KindSpec("shortlist", ShortlistPayload),
    KindSpec("structures", StructuresPayload),
    KindSpec("risk_review", RiskReviewPayload),
    KindSpec("proposal", ProposalPayload),
    KindSpec("journal", JournalPayload),
    KindSpec("note", NotePayload),
)


SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "context"


def render_schemas() -> dict[str, str]:
    """``<kind>.v<N>.json`` -> JSON Schema text for every registered kind (D27 registry).

    Committed under ``schemas/context/``; ``tests/test_context_schemas.py`` fails when a
    model changes without regenerating (``arc context schemas --write``).
    """
    return {
        f"{name}.v{spec.schema_version}.json": json.dumps(
            spec.model.model_json_schema(), indent=2, sort_keys=True
        )
        + "\n"
        for name, spec in KINDS.items()
    }


def kind_spec(kind: str) -> KindSpec:
    """Return the registered spec for *kind* or raise ``ValueError``."""
    try:
        return KINDS[kind]
    except KeyError:
        known = ", ".join(sorted(KINDS))
        msg = f"unknown context kind {kind!r}; registered kinds: {known}"
        raise ValueError(msg) from None


def validate_payload(kind: str, payload: BaseModel | Mapping[str, object]) -> BaseModel:
    """Validate *payload* against *kind*'s model and return the model instance.

    Payloads are normalised to JSON first (that is how they are stored), and
    validated in JSON mode so strict models (e.g. ``ChannelBrief``) accept
    ISO-8601 dates on read-back.
    """
    spec = kind_spec(kind)
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    return spec.model.model_validate_json(json.dumps(data, default=str))
