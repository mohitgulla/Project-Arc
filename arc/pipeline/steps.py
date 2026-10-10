"""E5.2 pipeline steps: Research → Quant → Risk → propose (+ gate), as routine handlers.

Each step is a D16 routine handler. It reads the context snapshot the dispatcher
recorded for its run and writes typed context entries through ``ctx.write``.
Steps never hand results to each other in memory. The chain order lives in
``config/routines.yaml`` (``research: chain: [quant, risk, propose]``).

What each step does:

``research`` (LLM, frontier tier)
    Computes regime/vol features for today's candidates and writes them as
    ``regime`` entries. It then records a second snapshot and asks Research
    to rank candidates. Filters: the ticker must be one of the snapshot's
    candidates; the stance and structure type must be valid; at most
    ``pipeline_max_shortlist`` tickers. Writes ``shortlist``.
``quant`` (LLM, frontier tier)
    Runs the deterministic chain scanner (E2.3) for each shortlisted ticker with
    the strategies the account profile (D25, ``config/account_profiles.yaml``)
    maps the stance to: ``margin`` bullish → bull put, bearish → bear call,
    neutral → iron condor; ``cash_debit`` bullish → bull call debit + long call,
    bearish → bear put debit + long put, neutral → no trade (journaled
    ``profile:no_neutral_structure``). It offers
    the Quant a menu of priced structures. The Quant may only pick from the
    menu: a pick is matched to a menu entry by its exact legs, and every number
    (net price, max gain/loss, breakevens, Greeks, PoP, EV, cost) comes from the
    scanner, not the LLM. Writes ``structures``.
``risk`` (LLM, frontier tier)
    Gets an advisory review (narrative plus sizing suggestion) for each
    structure. An assessment that names a structure Quant did not propose is
    dropped. Writes ``risk_review``.
``propose`` (deterministic, no LLM)
    For each shortlisted ticker (best first) that has no proposal yet for the
    session day, it takes Quant's best structure, re-prices it from a fresh
    chain, sizes it per D18 (:mod:`arc.sizing`), builds the ``Proposal``, runs
    the gate (halt switch included), and mints a token when the gate passes, the
    run is live (``env.mint_tokens``) and ``ARC_GATE_SECRET`` is set. It persists
    ``proposals`` and ``gate_decisions`` rows plus a ``proposal`` context entry
    that also carries the re-priced structure's ``exit_model``
    (:class:`arc.exits.ExitModelResult`, for the E6.1a card).
    It is idempotent per (day, ticker).

Nothing here submits orders. Order submission is ``arc.execution.submit`` (E6).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import math
import sqlite3
import time
from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import BaseModel, ValidationError

from arc.context.kinds import (
    NOTE_SECTION_MAX,
    Evidence,
    ExitCasePayload,
    ExitWatchlistPayload,
    NotePayload,
    NoteSection,
    NoteTopic,
    ProposalPayload,
    RegimePayload,
    RiskExitReviewPayload,
    RiskReviewPayload,
    ShortlistPayload,
    StructuresPayload,
)
from arc.context.store import ContextStore
from arc.control.effective import cost_model as cost_config
from arc.control.effective import exit_config, ranking_config
from arc.exits import ExitSummary, model_exits, realized_vol_forecast
from arc.ingest.llm import ScalpLLMError
from arc.ingest.scalp import extract_json_object
from arc.iv.store import safe_store as safe_iv_store
from arc.journal.models import LegQuote, MarketContext, PersonaCallMeta
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage, gate_reason
from arc.journal.store import JournalStore, Recorder
from arc.models import (
    LegIntent,
    Proposal,
    QuantMetrics,
    Sizing,
    Stance,
    Structure,
    StructureKind,
)
from arc.personas.builders import (
    build_quant_exit_prompt,
    build_quant_prompt,
    build_quant_revise_prompt,
    build_research_prompt,
    build_risk_exit_prompt,
    build_risk_open_prompt,
    build_risk_prompt,
    category_specs_input,
    quant_exit_input_from_context,
    quant_input_from_context,
    quant_revise_input_from_context,
    research_input_from_context,
    risk_exit_input_from_context,
    risk_input_from_context,
    ticker_facts_digest,
)
from arc.personas.entry_window import entry_terms, mentions_dte
from arc.personas.schemas import (
    ExitWatchItem,
    QuantExitOutput,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantReviseOutput,
    QuantSkip,
    QuantStructureOut,
    ResearchExclusion,
    ResearchExitOutput,
    ResearchOutput,
    ResearchRankedItem,
    ResearchThesisCheck,
    RiskAssessment,
    RiskExitOutput,
    RiskExitVerdict,
    RiskOpenAssessment,
    RiskOpenOutput,
    RiskOutput,
)
from arc.pipeline.analytics import build_analytics
from arc.pipeline.budget import BudgetView, budget_notice, read_budget
from arc.pipeline.dedupe import (
    DedupeConfig,
    IdeaFingerprint,
    RecentIdea,
    check_idea,
    fingerprint,
    next_admissible,
    recent_ideas,
)
from arc.pipeline.market import (
    PortfolioError,
    account_baseline,
    account_snapshot,
    build_portfolio,
    limit_price,
    live_gate_status,
    live_size_cap,
    market_snapshot,
    next_earnings,
    price_structure,
    proposal_betas,
)
from arc.pipeline.market_guard import MarketGuard, market_guard
from arc.pipeline.portfolio_context import (
    PortfolioContext,
    build_portfolio_context,
    render_exit_block,
    render_portfolio_context,
)
from arc.pipeline.research_pool import (
    EXIT_BLOCK_RESERVE_CHARS,
    POOL_BUDGET_CUT,
    POOL_MAX_LINES,
    IdeaPool,
    build_idea_pool,
    cut_pool,
)
from arc.pipeline.store import PersonaCallRepo
from arc.routines.handlers import JobResult
from arc.routines.loop import LoopInputs, LoopState, pnl_bucket
from arc.routines.runs import RoutineRunRepo
from arc.scanner.rank import live_net_ev_check
from arc.sizing import apply_live_cap, size_contracts
from arc.slack.blocks import esc
from arc.slack.digests import quant_card, research_card, risk_card
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import pandas as pd

    from arc.backtest.costs import CostModel
    from arc.broker.base import AccountInfo, BrokerPosition
    from arc.config import ArcSettings
    from arc.context.store import ContextEntry, ContextSnapshot
    from arc.exits import ExitConfig, ExitModelResult
    from arc.gate.band import PriceBand
    from arc.gate.inputs import Portfolio
    from arc.personas.schemas import PoolItem
    from arc.pipeline.env import PipelineEnv
    from arc.pipeline.market import PricedStructure
    from arc.positions.evaluate import PositionReview
    from arc.positions.exit_case import ExitCase
    from arc.routines.config import ResearchDiversificationSettings
    from arc.routines.handlers import Handler, JobContext
    from arc.scanner import ScanCandidate

log = structlog.get_logger(__name__)

__all__ = [
    "PersonaError",
    "build_prompt",
    "research",
    "research_step",
    "pipeline_handlers",
    "quant_open",
    "quant_open_step",
    "quant_propose",
    "quant_propose_step",
    "quant_revise",
    "quant_revise_step",
    "quant_exit_step",
    "risk_exit",
    "risk_exit_step",
    "risk_open",
    "risk_open_step",
]

SESSION_SUBJECT = "session"
STRUCTURE_TYPES = frozenset({"vertical_spread", "iron_condor", "long_call", "long_put"})
RESEARCH_READS = [
    "candidate",
    "regime",
    "channel_brief",
    "note",
    "vol_term",
    "macro_calendar",
    "position_review",  # E5.9: fresh E6.4 reviews feed the portfolio context
    "story",  # E4.7 (D47): per-category freshness lines (counts + headlines, by code)
    # E4.8a (D46): Finnhub per-ticker facts; rendered only with personas.finnhub_context on
    "earnings_history",
    "insider_activity",
    "analyst_recs",
    "fundamentals",
    # E13.8 (D56): the Scout's read (compact prompt); its ticker calls set the pool's
    # stance agreement
    "scout_read",
    # E13.17 (D56): ex-dividend dates for the exit-watch facts
    "ex_dividend",
    # E14.6 (D60): Stocktwits bull/bear per ticker; rendered only with
    # personas.retail_sentiment_context on (one fact per pool line)
    "retail_sentiment",
]  # == routines.yaml research.reads (D30 adds the options-data kinds)
# E5.9 drop reasons (Research stage; deterministic). Values == ReasonCode values.
DROP_CONCENTRATION = ReasonCode.DROP_CONCENTRATION.value
DROP_AT_CAP = ReasonCode.DROP_AT_CAP.value
DROP_DEDUPE = ReasonCode.DEDUPE_EXECUTED.value

# Drop reasons (stable keys; stored in persona_calls.dropped and step metrics).
DROP_NOT_CANDIDATE = "not_a_candidate"
DROP_DUPLICATE = "duplicate"
DROP_BAD_FIELD = "invalid_field"
DROP_NOT_IN_MENU = "not_in_menu"
DROP_NOT_SHORTLISTED = "not_shortlisted"
DROP_UNKNOWN_STRUCTURE = "unknown_structure"
# E5.7 funnel outcomes (card keys; journal codes in arc.journal.reasons)
FUNNEL_EXCLUDED = "excluded"  # Research excluded it, with a reason
FUNNEL_NOT_RANKED = "not_picked"  # neither ranked nor excluded (no reason given)
FUNNEL_OVER_BUDGET = "over_budget"  # ranked beyond pipeline_max_shortlist
FUNNEL_SKIPPED = "skipped"  # Quant skipped it, with a reason
FUNNEL_NOT_STRUCTURED = "not_structured"  # no structure and no reason from Quant


class PersonaError(RuntimeError):
    """A persona call failed (transport or unparseable reply). The step fails and the
    chain stops. ``arc routines run research --chain`` resumes from here."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _today(ctx: JobContext) -> _dt.date:
    return ctx.now.astimezone(ET).date()


def _stance(value: object) -> Stance | None:
    try:
        return Stance(str(value).strip().lower())
    except ValueError:
        return None


def _note(
    ctx: JobContext,
    subject: str,
    *,
    persona: str,
    topic: NoteTopic,
    title: str,
    about: list[str],
    sections: Sequence[tuple[str, str]] = (),
    body: str = "",
    facts: Mapping[str, str | float | int | bool] | None = None,
    stance: Stance | None = None,
    confidence: float | None = None,
    evidence: list[Evidence] | None = None,
) -> str | None:
    """Write a D27 ``note`` for persona narrative that would otherwise be discarded.

    E13.13 (note v5): *sections* are ``(label, text)`` pairs (``Thesis``, ``Regime``,
    ``Evidence``, ...); empty texts are dropped and each text is clipped to 1500
    chars at write time. *body* is the legacy free-text form (used only when no
    section has text). Truncates to the model limits. An invalid note is logged
    (``pipeline.note_invalid``) and skipped: a note never fails its step.
    """
    secs = [
        NoteSection(label=label, text=text.strip()[:NOTE_SECTION_MAX])  # type: ignore[arg-type]
        for label, text in sections
        if text and text.strip()
    ]
    body = body.strip()
    if not secs and not body:
        return None
    try:
        payload = NotePayload(
            persona=persona,  # type: ignore[arg-type]
            topic=topic,
            title=(title.strip() or topic.value)[:120],
            body=None if secs else body[:4000],
            sections=secs[:8],
            facts=dict(facts or {}),
            stance=stance,
            confidence=confidence,
            about=about,
            evidence=(evidence or [])[:20],
        )
    except ValidationError as exc:
        log.warning("pipeline.note_invalid", persona=persona, topic=topic, error=str(exc))
        return None
    return ctx.write("note", subject, payload).id


def _schema_block(model: type[BaseModel]) -> str:
    return f"\n## JSON Schema (authoritative)\n{json.dumps(model.model_json_schema())}\n"


def _with_constraints(prompt: str, rules: list[str], schema: type[BaseModel]) -> str:
    body = "\n".join(f"- {r}" for r in rules)
    return (
        f"{prompt}\n## Hard constraints (enforced by the pipeline; violations are dropped)\n"
        f"{body}\n- Respond with ONLY the JSON object: no prose, no code fences.\n"
        f"{_schema_block(schema)}"
    )


# persona → (context adapter, prompt builder, reply schema)
PROMPT_BUILDERS: dict[str, tuple[Callable[..., Any], Callable[..., str], type[BaseModel]]] = {
    "research": (research_input_from_context, build_research_prompt, ResearchOutput),
    "quant": (quant_input_from_context, build_quant_prompt, QuantOutput),
    "risk": (risk_input_from_context, build_risk_prompt, RiskOutput),
    # E13.9: Risk with verdicts; Quant's one revision round
    "risk_open": (risk_input_from_context, build_risk_open_prompt, RiskOpenOutput),
    "quant_revise": (quant_revise_input_from_context, build_quant_revise_prompt, QuantReviseOutput),
    # E13.17 (D56): Quant's hold/close judgement on code-built exit cases
    "quant_exit": (quant_exit_input_from_context, build_quant_exit_prompt, QuantExitOutput),
    # E13.18 (D56): Risk's close | hold verdict on Quant's exit cases
    "risk_exit": (risk_exit_input_from_context, build_risk_exit_prompt, RiskExitOutput),
}

# D56: category keys only a D49-era recorded ``categories`` input carries.
D49_ONLY_CATEGORIES = frozenset({"macro_data", "options_data"})

# prompt key → the persona whose LLM answers it (config/llm_routing.yaml)
LLM_PERSONA = {
    "risk_open": "risk",
    "quant_revise": "quant",
    "quant_exit": "quant",
    "risk_exit": "risk",
}


# E3.4a: personas whose prompt states the configured entry window + delta bands.
ENTRY_TERMS_PERSONAS = frozenset({"research", "quant", "risk", "risk_open", "quant_revise"})


def _replay_flags(persona: str, kwargs: dict[str, Any]) -> None:
    """Mark a recorded Research input from an older category generation (in place)."""
    if persona != "research":
        return
    if "categories" not in kwargs:
        # Recorded before D49 (no categories input): rebuild the D47 five-category
        # block, so `arc journal replay` still matches the recorded sha.
        kwargs["d47_replay"] = True
    elif set(kwargs["categories"] or {}) & D49_ONLY_CATEGORIES:
        # D56: recorded under the D49 six (macro_data / options_data): rebuild that block.
        kwargs["d49_replay"] = True


def build_prompt(
    persona: str,
    snapshot: ContextSnapshot,
    inputs: Mapping[str, Any],
    *,
    settings: ArcSettings | None = None,
) -> str:
    """The exact prompt a persona step sends, from its snapshot plus recorded inputs.

    ``inputs`` holds everything that is not in the context snapshot (portfolio,
    chains, scan date, the hard-constraint lines, the E3.4a ``entry_terms``). The
    steps call this, and so does ``arc journal replay``, so a replay rebuilds the
    prompt through the same code path and its sha256 must match
    ``persona_calls.prompt_sha256``. Inputs recorded before E3.4a carry no
    ``entry_terms``; pass *settings* to rebuild them under today's configured window.
    """
    from_context, builder, schema = PROMPT_BUILDERS[persona]
    kwargs = {k: v for k, v in inputs.items() if k != "rules"}
    _replay_flags(persona, kwargs)
    if persona == "research" and kwargs.get("exit_block"):
        # E13.17: the exit path's reply schema adds `exit_watchlist` (recorded input).
        schema = ResearchExitOutput
    if (
        settings is not None
        and persona in ENTRY_TERMS_PERSONAS
        and kwargs.get("entry_terms") is None
    ):
        kwargs["entry_terms"] = entry_terms(settings).model_dump(mode="json")
    return _with_constraints(
        builder(from_context(snapshot, **kwargs)), list(inputs["rules"]), schema
    )


class _Reply:
    def __init__(
        self,
        model: str,
        text: str,
        prompt: str,
        parsed: Any,
        meta: PersonaCallMeta,
    ) -> None:
        self.model = model
        self.text = text
        self.prompt = prompt
        self.parsed = parsed
        self.meta = meta


