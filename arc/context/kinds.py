"""Context kinds: every context entry's payload is validated by one of these models.

The kind registry is an immutable mapping built at import time. Cards that add a
kind later (e.g. E6.4 ``position_review``) add a line to :data:`KINDS`; unknown
kinds are rejected on write so nothing untyped reaches the store.

All payload models use ``extra="forbid"`` at the top level (D16).
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 — used at runtime in pydantic models
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from arc.features.snapshot import FeatureSnapshot
from arc.models import Candidate, Proposal
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


class ChannelBriefPayload(BaseModel):
    """Minimal channel brief (E4.4 replaces this with its full ``ChannelBrief``).

    TTL and supersede policy come from the channel profile (D14), passed by the
    producer on write.
    """

    model_config = _FORBID

    channel: str
    video_id: str
    url: str
    published_at: _dt.datetime
    tickers: list[str] = Field(default_factory=list)
    summary: str = ""


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
    """Full trade proposal (pre-gate)."""

    model_config = _FORBID


class JournalPayload(AuditorOutput):
    """Auditor daily journal."""

    model_config = _FORBID


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
)


def kind_spec(kind: str) -> KindSpec:
    """Return the registered spec for *kind* or raise ``ValueError``."""
    try:
        return KINDS[kind]
    except KeyError:
        known = ", ".join(sorted(KINDS))
        msg = f"unknown context kind {kind!r}; registered kinds: {known}"
        raise ValueError(msg) from None


def validate_payload(kind: str, payload: BaseModel | Mapping[str, object]) -> BaseModel:
    """Validate *payload* against *kind*'s model and return the model instance."""
    spec = kind_spec(kind)
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    return spec.model.model_validate(data)
