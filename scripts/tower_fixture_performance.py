"""Performance history for the tower fixture DB (E8.7c): ~40 closed trades over 3+ months.

Loaded by ``scripts/tower_fixture_db.py --history`` (``build(..., history=True)``). The
default fixture stays as it is (the Overview and Trades tests pin its counts); the
Performance page tests and e2e build it with the history on top.

Everything is derived from the trade index (no randomness), anchored on the fixture's
*now*. The rows go through the same tables the live system writes:

- per trade: candidate, open proposal (``regime`` column), ``market_contexts`` analytics
  (managed + static exit model, entry slippage, fee breakdown, account profile), gate
  pass, approval (owner or auto), filled open execution + order + fill, the chain
  (``routine_runs``) with a Research ``shortlist`` pick and the Quant's ``proposed``
  decision (stated confidence / PoP for calibration);
- the close: a close proposal + filled close execution + ``exit:closed`` decision with
  the realised P&L (the scorecard's source), or a reconcile expiry settlement with the
  settle price; the ``outcomes`` row with the D19 hold-to-expiry shadow P&L; an owner or
  auditor review on some;
- a few gate-failed proposals (violation histogram) and one broker smoke-test trade (an
  ``arc-<hex>`` client id) that the page leaves out unless ``include_tests``;
- daily ``pnl_snapshots`` from 115 days back up to the default fixture's first day,
  with equity moving by each day's realised P&L plus a deterministic wobble.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from arc.context.ttl import to_db
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.repos import FillRepo, OrderRepo, PnlSnapshotRepo
from arc.structures import debit_vertical, long_call, long_put
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.models import Structure

__all__ = ["HISTORY_TRADES", "add_history"]

HISTORY_TRADES = 40
FIRST_DAY_BACK = 115  # first equity snapshot, days before now
_TICKERS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "XLE", "IWM", "META")
_REGIMES = ("bull", "bull", "chop", "bear")
_PROFILES = ("cash_debit", "cash_debit", "cash_long_only")
# (exit_reason, realised $ per contract) cycle: wins and losses, early exits and expiries
_EXITS = (
    ("profit_target", 145.0),
    ("stop", -120.0),
    ("profit_target", 92.0),
    ("dte_exit", -35.0),
    ("expiry", 210.0),
    ("stop", -160.0),
    ("reallocate", 60.0),
    ("expiry", -250.0),
    ("profit_target", 118.0),
    ("dte_exit", 22.0),
)
_EXIT_CODES = {
    "profit_target": "exit:take_profit",
    "stop": "exit:stop",
    "dte_exit": "exit:dte",
    "reallocate": "exit:reallocate",
}


def _h(tag: str) -> str:
    return hashlib.sha256(f"arc-tower-fixture:{tag}".encode()).hexdigest()


def _ins(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    conn.execute(
        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",  # noqa: S608
        list(row.values()),
    )


def _weekday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _structure(i: int, root: str, exp: dt.date, as_of: dt.date) -> Structure:
    k = 100 + 5 * (i % 7)
    shape = i % 4
    if shape == 0:
        return debit_vertical("call", root, exp, long_strike=k, long_premium="4.80",
                              short_strike=k + 10, short_premium="2.10", as_of=as_of)  # fmt: skip
    if shape == 1:
        return debit_vertical("put", root, exp, long_strike=k + 10, long_premium="5.20",
                              short_strike=k, short_premium="2.40", as_of=as_of)  # fmt: skip
    if shape == 2:
        return long_call(root, exp, k, "3.40", as_of=as_of)
    return long_put(root, exp, k, "3.10", as_of=as_of)


def _closing(st: Structure, *, close_net: Decimal) -> Structure:
    """The structure that closes *st*: every leg inverted, priced to *close_net* per share."""
    from arc.models import Leg, LegIntent
    from arc.structures import analyze, parse_occ

    legs = [
        Leg(occ_symbol=leg.occ_symbol,
            side=LegIntent.SHORT if leg.side == LegIntent.LONG else LegIntent.LONG,
            ratio=leg.ratio, premium=leg.premium)
        for leg in st.legs
    ]  # fmt: skip
    expiry = parse_occ(st.legs[0].occ_symbol).expiration
    as_of = expiry - dt.timedelta(days=st.dte)
    k = close_net / analyze(legs, as_of=as_of).net_debit_credit
    legs = [leg.model_copy(update={"premium": (leg.premium or Decimal(0)) * k}) for leg in legs]
    return analyze(legs, as_of=as_of).model_copy(update={"kind": st.kind})


def _decision(conn: sqlite3.Connection, did: str, **row: Any) -> None:
    base = {"id": did, "chain_run_id": None, "run_id": None, "reason_text": "",
            "confidence": None, "proposal_hash": None, "payload": "{}"}  # fmt: skip
    _ins(conn, "decisions", {**base, **row})


def _proposal(
    conn: sqlite3.Connection,
    tag: str,
    root: str,
    st: Structure,
    *,
    at: dt.datetime,
    contracts: int,
    kind: str = "open",
    run_id: str | None = None,
    regime: str | None = None,
    pop: float = 0.55,
) -> str:
    h = _h(tag)
    cid = f"cand-{tag}"
    _ins(conn, "candidates", {
        "id": cid, "ticker": root, "stance": "bullish", "catalyst_type": "technical",
        "confidence": 0.6, "sources": "[]", "created_at": to_db(at),
    })  # fmt: skip
    _ins(conn, "proposals", {
        "id": f"prop-{tag}", "candidate_id": cid, "proposal_hash": h,
        "structure_json": st.model_dump_json(), "thesis": f"{root} history thesis",
        "quant_json": json.dumps({"pop": pop, "ev": "15.0", "cost_bps": 30.0}),
        "sizing_json": json.dumps({"contracts": contracts, "notional": "400", "pct_equity": 0.01}),
        "expires_at": to_db(at + dt.timedelta(minutes=30)), "created_at": to_db(at),
        "run_id": run_id, "day": at.astimezone(ET).date().isoformat(), "ticker": root,
        "kind": kind, "regime": regime,
    })  # fmt: skip
    return h


def _gate(conn: sqlite3.Connection, h: str, at: dt.datetime, violations: list[str]) -> None:
    _ins(conn, "gate_decisions", {
        "id": f"gd-{h[:16]}", "proposal_hash": h, "passed": int(not violations),
        "violations_json": json.dumps(violations),
        "decided_at": to_db(at + dt.timedelta(seconds=20)),
    })  # fmt: skip


def _approve(conn: sqlite3.Connection, h: str, root: str, at: dt.datetime, by: str) -> None:
    _ins(conn, "approval_requests", {
        "proposal_hash": h, "ticker": root, "day": at.date().isoformat(), "proposal_json": "{}",
        "status": "approved", "channel": "log", "expires_at": to_db(at + dt.timedelta(minutes=30)),
        "created_at": to_db(at + dt.timedelta(seconds=30)),
        "decided_at": to_db(at + dt.timedelta(minutes=1)), "decided_by": by,
    })  # fmt: skip


def _fill(  # noqa: PLR0913 - one execution row
    conn: sqlite3.Connection,
    h: str,
    *,
    at: dt.datetime,
    kind: str,
    contracts: int,
    price: Decimal,
    structure_id: str | None = None,
    client_id: str | None = None,
) -> None:
    ex = ExecutionRepo(conn)
    ex.start(proposal_hash=h, kind=kind, token_version="arc2", band_lo=price - Decimal("0.2"),
             band_hi=price + Decimal("0.2"), max_steps=3, contracts=contracts, now=at,
             structure_id=structure_id)  # fmt: skip
    ex.attempt(h)
    ex.finish(h, status="filled", filled_qty=contracts, fill_price=price, steps_used=1,
              now=at + dt.timedelta(minutes=2))  # fmt: skip
    oid = OrderRepo(conn).create(
        proposal_hash=h, client_order_id=client_id or f"arc2.{h[:16]}.s1",
        created_at=to_db(at),
    )  # fmt: skip
    FillRepo(conn).insert(
        order_id=oid, qty=contracts, price=str(price), filled_at=to_db(at + dt.timedelta(minutes=2))
    )


def _analytics(i: int, contracts: int, pop: float, profile: str) -> dict[str, Any]:
    managed_ev = round(18.0 - 4.0 * (i % 5), 2)
    return {
        "entry_slippage": round(4.0 + (i % 3), 2),
        "entry_fees": {
            "commission": 0.65 * (i % 2),
            "orf": 0.04,
            "occ": 0.05,
            "cat": 0.0,
            "taf": 0.0,
            "sec": 0.0,
        },  # fmt: skip
        "account_profile": profile,
        "exit_model": {
            "managed": {"net_ev": managed_ev, "pop": round(pop + 0.03, 3)},
            "static": {"net_ev": round(managed_ev - 3.5, 2), "pop": round(pop - 0.04, 3)},
        },
    }


def _trade(  # noqa: PLR0915 - one linear trade script
    conn: sqlite3.Connection, i: int, now: dt.datetime, *, test_leg: bool = False
) -> tuple[dt.date, float]:
    """One closed trade; returns (close day, realised $)."""
    tag = f"hist-{i:02d}" if not test_leg else "hist-smoke"
    root = _TICKERS[i % len(_TICKERS)]
    opened_day = _weekday((now - dt.timedelta(days=115 - int(i * 2.2))).date())
    hold = 3 + (i * 5) % 11
    reason, per = _EXITS[i % len(_EXITS)]
    contracts = 1 + i % 3
    opened = dt.datetime.combine(opened_day, dt.time(10, 5 + i % 40), tzinfo=ET)
    close_day = _weekday(opened_day + dt.timedelta(days=hold))
    expiry = reason == "expiry"
    exp = close_day if expiry else close_day + dt.timedelta(days=12)
    st = _structure(i, root, exp, opened_day)
    regime = _REGIMES[i % len(_REGIMES)]
    profile = _PROFILES[i % len(_PROFILES)]
    pop = round(0.45 + 0.05 * (i % 6), 2)
    chain, run = f"chain-{tag}", f"run-{tag}"
    _ins(conn, "routine_runs", {
        "run_id": run, "job": "quant", "chain_run_id": chain, "step_index": 2,
        "reason": "schedule", "scheduled_for": to_db(opened - dt.timedelta(minutes=5)),
        "started_at": to_db(opened - dt.timedelta(minutes=5)),
        "finished_at": to_db(opened - dt.timedelta(minutes=1)), "status": "ok",
    })  # fmt: skip
    h = _proposal(conn, tag, root, st, at=opened, contracts=contracts, run_id=run,
                  regime=regime, pop=pop)  # fmt: skip
    _ins(conn, "market_contexts", {
        "id": f"mc-{tag}", "proposal_hash": h, "quotes_as_of": to_db(opened),
        "payload": json.dumps({"regime": regime,
                               "analytics": _analytics(i, contracts, pop, profile)}),
        "created_at": to_db(opened),
    })  # fmt: skip
    _decision(conn, f"dec-{tag}-dir", chain_run_id=chain, persona="research", stage="shortlist",
              subject=root, choice="selected", reason_code="shortlisted",
              confidence=round(0.5 + 0.08 * (i % 5), 2),
              at=to_db(opened - dt.timedelta(minutes=4)))  # fmt: skip
    _decision(conn, f"dec-{tag}-prop", chain_run_id=chain, run_id=run, persona="quant",
              stage="propose", subject=root, choice="selected", reason_code="proposed",
              proposal_hash=h, payload=json.dumps({"quant": {"pop": pop}}),
              at=to_db(opened))  # fmt: skip
    _gate(conn, h, opened, [])
    _approve(conn, h, root, opened, "arc:auto-approve" if i % 3 == 0 else "U0OWNER001")
    entry = st.net_debit_credit
    client = f"arc-{h[:12]}" if test_leg else None
    _fill(conn, h, at=opened + dt.timedelta(minutes=3), kind="open", contracts=contracts,
          price=entry + Decimal("0.05"), client_id=client)  # fmt: skip
    sid = OpenStructureRepo(conn).open(
        ticker=root, open_proposal_hash=h, candidate_id=f"cand-{tag}",
        structure_json=st.model_dump_json(), contracts=contracts,
        entry_net=entry + Decimal("0.05"), now=opened + dt.timedelta(minutes=5), commit=False,
    )  # fmt: skip
    realised = round(per * contracts * (1.0 + 0.1 * (i % 4)), 2)
    fill_entry = entry + Decimal("0.05")
    close_net = -(fill_entry + Decimal(str(realised)) / (100 * contracts))
    close_net = close_net.quantize(Decimal("0.01"))
    close_net = min(close_net, Decimal("-0.05"))  # a long structure never costs to close
    realised = float(-(fill_entry + close_net) * 100 * contracts)
    closed_at = dt.datetime.combine(close_day, dt.time(14, 30), tzinfo=ET)
    shadow = round(realised * (1.4 if i % 3 == 0 else 0.5) - 40.0 * (i % 2), 2)
    if expiry:
        closed_at = dt.datetime.combine(close_day, dt.time(16, 35), tzinfo=ET)
        _decision(conn, f"dec-{tag}-exp", persona="auditor", stage="reconcile", subject=root,
                  choice="noted", reason_code="reconcile:expired",
                  payload=json.dumps({"structure_id": sid, "realized_pnl": str(realised),
                                      "settle": str(100 + 5 * (i % 7) + 3)}),
                  at=to_db(closed_at))  # fmt: skip
        shadow = realised
    else:
        hc = _proposal(conn, f"{tag}-close", root,
                       _closing(st, close_net=close_net - Decimal("0.03")),
                       at=closed_at - dt.timedelta(minutes=6), contracts=contracts,
                       kind="close")  # fmt: skip
        _gate(conn, hc, closed_at - dt.timedelta(minutes=6), [])
        _approve(conn, hc, root, closed_at - dt.timedelta(minutes=6), "U0OWNER001")
        OpenStructureRepo(conn).set_exit(sid, proposal_hash=hc, reason=reason,
                                         day=close_day.isoformat(), commit=False)  # fmt: skip
        _fill(conn, hc, at=closed_at - dt.timedelta(minutes=4), kind="close",
              contracts=contracts, price=close_net, structure_id=sid)  # fmt: skip
        _decision(conn, f"dec-{tag}-exit", persona="investor", stage="exit", subject=root,
                  choice="selected", reason_code=_EXIT_CODES[reason], proposal_hash=hc,
                  at=to_db(closed_at - dt.timedelta(minutes=7)))  # fmt: skip
        _decision(conn, f"dec-{tag}-closed", persona="system", stage="order", subject=root,
                  choice="filled", reason_code="exit:closed", proposal_hash=hc,
                  payload=json.dumps({"structure_id": sid, "realized_pnl": str(realised)}),
                  at=to_db(closed_at))  # fmt: skip
    OpenStructureRepo(conn).reduce(sid, closed_qty=contracts, close_net=close_net, now=closed_at,
                                   commit=False)  # fmt: skip
    _ins(conn, "outcomes", {
        "id": f"out-{tag}", "proposal_hash": h, "status": "closed", "contracts": contracts,
        "limit_price": str(entry), "entry_fill": str(fill_entry), "slippage_usd": "5.00",
        "slippage_bps": 12.0, "cost_bps": 30.0, "exit_fill": str(close_net),
        "realised_pnl": f"{realised:.2f}", "days_held": hold, "exit_reason": reason,
        "hold_to_expiry_shadow_pnl": f"{shadow:.2f}", "at": to_db(closed_at),
    })  # fmt: skip
    if i % 4 == 0:
        label = "good_decision_good_outcome" if realised > 0 else "good_decision_bad_outcome"
        _ins(conn, "decision_reviews", {
            "id": f"rev-{tag}", "proposal_hash": h, "decision_id": None, "label": label,
            "root_cause": "exit_management" if realised > 0 else "thesis",
            "notes": f"{root}: history review", "reviewer": "owner" if i % 8 == 0 else "auditor",
            "at": to_db(closed_at + dt.timedelta(hours=3)),
        })  # fmt: skip
    return close_day, realised


def add_history(conn: sqlite3.Connection, *, now: dt.datetime, first_equity_day: dt.date,
                base_equity: Decimal) -> None:  # fmt: skip
    """Add the history rows; equity snapshots end the day before *first_equity_day*."""
    realised_by_day: dict[dt.date, float] = {}
    for i in range(HISTORY_TRADES):
        day, pnl = _trade(conn, i, now)
        realised_by_day[day] = realised_by_day.get(day, 0.0) + pnl
    day, pnl = _trade(conn, 41, now, test_leg=True)  # the broker smoke test (arc-<hex>)
    realised_by_day[day] = realised_by_day.get(day, 0.0) + pnl

    # Gate-failed proposals across the period (violation histogram).
    fails = ("per_underlying_limit: max loss over cap", "spread_too_wide: 18% > 12%",
             "dte_window: 5 < 14", "spread_too_wide: 22% > 12%",
             "portfolio_delta_cap: 310 > 300", "earnings_blackout: AAPL reports in 2d")  # fmt: skip
    for k, v in enumerate(fails):
        at = dt.datetime.combine(
            _weekday((now - dt.timedelta(days=100 - 15 * k)).date()), dt.time(11), tzinfo=ET
        )
        root = _TICKERS[(k + 2) % len(_TICKERS)]
        exp = at.date() + dt.timedelta(days=30)
        h = _proposal(conn, f"hist-fail-{k}", root, _structure(k, root, exp, at.date()), at=at,
                      contracts=1)  # fmt: skip
        _gate(conn, h, at, [v])

    # Daily equity: every weekday from FIRST_DAY_BACK to the day before first_equity_day.
    days = []
    d = (now - dt.timedelta(days=FIRST_DAY_BACK)).date()
    while d < first_equity_day:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    moves = [
        realised_by_day.get(d, 0.0) + round(55 * math.sin(n / 3.1) - 8, 2)
        for n, d in enumerate(days)
    ]
    # Walk back from base_equity on the last day, so the default fixture's 10 days follow on.
    equities = [base_equity]
    for mv in reversed(moves[1:]):
        equities.append(equities[-1] - Decimal(str(round(mv, 2))))
    equities.reverse()
    prev = equities[0]
    for d, eq in zip(days, equities, strict=True):
        PnlSnapshotRepo(conn).insert(
            realized=f"{realised_by_day.get(d, 0.0):.2f}", unrealized="0",
            total=str(eq - base_equity),
            details_json=json.dumps({"day": d.isoformat(), "equity": str(eq),
                                     "last_equity": str(prev), "day_pnl": str(eq - prev),
                                     "open_structures": 2, "closed_today": 0, "clean": True}),
            snapshot_at=to_db(dt.datetime.combine(d, dt.time(16, 35), tzinfo=ET)), commit=False,
        )  # fmt: skip
        prev = eq
