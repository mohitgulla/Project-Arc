"""E6.4 intraday chain: ``positions.evaluate → investor.exits → risk.reallocate`` (D19).

Every step reads and writes through the context store and the audit DB (D16):
no in-memory hand-offs. Nothing here submits an order: every close or swap
open is a normal proposal (gate + ``arc2`` token + approval card), and only the
Investor (:mod:`arc.routines.investor`) submits, via ``arc.execution``, after
approval.

``positions.evaluate`` (deterministic)
    Marks every open structure, reviews it (:func:`arc.positions.evaluate.review_position`)
    and writes one ``position_review`` context entry per position.

``investor.exits`` (deterministic)
    Turns review signals (stop, profit target, time-adjusted target, DTE exit,
    remaining-EV floor) into close proposals via
    :func:`arc.execution.exits.propose_close`: limit at mid over the D24 band, one
    per structure per ET day, none while one is pending. Halted: nothing proposed.

``risk.reallocate`` (Risk persona, veto only)
    1. advances existing swaps: a swap whose close FILLED gets its open proposal
       (re-priced, re-sized, gated, carded); a close that did not fill cancels the
       swap and its open is never proposed,
    2. collects new proposals rejected for capacity only (gate
       :func:`~arc.gate.rules.capacity_rejection`, or sizing ``budget_exhausted``),
    3. scores (new, open) pairs (:func:`arc.positions.reallocate.score_swaps`),
    4. asks Risk to approve/veto each suggestion (missing verdict or LLM failure
       = veto, fail closed), then proposes the approved closes (``swap_id`` set).
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter
from typing import TYPE_CHECKING, Any, cast

import structlog

from arc.exits.position import OpenPosition, PositionMarks
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.models import LegIntent, Structure
from arc.positions.evaluate import PositionReview, SignalKind, review_position
from arc.positions.reallocate import (
    CapacityCandidate,
    ReallocRules,
    ScoredPair,
    SwapSuggestion,
    score_swaps,
)
from arc.routines.handlers import JobResult
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from decimal import Decimal

    from arc.config import ArcSettings
    from arc.exits import ExitConfig
    from arc.gate.halt import HaltSwitch
    from arc.gate.inputs import AccountSnapshot, Portfolio
    from arc.pipeline.env import PipelineEnv
    from arc.pipeline.market import PricedStructure
    from arc.routines.handlers import Handler, JobContext

__all__ = [
    "SIGNAL_CODES",
    "evaluate",
    "evaluate_step",
    "exits",
    "exits_step",
    "position_handlers",
    "reallocate",
    "reallocate_step",
]

log = structlog.get_logger(__name__)

SIGNAL_CODES: dict[SignalKind, ReasonCode] = {
    SignalKind.STOP: ReasonCode.EXIT_STOP,
    SignalKind.PROFIT_TARGET: ReasonCode.EXIT_TAKE_PROFIT,
    SignalKind.TIME_ADJUSTED_TARGET: ReasonCode.EXIT_TIME_ADJUSTED,
    SignalKind.DTE_EXIT: ReasonCode.EXIT_DTE,
    SignalKind.EXPIRY: ReasonCode.EXIT_EXPIRY,
    SignalKind.REMAINING_EV_FLOOR: ReasonCode.EXIT_EV_FLOOR,
}

_PAIR_CODES: dict[str, ReasonCode] = {
    "suggested": ReasonCode.REALLOC_SUGGESTED,
    "edge_below_min": ReasonCode.REALLOC_EDGE_BELOW_MIN,
    "pop_below_open": ReasonCode.REALLOC_POP_BELOW_OPEN,
    "frees_nothing": ReasonCode.REALLOC_FREES_NOTHING,
    "no_open_numbers": ReasonCode.REALLOC_NO_OPEN_NUMBERS,
    "churn_ticker": ReasonCode.REALLOC_CHURN_TICKER,
    "churn_day": ReasonCode.REALLOC_CHURN_DAY,
    "already_paired": ReasonCode.REALLOC_ALREADY_PAIRED,
}

_CLOSE_FILLED = "filled"
_CLOSE_DEAD = ("cancelled", "rejected", "partially_filled")
_REQ_DEAD = ("rejected", "expired", "not_actionable")


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _today(ctx: JobContext) -> _dt.date:
    return ctx.now.astimezone(ET).date()


def _eod(ctx: JobContext) -> bool:
    eod_from = _dt.time.fromisoformat(str(ctx.options.get("eod_marks_from", "15:30")))
    return ctx.now.astimezone(ET).time() >= eod_from


def _book(ctx: JobContext, env: PipelineEnv) -> tuple[Any, AccountSnapshot, Portfolio, HaltSwitch]:
    """Account, gate account snapshot (halt stamped), portfolio, halt switch."""
    from arc.gate.halt import HaltSwitch
    from arc.pipeline.budget import read_budget
    from arc.pipeline.market import account_snapshot, build_portfolio
    from arc.pipeline.steps import _account_inputs
    from arc.store.repos import HaltRepo

    settings = ctx.settings
    info, positions = _account_inputs(ctx, env)
    portfolio = build_portfolio(
        ctx.conn,
        positions,
        env.market,
        now=ctx.now,
        wash_sale_days=settings.wash_sale_days,
        r=settings.scanner_risk_free_rate,
    )
    switch = HaltSwitch(HaltRepo(ctx.conn))
    # D32: closes are charged against the full daily cap (the gate's order_budget rule).
    budget = read_budget(ctx, env, settings, now=ctx.clock())
    account = account_snapshot(info, ctx.now, orders_used_today=budget.budget.used)
    return info, switch.apply(account), portfolio, switch


def _gate_secret(ctx: JobContext, env: PipelineEnv) -> bytes | None:
    """The token secret for a live paper run; ``None`` for dry runs (never mint).

    E5.2b: a run that should mint but cannot raises before any proposal row is
    written, so it alerts instead of storing token-less PASSes nobody can execute.
    """
    if not env.mint_tokens:
        return None
    from arc.gate.token import TokenError, gate_secret
    from arc.pipeline.steps import GateSecretMissingError

    try:
        return gate_secret(ctx.settings)
    except TokenError as exc:
        msg = f"cannot mint a gate token: {exc} (set ARC_GATE_SECRET)"
        raise GateSecretMissingError(msg) from exc


def _theta_per_day(priced: PricedStructure, st: Structure, dte: int, r: float) -> float | None:
    """Position theta ($ per unit per calendar day) at current IVs; ``None`` without IVs."""
    from arc.structures import MarketInputs, net_greeks

    if priced.spot is None or dte < 1:
        return None
    ivs: dict[str, float] = {}
    for leg in st.legs:
        c = priced.contracts.get(leg.occ_symbol)
        iv = None if c is None else c.implied_volatility
        if not iv or not float(iv) > 0:
            return None
        ivs[leg.occ_symbol] = float(iv)
    g = net_greeks(st.legs, MarketInputs(spot=priced.spot, r=r, ivs=ivs), dte)
    return float(g.theta)


def _entry_managed_net_ev(conn: sqlite3.Connection, phash: str | None) -> float | None:
    """The open proposal's E2.4 managed Net EV ($ per unit), frozen in its market context."""
    if not phash:
        return None
    row = conn.execute(
        """SELECT json_extract(payload, '$.analytics.exit_model.managed.net_ev')
           FROM market_contexts WHERE proposal_hash = ?
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (phash,),
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def _minutes_since(opened_at: str | None, now: _dt.datetime) -> float | None:
    """Minutes from the open fill (``open_structures.opened_at``) to *now*."""
    if not opened_at:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(opened_at))
    except ValueError:
        return None
    ts = ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts
    return max((now - ts).total_seconds() / 60.0, 0.0)


def _reviews(ctx: JobContext) -> dict[str, PositionReview]:
    """This chain's ``position_review`` entries by structure id (latest wins)."""
    out: dict[str, PositionReview] = {}
    for e in ctx.snapshot.of_kind("position_review"):
        r = PositionReview.model_validate(e.payload)
        out[r.structure_id] = r
    return out


# ---------------------------------------------------------------------------
# positions.evaluate
# ---------------------------------------------------------------------------


def evaluate(ctx: JobContext, env: PipelineEnv, *, exit_cfg: ExitConfig | None = None) -> JobResult:
    """Review every open structure; write one ``position_review`` per position."""
    from arc.control.effective import cost_model, exit_config
    from arc.execution.exits import exit_pending, price_close
    from arc.pipeline.steps import _realized_vol
    from arc.store.execution import OpenStructureRepo

    settings = ctx.settings
    # D26: exits.yaml / costs.yaml + control-panel overrides carried by the settings
    cfg = exit_cfg or exit_config(settings)
    cost = cost_model(settings)
    today = _today(ctx)
    rows = OpenStructureRepo(ctx.conn).list_open()
    lines: list[str] = []
    errors: list[str] = []
    signals: Counter[str] = Counter()
    reviewed = 0
    for row in rows:
        t = str(row["ticker"])
        st = Structure.model_validate_json(row["structure_json"])
        try:
            priced = price_close(env.market, st, as_of=today, r=settings.scanner_risk_free_rate)
            mids = {k: float(c.mid) for k, c in priced.contracts.items() if c.mid is not None}
            review = review_position(
                structure_id=str(row["id"]),
                ticker=t,
                position=OpenPosition(
                    structure=st, entry_net=float(row["entry_net"]), contracts=row["contracts"]
                ),
                marks=PositionMarks(
                    as_of=today,
                    leg_mids=mids,
                    leg_spreads=priced.leg_spreads(),
                    spot=priced.spot,
                    iv=priced.atm_iv,
                    r=settings.scanner_risk_free_rate,
                    realized_vol=_realized_vol(ctx.snapshot, t),
                    end_of_day=_eod(ctx),
                ),
                exits=cfg,
                cost=cost,
                theta_per_day=_theta(priced, st, today, settings.scanner_risk_free_rate),
                exit_pending=exit_pending(ctx.conn, row),
                entry_managed_net_ev=_entry_managed_net_ev(ctx.conn, row["open_proposal_hash"]),
                minutes_since_fill=_minutes_since(row.get("opened_at"), ctx.now),
            )
        except (LookupError, ValueError) as exc:
            errors.append(f"{t}: cannot review ({exc})")
            log.warning("positions.review_failed", ticker=t, structure_id=row["id"], error=str(exc))
            continue
        ctx.record_input(
            f"marks:{row['id']}",
            "fixture" if env.offline else "alpaca",
            {"mids": mids, "spot": priced.spot, "atm_iv": priced.atm_iv},
            as_of=priced.spot_as_of or ctx.now,
            count=len(mids),
        )
        with ctx.conn:
            ctx.write("position_review", str(row["id"]), review)
        reviewed += 1
        sig = review.signal
        if sig is not None:
            signals[sig.kind.value] += 1
        lines.append(
            f"{t} {review.kind or 'structure'} {review.dte} DTE P&L ${review.pnl_total:+,.0f}"
            + (f" -> {sig.kind.value}" if sig else "")
        )
    summary = "; ".join(lines) or "no open positions"
    if errors:
        summary += "; " + "; ".join(errors)
    return JobResult(
        summary=summary,
        metrics={
            "positions": len(rows),
            "reviewed": reviewed,
            "signals": sum(signals.values()),
            **{f"signal_{k}": v for k, v in signals.items()},
            "errors": len(errors),
        },
        notice="; ".join(errors),
    )


def _theta(priced: PricedStructure, st: Structure, today: _dt.date, r: float) -> float | None:
    from arc.structures import parse_occ
    from arc.utils.calendar import dte_calendar

    dte = dte_calendar(today, parse_occ(st.legs[0].occ_symbol).expiration)
    try:
        return _theta_per_day(priced, st, dte, r)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# investor.exits
# ---------------------------------------------------------------------------


def exits(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """Propose closes (gate + token + approval card) for positions with a review signal."""
    from arc.execution.exits import exit_pending, price_close, propose_close
    from arc.store.execution import OpenStructureRepo

    settings = ctx.settings
    reviews = _reviews(ctx)
    flagged = [r for r in reviews.values() if r.signal is not None]
    if not flagged:
        return JobResult(summary="no exit signals", metrics={"signals": 0, "proposed": 0})
    secret = _gate_secret(ctx, env)
    info, account, portfolio, switch = _book(ctx, env)
    if switch.is_halted():
        return JobResult(
            summary=f"halted: {len(flagged)} exit signal(s) not proposed",
            metrics={"signals": len(flagged), "proposed": 0, "halted": True},
        )
    repo = OpenStructureRepo(ctx.conn)
    day = _today(ctx).isoformat()
    lines: list[str] = []
    errors: list[str] = []
    alerts: list[str] = []
    proposed = passed = quote_blocked = 0
    for rv in flagged:
        row = repo.get(rv.structure_id)
        if row is None or row["status"] != "open":
            continue
        if exit_pending(ctx.conn, row) or row.get("exit_day") == day:
            continue
        sig = rv.signal
        assert sig is not None  # noqa: S101 - filtered above
        st = Structure.model_validate_json(row["structure_json"])
        try:
            priced = price_close(
                env.market, st, as_of=_today(ctx), r=settings.scanner_risk_free_rate
            )
        except (LookupError, ValueError) as exc:
            errors.append(f"{rv.ticker}: cannot price close ({exc})")
            continue
        res = propose_close(
            ctx.conn,
            row=row,
            priced=priced,
            thesis=f"Exit ({sig.kind.value}): {sig.detail}; structure {row['id']}",
            reason=sig.kind.value,
            reason_code=SIGNAL_CODES[sig.kind],
            persona=JournalPersona.INVESTOR,
            close_now_net=rv.close_now_net,
            settings=settings,
            account=account,
            portfolio=portfolio,
            switch=switch,
            now=ctx.clock(),  # E5.2b: quotes were just fetched
            run_id=ctx.run_id,
            write_context=ctx.write,
            secret=secret,
            payload={"review": rv.model_dump(mode="json", exclude={"structure"})},
        )
        if res.alert:
            alerts.append(res.alert)
        if res.proposal_hash is None:  # E6.2a: quotes unusable; the next tick retries
            quote_blocked += 1
            errors.append(res.line)
            continue
        proposed += 1
        passed += res.passed
        lines.append(f"{res.line} ({sig.detail})")
    notices = [f"exit proposed: {line}" for line in lines if "gate PASS" in line] + alerts
    return JobResult(
        summary="; ".join(lines + errors) or "no exits proposed",
        metrics={
            "signals": len(flagged),
            "proposed": proposed,
            "gate_passed": passed,
            "quote_blocked": quote_blocked,
        },
        notice="; ".join(notices),
    )


# ---------------------------------------------------------------------------
# risk.reallocate
# ---------------------------------------------------------------------------


def _rules(settings: ArcSettings) -> ReallocRules:
    return ReallocRules(
        min_edge=settings.realloc_min_edge,
        pop_tolerance=settings.realloc_pop_tolerance,
        max_per_day=settings.realloc_max_swaps_per_day,
        max_per_ticker_per_day=settings.realloc_max_swaps_per_ticker_per_day,
    )


def capacity_candidates(
    conn: sqlite3.Connection, day: str, *, taken: set[str]
) -> tuple[list[CapacityCandidate], dict[str, dict[str, Any]]]:
    """Today's entries blocked for capacity only, with what re-proposing them needs.

    Two sources, both already in the audit store (no new table):
    ``sizing:budget_exhausted`` journal rows (existing exposure used the budget up;
    they carry ``realloc_source``), and open proposals whose gate decision failed
    only capacity rules (:func:`arc.gate.rules.capacity_rejection`).
    """
    from arc.gate.rules import CapacityRejection, RuleCode, capacity_rejection

    out: list[CapacityCandidate] = []
    sources: dict[str, dict[str, Any]] = {}
    from arc.context.ttl import to_db

    start = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(0), tzinfo=ET)
    lo, hi = to_db(start), to_db(start + _dt.timedelta(days=1))
    rows = conn.execute(
        """SELECT id, subject, payload FROM decisions
           WHERE reason_code = ? AND at >= ? AND at < ? ORDER BY at, id""",
        (ReasonCode.SIZING_BUDGET_EXHAUSTED.value, lo, hi),
    ).fetchall()
    for r in rows:
        src = json.loads(r["payload"]).get("realloc_source")
        ref = str(r["id"])
        if not src or ref in taken or not src.get("net_ev") or not src.get("buying_power"):
            continue
        cand = CapacityCandidate(
            source_ref=ref,
            ticker=str(r["subject"]),
            kind=src.get("kind"),
            rejected_for=CapacityRejection.BUYING_POWER,
            violation_codes=[RuleCode.PER_UNDERLYING.value],
            net_ev=float(src["net_ev"]),
            pop=float(src["pop"]),
            buying_power=float(src["buying_power"]),
        )
        out.append(cand)
        sources[ref] = src
    prows = conn.execute(
        """SELECT p.proposal_hash, p.ticker, g.violations_json,
                  (SELECT c.payload FROM context_entries c
                    WHERE c.kind = 'proposal' AND c.run_id = p.run_id AND c.subject = p.ticker
                    ORDER BY c.created_at DESC, c.rowid DESC LIMIT 1) AS ctx
           FROM proposals p JOIN gate_decisions g ON g.proposal_hash = p.proposal_hash
           WHERE p.day = ? AND p.kind = 'open' AND p.swap_id IS NULL AND g.passed = 0
           ORDER BY p.created_at, p.rowid""",
        (day,),
    ).fetchall()
    for r in prows:
        ref = str(r["proposal_hash"])
        why = capacity_rejection(json.loads(r["violations_json"] or "[]"))
        if why is None or ref in taken or r["ctx"] is None:
            continue
        payload = json.loads(r["ctx"])
        model = payload.get("exit_model") or {}
        managed = model.get("managed") or {}
        st = payload["structure"]
        bp = st.get("buying_power") or st.get("max_loss")
        if managed.get("net_ev") is None or not bp:
            continue
        codes = sorted({v.split(":", 1)[0].strip() for v in json.loads(r["violations_json"])})
        out.append(
            CapacityCandidate(
                source_ref=ref,
                ticker=str(r["ticker"]),
                kind=st.get("kind"),
                rejected_for=why,
                violation_codes=codes,
                net_ev=float(managed["net_ev"]),
                pop=float(managed["pop"]),
                buying_power=float(bp),
            )
        )
        sources[ref] = {
            "candidate_id": payload["candidate_id"],
            "thesis": payload["thesis"],
            "quant": payload["quant"],
            "risk_narrative": payload.get("risk_narrative", ""),
            "suggestion": int(payload["sizing"]["contracts"]),
            "kind": st.get("kind"),
            "legs": [[leg["occ_symbol"], leg["side"], leg["ratio"]] for leg in st["legs"]],
        }
    return out, sources


def _advance_swaps(
    ctx: JobContext,
    env: PipelineEnv,
    book: tuple[Any, AccountSnapshot, Portfolio, HaltSwitch],
    secret: bytes | None,
) -> list[str]:
    """Move ``closing`` swaps on: close filled → propose the open; close dead → cancel."""
    from arc.store.swaps import SwapRepo

    repo = SwapRepo(ctx.conn)
    lines: list[str] = []
    for sw in repo.by_status("closing"):
        close_hash = sw["close_proposal_hash"]
        req = ctx.conn.execute(
            "SELECT status FROM approval_requests WHERE proposal_hash = ?", (close_hash,)
        ).fetchone()
        ex = ctx.conn.execute(
            "SELECT status FROM executions WHERE proposal_hash = ?", (close_hash,)
        ).fetchone()
        gate = ctx.conn.execute(
            """SELECT passed FROM gate_decisions WHERE proposal_hash = ?
               ORDER BY decided_at DESC, rowid DESC LIMIT 1""",
            (close_hash,),
        ).fetchone()
        why: str | None = None
        if gate is not None and not gate["passed"]:
            why = "close failed the gate"
        elif req is not None and req["status"] in _REQ_DEAD:
            why = f"close approval {req['status']}"
        elif ex is not None and ex["status"] in _CLOSE_DEAD:
            why = f"close {ex['status']}"
        elif ex is not None and ex["status"] == _CLOSE_FILLED:
            lines.append(_open_leg(ctx, env, sw, book, secret))
            continue
        elif sw["day"] != _today(ctx).isoformat():
            why = "close did not fill by the end of its day"
        if why is not None:
            _cancel(ctx, sw, why)
            lines.append(f"swap {sw['id']} cancelled: {why}")
    return lines


def _cancel(ctx: JobContext, sw: dict[str, Any], why: str) -> None:
    from arc.journal.store import JournalStore
    from arc.store.swaps import SwapRepo

    with ctx.conn:
        SwapRepo(ctx.conn).update(
            sw["id"], status="cancelled", now=ctx.now, detail=why, commit=False
        )
        JournalStore(ctx.conn).record(
            persona=JournalPersona.RISK,
            stage=Stage.REALLOCATE,
            subject=sw["open_ticker"],
            choice=Choice.CANCELLED,
            reason_code=ReasonCode.REALLOC_CANCELLED,
            reason_text=f"swap {sw['id']}: {why}; the open is not proposed",
            proposal_hash=sw["close_proposal_hash"],
            payload={"swap_id": sw["id"]},
            at=ctx.now,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        )
    log.info("positions.swap_cancelled", swap_id=sw["id"], why=why)


def _open_leg(
    ctx: JobContext,
    env: PipelineEnv,
    sw: dict[str, Any],
    book: tuple[Any, AccountSnapshot, Portfolio, HaltSwitch],
    secret: bytes | None,
) -> str:
    """The close filled: re-price, re-size (D18 + S-7), gate and card the swap's open."""
    from arc.gate.halt import evaluate_with_halt
    from arc.gate.rules import proposal_hash
    from arc.gate.token import issue_token
    from arc.journal.store import JournalStore
    from arc.models import Proposal, QuantMetrics, Sizing
    from arc.pipeline.market import limit_price, market_snapshot, next_earnings, price_structure
    from arc.pipeline.steps import (
        band_for,
        existing_max_loss,
        worst_loss_per_contract,
    )
    from arc.sizing import size_contracts
    from arc.store.repos import GateDecisionRepo, ProposalRepo
    from arc.store.swaps import SwapRepo

    settings = ctx.settings
    info, account, portfolio, switch = book
    src = json.loads(sw["suggestion_json"])["source"]
    t = sw["open_ticker"]
    try:
        priced = price_structure(
            env.market,
            [(o, LegIntent(s), int(n)) for o, s, n in src["legs"]],
            as_of=_today(ctx),
            r=settings.scanner_risk_free_rate,
        )
    except (LookupError, ValueError) as exc:
        _cancel(ctx, sw, f"open could not be re-priced ({exc})")
        return f"swap {sw['id']} cancelled: open could not be re-priced"
    st = priced.structure
    now = ctx.clock()  # E5.2b: judge quote age against a clock read after the fetch
    earnings = next_earnings(ctx.conn, [t], _today(ctx))
    market = market_snapshot(priced.contracts, earnings)
    limit = limit_price(st.net_debit_credit, settings.limit_tick)
    band = band_for(st, limit, market, settings)
    size = size_contracts(
        suggestion=int(src["suggestion"]),
        max_loss_per_contract=worst_loss_per_contract(st, band),
        equity=info.equity,
        cap_pct=settings.max_alloc_pct,
        existing_max_loss=existing_max_loss(portfolio, t),
    )
    if not size.trade:
        _cancel(ctx, sw, f"open no longer fits after the close ({size.reason})")
        return f"swap {sw['id']} cancelled: {size.reason}"
    quant = QuantMetrics.model_validate(src["quant"])
    proposal = Proposal(
        candidate_id=str(src["candidate_id"]),
        structure=st,
        thesis=f"{src['thesis']} (swap {sw['id']}: reallocated from {sw['close_ticker']})",
        quant=quant,
        risk_narrative=str(src.get("risk_narrative", "")),
        sizing=Sizing(
            contracts=size.contracts,
            notional=size.max_loss_total,
            pct_equity=min(size.pct_equity, 1.0),
        ),
        expires_at=now + _dt.timedelta(seconds=settings.approval_ttl_seconds),
        limit_price=limit,
    )
    decision = evaluate_with_halt(
        switch, proposal, account, portfolio, settings, market=market, now=now, band=band
    )
    if secret is not None and decision.passed:
        decision = issue_token(decision, proposal, secret=secret, now=now, band=band)
    phash = proposal_hash(proposal)
    day = _today(ctx).isoformat()
    with ctx.conn:
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
            kind="open",
            swap_id=sw["id"],
            commit=False,
        )
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
        SwapRepo(ctx.conn).update(
            sw["id"], status="open_proposed", now=ctx.now, open_proposal_hash=phash, commit=False
        )
        JournalStore(ctx.conn).record(
            persona=JournalPersona.RISK,
            stage=Stage.REALLOCATE,
            subject=t,
            choice=Choice.SELECTED if decision.passed else Choice.FAILED,
            reason_code=ReasonCode.REALLOC_OPEN_PROPOSED,
            reason_text=proposal.thesis
            + ("" if decision.passed else f"; gate: {'; '.join(decision.violations)}"),
            proposal_hash=phash,
            payload={
                "swap_id": sw["id"],
                "sizing": size.model_dump(mode="json"),
                "band": band.model_dump(mode="json"),
            },
            at=ctx.now,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        )
        ctx.write("proposal", t, proposal)
    verdict = "PASS" if decision.passed else f"FAIL ({'; '.join(decision.violations)})"
    log.info("positions.swap_open_proposed", swap_id=sw["id"], proposal_hash=phash)
    return f"swap {sw['id']} open {t} x{size.contracts} limit {limit:+} gate {verdict}"


