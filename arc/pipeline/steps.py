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
    Runs the deterministic chain scanner (E2.3) for each shortlisted ticker
    (bullish → bull put, bearish → bear call, neutral → iron condor) and offers
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
    ``proposals`` and ``gate_decisions`` rows plus a ``proposal`` context entry.
    It is idempotent per (day, ticker).

Nothing here submits orders. Order submission is ``arc.execution.submit`` (E6).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import BaseModel, ValidationError

from arc.context.kinds import (
    ProposalPayload,
    RegimePayload,
    RiskReviewPayload,
    ShortlistPayload,
    StructuresPayload,
)
from arc.context.store import ContextStore
from arc.ingest.llm import ScoutLLMError
from arc.ingest.scout import extract_json_object
from arc.models import LegIntent, Proposal, QuantMetrics, Sizing, Stance
from arc.personas.builders import (
    build_director_prompt,
    build_quant_prompt,
    build_risk_prompt,
    director_input_from_context,
    quant_input_from_context,
    risk_input_from_context,
)
from arc.personas.schemas import (
    DirectorOutput,
    DirectorRankedItem,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
    RiskAssessment,
    RiskOutput,
)
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

    from arc.config import ArcSettings
    from arc.context.store import ContextSnapshot
    from arc.gate.inputs import Portfolio
    from arc.pipeline.env import PipelineEnv
    from arc.routines.handlers import Handler, JobContext
    from arc.scanner import ScanCandidate

log = structlog.get_logger(__name__)

