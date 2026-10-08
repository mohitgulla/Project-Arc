"""Strict JSON output schemas for each Arc persona.

Every persona returns structured JSON validated against these pydantic models.
Schemas are referenced by the SKILL.md files and used in golden tests.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.exits.model import ExitSummary
from arc.models import CatalystType, Stance
from arc.positions.exit_case import ThesisStatus3

# ---------------------------------------------------------------------------
# Scalp — surfaces Candidate objects from raw information sources
# ---------------------------------------------------------------------------


class ScalpCandidateOut(BaseModel):
    """A single candidate surfaced by Scalp."""

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


class ScalpOutput(BaseModel):
    """Scalp persona output: a list of trading candidates."""

    candidates: list[ScalpCandidateOut] = Field(..., description="Candidates surfaced this scan")
    scan_summary: str = Field(..., description="Brief summary of what was scanned and key themes")


# ---------------------------------------------------------------------------
# Scalp stage 1 (E4.5, D30) — one short digest per story
# ---------------------------------------------------------------------------


class StoryEvidenceOut(BaseModel):
    """A verbatim quote from one of the story's documents."""

    url: str = Field(..., description="The url= of the document the quote is copied from")
    quote: str = Field(..., description="Verbatim excerpt (<= 200 chars), copied exactly")


class StoryDigestOut(BaseModel):
    """Stage-1 digest of one story."""

    story_id: str = Field(..., description="The story id exactly as given ([story <id>])")
    summary: str = Field(..., description="One sentence: what happened and why it matters")
    catalyst_type: CatalystType | None = Field(
        None, description="earnings | macro | sector | news | technical, or null"
    )
    catalyst_date: str | None = Field(None, description="ISO date of the catalyst, or null")
    evidence: list[StoryEvidenceOut] = Field(default_factory=list, max_length=3)


class StoryDigestOutput(BaseModel):
    """Stage-1 reply: one digest per story in the batch."""

    stories: list[StoryDigestOut]


# ---------------------------------------------------------------------------
# Research — ranks and filters candidates, adds thesis
# ---------------------------------------------------------------------------


_EVIDENCE_MAX_ITEMS = 3
_EVIDENCE_MAX_CHARS = 160

# E5.9 (D33): portfolio-aware Research vocabulary. Deterministic code reads these
# values; the free text next to them goes to `note` context entries, never to the gate.
type PortfolioFit = Literal["diversifies", "hedges", "adds_concentration", "neutral"]
type PortfolioVerdict = Literal["balanced", "concentrated", "hedge_needed", "reduce_risk"]
type ThesisStatus = Literal["intact", "weakened", "invalidated"]
type NoTradeReason = Literal[
    "none", "no_fit", "too_volatile", "unclear", "budget", "portfolio_full"
]


class ResearchRankedItem(BaseModel):
    """A single ticker ranked by Research."""

    ticker: str
    rank: int = Field(..., ge=1, description="1 = highest conviction")
    thesis: str = Field(..., description="Research's thesis: why this ticker, what structure style")
    regime_context: str = Field(..., description="Current regime assessment for this underlying")
    suggested_structure_type: str = Field(
        ...,
        description="Suggested structure: vertical_spread | iron_condor | long_call | long_put",
    )
    stance: str = Field(..., description="bullish | bearish | neutral")
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            f"Up to {_EVIDENCE_MAX_ITEMS} short, grounded facts behind the pick, e.g. "
            "'8-K: buyback $50B, Sep 24', 'IV rank 18'. Extra items / characters are cut."
        ),
    )

    portfolio_fit: PortfolioFit | None = Field(
        None,
        description=(
            "E5.9 (D33), only when the book is not empty: diversifies | hedges | "
            "adds_concentration | neutral. The pipeline drops adds_concentration picks that "
            "push a flagged dimension over its threshold."
        ),
    )

    @field_validator("evidence", mode="before")
    @classmethod
    def _trim_evidence(cls, v: object) -> object:
        """Deterministic trim (E5.7): at most 3 non-empty items of ≤160 chars each."""
        if not isinstance(v, list):
            return v
        items = [str(x).strip()[:_EVIDENCE_MAX_CHARS] for x in v if str(x).strip()]
        return items[:_EVIDENCE_MAX_ITEMS]


