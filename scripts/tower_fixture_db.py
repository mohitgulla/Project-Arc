"""Build a populated scratch audit store for the control tower (E8.7a; E8.7b-d reuse it).

    .venv/bin/python scripts/tower_fixture_db.py <out.db> [--now 2026-09-28T15:40:00-04:00]

Everything is anchored on *now* (default: the current ET time), so a tower served over
the DB right after building it reads as live; move the browser clock forward past the
monitor's stale threshold (3 x 5 min) to see the stale state. The DB holds:

- 3 open structures (SPY bull call debit vertical, QQQ bear put debit vertical, NVDA
  long call) with an exit pending on QQQ, and one closed structure (AMD);
- 10 ET days of ``pnl_snapshots`` (one reconcile MISMATCH day) and 30 ``monitor``
  heartbeats at 5-min cadence carrying equity, Greeks, the D32 order budget and
  per-leg broker marks; ``tick`` and ``health`` heartbeats;
- proposals in every status: proposed (no gate yet), gate FAIL (two violations),
  approval pending, approved + working, rejected, expired, execution cancelled,
  filled (with orders + fills), a close (exit) proposal;
- one active halt, one open and one resolved ops alert, a reconcile ``held`` flag
  (NVDA not held at the broker);
- E8.7b Trades drill-down rows (``scripts/tower_fixture_trades.py``): the SPY open has
  every detail section (chain, persona calls, decision trail, full market context,
  regime snapshot, run manifest, order events) and AMD has a close-to-reallocate pair
  (AMD close -> XLE open), an outcome and an owner review;
- with ``--ops``, the E8.7d Ops page rows (``scripts/tower_fixture_ops.py``);
- a real ``arc2`` gate token (minted with :data:`FIXTURE_GATE_SECRET`, a test-only
  secret) on every executed proposal's ``gate_decisions.token`` and the matching
  ``<token>.s1`` as its ``orders.client_order_id``, exactly as the ladder writes them, so
  the tower's never-serve-the-token rule (E8.7b1) is tested against the real shape.

Never point this at ``data/arc.db``: it refuses to overwrite an existing file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.context.ttl import to_db
from arc.gate.band import PriceBand
from arc.gate.token import OrderPayload, client_order_id, mint_band
from arc.models import GateDecision, LegIntent, Structure
from arc.monitoring.store import AlertRepo, HeartbeatRepo
from arc.store.db import connect
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import (
    CandidateRepo,
    FillRepo,
    GateDecisionRepo,
    HaltRepo,
    OrderRepo,
    PnlSnapshotRepo,
    PositionsSnapshotRepo,
    ProposalRepo,
)
from arc.structures import debit_vertical, long_call
from arc.utils.calendar import ET, now_et

if TYPE_CHECKING:
    import sqlite3

log = structlog.get_logger(__name__)

N_MARKS = 30
CADENCE = dt.timedelta(minutes=5)
N_DAYS = 10
BASE_EQUITY = Decimal("100000")
# E5.9b (D43): broker last_equity (official closing prices) vs Arc's live-quote close,
# the Fri 2026-10-02 offset (102,239.29 - 101,241.15). Never the day P&L basis.
BROKER_CLOSE_SKEW = Decimal("998.14")
#: Test-only HMAC secret for the fixture's gate tokens (never a real ``ARC_GATE_SECRET``).
FIXTURE_GATE_SECRET = b"tower-fixture-test-secret-not-for-trading-0001"


def phash(tag: str) -> str:
    """Deterministic 64-hex proposal hash for fixture row *tag*."""
    return hashlib.sha256(f"arc-tower-fixture:{tag}".encode()).hexdigest()


@dataclass(frozen=True)
class Pos:
    tag: str
    ticker: str
    structure: Structure
    contracts: int
    entry: Decimal  # per share, + debit
    opened: dt.datetime
    drift: float  # per-share mark drift over the 30 marks (sign = direction)


def _weekdays_before(day: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    d = day
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def _proposal(
    conn: sqlite3.Connection,
    tag: str,
    ticker: str,
    st: Structure,
    *,
    at: dt.datetime,
    contracts: int,
    kind: str = "open",
    ev: str = "18.5",
    pop: float = 0.58,
    net_ev: float | None = 12.4,
) -> str:
    h = phash(tag)
    cid = CandidateRepo(conn).insert(
        ticker=ticker, stance="bullish", catalyst_type="earnings", confidence=0.7,
        created_at=to_db(at),
    )  # fmt: skip
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash=h, structure_json=st.model_dump_json(),
        thesis=f"{ticker} fixture thesis", kind=kind,
        quant_json=json.dumps({"pop": pop, "ev": ev, "cost_bps": 35.0}),
        sizing_json=json.dumps({"contracts": contracts, "notional": "500", "pct_equity": 0.01}),
        expires_at=to_db(at + dt.timedelta(minutes=30)), created_at=to_db(at),
        day=at.date().isoformat(), ticker=ticker, commit=False,
    )  # fmt: skip
    if net_ev is not None:
        payload = {"analytics": {"exit_model": {"managed": {"net_ev": net_ev, "pop": pop - 0.04}}}}
        conn.execute(
            """INSERT INTO market_contexts (id, proposal_hash, payload, quotes_as_of, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (f"mc-{h[:12]}", h, json.dumps(payload), to_db(at), to_db(at)),
        )
    return h


