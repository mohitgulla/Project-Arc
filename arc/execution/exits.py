"""Exit proposals for open structures (E6.2; exit rules are E2.4's; E6.4 reuses the body).

Each structure in ``open_structures`` is marked at current mids and checked with
:func:`arc.exits.evaluate_position` against ``config/exits.yaml`` (the same policy
as the proposal card and backtests; no thresholds live here). When a rule fires,
the closing order is proposed like an entry (:func:`propose_close`):

1. the closing legs (every intent inverted) are re-priced at mid,
2. the gate evaluates it with ``closing=True`` over the D24 price band (the
   halt, TTL, freshness, spread/NBBO/tick and band checks all still run; every
   leg must reduce a held position),
3. a passed decision gets an ``arc2`` token, and the proposal (``kind='close'``)
   goes to the approval sweep, which posts an ``[Investor] Exit`` card,
4. once approved, the Investor works it through the band (:mod:`arc.routines.investor`).

At most one exit proposal per structure per ET day, and none while one is still
pending or working. Stops are evaluated on end-of-day marks only (D23): marks
count as end of day from ``eod_marks_from`` (default 15:30 ET), so the last
monitor runs of the session can still fire the stop while the market is open.
No exits are proposed while trading is halted.

E6.4: the intraday ``positions.evaluate → investor.exits → risk.reallocate`` chain
(:mod:`arc.positions`) calls :func:`propose_close` for review signals (profit
target, time-adjusted target, remaining-EV floor, stop, DTE) and for the close
leg of a close-to-reallocate swap. :func:`propose_exits` is the E6.2 monitor path.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.exits import load_exit_config
from arc.exits.policy import ExitReason
from arc.exits.position import OpenPosition, PositionMarks, evaluate_position
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.models import LegIntent, Proposal, QuantMetrics, Sizing, Structure
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.exits import ExitConfig
    from arc.gate.halt import HaltSwitch
    from arc.gate.inputs import AccountSnapshot, Portfolio
    from arc.pipeline.market import PricedStructure

__all__ = [
    "EXIT_CODES",
    "CloseOutcome",
    "ExitRun",
    "exit_legs",
    "exit_pending",
    "price_close",
    "propose_close",
    "propose_exits",
]

log = structlog.get_logger(__name__)

EXIT_CODES = {
    ExitReason.TAKE_PROFIT: ReasonCode.EXIT_TAKE_PROFIT,
    ExitReason.STOP: ReasonCode.EXIT_STOP,
    ExitReason.DTE_EXIT: ReasonCode.EXIT_DTE,
    ExitReason.EXPIRY: ReasonCode.EXIT_EXPIRY,
}


@dataclass
class ExitRun:
    evaluated: int = 0
    proposed: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CloseOutcome:
    """What :func:`propose_close` wrote: the close proposal and its gate verdict."""

    proposal_hash: str
    passed: bool
    violations: list[str]
    line: str


def exit_legs(structure: Structure) -> list[tuple[str, LegIntent, int]]:
    """The legs that close *structure*: every intent inverted, same ratio."""
    return [
        (
            leg.occ_symbol,
            LegIntent.SHORT if leg.side == LegIntent.LONG else LegIntent.LONG,
            leg.ratio,
        )
        for leg in structure.legs
    ]


def _eod(now: _dt.datetime, eod_from: _dt.time) -> bool:
    return now.astimezone(ET).time() >= eod_from


def exit_pending(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """True while this structure's last exit is still awaiting a decision or working."""
    phash = row["exit_proposal_hash"]
    if not phash:
        return False
    req = conn.execute(
        "SELECT status FROM approval_requests WHERE proposal_hash = ?", (phash,)
    ).fetchone()
    if req is None or req["status"] == "pending":
        return True  # not posted yet, or waiting for a click
    ex = conn.execute("SELECT status FROM executions WHERE proposal_hash = ?", (phash,)).fetchone()
    if req["status"] == "approved" and (ex is None or ex["status"] == "working"):
        return True
    return ex is not None and ex["status"] == "unconfirmed"  # reconcile first


