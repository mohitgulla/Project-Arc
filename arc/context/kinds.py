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
    QuantOutput,
    ReconcileOutput,
    ResearchOutput,
    RiskOpenAssessment,
    RiskOutput,
)
from arc.positions.evaluate import PositionReview
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
    """Scalp candidate (E4.2)."""

    model_config = _FORBID


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


class PositionReviewPayload(PositionReview):
    """E6.4 deterministic review of one open position (subject = open structure id)."""

    model_config = _FORBID


class PortfolioContextPayload(PortfolioContext):
    """E5.9 (D33): Research's deterministic view of the open book (subject ``session``)."""

    model_config = _FORBID


class JournalPayload(ReconcileOutput):
    """Auditor daily journal."""  # D56: Broker reconcile journal; schema text pinned (v1)

    model_config = _FORBID


class NoteTopic(enum.StrEnum):
    """What a :class:`NotePayload` is about (D27)."""

    THESIS = "thesis"  # why a trade/ticker/idea (Research, Quant)
    REGIME_VIEW = "regime_view"  # market/sector regime read (Research, Scalp)
    PORTFOLIO_VIEW = "portfolio_view"  # E5.9: Research's read of the open book
    THESIS_CHECK = "thesis_check"  # E5.9: is an open position's thesis still intact?
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
    KindSpec("candidate", CandidatePayload, schema_version=2),  # E4.5: corroboration
    KindSpec("regime", RegimePayload, schema_version=2),  # E4.12: iv_percentile_ext
    KindSpec("shortlist", ShortlistPayload, schema_version=3),  # E5.9: portfolio_view/no_trade
    KindSpec("structures", StructuresPayload, schema_version=3),  # E13.9: revision_of/kept
    KindSpec("risk_review", RiskReviewPayload, schema_version=2),  # E13.9: verdicts
    KindSpec("proposal", ProposalPayload, schema_version=2),  # E13.9: revised
    KindSpec("position_review", PositionReviewPayload, schema_version=2),  # E6.4a: floor window
    KindSpec("portfolio_context", PortfolioContextPayload),  # E5.9 (D33)
    KindSpec("journal", JournalPayload),
    KindSpec("note", NotePayload, schema_version=2),  # E13.1 (D56): scalp/research/broker/ops
    # E4.5 (D30): story digests + options-trading data sources
    KindSpec("story", StoryPayload),
    KindSpec("vol_term", VolTermPayload),
    KindSpec("put_call", PutCallPayload),
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
