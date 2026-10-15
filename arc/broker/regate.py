"""D101 (E24.1): re-gate an open ladder whose market moved past its band.

The ladder (:mod:`arc.execution.ladder`) re-reads the legs' quotes before every
step k >= 1 through :func:`quote_reader`. When the fresh mid is past the band's
worst price it calls the :func:`regater` built here, which re-prices the **same**
structure, legs and quantity at the fresh mid and runs the **full** gate on it:

- the approval TTL (the re-priced proposal keeps the approved ``expires_at``);
- opens: the live Net EV floor (E6.4a) and, in the D32 restrictive tier, its
  stricter floors, on the fresh quotes (no Research/Quant/Risk LLM call);
- the band (:func:`arc.gate.rules.price_band` on the fresh snapshot) and every
  gate rule via :func:`arc.gate.halt.evaluate_with_halt` (halt, budget, max loss at
  the band's worst price, ``max_gain_cap``, cash, sleeves, Greek caps, ...);
- the D101 price-move ceiling (:func:`arc.gate.band.regate_move_ok`), skipped for
  a close inside the D73 expiry guard's closing window.

On a pass a new ``arc2`` token is minted for the re-priced proposal, so the old
token can never authorise a price in the new band (its proposal hash and band
differ). The re-priced proposal lives in memory only: the original proposal,
its approval and its ``executions`` row stay the record; the re-gate is
journalled (``order:band_moved``, ``gate:pass`` / ``gate:<rule>``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.execution.ladder import MarketRead, RegateResult
    from arc.gate.band import PriceBand
    from arc.models import ApprovalRecord, Proposal
    from arc.pipeline.market import PricedStructure

__all__ = ["quote_reader", "regater"]

log = structlog.get_logger(__name__)


def _legs(proposal: Proposal) -> list[tuple[str, Any, int]]:
    return [(leg.occ_symbol, leg.side, leg.ratio) for leg in proposal.structure.legs]


def _read(
    market: MarketDataProvider,
    proposal: Proposal,
    *,
    now: _dt.datetime,
    r: float,
    require_iv: bool = False,
) -> MarketRead | None:
    from arc.execution.ladder import MarketRead
    from arc.gate.rules import combo_nbbo
    from arc.pipeline.market import market_snapshot, price_structure
    from arc.utils.calendar import ET

    try:
        priced = price_structure(
            market, _legs(proposal), as_of=now.astimezone(ET).date(), r=r, require_iv=require_iv
        )
    except (LookupError, ValueError) as exc:
        log.warning("broker.band_check_no_quote", error=str(exc))
        return None
    snap = market_snapshot(priced.contracts, {})
    nbbo = combo_nbbo(proposal.structure.legs, snap)
    stamps = [c.quote_timestamp for c in priced.contracts.values() if c.quote_timestamp]
    age = (now - min(stamps)).total_seconds() if stamps else None
    return MarketRead(
        mid=Decimal(priced.structure.net_debit_credit),
        far=nbbo[1] if nbbo is not None else None,
        quote_age_s=age,
        context=priced,
    )


def quote_reader(
    market: MarketDataProvider,
    proposal: Proposal,
    *,
    clock: Callable[[], _dt.datetime],
    r: float,
) -> Callable[[], MarketRead | None]:
    """D101: the between-step quote check (same provider and pricing as ``fresh_mid_of``)."""

    def _quotes() -> MarketRead | None:
        return _read(market, proposal, now=clock(), r=r)

    return _quotes


def _result(status: str, detail: str, **payload: Any) -> RegateResult:
    from arc.execution.ladder import RegateResult

    return RegateResult(status, detail, payload={k: v for k, v in payload.items() if v is not None})


def regater(
    *,
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    market: MarketDataProvider,
    settings: ArcSettings,
    proposal: Proposal,
    approval: ApprovalRecord | None,
    kind: str,
    clock: Callable[[], _dt.datetime],
) -> Callable[[PriceBand, MarketRead, int], RegateResult]:
    """The ladder's D101 re-gate for *proposal* (the approved one, never a re-gated copy).

    The returned callable takes the approved band, the fresh quote read and the
    re-gate's ordinal, and returns a :class:`arc.execution.ladder.RegateResult`.
    It never submits and never calls an LLM; any exception fails closed in the ladder.
    """

    def _regate(approved: PriceBand, read: MarketRead, n: int) -> RegateResult:
        return _run(
            conn=conn,
            broker=broker,
            market=market,
            settings=settings,
            proposal=proposal,
            approval=approval,
            kind=kind,
            now=clock(),
            approved=approved,
            read=read,
            n=n,
        )

    return _regate


def _closing_window(settings: ArcSettings, proposal: Proposal, now: _dt.datetime) -> Any:
    from arc.control.effective import exit_config
    from arc.execution.exits import structure_expiry
    from arc.exits.expiry import closing_window
    from arc.utils.calendar import ET

    guard = exit_config(settings).positions.expiry_guard
    today = now.astimezone(ET).date()
    return closing_window(structure_expiry(proposal.structure), today, guard), guard


def _realized_vol(conn: sqlite3.Connection, ticker: str, now: _dt.datetime) -> float | None:
    """Realised-vol forecast from the ticker's latest active ``regime`` entry (read-only)."""
    from arc.context.store import ContextStore
    from arc.exits import realized_vol_forecast

    try:
        rows = ContextStore(conn).query(as_of=now, kinds=["regime"], subjects=[ticker])
    except Exception:  # noqa: BLE001 - no regime context: the model runs without realised vol
        return None
    vol = rows[-1].payload.get("vol") if rows else None
    if not isinstance(vol, dict):
        return None
    return realized_vol_forecast(vol.get("hv20"), vol.get("hv60"))


