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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.exits.model import ExitModelResult  # noqa: TC001 - pydantic field
from arc.features.snapshot import FeatureSnapshot
from arc.models import Candidate, CatalystType, ChannelBrief, Proposal, Stance
from arc.personas.schemas import (
    ExitWatchItem,
    QuantOutput,
    ReconcileOutput,
    ResearchOutput,
    RiskExitVerdict,
    RiskOpenAssessment,
    RiskOutput,
    ScoutTickerCall,
)
from arc.positions.evaluate import PositionReview
from arc.positions.exit_case import ExitCase
from arc.positions.portfolio import MarketGuard, PortfolioContext
from arc.universe.tiers import ActiveUniverse, UniverseTierPayload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.personas.schemas import ResearchRankedItem

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
    """Candidate entry (E4.2 Scalp; v3, E13.7: also the Scout).

    v3 (D53/D56, text-free additions, both defaulted so v2 rows still validate):
    ``feed`` names the persona that raised it; ``origins`` are the Scout's YouTube
    channel ids (``youtube:<slug>``), validated by code against that run's briefs.
    """

    model_config = _FORBID

    feed: Literal["scalp", "scout"] = "scalp"
    origins: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("origins")
    @classmethod
    def _origin_ids(cls, v: list[str]) -> list[str]:
        for o in v:
            if not o or len(o) > 80 or any(ch.isspace() for ch in o):
                msg = f"origin must be a single id token, got {o[:60]!r}"
                raise ValueError(msg)
        return v


class RegimePayload(FeatureSnapshot):
    """Regime + vol features for one underlying (E4.3)."""

    model_config = _FORBID


class ShortlistPayload(ResearchOutput):
    """Research ranked shortlist (v2, E5.7: every ranked name, exclusions, evidence).

    ``budget`` is the Quant/Risk budget (``pipeline_max_shortlist``) in force when the
    Research ran: the first ``budget`` ranked tickers get a structure; the rest stay
    on the card as "Ranked, not structured". Never shown to Research.
    """

    model_config = _FORBID

    budget: int | None = Field(None, ge=0)
    # E5.9 (D33), schema v3. All default: v2 rows still validate.
    market_guard: MarketGuard | None = Field(
        None, description="The deterministic market-conditions guard result for this run"
    )
    suppressed: list[str] = Field(
        default_factory=list,
        description="Ideas (ticker stance structure) the dedupe held back from Research",
    )
    # E13.8 (D56/D53), schema v4 (additive): the idea pool's make-up; None = the
    # control (research_idea_pool scalp + research_compact_prompt full) or a v3 row.
    pool_counts: dict[Literal["scalp", "scout", "both", "scout_only_capped"], int] | None = Field(
        None, description="Idea pool size by feed, plus Scout-only ideas cut by the cap"
    )
    # E13.17 (D56), schema v5 (additive): the exit watchlist's hold / review counts;
    # None = personas.exit_path deterministic (no watchlist) or an older row.
    exit_watchlist_counts: dict[Literal["hold", "review"], int] | None = Field(
        None, description="Research's exit watchlist: positions to hold / to review"
    )

    def budgeted(self) -> list[ResearchRankedItem]:
        """The ranked items inside the Quant/Risk budget (all of them when unset)."""
        ranked = sorted(self.shortlist, key=lambda i: i.rank)
        return ranked if self.budget is None else ranked[: self.budget]

    def over_budget(self) -> list[ResearchRankedItem]:
        ranked = sorted(self.shortlist, key=lambda i: i.rank)
        return [] if self.budget is None else ranked[self.budget :]