class ResearchExclusion(BaseModel):
    """A candidate Research will not trade, with its one-line reason (E5.7)."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    reason: str = Field(..., min_length=1, description="One line: why this candidate is excluded")


class PoolItem(BaseModel):
    """One ticker of Research's idea pool (E13.8, D56/D53); code-built, text-free.

    Built by :func:`arc.pipeline.research_pool.build_idea_pool` from the active
    ``candidate`` entries (+ the Scout's ``scout_read`` calls) and recorded in the
    Research prompt inputs, so ``arc journal replay`` rebuilds the same prompt.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z][A-Z0-9.]{0,9}$")
    stance: Stance = Field(..., description="the higher-confidence feed's stance")
    confidence: float = Field(..., ge=0.0, le=1.0, description="max over feeds")
    feeds: list[Literal["scalp", "scout"]] = Field(..., min_length=1)
    origins: int = Field(..., ge=1, description="distinct sources/origins across feeds")
    agreement: Literal["agree", "disagree", "single"]
    tier: Literal["core", "momentum", "discovery", "none"] = "none"
    candidate_ids: list[str] = Field(..., min_length=1)
    # display-only facts carried from the candidate (no persona text)
    catalyst_type: CatalystType | None = None
    catalyst_date: str | None = Field(None, description="YYYY-MM-DD, if any")


class ResearchPortfolioView(BaseModel):
    """Research's read of the open book (E5.9); ``notes`` is stored as a note."""

    model_config = ConfigDict(extra="forbid")

    verdict: PortfolioVerdict
    notes: str = Field("", description="One or two lines on diversification and risk")


class ResearchThesisCheck(BaseModel):
    """Is an open position's original thesis still intact? Advisory for E6.4 (E5.9)."""

    model_config = ConfigDict(extra="forbid")

    structure_id: str
    status: ThesisStatus
    reason: str = Field("", description="One line: what changed, or why it still holds")


# E13.17 (D56): Research-managed exits. Three-state thesis status of the exit
# watchlist (defined in arc.positions.exit_case); ``broken`` maps to the E5.9
# ``invalidated`` when the legacy ``thesis_check`` note is written.
_WATCH_EVIDENCE_MAX_ITEMS = 4
_WATCH_EVIDENCE_MAX_CHARS = 160


class ExitWatchItem(BaseModel):
    """One open position on Research's exit watchlist (E13.17, D56).

    ``review`` asks Quant for an exit case; ``hold`` keeps the position. Advisory only:
    a watchlist item never creates a proposal, and mandatory exits (stop, DTE exit,
    expiry) stay deterministic.
    """

    model_config = ConfigDict(extra="forbid")

    structure_id: str
    ticker: str
    action: Literal["hold", "review"]
    thesis_status: ThesisStatus3
    evidence: list[str] = Field(
        default_factory=list,
        max_length=_WATCH_EVIDENCE_MAX_ITEMS,
        description="Up to 4 short facts (each <= 160 chars); cite a story/scout/fact id",
    )
    reason: str = Field("", max_length=240)

    @field_validator("evidence", mode="before")
    @classmethod
    def _trim_evidence(cls, v: object) -> object:
        """Deterministic trim: at most 4 non-empty items of <= 160 chars each."""
        if not isinstance(v, list):
            return v
        items = [str(x).strip()[:_WATCH_EVIDENCE_MAX_CHARS] for x in v if str(x).strip()]
        return items[:_WATCH_EVIDENCE_MAX_ITEMS]

    @field_validator("reason", mode="before")
    @classmethod
    def _trim_reason(cls, v: object) -> object:
        return str(v).strip()[:240] if isinstance(v, str) else v


class ResearchOutput(BaseModel):
    """Research persona output: every candidate ranked or excluded with a reason."""

    shortlist: list[ResearchRankedItem] = Field(
        ...,
        description="Every candidate you would consider trading, best first (no cap)",
    )
    excluded: list[ResearchExclusion] = Field(
        default_factory=list,
        description="Candidates not ranked, each with a one-line reason",
    )
    market_regime: str = Field(
        ...,
        description="Overall market regime: risk_on | risk_off | transitional",
    )
    session_notes: str = Field(
        ..., description="Research's summary of today's opportunity landscape"
    )
    # E5.9 (D33): portfolio assessment. Every field defaults, so the empty-book reply
    # (and every stored v2 shortlist) validates unchanged.
    portfolio_view: ResearchPortfolioView | None = Field(
        None, description="Only when the book is not empty: verdict + notes"
    )
    thesis_checks: list[ResearchThesisCheck] = Field(
        default_factory=list,
        description="One per open structure when the book is not empty",
    )
    no_trade_reason: NoTradeReason | None = Field(
        None,
        description=(
            "Why the shortlist is empty: no_fit | too_volatile | unclear | budget | "
            "portfolio_full (none / null when something is ranked)"
        ),
    )


