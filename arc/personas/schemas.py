"""Strict JSON output schemas for each Arc persona.

Every persona returns structured JSON validated against these pydantic models.
Schemas are referenced by the SKILL.md files and used in golden tests.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from arc.models import CatalystType, Stance

# ---------------------------------------------------------------------------
# Scout — surfaces Candidate objects from raw information sources
# ---------------------------------------------------------------------------


class ScoutCandidateOut(BaseModel):
    """A single candidate surfaced by Scout."""

    ticker: str = Field(..., description="Underlying symbol, e.g. 'AAPL'")
    stance: Stance = Field(..., description="Directional stance: bullish | bearish | neutral")
    catalyst_type: CatalystType = Field(
        ...,
        description="One of: earnings, macro, sector, news, technical",
    )
    catalyst_date: str | None = Field(None, description="ISO-8601 date of catalyst, if known")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score 0-1")
    sources: list[str] = Field(
        ..., min_length=1, description="At least one source URL or reference"
    )
    rationale: str = Field(..., description="One-paragraph explanation of the catalyst and stance")


class ScoutOutput(BaseModel):
    """Scout persona output: a list of trading candidates."""

    candidates: list[ScoutCandidateOut] = Field(..., description="Candidates surfaced this scan")
    scan_summary: str = Field(..., description="Brief summary of what was scanned and key themes")


# ---------------------------------------------------------------------------
# Director — ranks and filters candidates, adds thesis
# ---------------------------------------------------------------------------


class DirectorRankedItem(BaseModel):
    """A single ticker ranked by the Director."""

    ticker: str
    rank: int = Field(..., ge=1, description="1 = highest conviction")
    thesis: str = Field(..., description="Director's thesis: why this ticker, what structure style")
    regime_context: str = Field(..., description="Current regime assessment for this underlying")
    suggested_structure_type: str = Field(
        ...,
        description="Suggested structure: vertical_spread | iron_condor | long_call | long_put",
    )
    stance: str = Field(..., description="bullish | bearish | neutral")
    confidence: float = Field(..., ge=0.0, le=1.0)


class DirectorOutput(BaseModel):
    """Director persona output: ranked shortlist with thesis."""

    shortlist: list[DirectorRankedItem] = Field(..., description="Ranked candidates, best first")
    market_regime: str = Field(
        ...,
        description="Overall market regime: risk_on | risk_off | transitional",
    )
    session_notes: str = Field(
        ..., description="Director's summary of today's opportunity landscape"
    )


# ---------------------------------------------------------------------------
# Quant — proposes concrete option structures with analytics
# ---------------------------------------------------------------------------


class QuantLeg(BaseModel):
    """A single leg in a proposed structure."""

    occ_symbol: str = Field(..., description="OCC option symbol")
    side: str = Field(..., description="long | short")
    ratio: int = Field(1, ge=1)
    strike: float = Field(..., description="Strike price")
    expiry: str = Field(..., description="Expiry date ISO-8601")
    option_type: str = Field(..., description="call | put")


class QuantGreeks(BaseModel):
    """Net position Greeks for a structure."""

    delta: float
    gamma: float
    vega: float
    theta: float


class QuantStructureOut(BaseModel):
    """A single structure proposed by Quant."""

    ticker: str
    structure_type: str = Field(
        ...,
        description="vertical_spread | iron_condor | long_call | long_put",
    )
    legs: list[QuantLeg] = Field(..., min_length=1)
    net_debit_credit: float = Field(..., description="Positive=debit, negative=credit")
    max_gain: float | None = None
    max_loss: float | None = None
    breakevens: list[float] = Field(default_factory=list)
    greeks: QuantGreeks
    dte: int = Field(..., ge=0)
    pop: float = Field(..., ge=0.0, le=1.0, description="Probability of profit")
    ev_per_contract: float = Field(..., description="Expected value per contract")
    cost_bps: float = Field(..., ge=0.0, description="Estimated round-trip cost in basis points")
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., description="Why this structure for this candidate")


class QuantOutput(BaseModel):
    """Quant persona output: proposed structures with analytics."""

    structures: list[QuantStructureOut] = Field(..., description="Proposed structures, best first")
    analysis_notes: str = Field(..., description="Quant's overall analysis summary")


# ---------------------------------------------------------------------------
# Risk — advisory risk assessment (no sizing authority)
# ---------------------------------------------------------------------------


class RiskAssessment(BaseModel):
    """Risk assessment for a single proposed structure."""

    ticker: str
    structure_type: str
    risk_rating: str = Field(
        ...,
        description="low | moderate | elevated | high",
    )
    concentration_warning: bool = Field(
        False, description="True if adding this would exceed concentration limits"
    )
    greek_budget_impact: str = Field(
        ...,
        description="How this trade impacts portfolio-level Greek budgets",
    )
    calendar_concerns: str = Field(
        ...,
        description="Earnings, holidays, or DTE concerns",
    )
    sizing_suggestion: int = Field(
        ...,
        ge=0,
        description="Advisory contract count suggestion (not authoritative — gate decides)",
    )
    max_loss_pct_equity: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Max loss as fraction of equity",
    )
    narrative: str = Field(..., description="Full risk narrative for human review")


class RiskOutput(BaseModel):
    """Risk persona output: advisory risk assessments.

    Risk output is ADVISORY ONLY — sizing and limit enforcement
    are handled by the deterministic gate, not this persona.
    """

    assessments: list[RiskAssessment] = Field(
        ..., description="One assessment per proposed structure"
    )
    portfolio_summary: str = Field(
        ...,
        description="Current portfolio risk posture summary",
    )
    advisory_notes: str = Field(
        ...,
        description="Overall risk advisory for this session",
    )


# ---------------------------------------------------------------------------
# Investor — order plan for approved proposals
# ---------------------------------------------------------------------------


class ImprovementStep(BaseModel):
    """A price improvement step in the execution plan."""

    step_number: int = Field(..., ge=1)
    price: float = Field(..., description="Limit price for this step")
    wait_seconds: int = Field(..., ge=0, description="Seconds to wait before moving to next step")


class InvestorPlan(BaseModel):
    """Investor plan for a single approved proposal."""

    ticker: str
    structure_type: str
    order_type: str = Field(..., description="limit | market (always limit for Phase 1)")
    initial_limit_price: float = Field(..., description="Initial limit at mid-price")
    improvement_steps: list[ImprovementStep] = Field(
        ..., description="Bounded price improvement steps"
    )
    timeout_seconds: int = Field(..., ge=0, description="Total timeout before cancel")
    contracts: int = Field(..., ge=1)
    notes: str = Field(..., description="Execution notes and rationale")


class InvestorOutput(BaseModel):
    """Investor persona output: order plans for approved proposals."""

    plans: list[InvestorPlan] = Field(..., description="One plan per approved proposal")
    market_conditions_note: str = Field(
        ..., description="Current market conditions relevant to execution"
    )


# ---------------------------------------------------------------------------
# Auditor — daily journal, anomalies, lessons
# ---------------------------------------------------------------------------


class AnomalyReport(BaseModel):
    """A single anomaly detected during reconciliation."""

    category: str = Field(
        ...,
        description="fill_discrepancy | position_mismatch | pnl_deviation | missing_event | other",
    )
    severity: str = Field(..., description="info | warning | critical")
    description: str
    affected_orders: list[str] = Field(default_factory=list, description="Order IDs affected")


class LessonLearned(BaseModel):
    """A lesson extracted from recent trading activity."""

    topic: str
    observation: str
    recommendation: str


class AuditorOutput(BaseModel):
    """Auditor persona output: daily journal with anomalies and lessons."""

    journal_date: str = Field(..., description="ISO-8601 date")
    daily_pnl: float = Field(..., description="Net daily P&L")
    open_positions: int = Field(..., ge=0)
    closed_today: int = Field(..., ge=0)
    fills_reviewed: int = Field(..., ge=0)
    anomalies: list[AnomalyReport] = Field(default_factory=list)
    lessons: list[LessonLearned] = Field(default_factory=list)
    journal_narrative: str = Field(..., description="Full daily journal narrative")
    reconciliation_status: str = Field(..., description="clean | discrepancies_found | pending")