def _ask[M: BaseModel](
    ctx: JobContext,
    env: PipelineEnv,
    persona: str,
    snapshot: ContextSnapshot,
    inputs: dict[str, Any],
    schema: type[M],
) -> tuple[_Reply, M]:
    """One persona LLM call. Failures are recorded in ``persona_calls`` and raised."""
    prompt = build_prompt(persona, snapshot, inputs)
    repo = PersonaCallRepo(ctx.conn)
    llm = env.llm(LLM_PERSONA.get(persona, persona))
    started = time.monotonic()
    try:
        reply = llm.complete(prompt)
    except ScalpLLMError as exc:
        repo.insert(
            run_id=ctx.run_id,
            persona=persona,
            model=getattr(llm, "model", "unknown"),
            snapshot_id=snapshot.id,
            prompt=prompt,
            raw_response=None,
            status="llm_error",
            error=str(exc),
            at=ctx.now,
            meta=PersonaCallMeta(
                prompt_text=prompt,
                prompt_inputs=inputs,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
        msg = f"{persona} LLM call failed: {exc}"
        raise PersonaError(msg) from exc
    meta = PersonaCallMeta(
        prompt_text=prompt,
        prompt_inputs=inputs,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        cost_usd=reply.cost_usd,
    )
    try:
        parsed = schema.model_validate(extract_json_object(reply.text))
    except (ValueError, ValidationError) as exc:
        repo.insert(
            run_id=ctx.run_id,
            persona=persona,
            model=reply.model,
            snapshot_id=snapshot.id,
            prompt=prompt,
            raw_response=reply.text,
            status="parse_error",
            error=str(exc)[:2000],
            at=ctx.now,
            meta=meta,
        )
        msg = f"{persona} reply does not match {schema.__name__}: {str(exc)[:300]}"
        raise PersonaError(msg) from exc
    return _Reply(reply.model, reply.text, prompt, parsed, meta), parsed


def _record_ok(
    ctx: JobContext, persona: str, reply: _Reply, snapshot_id: str, dropped: Counter[str]
) -> str:
    """Record the successful call **without committing**: the step's context write
    commits it together with its decision records (one transaction)."""
    return PersonaCallRepo(ctx.conn).insert(
        run_id=ctx.run_id,
        persona=persona,
        model=reply.model,
        snapshot_id=snapshot_id,
        prompt=reply.prompt,
        raw_response=reply.text,
        status="ok",
        dropped=dict(dropped),
        at=ctx.now,
        meta=reply.meta,
        commit=False,
    )


def _journal(ctx: JobContext, snapshot_id: str | None) -> Recorder:
    return Recorder(
        ctx.conn,
        at=ctx.now,
        chain_run_id=ctx.chain_run_id,
        run_id=ctx.run_id,
        inputs_snapshot_id=snapshot_id,
    )


def _latest[M: BaseModel](snapshot: ContextSnapshot, kind: str, model: type[M]) -> M | None:
    entry = snapshot.latest(kind, SESSION_SUBJECT)
    return model.model_validate(entry.payload) if entry else None


def _legs_key(legs: list[QuantLeg] | list[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    pairs = [(leg.occ_symbol, leg.side) if isinstance(leg, QuantLeg) else leg for leg in legs]
    return frozenset((parse_occ(sym).format(), side.lower()) for sym, side in pairs)


def _source(env: PipelineEnv) -> str:
    """Where a step's market/broker data came from (D27 run manifest)."""
    return "fixture" if env.offline else "alpaca"


def _account_inputs(ctx: JobContext, env: PipelineEnv) -> tuple[AccountInfo, list[BrokerPosition]]:
    """Fetch the account and positions, recording both on the run manifest."""
    info = env.account()
    positions = env.positions()
    ctx.record_input("account", _source(env), info, as_of=ctx.now)
    ctx.record_input("positions", _source(env), positions, as_of=ctx.now, count=len(positions))
    return info, positions


def _portfolio_summary(
    ctx: JobContext, env: PipelineEnv, settings: ArcSettings
) -> tuple[str, Decimal]:
    info, positions = _account_inputs(ctx, env)
    opts = [p for p in positions if p.asset_class == "us_option"]
    roots = sorted({parse_occ(p.symbol).root for p in opts})
    text = (
        f"Equity ${info.equity:,.2f}. {len(roots)} open option position(s)"
        + (f" on {', '.join(roots)}" if roots else "")
        + f". Max {settings.max_open_positions} positions, {settings.max_alloc_pct:.0%} of "
        "equity max loss per underlying."
    )
    return text, info.equity


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def regime_kwargs(settings: ArcSettings) -> dict[str, object]:
    """``estimate_regime`` keyword arguments from the effective settings (D77).

    v1 (rollback) gets no extra kwargs, so its output stays byte-identical to the
    pre-v2 code; v2 gets every ``regime_*`` knob.
    """
    if settings.regime_model == "v1":
        return {"model": "v1"}
    return {
        "model": "v2",
        "trend_z": settings.regime_trend_z,
        "vol_scale_window": settings.regime_vol_scale_window,
        "vol_rank_window": settings.regime_vol_rank_window,
        "fit_window": settings.regime_fit_window,
        "alpha": settings.regime_alpha,
    }


def regime_history_days(settings: ArcSettings) -> int:
    """Calendar days of daily bars the regime step fetches (one call per ticker).

    v2 needs ``max(vol_scale_window, 20) + fit_window + 1`` sessions for a full fit
    window (and ``vol_rank_window + 20`` for a full vol percentile). Sessions -> days
    at 1.5 (365/252 plus holidays), never less than the 400 days v1 always used.
    """
    if settings.regime_model == "v1":
        return _REGIME_V1_DAYS
    sessions = max(
        max(settings.regime_vol_scale_window, 20) + settings.regime_fit_window + 1,
        settings.regime_vol_rank_window + 21,
    )
    return max(_REGIME_V1_DAYS, math.ceil(sessions * 1.5))


_REGIME_V1_DAYS = 400


def _regime_entries(ctx: JobContext, env: PipelineEnv, tickers: list[str]) -> list[str]:
    """Write today's regime/vol features for *tickers* lacking one; return tickers written.

    E4.12 (D55): IV comes from ``iv_daily`` (our series: ``alpaca_cm30`` then
    ``alpaca_backfill``) plus today's 30-DTE IV from the live chain (a stored row for
    today wins). Rank/percentile need ``iv_min_obs_rank`` observations; under that, a
    fresh Option Strategist percentile is shown as ``iv_percentile_ext`` (labelled,
    context only, never a gate input).
    """
    import pandas as pd

    from arc.features.snapshot import build_snapshot_from_bars
    from arc.iv.store import safe_store

    settings = ctx.settings
    today = _today(ctx)
    store = safe_store(ctx.conn)
    written: list[str] = []
    rkw = regime_kwargs(settings)
    days = regime_history_days(settings)
    todo = [
        t
        for t in tickers
        if (have := ctx.snapshot.latest("regime", t)) is None
        or have.payload.get("as_of") != today.isoformat()
    ]
    refs = _TechRefs(ctx, env, today, days, todo)
    for t in todo:
        try:
            bars = refs.bars(t)
            series = store.series(t, until=today) if store is not None else {}
            current = None if today in series else _live_iv30(env, t, today, settings)
            sector_etf = refs.sector_etf(t)
            snap = build_snapshot_from_bars(
                t,
                bars,
                today,
                iv_history=pd.Series(series, dtype=float) if series else None,
                current_iv=current,
                regime_kwargs=rkw,
                min_iv_obs=settings.iv_min_obs_rank,
                benchmark=None if t == refs.benchmark else refs.closes(refs.benchmark),
                sector=refs.closes(sector_etf) if sector_etf else None,
                sector_etf=sector_etf,
            )
            if store is not None and snap.vol.iv_percentile is None:
                ext = store.latest_external(t, until=today)
                if (
                    ext is not None
                    and ext.ext_percentile is not None
                    and (today - ext.day).days <= settings.iv_ext_max_age_days
                ):
                    vol = snap.vol.model_copy(
                        update={
                            "iv_percentile_ext": ext.ext_percentile,
                            "iv_percentile_ext_source": f"optionstrategist@{ext.day}",
                        }
                    )
                    snap = snap.model_copy(update={"vol": vol})
        except Exception as exc:  # noqa: BLE001 - features are context, not a gate input
            log.warning("pipeline.regime_failed", ticker=t, error=str(exc))
            continue
        ctx.write("regime", t, RegimePayload.model_validate(snap.model_dump()))
        written.append(t)
    return written


TECH_BENCHMARK = "SPY"  # E16.2: the rs_spy_* reference (the field names fix it)


class _TechRefs:
    """E16.2 (D76): one regime step's daily bars, fetched at most once per symbol.

    The relative-strength references (SPY, and each mapped sector ETF from
    ``technicals.sector_etf``) are fetched once per run and reused
    for every ticker; a ticker that is itself a reference reuses that fetch. A
    reference that fails to load is ``None`` (its ``rs_*`` fields stay ``None``).
    """

    def __init__(
        self,
        ctx: JobContext,
        env: PipelineEnv,
        today: _dt.date,
        days: int,
        tickers: Sequence[str],
    ) -> None:
        from arc.pipeline.portfolio_context import load_sectors

        cfg = ctx.routines.technicals
        self._ctx, self._env, self._today, self._days = ctx, env, today, days
        self._bars: dict[str, list[Any]] = {}
        self._closes: dict[str, pd.Series | None] = {}
        self.benchmark = TECH_BENCHMARK
        self._etf_by_sector = dict(cfg.sector_etf)
        self._sectors = load_sectors() if tickers and self._etf_by_sector else {}

    def bars(self, ticker: str) -> list[Any]:
        """*ticker*'s daily bars (recorded on the run manifest once); raises on failure."""
        if ticker not in self._bars:
            env, today = self._env, self._today
            bars = env.market.history_bars(ticker, today - _dt.timedelta(days=self._days), today)
            self._ctx.record_input(
                f"bars:{ticker}", _source(env), bars, as_of=self._ctx.now, count=len(bars)
            )
            self._bars[ticker] = bars
        return self._bars[ticker]

    def sector_etf(self, ticker: str) -> str | None:
        """The sector ETF for *ticker* (``None``: no sector, unmapped, or the ETF itself)."""
        etf = self._etf_by_sector.get(self._sectors.get(ticker.upper(), ""))
        return None if etf is None or etf == ticker.upper() else etf

    def closes(self, symbol: str) -> pd.Series | None:
        """Daily closes of a reference symbol (``None`` when its bars fail to load)."""
        from arc.features._series import closes_from_bars

        if symbol not in self._closes:
            try:
                self._closes[symbol] = closes_from_bars(self.bars(symbol))
            except Exception as exc:  # noqa: BLE001 - a missing reference only drops rs_*
                log.warning("pipeline.technicals_ref_failed", symbol=symbol, error=str(exc)[:200])
                self._closes[symbol] = None
        return self._closes[symbol]


def _live_iv30(
    env: PipelineEnv, ticker: str, today: _dt.date, settings: ArcSettings
) -> float | None:
    """Today's 30-DTE ATM IV from the live chain (``None`` on any data failure)."""
    from arc.iv.record import iv30_from_chain

    try:
        row = iv30_from_chain(
            env.market, ticker, today, max_spread_pct=settings.spot_max_spread_pct
        )
    except Exception as exc:  # noqa: BLE001 - IV is context; a missing chain is a None
        log.info("pipeline.regime_iv_unavailable", ticker=ticker, error=str(exc)[:200])
        return None
    return row.iv30


def _filter_shortlist(
    out: ResearchOutput, candidates: Mapping[str, Stance]
) -> tuple[list[ResearchRankedItem], Counter[str], list[tuple[ResearchRankedItem, str]]]:
    """Kept items, drop counts, and each dropped item with its reason.

    E5.7: no count cap here. Research ranks every candidate it would trade;
    ``pipeline_max_shortlist`` is the Quant/Risk budget, applied by :func:`quant`.
    """
    dropped: Counter[str] = Counter()
    rejected: list[tuple[ResearchRankedItem, str]] = []
    kept: list[ResearchRankedItem] = []
    seen: set[str] = set()

    def drop(item: ResearchRankedItem, reason: str) -> None:
        dropped[reason] += 1
        rejected.append((item, reason))

    for item in sorted(out.shortlist, key=lambda i: (i.rank, -i.confidence)):
        t = item.ticker.strip().upper()
        if t not in candidates:
            drop(item, DROP_NOT_CANDIDATE)
            continue
        if t in seen:
            drop(item, DROP_DUPLICATE)
            continue
        stype = item.suggested_structure_type.strip().lower()
        stance = item.stance.strip().lower()
        if stype not in STRUCTURE_TYPES or stance not in {s.value for s in Stance}:
            drop(item, DROP_BAD_FIELD)
            continue
        seen.add(t)
        kept.append(
            item.model_copy(
                update={
                    "ticker": t,
                    "rank": len(kept) + 1,
                    "stance": stance,
                    "suggested_structure_type": stype,
                }
            )
        )
    return kept, dropped, rejected


def _filter_excluded(
    out: ResearchOutput, candidates: Mapping[str, Stance], ranked: set[str]
) -> list[ResearchExclusion]:
    """Research's exclusions that name a real, un-ranked candidate (first wins)."""
    kept: dict[str, ResearchExclusion] = {}
    for ex in out.excluded:
        t = ex.ticker.strip().upper()
        if t in candidates and t not in ranked and t not in kept and ex.reason.strip():
            kept[t] = ResearchExclusion(ticker=t, reason=ex.reason.strip()[:300])
    return list(kept.values())


def _scalp_evidence(snapshot: ContextSnapshot) -> dict[str, str]:
    """ticker -> one display line of the Scalp data behind a pick (digest card only)."""
    out: dict[str, str] = {}
    for e in snapshot.of_kind("candidate"):
        p = e.payload
        when = ""
        if p.get("catalyst_date"):
            try:
                when = f" {_dt.datetime.fromisoformat(str(p['catalyst_date'])):%b %d}"
            except ValueError:
                when = ""
        out[e.subject] = esc(
            f"Scalp {p.get('stance', '?')} · {p.get('catalyst_type', '?')} catalyst{when} · "
            f"{float(p.get('confidence', 0)):.0%} confidence"
        )
    return out


def _profile_rule(settings: ArcSettings) -> str:
    """The account-profile line every persona prompt carries (D25)."""
    prof = settings.profile
    dead = [s for s in ("bullish", "bearish", "neutral") if not prof.strategies_for(s)]
    line = f"The account can only trade what its {prof.summary()}"
    if dead:
        line += f" Stance(s) {', '.join(dead)} have no structure under it: no trade."
    return line


def _research_rules(
    cands: Mapping[str, Stance],
    settings: ArcSettings | None = None,
    budget: BudgetView | None = None,
    portfolio: PortfolioContext | None = None,
    diversification: ResearchDiversificationSettings | None = None,
) -> list[str]:
    """Research constraints (E5.7: rank, don't gatekeep; no count cap in the prompt).

    D32: in the restrictive order-budget tier an advisory line is added; the
    deterministic cap is the lowered Quant/Risk budget (:func:`_shortlist_limit`).
    E13.8 (D56): the rules name the idea pool and Research lists no exclusions;
    E13.17: with open positions they ask for the exit watchlist.
    """
    rules = [
        f"shortlist tickers MUST come from the idea pool above: {', '.join(sorted(cands))}.",
        "Rank every pool ticker you would consider trading, best first (rank 1 = highest "
        "conviction), each with thesis, regime context and the evidence behind it. "
        "Do not cap the list yourself.",
        "Tickers you do not rank need no reason; leave `excluded` empty.",
        "evidence: up to 3 short, grounded facts per pick taken from the inputs above "
        "(e.g. '8-K: buyback $50B, Sep 24', 'IV rank 18'); no invented numbers.",
        "stance: bullish | bearish | neutral. suggested_structure_type: vertical_spread | "
        "iron_condor | long_call | long_put.",
        "An empty shortlist is a valid answer when nothing is worth trading.",
    ]
    if settings is not None:
        rules.append(_profile_rule(settings))
    if budget is not None and budget.tier.restricted:
        b = budget.budget
        rules.append(
            f"order budget tier: {b.tier.value} ({b.used}/{b.limit}); propose at most one "
            "high-conviction idea or none."
        )
    if portfolio is not None and not portfolio.empty and portfolio.aggregates is not None:
        ag = portfolio.aggregates
        if diversification is not None and diversification.is_relaxed:
            drops = (
                "adds_concentration picks on a flagged sector are dropped by the pipeline "
                f"once the book holds {diversification.max_names_per_industry} names in "
                "that industry; a shared industry alone is not a reason to exclude."
            )
        else:
            drops = (
                "adds_concentration picks on a flagged sector/stance/expiry are dropped "
                "by the pipeline."
            )
        rules.append(
            "portfolio_fit is required per pick (diversifies | hedges | adds_concentration | "
            f"neutral); {drops} Names at their per-underlying max-loss cap "
            f"({', '.join(ag.at_cap_underlyings) or 'none'}) cannot be opened."
        )
        ids = ", ".join(p.structure_id for p in portfolio.positions[:12])
        rules.append(  # E13.17: the exit watchlist replaces thesis_checks
            "portfolio_view.verdict: balanced | concentrated | hedge_needed | reduce_risk; "
            f"one exit_watchlist item per open structure_id ({ids}); action hold | "
            "review; thesis_status intact | weakened | broken; evidence <= 4 items of "
            "<= 160 chars each; reason <= 240 chars."
        )
    return rules


def _shortlist_limit(settings: ArcSettings, budget: BudgetView) -> int:
    """Quant/Risk budget: the restrictive tier's lower cap once ``used >= restrict_at`` (D32)."""
    limit = settings.pipeline_max_shortlist
    if budget.tier.restricted:
        limit = min(limit, settings.order_budget_restrictive_research_max_shortlist)
    return limit


def _research_no_opens(
    ctx: JobContext,
    guard: MarketGuard,
    budget: BudgetView,
    notice: str,
    cands: Mapping[str, Stance],
) -> JobResult:
    """E5.9: the market guard blocked new opens; stop the entry chain before the LLM."""
    code = guard.code or ReasonCode.MARKET_UNCLEAR
    why = guard.summary()
    j = _journal(ctx, ctx.snapshot.id)
    j.add(
        JournalPersona.SYSTEM,
        Stage.SHORTLIST,
        SESSION_SUBJECT,
        Choice.NO_TRADE,
        code,
        reason_text=why,
        payload=guard.model_dump(mode="json"),
    )
    payload = ShortlistPayload(
        shortlist=[],
        market_regime=guard.regime or "unknown",
        session_notes=why,
        no_trade_reason="unclear",
        market_guard=guard,
    )
    ctx.write("shortlist", SESSION_SUBJECT, payload)
    log.info("pipeline.market_guard_blocked", reasons=guard.reasons, code=code.value)
    return JobResult(
        summary=f"{why}; empty shortlist ({len(cands)} candidates not ranked)",
        metrics={"shortlist": 0, "market_guard_blocked": 1, **budget.metrics()},
        notice=notice,
        # E13.18: the chain goes on: the guard blocks new opens, never exits.mandatory /
        # the exit review (the open steps see the empty shortlist and return without
        # an LLM call).
        stop_chain=False,
        card=research_card(
            payload,
            candidates=len(cands),
            dropped=[],
            funnel=(),
            budget=0,
            evidence=[],
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


def _portfolio_context(
    ctx: JobContext,
    env: PipelineEnv,
    settings: ArcSettings,
    budget: BudgetView,
    *,
    snap: ContextSnapshot | None = None,
) -> PortfolioContext:
    """Build, record and store the E5.9 portfolio context for this run.

    E13.17: each position carries its :class:`PositionFacts` (read from *snap*, the
    refreshed Research snapshot), and the reviews computed here are written as
    ``position_review`` (subject = structure id) so ``quant.exit`` reads the marks
    Research saw.
    """
    from arc.gate.halt import HaltSwitch
    from arc.pipeline.market import PortfolioError, build_portfolio
    from arc.store.repos import HaltRepo

    info, positions = _account_inputs(ctx, env)
    portfolio = None
    try:
        portfolio = build_portfolio(
            ctx.conn,
            positions,
            env.market,
            now=ctx.now,
            wash_sale_days=settings.wash_sale_days,
            r=settings.scanner_risk_free_rate,
            spot_max_spread_pct=settings.spot_max_spread_pct,
        )
    except PortfolioError as exc:  # Greeks fall back to as-opened; propose re-checks
        log.warning("pipeline.portfolio_context_unvalued", error=str(exc))
    computed: dict[str, PositionReview] = {}
    pctx = build_portfolio_context(
        ctx.conn,
        env,
        settings,
        info=info,
        now=ctx.now,
        halted=HaltSwitch(HaltRepo(ctx.conn)).opens_blocked(),  # E11.4: opens stop either way
        budget_tier=budget.tier.value,
        portfolio=portfolio,
        snapshot=ctx.snapshot,
        diversification=ctx.routines.director_diversification,
        facts=True,
        facts_snapshot=snap,
        reviews_out=computed,
    )
    for sid in sorted(computed):
        ctx.write("position_review", sid, computed[sid])
    ctx.write("portfolio_context", SESSION_SUBJECT, pctx)
    return pctx


def _held_and_recent(
    cands: Mapping[str, Stance],
    priors: list[RecentIdea],
    pctx: PortfolioContext,
    now: _dt.datetime,
    cfg: DedupeConfig,
) -> tuple[dict[str, str], list[str]]:
    """Research-stage dedupe by ``ticker|stance``.

    Returns ``held`` (candidate ticker -> stance held by an OPEN structure; those
    picks are dropped outright) and the prompt lines for every recently suggested
    idea on a candidate, so Research sees why a name is off the table.
    """
    held: dict[str, str] = {}
    for p in pctx.positions:
        if p.ticker in cands:
            held[p.ticker] = p.stance.value
    lines: list[str] = []
    seen: set[str] = set()
    for r in priors:
        fp = IdeaFingerprint.parse(r.fingerprint)
        if fp.ticker not in cands or fp.prefix() in seen:
            continue
        seen.add(fp.prefix())
        until = next_admissible(now, r, cfg)
        when = "held (open position)" if r.open_structure else f"{r.kind.value} {r.at:%b %d}"
        lines.append(
            f"- {fp.ticker} {fp.stance.value} {fp.structure_type}: {when}"
            + (f"; admissible again {until:%b %d}" if until else "")
            + (
                f" (or if spot moves {cfg.reprice_move_pct:.0%} from {r.spot} / regime changes)"
                if not r.open_structure and r.spot is not None
                else ""
            )
        )
    return held, lines


def _portfolio_filter(
    kept: list[ResearchRankedItem],
    pctx: PortfolioContext,
    held: Mapping[str, str],
    settings: ArcSettings,
    diversification: ResearchDiversificationSettings | None = None,
    *,
    industries: Mapping[str, str] | None = None,
) -> tuple[list[ResearchRankedItem], Counter[str], list[tuple[ResearchRankedItem, str]]]:
    """E5.9 deterministic Research-stage drops (portfolio + held ideas).

    * ``dedupe``: an open structure already holds this ticker in this stance.
    * ``at_cap``: the underlying is at its per-underlying max-loss cap.
    * ``adds_concentration``: Research says the pick adds concentration and the
      dimension it lands on (sector / stance / expiry) is already flagged.

    E12.5 (D51) ``relaxed`` diversification changes only the last rule: the pick is
    dropped when its ticker is already held (the D33 dedupe), or when its sector is
    flagged **and** the book plus the picks kept so far already hold
    ``max_names_per_industry`` names in its industry (``config/sectors.yaml``
    ``industries:``; an unmapped name counts by its sector). Stance skew alone no
    longer drops. Re-ranks the survivors 1..n.
    """
    from arc.pipeline.portfolio_context import load_industries, load_sectors

    relaxed = diversification is not None and diversification.is_relaxed
    max_names = diversification.max_names_per_industry if diversification is not None else 0
    dropped: Counter[str] = Counter()
    rejected: list[tuple[ResearchRankedItem, str]] = []
    out: list[ResearchRankedItem] = []
    ag = pctx.aggregates
    sectors = {p.ticker: p.sector for p in pctx.positions}
    sector_map: dict[str, str] | None = None

    def sector_of(ticker: str) -> str:
        nonlocal sector_map
        if ticker in sectors:
            return sectors[ticker]
        if sector_map is None:
            sector_map = load_sectors()
        return sector_map.get(ticker, "unknown")

    names_by_industry: dict[str, set[str]] = {}
    if relaxed:
        imap = industries if industries is not None else load_industries()

        def industry_of(ticker: str) -> str:
            return imap.get(ticker) or f"sector:{sector_of(ticker)}"

        for p in pctx.positions:
            names_by_industry.setdefault(industry_of(p.ticker), set()).add(p.ticker)

    for item in kept:
        if held.get(item.ticker) == item.stance:
            dropped[DROP_DEDUPE] += 1
            rejected.append((item, DROP_DEDUPE))
            continue
        if ag is not None and item.ticker in ag.at_cap_underlyings:
            dropped[DROP_AT_CAP] += 1
            rejected.append((item, DROP_AT_CAP))
            continue
        if ag is not None and item.portfolio_fit == "adds_concentration":
            sector = sector_of(item.ticker)
            if relaxed:
                names = names_by_industry.get(industry_of(item.ticker), set())
                flagged = item.ticker in ag.by_underlying or (
                    sector in ag.flagged_sectors and len(names - {item.ticker}) >= max_names
                )
            else:
                flagged = (
                    sector in ag.flagged_sectors
                    or item.stance in ag.flagged_stances
                    or item.ticker in ag.by_underlying
                )
            if flagged:
                dropped[DROP_CONCENTRATION] += 1
                rejected.append((item, DROP_CONCENTRATION))
                continue
        if relaxed:
            names_by_industry.setdefault(industry_of(item.ticker), set()).add(item.ticker)
        out.append(item.model_copy(update={"rank": len(out) + 1}))
    return out, dropped, rejected


#: Scanner strategies that collect premium: a stance the profile maps to any of them is
#: not "long premium only", so the anti-chase filter leaves it alone (E16.3).
_CREDIT_STRATEGY_NAMES = frozenset({"bull_put", "bear_call", "iron_condor"})


@dataclasses.dataclass
class _AntiChase:
    """E16.3 (D76/D78): what the anti-chase filter did in one Research run."""

    kept: list[ResearchRankedItem]
    dropped: Counter[str] = dataclasses.field(default_factory=Counter)
    rejected: list[tuple[ResearchRankedItem, str]] = dataclasses.field(default_factory=list)
    # ticker -> the verdict (every directional long-premium idea checked)
    verdicts: dict[str, Any] = dataclasses.field(default_factory=dict)
    # tickers kept because their technicals were missing / the VWAP fetch failed
    tech_missing: list[ResearchRankedItem] = dataclasses.field(default_factory=list)
    vwap_missing: list[tuple[ResearchRankedItem, str]] = dataclasses.field(default_factory=list)


def _long_premium_only(stance: str, settings: ArcSettings) -> bool:
    """The profile maps *stance* to debit structures only (long calls/puts, debit verticals)."""
    names = settings.profile.strategies_for(stance)
    return bool(names) and not (set(names) & _CREDIT_STRATEGY_NAMES)


def _session_vwap_stretch(
    ctx: JobContext,
    env: PipelineEnv,
    ticker: str,
    atr14: float | None,
    take: Callable[[], object] | None,
) -> tuple[float | None, str]:
    """``((spot - session VWAP) / ATR14, why-missing)`` from today's 5-min bars.

    Spot is the last bar's close (no extra quote call). One ``history_bars`` request,
    taken from the shared ``alpaca_data:calls`` budget (*take*). Any failure returns
    ``(None, reason)``: the caller skips the VWAP part and keeps the daily rule.
    """
    from arc.features.technicals import session_vwap, vwap_stretch_atr

    today = _today(ctx)
    try:
        if take is not None:
            take()
        bars = env.market.history_bars(ticker, today, today, "5Min")
        ctx.record_input(f"bars5m:{ticker}", _source(env), bars, as_of=ctx.now, count=len(bars))
    except Exception as exc:  # noqa: BLE001 - a missing intraday read only skips VWAP
        return None, f"5-min bars failed: {exc}"[:200]
    open_at = _dt.time(9, 30)
    vwap = session_vwap(bars, start=open_at, end=ctx.now)
    spot = None
    for b in bars:
        if b.timestamp.astimezone(ET) < ctx.now:
            spot = float(b.close)
    out = vwap_stretch_atr(spot, vwap, atr14)
    if out is None:
        return None, "no session bars yet" if vwap is None else "no ATR14"
    return out, ""


def _alpaca_take(ctx: JobContext, env: PipelineEnv) -> Callable[[], object] | None:
    """The shared Alpaca data budget (``routine_state[alpaca_data:calls]``); None offline."""
    if env.offline:
        return None
    from arc.ingest.finnhub import DbRateLimiter
    from arc.iv.alpaca_history import RATE_STATE_KEY

    return DbRateLimiter(
        ctx.conn, calls_per_minute=ctx.settings.alpaca_data_calls_per_minute, key=RATE_STATE_KEY
    ).acquire


def _anti_chase_filter(
    ctx: JobContext,
    env: PipelineEnv,
    snap: ContextSnapshot,
    kept: list[ResearchRankedItem],
) -> _AntiChase:
    """E16.3 (D76/D78): drop directional long-premium ideas whose move is stretched.

    Runs after Research ranks (and after the E5.9 portfolio drops), before Quant.
    Only a bullish/bearish idea the account profile can structure as long premium
    only is checked (a credit-capable stance and neutral ideas pass untouched). The
    rule is :func:`arc.features.technicals.is_stretched` on the ``regime`` entry's
    technicals. Missing technicals keep the idea (journaled ``technicals_missing``).
    With ``anti_chase.vwap`` on, each checked idea also costs one 5-min bars request
    (shared Alpaca budget); a failed fetch skips only the VWAP part (``vwap_missing``).
    Survivors are re-ranked 1..n. Deterministic: no LLM, never a gate input.
    """
    from arc.features.technicals import TechnicalFeatures, is_stretched

    cfg = ctx.routines.anti_chase
    rule = cfg.rule()
    res = _AntiChase(kept=[])
    take = _alpaca_take(ctx, env) if cfg.vwap else None
    for item in kept:
        stance = item.stance.strip().lower()
        if stance not in ("bullish", "bearish") or not _long_premium_only(stance, ctx.settings):
            res.kept.append(item.model_copy(update={"rank": len(res.kept) + 1}))
            continue
        entry = snap.latest("regime", item.ticker)
        raw = (entry.payload.get("technicals") if entry is not None else None) or None
        tech = TechnicalFeatures.model_validate(raw) if raw is not None else None
        vwap_stretch: float | None = None
        if cfg.vwap:
            vwap_stretch, why = _session_vwap_stretch(
                ctx, env, item.ticker, tech.atr14 if tech is not None else None, take
            )
            if vwap_stretch is None:
                res.vwap_missing.append((item, why))
        verdict = is_stretched(tech, stance, rule, vwap_stretch=vwap_stretch)
        res.verdicts[item.ticker] = verdict
        if verdict.stretched:
            res.dropped[ReasonCode.STRETCHED_ENTRY.value] += 1
            res.rejected.append((item, ReasonCode.STRETCHED_ENTRY.value))
            continue
        if verdict.status == "missing" and tech is None:
            res.tech_missing.append(item)
        res.kept.append(item.model_copy(update={"rank": len(res.kept) + 1}))
    return res


def _journal_anti_chase(j: Recorder, chase: _AntiChase, call_id: str | None) -> None:
    """E16.3: one record per stretched drop (with the numbers), per kept-without-data idea."""
    for item, _ in chase.rejected:
        v = chase.verdicts[item.ticker]
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            item.ticker,
            Choice.REJECTED,
            ReasonCode.STRETCHED_ENTRY,
            reason_text=f"{item.stance}: {v.text()}",
            confidence=item.confidence,
            persona_call_id=call_id,
            payload={"idea": item.model_dump(mode="json"), "anti_chase": v.model_dump(mode="json")},
        )
    for item in chase.tech_missing:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            item.ticker,
            Choice.NOTED,
            ReasonCode.TECHNICALS_MISSING,
            reason_text="no technicals on the regime entry: idea kept (optional filter)",
            persona_call_id=call_id,
        )
    for item, why in chase.vwap_missing:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            item.ticker,
            Choice.NOTED,
            ReasonCode.VWAP_MISSING,
            reason_text=f"VWAP part skipped ({why}); daily rule applied",
            persona_call_id=call_id,
        )


def _no_trade_reason(out: ResearchOutput, kept: list[ResearchRankedItem]) -> str | None:
    """The explicit no-trade outcome (E5.9): only meaningful with an empty shortlist."""
    if kept:
        return None
    r = out.no_trade_reason
    return r if r and r != "none" else "no_fit"


def _portfolio_notes(
    ctx: JobContext,
    payload: ShortlistPayload,
    pctx: PortfolioContext,
    entry_id: str,
    call_id: str,
) -> None:
    """E5.9: Research's portfolio read and thesis checks as note context + journal."""
    if pctx.empty:
        return
    j = _journal(ctx, ctx.snapshot.id)
    if payload.portfolio_view is not None:
        pv = payload.portfolio_view
        _note(
            ctx,
            SESSION_SUBJECT,
            persona="research",
            topic=NoteTopic.PORTFOLIO_VIEW,
            title=f"Portfolio: {pv.verdict}",
            sections=[("Portfolio", pv.notes)],
            facts={"verdict": pv.verdict},
            about=[entry_id],
        )
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            SESSION_SUBJECT,
            Choice.NOTED,
            ReasonCode.PORTFOLIO_VIEW,
            reason_text=f"{pv.verdict}: {pv.notes}".strip(": "),
            persona_call_id=call_id,
            payload=pv.model_dump(mode="json"),
        )
    by_id = {p.structure_id: p for p in pctx.positions}
    for c in payload.thesis_checks:
        pos = by_id[c.structure_id]
        _note(
            ctx,
            c.structure_id,
            persona="research",
            topic=NoteTopic.THESIS_CHECK,
            title=f"{pos.ticker} thesis {c.status}",
            sections=[("Thesis", c.reason)],
            facts={"status": c.status},
            about=[entry_id],
            stance=pos.stance,
        )
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            c.structure_id,
            Choice.NOTED,
            ReasonCode.THESIS_CHECK,
            reason_text=f"{pos.ticker}: {c.status}: {c.reason}".strip(": "),
            persona_call_id=call_id,
            payload=c.model_dump(mode="json"),
        )


def _held_tickers(conn: sqlite3.Connection) -> set[str]:
    """Tickers of open structures (E13.17: IV rank for the exit-watch facts)."""
    rows = conn.execute("SELECT DISTINCT ticker FROM open_structures WHERE status = 'open'")
    return {str(r[0]) for r in rows}


def _exit_watch_rules(ctx: JobContext) -> list[str]:
    """E13.17: the exit-policy lines Research sees with the exit watch (deterministic)."""
    exits = exit_config(ctx.settings)
    return [
        f"Exit policy: {exits.policy_for(None).summary()}",
        "Mandatory exits (stop, DTE exit, expiry) are closed by code; `review` asks "
        "Quant to judge hold or close (rolling is not an option).",
    ]


def _valid_watch_items(out: ResearchOutput, pctx: PortfolioContext) -> list[ExitWatchItem]:
    """E13.17: Research's watch items for open structures, one per structure (first wins).

    The ticker is taken from the open book, never from the reply.
    """
    if not isinstance(out, ResearchExitOutput):
        return []
    by_id = {p.structure_id: p for p in pctx.positions}
    seen: set[str] = set()
    items: list[ExitWatchItem] = []
    for w in out.exit_watchlist:
        pos = by_id.get(w.structure_id)
        if pos is None or w.structure_id in seen:
            continue
        seen.add(w.structure_id)
        items.append(w.model_copy(update={"ticker": pos.ticker}))
    return items


def _watch_thesis_checks(items: Sequence[ExitWatchItem]) -> list[ResearchThesisCheck]:
    """E13.17: the legacy E5.9 thesis checks from watch items (``broken`` -> ``invalidated``)."""
    return [
        ResearchThesisCheck(
            structure_id=w.structure_id,
            status="invalidated" if w.thesis_status == "broken" else w.thesis_status,
            reason=w.reason,
        )
        for w in items
    ]


def _watch_inputs(pctx: PortfolioContext) -> dict[str, int]:
    """Code-counted inputs Research had for the exit watch."""
    facts = [p.facts for p in pctx.positions if p.facts is not None]
    return {
        "stories": sum(f.stories_fresh for f in facts),
        "scout_mentions": sum(1 for f in facts if f.scout_mention is not None),
        "iv_rank_known": sum(1 for f in facts if f.iv_rank is not None),
        "earnings_known": sum(1 for f in facts if f.next_earnings is not None),
        "ex_div_known": sum(1 for f in facts if f.ex_dividend is not None),
    }


def _exit_watchlist(
    ctx: JobContext,
    items: list[ExitWatchItem],
    pctx: PortfolioContext,
    call_id: str,
) -> ExitWatchlistPayload | None:
    """E13.17: store Research's exit watchlist and journal one row per open position.

    Advisory only: nothing here creates a proposal.
    """
    if pctx.empty:
        return None
    have = {w.structure_id for w in items}
    missing = [p.structure_id for p in pctx.positions if p.structure_id not in have]
    payload = ExitWatchlistPayload(
        as_of=ctx.now.isoformat(),
        items=items,
        positions_seen=len(pctx.positions),
        missing=missing,
        inputs=_watch_inputs(pctx),
    )
    ctx.write("exit_watchlist", SESSION_SUBJECT, payload)
    j = _journal(ctx, ctx.snapshot.id)
    for w in items:
        review = w.action == "review"
        _note(
            ctx,
            w.structure_id,
            persona="research",
            topic=NoteTopic.EXIT_WATCH,
            title=f"{w.ticker} exit watch: {w.action} ({w.thesis_status})",
            sections=[
                ("Exits", w.reason),
                ("Evidence", "\n".join(f"- {e}" for e in w.evidence)),
            ],
            facts={"action": w.action, "thesis_status": w.thesis_status},
            about=[],
        )
        j.add(
            JournalPersona.RESEARCH,
            Stage.EXIT,
            w.structure_id,
            Choice.NOTED,
            ReasonCode.EXIT_WATCH_REVIEW if review else ReasonCode.EXIT_WATCH_HOLD,
            reason_text=f"{w.ticker}: {w.thesis_status}: {w.reason}".strip(": "),
            persona_call_id=call_id,
            payload=w.model_dump(mode="json"),
        )
    by_id = {p.structure_id: p for p in pctx.positions}
    for sid in missing:
        j.add(
            JournalPersona.RESEARCH,
            Stage.EXIT,
            sid,
            Choice.NOTED,
            ReasonCode.EXIT_WATCH_MISSING,
            reason_text=f"{by_id[sid].ticker}: no exit watch item from Research; hold",
            persona_call_id=call_id,
        )
    return payload


def _loop_inputs(
    snap: ContextSnapshot,
    pctx: PortfolioContext,
    priors: list[RecentIdea],
    budget: BudgetView,
    *,
    pending_orders: int,
    bucket_pct: float,
    facts_tickers: list[str] | None = None,
    extra_facts: list[str] | None = None,
) -> LoopInputs:
    """The deterministic, rounded inputs the D31 change-aware skip digests.

    E4.8a: with ``personas.finnhub_context`` on, the Finnhub facts in scope enter as
    ``<kind>:<ticker>@<as_of>`` only, so a re-fetch of the same data is no change.
    E14.6: *extra_facts* (the ``retail_sentiment:<ticker>@<as_of>`` keys, flag on only)
    join them, so the 12:30 sentiment refresh counts as a change.
    """
    return LoopInputs(
        facts=sorted([*ticker_facts_digest(snap, facts_tickers or []), *(extra_facts or [])]),
        candidates=sorted(f"{e.id}@{e.schema_version}" for e in snap.of_kind("candidate")),
        regimes=sorted(f"{e.subject}@{e.id}" for e in snap.of_kind("regime")),
        briefs=sorted(f"{e.subject}@{e.id}" for e in snap.of_kind("channel_brief")),
        positions=sorted(f"{p.structure_id}:{p.contracts}" for p in pctx.positions),
        pnl_bucket=pnl_bucket(pctx.account.day_pnl, pctx.account.equity, bucket_pct),
        pending_orders=pending_orders,
        budget_tier=budget.tier.value,
        suppressed=sorted(f"{r.fingerprint}:{r.kind.value}" for r in priors),
    )


def _pending_orders(conn: sqlite3.Connection) -> int:
    """Approval requests still pending plus executions not yet resolved."""
    n = conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'").fetchone()
    m = conn.execute(
        "SELECT COUNT(*) FROM executions WHERE status IN ('working', 'unconfirmed')"
    ).fetchone()
    return int(n[0]) + int(m[0])


def _loop_digest_of(ctx: JobContext) -> str | None:
    for item in ctx.external_inputs:
        if item.name == "loop_inputs":
            return item.digest
    return None


def _loop_no_change(
    ctx: JobContext,
    snap: ContextSnapshot,
    pctx: PortfolioContext,
    priors: list[RecentIdea],
    budget: BudgetView,
    *,
    facts_tickers: list[str] | None = None,
    extra_facts: list[str] | None = None,
) -> JobResult | None:
    """D31 change-aware skip: same inputs as the last loop -> no LLM call.

    Only the loop persona (``routines.loop.job``) skips; a manual or scheduled
    Research outside the loop always runs in full. The digest is recorded as an
    external input of the run (D27), so the manifest shows what was compared.
    """
    loop = ctx.routines.loop
    inputs = _loop_inputs(
        snap,
        pctx,
        priors,
        budget,
        pending_orders=_pending_orders(ctx.conn),
        bucket_pct=loop.pnl_bucket_pct,
        facts_tickers=facts_tickers or [],
        extra_facts=extra_facts,
    )
    new_digest = inputs.digest()
    log.debug("pipeline.loop_inputs", digest=new_digest[:12], **inputs.payload())
    ctx.record_input("loop_inputs", "db", inputs.payload())
    if not ctx.is_loop_run:
        return None
    state = LoopState(ctx.conn)
    if not state.should_skip(new_digest, ctx.now, loop.max_idle):
        # A full run: the digest and ``last_full_run`` are recorded only once the
        # Research's evaluation has completed (see ``_loop_record_full_run``), so a
        # failed LLM call never mutes the next ``max_idle`` of slots as ``no_change``.
        return None
    state.record_digest(new_digest, ctx.now, full_run=False)
    last = state.last_full_run()
    age = f"{int((ctx.now - last).total_seconds() // 60)}m" if last else "?"
    why = f"no_change: inputs unchanged since the last full loop ({age} ago)"
    j = _journal(ctx, snap.id)
    j.add(
        JournalPersona.RESEARCH,
        Stage.SHORTLIST,
        SESSION_SUBJECT,
        Choice.NO_TRADE,
        ReasonCode.LOOP_NO_CHANGE,
        reason_text=why,
        payload={"digest": new_digest, "last_full_run": last.isoformat() if last else None},
    )
    log.info("pipeline.loop_no_change", digest=new_digest[:12], last_full_run=last)
    return JobResult(
        summary=why,
        metrics={
            "shortlist": 0,
            "no_change": True,
            "loop_digest": new_digest,
            "open_positions": len(pctx.positions),
            **budget.metrics(),
        },
    )


def _loop_record_full_run(ctx: JobContext) -> None:
    """D31: a completed Research evaluation is the loop's new ``last_full_run``.

    Called only after the LLM reply was parsed and the shortlist written, so a
    Research run that failed (LLM outage, schema error) leaves the previous
    ``last_full_run`` in place and the next slot evaluates in full again.
    """
    if not ctx.is_loop_run:
        return
    digest = _loop_digest_of(ctx)
    if digest:
        LoopState(ctx.conn).record_digest(digest, ctx.now, full_run=True)


def _research_facts_tickers(ctx: JobContext, cand_entries: list[Any]) -> list[str]:
    """E4.8a: the candidate tickers (highest confidence first) Research gets facts
    for; ``[]`` when ``personas.finnhub_context`` is off."""
    cfg = ctx.routines.finnhub_context
    if not cfg.enabled:
        return []
    ranked = sorted(
        cand_entries, key=lambda e: (-float(e.payload.get("confidence") or 0.0), e.subject)
    )
    return list(dict.fromkeys(e.subject for e in ranked))[: cfg.research_max_tickers]


def _research_ticker_facts(ctx: JobContext, cand_entries: list[Any]) -> dict[str, Any] | None:
    """E4.8a: the recorded ``ticker_facts`` prompt input, or None with the flag off."""
    cfg = ctx.routines.finnhub_context
    if not cfg.enabled:
        return None
    tickers = _research_facts_tickers(ctx, cand_entries)
    return cfg.prompt_options(tickers, cfg.research_max_tickers)


def _research_sentiment(
    ctx: JobContext, snap: ContextSnapshot, tickers: Sequence[str]
) -> tuple[dict[str, str] | None, list[str]]:
    """E14.6 (D60): ``(ticker -> "ST 80% bull (…)", digest keys)`` for the pool tickers,
    or ``(None, [])`` with ``personas.retail_sentiment_context`` off."""
    if not ctx.routines.retail_sentiment_context.enabled:
        return None, []
    from arc.context.categories import SourceCategory
    from arc.personas.retail_sentiment import (
        fresh_sentiment,
        research_sentiment_facts,
        sentiment_digest,
    )

    max_age = ctx.routines.category_spec(SourceCategory.RETAIL_BUZZ).max_age
    readings = fresh_sentiment(snap, as_of=ctx.now, max_age=max_age)
    return research_sentiment_facts(readings, tickers), sentiment_digest(readings, tickers)


def _youtube_channels(ctx: JobContext) -> list[dict[str, str]]:
    """``[{slug, label}]`` of the ``youtube.briefs`` job (E4.6), ``[]`` if not configured."""
    from arc.ingest.channels.daily import JOB, configured_channels

    found = ctx.routines.job(JOB)
    return configured_channels(found[1].options if found else None)


def _pool_tiers(ctx: JobContext) -> dict[str, str]:
    """E13.8: ``ticker -> tier`` for the pool lines (display only; ``{}`` on error)."""
    from arc.universe.tiers import tier_membership

    try:
        return {
            t: tier.value for t, tier in tier_membership(ctx.conn, ctx.settings, ctx.now).items()
        }
    except Exception as exc:  # noqa: BLE001 - a display column never fails Research
        log.warning("research.pool_tiers_unavailable", error=str(exc)[:200])
        return {}


def _research_prompt_limit(settings: ArcSettings) -> int:
    """E13.8: the compact prompt's budget net of E13.17's exit-block reserve."""
    return settings.research_prompt_max_chars - EXIT_BLOCK_RESERVE_CHARS


def _log_prompt_size(
    snap: ContextSnapshot, inputs: Mapping[str, Any], settings: ArcSettings
) -> int:
    """Log ``research.prompt_over_budget`` when the prompt exceeds the budget."""
    chars = len(build_prompt("research", snap, inputs))
    limit = _research_prompt_limit(settings)
    if chars > limit:
        log.warning(
            "research.prompt_over_budget",
            chars=chars,
            limit=limit,
            max_chars=settings.research_prompt_max_chars,
            compact=bool(inputs.get("compact")),
        )
    return chars


def _fit_research_budget(
    snap: ContextSnapshot,
    inputs: dict[str, Any],
    items: Sequence[PoolItem],
    settings: ArcSettings,
    rules_for: Callable[[Mapping[str, Stance]], list[str]],
) -> tuple[dict[str, Any], list[PoolItem]]:
    """E13.8 (D54): fit the compact prompt in ``research_prompt_max_chars`` - 7,200.

    The pool is listed up to ``POOL_MAX_LINES``. Over budget: the headlines go first
    (``max_headlines`` 0), then the pool is cut to its top ``POOL_BUDGET_CUT`` by
    confidence. Returns the (re-recorded) inputs and the cut pool items, which the
    step journals as ``over_prompt_budget``.
    """
    limit = _research_prompt_limit(settings)

    def _size(out: Mapping[str, Any]) -> int:
        # E13.17's exit block lives in the reserve: measure the prompt without it.
        rest = {k: v for k, v in out.items() if k not in ("exit_block", "exit_rules")}
        return len(build_prompt("research", snap, rest))

    def _with_pool(keep: list[PoolItem]) -> dict[str, Any]:
        cands = {i.ticker: Stance(i.stance) for i in keep}
        return {
            **inputs,
            "idea_pool": [i.model_dump(mode="json") for i in keep],
            "candidate_tickers": sorted(cands),
            "rules": rules_for(cands),
        }

    kept, cut = cut_pool(items, POOL_MAX_LINES)
    out = _with_pool(kept) if cut else inputs
    if _size(out) <= limit:
        return out, cut
    out = {**out, "max_headlines": 0}
    if _size(out) <= limit:
        return out, cut
    kept, more = cut_pool(kept, POOL_BUDGET_CUT)
    out = _with_pool(kept) | {"max_headlines": 0}
    _log_prompt_size(snap, out, settings)
    return out, [*cut, *more]


def _no_candidates(ctx: JobContext) -> JobResult:
    """Research with an empty idea pool: a journaled no-trade and an empty shortlist."""
    j = _journal(ctx, ctx.snapshot.id)
    j.add(
        JournalPersona.RESEARCH,
        Stage.SHORTLIST,
        SESSION_SUBJECT,
        Choice.NO_TRADE,
        ReasonCode.NO_CANDIDATES,
        reason_text="no active Scalp candidates",
    )
    ctx.write(
        "shortlist",
        SESSION_SUBJECT,
        ShortlistPayload(shortlist=[], market_regime="unknown", session_notes="no candidates"),
    )
    return JobResult(summary="no active candidates; empty shortlist", metrics={"shortlist": 0})


def research(ctx: JobContext, env: PipelineEnv) -> JobResult:
    settings = ctx.settings
    all_entries = ctx.snapshot.of_kind("candidate")
    if not all_entries:
        return _no_candidates(ctx)
    # E13.8 (D56/D53): the idea pool = Scalp + Scout candidates merged by ticker.
    pool = build_idea_pool(
        ctx.snapshot,
        merged=True,
        max_scout_only=ctx.routines.funnel.research.max_scout_only_ideas,
        tiers=_pool_tiers(ctx),
    )
    pool_set = set(pool.tickers)
    cand_entries = [e for e in all_entries if e.subject in pool_set]
    cands = {e.subject: Stance(e.payload["stance"]) for e in cand_entries}
    if not cands:
        return _no_candidates(ctx)

    # D32: the day's order budget decides how much the entry chain may do.
    budget = read_budget(ctx, env, settings, now=ctx.clock())
    notice = budget_notice(ctx, budget.budget)
    if not budget.tier.opens_allowed:
        # Opens are exhausted: stop the entry chain here, before any LLM spend.
        # Quant/Risk/propose see an empty shortlist and do nothing.
        why = f"order budget {budget.tier.value}: {budget.budget.summary()}; no new opens"
        j = _journal(ctx, ctx.snapshot.id)
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            SESSION_SUBJECT,
            Choice.NO_TRADE,
            ReasonCode.ORDER_BUDGET_EXHAUSTED,
            reason_text=why,
            payload=budget.budget.model_dump(mode="json"),
        )
        ctx.write(
            "shortlist",
            SESSION_SUBJECT,
            ShortlistPayload(shortlist=[], market_regime="unknown", session_notes=why),
        )
        log.info("pipeline.order_budget_exhausted", **budget.budget.brief())
        return JobResult(
            summary=f"{why}; empty shortlist",
            metrics={"shortlist": 0, **budget.metrics()},
            notice=notice,
        )

    # D51: the market reference (SPY, QQQ) always gets a fresh regime entry, so the
    # D33 guard reads SPY even when SPY is not a candidate (it left the core list).
    from arc.universe.tiers import market_reference

    regimes = _regime_entries(
        ctx,
        env,
        sorted(
            set(cands)
            | set(market_reference(settings))
            # E13.17: IV rank for every open position's exit-watch facts
            | _held_tickers(ctx.conn)
        ),
    )
    # Re-read (and record) the context now that today's regime entries exist.
    snap = ContextStore(ctx.conn).snapshot(ctx.now, kinds=RESEARCH_READS, run_id=ctx.run_id)
    RoutineRunRepo(ctx.conn).set_inputs(ctx.run_id, [ctx.snapshot.id, snap.id])

    # E5.9 (D33): market-conditions guard, before any LLM spend. Missing VIX fails
    # closed for new opens; exits (the positions chain) never come through here.
    guard = market_guard(snap, settings, now=ctx.now, vix_quote=env.vix_quote)
    ctx.record_input("market_guard", "context", guard, as_of=ctx.now)
    if not guard.opens_allowed:
        return _research_no_opens(ctx, guard, budget, notice, cands)

    summary, _ = _portfolio_summary(ctx, env, settings)
    # E5.9 (D33): the open book as Research sees it (deterministic, stored as context).
    # E13.17: with the per-position exit facts.
    pctx = _portfolio_context(ctx, env, settings, budget, snap=snap)
    dedupe_cfg = DedupeConfig.from_settings(settings, budget.tier)
    priors = recent_ideas(ctx.conn, now=ctx.now, cfg=dedupe_cfg)
    ctx.record_input(
        "recent_ideas", "db", [r.model_dump(mode="json") for r in priors], count=len(priors)
    )
    held, recent_lines = _held_and_recent(cands, priors, pctx, ctx.now, dedupe_cfg)
    # D31: change-aware skip. Same inputs as the previous loop and a full run not
    # yet due (loop.max_idle) -> no LLM call; the chain runs its deterministic tail.
    sentiment, sentiment_keys = _research_sentiment(ctx, snap, pool.tickers)
    skip = _loop_no_change(
        ctx,
        snap,
        pctx,
        priors,
        budget,
        facts_tickers=_research_facts_tickers(ctx, cand_entries),
        extra_facts=sentiment_keys,
    )
    if skip is not None:
        return skip
    # Quant/Risk budget (never shown to Research); D32 lowers it in the restrictive tier.
    qr_budget = _shortlist_limit(settings, budget)
    diversification = ctx.routines.director_diversification  # E12.5 (D51)
    inputs = {
        "portfolio_summary": summary,
        "scan_date": _today(ctx).isoformat(),
        "max_notes": settings.pipeline_max_context_notes,
        "portfolio_block": "" if pctx.empty else render_portfolio_context(pctx, settings),
        "recent_ideas": "\n".join(recent_lines),
        "entry_terms": entry_terms(settings).model_dump(mode="json"),
        "rules": _research_rules(cands, settings, budget, pctx, diversification),
        # E4.6 (D45): [{slug, label, category}] of the youtube.briefs job, for the n/N lines.
        "youtube_channels": _youtube_channels(ctx),
        # D49: the effective categories block (labels + max_age), so a replay judges
        # typed-context freshness against the windows this run used.
        "categories": category_specs_input(ctx.routines),
    }
    facts = _research_ticker_facts(ctx, cand_entries)
    if facts is not None:  # E4.8a: absent when the flag is off (prompt unchanged)
        inputs["ticker_facts"] = facts
    if diversification.is_relaxed:  # E12.5: absent when strict (prompt unchanged)
        inputs["diversification"] = diversification.mode
    if sentiment is not None:  # E14.6: absent when the flag is off (prompt unchanged)
        inputs["retail_sentiment"] = sentiment
    if ctx.routines.research_technicals.enabled:  # E16.2: absent when off (prompt unchanged)
        inputs["technicals"] = True
    # E13.8: the merged idea pool, recorded with the compact prompt's inputs
    inputs["idea_pool"] = [i.model_dump(mode="json") for i in pool.items]
    inputs["candidate_tickers"] = sorted(cands)
    inputs["pool_merged"] = True
    if not pctx.empty:  # E13.17: the exit watch (absent with no open positions)
        inputs["exit_block"] = render_exit_block(pctx, settings)
        inputs["exit_rules"] = _exit_watch_rules(ctx)
    inputs["compact"] = True
    inputs, budget_cut = _fit_research_budget(
        snap,
        inputs,
        pool.items,
        settings,
        lambda c: _research_rules(c, settings, budget, pctx, diversification),
    )
    for item in budget_cut:
        cands.pop(item.ticker, None)
    schema: type[ResearchOutput] = ResearchExitOutput if "exit_block" in inputs else ResearchOutput
    reply, out = _ask(ctx, env, "research", snap, inputs, schema)
    kept, dropped, rejected = _filter_shortlist(out, cands)
    kept, pdropped, prejected = _portfolio_filter(kept, pctx, held, settings, diversification)
    dropped.update(pdropped)
    rejected.extend(prejected)
    chase: _AntiChase | None = None
    if ctx.routines.anti_chase.enabled:  # E16.3 (D78 on): off = pipeline as before E16.3
        chase = _anti_chase_filter(ctx, env, snap, kept)
        kept = chase.kept
        dropped.update(chase.dropped)
    ranked = {i.ticker for i in kept}
    excluded = _filter_excluded(out, cands, ranked)
    call_id = _record_ok(ctx, "research", reply, snap.id, dropped)
    no_trade = _no_trade_reason(out, kept)

    j = _journal(ctx, snap.id)
    pool_feeds = {i.ticker: i.feeds for i in pool.items}
    for e in snap.of_kind("candidate"):  # what Research was offered (the pool)
        if e.subject not in cands:
            continue
        scout_only = pool_feeds.get(e.subject) == ["scout"]
        j.add(
            JournalPersona.SCOUT if scout_only else JournalPersona.SCALP,
            Stage.CANDIDATE,
            e.subject,
            Choice.SELECTED,
            ReasonCode.SCOUT_CANDIDATE if scout_only else ReasonCode.SCALP_CANDIDATE,
            confidence=e.payload.get("confidence"),
            payload=e.payload,
        )
    # E13.8: pool cuts by code (Scout-only cap; compact prompt budget), never offered
    for item, code in [
        *[(i, ReasonCode.POOL_SCOUT_ONLY_CAP) for i in pool.capped],
        *[(i, ReasonCode.POOL_OVER_PROMPT_BUDGET) for i in budget_cut],
    ]:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            item.ticker,
            Choice.REJECTED,
            code,
            confidence=item.confidence,
            payload=item.model_dump(mode="json"),
        )
    j.add(
        JournalPersona.RESEARCH,
        Stage.SHORTLIST,
        SESSION_SUBJECT,
        Choice.NOTED,
        ReasonCode.MARKET_READ,
        reason_text=f"{out.market_regime}: {out.session_notes}".strip(": "),
        persona_call_id=call_id,
        payload={"market_regime": out.market_regime, "session_notes": out.session_notes},
    )
    for item in kept:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            item.ticker,
            Choice.SELECTED,
            ReasonCode.SHORTLISTED,
            reason_text=item.thesis,
            confidence=item.confidence,
            persona_call_id=call_id,
            payload=item.model_dump(mode="json"),
        )
    for item, reason in rejected:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            item.ticker.strip().upper() or SESSION_SUBJECT,
            Choice.REJECTED,
            ReasonCode(reason),
            reason_text=item.thesis,
            confidence=item.confidence,
            persona_call_id=call_id,
            payload=item.model_dump(mode="json"),
        )
    if chase is not None:
        _journal_anti_chase(j, chase, call_id)
    for ex in excluded:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            ex.ticker,
            Choice.REJECTED,
            ReasonCode.RESEARCH_EXCLUDED,
            reason_text=ex.reason,
            persona_call_id=call_id,
        )
    accounted = ranked | {e.ticker for e in excluded}
    if chase is not None:  # E16.3: a stretched drop was ranked; it has its own record
        accounted |= {i.ticker for i, _ in chase.rejected}
    not_ranked = sorted(set(cands) - accounted)
    for t in not_ranked:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            t,
            Choice.REJECTED,
            ReasonCode.NOT_RANKED,
            reason_text="neither ranked nor excluded by Research (no reason given)",
            persona_call_id=call_id,
        )
    if no_trade is not None:
        j.add(
            JournalPersona.RESEARCH,
            Stage.SHORTLIST,
            SESSION_SUBJECT,
            Choice.NO_TRADE,
            ReasonCode.RESEARCH_NO_TRADE,
            reason_text=f"{no_trade}: {out.session_notes}".strip(": "),
            persona_call_id=call_id,
            payload={"no_trade_reason": no_trade, "market_regime": out.market_regime},
        )
    watch_items = _valid_watch_items(out, pctx) if "exit_block" in inputs else []
    payload = ShortlistPayload(
        shortlist=kept,
        excluded=excluded,
        market_regime=out.market_regime,
        session_notes=out.session_notes,
        budget=qr_budget,
        portfolio_view=out.portfolio_view if not pctx.empty else None,
        thesis_checks=_watch_thesis_checks(watch_items),
        no_trade_reason=no_trade,
        market_guard=guard,
        suppressed=[ln.removeprefix("- ") for ln in recent_lines],
        pool_counts=IdeaPool([i for i in pool.items if i.ticker in cands], pool.capped).counts(),
        exit_watchlist_counts=(
            {
                "hold": len(pctx.positions) - sum(w.action == "review" for w in watch_items),
                "review": sum(w.action == "review" for w in watch_items),
            }
            if "exit_block" in inputs
            else None
        ),
    )
    entry = ctx.write("shortlist", SESSION_SUBJECT, payload)
    _loop_record_full_run(ctx)
    _portfolio_notes(ctx, payload, pctx, entry.id, call_id)
    if "exit_block" in inputs:
        _exit_watchlist(ctx, watch_items, pctx, call_id)
    _note(
        ctx,
        SESSION_SUBJECT,
        persona="research",
        topic=NoteTopic.REGIME_VIEW,
        title=f"Regime: {out.market_regime}",
        sections=[("Regime", out.market_regime), ("Summary", out.session_notes)],
        about=[entry.id],
    )
    for item in kept:
        _note(
            ctx,
            item.ticker,
            persona="research",
            topic=NoteTopic.THESIS,
            title=f"{item.ticker} {item.stance} thesis",
            sections=[
                ("Thesis", item.thesis),
                ("Regime", item.regime_context),
                ("Evidence", "\n".join(f"- {e}" for e in item.evidence)),
            ],
            facts={"rank": item.rank},
            about=[entry.id],
            stance=_stance(item.stance),
            confidence=item.confidence,
        )
    names = ", ".join(f"{i.ticker} ({i.stance})" for i in kept) or "none"
    drop_items = [(i.ticker.strip().upper() or "?", reason) for i, reason in rejected]
    if chase is not None:
        drop_items += [(i.ticker, reason) for i, reason in chase.rejected]
    funnel = [
        *[(e.ticker, FUNNEL_EXCLUDED, e.reason) for e in excluded],
        *[(t, FUNNEL_NOT_RANKED, "") for t in not_ranked],
    ]
    over = payload.over_budget()
    return JobResult(
        summary=f"{len(cands)} candidates → ranked {len(kept)}: {names}"
        + (f" (budget {qr_budget}; {len(over)} not structured)" if over else "")
        + (f"; excluded {len(excluded)}" if excluded else "")
        + (f"; dropped {dict(dropped)}" if dropped else "")
        + (f"; no trade ({no_trade})" if no_trade else ""),
        metrics={
            "shortlist": len(kept),
            "budgeted": len(payload.budgeted()),
            "over_budget": len(over),
            "excluded": len(excluded),
            "not_ranked": len(not_ranked),
            "regime_written": len(regimes),
            "open_positions": len(pctx.positions),
            "suppressed": len(recent_lines),
            "thesis_checks": len(payload.thesis_checks),
            **(
                {"exit_watch_review": (payload.exit_watchlist_counts or {}).get("review", 0)}
                if payload.exit_watchlist_counts is not None
                else {}
            ),
            "no_change": False,
            "loop_digest": _loop_digest_of(ctx),
            **dropped,
            **budget.metrics(),
        },
        notice=notice,
        # E5.9: no Quant/Risk LLM calls on an empty shortlist. E13.17: with an exit
        # watchlist written the chain goes on so quant.exit runs (the open steps see the
        # empty shortlist and return without an LLM call).
        stop_chain=no_trade is not None and payload.exit_watchlist_counts is None,
        card=research_card(
            payload,
            candidates=len(cands),
            dropped=drop_items,
            funnel=funnel,
            budget=qr_budget,
            evidence=_scalp_evidence(snap),
            exits=watch_items,  # E13.13: the stored watchlist items
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Quant
# ---------------------------------------------------------------------------


def _strategies(stance: str, settings: ArcSettings) -> list[Any]:
    """Scanner strategies for a Research stance under the active account profile (D25).

    ``[]`` means the profile has no structure for the stance (e.g. neutral under
    ``cash_debit``): no trade.
    """
    from arc.scanner import ScanStrategy

    return [ScanStrategy(s) for s in settings.profile.strategies_for(stance)]


def _structure_type(c: ScanCandidate) -> str:
    v = c.strategy.value
    return v if v in ("iron_condor", "long_call", "long_put") else "vertical_spread"


def _quant_leg(sym: str, side: str, ratio: int) -> QuantLeg:
    occ = parse_occ(sym)
    return QuantLeg(
        occ_symbol=occ.format(),
        side=side,
        ratio=ratio,
        strike=float(occ.strike),
        expiry=occ.expiration.isoformat(),
        option_type=occ.kind.value,
    )


def _profile_reason(stance: str) -> ReasonCode:
    return (
        ReasonCode.PROFILE_NO_NEUTRAL
        if stance.strip().lower() == "neutral"
        else ReasonCode.PROFILE_NO_STRUCTURE
    )


def _cost_bps(c: ScanCandidate) -> float:
    """Round-trip cost vs. capital at risk (max loss), in bps.

    ``c.cost`` is the entry slippage + fees under the shared ``CostModel``
    (``config/costs.yaml``); the round trip assumes the exit costs the same.
    """
    max_loss = float(c.structure.max_loss or 0)
    return round(2 * c.cost / max_loss * 10_000, 1) if max_loss > 0 else 0.0


def _realized_vol(snapshot: ContextSnapshot, ticker: str) -> float | None:
    """Realised-vol forecast (mean HV20/HV60) from the ticker's ``regime`` entry, if any."""
    entry = snapshot.latest("regime", ticker)
    vol = entry.payload.get("vol") if entry is not None else None
    if not isinstance(vol, dict):
        return None
    return realized_vol_forecast(vol.get("hv20"), vol.get("hv60"))


def _exit_model(
    c: ScanCandidate, spot: float, exits: ExitConfig, r: float, realized_vol: float | None
) -> ExitModelResult | None:
    """E2.4 static vs managed numbers for a scanner candidate (None without an IV)."""
    if c.atm_iv is None or c.dte < 1:
        return None
    return model_exits(
        c.structure,
        exits.policy_for(c.structure.kind),
        spot=spot,
        iv=c.atm_iv,
        r=r,
        cfg=exits.model,
        spreads=c.leg_spreads,
        realized_vol=realized_vol,
    )


def _menu_rank_key(summary: ExitSummary | None, by: str) -> float:
    """Sort key (ascending) for ``pipeline.rank_menu_by``; unmodelled entries sort last."""
    val = None if summary is None else getattr(summary, by)
    return math.inf if val is None else -float(val)


def _menu_entry(c: ScanCandidate, exit_summary: ExitSummary | None = None) -> dict[str, Any]:
    st = c.structure
    return {
        "structure_type": _structure_type(c),
        "strategy": c.strategy.value,
        "legs": [
            _quant_leg(leg.occ_symbol, leg.side.value, leg.ratio).model_dump()
            | {"mid": float(leg.premium) if leg.premium is not None else None}
            for leg in st.legs
        ],
        "expiration": c.expiration.isoformat(),
        "dte": c.dte,
        "net_debit_credit": float(st.net_debit_credit),
        "max_gain": float(st.max_gain) if st.max_gain is not None else None,
        "max_loss": float(st.max_loss) if st.max_loss is not None else None,
        "breakevens": [float(b) for b in st.breakevens],
        "short_deltas": c.short_deltas,
        "credit_width": c.credit_width,
        "pop": c.pop,
        "ev_per_contract": c.ev_proxy,
        "cost_bps": _cost_bps(c),
        "greeks": {"delta": st.greeks.delta, "vega": st.greeks.vega, "theta": st.greeks.theta},
        "exits": None if exit_summary is None else exit_summary.model_dump(mode="json"),
    }


def _to_quant_structure(
    ticker: str,
    c: ScanCandidate,
    *,
    confidence: float,
    rationale: str,
    exit_summary: ExitSummary | None = None,
) -> QuantStructureOut:
    st = c.structure
    return QuantStructureOut(
        ticker=ticker,
        structure_type=_structure_type(c),
        legs=[_quant_leg(leg.occ_symbol, leg.side.value, leg.ratio) for leg in st.legs],
        net_debit_credit=float(st.net_debit_credit),
        max_gain=float(st.max_gain) if st.max_gain is not None else None,
        max_loss=float(st.max_loss) if st.max_loss is not None else None,
        breakevens=[float(b) for b in st.breakevens],
        greeks=QuantGreeks(
            delta=st.greeks.delta, gamma=st.greeks.gamma, vega=st.greeks.vega, theta=st.greeks.theta
        ),
        dte=c.dte,
        pop=c.pop,
        ev_per_contract=c.ev_proxy,
        cost_bps=_cost_bps(c),
        confidence=confidence,
        rationale=rationale,
        exits=exit_summary,
    )


def _quant_rules(no_chain: list[str], settings: ArcSettings | None = None) -> list[str]:
    extra = [] if settings is None else [_profile_rule(settings)]
    return [
        *extra,
        "Choose ONLY from the per-ticker `menu` above; copy each chosen structure's legs "
        "(occ_symbol, side, ratio) verbatim. Anything else is discarded.",
        "At most one structure per ticker, best first. For every ticker with a menu, "
        "either choose one structure or list it in `skipped` ({ticker, reason}) with a "
        "one-line reason.",
        "All analytics (price, max gain/loss, breakevens, Greeks, PoP, EV, cost, exits) "
        "are replaced by the pipeline's own numbers; your value-add is the choice, "
        "confidence and rationale.",
        "Positions are managed under the exit policy (`exits.policy`), not held to expiry: "
        "weigh `exits.managed_pop` / `exits.managed_net_ev` next to the static numbers.",
        f"Tickers without a tradable chain: {', '.join(no_chain) or 'none'}.",
    ]


@dataclasses.dataclass
class _QuantMenus:
    """The scanner menus Quant chooses from (E13.9: shared by quant.open and quant.revise)."""

    summaries: dict[int, ExitSummary] = dataclasses.field(default_factory=dict)
    menus: dict[str, dict[frozenset[tuple[str, str]], ScanCandidate]] = dataclasses.field(
        default_factory=dict
    )
    chains: dict[str, Any] = dataclasses.field(default_factory=dict)
    spots: dict[str, float] = dataclasses.field(default_factory=dict)
    no_chain: list[str] = dataclasses.field(default_factory=list)
    no_chain_why: dict[str, str] = dataclasses.field(default_factory=dict)
    # (ticker, stance) the account profile cannot trade
    no_profile: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def _quant_menus(ctx: JobContext, env: PipelineEnv, items: list[ResearchRankedItem]) -> _QuantMenus:
    """Scan each item's chain into a menu (with the E2.4 exit model per structure)."""
    from arc.scanner import ScanParams, scan

    settings = ctx.settings
    m = _QuantMenus()
    summaries, menus, chains, spots = m.summaries, m.menus, m.chains, m.spots
    no_chain, no_chain_why, no_profile = m.no_chain, m.no_chain_why, m.no_profile
    today = _today(ctx)
    iv_store = safe_iv_store(ctx.conn)  # E4.12: scanner IV rank from iv_daily
    exits = exit_config(settings)  # D26: exits.yaml + control-panel overrides
    for item in items:
        strategies = _strategies(item.stance, settings)
        if not strategies:
            no_profile.append((item.ticker, item.stance))
            log.info(
                "pipeline.profile_no_structure",
                ticker=item.ticker,
                stance=item.stance,
                profile=settings.account_profile,
                reason=_profile_reason(item.stance).value,
            )
            continue
        try:
            params = ScanParams.from_settings(
                settings, strategies=strategies, top=settings.pipeline_scan_top
            )
            history = iv_store.series(item.ticker, until=today) if iv_store is not None else {}
            res = scan(env.market, item.ticker, params, as_of=today, iv_history=history)
            ctx.record_input(
                f"chain:{item.ticker}",
                _source(env),
                res,
                as_of=ctx.now,
                count=len(res.candidates),
            )
        except Exception as exc:  # noqa: BLE001 - one ticker's chain must not sink the rest
            log.warning("pipeline.scan_failed", ticker=item.ticker, error=str(exc))
            no_chain.append(item.ticker)
            no_chain_why[item.ticker] = f"scan failed: {exc}"[:500]
            continue
        if not res.candidates:
            no_chain.append(item.ticker)
            no_chain_why[item.ticker] = "scanner found no structure passing its filters"
            continue
        spots[item.ticker] = res.spot
        cands = list(res.candidates)
        rv = _realized_vol(ctx.snapshot, item.ticker)
        for c in cands:
            model = _exit_model(c, res.spot, exits, settings.scanner_risk_free_rate, rv)
            if model is not None:
                summaries[id(c)] = ExitSummary.from_result(model)
        by = exits.pipeline.rank_menu_by
        if by != "scanner":
            cands.sort(key=lambda c: _menu_rank_key(summaries.get(id(c)), by))
        menus[item.ticker] = {
            _legs_key([(leg.occ_symbol, leg.side.value) for leg in c.structure.legs]): c
            for c in cands
        }
        chains[item.ticker] = {
            "stance": item.stance,
            "iv": res.iv.model_dump(mode="json"),
            "exit_policy_note": "exits: static = hold to expiry; managed = under the exit "
            "policy in config/exits.yaml (both after costs, $ per contract). Paths move at "
            "the realised-vol forecast (path_vol, mean HV20/HV60) and are priced at IV; "
            "vrp = IV − forecast; rorc_day = managed net EV / (max loss × days held).",
            "menu": [_menu_entry(c, summaries.get(id(c))) for c in cands],
        }

    return m


def quant_open(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """``quant.open`` (was ``quant``): choose one structure per budgeted shortlist name."""
    settings = ctx.settings
    j = _journal(ctx, ctx.snapshot.id)
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    # E5.7: Research ranks everything; only the first `budget` get a structure.
    over = shortlist.over_budget() if shortlist else []
    over_budget = [i.ticker for i in over]
    for item in over:
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            item.ticker,
            Choice.NO_TRADE,
            ReasonCode.OVER_BUDGET,
            reason_text=f"ranked #{item.rank}, beyond the Quant/Risk budget "
            f"(pipeline_max_shortlist={shortlist.budget if shortlist else '?'})",
            confidence=item.confidence,
        )
    if shortlist is None or not shortlist.shortlist:
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            SESSION_SUBJECT,
            Choice.NO_TRADE,
            ReasonCode.NO_STRUCTURE,
            reason_text="empty shortlist",
        )
        ctx.write(
            "structures",
            SESSION_SUBJECT,
            StructuresPayload(structures=[], analysis_notes="empty shortlist"),
        )
        return JobResult(summary="empty shortlist; no structures", metrics={"structures": 0})

    today = _today(ctx)
    m = _quant_menus(ctx, env, shortlist.budgeted())
    summaries, menus, chains, spots = m.summaries, m.menus, m.chains, m.spots
    no_chain, no_chain_why, no_profile = m.no_chain, m.no_chain_why, m.no_profile

    def journal_no_chain(call_id: str | None = None) -> None:
        for t in no_chain:
            j.add(
                JournalPersona.QUANT,
                Stage.STRUCTURE,
                t,
                Choice.NO_TRADE,
                ReasonCode.NO_CHAIN,
                reason_text=no_chain_why.get(t, ""),
                persona_call_id=call_id,
            )

    def journal_no_profile(call_id: str | None = None) -> None:
        for t, stance in no_profile:
            j.add(
                JournalPersona.QUANT,
                Stage.STRUCTURE,
                t,
                Choice.NO_TRADE,
                _profile_reason(stance),
                reason_text=(
                    f"account profile {settings.account_profile} has no structure "
                    f"for a {stance} stance"
                ),
                persona_call_id=call_id,
                payload={"stance": stance, "account_profile": settings.account_profile},
            )

    profile_note = "; ".join(
        f"{t} ({s}): no structure under profile {settings.account_profile}" for t, s in no_profile
    )
    # Deterministic skips (no persona involved): every budgeted ticker is accounted for.
    auto_skips = [
        *[QuantSkip(ticker=t, reason=no_chain_why.get(t) or "no tradable chain") for t in no_chain],
        *[
            QuantSkip(
                ticker=t,
                reason=f"account profile {settings.account_profile} has no structure "
                f"for a {s} stance",
            )
            for t, s in no_profile
        ],
    ]
    if not menus:
        journal_no_chain()
        journal_no_profile()
        notes = [
            n
            for n in (profile_note, no_chain and f"no tradable chain for {', '.join(no_chain)}")
            if n
        ]
        ctx.write(
            "structures",
            SESSION_SUBJECT,
            StructuresPayload(
                structures=[],
                skipped=auto_skips,
                over_budget=over_budget,
                analysis_notes="; ".join(notes),
            ),
        )
        return JobResult(
            summary="; ".join(
                [
                    *([profile_note] if profile_note else []),
                    *([f"no scanner structures for {', '.join(no_chain)}"] if no_chain else []),
                ]
            ),
            metrics={"structures": 0, "no_chain": len(no_chain), "no_profile": len(no_profile)},
        )

    inputs = {
        "chains_json": json.dumps(chains, indent=2, sort_keys=True),
        "underlying_prices_json": json.dumps(spots, sort_keys=True),
        "scan_date": today.isoformat(),
        "entry_terms": entry_terms(settings).model_dump(mode="json"),
        "rules": _quant_rules(no_chain, settings),
    }
    reply, out = _ask(ctx, env, "quant", ctx.snapshot, inputs, QuantOutput)
    dropped: Counter[str] = Counter()
    kept: list[QuantStructureOut] = []
    chosen: dict[str, frozenset[tuple[str, str]]] = {}
    rejected: list[tuple[QuantStructureOut, str]] = []
    for s in out.structures:
        t = s.ticker.strip().upper()
        reason = None
        match = None
        key: frozenset[tuple[str, str]] | None = None
        if t not in menus:
            reason = DROP_NOT_SHORTLISTED
        elif t in chosen:
            reason = DROP_DUPLICATE
        else:
            try:
                key = _legs_key(s.legs)
                match = menus[t].get(key)
            except ValueError:
                match = None
            if match is None:
                reason = DROP_NOT_IN_MENU
        if reason is not None or match is None or key is None:
            dropped[reason or DROP_NOT_IN_MENU] += 1
            rejected.append((s, reason or DROP_NOT_IN_MENU))
            continue
        chosen[t] = key
        kept.append(
            _to_quant_structure(
                t,
                match,
                confidence=s.confidence,
                rationale=s.rationale,
                exit_summary=summaries.get(id(match)),
            )
        )
    call_id = _record_ok(ctx, "quant", reply, ctx.snapshot.id, dropped)

    # E5.7: a budgeted ticker with a menu is either structured, skipped with the
    # Quant's reason, or recorded as "not structured" (no structure, no reason).
    quant_skips: dict[str, QuantSkip] = {}
    for sk in out.skipped:
        t = sk.ticker.strip().upper()
        if t in menus and t not in chosen and t not in quant_skips and sk.reason.strip():
            quant_skips[t] = QuantSkip(ticker=t, reason=sk.reason.strip()[:300])
    # E3.4a: every menu item is inside the configured entry window, so a skip that
    # cites DTE applies a window the config never set. Observability only.
    window = settings.entry_dte_window
    for t, sk in quant_skips.items():
        if mentions_dte(sk.reason):
            log.warning(
                "quant.dte_rule_outside_config",
                ticker=t,
                reason=sk.reason,
                dte_window=f"{window[0]}-{window[1]}",
                menu_dtes=sorted({c.dte for c in menus[t].values()}),
                account_profile=settings.account_profile,
            )
    not_structured = [t for t in menus if t not in chosen and t not in quant_skips]

    by_ticker = {q.ticker: q for q in kept}
    for t, menu in menus.items():
        if t in quant_skips:
            j.add(
                JournalPersona.QUANT,
                Stage.STRUCTURE,
                t,
                Choice.NO_TRADE,
                ReasonCode.QUANT_SKIPPED,
                reason_text=quant_skips[t].reason,
                persona_call_id=call_id,
            )
        elif t not in chosen:
            j.add(
                JournalPersona.QUANT,
                Stage.STRUCTURE,
                t,
                Choice.NO_TRADE,
                ReasonCode.NOT_STRUCTURED,
                reason_text="Quant picked no structure from this ticker's menu and gave no reason",
                persona_call_id=call_id,
            )
        for key, c in menu.items():
            entry = _menu_entry(c, summaries.get(id(c)))
            if chosen.get(t) == key:
                q = by_ticker[t]
                j.add(
                    JournalPersona.QUANT,
                    Stage.STRUCTURE,
                    t,
                    Choice.SELECTED,
                    ReasonCode.CHOSEN_FROM_MENU,
                    reason_text=q.rationale,
                    confidence=q.confidence,
                    persona_call_id=call_id,
                    payload=q.model_dump(mode="json") | {"strategy": entry["strategy"]},
                )
            else:
                j.add(
                    JournalPersona.QUANT,
                    Stage.STRUCTURE,
                    t,
                    Choice.REJECTED,
                    ReasonCode.MENU_NOT_CHOSEN,
                    persona_call_id=call_id,
                    payload=entry,
                )
    for s, reason in rejected:
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            s.ticker.strip().upper() or SESSION_SUBJECT,
            Choice.REJECTED,
            ReasonCode(reason),
            reason_text=s.rationale,
            confidence=s.confidence,
            persona_call_id=call_id,
            payload=s.model_dump(mode="json"),
        )
    journal_no_chain(call_id)
    journal_no_profile(call_id)
    payload = StructuresPayload(
        structures=kept,
        skipped=[*quant_skips.values(), *auto_skips],
        not_structured=not_structured,
        over_budget=over_budget,
        analysis_notes=out.analysis_notes,
    )
    entry = ctx.write("structures", SESSION_SUBJECT, payload)
    for s in kept:
        _note(
            ctx,
            s.ticker,
            persona="quant",
            topic=NoteTopic.THESIS,
            title=f"{s.ticker} {s.structure_type}",
            sections=[("Thesis", s.rationale)],
            about=[entry.id],
            confidence=s.confidence,
        )
    _note(
        ctx,
        SESSION_SUBJECT,
        persona="quant",
        topic=NoteTopic.OBSERVATION,
        title="Quant analysis",
        sections=[("Summary", out.analysis_notes)],
        about=[entry.id],
    )
    desc = "; ".join(
        f"{s.ticker} {s.structure_type} "
        f"{'/'.join(f'{leg.strike:g}' for leg in s.legs)} {s.legs[0].expiry} "
        f"net {s.net_debit_credit:+.2f} PoP {s.pop:.2f}"
        for s in kept
    )
    return JobResult(
        summary=(desc or "no structure chosen")
        + (f"; dropped {dict(dropped)}" if dropped else "")
        + (f"; no chain: {', '.join(no_chain)}" if no_chain else "")
        + (f"; skipped: {', '.join(quant_skips)}" if quant_skips else "")
        + (f"; not structured: {', '.join(not_structured)}" if not_structured else "")
        + (f"; over budget: {len(over_budget)}" if over_budget else "")
        + (f"; {profile_note}" if profile_note else ""),
        metrics={
            "structures": len(kept),
            "no_chain": len(no_chain),
            "no_profile": len(no_profile),
            "skipped": len(quant_skips),
            "not_structured": len(not_structured),
            "over_budget": len(over_budget),
            **dropped,
        },
        card=quant_card(
            payload,
            dropped=dropped,
            dropped_items=[(q.ticker.strip().upper() or "?", r) for q, r in rejected],
            no_chain=no_chain,
            not_structured=not_structured,
            over_budget=over_budget,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Quant revision round (E13.9)
# ---------------------------------------------------------------------------


def quant_revise(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """``quant.revise``: one round answering Risk's ``revise`` verdicts (E13.9).

    Deterministic around one Quant call:

    * skipped (chain continues) when no assessment is ``revise``;
    * ``reject`` structures are dropped, ``accept`` ones pass through unchanged;
    * Quant re-chooses from the same scanner menu only for the ``revise`` tickers, or
      keeps its first structure (``kept``); anything else is dropped (a ticker outside
      the revise set -> ``not_shortlisted``), so the output never exceeds revise + kept;
    * the result is a new ``structures`` entry (``revision_of`` = the review's id) that
      supersedes the first. Risk does not run again: the gate and approval do.
    """
    from arc.routines.handlers import JobSkippedError

    settings = ctx.settings
    review_entry = ctx.snapshot.latest("risk_review", SESSION_SUBJECT)
    review = _latest(ctx.snapshot, "risk_review", RiskReviewPayload)
    first = _latest(ctx.snapshot, "structures", StructuresPayload)
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    if review_entry is None or review is None or first is None or shortlist is None:
        msg = "no revise requests (no risk review)"
        raise JobSkippedError(msg, continue_chain=True)
    if first.revision_of is not None:  # exactly one round, even on a re-run
        msg = "already revised this chain"
        raise JobSkippedError(msg, continue_chain=True)
    verdict = {a.ticker: a.verdict for a in review.assessments}
    revise_set = sorted(t for t, v in verdict.items() if v == "revise")
    if not revise_set:
        msg = "no revise requests"
        raise JobSkippedError(msg, continue_chain=True)

    j = _journal(ctx, ctx.snapshot.id)
    firsts = {q.ticker: q for q in first.structures}
    passthrough = [q for q in first.structures if verdict.get(q.ticker, "accept") == "accept"]
    rejected_t = sorted(t for t, v in verdict.items() if v == "reject")
    items = [i for i in shortlist.budgeted() if i.ticker in revise_set]
    m = _quant_menus(ctx, env, items)
    menus, summaries = m.menus, m.summaries
    today = _today(ctx)
    inputs = {
        "chains_json": json.dumps(m.chains, indent=2, sort_keys=True),
        "underlying_prices_json": json.dumps(m.spots, sort_keys=True),
        "scan_date": today.isoformat(),
        "entry_terms": entry_terms(settings).model_dump(mode="json"),
        "rules": [
            *_quant_rules(m.no_chain, settings),
            "REVISION: return structures only for "
            + ", ".join(revise_set)
            + " (at most one each), or list a ticker in `kept` to keep your first structure.",
        ],
    }
    reply, out = _ask(ctx, env, "quant_revise", ctx.snapshot, inputs, QuantReviseOutput)
    dropped: Counter[str] = Counter()
    revised: list[QuantStructureOut] = []
    bad: list[tuple[QuantStructureOut, str]] = []
    done: set[str] = set()
    for q in out.structures:
        t = q.ticker.strip().upper()
        reason = None
        match = None
        if t not in revise_set or t not in menus:
            reason = DROP_NOT_SHORTLISTED
        elif t in done:
            reason = DROP_DUPLICATE
        else:
            try:
                match = menus[t].get(_legs_key(q.legs))
            except ValueError:
                match = None
            if match is None:
                reason = DROP_NOT_IN_MENU
        if reason is not None or match is None:
            dropped[reason or DROP_NOT_IN_MENU] += 1
            bad.append((q, reason or DROP_NOT_IN_MENU))
            continue
        done.add(t)
        revised.append(
            _to_quant_structure(
                t,
                match,
                confidence=q.confidence,
                rationale=q.rationale,
                exit_summary=summaries.get(id(match)),
            )
        )
    kept_t = sorted({k.strip().upper() for k in out.kept} & set(revise_set) - done & set(firsts))
    # A revise ticker Quant neither re-chose nor kept is dropped (no structure).
    dropped_t = [t for t in revise_set if t not in done and t not in kept_t]
    call_id = _record_ok(ctx, "quant_revise", reply, ctx.snapshot.id, dropped)

    for q in revised:
        before = firsts.get(q.ticker)
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            q.ticker,
            Choice.SELECTED,
            ReasonCode.QUANT_REVISED,
            reason_text=q.rationale,
            confidence=q.confidence,
            persona_call_id=call_id,
            payload=q.model_dump(mode="json")
            | {"replaces": None if before is None else before.model_dump(mode="json")},
        )
    for t in kept_t:
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            t,
            Choice.SELECTED,
            ReasonCode.QUANT_KEPT,
            reason_text="kept the first structure after Risk's revise request",
            persona_call_id=call_id,
        )
    for t in dropped_t:
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            t,
            Choice.NO_TRADE,
            ReasonCode.NOT_STRUCTURED,
            reason_text="Quant neither revised nor kept this structure after Risk's request",
            persona_call_id=call_id,
        )
    for q, reason in bad:
        j.add(
            JournalPersona.QUANT,
            Stage.STRUCTURE,
            q.ticker.strip().upper() or SESSION_SUBJECT,
            Choice.REJECTED,
            ReasonCode(reason),
            reason_text=q.rationale,
            confidence=q.confidence,
            persona_call_id=call_id,
            payload=q.model_dump(mode="json"),
        )
    structures = [
        *passthrough,
        *[firsts[t] for t in kept_t],
        *revised,
    ]
    order = {i.ticker: n for n, i in enumerate(shortlist.shortlist)}
    structures.sort(key=lambda q: order.get(q.ticker, len(order)))
    payload = StructuresPayload(
        structures=structures,
        skipped=first.skipped,
        not_structured=sorted({*first.not_structured, *dropped_t}),
        over_budget=first.over_budget,
        analysis_notes=out.analysis_notes,
        revision_of=review_entry.id,
        kept=kept_t,
    )
    entry = ctx.write("structures", SESSION_SUBJECT, payload)
    _note(
        ctx,
        SESSION_SUBJECT,
        persona="quant",
        topic=NoteTopic.OBSERVATION,
        title="Quant revision",
        sections=[("Summary", out.analysis_notes)],
        about=[entry.id],
    )
    desc = "; ".join(
        f"{q.ticker} {q.structure_type} "
        f"{'/'.join(f'{leg.strike:g}' for leg in q.legs)} {q.legs[0].expiry} "
        f"net {q.net_debit_credit:+.2f} PoP {q.pop:.2f}"
        for q in revised
    )
    return JobResult(
        summary=(f"revised: {desc}" if desc else "nothing revised")
        + (f"; kept: {', '.join(kept_t)}" if kept_t else "")
        + (f"; dropped: {', '.join(dropped_t)}" if dropped_t else "")
        + (f"; rejected by Risk: {', '.join(rejected_t)}" if rejected_t else "")
        + (f"; invalid {dict(dropped)}" if dropped else ""),
        metrics={
            "revise_requests": len(revise_set),
            "revised": len(revised),
            "kept": len(kept_t),
            "dropped": len(dropped_t),
            "rejected": len(rejected_t),
            "structures": len(structures),
            **dropped,
        },
        card=quant_card(
            payload,
            dropped=dropped,
            dropped_items=[(q.ticker.strip().upper() or "?", r) for q, r in bad],
            no_chain=m.no_chain,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
            revision=True,
            kept=kept_t,
        ),
    )


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