class ResearchExitOutput(ResearchOutput):
    """Research reply with open positions (E13.17, D56).

    A subclass (not a new field on :class:`ResearchOutput`) so a recorded
    pre-cutover prompt, which embeds the reply schema, replays byte-identical.
    ``exit_watchlist`` replaces ``thesis_checks`` (kept for stored shortlists).
    """

    exit_watchlist: list[ExitWatchItem] = Field(
        default_factory=list,
        description="One per open structure (exit watch): hold | review, thesis status, evidence",
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
    exits: ExitSummary | None = Field(
        None,
        description="Static vs managed-exit PoP/net EV (E2.4). Filled by the pipeline; leave null.",
    )


class QuantSkip(BaseModel):
    """A budgeted ticker the Quant does not structure, with its reason (E5.7)."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    reason: str = Field(..., min_length=1, description="One line: why no structure")


class QuantOutput(BaseModel):
    """Quant persona output: one structure or one skip per shortlisted ticker."""

    structures: list[QuantStructureOut] = Field(..., description="Proposed structures, best first")
    skipped: list[QuantSkip] = Field(
        default_factory=list,
        description="Shortlisted tickers with no structure, each with a one-line reason",
    )
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
# E13.9 (D56): Quant <-> Risk open path. Risk returns a verdict per structure;
# one bounded `quant.revise` round answers the `revise` ones.
#
# The verdict fields live on subclasses so the flag-off Risk/Quant prompts (whose
# JSON Schema block is rendered from RiskOutput / QuantOutput) stay byte-identical.
# ---------------------------------------------------------------------------

RiskVerdict = Literal["accept", "revise", "reject"]
ReviseReason = Literal[
    "size", "width", "dte", "strike", "structure_type", "concentration", "calendar"
]


class RiskReviseRequest(BaseModel):
    """What Risk wants Quant to change about one structure (advisory text + hints)."""

    model_config = ConfigDict(extra="forbid")

    reason: ReviseReason
    instruction: str = Field(..., max_length=240, description="Advisory text for Quant")
    max_contracts: int | None = Field(None, ge=0)
    target_dte: tuple[int, int] | None = None
    preferred_structure_type: str | None = None


class RiskOpenAssessment(RiskAssessment):
    """A Risk assessment with the E13.9 verdict (``accept`` when absent)."""

    verdict: RiskVerdict = Field(
        "accept",
        description="accept = trade as is; revise = ask Quant for one change "
        "(set revise_request); reject = do not trade it",
    )
    revise_request: RiskReviseRequest | None = Field(
        None, description="Required when verdict is revise; null otherwise"
    )


class RiskOpenOutput(RiskOutput):
    """Risk output on the E13.9 open path (verdicts; always since E13.15)."""

    assessments: list[RiskOpenAssessment] = Field(  # type: ignore[assignment]
        ..., description="One assessment (with a verdict) per proposed structure"
    )


class QuantReviseOutput(QuantOutput):
    """Quant's reply in the one ``quant.revise`` round (E13.9)."""

    kept: list[str] = Field(
        default_factory=list,
        description="Tickers Risk asked to revise that you keep unchanged (one-line "
        "reason in analysis_notes)",
    )


# E13.18 (D56): Risk's review of Quant's exit cases.
type RiskExitReasonCode = Literal[
    "thesis_broken",
    "ev_exhausted",
    "risk_event",
    "capacity",
    "concentration",
    "thesis_intact",
    "ev_remaining",
    "costs_exceed_gain",
    "await_eod_marks",
]


class RiskExitVerdict(BaseModel):
    """Risk's verdict on one exit case (E13.18): ``close`` or ``hold``, with a reason.

    Advisory input to code: a ``close`` still goes through ``propose_close`` (gate
    ``closing=True``, price band, token, approval); a ``hold`` is journaled.
    """

    model_config = ConfigDict(extra="forbid")

    structure_id: str = Field(..., description="The case's structure_id, copied verbatim")
    verdict: Literal["close", "hold"]
    reason_code: RiskExitReasonCode
    reason: str = Field(..., max_length=240)

    @field_validator("reason", mode="before")
    @classmethod
    def _trim_reason(cls, v: object) -> object:
        return str(v).strip()[:240] if isinstance(v, str) else v


class RiskExitOutput(BaseModel):
    """Risk's reply on ``risk.exit`` (E13.18): one verdict per exit case.

    A case missing from ``verdicts`` is ``hold`` (fail closed); unknown ids are ignored.
    """

    model_config = ConfigDict(extra="forbid")

    verdicts: list[RiskExitVerdict] = Field(default_factory=list)


class QuantExitJudgement(BaseModel):
    """Quant's call on one exit case (E13.17, D56): hold or close, with why.

    The numbers come from code (``position_review`` + position facts); this is the
    judgement only. Roll is not an option in D56 (open decision 13).
    """

    model_config = ConfigDict(extra="forbid")

    structure_id: str = Field(..., description="The case's structure_id, copied verbatim")
    recommendation: Literal["hold", "close"]
    rationale: str = Field(..., max_length=400)

    @field_validator("rationale", mode="before")
    @classmethod
    def _trim_rationale(cls, v: object) -> object:
        return str(v).strip()[:400] if isinstance(v, str) else v


class QuantExitOutput(BaseModel):
    """Quant's reply on ``quant.exit`` (E13.17): one judgement per exit case.

    A case missing from ``cases`` fails closed to ``hold``.
    """

    model_config = ConfigDict(extra="forbid")

    cases: list[QuantExitJudgement] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Broker — order plan for approved proposals (D56: was the Investor; deterministic)
# ---------------------------------------------------------------------------


class ImprovementStep(BaseModel):
    """A price improvement step in the execution plan."""

    step_number: int = Field(..., ge=1)
    price: float = Field(..., description="Limit price for this step")
    wait_seconds: int = Field(..., ge=0, description="Seconds to wait before moving to next step")


class BrokerPlan(BaseModel):
    """The Broker's ladder for a single approved proposal (rendered on the order card)."""

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


# ---------------------------------------------------------------------------
# Broker reconcile — daily journal, anomalies, lessons (D56: was the Auditor)
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


class ReconcileOutput(BaseModel):
    """Broker reconcile output: daily journal with anomalies and lessons (``journal`` kind)."""

    journal_date: str = Field(..., description="ISO-8601 date")
    daily_pnl: float = Field(..., description="Net daily P&L")
    open_positions: int = Field(..., ge=0)
    closed_today: int = Field(..., ge=0)
    fills_reviewed: int = Field(..., ge=0)
    anomalies: list[AnomalyReport] = Field(default_factory=list)
    lessons: list[LessonLearned] = Field(default_factory=list)
    journal_narrative: str = Field(..., description="Full daily journal narrative")
    reconciliation_status: str = Field(..., description="clean | discrepancies_found | pending")


class ScoutTickerCall(BaseModel):
    """One Scout ticker call (E13.7, D56), backed by YouTube channel briefs only."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z][A-Z0-9.]{0,9}$")
    stance: Stance
    confidence: float = Field(..., ge=0.0, le=1.0)
    horizon: Literal["days", "weeks"]
    origins: list[str] = Field(
        ...,
        min_length=1,
        max_length=6,
        description="YouTube channel ids from this run's briefs, e.g. 'youtube:stockedup'",
    )
    thesis: str = Field(..., max_length=240)
    catalyst_type: CatalystType
    catalyst_date: str | None = Field(None, description="ISO-8601 date, if any")

    @field_validator("catalyst_type", mode="before")
    @classmethod
    def _brief_catalyst_kind(cls, v: object) -> object:
        """Map a channel brief's catalyst ``kind`` onto :class:`CatalystType`.

        The Scout reads brief JSON whose catalysts use the brief vocabulary
        (``fed`` / ``geopolitical`` / ``other``) and sometimes copies it into its own
        call (live 2026-10-08: ``geopolitical`` failed the whole run twice). Same map
        as the brief → candidate path (``arc.ingest.channels.briefs._CATALYST_TYPE``).
        """
        if isinstance(v, str):
            return _BRIEF_KIND_TO_CATALYST.get(v.strip().lower(), v)
        return v


# Brief catalyst kinds that are not CatalystType values (BriefCatalystKind, D45).
_BRIEF_KIND_TO_CATALYST: dict[str, str] = {
    "fed": CatalystType.MACRO.value,
    "geopolitical": CatalystType.NEWS.value,
    "other": CatalystType.NEWS.value,
}


SCOUT_PROSE_MAX = 600


class ScoutOutput(BaseModel):
    """Scout reply (E13.7, D56): the daily slow-feed read, sections in fixed order."""

    model_config = ConfigDict(extra="forbid")

    regime: str = Field(..., max_length=SCOUT_PROSE_MAX)
    options_sentiment: str = Field(..., max_length=SCOUT_PROSE_MAX)
    themes: list[str] = Field(default_factory=list, max_length=8)
    ticker_calls: list[ScoutTickerCall] = Field(default_factory=list, max_length=30)
    discovery: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Ordered subset of ticker_calls tickers for the discovery tier, best first",
    )
    risks: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("regime", "options_sentiment", mode="before")
    @classmethod
    def _clip_prose(cls, v: object) -> object:
        """Clip an over-long prose section to 600 chars instead of failing the run.

        The model overshoots 600 by a few dozen characters often enough to fail the
        whole daily Scout (live 2026-10-07: 625 chars, two runs in a row). The
        section is display/context prose, so clipping at a word boundary is safe.
        """
        if isinstance(v, str) and len(v) > SCOUT_PROSE_MAX:
            cut = v[: SCOUT_PROSE_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
            return cut + "…"
        return v

    @field_validator("themes")
    @classmethod
    def _theme_lines(cls, v: list[str]) -> list[str]:
        for t in v:
            if len(t) > 160:
                msg = "each theme is one line of at most 160 characters"
                raise ValueError(msg)
        return v