def _ev_floor(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    priced: PricedStructure,
    *,
    ticker: str,
    now: _dt.datetime,
    restricted: bool,
) -> tuple[bool, str]:
    """Opens: the E6.4a live Net EV floor (and the D32 restrictive floors) on fresh quotes."""
    from arc.control.effective import cost_model, exit_config, ranking_config
    from arc.pipeline import steps
    from arc.scanner.rank import live_net_ev_check

    exits = exit_config(settings)
    cost = cost_model(settings)
    st = priced.structure
    rv = _realized_vol(conn, ticker, now)
    model = steps._proposal_exit_model(  # noqa: SLF001 - same model as quant.propose
        priced,
        exits,
        settings.scanner_risk_free_rate,
        rv,
        cost,
        settings.exit_block_for_dte(st.dte),
    )
    ok, why = live_net_ev_check(
        None if model is None else model.managed.net_ev, ranking_config(settings).filters
    )
    if not ok or not restricted:
        return ok, why
    return steps._restrictive_check(priced, exits, settings, cost, rv)  # noqa: SLF001


def _run(
    *,
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    market: MarketDataProvider,
    settings: ArcSettings,
    proposal: Proposal,
    approval: ApprovalRecord | None,
    kind: str,
    now: _dt.datetime,
    approved: PriceBand,
    read: MarketRead,
    n: int,
) -> RegateResult:
    from arc.budget.orders import current_budget, tier_settings
    from arc.execution.ladder import Regated, RegateResult
    from arc.gate.band import regate_move_ok
    from arc.gate.halt import HaltSwitch, evaluate_with_halt
    from arc.gate.rules import grid_for, price_band
    from arc.gate.rules import proposal_hash as hash_proposal
    from arc.gate.token import gate_secret, issue_token
    from arc.pipeline.market import (
        account_baseline,
        account_snapshot,
        build_portfolio,
        limit_price,
        live_gate_status,
        market_snapshot,
        next_earnings,
        proposal_betas,
    )
    from arc.store.repos import HaltRepo
    from arc.structures import parse_occ
    from arc.utils.calendar import ET

    if now >= proposal.expires_at:
        return _result("ttl_expired", f"approval TTL ended at {proposal.expires_at.isoformat()}")
    closing = kind == "close"
    window, guard = _closing_window(settings, proposal, now) if closing else (None, None)
    in_window = bool(window is not None and window.in_window)
    close_max_steps = guard.steps if in_window and guard is not None else None

    priced: PricedStructure | None = read.context
    if not closing:  # opens: the EV model needs IVs and the spot
        fresh = _read(market, proposal, now=now, r=settings.scanner_risk_free_rate, require_iv=True)
        priced = fresh.context if fresh is not None else None
    if priced is None:
        return _result("no_quote", "no usable quote to re-price at")

    root = parse_occ(proposal.structure.legs[0].occ_symbol).root
    day = now.astimezone(ET).date()
    # This ladder's own remaining reservation is not counted twice (the ladder excludes it too).
    own = hash_proposal(proposal)
    budget = current_budget(conn, broker, settings, now=now, exclude_proposal_hash=own)
    gs = settings if closing else tier_settings(settings, budget.tier)
    if not closing:
        ok, why = _ev_floor(
            conn, gs, priced, ticker=root, now=now, restricted=budget.tier.restricted
        )
        if not ok:
            return _result("gate_failed", f"Net EV floor: {why}", ev_floor=why)

    snap = market_snapshot(
        priced.contracts,
        {} if closing else next_earnings(conn, [root], day),
        {root: priced.spot},
        proposal_betas(conn, [root], day),
        quote_ref_time=priced.quote_ref_times(now),
    )
    st = priced.structure.model_copy(update={"sleeve": proposal.structure.sleeve})
    limit = limit_price(Decimal(st.net_debit_credit), grid_for(st.legs, snap, gs))
    band = price_band(st.legs, limit, snap, gs, max_steps=close_max_steps)
    move = {
        "approved_band_lo": str(approved.lo),
        "approved_band_hi": str(approved.hi),
        "regated_band_lo": str(band.lo),
        "regated_band_hi": str(band.hi),
        "max_move_pct": settings.execution_regate_max_move_pct,
        "expiry_window": in_window or None,
    }
    ceiling = Decimal(str(settings.execution_regate_max_move_pct))
    if not in_window and not regate_move_ok(approved, band, ceiling):
        return _result(
            "refused_move",
            f"new worst price {band.hi:+} is more than {ceiling:.0%} of the approved mid "
            f"{approved.lo:+} past the approved worst price {approved.hi:+}",
            **move,
        )

    regated = proposal.model_copy(update={"structure": st, "limit_price": limit})
    info, positions = broker.account(), broker.positions()
    fetched = now
    account = account_snapshot(
        info,
        fetched,
        baseline=account_baseline(conn, info, fetched),
        orders_used_today=budget.used,
        live_gate_met=live_gate_status(conn, gs, now=fetched),
    )
    portfolio = build_portfolio(
        conn,
        positions,
        market,
        now=now,
        wash_sale_days=gs.wash_sale_days,
        r=gs.scanner_risk_free_rate,
        spot_max_spread_pct=gs.spot_max_spread_pct,
        exclude_reserved=own,  # D116: the order being re-gated is not its own deployed
    )
    decision = evaluate_with_halt(
        HaltSwitch(HaltRepo(conn)),
        regated,
        account,
        portfolio,
        gs,
        market=snap,
        now=now,
        band=band,
        closing=closing,
        close_max_steps=close_max_steps,
    )
    if not decision.passed:
        return _result(
            "gate_failed",
            "; ".join(decision.violations)[:500] or "gate did not pass",
            violations=list(decision.violations),
            **move,
        )
    decision = issue_token(decision, regated, secret=gate_secret(settings), now=now, band=band)
    new_hash = hash_proposal(regated)
    bound = approval.model_copy(update={"proposal_hash": new_hash}) if approval else None
    log.info("broker.regated", n=n, ticker=root, lo=str(band.lo), hi=str(band.hi))
    return RegateResult(
        "passed",
        f"gate passed at mid {limit:+}",
        regated=Regated(proposal=regated, decision=decision, approval=bound, band=band),
        payload={k: v for k, v in move.items() if v is not None} | {"regated_hash": new_hash},
    )
