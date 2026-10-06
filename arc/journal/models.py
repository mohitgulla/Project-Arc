"""Data contracts of the decision journal (E7.4, PLAN §2.3). Pydantic v2, ``extra="forbid"``."""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.journal.analytics import ProposalAnalytics  # noqa: TC001 - pydantic field
from arc.journal.reasons import (
    Choice,
    JournalPersona,
    ReasonCode,
    Reviewer,
    ReviewLabel,
    RootCause,
    Stage,
)

__all__ = [
    "DecisionRecord",
    "DecisionReview",
    "LegQuote",
    "MarketContext",
    "OutcomeRecord",
    "OutcomeStatus",
    "PersonaCallMeta",
]

_FORBID = ConfigDict(extra="forbid", frozen=True)


def _aware(v: _dt.datetime) -> _dt.datetime:
    if v.tzinfo is None:
        msg = "timestamps must be timezone-aware"
        raise ValueError(msg)
    return v


class DecisionRecord(BaseModel):
    """One decision: who decided what about which subject, why, and on what inputs."""

    model_config = _FORBID

    id: str
    chain_run_id: str | None = None
    run_id: str | None = None
    persona: JournalPersona
    stage: Stage
    subject: str = Field(..., min_length=1, description="Ticker, or 'session'")
    choice: Choice
    reason_code: ReasonCode
    reason_text: str = ""
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    inputs_snapshot_id: str | None = None
    persona_call_id: str | None = None
    proposal_hash: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    supersedes_id: str | None = None
    at: _dt.datetime

    _at = field_validator("at")(_aware)


class PersonaCallMeta(BaseModel):
    """Usage metadata of one persona LLM call (stored on ``persona_calls``)."""

    model_config = _FORBID

    prompt_text: str
    prompt_inputs: dict[str, Any] = Field(
        default_factory=dict, description="Non-context inputs needed to rebuild the prompt"
    )
    input_tokens: int | None = Field(None, ge=0)
    output_tokens: int | None = Field(None, ge=0)
    latency_ms: int | None = Field(None, ge=0)
    cost_usd: float | None = Field(
        None, ge=0.0, description="None = unknown; 0 on a subscription (tokens still counted)"
    )


class LegQuote(BaseModel):
    model_config = _FORBID

    occ_symbol: str
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    iv: float | None = None
    quote_time: _dt.datetime | None = None


class MarketContext(BaseModel):
    """Market state frozen at proposal time (what the gate and the personas saw)."""

    model_config = _FORBID

    proposal_hash: str
    subject: str
    underlying_last: float | None = None
    atm_iv: float | None = None
    ivr: float | None = None
    hv20: float | None = None
    regime: str | None = None
    legs: list[LegQuote] = Field(default_factory=list)
    quotes_as_of: _dt.datetime | None = Field(None, description="Oldest leg quote time")
    at: _dt.datetime
    analytics: ProposalAnalytics | None = Field(
        None, description="E6.1a card v2 numbers (cost, liquidity, moneyness, vol, exit model)"
    )

    _at = field_validator("at")(_aware)


class OutcomeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED_WORTHLESS = "expired_worthless"
    NEVER_FILLED = "never_filled"
    NOT_TRADED = "not_traded"


class OutcomeRecord(BaseModel):
    """What actually happened to a proposal. Computed deterministically (attribution.py).

    Money is per position in dollars; prices are per share (+debit / -credit),
    like :class:`~arc.models.Structure`. Fields stay ``None`` until E6.2 (fills)
    and E6.3 (marks/reconciliation) provide them.
    """

    model_config = _FORBID

    proposal_hash: str
    status: OutcomeStatus
    contracts: int | None = Field(None, ge=0)
    limit_price: Decimal | None = None
    entry_fill: Decimal | None = None
    slippage_usd: Decimal | None = Field(None, description="+ = worse than the limit")
    slippage_bps: float | None = Field(None, description="Slippage vs capital at risk, in bps")
    cost_bps: float | None = Field(None, description="Quant's expected round-trip cost, bps")
    exit_fill: Decimal | None = None
    realised_pnl: Decimal | None = None
    max_adverse_excursion: Decimal | None = Field(None, le=0)
    days_held: int | None = Field(None, ge=0)
    exit_reason: str | None = None
    ev_total: Decimal | None = None
    pnl_vs_ev: Decimal | None = None
    hold_to_expiry_shadow_pnl: Decimal | None = None
    supersedes_id: str | None = None
    at: _dt.datetime

    _at = field_validator("at")(_aware)


class DecisionReview(BaseModel):
    """Post-hoc review. An LLM reviewer may only write these, citing existing decisions."""

    model_config = _FORBID

    id: str | None = None
    proposal_hash: str | None = None
    decision_id: str | None = None
    label: ReviewLabel
    root_cause: RootCause
    notes: str = ""
    reviewer: Reviewer
    cites: list[str] = Field(
        default_factory=list, description="decision ids this review relies on (FK-checked)"
    )
    supersedes_id: str | None = None
    at: _dt.datetime

    _at = field_validator("at")(_aware)

    @model_validator(mode="after")
    def _target(self) -> DecisionReview:
        if self.proposal_hash is None and self.decision_id is None:
            msg = "a review targets a proposal_hash or a decision_id"
            raise ValueError(msg)
        return self