class StructuresPayload(QuantOutput):
    """Quant structures with analytics (v2, E5.7: every budgeted ticker accounted for).

    ``skipped`` holds the Quant's own skips plus the deterministic ones (no chain,
    account profile). ``not_structured`` = budgeted tickers with a menu that got
    neither a structure nor a reason. ``over_budget`` = ranked beyond the budget.

    v3 (E13.9, additive): a ``quant.revise`` entry sets ``revision_of`` (the
    ``risk_review`` entry id it answers) and ``kept`` (revise-requested tickers
    re-emitted unchanged); it supersedes the first ``structures`` entry.
    """

    model_config = _FORBID

    not_structured: list[str] = Field(default_factory=list)
    over_budget: list[str] = Field(default_factory=list)
    revision_of: str | None = None
    kept: list[str] = Field(default_factory=list)


class RiskReviewPayload(RiskOutput):
    """Risk persona advisory review (v2, E13.9: per-structure ``verdict``, default accept)."""

    model_config = _FORBID

    assessments: list[RiskOpenAssessment] = Field(  # type: ignore[assignment]
        ..., description="One assessment per proposed structure"
    )


class ProposalPayload(Proposal):
    """Full trade proposal (pre-gate), plus the E2.4 exit model for the card.

    ``exit_model`` is context only: it is not part of :class:`Proposal`, so it never
    enters the gate's proposal hash. ``revised`` (E13.9) marks a structure that came
    out of the ``quant.revise`` round; context only, never hashed.
    """

    model_config = _FORBID

    exit_model: ExitModelResult | None = None
    revised: bool = False
    # E13.18 (D56), schema v3: Risk's exit verdict on a research-path close (closes
    # only; context only, never hashed).
    exit_review: RiskExitVerdict | None = None


class PositionReviewPayload(PositionReview):
    """E6.4 deterministic review of one open position (subject = open structure id)."""

    model_config = _FORBID


class PortfolioContextPayload(PortfolioContext):
    """E5.9 (D33): Research's deterministic view of the open book (subject ``session``).

    v2 (E13.17, additive): each position may carry ``facts`` (exit path only).
    """

    model_config = _FORBID


class ExitWatchlistPayload(BaseModel):
    """Research's exit watchlist (E13.17, D56); kind ``exit_watchlist``, subject ``session``.

    ``items`` are the code-validated watch items (unknown structure ids dropped, one
    per structure, a missing position defaults to ``hold``). ``inputs`` counts what
    Research had to go on, by code: stories, scout_mentions, iv_rank_known,
    earnings_known, ex_div_known. Advisory: it never creates a proposal.
    """

    model_config = _FORBID

    as_of: str
    items: list[ExitWatchItem] = Field(default_factory=list)
    positions_seen: int = Field(..., ge=0)
    missing: list[str] = Field(
        default_factory=list, description="Open structure ids Research gave no item for"
    )
    inputs: dict[str, int] = Field(default_factory=dict)
    schema_version: int = 1


class ExitCasePayload(ExitCase):
    """One exit case Quant judged (E13.17, D56); kind ``exit_case``, subject = structure id."""

    model_config = _FORBID


class RiskExitReviewPayload(BaseModel):
    """Risk's review of the exit cases (E13.18, D56); kind ``risk_exit_review``.

    Subject ``session``. ``verdicts`` holds one entry per reviewed case (code fills a
    missing one with ``hold``). ``unavailable`` = the Risk call failed, timed out or
    did not parse: every verdict is ``hold`` and ``quant.propose`` applies the fallback
    rules (a deterministic discretionary signal closes as today).
    """

    model_config = _FORBID

    as_of: str
    case_ids: list[str] = Field(default_factory=list, description="exit_case entry ids reviewed")
    verdicts: list[RiskExitVerdict] = Field(default_factory=list)
    unavailable: bool = False
    persona_call_id: str | None = None
    schema_version: int = 1


class JournalPayload(ReconcileOutput):
    """Auditor daily journal."""  # D56: Broker reconcile journal; schema text pinned (v1)

    model_config = _FORBID