def _journal_pairs(ctx: JobContext, pairs: list[ScoredPair], persona_call_id: str | None) -> None:
    from arc.journal.store import JournalStore

    store = JournalStore(ctx.conn)
    for p in pairs:
        store.record(
            persona=JournalPersona.SYSTEM,
            stage=Stage.REALLOCATE,
            subject=p.open_ticker,
            choice=Choice.SELECTED if p.outcome == "suggested" else Choice.REJECTED,
            reason_code=_PAIR_CODES[p.outcome],
            reason_text=p.detail,
            payload=p.model_dump(mode="json"),
            at=ctx.now,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
            persona_call_id=persona_call_id,
        )


def _risk_verdicts(
    ctx: JobContext,
    env: PipelineEnv,
    keyed: dict[str, SwapSuggestion],
    portfolio: Portfolio,
    equity: Decimal,
) -> tuple[dict[str, tuple[bool, str]], str | None]:
    """Ask Risk (veto only). Any failure vetoes every suggestion (fail closed)."""
    from collections import Counter as _C

    from arc.personas.schemas import RiskSwapReview
    from arc.pipeline.steps import PersonaError, _ask, _record_ok

    inputs = {
        "suggestions_json": json.dumps(
            [{"swap_id": k, **s.model_dump(mode="json")} for k, s in keyed.items()],
            indent=2,
            sort_keys=True,
        ),
        "portfolio_json": portfolio.model_dump_json(indent=2),
        "account_equity": float(equity),
        "scan_date": _today(ctx).isoformat(),
        "rules": [
            "You may only approve or veto the listed swap_ids; unknown ids are ignored.",
            "A swap without a verdict is vetoed.",
        ],
    }
    try:
        reply, out = _ask(ctx, env, "risk_swap", ctx.snapshot, inputs, RiskSwapReview)
    except PersonaError as exc:
        return {k: (False, f"Risk unavailable, vetoed (fail closed): {exc}") for k in keyed}, None
    call_id = _record_ok(ctx, "risk_swap", reply, ctx.snapshot.id, _C())
    got = {v.swap_id: v for v in out.verdicts if v.swap_id in keyed}
    return {
        k: (got[k].approve, got[k].narrative) if k in got else (False, "no verdict: vetoed")
        for k in keyed
    }, call_id