def price_close(
    market: MarketDataProvider, st: Structure, *, as_of: _dt.date, r: float
) -> PricedStructure:
    """The closing legs of *st* re-priced at mid (never blocked on a missing IV)."""
    from arc.pipeline.market import price_structure

    return price_structure(
        market,
        exit_legs(st),
        as_of=as_of,
        r=r,
        require_iv=False,  # a close needs mids only; never block an exit on IV
    )


def propose_close(
    conn: sqlite3.Connection,
    *,
    row: dict[str, Any],
    priced: PricedStructure,
    thesis: str,
    reason: str,
    reason_code: ReasonCode,
    persona: JournalPersona,
    close_now_net: float,
    settings: ArcSettings,
    account: AccountSnapshot,
    portfolio: Portfolio,
    switch: HaltSwitch,
    now: _dt.datetime,
    run_id: str | None,
    write_context: Any,
    mint: bool,
    payload: dict[str, Any],
    swap_id: str | None = None,
) -> CloseOutcome:
    """Propose closing the open structure *row* at the re-priced *priced* legs.

    Gate (``closing=True``) over the D24 band, ``arc2`` token on PASS when *mint*,
    then one transaction: proposal (``kind='close'``) + gate decision + the
    structure's pending exit + a journal row + the ``proposal`` context entry the
    approval card renders from. Never submits: the Investor does, after approval.
    """
    from arc.gate.halt import evaluate_with_halt
    from arc.gate.rules import price_band, proposal_hash
    from arc.gate.token import TokenError, gate_secret, issue_token
    from arc.pipeline.market import limit_price, market_snapshot
    from arc.store.execution import OpenStructureRepo
    from arc.store.repos import GateDecisionRepo, ProposalRepo

    t = str(row["ticker"])
    day = now.astimezone(ET).date().isoformat()
    close = priced.structure
    limit = limit_price(close.net_debit_credit, settings.limit_tick)
    snap = market_snapshot(priced.contracts, {})
    band = price_band(close.legs, limit, snap, settings)
    n = int(row["contracts"])
    proposal = Proposal(
        candidate_id=str(row["candidate_id"]),
        structure=close,
        thesis=thesis,
        quant=QuantMetrics(pop=0.0, ev=Decimal(str(close_now_net))),
        risk_narrative="",
        sizing=Sizing(
            contracts=n,
            notional=abs(limit) * 100 * n,
            pct_equity=min(float(abs(limit) * 100 * n / account.equity), 1.0)
            if account.equity > 0
            else 0.0,
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
        market=snap,
        now=now,
        band=band,
        closing=True,
    )
    if mint and decision.passed:
        try:
            decision = issue_token(
                decision, proposal, secret=gate_secret(settings), now=now, band=band
            )
        except TokenError as exc:
            log.warning("exits.no_gate_token", reason=str(exc))
    phash = proposal_hash(proposal)
    with conn:
        ProposalRepo(conn).insert(
            candidate_id=proposal.candidate_id,
            proposal_hash=phash,
            structure_json=close.model_dump_json(),
            thesis=proposal.thesis,
            quant_json=proposal.quant.model_dump_json(),
            sizing_json=proposal.sizing.model_dump_json(),
            expires_at=proposal.expires_at.isoformat(),
            created_at=now.isoformat(),
            run_id=run_id,
            day=day,
            ticker=t,
            kind="close",
            swap_id=swap_id,
            commit=False,
        )
        GateDecisionRepo(conn).insert(
            proposal_hash=phash,
            passed=decision.passed,
            violations=decision.violations,
            token=decision.token,
            account_snapshot=decision.account_snapshot,
            decided_at=now.isoformat(),
            run_id=run_id,
            commit=False,
        )
        OpenStructureRepo(conn).set_exit(
            row["id"], proposal_hash=phash, reason=reason, day=day, commit=False
        )
        JournalStore(conn).record(
            persona=persona,
            stage=Stage.EXIT,
            subject=t,
            choice=Choice.SELECTED if decision.passed else Choice.NO_TRADE,
            reason_code=reason_code if decision.passed else ReasonCode.EXIT_NOT_PROPOSED,
            reason_text=proposal.thesis
            + ("" if decision.passed else f"; gate: {'; '.join(decision.violations)}"),
            proposal_hash=phash,
            payload={
                "structure_id": row["id"],
                "fired": reason,
                "limit_price": str(limit),
                "band": band.model_dump(mode="json"),
                **({"swap_id": swap_id} if swap_id else {}),
                **payload,
            },
            at=now,
            run_id=run_id,
        )
        write_context("proposal", t, proposal)
    verdict = "PASS" if decision.passed else f"FAIL ({'; '.join(decision.violations)})"
    line = f"{t} exit {reason} x{n} limit {limit:+} worst {band.hi:+} gate {verdict}"
    log.info(
        "exits.proposed",
        ticker=t,
        structure_id=row["id"],
        fired=reason,
        proposal_hash=phash,
        passed=decision.passed,
        swap_id=swap_id,
    )
    return CloseOutcome(phash, decision.passed, list(decision.violations), line)


def propose_exits(
    conn: sqlite3.Connection,
    *,
    market: MarketDataProvider,
    settings: ArcSettings,
    account: AccountSnapshot,
    portfolio: Portfolio,
    switch: HaltSwitch,
    now: _dt.datetime,
    run_id: str | None,
    write_context: Any,
    mint: bool,
    eod_from: _dt.time = _dt.time(15, 30),
    exits: ExitConfig | None = None,
) -> ExitRun:
    """Evaluate every open structure; propose (gate + token + card) the ones whose rule fired.

    ``write_context(kind, subject, payload)`` records the proposal context entry the
    approval card is rendered from (``JobContext.write``).
    """
    from arc.store.execution import OpenStructureRepo

    out = ExitRun()
    if switch.is_halted():
        out.lines.append("halted: no exit proposals")
        return out
    cfg = exits or load_exit_config()
    repo = OpenStructureRepo(conn)
    today = now.astimezone(ET).date()
    day = today.isoformat()
    for row in repo.list_open():
        t = row["ticker"]
        if exit_pending(conn, row) or row.get("exit_day") == day:
            continue
        out.evaluated += 1
        st = Structure.model_validate_json(row["structure_json"])
        try:
            priced = price_close(market, st, as_of=today, r=settings.scanner_risk_free_rate)
            mids = {k: float(c.mid) for k, c in priced.contracts.items() if c.mid is not None}
            state = evaluate_position(
                OpenPosition(
                    structure=st, entry_net=float(row["entry_net"]), contracts=row["contracts"]
                ),
                PositionMarks(
                    as_of=today,
                    leg_mids=mids,
                    leg_spreads=priced.leg_spreads(),
                    end_of_day=_eod(now, eod_from),
                ),
                cfg.policy_for(st.kind),
            )
        except (LookupError, ValueError) as exc:
            out.errors.append(f"{t}: cannot evaluate exit ({exc})")
            log.warning("exits.evaluate_failed", ticker=t, structure_id=row["id"], error=str(exc))
            continue
        if state.fired is None:
            continue
        res = propose_close(
            conn,
            row=row,
            priced=priced,
            thesis=(
                f"Exit ({state.fired.value}): P&L {state.pnl_total:+,.2f} "
                f"({state.pnl_per_share:+.2f}/sh), {state.dte} DTE; structure {row['id']}"
            ),
            reason=state.fired.value,
            reason_code=EXIT_CODES[state.fired],
            persona=JournalPersona.INVESTOR,
            close_now_net=state.close_now_net,
            settings=settings,
            account=account,
            portfolio=portfolio,
            switch=switch,
            now=now,
            run_id=run_id,
            write_context=write_context,
            mint=mint,
            payload={"state": state.model_dump(mode="json")},
        )
        out.proposed.append(res.proposal_hash)
        out.lines.append(res.line)
    return out