class NoteTopic(enum.StrEnum):
    """What a :class:`NotePayload` is about (D27)."""

    THESIS = "thesis"  # why a trade/ticker/idea (Research, Quant)
    REGIME_VIEW = "regime_view"  # market/sector regime read (Research, Scalp)
    PORTFOLIO_VIEW = "portfolio_view"  # E5.9: Research's read of the open book
    THESIS_CHECK = "thesis_check"  # E5.9: is an open position's thesis still intact?
    EXIT_WATCH = "exit_watch"  # E13.17: Research's exit watchlist read (session)
    OBSERVATION = "observation"  # informational: news theme, scan summary (Scalp)
    RISK_FLAG = "risk_flag"  # portfolio/calendar concern (Risk)
    LESSON = "lesson"  # post-trade learning (Broker reconcile / Ops)
    EXECUTION = "execution"  # fill/market-conditions note (Broker)


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

    persona: Literal[
        "scalp", "scout", "research", "quant", "risk", "investor", "auditor", "broker", "ops"
    ] = Field(
        ...,
        description=(
            "Author. D54: 'scout' before the rename cutover is the Scalp (legacy rows); "
            "after it, the slow-feed Scout persona (E5.13). D56: stored v1 'sweep' / "
            "'director' notes read as 'scalp' / 'research'."
        ),
    )

    @field_validator("persona", mode="before")
    @classmethod
    def _legacy_persona(cls, v: object) -> object:
        # D56 (E13.1): v1 notes were written as 'sweep' / 'director' (arc.journal.legacy).
        return {"sweep": "scalp", "director": "research"}.get(v, v) if isinstance(v, str) else v

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
    # v3 (E13.10): code-counted facts about the run the note describes (e.g. the
    # Scalp's ``mentions`` count). Defaulted, so v2 rows still validate.
    facts: dict[str, int] = Field(default_factory=dict, max_length=12)


# ---------------------------------------------------------------------------
# E4.5 (D30): story digests + options-trading data sources
# ---------------------------------------------------------------------------


class StoryEvidence(BaseModel):
    """A verbatim quote from one of the story's documents (checked against the text)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(..., min_length=1)
    quote: str = Field(..., min_length=1, max_length=300)


class StoryPayload(BaseModel):
    """Stage-1 digest of one story (a cluster of near-duplicate docs, D30).

    ``distinct_sources`` / ``source_keys`` / ``urls`` are computed by the code from
    the cluster, never by the LLM; the LLM only writes ``summary``, the catalyst and
    the evidence quotes (each quote must occur in its document or it is dropped).
    ``mode`` = ``llm`` (cheap-tier digest) or ``extractive`` (no LLM: fixtures, or
    the digest call failed).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: str = Field(..., min_length=1)
    headline: str = Field(..., max_length=300)
    category: str
    source_keys: list[str] = Field(..., min_length=1)
    distinct_sources: int = Field(..., ge=1)
    doc_ids: list[str] = Field(..., min_length=1)
    urls: list[str] = Field(..., min_length=1, max_length=50)
    first_published: str
    last_published: str
    tickers: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(..., min_length=1, max_length=600)
    catalyst_type: CatalystType | None = None
    catalyst_date: str | None = None
    evidence: list[StoryEvidence] = Field(default_factory=list, max_length=3)
    mode: Literal["llm", "extractive"] = "llm"


class VolTermPayload(BaseModel):
    """VIX term structure (Cboe daily closes): feeds the regime read (E4.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str = Field(..., description="Trading date of the closes (YYYY-MM-DD)")
    vix9d: float | None = None
    vix: float = Field(..., gt=0)
    vix3m: float | None = None
    vvix: float | None = None
    ratio_3m_1m: float | None = Field(None, description="VIX3M / VIX (> 1 = contango)")
    ratio_9d_1m: float | None = Field(None, description="VIX9D / VIX (> 1 = front stress)")
    structure: Literal["contango", "flat", "backwardation"]
    source: str = "cboe"


class PutCallPayload(BaseModel):
    """Cboe daily put/call ratios (options sentiment)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str
    total: float | None = None
    equity: float | None = None
    index: float | None = None
    etp: float | None = None
    spx: float | None = None
    vix: float | None = None
    source: str = "cboe"


