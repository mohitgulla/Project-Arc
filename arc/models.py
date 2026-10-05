"""Data contracts for Arc (pydantic v2 model stubs).

See PLAN.md section 2.3 for the full specification of each model.
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from arc.utils.calendar import ET

# ---------------------------------------------------------------------------
# Candidate (from Sweep persona)
# ---------------------------------------------------------------------------


class CatalystType(StrEnum):
    """Type of catalyst identified by the Sweep."""

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
    """A trading candidate surfaced by the Sweep persona.

    Funnel discipline (E4.2): this is the only Sweep artefact that flows
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
    corroboration: int | None = Field(
        None,
        ge=0,
        description=(
            "Distinct registry sources behind `sources` (D30), computed by the pipeline; "
            "repeated items from one source count once. None = not computed (pre-E4.5)."
        ),
    )
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
    premium: Decimal | None = Field(
        None,
        ge=0,
        description="Per-share option price used for analytics (e.g. mid). Required by "
        "arc.structures payoff / max gain / max loss math.",
    )


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


class StructureKind(StrEnum):
    """Structure classification (Phase-1 whitelist per PLAN D4, plus OTHER)."""

    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    VERTICAL_DEBIT = "vertical_debit"
    VERTICAL_CREDIT = "vertical_credit"
    IRON_CONDOR = "iron_condor"
    OTHER = "other"


class Structure(BaseModel):
    """A multi-leg option structure with analytics.

    Units (as produced by :mod:`arc.structures`):
      - ``net_debit_credit``: per-share price (the mleg limit-price convention).
      - ``max_gain`` / ``max_loss`` / ``buying_power``: dollars per one unit of the
        structure (contract multiplier applied). ``None`` means unbounded.
      - ``greeks``: position Greeks in share-equivalents (per-share Greek x 100 x
        signed ratio, summed over legs).
    """

    legs: list[Leg]
    kind: StructureKind | None = None
    net_debit_credit: Decimal = Field(..., description="Positive = debit, negative = credit")
    max_gain: Decimal | None = None
    max_loss: Decimal | None = None
    breakevens: list[Decimal] = Field(default_factory=list)
    greeks: Greeks = Field(default_factory=Greeks)
    dte: int = Field(..., ge=0)
    liquidity: Liquidity = Field(default_factory=Liquidity)
    buying_power: Decimal | None = Field(
        None, description="Estimated buying-power reduction per unit (Reg-T style)"
    )


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
    limit_price: Decimal | None = Field(
        None,
        description="Per-share net limit price for the order (positive = debit, negative = "
        "credit). None means the structure's net_debit_credit (mid).",
    )
    earnings_play: bool = Field(
        False, description="Director flagged this as a deliberate earnings play (PLAN §5)."
    )
    risk_concurs: bool = Field(
        False, description="Risk persona concurs with the earnings-play flag (PLAN §5)."
    )


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


class TranscriptSource(StrEnum):
    """How a YouTube transcript was produced (E4.1b)."""

    CAPTIONS = "captions"
    AUDIO = "audio"


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
    transcript_source: TranscriptSource | None = Field(
        None,
        description="YouTube only: whether the text came from captions or local audio STT",
    )
    channel_id: str | None = Field(
        None, description="YouTube channel id (youtube source only); selects the E4.4 processor"
    )
    title: str = Field("", description="Document / video title when the source has one")


# ---------------------------------------------------------------------------
# ChannelBrief (per-channel processor output — E4.4, D14)
# ---------------------------------------------------------------------------

_TICKER_PATTERN = r"^[A-Z][A-Z0-9.]{0,9}$"
QUOTE_MAX_CHARS = 240

Ticker = Annotated[str, StringConstraints(pattern=_TICKER_PATTERN)]
Quote = Annotated[str, StringConstraints(min_length=1, max_length=QUOTE_MAX_CHARS)]
Unit = Annotated[float, Field(ge=0.0, le=1.0)]