__all__ = [
    "PersonaError",
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
DIRECTOR_READS = ["candidate", "regime", "channel_brief"]

# Drop reasons (stable keys; stored in persona_calls.dropped and step metrics).
DROP_NOT_CANDIDATE = "not_a_candidate"
DROP_DUPLICATE = "duplicate"
DROP_BAD_FIELD = "invalid_field"
DROP_OVER_LIMIT = "over_limit"
DROP_NOT_IN_MENU = "not_in_menu"
DROP_NOT_SHORTLISTED = "not_shortlisted"
DROP_UNKNOWN_STRUCTURE = "unknown_structure"


class PersonaError(RuntimeError):
    """A persona call failed (transport or unparseable reply). The step fails and the
    chain stops. ``arc routines run director --chain`` resumes from here."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _today(ctx: JobContext) -> _dt.date:
    return ctx.now.astimezone(ET).date()


def _schema_block(model: type[BaseModel]) -> str:
    return f"\n## JSON Schema (authoritative)\n{json.dumps(model.model_json_schema())}\n"


def _with_constraints(prompt: str, rules: list[str], schema: type[BaseModel]) -> str:
    body = "\n".join(f"- {r}" for r in rules)
    return (
        f"{prompt}\n## Hard constraints (enforced by the pipeline; violations are dropped)\n"
        f"{body}\n- Respond with ONLY the JSON object: no prose, no code fences.\n"
        f"{_schema_block(schema)}"
    )


class _Reply:
    def __init__(self, model: str, text: str, prompt: str, parsed: Any) -> None:
        self.model = model
        self.text = text
        self.prompt = prompt
        self.parsed = parsed


def _ask[M: BaseModel](
    ctx: JobContext,
    env: PipelineEnv,
    persona: str,
    prompt: str,
    schema: type[M],
    snapshot_id: str,
) -> tuple[_Reply, M]:
    """One persona LLM call. Failures are recorded in ``persona_calls`` and raised."""
    repo = PersonaCallRepo(ctx.conn)
    llm = env.llm(persona)
    try:
        reply = llm.complete(prompt)
    except ScoutLLMError as exc:
        repo.insert(
            run_id=ctx.run_id,
            persona=persona,
            model=getattr(llm, "model", "unknown"),
            snapshot_id=snapshot_id,
            prompt=prompt,
            raw_response=None,
            status="llm_error",
            error=str(exc),
            at=ctx.now,
        )
        msg = f"{persona} LLM call failed: {exc}"
        raise PersonaError(msg) from exc
    try:
        parsed = schema.model_validate(extract_json_object(reply.text))
    except (ValueError, ValidationError) as exc:
        repo.insert(
            run_id=ctx.run_id,
            persona=persona,
            model=reply.model,
            snapshot_id=snapshot_id,
            prompt=prompt,
            raw_response=reply.text,
            status="parse_error",
            error=str(exc)[:2000],
            at=ctx.now,
        )
        msg = f"{persona} reply does not match {schema.__name__}: {str(exc)[:300]}"
        raise PersonaError(msg) from exc
    return _Reply(reply.model, reply.text, prompt, parsed), parsed


def _record_ok(
    ctx: JobContext, persona: str, reply: _Reply, snapshot_id: str, dropped: Counter[str]
) -> None:
    PersonaCallRepo(ctx.conn).insert(
        run_id=ctx.run_id,
        persona=persona,
        model=reply.model,
        snapshot_id=snapshot_id,
        prompt=reply.prompt,
        raw_response=reply.text,
        status="ok",
        dropped=dict(dropped),
        at=ctx.now,
    )


def _latest[M: BaseModel](snapshot: ContextSnapshot, kind: str, model: type[M]) -> M | None:
    entry = snapshot.latest(kind, SESSION_SUBJECT)
    return model.model_validate(entry.payload) if entry else None


def _legs_key(legs: list[QuantLeg] | list[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    pairs = [(leg.occ_symbol, leg.side) if isinstance(leg, QuantLeg) else leg for leg in legs]
    return frozenset((parse_occ(sym).format(), side.lower()) for sym, side in pairs)


def _portfolio_summary(env: PipelineEnv, settings: ArcSettings) -> tuple[str, Decimal]:
    info = env.account()
    positions = env.positions()
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
            snap = build_snapshot_from_bars(t, bars, today)
        except Exception as exc:  # noqa: BLE001 - features are context, not a gate input
            log.warning("pipeline.regime_failed", ticker=t, error=str(exc))
            continue
        ctx.write("regime", t, RegimePayload.model_validate(snap.model_dump()))
        written.append(t)
    return written


def _filter_shortlist(
    out: DirectorOutput, candidates: Mapping[str, Stance], limit: int
) -> tuple[list[DirectorRankedItem], Counter[str], list[tuple[str, str]]]:
    """Kept items, drop counts by reason, and ``(ticker, reason)`` per drop."""
    dropped: Counter[str] = Counter()
    items: list[tuple[str, str]] = []
    kept: list[DirectorRankedItem] = []
    seen: set[str] = set()

    def drop(t: str, reason: str) -> None:
        dropped[reason] += 1
        items.append((t, reason))

    for item in sorted(out.shortlist, key=lambda i: (i.rank, -i.confidence)):
        t = item.ticker.strip().upper()
        if t not in candidates:
            drop(t, DROP_NOT_CANDIDATE)
            continue
        if t in seen:
            drop(t, DROP_DUPLICATE)
            continue
        stype = item.suggested_structure_type.strip().lower()
        stance = item.stance.strip().lower()
        if stype not in STRUCTURE_TYPES or stance not in {s.value for s in Stance}:
            drop(t, DROP_BAD_FIELD)
            continue
        if len(kept) >= limit:
            drop(t, DROP_OVER_LIMIT)
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
    return kept, dropped, items


def _scout_evidence(ctx: JobContext) -> dict[str, str]:
    """ticker -> one display line of the Scout data behind a pick (digest card only)."""
    out: dict[str, str] = {}
    for e in ctx.snapshot.of_kind("candidate"):
        p = e.payload
        when = ""
        if p.get("catalyst_date"):
            try:
                when = f" {_dt.datetime.fromisoformat(str(p['catalyst_date'])):%b %d}"
            except ValueError:
                when = ""
        n = len(p.get("sources") or [])
        out[e.subject] = esc(
            f"Scout {p.get('stance', '?')} · {p.get('catalyst_type', '?')} catalyst{when} · "
            f"{float(p.get('confidence', 0)):.0%} confidence · {n} source{'' if n == 1 else 's'}"
        )
    return out


def director(ctx: JobContext, env: PipelineEnv) -> JobResult:
    settings = ctx.settings
    cands = {e.subject: Stance(e.payload["stance"]) for e in ctx.snapshot.of_kind("candidate")}
    if not cands:
        ctx.write(
            "shortlist",
            SESSION_SUBJECT,
            ShortlistPayload(shortlist=[], market_regime="unknown", session_notes="no candidates"),
        )
        return JobResult(summary="no active candidates; empty shortlist", metrics={"shortlist": 0})

    regimes = _regime_entries(ctx, env, sorted(cands))
    # Re-read (and record) the context now that today's regime entries exist.
    snap = ContextStore(ctx.conn).snapshot(ctx.now, kinds=DIRECTOR_READS, run_id=ctx.run_id)
    RoutineRunRepo(ctx.conn).set_inputs(ctx.run_id, [ctx.snapshot.id, snap.id])

    summary, _ = _portfolio_summary(env, settings)
    limit = settings.pipeline_max_shortlist
    inp = director_input_from_context(
        snap, portfolio_summary=summary, scan_date=_today(ctx).isoformat()
    )
    prompt = _with_constraints(
        build_director_prompt(inp),
        [
            f"shortlist tickers MUST come from the candidates above: {', '.join(sorted(cands))}.",
            f"At most {limit} tickers, one entry each, rank 1 = highest conviction.",
            "stance: bullish | bearish | neutral. suggested_structure_type: vertical_spread | "
            "iron_condor | long_call | long_put.",
            "An empty shortlist is a valid answer when nothing is worth trading.",
        ],
        DirectorOutput,
    )
    reply, out = _ask(ctx, env, "director", prompt, DirectorOutput, snap.id)
    kept, dropped, drop_items = _filter_shortlist(out, cands, limit)
    _record_ok(ctx, "director", reply, snap.id, dropped)
    payload = ShortlistPayload(
        shortlist=kept, market_regime=out.market_regime, session_notes=out.session_notes
    )
    ctx.write("shortlist", SESSION_SUBJECT, payload)
    names = ", ".join(f"{i.ticker} ({i.stance})" for i in kept) or "none"
    picked = {i.ticker for i in kept} | {t for t, _ in drop_items}
    not_picked = [(t, "not_picked") for t in sorted(cands) if t not in picked]
    return JobResult(
        summary=f"{len(cands)} candidates → shortlist: {names}"
        + (f"; dropped {dict(dropped)}" if dropped else ""),
        metrics={"shortlist": len(kept), "regime_written": len(regimes), **dropped},
        card=director_card(
            payload,
            candidates=len(cands),
            dropped=[*drop_items, *not_picked],
            evidence=_scout_evidence(ctx),
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Quant
# ---------------------------------------------------------------------------


def _strategies(stance: str) -> list[Any]:
    from arc.scanner import ScanStrategy

    return {
        "bullish": [ScanStrategy.BULL_PUT],
        "bearish": [ScanStrategy.BEAR_CALL],
    }.get(stance, [ScanStrategy.IRON_CONDOR])


def _structure_type(c: ScanCandidate) -> str:
    return "iron_condor" if c.strategy.value == "iron_condor" else "vertical_spread"


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


def _cost_bps(c: ScanCandidate) -> float:
    """Round-trip half-spread cost vs. capital at risk (max loss), in bps."""
    max_loss = float(c.structure.max_loss or 0)
    return round(2 * c.cost / max_loss * 10_000, 1) if max_loss > 0 else 0.0


def _menu_entry(c: ScanCandidate) -> dict[str, Any]:
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
    }


def _to_quant_structure(
    ticker: str, c: ScanCandidate, *, confidence: float, rationale: str
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
    )


def quant(ctx: JobContext, env: PipelineEnv) -> JobResult:
    from arc.scanner import ScanParams, load_iv_history, scan

    settings = ctx.settings
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    if shortlist is None or not shortlist.shortlist:
        ctx.write(
            "structures",
            SESSION_SUBJECT,
            StructuresPayload(structures=[], analysis_notes="empty shortlist"),
        )
        return JobResult(summary="empty shortlist; no structures", metrics={"structures": 0})

    today = _today(ctx)
    menus: dict[str, dict[frozenset[tuple[str, str]], ScanCandidate]] = {}
    chains: dict[str, Any] = {}
    spots: dict[str, float] = {}
    no_chain: list[str] = []
    for item in shortlist.shortlist:
        try:
            params = ScanParams.from_settings(
                settings, strategies=_strategies(item.stance), top=settings.pipeline_scan_top
            )
            history = load_iv_history(env.iv_history_dir, item.ticker) if env.iv_history_dir else {}
            res = scan(env.market, item.ticker, params, as_of=today, iv_history=history)
        except Exception as exc:  # noqa: BLE001 - one ticker's chain must not sink the rest
            log.warning("pipeline.scan_failed", ticker=item.ticker, error=str(exc))
            no_chain.append(item.ticker)
            continue
        if not res.candidates:
            no_chain.append(item.ticker)
            continue
        spots[item.ticker] = res.spot
        menus[item.ticker] = {
            _legs_key([(leg.occ_symbol, leg.side.value) for leg in c.structure.legs]): c
            for c in res.candidates
        }
        chains[item.ticker] = {
            "stance": item.stance,
            "iv": res.iv.model_dump(mode="json"),
            "menu": [_menu_entry(c) for c in res.candidates],
        }

    if not menus:
        ctx.write(
            "structures",
            SESSION_SUBJECT,
            StructuresPayload(
                structures=[], analysis_notes=f"no tradable chain for {', '.join(no_chain)}"
            ),
        )
        return JobResult(
            summary=f"no scanner structures for {', '.join(no_chain)}",
            metrics={"structures": 0, "no_chain": len(no_chain)},
        )

    inp = quant_input_from_context(
        ctx.snapshot,
        chains_json=json.dumps(chains, indent=2, sort_keys=True),
        underlying_prices_json=json.dumps(spots, sort_keys=True),
        scan_date=today.isoformat(),
    )
    prompt = _with_constraints(
        build_quant_prompt(inp),
        [
            "Choose ONLY from the per-ticker `menu` above; copy each chosen structure's legs "
            "(occ_symbol, side, ratio) verbatim. Anything else is discarded.",
            "At most one structure per ticker, best first. You may omit a ticker.",
            "All analytics (price, max gain/loss, breakevens, Greeks, PoP, EV, cost) are "
            "replaced by the pipeline's own numbers; your value-add is the choice, confidence "
            "and rationale.",
            f"Tickers without a tradable chain: {', '.join(no_chain) or 'none'}.",
        ],
        QuantOutput,
    )
    reply, out = _ask(ctx, env, "quant", prompt, QuantOutput, ctx.snapshot.id)
    dropped: Counter[str] = Counter()
    drop_items: list[tuple[str, str]] = []
    kept: list[QuantStructureOut] = []
    seen: set[str] = set()
    for s in out.structures:
        t = s.ticker.strip().upper()
        if t not in menus:
            reason = DROP_NOT_SHORTLISTED
        elif t in seen:
            reason = DROP_DUPLICATE
        else:
            try:
                match = menus[t].get(_legs_key(s.legs))
            except ValueError:
                match = None
            if match is not None:
                seen.add(t)
                kept.append(
                    _to_quant_structure(t, match, confidence=s.confidence, rationale=s.rationale)
                )
                continue
            reason = DROP_NOT_IN_MENU
        dropped[reason] += 1
        drop_items.append((t, reason))
    _record_ok(ctx, "quant", reply, ctx.snapshot.id, dropped)
    payload = StructuresPayload(structures=kept, analysis_notes=out.analysis_notes)
    ctx.write("structures", SESSION_SUBJECT, payload)
    desc = "; ".join(
        f"{s.ticker} {s.structure_type} "
        f"{'/'.join(f'{leg.strike:g}' for leg in s.legs)} {s.legs[0].expiry} "
        f"net {s.net_debit_credit:+.2f} PoP {s.pop:.2f}"
        for s in kept
    )
    return JobResult(
        summary=(desc or "no structure chosen")
        + (f"; dropped {dict(dropped)}" if dropped else "")
        + (f"; no chain: {', '.join(no_chain)}" if no_chain else ""),
        metrics={"structures": len(kept), "no_chain": len(no_chain), **dropped},
        card=quant_card(
            payload,
            dropped=dropped,
            dropped_items=drop_items,
            no_chain=no_chain,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


def risk(ctx: JobContext, env: PipelineEnv) -> JobResult:
    settings = ctx.settings
    structures = _latest(ctx.snapshot, "structures", StructuresPayload)
    if structures is None or not structures.structures:
        ctx.write(
            "risk_review",
            SESSION_SUBJECT,
            RiskReviewPayload(
                assessments=[], portfolio_summary="", advisory_notes="nothing to review"
            ),
        )
        return JobResult(summary="no structures to review", metrics={"assessments": 0})

    today = _today(ctx)
    info = env.account()
    try:
        portfolio = build_portfolio(
            ctx.conn,
            env.positions(),
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
        (s.ticker, s.structure_type): int(budget // Decimal(str(s.max_loss)))
        for s in structures.structures
        if s.max_loss
    }
    inp = risk_input_from_context(
        ctx.snapshot,
        portfolio_json=portfolio.model_dump_json(indent=2),
        calendar_json=json.dumps(calendar, indent=2),
        account_equity=float(info.equity),
        scan_date=today.isoformat(),
    )
    prompt = _with_constraints(
        build_risk_prompt(inp),
        [
            "Exactly one assessment per proposed structure, with ticker and structure_type "
            "copied verbatim from it.",
            "sizing_suggestion is advisory. The pipeline trades min(your suggestion, "
            f"floor({settings.max_alloc_pct:.0%} × equity / max_loss)); caps per structure: "
            + ", ".join(f"{t} {k}: {n}" for (t, k), n in sorted(caps.items()))
            + ". Suggest 0 to decline a trade.",
        ],
        RiskOutput,
    )
    reply, out = _ask(ctx, env, "risk", prompt, RiskOutput, ctx.snapshot.id)
    wanted = {(s.ticker, s.structure_type) for s in structures.structures}
    dropped: Counter[str] = Counter()
    drop_items: list[tuple[str, str]] = []
    kept: list[RiskAssessment] = []
    seen: set[tuple[str, str]] = set()
    for a in out.assessments:
        key = (a.ticker.strip().upper(), a.structure_type.strip().lower())
        reason = (
            DROP_UNKNOWN_STRUCTURE if key not in wanted else DROP_DUPLICATE if key in seen else None
        )
        if reason is not None:
            dropped[reason] += 1
            drop_items.append((f"{key[0]} {key[1]}", reason))
            continue
        seen.add(key)
        kept.append(a.model_copy(update={"ticker": key[0], "structure_type": key[1]}))
    _record_ok(ctx, "risk", reply, ctx.snapshot.id, dropped)
    payload = RiskReviewPayload(
        assessments=kept,
        portfolio_summary=out.portfolio_summary,
        advisory_notes=out.advisory_notes,
    )
    ctx.write("risk_review", SESSION_SUBJECT, payload)
    max_loss = {(s.ticker, s.structure_type): s.max_loss for s in structures.structures}
    sized = {
        (a.ticker, a.structure_type): size_contracts(
            suggestion=a.sizing_suggestion,
            max_loss_per_contract=(
                None
                if max_loss.get((a.ticker, a.structure_type)) is None
                else Decimal(str(max_loss[(a.ticker, a.structure_type)]))
            ),
            equity=info.equity,
            cap_pct=settings.max_alloc_pct,
        )
        for a in kept
    }
    desc = "; ".join(f"{a.ticker} {a.risk_rating}, suggests {a.sizing_suggestion}" for a in kept)
    missing = sorted(f"{t} {k}" for t, k in wanted - seen)
    return JobResult(
        summary=(desc or "no assessments")
        + (f"; not assessed: {', '.join(missing)}" if missing else "")
        + (f"; dropped {dict(dropped)}" if dropped else ""),
        metrics={"assessments": len(kept), "not_assessed": len(missing), **dropped},
        card=risk_card(
            payload,
            sized=sized,
            max_gain={(s.ticker, s.structure_type): s.max_gain for s in structures.structures},
            cap_pct=settings.max_alloc_pct,
            dropped=dropped,
            dropped_items=drop_items,
            not_assessed=missing,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# propose (deterministic) + gate
# ---------------------------------------------------------------------------


def _existing(conn: sqlite3.Connection, day: str, ticker: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM proposals WHERE day = ? AND ticker = ?", (day, ticker)
    ).fetchone()
    return row is not None


def _mint(decision: Any, proposal: Proposal, settings: ArcSettings, now: _dt.datetime) -> Any:
    from arc.gate.token import TokenError, gate_secret, issue_token

    if not decision.passed:
        return decision
    try:
        secret = gate_secret(settings)
    except TokenError as exc:
        log.warning("pipeline.no_gate_token", reason=str(exc))
        return decision
    return issue_token(decision, proposal, secret=secret, now=now)


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


def propose(ctx: JobContext, env: PipelineEnv) -> JobResult:
    from arc.gate.halt import HaltSwitch, evaluate_with_halt
    from arc.gate.rules import proposal_hash
    from arc.store.repos import GateDecisionRepo, HaltRepo, ProposalRepo

    settings = ctx.settings
    now = ctx.now
    day = _today(ctx).isoformat()
    shortlist = _latest(ctx.snapshot, "shortlist", ShortlistPayload)
    structures = _latest(ctx.snapshot, "structures", StructuresPayload)
    review = _latest(ctx.snapshot, "risk_review", RiskReviewPayload)
    if not (shortlist and structures and review) or not structures.structures:
        return JobResult(summary="nothing to propose", metrics={"proposals": 0})

    cand_ids = {e.subject: e.payload.get("id") for e in ctx.snapshot.of_kind("candidate")}
    by_ticker = {s.ticker: s for s in reversed(structures.structures)}  # first (best) wins
    assessed = {(a.ticker, a.structure_type): a for a in review.assessments}

    info = env.account()
    account = account_snapshot(info, now)
    portfolio = build_portfolio(
        ctx.conn,
        env.positions(),
        env.market,
        now=now,
        wash_sale_days=settings.wash_sale_days,
        r=settings.scanner_risk_free_rate,
    )
    earnings = next_earnings(ctx.conn, list(by_ticker), _today(ctx))
    switch = HaltSwitch(HaltRepo(ctx.conn))

    skipped: Counter[str] = Counter()
    lines: list[str] = []
    passed = proposals = 0
    for item in shortlist.shortlist:
        t = item.ticker
        qs = by_ticker.get(t)
        if qs is None:
            skipped["no_structure"] += 1
            continue
        if _existing(ctx.conn, day, t):
            skipped["exists"] += 1
            lines.append(f"{t}: already proposed today")
            continue
        a = assessed.get((t, qs.structure_type))
        if a is None:
            skipped["no_risk_review"] += 1
            continue
        if not cand_ids.get(t):
            skipped["no_candidate"] += 1
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
            skipped["reprice_failed"] += 1
            continue
        st = priced.structure
        size = size_contracts(
            suggestion=a.sizing_suggestion,
            max_loss_per_contract=st.max_loss,
            equity=info.equity,
            cap_pct=settings.max_alloc_pct,
        )
        if not size.trade:
            skipped["sizing"] += 1
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
            limit_price=limit_price(st.net_debit_credit, settings.limit_tick),
        )
        decision = evaluate_with_halt(
            switch,
            proposal,
            account,
            portfolio,
            settings,
            market=market_snapshot(priced.contracts, earnings),
            now=now,
        )
        if env.mint_tokens:
            decision = _mint(decision, proposal, settings, now)
        phash = proposal_hash(proposal)
        try:
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
            )
        except sqlite3.IntegrityError:  # a concurrent run won the (day, ticker) slot
            ctx.conn.rollback()
            skipped["exists"] += 1
            continue
        GateDecisionRepo(ctx.conn).insert(
            proposal_hash=phash,
            passed=decision.passed,
            violations=decision.violations,
            token=decision.token,
            account_snapshot=decision.account_snapshot,
            decided_at=now.isoformat(),
            run_id=ctx.run_id,
        )
        ctx.write("proposal", t, ProposalPayload.model_validate(proposal.model_dump()))
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
        metrics={"proposals": proposals, "gate_passed": passed, **skipped},
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
    if env.scout_llm is not None:
        scout_llm = env.scout_llm
        handlers["scout"] = lambda ctx: scout_persona(ctx, llm=scout_llm)
    return handlers
