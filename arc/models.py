"""Data contracts for Arc (pydantic v2 model stubs).

See PLAN.md section 2.3 for the full specification of each model.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Candidate (from Scout persona)
# ---------------------------------------------------------------------------


class CatalystType(StrEnum):
    """Type of catalyst identified by the Scout."""

    EARNINGS = "earnings"
    MACRO = "macro"
    SECTOR = "sector"
    NEWS = "news"
    TECHNICAL = "technical"


class Stance(StrEnum):
    """Directional stance of a candidate."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Candidate(BaseModel):
    """A trading candidate surfaced by the Scout persona.

    Funnel discipline (E4.2): this is the only Scout artefact that flows
    downstream to the scanner. It deliberately carries **no free text** —
    every field is an enum, a symbol, a number, a date or a source URL.
    Persona rationale stays in the audit store and never leaves it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Audit-store row id once persisted")
    ticker: str = Field(
        ...,
        pattern=r"^[A-Z][A-Z0-9.]{0,9}$",
        description="Underlying symbol, e.g. 'AAPL'",
    )
    stance: Stance = Field(..., description="Directional stance: bullish | bearish | neutral")
    catalyst_type: CatalystType
    catalyst_date: datetime | None = Field(None, description="Date of the catalyst event")
    confidence: float = Field(..., ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)
    created_at: datetime

    @field_validator("sources")
    @classmethod
    def _sources_are_references(cls, v: list[str]) -> list[str]:
        """Sources are references (URLs / ids), never prose."""
        for s in v:
            if not s or len(s) > 2048 or any(ch.isspace() for ch in s):
                msg = f"source must be a single reference token (URL or id), got {s[:60]!r}"
                raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Structure (option legs + analytics)
# ---------------------------------------------------------------------------


class LegIntent(StrEnum):
    """Intent of an individual leg."""

    LONG = "long"
    SHORT = "short"


class Leg(BaseModel):
    """A single option leg within a structure."""

    occ_symbol: str = Field(..., description="OCC option symbol")
    side: LegIntent
    ratio: int = Field(1, ge=1)
    intent: str = Field("", description="Human-readable role, e.g. 'long call wing'")


class Greeks(BaseModel):
    """Position-level Greeks."""

    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    rho: float = 0.0
    vanna: float = 0.0
    volga: float = 0.0


class Liquidity(BaseModel):
    """Liquidity metrics for the structure."""

    spread_pct: float = Field(0.0, description="Bid-ask spread as % of mid")
    open_interest: int = 0
    volume: int = 0


class Structure(BaseModel):
    """A multi-leg option structure with analytics."""

    legs: list[Leg]
    net_debit_credit: Decimal = Field(..., description="Positive = debit, negative = credit")
    max_gain: Decimal | None = None
    max_loss: Decimal | None = None
    breakevens: list[Decimal] = Field(default_factory=list)
    greeks: Greeks = Field(default_factory=Greeks)
    dte: int = Field(..., ge=0)
    liquidity: Liquidity = Field(default_factory=Liquidity)


# ---------------------------------------------------------------------------
# Proposal (candidate + structure + sizing)
# ---------------------------------------------------------------------------


class QuantMetrics(BaseModel):
    """Quantitative metrics for a proposal."""

    pop: float = Field(..., ge=0.0, le=1.0, description="Probability of profit")
    ev: Decimal = Field(..., description="Expected value per contract")
    cost_bps: float = Field(0.0, description="Estimated round-trip cost in basis points")


class Sizing(BaseModel):
    """Position sizing for a proposal."""

    contracts: int = Field(..., ge=1)
    notional: Decimal = Field(..., description="Notional exposure")
    pct_equity: float = Field(..., ge=0.0, le=1.0, description="% of account equity")


class Proposal(BaseModel):
    """A fully formed trade proposal ready for gate evaluation."""

    candidate_id: str
    structure: Structure
    thesis: str = Field(..., description="Persona-generated thesis text")
    quant: QuantMetrics
    risk_narrative: str = ""
    sizing: Sizing
    expires_at: datetime


# ---------------------------------------------------------------------------
# GateDecision
# ---------------------------------------------------------------------------


class GateDecision(BaseModel):
    """Result of the deterministic risk-proxy gate evaluation."""

    proposal_hash: str
    passed: bool
    violations: list[str] = Field(default_factory=list)
    token: str | None = Field(None, description="HMAC gate token if passed")
    account_snapshot: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# ApprovalRecord
# ---------------------------------------------------------------------------


class ApprovalDecision(StrEnum):
    """Human decision on a proposal."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalRecord(BaseModel):
    """Record of a human approval decision from Slack."""

    proposal_hash: str
    slack_user: str
    slack_ts: str
    decision: ApprovalDecision
    at: datetime


# ---------------------------------------------------------------------------
# Order (state machine)
# ---------------------------------------------------------------------------


class OrderState(StrEnum):
    """Order lifecycle states."""

    PROPOSED = "proposed"
    GATED = "gated"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderEvent(BaseModel):
    """A single state-transition event in the order lifecycle."""

    from_state: OrderState
    to_state: OrderState
    at: datetime
    detail: str = ""


class Order(BaseModel):
    """An order tracked through its full lifecycle."""

    id: str
    proposal_hash: str
    state: OrderState = OrderState.PROPOSED
    events: list[OrderEvent] = Field(default_factory=list)
    broker_order_id: str | None = None
    client_order_id: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# RawDoc (ingestion output — E4.1)
# ---------------------------------------------------------------------------


class RawDoc(BaseModel):
    """A raw document ingested by a source connector.

    Each connector yields ``RawDoc`` instances which are deduplicated
    by ``content_hash`` (SHA-256 of ``source + url``) and stored in
    the audit database.
    """

    source: str = Field(..., description="Connector name: rss | edgar | earnings | youtube")
    url: str = Field(..., description="Canonical URL of the source document")
    published_at: datetime
    text: str = Field(..., description="Extracted plain text / transcript")
    tickers_hint: list[str] = Field(
        default_factory=list,
        description="Tickers mentioned or associated with this document",
    )
    content_hash: str = Field("", description="SHA-256 hex digest of source+url for dedupe")