# E13.5 (D56): options_slow, Cboe daily market statistics + CFE VX settlements.
PcSegment = Literal["total", "index", "etp", "equity", "vix", "spx"]
OiProduct = Literal["all", "index", "etp", "equity", "vix", "spx"]


class PcRatio(BaseModel):
    """One Cboe put/call segment for a session, with that product's call/put volume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment: PcSegment
    ratio: float = Field(..., ge=0)
    call_volume: int | None = Field(None, ge=0)
    put_volume: int | None = Field(None, ge=0)


class ProductOi(BaseModel):
    """Open interest (and volume) per Cboe product group for a session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: OiProduct
    call_oi: int = Field(..., ge=0)
    put_oi: int = Field(..., ge=0)
    total_oi: int = Field(..., ge=0)
    volume: int | None = Field(None, ge=0)


class OptionsDailyPayload(BaseModel):
    """Cboe daily options market statistics (E13.5, D56); subject = ``market``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str = Field(..., description="Session date of the statistics (YYYY-MM-DD)")
    fetched_at: str = Field(..., description="ISO time (ET) of the fetch")
    ratios: list[PcRatio] = Field(..., min_length=1)
    open_interest: list[ProductOi] = Field(default_factory=list)
    source: Literal["cboe"] = "cboe"
    url: str


class VxPoint(BaseModel):
    """One CFE VX futures settlement (monthly, or a weekly flagged ``weekly``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(..., description='CFE symbol, e.g. "VX/V6" (monthly) or "VX40/V6"')
    expiry: str = Field(..., description="Expiration date (YYYY-MM-DD)")
    settle: float = Field(..., gt=0)
    weekly: bool = False


class VxCurvePayload(BaseModel):
    """CFE VX futures settlement curve (E13.5, D56); subject = ``market``.

    ``front`` / ``second`` / ``back`` are the 1st, 2nd and last *monthly* settles;
    ``shape`` is ``flat`` when ``|slope_1_2_pct|`` is below ``options_slow.vx_flat_band``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str = Field(..., description="Settlement (session) date (YYYY-MM-DD)")
    fetched_at: str = Field(..., description="ISO time (ET) of the fetch")
    points: list[VxPoint] = Field(
        ..., min_length=2, description="Monthlies first by expiry, then weeklies by expiry"
    )
    front: float = Field(..., gt=0)
    second: float = Field(..., gt=0)
    back: float = Field(..., gt=0)
    slope_1_2_pct: float = Field(..., description="(second / front - 1) * 100")
    shape: Literal["contango", "flat", "backwardation"]
    source: Literal["cboe_cfe"] = "cboe_cfe"
    url: str


# E13.6 (D56): options_fast, Cboe ~15-min delayed quotes (index vols, per-ticker chain
# top-of-book) + the exchange symbol_data volume CSVs. Context only, never gate inputs.
IndexVolSymbol = Literal["VIX", "VIX9D", "VXN", "VIX1D", "VIX3M", "VVIX"]
IndexVolFlag = Literal["9d_over_30d", "backwardation_30d_3m", "vix_gt_25", "vix_gt_35"]
SymbolDataMarket = Literal["opt", "cone", "ctwo", "exo"]


class IndexVol(BaseModel):
    """One Cboe delayed index quote (~15 minutes delayed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: IndexVolSymbol
    value: float = Field(..., gt=0)
    as_of: str = Field(..., description="Quote time (ET ISO), ~15 minutes delayed")


class IndexVolsPayload(BaseModel):
    """Intraday VIX complex + VXN (E13.6, D56); subject = ``market``.

    ``ratio_9d_30d`` = VIX9D / VIX, ``ratio_30d_3m`` = VIX / VIX3M (> 1 = backwardation).
    Flips (a flag turning on or off) are derived against the previous entry when the
    tape is rendered (:func:`arc.ingest.cboe_fast.detect_flips`), not stored.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fetched_at: str
    quotes: list[IndexVol] = Field(..., min_length=1)
    ratio_9d_30d: float | None = None
    ratio_30d_3m: float | None = None
    flags: list[IndexVolFlag] = Field(default_factory=list)
    source: Literal["cboe_delayed"] = "cboe_delayed"

    def value(self, symbol: str) -> float | None:
        return next((q.value for q in self.quotes if q.symbol == symbol), None)


class BookLevel(BaseModel):
    """Top-of-book of one near-ATM contract from the delayed chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occ_symbol: str
    option_type: Literal["call", "put"]
    strike: float
    expiry: str
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    spread_pct: float | None = Field(..., description="(ask - bid) / mid; None if not two-sided")
    iv: float | None
    open_interest: int | None
    volume: int = Field(..., ge=0)


class ChainSnapshotPayload(BaseModel):
    """Per-ticker delayed chain snapshot (E13.6, D56); subject = ticker.

    Volumes are session-to-date over the whole chain; ``book`` is the 3 strikes
    nearest spot (call + put) on ``expiry``, the nearest expiry inside the entry
    DTE window.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    fetched_at: str
    spot: float | None
    expiry: str
    call_volume_td: int = Field(..., ge=0)
    put_volume_td: int = Field(..., ge=0)
    put_call_volume: float | None
    atm_spread_pct: float | None
    atm_oi: int | None
    book: list[BookLevel] = Field(..., max_length=6)
    source: Literal["cboe_delayed"] = "cboe_delayed"


class ExchangeVolumeRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    underlying: str
    market: SymbolDataMarket
    volume: int = Field(..., ge=0)
    matched: int | None
    routed: int | None
    contracts: int = Field(..., ge=0)


class ExchangeVolumePayload(BaseModel):
    """Cboe exchange ``symbol_data`` volume per underlying (E13.6, D56); subject = ``market``.

    Active-list names + the top 10 others by volume: Tower/Ops visibility only, never
    a Scout input and never a discovery source (D56 owner decision 1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fetched_at: str
    rows: list[ExchangeVolumeRow] = Field(..., max_length=60)
    total_rows_parsed: int = Field(..., ge=0)
    source: Literal["cboe_symbol_data"] = "cboe_symbol_data"


class MacroEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: str = Field(..., description="YYYY-MM-DD (ET)")
    time: str | None = Field(None, description="HH:MM ET when known")
    kind: Literal["fomc", "cpi", "ppi", "nfp", "jolts", "eci", "pce", "gdp", "other"]
    name: str = Field(..., max_length=120)
    source: str


class MacroCalendarPayload(BaseModel):
    """Upcoming scheduled macro events (FOMC decisions, BLS releases): IV-crush/event risk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str
    horizon_days: int = Field(..., ge=1)
    events: list[MacroEvent] = Field(default_factory=list)


class ExDividendPayload(BaseModel):
    """Next cash dividend for one underlying (early-assignment risk on short calls)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    ex_date: str
    amount: float | None = Field(None, ge=0)
    record_date: str | None = None
    payable_date: str | None = None
    source: str = "alpaca"


# ---------------------------------------------------------------------------
# E4.8 (D46): Finnhub per-ticker context. Data only: never a gate input, and the
# gate never imports these (import-linter contract in pyproject.toml).
# ---------------------------------------------------------------------------


class EarningsQuarter(BaseModel):
    """One reported quarter (Finnhub ``/stock/earnings``); EPS in USD per share."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period: str = Field(..., description="Fiscal quarter end (YYYY-MM-DD)")
    actual: float | None = None
    estimate: float | None = None
    surprise: float | None = Field(None, description="actual - estimate")
    surprise_pct: float | None = Field(None, description="surprise / |estimate| x 100")


class EarningsHistoryPayload(BaseModel):
    """Last 4-8 reported quarters with EPS surprise; subject = ticker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    quarters: list[EarningsQuarter] = Field(default_factory=list, max_length=8)
    beat_count: int = Field(..., ge=0, description="Quarters with actual > estimate")
    miss_count: int = Field(..., ge=0, description="Quarters with actual < estimate")
    as_of: str = Field(..., description="ET date the data was fetched (YYYY-MM-DD)")
    source: Literal["finnhub"] = "finnhub"


class InsiderActivityPayload(BaseModel):
    """Open-market insider buys/sells (Form 4 codes P/S only) in the last ``window_days``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    window_days: int = Field(..., ge=1)
    buy_count: int = Field(..., ge=0)
    sell_count: int = Field(..., ge=0)
    net_shares: int = Field(..., description="Shares bought - shares sold")
    net_value_usd: float | None = Field(
        None, description="Sum of signed shares x price over priced trades; None = no price"
    )
    distinct_insiders_buying: int = Field(..., ge=0)
    distinct_insiders_selling: int = Field(..., ge=0)
    last_txn_date: str | None = None
    cluster_buy: bool = Field(
        ..., description="At least cluster_buyers distinct buyers within cluster_days"
    )
    as_of: str
    source: Literal["finnhub"] = "finnhub"


class AnalystRecCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period: str
    strong_buy: int = Field(..., ge=0)
    buy: int = Field(..., ge=0)
    hold: int = Field(..., ge=0)
    sell: int = Field(..., ge=0)
    strong_sell: int = Field(..., ge=0)


class AnalystRecsPayload(BaseModel):
    """Latest monthly recommendation trend vs the month before; subject = ticker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    period: str
    strong_buy: int = Field(..., ge=0)
    buy: int = Field(..., ge=0)
    hold: int = Field(..., ge=0)
    sell: int = Field(..., ge=0)
    strong_sell: int = Field(..., ge=0)
    prev_period: AnalystRecCounts | None = None
    net_change: int | None = Field(
        None, description="(strong_buy+buy-sell-strong_sell) now minus the same for prev_period"
    )
    as_of: str
    source: Literal["finnhub"] = "finnhub"


class FundamentalsPayload(BaseModel):
    """Trimmed Finnhub basic financials (D46 field set); missing = None, never 0."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    beta: float | None = None
    high_52w: float | None = None
    high_52w_date: str | None = None
    low_52w: float | None = None
    low_52w_date: str | None = None
    market_cap_musd: float | None = Field(None, description="Market cap, USD millions")
    rel_sp500_4w: float | None = Field(None, description="Price relative to S&P 500, %")
    rel_sp500_13w: float | None = None
    rel_sp500_26w: float | None = None
    rel_sp500_52w: float | None = None
    return_5d_pct: float | None = None
    return_ytd_pct: float | None = None
    forward_pe: float | None = None
    eps_growth_ttm_yoy: float | None = Field(None, description="%")
    revenue_growth_ttm_yoy: float | None = Field(None, description="%")
    as_of: str
    source: Literal["finnhub"] = "finnhub"


# ---------------------------------------------------------------------------
# E13.7 (D56): the Scout's daily read (subject = "session")
# ---------------------------------------------------------------------------


class ScoutCategoryPresence(BaseModel):
    """Code-counted briefs of one YouTube category in a Scout run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    present: int = Field(..., ge=0)
    configured: int = Field(..., ge=0)
    missing: list[str] = Field(
        default_factory=list, description="Channel labels with no fresh brief"
    )


class ScoutInputsPresence(BaseModel):
    """What the Scout actually read, counted by code (not the LLM)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    youtube_macro: ScoutCategoryPresence
    youtube_micro: ScoutCategoryPresence
    options_daily: str | None = Field(None, description="as_of of the fresh entry; None = absent")
    vx_curve: str | None = None
    vol_term: str | None = None


class ScoutReadPayload(BaseModel):
    """The Scout's structured daily read (E13.7, D56); ``scout_read`` kind.

    ``discovery`` is the code-screened discovery tier written this run (<=
    ``funnel.scout.max_discovery``); ``discovery_fill`` its length, alerted as
    ``coverage:scout`` below ``funnel.scout.min_discovery_alert``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str = Field(..., description="ISO time (ET) of the run")
    session: str = Field(..., description="Trading session the read is for (YYYY-MM-DD)")
    regime: str = Field(..., max_length=600)
    options_sentiment: str = Field(..., max_length=600)
    themes: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=6)
    ticker_calls: list[ScoutTickerCall] = Field(default_factory=list, max_length=30)
    inputs: ScoutInputsPresence
    discovery: list[str] = Field(default_factory=list, max_length=25)
    discovery_fill: int = Field(..., ge=0)
    screened_out: dict[str, str] = Field(
        default_factory=dict, description="ticker -> why it is not in discovery"
    )
    prompt_sha: str
    model: str


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
    KindSpec("candidate", CandidatePayload, schema_version=3),  # E13.7: feed, origins
    KindSpec("regime", RegimePayload, schema_version=2),  # E4.12: iv_percentile_ext
    KindSpec("shortlist", ShortlistPayload, schema_version=5),  # E13.17: exit_watchlist_counts
    KindSpec("structures", StructuresPayload, schema_version=3),  # E13.9: revision_of/kept
    KindSpec("risk_review", RiskReviewPayload, schema_version=2),  # E13.9: verdicts
    KindSpec("proposal", ProposalPayload, schema_version=3),  # E13.18: exit_review
    KindSpec("position_review", PositionReviewPayload, schema_version=2),  # E6.4a: floor window
    KindSpec("portfolio_context", PortfolioContextPayload, schema_version=2),  # E13.17: facts
    KindSpec("journal", JournalPayload),
    KindSpec("note", NotePayload, schema_version=4),  # E13.17: exit_watch topic (v3 E13.10 facts)
    # E4.5 (D30): story digests + options-trading data sources
    KindSpec("story", StoryPayload),
    KindSpec("vol_term", VolTermPayload),
    KindSpec("put_call", PutCallPayload),
    # E13.5 (D56): options_slow (Cboe daily stats + CFE VX settlement curve; subject market)
    KindSpec("options_daily", OptionsDailyPayload),
    KindSpec("vx_curve", VxCurvePayload),
    # E13.6 (D56): options_fast (Cboe delayed quotes + exchange symbol_data, 30-min RTH)
    KindSpec("index_vols", IndexVolsPayload),  # subject market
    KindSpec("chain_snapshot", ChainSnapshotPayload),  # subject = ticker
    KindSpec("exchange_volume", ExchangeVolumePayload),  # subject market
    KindSpec("macro_calendar", MacroCalendarPayload),
    KindSpec("ex_dividend", ExDividendPayload),
    # E4.8 (D46): Finnhub per-ticker context (subject = ticker)
    KindSpec("earnings_history", EarningsHistoryPayload),
    KindSpec("insider_activity", InsiderActivityPayload),
    KindSpec("analyst_recs", AnalystRecsPayload),
    KindSpec("fundamentals", FundamentalsPayload),
    # D51 (E12.1): tiered universe. universe_tier subject = tier name (E12.2/E12.3
    # write momentum/trending); active_universe subject = "active" (one per resolve).
    KindSpec("universe_tier", UniverseTierPayload, schema_version=2),  # E12.2: url, partial
    KindSpec("active_universe", ActiveUniverse, schema_version=2),  # E13.4: model, dropped rank
    # E13.7 (D56): the Scout's daily read; subject = "session"
    KindSpec("scout_read", ScoutReadPayload),
    # E13.17 (D56): Research-managed exits. exit_watchlist subject = "session";
    # exit_case subject = open structure id.
    KindSpec("exit_watchlist", ExitWatchlistPayload),
    KindSpec("exit_case", ExitCasePayload),
    # E13.18 (D56): Risk's exit review; subject = "session"
    KindSpec("risk_exit_review", RiskExitReviewPayload),
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