def _gate(
    conn: sqlite3.Connection, h: str, at: dt.datetime, violations: list[str] | None = None
) -> None:
    GateDecisionRepo(conn).insert(
        proposal_hash=h, passed=not violations, violations=violations or [],
        decided_at=to_db(at + dt.timedelta(seconds=20)), commit=False,
    )  # fmt: skip


def _approval(conn: sqlite3.Connection, h: str, ticker: str, at: dt.datetime, status: str) -> None:
    decided = status not in ("pending",)
    conn.execute(
        """INSERT INTO approval_requests
               (proposal_hash, ticker, day, proposal_json, status, channel, expires_at,
                created_at, decided_at, decided_by)
           VALUES (?, ?, ?, '{}', ?, 'log', ?, ?, ?, ?)""",
        (
            h, ticker, at.date().isoformat(), status,
            to_db(at + dt.timedelta(minutes=30)), to_db(at + dt.timedelta(seconds=30)),
            to_db(at + dt.timedelta(minutes=2)) if decided else None,
            ("arc:ttl" if status == "expired" else "U0OWNER001") if decided else None,
        ),
    )  # fmt: skip


def _mint(conn: sqlite3.Connection, h: str, at: dt.datetime, contracts: int) -> str:
    """Mint a real ``arc2`` band token for *h* (band 1.00-3.00, 3 steps, like
    :func:`_execute`'s execution row) and store it on its gate decision."""
    st = Structure.model_validate_json(
        conn.execute(
            "SELECT structure_json FROM proposals WHERE proposal_hash = ?", (h,)
        ).fetchone()[0]
    )
    band = PriceBand(lo=Decimal("1.00"), hi=Decimal("3.00"), max_steps=3)
    order = OrderPayload.from_values(
        [
            (leg.occ_symbol, "buy" if leg.side is LegIntent.LONG else "sell", leg.ratio)
            for leg in st.legs
        ],
        qty=contracts,
        limit_price=band.lo,
    )
    token = mint_band(
        h, GateDecision(proposal_hash=h, passed=True), order=order, band=band,
        secret=FIXTURE_GATE_SECRET, expires_at=at + dt.timedelta(minutes=30), now=at,
    )  # fmt: skip
    conn.execute("UPDATE gate_decisions SET token = ? WHERE proposal_hash = ?", (token, h))
    return token


def _execute(
    conn: sqlite3.Connection,
    h: str,
    *,
    at: dt.datetime,
    kind: str,
    contracts: int,
    status: str,
    fill: Decimal | None = None,
) -> None:
    ex = ExecutionRepo(conn)
    ex.start(
        proposal_hash=h, kind=kind, token_version="arc2", band_lo=Decimal("1.00"),
        band_hi=Decimal("3.00"), max_steps=3, contracts=contracts, now=at + dt.timedelta(minutes=3),
    )  # fmt: skip
    token = _mint(conn, h, at + dt.timedelta(minutes=3), contracts)
    if status == "working":
        return
    ex.attempt(h)
    filled = contracts if status == "filled" else 0
    ex.finish(
        h, status=status, filled_qty=filled, fill_price=fill if filled else None,
        steps_used=1 if filled else None, now=at + dt.timedelta(minutes=5),
    )  # fmt: skip
    oid = OrderRepo(conn).create(
        proposal_hash=h,
        client_order_id=client_order_id(token, 1),
        created_at=to_db(at + dt.timedelta(minutes=3)),
    )
    if filled and fill is not None:
        FillRepo(conn).insert(
            order_id=oid, qty=filled, price=str(fill), filled_at=to_db(at + dt.timedelta(minutes=5))
        )


