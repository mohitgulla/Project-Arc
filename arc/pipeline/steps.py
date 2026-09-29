"""E5.2 pipeline steps: Director → Quant → Risk → propose (+ gate), as routine handlers.

Each step is a D16 routine handler. It reads the context snapshot the dispatcher
recorded for its run and writes typed context entries through ``ctx.write``.
Steps never hand results to each other in memory. The chain order lives in
``config/routines.yaml`` (``director: chain: [quant, risk, propose]``).

What each step does:

``director`` (LLM, frontier tier)
    Computes regime/vol features for today's candidates and writes them as
    ``regime`` entries. It then records a second snapshot and asks the Director
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
    Evidence,
    NotePayload,
    NoteTopic,
    ProposalPayload,
    RegimePayload,
    RiskReviewPayload,
    ShortlistPayload,
    StructuresPayload,
)
from arc.context.store import ContextStore
from arc.control.effective import cost_model as cost_config
from arc.control.effective import exit_config
from arc.exits import ExitSummary, model_exits, realized_vol_forecast
from arc.ingest.llm import ScoutLLMError
from arc.ingest.scout import extract_json_object
from arc.journal.models import LegQuote, MarketContext, PersonaCallMeta
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage, gate_reason
from arc.journal.store import JournalStore, Recorder
from arc.models import LegIntent, Proposal, QuantMetrics, Sizing, Stance
from arc.personas.builders import (
    build_director_prompt,
    build_quant_prompt,
    build_risk_prompt,
    build_risk_swap_prompt,
    director_input_from_context,
    quant_input_from_context,
    risk_input_from_context,
    risk_swap_input_from_context,
)
from arc.personas.schemas import (
    DirectorExclusion,
    DirectorOutput,
    DirectorRankedItem,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantSkip,
    QuantStructureOut,
    RiskAssessment,
    RiskOutput,
    RiskSwapReview,
)
from arc.pipeline.analytics import build_analytics
from arc.pipeline.budget import BudgetView, budget_notice, read_budget
from arc.pipeline.market import (
    PortfolioError,
    account_snapshot,
    build_portfolio,
    limit_price,
    market_snapshot,
    next_earnings,
    price_structure,
)
from arc.pipeline.store import PersonaCallRepo
from arc.routines.handlers import JobResult
from arc.routines.runs import RoutineRunRepo
from arc.sizing import size_contracts
from arc.slack.blocks import esc
from arc.slack.digests import director_card, quant_card, risk_card
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from arc.backtest.costs import CostModel
    from arc.broker.base import AccountInfo, BrokerPosition
    from arc.config import ArcSettings
    from arc.context.store import ContextSnapshot
    from arc.exits import ExitConfig, ExitModelResult
    from arc.gate.band import PriceBand
    from arc.gate.inputs import Portfolio
    from arc.pipeline.env import PipelineEnv
    from arc.pipeline.market import PricedStructure
    from arc.routines.handlers import Handler, JobContext
    from arc.scanner import ScanCandidate

log = structlog.get_logger(__name__)

__all__ = [
    "PersonaError",
    "build_prompt",
    "director",
    "director_step",
    "pipeline_handlers",
    "propose",
    "propose_step",
    "quant",
    "quant_step",
    "risk",
    "risk_step",
]

SESSION_SUBJECT = "session"
STRUCTURE_TYPES = frozenset({"vertical_spread", "iron_condor", "long_call", "long_put"})
DIRECTOR_READS = [
    "candidate",
    "regime",
    "channel_brief",
    "note",
    "vol_term",
    "put_call",
    "macro_calendar",
    "unusual_options",
]  # == routines.yaml director.reads (D30 adds the options-data kinds)

# Drop reasons (stable keys; stored in persona_calls.dropped and step metrics).
DROP_NOT_CANDIDATE = "not_a_candidate"
DROP_DUPLICATE = "duplicate"
DROP_BAD_FIELD = "invalid_field"
DROP_NOT_IN_MENU = "not_in_menu"
DROP_NOT_SHORTLISTED = "not_shortlisted"
DROP_UNKNOWN_STRUCTURE = "unknown_structure"
# E5.7 funnel outcomes (card keys; journal codes in arc.journal.reasons)
FUNNEL_EXCLUDED = "excluded"  # Director excluded it, with a reason
FUNNEL_NOT_RANKED = "not_picked"  # neither ranked nor excluded (no reason given)
FUNNEL_OVER_BUDGET = "over_budget"  # ranked beyond pipeline_max_shortlist
FUNNEL_SKIPPED = "skipped"  # Quant skipped it, with a reason
FUNNEL_NOT_STRUCTURED = "not_structured"  # no structure and no reason from Quant


class PersonaError(RuntimeError):
    """A persona call failed (transport or unparseable reply). The step fails and the
    chain stops. ``arc routines run director --chain`` resumes from here."""


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
    body: str,
    about: list[str],
    stance: Stance | None = None,
    confidence: float | None = None,
    evidence: list[Evidence] | None = None,
) -> str | None:
    """Write a D27 ``note`` for persona narrative that would otherwise be discarded.

    Truncates to the model limits. An invalid note is logged (``pipeline.note_invalid``)
    and skipped: a note never fails its step. A contract violation still does.
    """
    body = body.strip()
    if not body:
        return None
    try:
        payload = NotePayload(
            persona=persona,  # type: ignore[arg-type]
            topic=topic,
            title=(title.strip() or topic.value)[:120],
            body=body[:4000],
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
    "director": (director_input_from_context, build_director_prompt, DirectorOutput),
    "quant": (quant_input_from_context, build_quant_prompt, QuantOutput),
    "risk": (risk_input_from_context, build_risk_prompt, RiskOutput),
    # E6.4 risk.reallocate: the Risk persona's close-to-reallocate review (veto only)
    "risk_swap": (risk_swap_input_from_context, build_risk_swap_prompt, RiskSwapReview),
}

# prompt key → the persona whose LLM answers it (config/llm_routing.yaml)
LLM_PERSONA = {"risk_swap": "risk"}


def build_prompt(persona: str, snapshot: ContextSnapshot, inputs: Mapping[str, Any]) -> str:
    """The exact prompt a persona step sends, from its snapshot plus recorded inputs.

    ``inputs`` holds everything that is not in the context snapshot (portfolio,
    chains, scan date, the hard-constraint lines). The steps call this, and so
    does ``arc journal replay``, so a replay rebuilds the prompt through the
    same code path and its sha256 must match ``persona_calls.prompt_sha256``.
    """
    from_context, builder, schema = PROMPT_BUILDERS[persona]
    kwargs = {k: v for k, v in inputs.items() if k != "rules"}
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
    except ScoutLLMError as exc:
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
# Director
# ---------------------------------------------------------------------------


def _regime_entries(ctx: JobContext, env: PipelineEnv, tickers: list[str]) -> list[str]:
    """Write today's regime/vol features for *tickers* lacking one; return tickers written."""
    from arc.features.snapshot import build_snapshot_from_bars

    today = _today(ctx)
    written: list[str] = []
    for t in tickers:
        have = ctx.snapshot.latest("regime", t)
        if have is not None and have.payload.get("as_of") == today.isoformat():
            continue
        try:
            bars = env.market.history_bars(t, today - _dt.timedelta(days=400), today)
            ctx.record_input(f"bars:{t}", _source(env), bars, as_of=ctx.now, count=len(bars))
            snap = build_snapshot_from_bars(t, bars, today)
        except Exception as exc:  # noqa: BLE001 - features are context, not a gate input
            log.warning("pipeline.regime_failed", ticker=t, error=str(exc))
            continue
        ctx.write("regime", t, RegimePayload.model_validate(snap.model_dump()))
        written.append(t)
    return written