def reallocate(ctx: JobContext, env: PipelineEnv) -> JobResult:
    """Advance open swaps, then score, review (Risk) and start new ones (see module doc)."""
    from arc.execution.exits import exit_pending, price_close, propose_close
    from arc.journal.store import JournalStore
    from arc.store.execution import OpenStructureRepo
    from arc.store.swaps import SwapRepo

    settings = ctx.settings
    day = _today(ctx).isoformat()
    swaps = SwapRepo(ctx.conn)
    pending = swaps.by_status("closing")
    reviews = _reviews(ctx)
    taken = swaps.sources()
    cands, sources = capacity_candidates(ctx.conn, day, taken=taken)
    if not pending and (not cands or not reviews):
        return JobResult(
            summary="no swaps to advance; no capacity-blocked entries"
            if not cands
            else "no position reviews to pair with",
            metrics={"candidates": len(cands), "suggested": 0, "swaps_started": 0},
        )
    secret = _gate_secret(ctx, env)
    book = _book(ctx, env)
    info, account, portfolio, switch = book
    lines = _advance_swaps(ctx, env, book, secret) if pending else []
    if switch.is_halted():
        return JobResult(
            summary="; ".join([*lines, "halted: no new swaps"]),
            metrics={"candidates": len(cands), "suggested": 0, "swaps_started": 0, "halted": True},
        )
    count, per_ticker = swaps.churn(day)
    suggestions, pairs = score_swaps(
        [r for r in reviews.values() if r.signal is None],  # flagged ones are closing anyway
        cands,
        _rules(settings),
        swaps_today=count,
        ticker_swaps_today=per_ticker,
    )
    if not suggestions:
        with ctx.conn:
            _journal_pairs(ctx, pairs, None)
        lines.append(f"{len(pairs)} pair(s) scored, none cleared the D19 edge/PoP/churn rules")
        return JobResult(
            summary="; ".join(lines),
            metrics={
                "candidates": len(cands),
                "pairs": len(pairs),
                "suggested": 0,
                "swaps_started": 0,
            },
        )
    keyed = {f"sg{i + 1}": s for i, s in enumerate(suggestions)}
    verdicts, call_id = _risk_verdicts(ctx, env, keyed, portfolio, info.equity)
    with ctx.conn:
        _journal_pairs(ctx, pairs, call_id)
    repo = OpenStructureRepo(ctx.conn)
    started = vetoed = 0
    alerts: list[str] = []
    for key, s in keyed.items():
        ok, why = verdicts[key]
        suggestion_json = json.dumps(
            {"suggestion": s.model_dump(mode="json"), "source": sources[s.source_ref], "risk": why}
        )
        if not ok:
            with ctx.conn:
                sid = swaps.create(
                    day=day, status="vetoed", close_structure_id=s.close_structure_id,
                    close_ticker=s.close_ticker, open_ticker=s.open_ticker,
                    source_ref=s.source_ref, suggestion_json=suggestion_json, now=ctx.now,
                    run_id=ctx.run_id, detail=why, commit=False,
                )  # fmt: skip
                JournalStore(ctx.conn).record(
                    persona=JournalPersona.RISK,
                    stage=Stage.REALLOCATE,
                    subject=s.open_ticker,
                    choice=Choice.REJECTED,
                    reason_code=ReasonCode.REALLOC_VETOED,
                    reason_text=why,
                    persona_call_id=call_id,
                    payload={"swap_id": sid, **s.model_dump(mode="json")},
                    at=ctx.now,
                    run_id=ctx.run_id,
                    chain_run_id=ctx.chain_run_id,
                )
            vetoed += 1
            lines.append(f"{s.close_ticker}->{s.open_ticker} vetoed by Risk: {why}")
            continue
        row = repo.get(s.close_structure_id)
        if row is None or row["status"] != "open" or exit_pending(ctx.conn, row):
            continue
        st = Structure.model_validate_json(row["structure_json"])
        try:
            priced = price_close(
                env.market, st, as_of=_today(ctx), r=settings.scanner_risk_free_rate
            )
        except (LookupError, ValueError) as exc:
            lines.append(f"{s.close_ticker}: cannot price the swap close ({exc})")
            continue
        rv = reviews[s.close_structure_id]
        with ctx.conn:
            sid = swaps.create(
                day=day, status="closing", close_structure_id=s.close_structure_id,
                close_ticker=s.close_ticker, open_ticker=s.open_ticker, source_ref=s.source_ref,
                suggestion_json=suggestion_json, now=ctx.now, run_id=ctx.run_id,
                detail=why, commit=False,
            )  # fmt: skip
            JournalStore(ctx.conn).record(
                persona=JournalPersona.RISK,
                stage=Stage.REALLOCATE,
                subject=s.open_ticker,
                choice=Choice.APPROVED,
                reason_code=ReasonCode.REALLOC_APPROVED,
                reason_text=why,
                persona_call_id=call_id,
                payload={"swap_id": sid, **s.model_dump(mode="json")},
                at=ctx.now,
                run_id=ctx.run_id,
                chain_run_id=ctx.chain_run_id,
            )
        res = propose_close(
            ctx.conn,
            row=row,
            priced=priced,
            thesis=(
                f"Close to reallocate (swap {sid}): {s.detail}. The {s.open_ticker} open is "
                "proposed only after this close fills."
            ),
            reason="reallocate",
            reason_code=ReasonCode.EXIT_REALLOCATE,
            persona=JournalPersona.RISK,
            close_now_net=rv.close_now_net,
            settings=settings,
            account=account,
            portfolio=portfolio,
            switch=switch,
            now=ctx.clock(),
            run_id=ctx.run_id,
            write_context=ctx.write,
            secret=secret,
            payload={"swap": s.model_dump(mode="json")},
            swap_id=sid,
        )
        swaps.update(sid, status="closing", now=ctx.now, close_proposal_hash=res.proposal_hash)
        if res.alert:
            alerts.append(res.alert)
        if res.proposal_hash is None:  # E6.2a: no close on unusable quotes, so no open
            _cancel(ctx, swaps.get(sid) or {}, "close quotes unusable")
            lines.append(f"swap {sid} cancelled: {res.line}")
            continue
        if not res.passed:
            _cancel(ctx, swaps.get(sid) or {}, "close failed the gate")
        started += 1
        lines.append(f"swap {sid}: {res.line}")
    return JobResult(
        summary="; ".join(lines),
        metrics={
            "candidates": len(cands),
            "pairs": len(pairs),
            "suggested": len(suggestions),
            "vetoed": vetoed,
            "swaps_started": started,
        },
        notice="; ".join([*(line for line in lines if "gate PASS" in line), *alerts]),
    )


# ---------------------------------------------------------------------------
# Handler entry points
# ---------------------------------------------------------------------------


def _env(ctx: JobContext) -> PipelineEnv:
    from arc.pipeline.steps import _LazyEnv

    return cast("PipelineEnv", _LazyEnv(ctx))


def evaluate_step(ctx: JobContext) -> JobResult:
    return evaluate(ctx, _env(ctx))


def exits_step(ctx: JobContext) -> JobResult:
    return exits(ctx, _env(ctx))


def reallocate_step(ctx: JobContext) -> JobResult:
    return reallocate(ctx, _env(ctx))


def position_handlers(env: PipelineEnv) -> dict[str, Handler]:
    """Dispatcher overrides binding the E6.4 chain to one *env* (fixtures / dry runs)."""
    return {
        "positions.evaluate": lambda ctx: evaluate(ctx, env),
        "investor.exits": lambda ctx: exits(ctx, env),
        "risk.reallocate": lambda ctx: reallocate(ctx, env),
    }