_BRIEF_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)


class LevelKind(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"
    PIVOT = "pivot"
    TARGET = "target"
    STOP = "stop"


class CallHorizon(StrEnum):
    INTRADAY = "intraday"
    NEXT_SESSION = "next_session"
    SWING = "swing"
    LONG_TERM = "long_term"


class InstrumentHint(StrEnum):
    SHARES = "shares"
    CALLS = "calls"
    PUTS = "puts"
    SPREAD = "spread"
    NONE = "none"


class BriefCatalystKind(StrEnum):
    EARNINGS = "earnings"
    MACRO = "macro"
    FED = "fed"
    GEOPOLITICAL = "geopolitical"
    SECTOR = "sector"
    OTHER = "other"


class ExpectedImpact(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


class MarketBias(BaseModel):
    """The host's overall market stance for the session the brief applies to."""

    model_config = _BRIEF_CONFIG

    stance: Stance
    confidence: Unit
    quote: Quote


class BriefLevel(BaseModel):
    """A price level the host names on a ticker."""

    model_config = _BRIEF_CONFIG

    ticker: Ticker
    kind: LevelKind
    price: Annotated[float, Field(gt=0.0)]
    quote: Quote
    unverified_price: bool = Field(
        False, description="True when no underlying price was available to sanity-check it"
    )


class BriefCall(BaseModel):
    """A directional call the host asserts or recommends."""

    model_config = _BRIEF_CONFIG

    ticker: Ticker
    stance: Stance
    horizon: CallHorizon
    instrument_hint: InstrumentHint
    conviction: Unit
    quote: Quote


class BriefCatalyst(BaseModel):
    """A scheduled or ongoing event the host says matters."""

    model_config = _BRIEF_CONFIG

    event: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    kind: BriefCatalystKind
    date: date_ | None = None
    tickers: list[Ticker] = Field(default_factory=list)
    expected_impact: ExpectedImpact
    quote: Quote


class BriefRiskFlag(BaseModel):
    """A market-wide risk the host flags (e.g. 'yields > 5%')."""

    model_config = _BRIEF_CONFIG

    flag: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    severity: Severity
    quote: Quote


class ChannelBrief(BaseModel):
    """Validated, structured summary of one channel video (PLAN D14, card E4.4).

    Every item carries a verbatim transcript ``quote`` that was checked by
    code. There is deliberately no sizing / order field and ``extra="forbid"``
    rejects any attempt to add one.
    """

    model_config = _BRIEF_CONFIG

    brief_id: str = Field(..., min_length=1)
    channel_slug: str = Field(..., pattern=r"^[A-Za-z0-9_-]+$")
    video_id: str = Field(..., min_length=1)
    video_url: str = Field(..., min_length=1)
    title: str
    published_at: datetime
    applies_to_session: date_
    guidelines_version: str = Field(..., min_length=1)
    market_bias: MarketBias | None = None
    levels: list[BriefLevel] = Field(default_factory=list)
    calls: list[BriefCall] = Field(default_factory=list)
    catalysts: list[BriefCatalyst] = Field(default_factory=list)
    risk_flags: list[BriefRiskFlag] = Field(default_factory=list)
    tickers_mentioned: list[Ticker] = Field(default_factory=list)
    sponsor_segments_removed: bool = False

    @field_validator("published_at")
    @classmethod
    def _published_at_et(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "published_at must be timezone-aware"
            raise ValueError(msg)
        return v.astimezone(ET)


class Performance(BaseModel):
    """Account Day/MTD/YTD performance from ``pnl_snapshots`` (E6.3, D28). $ and fractions.

    Shared by the Auditor digest (:mod:`arc.slack.digests`) and the control tower.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    day_pnl: float
    day_pct: float | None = None
    mtd_pnl: float | None = None
    mtd_pct: float | None = None
    ytd_pnl: float | None = None
    ytd_pct: float | None = None
    equity: float | None = None