def _filter_shortlist(
    out: DirectorOutput, candidates: Mapping[str, Stance]
) -> tuple[list[DirectorRankedItem], Counter[str], list[tuple[DirectorRankedItem, str]]]:
    """Kept items, drop counts, and each dropped item with its reason.

    E5.7: no count cap here. The Director ranks every candidate it would trade;
    ``pipeline_max_shortlist`` is the Quant/Risk budget, applied by :func:`quant`.
    """
    dropped: Counter[str] = Counter()
    rejected: list[tuple[DirectorRankedItem, str]] = []
    kept: list[DirectorRankedItem] = []
    seen: set[str] = set()

    def drop(item: DirectorRankedItem, reason: str) -> None:
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
    out: DirectorOutput, candidates: Mapping[str, Stance], ranked: set[str]
) -> list[DirectorExclusion]:
    """The Director's exclusions that name a real, un-ranked candidate (first wins)."""
    kept: dict[str, DirectorExclusion] = {}
    for ex in out.excluded:
        t = ex.ticker.strip().upper()
        if t in candidates and t not in ranked and t not in kept and ex.reason.strip():
            kept[t] = DirectorExclusion(ticker=t, reason=ex.reason.strip()[:300])
    return list(kept.values())


def _scout_evidence(snapshot: ContextSnapshot) -> dict[str, str]:
    """ticker -> one display line of the Scout data behind a pick (digest card only)."""
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
            f"Scout {p.get('stance', '?')} · {p.get('catalyst_type', '?')} catalyst{when} · "
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