def _mark(entry: Decimal, drift: float, i: int) -> Decimal:
    """Per-share structure mark at monitor step *i* (deterministic wave + drift)."""
    x = i / (N_MARKS - 1)
    wobble = 0.06 * float(entry) * math.sin(i / 2.7)
    return Decimal(str(round(float(entry) + drift * x + wobble, 2)))


def _load_script(name: str):  # noqa: ANN202 - a module loaded by path
    """``scripts/<name>.py`` (loaded by path: ``scripts`` is no package)."""
    here = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, here)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


def _trades_module():  # noqa: ANN202 - a module loaded by path
    """``scripts/tower_fixture_trades.py`` (loaded by path: ``scripts`` is no package)."""
    here = Path(__file__).resolve().parent / "tower_fixture_trades.py"
    spec = importlib.util.spec_from_file_location("tower_fixture_trades", here)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(
    path: Path,
    now: dt.datetime | None = None,
    *,
    history: bool = False,
    ops: bool = False,
    exits: bool = False,
) -> Path:
    """Create *path* (must not exist), migrate it and fill it with the fixture rows.

    *history* adds the E8.7c Performance history (``scripts/tower_fixture_performance.py``:
    ~40 closed trades and daily equity over the 3+ months before the 10 recent days).
    *ops* adds the E8.7d Ops page rows (``scripts/tower_fixture_ops.py``: a full
    simulated schedule with run manifests, alerts, context, LLM usage, config changes).
    *exits* adds the E13.14 Research exit chain (``scripts/tower_fixture_exits.py``:
    watchlist, exit cases, Risk verdicts).
    """
    if path.exists():
        msg = f"{path} exists; the fixture builder never overwrites a DB"
        raise FileExistsError(msg)
    now = (now or now_et()).astimezone(ET).replace(microsecond=0)
    today = now.date()
    conn = connect(path)
    migrate(conn)

    exp1 = today + dt.timedelta(days=31)
    exp2 = today + dt.timedelta(days=17)
    exp3 = today + dt.timedelta(days=45)
    positions = [
        Pos("pos-spy", "SPY", debit_vertical(
            "call", "SPY", exp1, long_strike=660, long_premium="9.10", short_strike=670,
            short_premium="4.95", as_of=today), 3, Decimal("4.15"),
            now - dt.timedelta(days=5, hours=2), 0.85),
        Pos("pos-qqq", "QQQ", debit_vertical(
            "put", "QQQ", exp2, long_strike=590, long_premium="8.40", short_strike=580,
            short_premium="5.10", as_of=today), 2, Decimal("3.30"),
            now - dt.timedelta(days=3, hours=1), -0.95),
        Pos("pos-nvda", "NVDA", long_call("NVDA", exp3, 190, "7.25", as_of=today), 1,
            Decimal("7.25"), now - dt.timedelta(hours=3), 1.10),
    ]  # fmt: skip

    # -- open structures (their open proposals: gate pass, approved, filled) -------------
    sids: dict[str, str] = {}
    for p in positions:
        h = _proposal(conn, p.tag, p.ticker, p.structure, at=p.opened, contracts=p.contracts)
        _gate(conn, h, p.opened)
        _approval(conn, h, p.ticker, p.opened, "approved")
        _execute(
            conn, h, at=p.opened, kind="open", contracts=p.contracts, status="filled", fill=p.entry
        )
        sids[p.tag] = OpenStructureRepo(conn).open(
            ticker=p.ticker, open_proposal_hash=h, candidate_id="fixture",
            structure_json=p.structure.model_dump_json(), contracts=p.contracts,
            entry_net=p.entry, now=p.opened + dt.timedelta(minutes=5), commit=False,
        )  # fmt: skip

    # A closed structure (AMD, closed yesterday at a profit).
    amd_at = now - dt.timedelta(days=8)
    amd = debit_vertical(
        "call", "AMD", today - dt.timedelta(days=1), long_strike=160, long_premium="6.00",
        short_strike=170, short_premium="2.40", as_of=amd_at.date(),
    )  # fmt: skip
    h_amd = _proposal(conn, "pos-amd", "AMD", amd, at=amd_at, contracts=2)
    _gate(conn, h_amd, amd_at)
    _approval(conn, h_amd, "AMD", amd_at, "approved")
    _execute(
        conn, h_amd, at=amd_at, kind="open", contracts=2, status="filled", fill=Decimal("3.60")
    )
    sid_amd = OpenStructureRepo(conn).open(
        ticker="AMD", open_proposal_hash=h_amd, candidate_id="fixture",
        structure_json=amd.model_dump_json(), contracts=2, entry_net=Decimal("3.60"),
        now=amd_at, commit=False,
    )  # fmt: skip
    OpenStructureRepo(conn).reduce(
        sid_amd,
        closed_qty=2,
        close_net=Decimal("-5.10"),
        now=now - dt.timedelta(days=1),
        commit=False,
    )

    # Exit pending on QQQ: close proposal, gate pass, approval pending.
    qqq = positions[1]
    exit_at = now - dt.timedelta(minutes=40)
    h_exit = _proposal(conn, "exit-qqq", "QQQ", qqq.structure, at=exit_at, contracts=2,
                       kind="close", ev="0", pop=0.5, net_ev=None)  # fmt: skip
    _gate(conn, h_exit, exit_at)
    _approval(conn, h_exit, "QQQ", exit_at, "pending")
    OpenStructureRepo(conn).set_exit(
        sids["pos-qqq"], proposal_hash=h_exit, reason="profit_target", day=today.isoformat(),
        commit=False,
    )  # fmt: skip

    # -- today's proposals: one per status --------------------------------------------
    def vert(root: str, k: int, exp_days: int = 24) -> Structure:
        return debit_vertical("call", root, today + dt.timedelta(days=exp_days), long_strike=k,
                              long_premium="5.00", short_strike=k + 10, short_premium="2.20",
                              as_of=today)  # fmt: skip

    at = now - dt.timedelta(hours=5)
    step = dt.timedelta(minutes=25)
    # proposed: no gate decision yet
    _proposal(
        conn, "p-googl", "GOOGL", vert("GOOGL", 250), at=now - dt.timedelta(minutes=8), contracts=2
    )
    # gate FAIL with two violations
    h = _proposal(conn, "p-aapl", "AAPL", vert("AAPL", 250), at=at, contracts=4, net_ev=-3.2)
    _gate(conn, h, at, ["per_underlying_limit: max loss $5,400 on AAPL > $5,000 (5.00% of equity)",
                        "spread_too_wide: leg AAPL 250C spread 14% > 10%"])  # fmt: skip
    _approval(conn, h, "AAPL", at, "not_actionable")
    # approval pending
    at += step
    h = _proposal(
        conn, "p-msft", "MSFT", vert("MSFT", 520), at=now - dt.timedelta(minutes=12), contracts=1
    )
    _gate(conn, h, now - dt.timedelta(minutes=12))
    _approval(conn, h, "MSFT", now - dt.timedelta(minutes=12), "pending")
    # approved, execution working
    h = _proposal(conn, "p-meta", "META", vert("META", 740), at=at, contracts=1)
    _gate(conn, h, at)
    _approval(conn, h, "META", at, "approved")
    _execute(conn, h, at=at, kind="open", contracts=1, status="working")
    # rejected
    at += step
    h = _proposal(
        conn, "p-tsla", "TSLA", vert("TSLA", 440), at=at, contracts=1, pop=0.44, net_ev=2.1
    )
    _gate(conn, h, at)
    _approval(conn, h, "TSLA", at, "rejected")
    # expired
    at += step
    h = _proposal(conn, "p-amzn", "AMZN", vert("AMZN", 230), at=at, contracts=2)
    _gate(conn, h, at)
    _approval(conn, h, "AMZN", at, "expired")
    # approved, execution cancelled (no fill)
    at += step
    h = _proposal(conn, "p-iwm", "IWM", vert("IWM", 245), at=at, contracts=3)
    _gate(conn, h, at)
    _approval(conn, h, "IWM", at, "approved")
    _execute(conn, h, at=at, kind="open", contracts=3, status="cancelled")
    # An old gate FAIL outside the 24h window (Trades page only).
    old = now - dt.timedelta(days=4)
    h = _proposal(conn, "p-dia-old", "DIA", vert("DIA", 460), at=old, contracts=1)
    _gate(conn, h, old, ["halted: trading halted"])

    # -- E8.7b Trades drill-down rows ----------------------------------------------------
    by_tag = {p.tag: p for p in positions}
    _trades_module().add_trade_rows(
        conn, now=now, spy_hash=phash("pos-spy"), spy_structure=by_tag["pos-spy"].structure,
        spy_opened=by_tag["pos-spy"].opened, amd_hash=h_amd, amd_structure_id=sid_amd,
        amd_opened=amd_at,
        filled_hashes={**{phash(p.tag): p.opened for p in positions}, h_amd: amd_at},
        make_proposal=lambda tag, ticker, st, **kw: _proposal(conn, tag, ticker, st, **kw),
        make_gate=lambda h, at: _gate(conn, h, at),
        make_approval=lambda h, t, at, status: _approval(conn, h, t, at, status),
        make_execute=lambda h, **kw: _execute(conn, h, **kw),
        vert=lambda root, k: vert(root, k),
    )  # fmt: skip

    # -- monitor heartbeats: 30 marks at 5-min cadence ending 2 minutes ago ---------------
    days = _weekdays_before(today, N_DAYS)
    if history:
        _load_script("tower_fixture_performance").add_history(
            conn, now=now, first_equity_day=days[0], base_equity=BASE_EQUITY
        )
    equities: list[Decimal] = []
    eq = BASE_EQUITY
    for i, _ in enumerate(days):
        eq += Decimal(str(round(260 * math.sin(i * 1.3) + 90, 2)))
        equities.append(eq)
    last_equity = equities[-1]
    first_mark = now - dt.timedelta(minutes=2) - CADENCE * (N_MARKS - 1)
    for i in range(N_MARKS):
        legs: list[dict[str, str]] = []
        for p in positions:
            if p.opened > first_mark + CADENCE * i:
                continue
            # Structure move since entry and since yesterday's close, per share; the long leg
            # carries 70% of it and the short leg the rest, so Σ legs = the structure mark.
            move = _mark(p.entry, p.drift, i) - p.entry
            day_move = Decimal(str(round(p.drift * 0.2, 2)))
            n = p.contracts
            single = len(p.structure.legs) == 1
            for leg in p.structure.legs:
                long = leg.side.value == "long"
                w = Decimal(1) if single else (Decimal("0.7") if long else Decimal("-0.3"))
                prem = leg.premium or Decimal(0)
                cur = max((prem + move * w).quantize(Decimal("0.01")), Decimal("0.05"))
                last = max((cur - day_move * w).quantize(Decimal("0.01")), Decimal("0.05"))
                qty = n if long else -n
                pl = (cur - prem) * 100 * qty
                legs.append({
                    "symbol": leg.occ_symbol, "qty": str(qty), "side": leg.side.value,
                    "asset_class": "us_option", "avg_entry_price": str(leg.premium),
                    "current_price": str(cur), "lastday_price": str(last),
                    "change_today": str(round((cur - last) / last, 5)),
                    "market_value": str(cur * 100 * qty),
                    "unrealized_pl": str(pl.quantize(Decimal("0.01"))),
                })  # fmt: skip
        equity = last_equity + Decimal(str(round(120 * math.sin(i / 4) + 6 * i, 2)))
        HeartbeatRepo(conn).record(
            "monitor", "ok", at=first_mark + CADENCE * i,
            detail={
                "valued": True, "positions": len(positions),
                # E5.9b (D43): the broker's raw last_equity (closing-price valuation) is
                # off Arc's own close; the day P&L baseline is prev_close (Arc's close).
                "equity": float(equity), "last_equity": float(last_equity + BROKER_CLOSE_SKEW),
                "prev_close": float(last_equity), "prev_close_source": "arc_close",
                "day_pnl": float(equity - last_equity), "cash": 88000.0,
                "buying_power": 88000.0, "options_buying_power": 88000.0,
                "delta": 42.5 + i * 0.4, "dollar_delta": 18_500.0 + 160 * i,
                "gamma": 0.8, "vega": 21000.0 + 150 * i,
                "theta": -34.2, "max_loss": 3910.0, "halted": True,
                "order_budget": {"used": 31, "limit": 200, "tier": "normal"},
                "orders_used": 31, "orders_limit": 200, "broker_requests": 6, "legs": legs,
            },
        )  # fmt: skip

    # -- reconciled daily P&L: 10 weekdays before today ----------------------------------
    prev = BASE_EQUITY
    for i, (day, eq_day) in enumerate(zip(days, equities, strict=True)):
        clean = i != N_DAYS - 3
        PnlSnapshotRepo(conn).insert(
            realized=str(Decimal("150") if i == N_DAYS - 1 else Decimal("0")),
            unrealized=str(Decimal("-40") + i * 12), total=str(eq_day - BASE_EQUITY),
            details_json=json.dumps({
                "day": day.isoformat(), "equity": str(eq_day), "last_equity": str(prev),
                "prev_close": str(prev), "prev_close_source": "arc_close",
                "day_pnl": str(eq_day - prev), "open_structures": 2, "closed_today": 0,
                "clean": clean,
            }),
            snapshot_at=to_db(dt.datetime.combine(day, dt.time(16, 35), tzinfo=ET)), commit=False,
        )  # fmt: skip
        prev = eq_day

    # -- reconcile held flags (NVDA opened after the last reconcile: not held) ------------
    PositionsSnapshotRepo(conn).insert(
        positions_json=json.dumps({"day": days[-1].isoformat(), "structures": [
            {"structure_id": sids["pos-spy"], "ticker": "SPY", "held": True, "legs": {}},
            {"structure_id": sids["pos-qqq"], "ticker": "QQQ", "held": True, "legs": {}},
            {"structure_id": sids["pos-nvda"], "ticker": "NVDA", "held": False, "legs": {}},
        ]}),
        snapshot_at=to_db(dt.datetime.combine(days[-1], dt.time(16, 35), tzinfo=ET)), commit=False,
    )  # fmt: skip

    # -- ops: halt, alerts, tick + health ------------------------------------------------
    HaltRepo(conn).halt(
        reason=f"reconciliation {days[-3]:%Y-%m-%d}: 1 mismatch(es) (fill_unknown)",
        actor="arc:reconcile", at=to_db(now - dt.timedelta(hours=1, minutes=10)),
    )  # fmt: skip
    alerts = AlertRepo(conn)
    alerts.open("missed:scalp:12:00", "missed_window", "scalp slot 12:00 ET missed",
                at=now - dt.timedelta(minutes=50))  # fmt: skip
    alerts.open("gateway", "gateway_down", "Hermes gateway not responding",
                at=now - dt.timedelta(hours=6))  # fmt: skip
    alerts.resolve("gateway", at=now - dt.timedelta(hours=5, minutes=40))
    # E8.8b: a run of one-off missed slots (the owner's live "wall of missed_window"); Recent
    # Activity groups them with the open scalp alert into one `missed_window ×12` row. One
    # more 30 h back sits outside the 24 h window, and one coverage alert gives a 9th row so
    # the 8-row cap shows.
    for k in range(11):
        at = now - dt.timedelta(hours=2, minutes=5 * k)
        alerts.open(f"missed:rss:{k}", "missed_window", f"rss slot {at:%H:%M} ET missed",
                    at=at, resolved=True)  # fmt: skip
    alerts.open("missed:rss:old", "missed_window", "rss slot missed (yesterday)",
                at=now - dt.timedelta(hours=30), resolved=True)  # fmt: skip
    alerts.open("coverage:monitor", "coverage", "monitor ran 9/12 slots in the last 60 min",
                at=now - dt.timedelta(hours=4), resolved=True)  # fmt: skip
    for k in range(6):
        HeartbeatRepo(conn).record(
            "tick",
            "ok",
            at=now - dt.timedelta(minutes=1) - CADENCE * k,
            detail={"counts": {"ok": 2}},
        )
    HeartbeatRepo(conn).record(
        "health", "ok", at=now - dt.timedelta(minutes=11), detail={"checks": {}}
    )
    if exits:
        _load_script("tower_fixture_exits").add_exits(conn, now, sids)
    if ops:
        ops_mod = _load_script("tower_fixture_ops")
        ops_mod.add_ops(conn, now)
        run = ops_mod.undeclared_run_id(conn)
        if run:
            ops_mod.write_log(path, run, now)
    conn.commit()
    conn.close()
    log.info("tower_fixture.built", path=str(path), now=now.isoformat())
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("out", type=Path, help="DB file to create (must not exist)")
    ap.add_argument("--now", help="anchor time, ISO 8601 (default: now, ET)")
    ap.add_argument(
        "--history", action="store_true", help="add the E8.7c Performance history (~40 trades)"
    )
    ap.add_argument(
        "--ops", action="store_true", help="add the E8.7d Ops page rows (runs, manifests, ...)"
    )
    ap.add_argument(
        "--exits", action="store_true", help="add the E13.14 shadow exit chain (Positions)"
    )
    args = ap.parse_args(argv)
    now = dt.datetime.fromisoformat(args.now) if args.now else None
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    try:
        build(args.out, now, history=args.history, ops=args.ops, exits=args.exits)
    except FileExistsError as exc:
        log.error("tower_fixture.refused", error=str(exc))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