def _risk_rules(settings: ArcSettings, caps: Mapping[tuple[str, str], int]) -> list[str]:
    return [
        _profile_rule(settings),
        "Exactly one assessment per proposed structure, with ticker and structure_type "
        "copied verbatim from it.",
        "Each structure's `exits` compares hold-to-expiry (static) with the managed exit "
        "policy (take profit / stop / DTE exit); positions will be managed, so weigh the "
        "managed PoP, net EV and stop probability.",
        "sizing_suggestion is advisory. The pipeline trades min(your suggestion, "
        f"floor({settings.max_alloc_pct:.0%} × equity / max_loss)); caps per structure: "
        + ", ".join(f"{t} {k}: {n}" for (t, k), n in sorted(caps.items()))
        + ". Suggest 0 to decline a trade.",
    ]


#: E13.9: the verdict rules added to Risk's hard constraints with the loop on.
_RISK_VERDICT_RULES = (
    "Every assessment carries a verdict: accept, revise (with revise_request) or reject. "
    "A rejected structure is never proposed; Quant answers revise requests once.",
    "revise_request is required with verdict revise and must be null otherwise; its "
    "instruction is at most 240 characters.",
)


def _open_assessment(a: RiskAssessment) -> RiskOpenAssessment:
    """*a* as a verdict-carrying assessment (a verdict-less one reads as ``accept``)."""
    if not isinstance(a, RiskOpenAssessment):
        return RiskOpenAssessment.model_validate(
            RiskAssessment.model_validate(a, from_attributes=True).model_dump()
        )
    if a.verdict == "revise" and a.revise_request is None:
        # A revise without a request gives Quant nothing to answer: treat as accept.
        log.warning("pipeline.risk_revise_without_request", ticker=a.ticker)
        return a.model_copy(update={"verdict": "accept"})
    if a.verdict != "revise" and a.revise_request is not None:
        return a.model_copy(update={"revise_request": None})
    return a