def _director_rules(
    cands: Mapping[str, Stance],
    settings: ArcSettings | None = None,
    budget: BudgetView | None = None,
) -> list[str]:
    """Director constraints (E5.7: rank, don't gatekeep; no count cap in the prompt).

    D32: in the restrictive order-budget tier an advisory line is added; the
    deterministic cap is the lowered Quant/Risk budget (:func:`_shortlist_limit`).
    """
    rules = [
        f"shortlist tickers MUST come from the candidates above: {', '.join(sorted(cands))}.",
        "Rank every candidate you would consider trading, best first (rank 1 = highest "
        "conviction), each with thesis, regime context and the evidence behind it. "
        "Do not cap the list yourself.",
        "Exclude a candidate only with a one-line reason in `excluded` "
        "({ticker, reason}); every candidate is either ranked or excluded.",
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
    return rules


def _shortlist_limit(settings: ArcSettings, budget: BudgetView) -> int:
    """Quant/Risk budget: the restrictive tier's lower cap once ``used >= restrict_at`` (D32)."""
    limit = settings.pipeline_max_shortlist
    if budget.tier.restricted:
        limit = min(limit, settings.order_budget_restrictive_director_max_shortlist)
    return limit


def director(ctx: JobContext, env: PipelineEnv) -> JobResult:
    settings = ctx.settings
    cand_entries = ctx.snapshot.of_kind("candidate")
    cands = {e.subject: Stance(e.payload["stance"]) for e in cand_entries}
    if not cands:
        j = _journal(ctx, ctx.snapshot.id)
        j.add(
            JournalPersona.DIRECTOR,
            Stage.SHORTLIST,
            SESSION_SUBJECT,
            Choice.NO_TRADE,
            ReasonCode.NO_CANDIDATES,
            reason_text="no active Scout candidates",
        )
        ctx.write(
            "shortlist",
            SESSION_SUBJECT,
            ShortlistPayload(shortlist=[], market_regime="unknown", session_notes="no candidates"),
        )
        return JobResult(summary="no active candidates; empty shortlist", metrics={"shortlist": 0})

    # D32: the day's order budget decides how much the entry chain may do.
    budget = read_budget(ctx, env, settings, now=ctx.clock())
    notice = budget_notice(ctx, budget.budget)
    if not budget.tier.opens_allowed:
        # Opens are exhausted: stop the entry chain here, before any LLM spend.
        # Quant/Risk/propose see an empty shortlist and do nothing.
        why = f"order budget {budget.tier.value}: {budget.budget.summary()}; no new opens"
        j = _journal(ctx, ctx.snapshot.id)
        j.add(
            JournalPersona.DIRECTOR,
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

    regimes = _regime_entries(ctx, env, sorted(cands))
    # Re-read (and record) the context now that today's regime entries exist.
    snap = ContextStore(ctx.conn).snapshot(ctx.now, kinds=DIRECTOR_READS, run_id=ctx.run_id)
    RoutineRunRepo(ctx.conn).set_inputs(ctx.run_id, [ctx.snapshot.id, snap.id])

    summary, _ = _portfolio_summary(ctx, env, settings)
    # Quant/Risk budget (never shown to the Director); D32 lowers it in the restrictive tier.
    qr_budget = _shortlist_limit(settings, budget)
    inputs = {
        "portfolio_summary": summary,
        "scan_date": _today(ctx).isoformat(),
        "max_notes": settings.pipeline_max_context_notes,
        "rules": _director_rules(cands, settings, budget),
    }
    reply, out = _ask(ctx, env, "director", snap, inputs, DirectorOutput)
    kept, dropped, rejected = _filter_shortlist(out, cands)
    ranked = {i.ticker for i in kept}
    excluded = _filter_excluded(out, cands, ranked)
    call_id = _record_ok(ctx, "director", reply, snap.id, dropped)

    j = _journal(ctx, snap.id)
    for e in snap.of_kind("candidate"):  # what the Director was offered
        j.add(
            JournalPersona.SCOUT,
            Stage.CANDIDATE,
            e.subject,
            Choice.SELECTED,
            ReasonCode.SCOUT_CANDIDATE,
            confidence=e.payload.get("confidence"),
            payload=e.payload,
        )
    j.add(
        JournalPersona.DIRECTOR,
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
            JournalPersona.DIRECTOR,
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
            JournalPersona.DIRECTOR,
            Stage.SHORTLIST,
            item.ticker.strip().upper() or SESSION_SUBJECT,
            Choice.REJECTED,
            ReasonCode(reason),
            reason_text=item.thesis,
            confidence=item.confidence,
            persona_call_id=call_id,
            payload=item.model_dump(mode="json"),
        )
    for ex in excluded:
        j.add(
            JournalPersona.DIRECTOR,
            Stage.SHORTLIST,
            ex.ticker,
            Choice.REJECTED,
            ReasonCode.DIRECTOR_EXCLUDED,
            reason_text=ex.reason,
            persona_call_id=call_id,
        )
    accounted = ranked | {e.ticker for e in excluded}
    not_ranked = sorted(set(cands) - accounted)
    for t in not_ranked:
        j.add(
            JournalPersona.DIRECTOR,
            Stage.SHORTLIST,
            t,
            Choice.REJECTED,
            ReasonCode.NOT_RANKED,
            reason_text="neither ranked nor excluded by the Director (no reason given)",
            persona_call_id=call_id,
        )
    payload = ShortlistPayload(
        shortlist=kept,
        excluded=excluded,
        market_regime=out.market_regime,
        session_notes=out.session_notes,
        budget=qr_budget,
    )
    entry = ctx.write("shortlist", SESSION_SUBJECT, payload)
    _note(
        ctx,
        SESSION_SUBJECT,
        persona="director",
        topic=NoteTopic.REGIME_VIEW,
        title=f"Regime: {out.market_regime}",
        body=f"{out.market_regime}: {out.session_notes}".strip(": "),
        about=[entry.id],
    )
    for item in kept:
        _note(
            ctx,
            item.ticker,
            persona="director",
            topic=NoteTopic.THESIS,
            title=f"{item.ticker} {item.stance} thesis",
            body="\n\n".join(
                x
                for x in (
                    item.thesis,
                    item.regime_context,
                    "Evidence:\n" + "\n".join(f"- {e}" for e in item.evidence)
                    if item.evidence
                    else "",
                )
                if x.strip()
            ),
            about=[entry.id],
            stance=_stance(item.stance),
            confidence=item.confidence,
        )
    names = ", ".join(f"{i.ticker} ({i.stance})" for i in kept) or "none"
    drop_items = [(i.ticker.strip().upper() or "?", reason) for i, reason in rejected]
    funnel = [
        *[(e.ticker, FUNNEL_EXCLUDED, e.reason) for e in excluded],
        *[(t, FUNNEL_NOT_RANKED, "") for t in not_ranked],
    ]
    over = payload.over_budget()
    return JobResult(
        summary=f"{len(cands)} candidates → ranked {len(kept)}: {names}"
        + (f" (budget {qr_budget}; {len(over)} not structured)" if over else "")
        + (f"; excluded {len(excluded)}" if excluded else "")
        + (f"; dropped {dict(dropped)}" if dropped else ""),
        metrics={
            "shortlist": len(kept),
            "budgeted": len(payload.budgeted()),
            "over_budget": len(over),
            "excluded": len(excluded),
            "not_ranked": len(not_ranked),
            "regime_written": len(regimes),
            **dropped,
            **budget.metrics(),
        },
        notice=notice,
        card=director_card(
            payload,
            candidates=len(cands),
            dropped=drop_items,
            funnel=funnel,
            budget=qr_budget,
            evidence=_scout_evidence(snap),
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Quant
# ---------------------------------------------------------------------------


def _strategies(stance: str, settings: ArcSettings) -> list[Any]:
    """Scanner strategies for a Director stance under the active account profile (D25).

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


def quant(ctx: JobContext, env: PipelineEnv) -> JobResult:
    from arc.scanner import ScanParams, load_iv_history, scan

    settings = ctx.settings
    j = _journal(ctx, ctx.snapshot.id)
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    # E5.7: the Director ranks everything; only the first `budget` get a structure.
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
    exits = exit_config(settings)  # D26: exits.yaml + control-panel overrides
    summaries: dict[int, ExitSummary] = {}
    menus: dict[str, dict[frozenset[tuple[str, str]], ScanCandidate]] = {}
    chains: dict[str, Any] = {}
    spots: dict[str, float] = {}
    no_chain: list[str] = []
    no_chain_why: dict[str, str] = {}
    no_profile: list[tuple[str, str]] = []  # (ticker, stance) the profile cannot trade
    for item in shortlist.budgeted():
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
            history = load_iv_history(env.iv_history_dir, item.ticker) if env.iv_history_dir else {}
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
            body=s.rationale,
            about=[entry.id],
            confidence=s.confidence,
        )
    _note(
        ctx,
        SESSION_SUBJECT,
        persona="quant",
        topic=NoteTopic.OBSERVATION,
        title="Quant analysis",
        body=out.analysis_notes,
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


def risk(ctx: JobContext, env: PipelineEnv) -> JobResult:
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
        "rules": _risk_rules(settings, caps),
    }
    reply, out = _ask(ctx, env, "risk", ctx.snapshot, inputs, RiskOutput)
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

    accept(out.assessments)
    call_id = _record_ok(ctx, "risk", reply, ctx.snapshot.id, dropped)
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
            r_reply, r_out = _ask(ctx, env, "risk", ctx.snapshot, repair_inputs, RiskOutput)
        except PersonaError as exc:
            log.warning("pipeline.risk_repair_failed", missing=first_missing, error=str(exc))
        else:
            before = set(seen)
            r_dropped_before = Counter(dropped)
            accept(r_out.assessments)
            repaired = [f"{t} {k}" for t, k in sorted(seen - before)]
            r_id = _record_ok(ctx, "risk", r_reply, ctx.snapshot.id, dropped - r_dropped_before)
            log.info("pipeline.risk_repair", missing=first_missing, repaired=repaired, call=r_id)
            call_id = call_id or r_id
    for a in kept:
        declined = a.sizing_suggestion < 1
        j.add(
            JournalPersona.RISK,
            Stage.RISK_REVIEW,
            a.ticker,
            Choice.NO_TRADE if declined else Choice.ASSESSED,
            ReasonCode.RISK_DECLINED if declined else ReasonCode.RISK_ASSESSED,
            reason_text=f"{a.risk_rating}: {a.narrative}",
            persona_call_id=call_id,
            payload=a.model_dump(mode="json")
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
        body="\n\n".join(x for x in (out.advisory_notes, out.portfolio_summary) if x.strip()),
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
    desc = "; ".join(f"{a.ticker} {a.risk_rating}, suggests {a.sizing_suggestion}" for a in kept)
    missing_s = [f"{t} {k}" for t, k in missing]
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
        ),
    )


# ---------------------------------------------------------------------------
# propose (deterministic) + gate
# ---------------------------------------------------------------------------


def _existing(conn: sqlite3.Connection, day: str, ticker: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM proposals WHERE day = ? AND ticker = ? AND kind = 'open'", (day, ticker)
    ).fetchone()
    return row is not None


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
    portfolio: Portfolio, underlying: str, max_loss: Decimal, g: Any, n: int
) -> Portfolio:
    from arc.gate.inputs import Position
    from arc.models import Greeks

    pg = portfolio.greeks
    return portfolio.model_copy(
        update={
            "positions": [*portfolio.positions, Position(underlying=underlying, max_loss=max_loss)],
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
    """What ``risk.reallocate`` needs to re-propose a capacity-blocked entry (E6.4).

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
    )


def propose(ctx: JobContext, env: PipelineEnv) -> JobResult:
    from arc.gate.halt import HaltSwitch, evaluate_with_halt
    from arc.gate.rules import proposal_hash
    from arc.store.repos import GateDecisionRepo, HaltRepo, ProposalRepo

    settings = ctx.settings
    # E5.2b: ``ctx.now`` is the chain start (before the Director/Quant/Risk LLM
    # calls) and keys ``day`` idempotency only. Data age, the gate, the token and
    # the proposal's expiry use ``ctx.clock()``: read at step start, after the
    # account fetch, and again after each ticker's quotes are fetched.
    now = ctx.clock()
    day = _today(ctx).isoformat()
    j = _journal(ctx, ctx.snapshot.id)
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    structures = _latest(ctx.snapshot, "structures", StructuresPayload)
    review = _latest(ctx.snapshot, "risk_review", RiskReviewPayload)
    if not (shortlist and structures and review) or not structures.structures:
        with ctx.conn:
            j.add(
                JournalPersona.SYSTEM,
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

    info, positions = _account_inputs(ctx, env)
    fetched_at = ctx.clock()  # as_of = broker fetch time
    # D32: today's order count stamps the gate snapshot; the tier caps the ladder.
    budget = read_budget(ctx, env, settings, now=fetched_at)
    notice = budget_notice(ctx, budget.budget)
    settings = budget.settings  # tier-adjusted improvement steps (band, gate, token agree)
    account = account_snapshot(info, fetched_at, orders_used_today=budget.budget.used)
    portfolio = build_portfolio(
        ctx.conn,
        positions,
        env.market,
        now=now,
        wash_sale_days=settings.wash_sale_days,
        r=settings.scanner_risk_free_rate,
    )
    earnings = next_earnings(ctx.conn, list(by_ticker), _today(ctx))
    switch = HaltSwitch(HaltRepo(ctx.conn))
    exits = exit_config(settings)  # D26: exits/costs yaml + control-panel overrides
    cost_model = cost_config(settings)

    skipped: Counter[str] = Counter()
    lines: list[str] = []
    passed = proposals = 0

    def skip(t: str, key: str, code: ReasonCode, text: str, **payload: Any) -> None:
        skipped[key] += 1
        with ctx.conn:  # no other output for this ticker: commit the decision alone
            j.add(
                JournalPersona.SYSTEM,
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
        if _existing(ctx.conn, day, t):
            skip(t, "exists", ReasonCode.ALREADY_PROPOSED, f"already proposed on {day}")
            lines.append(f"{t}: already proposed today")
            continue
        a = assessed.get((t, qs.structure_type))
        if a is None:
            skip(t, "no_risk_review", ReasonCode.NO_RISK_REVIEW, "no Risk assessment")
            continue
        if not cand_ids.get(t):
            skip(t, "no_candidate", ReasonCode.NO_CANDIDATE_ID, "no Scout candidate row")
            continue
        try:
            priced = price_structure(
                env.market,
                [(leg.occ_symbol, LegIntent(leg.side), leg.ratio) for leg in qs.legs],
                as_of=_today(ctx),
                r=settings.scanner_risk_free_rate,
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
        market = market_snapshot(priced.contracts, earnings)
        limit = limit_price(st.net_debit_credit, settings.limit_tick)
        # D24: the gate checks the whole price band; size at its worst price (D18).
        band = band_for(st, limit, market, settings)
        size = size_contracts(
            suggestion=a.sizing_suggestion,
            max_loss_per_contract=worst_loss_per_contract(st, band),
            equity=info.equity,
            cap_pct=settings.max_alloc_pct,
            existing_max_loss=existing_max_loss(portfolio, t),  # S-7: remaining budget
        )
        sizing_payload = size.model_dump(mode="json") | {
            "max_loss_per_contract": str(st.max_loss) if st.max_loss is not None else None,
            "worst_loss_per_contract": (
                str(worst) if (worst := worst_loss_per_contract(st, band)) is not None else None
            ),
            "equity": str(info.equity),
            "cap_pct": settings.max_alloc_pct,
        }
        if not size.trade:
            skipped["sizing"] += 1
            if size.code == "budget_exhausted":
                # E6.4: existing exposure on this underlying uses the budget up. Keep
                # what a close-to-reallocate swap needs (risk.reallocate reads it back).
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
                commit=False,
            )
        except sqlite3.IntegrityError:  # a concurrent run won the (day, ticker) slot
            ctx.conn.rollback()
            skip(t, "exists", ReasonCode.ALREADY_PROPOSED, "a concurrent run won the slot")
            continue
        GateDecisionRepo(ctx.conn).insert(
            proposal_hash=phash,
            passed=decision.passed,
            violations=decision.violations,
            token=decision.token,
            account_snapshot=decision.account_snapshot,
            decided_at=now.isoformat(),
            run_id=ctx.run_id,
            commit=False,
        )
        j.add(
            JournalPersona.SYSTEM,
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
            reason_text=f"min(Risk {size.suggestion}, cap {size.cap_contracts}) = {size.contracts}",
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
        exit_model = _proposal_exit_model(
            priced,
            exits,
            settings.scanner_risk_free_rate,
            _realized_vol(ctx.snapshot, t),
            cost_model,
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
            ProposalPayload.model_validate(proposal.model_dump() | {"exit_model": exit_model}),
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
            portfolio = _with_position(portfolio, t, size.max_loss_total, st.greeks, size.contracts)
    return JobResult(
        summary="; ".join(lines) or f"no proposals ({dict(skipped)})",
        metrics={"proposals": proposals, "gate_passed": passed, **skipped, **budget.metrics()},
        notice=notice,
    )


# ---------------------------------------------------------------------------
# Handler entry points
# ---------------------------------------------------------------------------

_STEPS: dict[str, Callable[[JobContext, PipelineEnv], JobResult]] = {
    "director": director,
    "quant": quant,
    "risk": risk,
    "propose": propose,
}


class _LazyEnv:
    """Builds the live env on first use, so empty-context runs never touch Alpaca/Hermes."""

    def __init__(self, ctx: JobContext) -> None:
        self._ctx = ctx
        self._env: PipelineEnv | None = None

    def __getattr__(self, name: str) -> Any:
        if self._env is None:
            from arc.pipeline.env import PipelineEnv

            self._env = PipelineEnv.live(self._ctx.settings)
        return getattr(self._env, name)


def _live_env(ctx: JobContext) -> PipelineEnv:
    return cast("PipelineEnv", _LazyEnv(ctx))


def director_step(ctx: JobContext) -> JobResult:
    return director(ctx, _live_env(ctx))


def quant_step(ctx: JobContext) -> JobResult:
    return quant(ctx, _live_env(ctx))


def risk_step(ctx: JobContext) -> JobResult:
    return risk(ctx, _live_env(ctx))


def propose_step(ctx: JobContext) -> JobResult:
    return propose(ctx, _live_env(ctx))


def pipeline_handlers(env: PipelineEnv) -> dict[str, Handler]:
    """Dispatcher overrides binding every E5.2 step (and the Scout) to one *env*."""
    from arc.routines.handlers import scout_persona

    def bind(fn: Callable[[JobContext, PipelineEnv], JobResult]) -> Handler:
        return lambda ctx: fn(ctx, env)

    handlers: dict[str, Handler] = {name: bind(fn) for name, fn in _STEPS.items()}
    if env.scout_llm is not None or env.universe_guard is not None:
        scout_llm, make_guard = env.scout_llm, env.universe_guard

        def scout(ctx: JobContext) -> JobResult:
            guard = make_guard(ctx.settings, ctx.now) if make_guard is not None else None
            return scout_persona(ctx, llm=scout_llm, guard=guard)

        handlers["scout"] = scout
    return handlers