def _assessment_payload(a: RiskOpenAssessment) -> dict[str, Any]:
    """Journal payload: the assessment with its verdict (E13.9)."""
    return a.model_dump(mode="json")


def risk_open(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """``risk.open`` (was ``risk``): review the structures; E13.9 verdicts when on."""
    settings = ctx.settings
    j = _journal(ctx, ctx.snapshot.id)
    structures = _latest(ctx.snapshot, "structures", StructuresPayload)
    if structures is None or not structures.structures:
        j.add(
            JournalPersona.RISK,
            Stage.RISK_REVIEW,
            SESSION_SUBJECT,
            Choice.NO_TRADE,
            ReasonCode.NO_STRUCTURE,
            reason_text="nothing to review",
        )
        ctx.write(
            "risk_review",
            SESSION_SUBJECT,
            RiskReviewPayload(
                assessments=[], portfolio_summary="", advisory_notes="nothing to review"
            ),
        )
        return JobResult(summary="no structures to review", metrics={"assessments": 0})

    today = _today(ctx)
    info, positions = _account_inputs(ctx, env)
    try:
        portfolio = build_portfolio(
            ctx.conn,
            positions,
            env.market,
            now=ctx.now,
            wash_sale_days=settings.wash_sale_days,
            r=settings.scanner_risk_free_rate,
            spot_max_spread_pct=settings.spot_max_spread_pct,
        )
    except PortfolioError as exc:
        msg = f"portfolio unavailable: {exc}"
        raise PersonaError(msg) from exc
    tickers = sorted({s.ticker for s in structures.structures})
    calendar = {
        "today": today.isoformat(),
        "next_earnings": {
            t: (d.isoformat() if d else None)
            for t, d in next_earnings(ctx.conn, tickers, today).items()
        },
        "earnings_unknown": [
            t for t in tickers if t not in next_earnings(ctx.conn, tickers, today)
        ],
        "expirations": sorted({s.legs[0].expiry for s in structures.structures}),
    }
    budget = Decimal(str(settings.max_alloc_pct)) * info.equity
    caps = {
        (s.ticker, s.structure_type): int(
            max(budget - existing_max_loss(portfolio, s.ticker), Decimal(0))
            // Decimal(str(s.max_loss))
        )
        for s in structures.structures
        if s.max_loss
    }
    inputs = {
        "portfolio_json": portfolio.model_dump_json(indent=2),
        "calendar_json": json.dumps(calendar, indent=2),
        "account_equity": float(info.equity),
        "scan_date": today.isoformat(),
        "entry_terms": entry_terms(settings).model_dump(mode="json"),
        "rules": _risk_rules(settings, caps),
    }
    # E13.9: Risk also gives a verdict per structure.
    prompt_key = "risk_open"
    inputs["rules"] = [*inputs["rules"], *_RISK_VERDICT_RULES]
    reply, out = _ask(ctx, env, prompt_key, ctx.snapshot, inputs, RiskOpenOutput)
    wanted = {(s.ticker, s.structure_type) for s in structures.structures}
    dropped: Counter[str] = Counter()
    kept: list[RiskAssessment] = []
    rejected: list[tuple[RiskAssessment, str]] = []
    seen: set[tuple[str, str]] = set()

    def accept(assessments: list[RiskAssessment]) -> None:
        for a in assessments:
            key = (a.ticker.strip().upper(), a.structure_type.strip().lower())
            if key not in wanted:
                dropped[DROP_UNKNOWN_STRUCTURE] += 1
                rejected.append((a, DROP_UNKNOWN_STRUCTURE))
                continue
            if key in seen:
                dropped[DROP_DUPLICATE] += 1
                rejected.append((a, DROP_DUPLICATE))
                continue
            seen.add(key)
            kept.append(a.model_copy(update={"ticker": key[0], "structure_type": key[1]}))

    accept(list(out.assessments))
    call_id = _record_ok(ctx, prompt_key, reply, ctx.snapshot.id, dropped)
    # E5.7: one repair re-ask for structures Risk left out; still missing → not_assessed.
    repaired: list[str] = []
    if wanted - seen:
        first_missing = sorted(wanted - seen)
        repair_inputs = inputs | {
            "rules": [
                *inputs["rules"],
                "REPAIR: your previous reply had no assessment for "
                + ", ".join(f"{t} {k}" for t, k in first_missing)
                + ". Return assessments for exactly these structures (ticker and "
                "structure_type verbatim).",
            ]
        }
        try:
            r_reply, r_out = _ask(
                ctx,
                env,
                prompt_key,
                ctx.snapshot,
                repair_inputs,
                RiskOpenOutput,
            )
        except PersonaError as exc:
            log.warning("pipeline.risk_repair_failed", missing=first_missing, error=str(exc))
        else:
            before = set(seen)
            r_dropped_before = Counter(dropped)
            accept(list(r_out.assessments))
            repaired = [f"{t} {k}" for t, k in sorted(seen - before)]
            r_id = _record_ok(ctx, prompt_key, r_reply, ctx.snapshot.id, dropped - r_dropped_before)
            log.info("pipeline.risk_repair", missing=first_missing, repaired=repaired, call=r_id)
            call_id = call_id or r_id
    # E13.9: verdicts are applied in code.
    kept = [_open_assessment(a) for a in kept]
    for a in kept:
        declined = a.sizing_suggestion < 1
        if a.verdict == "reject":
            choice, code = Choice.REJECTED, ReasonCode.RISK_REJECT
        elif a.verdict == "revise":
            choice, code = Choice.NOTED, ReasonCode.RISK_REVISE
        elif declined:
            choice, code = Choice.NO_TRADE, ReasonCode.RISK_DECLINED
        else:
            choice, code = Choice.ASSESSED, ReasonCode.RISK_ASSESSED
        req = a.revise_request
        j.add(
            JournalPersona.RISK,
            Stage.RISK_REVIEW,
            a.ticker,
            choice,
            code,
            reason_text=f"{a.risk_rating}: {a.narrative}"
            + (f" | revise ({req.reason}): {req.instruction}" if req is not None else ""),
            persona_call_id=call_id,
            payload=_assessment_payload(a)
            | {"cap_contracts": caps.get((a.ticker, a.structure_type))},
        )
    for a, reason in rejected:
        j.add(
            JournalPersona.RISK,
            Stage.RISK_REVIEW,
            a.ticker.strip().upper() or SESSION_SUBJECT,
            Choice.REJECTED,
            ReasonCode(reason),
            reason_text=a.narrative,
            persona_call_id=call_id,
            payload=a.model_dump(mode="json"),
        )
    missing = sorted(wanted - seen)
    for t, k in missing:
        j.add(
            JournalPersona.RISK,
            Stage.RISK_REVIEW,
            t,
            Choice.NO_TRADE,
            ReasonCode.NOT_ASSESSED,
            reason_text=f"Risk returned no assessment for {t} {k} (after one repair re-ask)",
            persona_call_id=call_id,
        )
    payload = RiskReviewPayload(
        assessments=kept,
        portfolio_summary=out.portfolio_summary,
        advisory_notes=out.advisory_notes,
    )
    entry = ctx.write("risk_review", SESSION_SUBJECT, payload)
    _note(
        ctx,
        SESSION_SUBJECT,
        persona="risk",
        topic=NoteTopic.RISK_FLAG,
        title="Risk advisory",
        sections=[("Risks", out.advisory_notes), ("Portfolio", out.portfolio_summary)],
        about=[entry.id],
    )
    by_key = {(s.ticker, s.structure_type): s for s in structures.structures}
    sized = {
        (a.ticker, a.structure_type): size_contracts(
            suggestion=a.sizing_suggestion,
            max_loss_per_contract=(
                None
                if by_key[(a.ticker, a.structure_type)].max_loss is None
                else Decimal(str(by_key[(a.ticker, a.structure_type)].max_loss))
            ),
            equity=info.equity,
            cap_pct=settings.max_alloc_pct,
            existing_max_loss=existing_max_loss(portfolio, a.ticker),
        )
        for a in kept
    }
    desc = "; ".join(
        f"{a.ticker} {a.risk_rating}, suggests {a.sizing_suggestion}" + f" [{a.verdict}]"
        for a in kept
    )
    missing_s = [f"{t} {k}" for t, k in missing]
    verdicts = Counter(a.verdict for a in kept)
    return JobResult(
        summary=(desc or "no assessments")
        + (f"; not assessed: {', '.join(missing_s)}" if missing_s else "")
        + (f"; repaired: {', '.join(repaired)}" if repaired else "")
        + (f"; dropped {dict(dropped)}" if dropped else ""),
        metrics={
            "assessments": len(kept),
            "not_assessed": len(missing_s),
            "repaired": len(repaired),
            **dropped,
            **{f"verdict_{v}": n for v, n in verdicts.items()},
        },
        card=risk_card(
            payload,
            sized=sized,
            max_gain={k: s.max_gain for k, s in by_key.items()},
            cap_pct=settings.max_alloc_pct,
            dropped=dropped,
            dropped_items=[
                (f"{a.ticker.strip().upper()} {a.structure_type.strip().lower()}", r)
                for a, r in rejected
            ],
            not_assessed=missing_s,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
            verdicts=True,
        ),
    )


# ---------------------------------------------------------------------------
# propose (deterministic) + gate
# ---------------------------------------------------------------------------


def _existing(conn: sqlite3.Connection, chain_run_id: str, ticker: str) -> bool:
    """One open proposal per ticker per chain run (retry / resume idempotency, E5.9)."""
    row = conn.execute(
        "SELECT 1 FROM proposals WHERE chain_run_id = ? AND ticker = ? AND kind = 'open'",
        (chain_run_id, ticker),
    ).fetchone()
    return row is not None


def _chain_key(ctx: JobContext, day: str) -> str:
    """The proposal idempotency key: the chain run id, or a per-run key for manual runs."""
    return ctx.chain_run_id or f"manual-{day}-{ctx.run_id}"


def band_for(st: Any, limit: Decimal, market: Any, settings: ArcSettings) -> PriceBand:
    """D24 band for a structure at *limit* (see :func:`arc.gate.rules.price_band`)."""
    from arc.gate.rules import price_band

    return price_band(st.legs, limit, market, settings)


def existing_max_loss(portfolio: Portfolio, underlying: str) -> Decimal:
    """Max loss already open on *underlying* (Sentinel S-7: sizing uses what is left)."""
    return sum((p.max_loss for p in portfolio.positions if p.underlying == underlying), Decimal(0))


def worst_loss_per_contract(st: Any, band: PriceBand) -> Decimal | None:
    """Max loss per contract if filled at the band's worst price (``None`` = unbounded)."""
    if st.max_loss is None:
        return None
    return max(st.max_loss + (band.hi - st.net_debit_credit) * 100, Decimal(0))


class GateSecretMissingError(RuntimeError):
    """A token-minting (live) propose run has no usable ``ARC_GATE_SECRET``."""


def _mint(
    decision: Any,
    proposal: Proposal,
    settings: ArcSettings,
    now: _dt.datetime,
    *,
    band: PriceBand | None = None,
) -> Any:
    from arc.gate.token import TokenError, gate_secret, issue_token

    if not decision.passed:
        return decision
    try:
        secret = gate_secret(settings)
    except TokenError as exc:
        # E5.2b: a live run that should mint but cannot fails the run (alert), rather
        # than storing a token-less PASS that can never execute and nobody notices.
        msg = f"live propose cannot mint a gate token: {exc} (set ARC_GATE_SECRET)"
        raise GateSecretMissingError(msg) from exc
    return issue_token(decision, proposal, secret=secret, now=now, band=band)


def _with_position(
    portfolio: Portfolio,
    underlying: str,
    max_loss: Decimal,
    g: Any,
    n: int,
    spot: Decimal | None = None,
    beta: Decimal | None = None,
) -> Portfolio:
    """The in-memory book with a gate-passed proposal added, so the next proposal in the
    same run sees it. D57: its dollar delta (Δ × n × spot) is carried forward too; a
    passed proposal always had a spot (the gate fails closed without one). D62: so is
    its beta-weighted dollar delta (Δ × n × spot × β used; β floored at 1.0)."""
    from arc.gate.inputs import Position
    from arc.models import Greeks

    pg = portfolio.greeks
    added = Decimal(str(g.delta)) * n * spot if spot is not None else Decimal(0)
    beta_used = max(beta if beta is not None else Decimal(1), Decimal(1))
    return portfolio.model_copy(
        update={
            "positions": [*portfolio.positions, Position(underlying=underlying, max_loss=max_loss)],
            "dollar_delta": portfolio.dollar_delta + added,
            "beta_dollar_delta": portfolio.beta_dollar_delta + added * beta_used,
            "greeks": Greeks(
                delta=pg.delta + g.delta * n,
                gamma=pg.gamma + g.gamma * n,
                vega=pg.vega + g.vega * n,
                theta=pg.theta + g.theta * n,
            ),
        }
    )


def _proposal_exit_model(
    priced: Any,
    exits: ExitConfig,
    r: float,
    realized_vol: float | None,
    cost: CostModel | None = None,
) -> ExitModelResult | None:
    """E2.4 model for the re-priced proposal structure (None without spot/IV)."""
    st = priced.structure
    if priced.spot is None or priced.atm_iv is None or st.dte < 1:
        return None
    try:
        return model_exits(
            st,
            exits.policy_for(st.kind),
            spot=priced.spot,
            iv=priced.atm_iv,
            r=r,
            cfg=exits.model,
            spreads=priced.leg_spreads(),
            realized_vol=realized_vol,
            cost=cost,
        )
    except ValueError as exc:  # e.g. a stop basis that does not fit this structure
        log.warning("pipeline.exit_model_failed", error=str(exc))
        return None


def _realloc_source(
    priced: Any,
    exits: ExitConfig,
    settings: ArcSettings,
    cost: CostModel | None,
    *,
    realized_vol: float | None,
    candidate_id: str,
    thesis: str,
    quant: QuantMetrics,
    risk_narrative: str,
    suggestion: int,
) -> dict[str, Any]:
    """What a swap needs to re-propose a capacity-blocked entry (E6.4).

    Stored on the ``sizing:budget_exhausted`` journal row (no new table). Managed net
    EV and PoP come from the E2.4 model at the re-priced mids, after all costs.
    """
    st = priced.structure
    model = _proposal_exit_model(priced, exits, settings.scanner_risk_free_rate, realized_vol, cost)
    bp = st.buying_power if st.buying_power is not None else st.max_loss
    return {
        "candidate_id": candidate_id,
        "thesis": thesis,
        "quant": quant.model_dump(mode="json"),
        "risk_narrative": risk_narrative,
        "suggestion": suggestion,
        "kind": st.kind.value if st.kind else None,
        "legs": [[leg.occ_symbol, leg.side.value, leg.ratio] for leg in st.legs],
        "net_ev": None if model is None else model.managed.net_ev,
        "pop": None if model is None else model.managed.pop,
        "buying_power": None if bp is None else float(bp),
    }


def _restrictive_check(
    priced: Any,
    exits: ExitConfig,
    settings: ArcSettings,
    cost: CostModel | None,
    realized_vol: float | None,
) -> tuple[bool, str]:
    """D32 restrictive tier: does the re-priced structure clear the stricter floors?

    Net EV floor = ``min_net_ev_multiplier × round-trip costs`` (entry costs twice);
    PoP floor = breakeven PoP (``max_loss / (max_loss + max_gain)``) +
    ``min_pop_delta_pp``. Both on the managed (exit-policy) numbers after costs; a
    structure without a managed model fails closed.
    """
    from arc.budget.orders import RestrictiveConfig, restrictive_floors

    st = priced.structure
    model = _proposal_exit_model(priced, exits, settings.scanner_risk_free_rate, realized_vol, cost)
    floors = restrictive_floors(
        RestrictiveConfig(
            min_net_ev_multiplier=settings.order_budget_restrictive_min_net_ev_multiplier,
            min_pop_delta_pp=settings.order_budget_restrictive_min_pop_delta_pp,
        ),
        base_net_ev_floor=0.0,
        base_pop_floor=0.0,
        round_trip_cost=0.0 if model is None else 2 * model.entry_costs,
        max_loss=None if st.max_loss is None else float(st.max_loss),
        max_gain=None if st.max_gain is None else float(st.max_gain),
    )
    if model is None:
        return floors.passes(None, None)
    return floors.passes(model.managed.net_ev, model.managed.pop)


_SIZING_REASONS = {
    "ok": ReasonCode.SIZING_OK,
    "capped": ReasonCode.SIZING_CAPPED,
    "live_capped": ReasonCode.SIZING_CAPPED,  # D70: the live cap; payload carries live_cap
    "cap_zero": ReasonCode.SIZING_CAP_ZERO,
    "budget_exhausted": ReasonCode.SIZING_BUDGET_EXHAUSTED,
    "risk_zero": ReasonCode.SIZING_RISK_ZERO,
    "unbounded": ReasonCode.SIZING_UNBOUNDED,
    "invalid_input": ReasonCode.SIZING_INVALID_INPUT,
}


def _market_context(
    snapshot: ContextSnapshot,
    ticker: str,
    phash: str,
    priced: PricedStructure,
    now: _dt.datetime,
) -> MarketContext:
    """Freeze what the market looked like when *phash* was proposed."""
    regime = snapshot.latest("regime", ticker)
    f = regime.payload if regime else {}
    vol = f.get("vol") or {}
    legs = [
        LegQuote(
            occ_symbol=sym,
            bid=c.bid,
            ask=c.ask,
            mid=c.mid,
            iv=c.implied_volatility,
            quote_time=c.quote_timestamp,
        )
        for sym, c in priced.contracts.items()
    ]
    times = [q.quote_time for q in legs if q.quote_time is not None]
    return MarketContext(
        proposal_hash=phash,
        subject=ticker,
        underlying_last=priced.spot,
        atm_iv=vol.get("iv"),
        ivr=vol.get("iv_rank"),
        hv20=vol.get("hv20"),
        regime=(f.get("regime") or {}).get("current"),
        legs=legs,
        quotes_as_of=min(times) if times else None,
        at=now,
        technicals=f.get("technicals"),  # E16.2: audit only (validated on the model)
    )


def _review_for(
    assessed: Mapping[tuple[str, str], RiskOpenAssessment],
    ticker: str,
    structure_type: str,
    *,
    revised: bool,
) -> RiskOpenAssessment | None:
    """Risk's assessment for this structure (E13.9: a revised one by ticker).

    Risk does not run again on a revision; its review of the ticker (the advisory
    sizing and narrative) carries over to the structure Quant re-chose for it.
    """
    a = assessed.get((ticker, structure_type))
    if a is not None or not revised:
        return a
    return next((v for (t, _), v in assessed.items() if t == ticker), None)


def quant_propose(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """``quant.propose`` (was ``propose``; D56: Quant owns it): size, gate, propose.

    E13.18: the exit cases are settled first
    (:func:`_propose_exits`: Risk ``close`` -> a close proposal through
    ``propose_close``; ``hold`` -> a journaled no-action), then the opens as before.
    Exits go first so a close's buying power is visible to the open path's sizing on
    the next loop and a failing open path never blocks risk management.
    """
    exit_res = _propose_exits(ctx, env)
    res = _propose_opens(ctx, env)
    res.metrics = {**res.metrics, **exit_res.metrics}
    res.summary = f"exits: {exit_res.summary}; opens: {res.summary}"
    res.notice = "; ".join(n for n in (exit_res.notice, res.notice) if n)
    return res


def _propose_opens(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """The open path of ``quant.propose`` (E5.2 / E13.9)."""
    from arc.gate.halt import HaltSwitch, evaluate_with_halt
    from arc.gate.rules import grid_for, proposal_hash
    from arc.store.repos import GateDecisionRepo, HaltRepo, ProposalRepo

    settings = ctx.settings
    # E5.2b: ``ctx.now`` is the chain start (before Research/Quant/Risk LLM
    # calls) and keys ``day`` idempotency only. Data age, the gate, the token and
    # the proposal's expiry use ``ctx.clock()``: read at step start, after the
    # account fetch, and again after each ticker's quotes are fetched.
    now = ctx.clock()
    day = _today(ctx).isoformat()
    chain_key = _chain_key(ctx, day)
    j = _journal(ctx, ctx.snapshot.id)
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    structures = _latest(ctx.snapshot, "structures", StructuresPayload)
    review = _latest(ctx.snapshot, "risk_review", RiskReviewPayload)
    if not (shortlist and structures and review) or not structures.structures:
        with ctx.conn:
            j.add(
                JournalPersona.QUANT,
                Stage.PROPOSE,
                SESSION_SUBJECT,
                Choice.NO_TRADE,
                ReasonCode.NO_STRUCTURE,
                reason_text="nothing to propose",
            )
        return JobResult(summary="nothing to propose", metrics={"proposals": 0})

    if env.mint_tokens:
        from arc.gate.token import TokenError, gate_secret

        try:  # fail before any broker call or proposal row, not after the gate
            gate_secret(settings)
        except TokenError as exc:
            msg = f"live propose cannot mint a gate token: {exc} (set ARC_GATE_SECRET)"
            raise GateSecretMissingError(msg) from exc

    cand_ids = {e.subject: e.payload.get("id") for e in ctx.snapshot.of_kind("candidate")}
    by_ticker = {s.ticker: s for s in reversed(structures.structures)}  # first (best) wins
    assessed = {(a.ticker, a.structure_type): a for a in review.assessments}
    # E13.9: a structure from the quant.revise round is marked `revised` on its proposal.
    revised = set() if structures.revision_of is None else {s.ticker for s in structures.structures}
    revised -= set(structures.kept)

    info, positions = _account_inputs(ctx, env)
    fetched_at = ctx.clock()  # as_of = broker fetch time
    # D32: today's order count stamps the gate snapshot; the tier caps the ladder.
    budget = read_budget(ctx, env, settings, now=fetched_at)
    notice = budget_notice(ctx, budget.budget)
    settings = budget.settings  # tier-adjusted improvement steps (band, gate, token agree)
    account = account_snapshot(
        info,
        fetched_at,
        baseline=account_baseline(ctx.conn, info, fetched_at),
        orders_used_today=budget.budget.used,
        live_gate_met=live_gate_status(ctx.conn, settings, now=fetched_at),
    )
    portfolio = build_portfolio(
        ctx.conn,
        positions,
        env.market,
        now=now,
        wash_sale_days=settings.wash_sale_days,
        r=settings.scanner_risk_free_rate,
        spot_max_spread_pct=settings.spot_max_spread_pct,
    )
    earnings = next_earnings(ctx.conn, list(by_ticker), _today(ctx))
    switch = HaltSwitch(HaltRepo(ctx.conn))
    exits = exit_config(settings)  # D26: exits/costs yaml + control-panel overrides
    cost_model = cost_config(settings)
    ranking = ranking_config(settings)  # E6.4a: live Net EV floor (ranking.yaml filters)

    skipped: Counter[str] = Counter()
    lines: list[str] = []
    passed = proposals = 0

    def skip(t: str, key: str, code: ReasonCode, text: str, **payload: Any) -> None:
        skipped[key] += 1
        with ctx.conn:  # no other output for this ticker: commit the decision alone
            j.add(
                JournalPersona.QUANT,
                Stage.PROPOSE,
                t,
                Choice.NO_TRADE,
                code,
                reason_text=text,
                payload=payload or None,
            )

    if not budget.tier.opens_allowed:
        for item in shortlist.shortlist:
            skip(
                item.ticker,
                "order_budget",
                ReasonCode.ORDER_BUDGET_EXHAUSTED,
                f"order budget {budget.tier.value}: {budget.budget.summary()}",
                **budget.budget.brief(),
            )
        return JobResult(
            summary=f"no proposals: {budget.budget.summary()}",
            metrics={"proposals": 0, **skipped, **budget.metrics()},
            notice=notice,
        )
    max_opens = (
        settings.order_budget_restrictive_max_new_opens_per_loop if budget.tier.restricted else None
    )
    dedupe_cfg = DedupeConfig.from_settings(settings, budget.tier)
    priors = recent_ideas(ctx.conn, now=now, cfg=dedupe_cfg)
    ctx.record_input(
        "recent_ideas", "db", [r.model_dump(mode="json") for r in priors], count=len(priors)
    )

    # Over-budget names were journalled OVER_BUDGET by the Quant step (E5.7).
    for item in shortlist.budgeted():
        t = item.ticker
        qs = by_ticker.get(t)
        if qs is None:
            skip(t, "no_structure", ReasonCode.NO_STRUCTURE, "Quant chose no structure")
            continue
        if max_opens is not None and proposals >= max_opens:
            skip(
                t,
                "budget_restrictive",
                ReasonCode.BUDGET_RESTRICTIVE,
                f"restrictive tier: at most {max_opens} new open(s) per run",
                **budget.budget.brief(),
            )
            lines.append(f"{t}: skipped (restrictive tier: {max_opens} open(s) per run)")
            continue
        if _existing(ctx.conn, chain_key, t):
            skip(t, "exists", ReasonCode.ALREADY_PROPOSED, "already proposed in this chain run")
            lines.append(f"{t}: already proposed in this chain run")
            continue
        a = _review_for(assessed, t, qs.structure_type, revised=t in revised)
        if a is None:
            skip(t, "no_risk_review", ReasonCode.NO_RISK_REVIEW, "no Risk assessment")
            continue
        if a.verdict == "reject":  # E13.9: Risk's reject is final (journalled by risk.open)
            skipped["risk_reject"] += 1
            lines.append(f"{t}: rejected by Risk")
            continue
        if not cand_ids.get(t):
            skip(t, "no_candidate", ReasonCode.NO_CANDIDATE_ID, "no Scalp candidate row")
            continue
        try:
            priced = price_structure(
                env.market,
                [(leg.occ_symbol, LegIntent(leg.side), leg.ratio) for leg in qs.legs],
                as_of=_today(ctx),
                r=settings.scanner_risk_free_rate,
                spot_max_spread_pct=settings.spot_max_spread_pct,
            )
        except (LookupError, ValueError) as exc:
            log.warning("pipeline.reprice_failed", ticker=t, error=str(exc))
            skip(t, "reprice_failed", ReasonCode.REPRICE_FAILED, str(exc)[:500])
            lines.append(f"{t}: reprice failed ({str(exc)[:200]})")
            continue
        ctx.record_input(
            f"quotes:{t}",
            _source(env),
            {"contracts": priced.contracts, "spot": priced.spot, "atm_iv": priced.atm_iv},
            as_of=priced.spot_as_of or now,
            count=len(priced.contracts),
        )
        st = priced.structure
        # E5.9 (D33): final idea dedupe on the full fingerprint (needs the live spot).
        fp = fingerprint(t, item.stance, st, priced.spot, structure_type=qs.structure_type)
        verdict = check_idea(
            fp,
            priors,
            spot=priced.spot,
            regime=shortlist.market_regime,
            cfg=dedupe_cfg,
        )
        if verdict.suppressed:
            code = verdict.reason_code or ReasonCode.DEDUPE_PROPOSED
            skip(
                t,
                "dedupe",
                code,
                f"repeat idea ({verdict.detail})",
                fingerprint=fp.key(),
                prior=verdict.prior.model_dump(mode="json") if verdict.prior else None,
            )
            lines.append(f"{t}: repeat idea ({verdict.detail})")
            continue
        if verdict.override is not None:
            with ctx.conn:
                j.add(
                    JournalPersona.QUANT,
                    Stage.PROPOSE,
                    t,
                    Choice.NOTED,
                    ReasonCode.DEDUPE_OVERRIDE,
                    reason_text=verdict.detail,
                    payload={"fingerprint": fp.key(), "override": verdict.override},
                )
        exit_model = _proposal_exit_model(
            priced,
            exits,
            settings.scanner_risk_free_rate,
            _realized_vol(ctx.snapshot, t),
            cost_model,
        )
        # E6.4a (D41): live Net EV floor. A structure whose managed Net EV after all
        # costs is <= ranking.filters.min_managed_net_ev never becomes a proposal.
        ev_ok, ev_why = live_net_ev_check(
            None if exit_model is None else exit_model.managed.net_ev, ranking.filters
        )
        if not ev_ok:
            skip(
                t,
                "net_ev_floor",
                ReasonCode.NET_EV_FLOOR,
                ev_why,
                managed_net_ev=None if exit_model is None else exit_model.managed.net_ev,
                floor=ranking.filters.min_managed_net_ev,
                structure_type=qs.structure_type,
            )
            lines.append(f"{t}: dropped (net EV floor: {ev_why})")
            continue
        if budget.tier.restricted:
            # D32 restrictive tier: stricter managed Net EV / PoP floors, deterministic.
            ok, why = _restrictive_check(
                priced, exits, settings, cost_model, _realized_vol(ctx.snapshot, t)
            )
            if not ok:
                skip(
                    t,
                    "budget_restrictive",
                    ReasonCode.BUDGET_RESTRICTIVE,
                    f"restrictive tier: {why}",
                    **budget.budget.brief(),
                )
                lines.append(f"{t}: dropped (restrictive tier: {why})")
                continue
        # Quotes were just fetched: judge their age (and stamp the gate, token and
        # expiry) against a clock read now, never a time taken before the fetch.
        now = ctx.clock()
        market = market_snapshot(
            priced.contracts,
            earnings,
            {t: priced.spot},
            proposal_betas(ctx.conn, [t], now.astimezone(ET).date()),
            quote_ref_time=priced.quote_ref_times(now),  # E10.2d: paired arm replay
        )
        limit = limit_price(st.net_debit_credit, grid_for(st.legs, market, settings))
        # D24: the gate checks the whole price band; size at its worst price (D18).
        band = band_for(st, limit, market, settings)
        size = size_contracts(
            suggestion=a.sizing_suggestion,
            max_loss_per_contract=worst_loss_per_contract(st, band),
            equity=info.equity,
            cap_pct=settings.max_alloc_pct,
            existing_max_loss=existing_max_loss(portfolio, t),  # S-7: remaining budget
        )
        live_cap = live_size_cap(settings, account)  # D70: None in paper / gate met
        size = apply_live_cap(size, live_cap, info.equity)
        sizing_payload = size.model_dump(mode="json") | {
            "max_loss_per_contract": str(st.max_loss) if st.max_loss is not None else None,
            "worst_loss_per_contract": (
                str(worst) if (worst := worst_loss_per_contract(st, band)) is not None else None
            ),
            "equity": str(info.equity),
            "cap_pct": settings.max_alloc_pct,
        }
        if live_cap is not None:
            sizing_payload["live_cap"] = live_cap
        if not size.trade:
            skipped["sizing"] += 1
            if size.code == "budget_exhausted":
                # E6.4: existing exposure on this underlying uses the budget up. Keep
                # what a close-to-reallocate swap needs (quant.propose reads it back).
                sizing_payload["realloc_source"] = _realloc_source(
                    priced,
                    exits,
                    settings,
                    cost_model,
                    realized_vol=_realized_vol(ctx.snapshot, t),
                    candidate_id=str(cand_ids[t]),
                    thesis=item.thesis,
                    quant=QuantMetrics(
                        pop=qs.pop, ev=Decimal(str(qs.ev_per_contract)), cost_bps=qs.cost_bps
                    ),
                    risk_narrative=a.narrative,
                    suggestion=a.sizing_suggestion,
                )
            with ctx.conn:
                j.add(
                    JournalPersona.SIZING,
                    Stage.SIZING,
                    t,
                    Choice.NO_TRADE,
                    _SIZING_REASONS[size.code],
                    reason_text=size.reason,
                    payload=sizing_payload,
                )
            lines.append(f"{t}: no trade ({size.reason})")
            continue
        proposal = Proposal(
            candidate_id=str(cand_ids[t]),
            structure=st,
            thesis=item.thesis,
            quant=QuantMetrics(
                pop=qs.pop, ev=Decimal(str(qs.ev_per_contract)), cost_bps=qs.cost_bps
            ),
            risk_narrative=a.narrative,
            sizing=Sizing(
                contracts=size.contracts,
                notional=size.max_loss_total,
                pct_equity=min(size.pct_equity, 1.0),
            ),
            expires_at=now + _dt.timedelta(seconds=settings.approval_ttl_seconds),
            limit_price=limit,
        )
        decision = evaluate_with_halt(
            switch,
            proposal,
            account,
            portfolio,
            settings,
            market=market,
            now=now,
            band=band,
        )
        if env.mint_tokens:
            decision = _mint(decision, proposal, settings, now, band=band)
        phash = proposal_hash(proposal)
        try:
            # One transaction: proposal + gate decision + journal + context entry.
            ProposalRepo(ctx.conn).insert(
                candidate_id=proposal.candidate_id,
                proposal_hash=phash,
                structure_json=st.model_dump_json(),
                thesis=proposal.thesis,
                quant_json=proposal.quant.model_dump_json(),
                risk_narrative=proposal.risk_narrative,
                sizing_json=proposal.sizing.model_dump_json(),
                expires_at=proposal.expires_at.isoformat(),
                created_at=now.isoformat(),
                run_id=ctx.run_id,
                day=day,
                ticker=t,
                chain_run_id=chain_key,
                fingerprint=fp.key(),
                spot=str(priced.spot),
                regime=shortlist.market_regime,
                commit=False,
            )
        except sqlite3.IntegrityError:
            ctx.conn.rollback()
            # Only the (chain, ticker) unique slot is a race a concurrent run can win.
            # Any other constraint (e.g. proposals.candidate_id -> candidates, which hid
            # the E10.2a arm's missing candidate rows) is a bug: fail the step loudly.
            if not _existing(ctx.conn, chain_key, t):
                raise
            skip(t, "exists", ReasonCode.ALREADY_PROPOSED, "a concurrent run won the slot")
            continue
        GateDecisionRepo(ctx.conn).insert(
            proposal_hash=phash,
            passed=decision.passed,
            violations=decision.violations,
            token=decision.token,
            account_snapshot=decision.account_snapshot,
            ref_time=decision.ref_time,
            decided_at=now.isoformat(),
            run_id=ctx.run_id,
            commit=False,
        )
        j.add(
            JournalPersona.QUANT,
            Stage.PROPOSE,
            t,
            Choice.SELECTED,
            ReasonCode.PROPOSED,
            proposal_hash=phash,
            payload={
                "structure": st.model_dump(mode="json"),
                "limit_price": str(proposal.limit_price),
                "band": band.model_dump(mode="json"),
                "quant": proposal.quant.model_dump(mode="json"),
            },
        )
        j.add(
            JournalPersona.SIZING,
            Stage.SIZING,
            t,
            Choice.SIZED,
            _SIZING_REASONS[size.code],
            reason_text=(
                f"min(Risk {size.suggestion}, cap {size.cap_contracts}) = {size.contracts}"
                if live_cap is None
                else f"min(Risk {size.suggestion}, cap {size.cap_contracts}, "
                f"live cap {live_cap}) = {size.contracts}"
            ),
            proposal_hash=phash,
            payload=sizing_payload,
        )
        if decision.passed:
            j.add(
                JournalPersona.GATE,
                Stage.GATE,
                t,
                Choice.PASSED,
                ReasonCode.GATE_PASS,
                reason_text="token issued" if decision.token else "no token (not executable)",
                proposal_hash=phash,
            )
        for v in decision.violations:
            j.add(
                JournalPersona.GATE,
                Stage.GATE,
                t,
                Choice.FAILED,
                gate_reason(v),
                reason_text=v,
                proposal_hash=phash,
            )
        regime_entry = ctx.snapshot.latest("regime", t)
        try:
            analytics = build_analytics(
                priced,
                cost=cost_model,
                regime=regime_entry.payload if regime_entry else None,
                exit_model=exit_model,
                account_profile=getattr(settings, "account_profile", None),
            )
        except ValueError as exc:  # no spot: the card renders without analytics
            log.warning("pipeline.analytics_failed", ticker=t, error=str(exc))
            analytics = None
        JournalStore(ctx.conn).record_market_context(
            _market_context(ctx.snapshot, t, phash, priced, now).model_copy(
                update={"analytics": analytics}
            )
        )
        ctx.write(
            "proposal",
            t,
            ProposalPayload.model_validate(
                proposal.model_dump() | {"exit_model": exit_model, "revised": t in revised}
            ),
        )
        proposals += 1
        strikes = "/".join(f"{parse_occ(leg.occ_symbol).strike.normalize():f}" for leg in st.legs)
        verdict = "PASS" if decision.passed else f"FAIL ({'; '.join(decision.violations)})"
        lines.append(
            f"{t} {st.kind.value if st.kind else 'structure'} {strikes} "
            f"{parse_occ(st.legs[0].occ_symbol).expiration} x{size.contracts} "
            f"limit {proposal.limit_price:+} max loss ${size.max_loss_total:,.0f} "
            f"({size.pct_equity:.2%}) gate {verdict}"
        )
        log.info(
            "pipeline.proposal",
            ticker=t,
            proposal_hash=phash,
            contracts=size.contracts,
            passed=decision.passed,
            violations=decision.violations,
            token=decision.token is not None,
            run_id=ctx.run_id,
        )
        if decision.passed:
            passed += 1
            portfolio = _with_position(
                portfolio,
                t,
                size.max_loss_total,
                st.greeks,
                size.contracts,
                market.underlying_spot.get(t),
                market.underlying_beta.get(t),
            )
    return JobResult(
        summary="; ".join(lines) or f"no proposals ({dict(skipped)})",
        metrics={"proposals": proposals, "gate_passed": passed, **skipped, **budget.metrics()},
        notice=notice,
    )


# ---------------------------------------------------------------------------
# quant.exit (E13.17 / D56): exit cases
# ---------------------------------------------------------------------------

_NO_JUDGEMENT = "no judgement (fail closed to hold)"


def _case_block(case: ExitCase, cap: int) -> str:
    """One exit case for the Quant prompt (≤ *cap* chars; facts as compact JSON)."""
    trig = "; ".join(f"{t.kind}: {t.detail}" for t in case.triggers)
    facts = json.dumps(case.facts.model_dump(mode="json", exclude_none=True), sort_keys=True)
    swap = ""
    if case.swap is not None:
        s = case.swap
        swap = (
            f"\n  swap: close for {s.open_ticker} (edge {s.edge:.3f}/BP vs "
            f"min {s.min_edge_required:.3f}; new PoP {s.new_pop:.2f})"
        )
    text = (
        f"- structure_id {case.structure_id} {case.ticker} {case.kind or 'structure'}\n"
        f"  triggers: {trig}\n  facts: {facts}{swap}"
    )
    return text if len(text) <= cap else text[: cap - 1] + "…"


def quant_exit(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """``quant.exit``: Quant's hold/close judgement on code-built exit cases (E13.17).

    Deterministic around one Quant call:

    * a position with a mandatory signal (stop / DTE exit / expiry), a pending exit or
      an exit already proposed today gets no case (``exit:case_skipped``);
    * triggers: Research's watch item says ``review``, a discretionary signal fired,
      or the position pairs with today's capacity rejections (D19 ``score_swaps``);
    * at most ``quant_exit_max_cases`` cases (watch reviews first), each prompt block
      ≤ ``quant_exit_case_max_chars``; a case Quant omits is ``hold``;
    * writes one ``exit_case`` per case + journal rows. Nothing is proposed here:
      ``risk.exit`` reviews the cases and ``quant.propose`` turns closes into proposals.
    """
    from arc.positions.evaluate import PositionReview as _Review
    from arc.positions.exit_case import (
        ExitCaseFacts,
        ExitSwap,
        case_skip_reason,
        triggers_for,
    )
    from arc.positions.reallocate import ReallocRules, pair_swaps
    from arc.positions.steps import capacity_candidates
    from arc.routines.handlers import JobSkippedError
    from arc.slack.digests import quant_exit_card
    from arc.store.execution import OpenStructureRepo
    from arc.store.swaps import SwapRepo

    settings = ctx.settings
    rows = {str(r["id"]): r for r in OpenStructureRepo(ctx.conn).list_open()}
    if not rows:
        msg = "no open positions"
        raise JobSkippedError(msg, continue_chain=True)
    reviews: dict[str, PositionReview] = {}
    for e in ctx.snapshot.of_kind("position_review"):
        r = _Review.model_validate(e.payload)
        if r.structure_id in rows:
            reviews[r.structure_id] = r
    wl_entry = ctx.snapshot.latest("exit_watchlist", SESSION_SUBJECT)
    watch = (
        {w.structure_id: w for w in ExitWatchlistPayload.model_validate(wl_entry.payload).items}
        if wl_entry is not None
        else {}
    )
    pc_entry = ctx.snapshot.latest("portfolio_context", SESSION_SUBJECT)
    held = PortfolioContext.model_validate(pc_entry.payload).positions if pc_entry else []
    facts = {p.structure_id: p.facts for p in held}
    today = _today(ctx)
    day = today.isoformat()
    swaps = SwapRepo(ctx.conn)
    capacity, _ = capacity_candidates(ctx.conn, day, taken=swaps.sources())
    count, per_ticker = swaps.churn(day)
    rules = ReallocRules(
        min_edge=settings.realloc_min_edge,
        pop_tolerance=settings.realloc_pop_tolerance,
        max_per_day=settings.realloc_max_swaps_per_day,
        max_per_ticker_per_day=settings.realloc_max_swaps_per_ticker_per_day,
    )
    skip: dict[str, str] = {}
    eligible: list[PositionReview] = []
    for sid in sorted(reviews):
        why = case_skip_reason(reviews[sid], today_exit=rows[sid].get("exit_day") == day)
        if why is not None:
            skip[sid] = why
        else:
            eligible.append(reviews[sid])
    paired = {
        sid: ExitSwap.of(sw)
        for sid, sw in pair_swaps(
            eligible, capacity, rules, swaps_today=count, ticker_swaps_today=per_ticker
        ).items()
    }
    exits = exit_config(settings)
    pending: list[ExitCase] = []
    for r in eligible:
        w = watch.get(r.structure_id)
        trig = triggers_for(r, w, paired.get(r.structure_id))
        if not trig:
            skip[r.structure_id] = "no_trigger"
            continue
        kind = rows[r.structure_id].get("kind") or r.kind
        try:
            policy = exits.policy_for(StructureKind(kind)) if kind else exits.policy_for(None)
        except ValueError:
            policy = exits.policy_for(None)
        case = ExitCasePayload(
            structure_id=r.structure_id,
            ticker=r.ticker,
            kind=r.kind,
            triggers=trig,
            facts=ExitCaseFacts.from_review(
                r,
                facts.get(r.structure_id),
                policy=policy,
                thesis_status=w.thesis_status if w is not None else None,
            ),
            swap=paired.get(r.structure_id),
            recommendation="hold",
            rationale=_NO_JUDGEMENT,
            review_id=wl_entry.id if w is not None and wl_entry is not None else None,
        )
        pending.append(case)
    # Research reviews first, then by structure id (stable); cap the case count.
    pending.sort(key=lambda c: (c.triggers[0].kind != "research_review", c.structure_id))
    over = pending[settings.quant_exit_max_cases :]
    cases = pending[: settings.quant_exit_max_cases]
    j = _journal(ctx, ctx.snapshot.id)
    for sid, why in sorted(skip.items()):
        j.add(
            JournalPersona.QUANT,
            Stage.EXIT,
            sid,
            Choice.NOTED,
            ReasonCode.EXIT_CASE_SKIPPED,
            reason_text=why,
            payload={"detail": why},
        )
    for c in over:
        j.add(
            JournalPersona.QUANT,
            Stage.EXIT,
            c.structure_id,
            Choice.NOTED,
            ReasonCode.EXIT_CASE_SKIPPED,
            reason_text="over_case_limit",
            payload={"detail": "over_case_limit", "limit": settings.quant_exit_max_cases},
        )
    skipped = Counter(skip.values())
    if not cases:
        ctx.conn.commit()
        msg = "no exit cases"
        raise JobSkippedError(msg, continue_chain=True)
    cap = settings.quant_exit_case_max_chars
    inputs = {
        "cases_block": "\n".join(_case_block(c, cap) for c in cases),
        "policy_summary": "\n".join(
            sorted({exits.policy_for(None).summary(), *(_policy_line(exits, c) for c in cases)})
        ),
        "scan_date": day,
        "rules": [
            "Return exactly one entry per case structure_id: "
            + ", ".join(c.structure_id for c in cases)
            + "; recommendation hold | close; rationale <= 400 chars.",
        ],
    }
    call_id: str | None = None
    got: dict[str, tuple[str, str]] = {}
    failed: str | None = None
    try:
        reply, out = _ask(ctx, env, "quant_exit", ctx.snapshot, inputs, QuantExitOutput)
    except PersonaError as exc:  # shadow never fails the open chain: every case holds
        log.warning("pipeline.quant_exit_failed", error=str(exc))
        failed = str(exc)[:200]
    else:
        call_id = _record_ok(ctx, "quant_exit", reply, ctx.snapshot.id, Counter())
        for jd in out.cases:
            if jd.structure_id in {c.structure_id for c in cases} and jd.structure_id not in got:
                got[jd.structure_id] = (jd.recommendation, jd.rationale)
    no_call = f"Quant call failed (fail closed to hold): {failed}"[:400] if failed else ""
    default = ("hold", no_call or _NO_JUDGEMENT)
    judged: list[ExitCasePayload] = []
    for c in cases:
        rec, why = got.get(c.structure_id, default)
        c2 = c.model_copy(update={"recommendation": rec, "rationale": why})
        judged.append(c2)
        ctx.write("exit_case", c2.structure_id, c2)
        j.add(
            JournalPersona.QUANT,
            Stage.EXIT,
            c2.structure_id,
            Choice.NOTED,
            ReasonCode.EXIT_CASE_BUILT,
            reason_text=f"{c2.ticker}: {rec}: {why}",
            persona_call_id=call_id,
            payload=c2.model_dump(mode="json") | {"mode": "research", "proposed": False},
        )
        _note(
            ctx,
            c2.structure_id,
            persona="quant",
            topic=NoteTopic.EXIT_WATCH,
            title=f"{c2.ticker} exit case: {rec}",
            sections=[("Exits", why)],
            facts={"recommendation": rec},
            about=[],
        )
    ctx.conn.commit()
    closes = sum(c.recommendation == "close" for c in judged)
    return JobResult(
        summary=(
            f"{len(judged)} exit case(s) judged ({closes} close); Risk reviews next"
            + (f"; {sum(skipped.values())} skipped" if skipped else "")
        ),
        metrics={
            "exit_cases": len(judged),
            "exit_close": closes,
            "exit_hold": len(judged) - closes,
            "exit_over_limit": len(over),
            "exit_missing_judgement": sum(c.structure_id not in got for c in cases),
            "exit_llm_failed": failed is not None,
            **{f"exit_skipped_{k}": n for k, n in sorted(skipped.items())},
        },
        card=quant_exit_card(
            judged,
            shadow=False,
            skipped=dict(skipped),
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


def _policy_line(exits: ExitConfig, case: ExitCase) -> str:
    try:
        kind = StructureKind(case.kind) if case.kind else None
    except ValueError:
        kind = None
    return f"{case.kind or 'default'}: {exits.policy_for(kind).summary()}"


# ---------------------------------------------------------------------------
# risk.exit + the quant.propose close branch (E13.18 / D56)
# ---------------------------------------------------------------------------

#: Per-case prompt clip for ``risk.exit`` (the card: cases <= 500 chars each).
RISK_EXIT_CASE_MAX_CHARS = 500
_HOLD_KEY = "exit_hold:{sid}:{kind}"
_NO_VERDICT = "no verdict: held (fail closed)"
_DISCRETIONARY_TRIGGERS = frozenset({"profit_target", "time_adjusted_target", "remaining_ev_floor"})


def _chain_entries(ctx: JobContext, kind: str) -> list[ContextEntry]:
    """This chain's entries of *kind* (latest per subject); any chain for a lone run.

    ``exit_case`` / ``risk_exit_review`` live 30 m, so a later loop must never act
    on an earlier loop's cases or verdicts.
    """
    out: dict[str, ContextEntry] = {}
    for e in ctx.snapshot.of_kind(kind):
        if ctx.chain_run_id is None or e.chain_run_id == ctx.chain_run_id:
            out[e.subject] = e
    return [out[k] for k in sorted(out)]


def _hold_count(conn: sqlite3.Connection, sid: str, kind: str) -> int:
    from arc.routines.runs import RoutineStateRepo

    val = RoutineStateRepo(conn).get(_HOLD_KEY.format(sid=sid, kind=kind))
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


def _reset_holds(conn: sqlite3.Connection, sid: str, keep: Sequence[str] = ()) -> None:
    """Drop *sid*'s hold streaks, except the signal kinds in *keep* (still firing)."""
    prefix = _HOLD_KEY.format(sid=sid, kind="")
    keys = {f"{prefix}{k}" for k in keep}
    with conn:
        for (key,) in conn.execute(
            "SELECT key FROM routine_state WHERE key >= ? AND key < ?", (prefix, prefix + "\uffff")
        ).fetchall():
            if key not in keys:
                conn.execute("DELETE FROM routine_state WHERE key = ?", (key,))


def _signal_kinds(case: ExitCase) -> list[str]:
    """The case's deterministic discretionary signal kinds (the hold limit applies)."""
    return [t.kind for t in case.triggers if t.kind in _DISCRETIONARY_TRIGGERS]


def risk_exit(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """``risk.exit`` (E13.18, D56): Risk's ``close`` | ``hold`` on this chain's exit cases.

    Deterministic around one Risk
    call: no case in this chain -> skipped without an LLM call; a case Risk omits is
    ``hold`` (fail closed); an LLM error or schema failure writes the review with
    ``unavailable=True`` (every case ``hold``) and ``quant.propose`` applies the
    fallback (a deterministic discretionary signal closes as the D23 policy would).
    Nothing is proposed here: ``quant.propose`` turns the verdicts into closes.
    """
    from arc.routines.handlers import JobSkippedError
    from arc.slack.digests import risk_exit_card

    entries = _chain_entries(ctx, "exit_case")
    if not entries:
        msg = "no exit cases in this chain"
        raise JobSkippedError(msg, continue_chain=True)
    cases = [ExitCasePayload.model_validate(e.payload) for e in entries]
    settings = ctx.settings
    limit = settings.exit_review_max_consecutive_holds
    pc = _latest(ctx.snapshot, "portfolio_context", PortfolioContext)
    blocks = []
    for c in cases:
        text = (
            _case_block(c, RISK_EXIT_CASE_MAX_CHARS)
            + f"\n  quant: {c.recommendation}: {' '.join(c.rationale.split())}"
        )
        cap = RISK_EXIT_CASE_MAX_CHARS
        blocks.append(text if len(text) <= cap else text[: cap - 1] + "…")
    hold_lines = [
        f"- {c.structure_id} {c.ticker} {k}: held {_hold_count(ctx.conn, c.structure_id, k)}"
        f"/{limit} consecutive review(s)"
        for c in cases
        for k in _signal_kinds(c)
    ]
    ids = [c.structure_id for c in cases]
    inputs = {
        "cases_block": "\n".join(blocks),
        "portfolio_block": "" if pc is None or pc.empty else render_portfolio_context(pc, settings),
        "hold_state": "\n".join(hold_lines),
        "scan_date": _today(ctx).isoformat(),
        "rules": [
            "Return exactly one verdict per case structure_id: " + ", ".join(ids) + ".",
            "verdict close | hold; reason_code from the listed codes; reason <= 240 chars.",
        ],
    }
    j = _journal(ctx, ctx.snapshot.id)
    call_id: str | None = None
    failed: str | None = None
    got: dict[str, RiskExitVerdict] = {}
    try:
        reply, out = _ask(ctx, env, "risk_exit", ctx.snapshot, inputs, RiskExitOutput)
    except PersonaError as exc:  # fail closed to hold; quant.propose applies the fallback
        log.warning("pipeline.risk_exit_failed", error=str(exc))
        failed = str(exc)[:200]
    else:
        call_id = _record_ok(ctx, "risk_exit", reply, ctx.snapshot.id, Counter())
        for v in out.verdicts:
            if v.structure_id in ids and v.structure_id not in got:
                got[v.structure_id] = v
    verdicts: dict[str, RiskExitVerdict] = {}
    for c in cases:
        v = got.get(c.structure_id)
        if v is None:
            why = f"Risk unavailable: held (fail closed): {failed}" if failed else _NO_VERDICT
            v = RiskExitVerdict(
                structure_id=c.structure_id,
                verdict="hold",
                reason_code="thesis_intact",
                reason=why[:240],
            )
        verdicts[c.structure_id] = v
        j.add(
            JournalPersona.RISK,
            Stage.EXIT,
            c.structure_id,
            Choice.NOTED,
            ReasonCode.EXIT_REVIEW_UNAVAILABLE if failed else ReasonCode.EXIT_RESEARCH_REVIEW,
            reason_text=f"{c.ticker}: {v.verdict} ({v.reason_code}): {v.reason}",
            persona_call_id=call_id,
            payload={
                "case": c.model_dump(mode="json"),
                "verdict": v.model_dump(mode="json"),
                "unavailable": failed is not None,
            },
        )
    review = RiskExitReviewPayload(
        as_of=ctx.now.isoformat(),
        case_ids=[e.id for e in entries],
        verdicts=list(verdicts.values()),
        unavailable=failed is not None,
        persona_call_id=call_id,
    )
    ctx.write("risk_exit_review", SESSION_SUBJECT, review)
    ctx.conn.commit()
    closes = sum(v.verdict == "close" for v in verdicts.values())
    return JobResult(
        summary=(
            f"{len(cases)} exit case(s) reviewed ({closes} close)"
            + (f"; Risk unavailable (fail closed to hold): {failed}" if failed else "")
        ),
        metrics={
            "exit_reviewed": len(cases),
            "exit_review_close": closes,
            "exit_review_hold": len(cases) - closes,
            "exit_review_missing": sum(c.structure_id not in got for c in cases),
            "exit_review_unavailable": failed is not None,
        },
        card=risk_exit_card(
            cases,
            verdicts,
            unavailable=failed is not None,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


def _propose_exits(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """The ``quant.propose`` close branch (E13.18).

    Per open position (deterministic; the LLM verdicts are inputs):

    * a mandatory signal, a pending exit or an exit already proposed today -> nothing
      (``exits.mandatory`` owns mandatory signals);
    * Risk ``close`` on this chain's case -> ``propose_close`` (gate ``closing=True``,
      band, token, approval card) with the verdict attached; a ``capacity`` close on a
      swap case opens a ``closing`` swap first (the open leg follows the fill);
    * Risk ``hold`` -> ``exit:hold_reviewed``; with a deterministic discretionary
      signal the hold is honoured for ``exit_review_max_consecutive_holds`` reviews,
      the next one closes (``exit:hold_limit_reached``);
    * no Risk review (``risk.exit`` unavailable, or the exit steps were skipped / the
      case was over the case limit): a deterministic discretionary signal closes as
      today (``exit:review_unavailable``); a Research-review / swap-only case holds.

    Then every ``closing`` swap advances (close filled -> the open is proposed).
    Under a total halt nothing is proposed (today's ``exits()`` rule).
    """
    from arc.execution.exits import exit_pending, price_close, propose_close
    from arc.positions.evaluate import PositionReview as _Review
    from arc.positions.exit_case import DISCRETIONARY_KINDS, case_skip_reason
    from arc.positions.steps import (
        SIGNAL_CODES,
        _advance_swaps,
        _book,
        _cancel,
        _gate_secret,
        capacity_candidates,
    )
    from arc.routines.runs import RoutineStateRepo
    from arc.store.execution import OpenStructureRepo
    from arc.store.swaps import SwapRepo

    settings = ctx.settings
    limit = settings.exit_review_max_consecutive_holds
    repo = OpenStructureRepo(ctx.conn)
    rows = {str(r["id"]): r for r in repo.list_open()}
    swaps = SwapRepo(ctx.conn)
    pending_swaps = swaps.by_status("closing")
    reviews: dict[str, PositionReview] = {}
    for e in ctx.snapshot.of_kind("position_review"):
        r = _Review.model_validate(e.payload)
        if r.structure_id in rows:
            reviews[r.structure_id] = r
    cases = {
        e.subject: ExitCasePayload.model_validate(e.payload)
        for e in _chain_entries(ctx, "exit_case")
        if e.subject in rows
    }
    rv_entries = _chain_entries(ctx, "risk_exit_review")
    risk = RiskExitReviewPayload.model_validate(rv_entries[-1].payload) if rv_entries else None
    verdicts = (
        {v.structure_id: v for v in risk.verdicts}
        if risk is not None and not risk.unavailable
        else {}
    )
    unavailable = risk is None or risk.unavailable
    day = _today(ctx).isoformat()
    # Positions to settle: every case, plus every unreviewed discretionary signal.
    todo: list[str] = sorted(
        set(cases)
        | {
            sid
            for sid, r in reviews.items()
            if any(s.kind in DISCRETIONARY_KINDS for s in r.signals)
        }
    )
    # A hold streak ends when its signal clears.
    for sid in reviews:
        if sid not in todo:
            _reset_holds(ctx.conn, sid)
    if not todo and not pending_swaps:
        return JobResult(summary="no exit cases", metrics={"exit_closes": 0, "exit_holds": 0})
    j = _journal(ctx, ctx.snapshot.id)
    secret = _gate_secret(ctx, env)
    book = _book(ctx, env)
    _info, account, portfolio, switch = book
    if switch.is_halted():
        return JobResult(
            summary=f"halted: {len(todo)} exit case(s) not proposed",
            metrics={"exit_closes": 0, "exit_holds": 0, "exit_halted": True},
        )
    state = RoutineStateRepo(ctx.conn)
    _cands, sources = capacity_candidates(ctx.conn, day, taken=swaps.sources())
    lines: list[str] = []
    alerts: list[str] = []
    closes = holds = fallbacks = 0

    def note(sid: str, code: ReasonCode, text: str, **payload: Any) -> None:
        with ctx.conn:
            j.add(
                JournalPersona.QUANT,
                Stage.EXIT,
                sid,
                Choice.NOTED,
                code,
                reason_text=text,
                payload=payload or None,
            )

    for sid in todo:
        row = rows[sid]
        rv = reviews.get(sid)
        case = cases.get(sid)
        ticker = str(row["ticker"])
        if rv is None:  # a case without fresh marks: nothing to price against
            note(sid, ReasonCode.EXIT_CASE_SKIPPED, "no position review", detail="no_review")
            continue
        skip = case_skip_reason(
            rv, today_exit=exit_pending(ctx.conn, row) or row.get("exit_day") == day
        )
        if skip is not None:
            note(sid, ReasonCode.EXIT_CASE_SKIPPED, skip, detail=skip)
            continue
        signals = [s for s in rv.signals if s.kind in DISCRETIONARY_KINDS]
        kinds = [s.kind.value for s in signals]
        verdict = verdicts.get(sid) if case is not None else None
        if case is not None and verdict is None and not unavailable:
            verdict = RiskExitVerdict(
                structure_id=sid, verdict="hold", reason_code="thesis_intact", reason=_NO_VERDICT
            )
        case_payload = case.model_dump(mode="json") if case is not None else None
        code: ReasonCode
        swap_close = False
        if verdict is None:
            # Risk did not review it: the D23 policy decides (discretionary signal only).
            if not signals:
                holds += 1
                note(
                    sid,
                    ReasonCode.EXIT_REVIEW_UNAVAILABLE,
                    f"{ticker}: Risk review unavailable; no deterministic signal: held",
                    case=case_payload,
                )
                lines.append(f"{ticker}: held (review unavailable)")
                continue
            code = ReasonCode.EXIT_REVIEW_UNAVAILABLE
            why = f"Risk review unavailable: policy {kinds[0]} ({signals[0].detail})"
            fallbacks += 1
        elif verdict.verdict == "hold":
            over = [k for k in kinds if _hold_count(ctx.conn, sid, k) >= limit]
            if not over:
                for k in kinds:
                    key = _HOLD_KEY.format(sid=sid, kind=k)
                    state.set(key, str(_hold_count(ctx.conn, sid, k) + 1), now=ctx.now)
                _reset_holds(ctx.conn, sid, keep=kinds)
                holds += 1
                note(
                    sid,
                    ReasonCode.EXIT_HOLD_REVIEWED,
                    f"{ticker}: Risk holds ({verdict.reason_code}): {verdict.reason}",
                    case=case_payload,
                    risk_review=verdict.model_dump(mode="json"),
                    holds={k: _hold_count(ctx.conn, sid, k) for k in kinds},
                    limit=limit,
                )
                lines.append(f"{ticker}: held ({verdict.reason_code})")
                continue
            code = ReasonCode.EXIT_HOLD_LIMIT
            why = f"hold limit: {over[0]} held for {limit} consecutive review(s)"
        else:
            swap_close = (
                case is not None
                and case.swap is not None
                and verdict.reason_code == "capacity"
                and case.swap.source_ref in sources
            )
            if swap_close:
                code = ReasonCode.EXIT_REALLOCATE
            elif signals:
                code = SIGNAL_CODES[signals[0].kind]
            else:
                code = ReasonCode.EXIT_RESEARCH_REVIEW
            why = f"Risk close ({verdict.reason_code}): {verdict.reason}"
        st = Structure.model_validate_json(row["structure_json"])
        try:
            priced = price_close(
                env.market, st, as_of=_today(ctx), r=settings.scanner_risk_free_rate
            )
        except (LookupError, ValueError) as exc:
            lines.append(f"{ticker}: cannot price close ({exc})")
            continue
        swap_id: str | None = None
        if swap_close:
            assert case is not None and case.swap is not None  # noqa: S101 - swap_close
            sw = case.swap
            with ctx.conn:
                swap_id = swaps.create(
                    day=day, status="closing", close_structure_id=sid, close_ticker=ticker,
                    open_ticker=sw.open_ticker, source_ref=sw.source_ref,
                    suggestion_json=json.dumps({
                        "suggestion": sw.model_dump(mode="json"),
                        "source": sources[sw.source_ref],
                        "risk": verdict.reason if verdict is not None else "",
                    }),
                    now=ctx.now, run_id=ctx.run_id, detail=why, commit=False,
                )  # fmt: skip
        reason = "reallocate" if swap_close else (kinds[0] if signals else "research_review")
        exit_review = verdict

        def write_ctx(kind: str, subject: str, p: Any, _v: Any = exit_review) -> Any:
            return ctx.write(
                kind, subject, ProposalPayload.model_validate(p.model_dump() | {"exit_review": _v})
            )

        res = propose_close(
            ctx.conn,
            row=row,
            priced=priced,
            thesis=f"Exit ({reason}): {why}; structure {sid}",
            reason=reason,
            reason_code=code,
            persona=JournalPersona.QUANT,
            close_now_net=rv.close_now_net,
            settings=settings,
            account=account,
            portfolio=portfolio,
            switch=switch,
            now=ctx.clock(),
            run_id=ctx.run_id,
            write_context=write_ctx,
            secret=secret,
            payload={
                "case": case_payload,
                "risk_review": verdict.model_dump(mode="json") if verdict is not None else None,
                "path": "research",
            },
            swap_id=swap_id,
        )
        if res.alert:
            alerts.append(res.alert)
        if swap_id is not None:
            swaps.update(
                swap_id, status="closing", now=ctx.now, close_proposal_hash=res.proposal_hash
            )
            if res.proposal_hash is None:
                _cancel(ctx, swaps.get(swap_id) or {}, "close quotes unusable")
            elif not res.passed:
                _cancel(ctx, swaps.get(swap_id) or {}, "close failed the gate")
        if res.proposal_hash is None:  # E6.2a: quotes unusable; the next loop retries
            lines.append(res.line)
            continue
        _reset_holds(ctx.conn, sid)
        closes += 1
        lines.append(f"{res.line} ({why})")
    if pending_swaps:
        lines.extend(_advance_swaps(ctx, env, book, secret))
    notices = [f"exit proposed: {line}" for line in lines if "gate PASS" in line] + alerts
    return JobResult(
        summary="; ".join(lines) or "no exits proposed",
        metrics={
            "exit_closes": closes,
            "exit_holds": holds,
            "exit_fallback_closes": fallbacks,
            "exit_review_unavailable": unavailable and bool(cases or fallbacks),
        },
        notice="; ".join(notices),
    )


# ---------------------------------------------------------------------------
# Handler entry points
# ---------------------------------------------------------------------------

_STEPS: dict[str, Callable[[JobContext, PipelineEnv], JobResult]] = {
    "research": research,
    "quant.open": quant_open,
    "risk.open": risk_open,
    "quant.revise": quant_revise,
    "quant.propose": quant_propose,
    "quant.exit": quant_exit,
    "risk.exit": risk_exit,
}


class _LazyEnv:
    """Builds the live env on first use, so empty-context runs never touch Alpaca/Hermes."""

    def __init__(self, ctx: JobContext) -> None:
        self._ctx = ctx
        self._env: PipelineEnv | None = None

    def __getattr__(self, name: str) -> Any:
        if self._env is None:
            from arc.pipeline.env import PipelineEnv

            self._env = PipelineEnv.live(
                self._ctx.settings, conn=self._ctx.conn, chain_run_id=self._ctx.chain_run_id
            )
        return getattr(self._env, name)


def _live_env(ctx: JobContext) -> PipelineEnv:
    return cast("PipelineEnv", _LazyEnv(ctx))


def research_step(ctx: JobContext) -> JobResult:
    return research(ctx, _live_env(ctx))


def quant_open_step(ctx: JobContext) -> JobResult:
    return quant_open(ctx, _live_env(ctx))


def risk_open_step(ctx: JobContext) -> JobResult:
    return risk_open(ctx, _live_env(ctx))


def quant_revise_step(ctx: JobContext) -> JobResult:
    return quant_revise(ctx, _live_env(ctx))


def quant_propose_step(ctx: JobContext) -> JobResult:
    return quant_propose(ctx, _live_env(ctx))


def quant_exit_step(ctx: JobContext) -> JobResult:
    return quant_exit(ctx, _live_env(ctx))


def risk_exit_step(ctx: JobContext) -> JobResult:
    return risk_exit(ctx, _live_env(ctx))


def _taped(env: PipelineEnv, ctx: JobContext) -> PipelineEnv:
    """An offline *env* with the E10.2 market tape on the step's store (as live has it).

    :meth:`PipelineEnv.live` wraps its market itself; an offline (fixture) env is
    wrapped here per step, so a fixture control loop records its reads while an arm
    may pair and a fixture arm chain replays them, aged on the step's clock (E10.2d).
    """
    if not env.offline:
        return env
    from arc.experiments.tape import tape_market

    market = tape_market(ctx.conn, ctx.chain_run_id, env.market, clock=ctx.clock)
    return env if market is env.market else dataclasses.replace(env, market=market)


def pipeline_handlers(env: PipelineEnv) -> dict[str, Handler]:
    """Dispatcher overrides binding every E5.2 step (and the Scalp) to one *env*."""
    from arc.routines.handlers import scalp_persona

    def bind(fn: Callable[[JobContext, PipelineEnv], JobResult]) -> Handler:
        return lambda ctx: fn(ctx, _taped(env, ctx))

    handlers: dict[str, Handler] = {name: bind(fn) for name, fn in _STEPS.items()}
    # E13.18: the mandatory-exit floor runs inside the Research chain.
    from arc.positions.steps import exits_mandatory

    handlers["exits.mandatory"] = bind(exits_mandatory)
    if env.scalp_llm is not None or env.universe_guard is not None:
        scalp_llm, make_guard = env.scalp_llm, env.universe_guard

        def scalp(ctx: JobContext) -> JobResult:
            guard = make_guard(ctx.settings, ctx.now) if make_guard is not None else None
            return scalp_persona(ctx, llm=scalp_llm, guard=guard)

        handlers["scalp"] = scalp
    return handlers
