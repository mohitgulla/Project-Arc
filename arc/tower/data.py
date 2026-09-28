"""Read-only views of the audit store for the control tower (E8.3).

Everything here is a pure read over SQLite:

- :func:`connect_ro` opens the DB with ``mode=ro`` (SQLite refuses any write,
  and a missing file is an error: the tower never creates or migrates a DB).
- :func:`load_snapshot` runs SELECTs only and returns a :class:`TowerSnapshot`.

Sources (the tower never calls the broker, the market data API or an LLM):

========================  ==========================================================
Section                   Table(s)
========================  ==========================================================
P&L                       ``pnl_snapshots`` (E6.3 reconcile, one per ET day) and the
                          latest ``monitor`` heartbeat (intraday equity, day P&L)
Positions                 ``open_structures`` (E6.2) + broker legs from the latest
                          ``monitor`` heartbeat, reconcile ``held`` flag from the
                          latest ``positions_snapshots`` row
Greeks                    latest ``monitor`` heartbeat (net Δ Γ ν Θ, max loss), with
                          the gate's portfolio caps (PLAN §5) for scale
Proposals                 ``proposals`` + latest ``gate_decisions`` +
                          ``approval_requests`` + ``executions``
Halts                     ``halts`` (active and recent)
Gate violations           failed ``gate_decisions``, one row per violation
Ops                       ``heartbeats`` (tick / health) and open ``ops_alerts``
========================  ==========================================================
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arc.reconcile.performance import daily_equity, performance_from
from arc.slack.digests import Performance  # noqa: TC001 - pydantic field
from arc.utils.calendar import ET

__all__ = [
    "GateViolation",
    "GreeksView",
    "HaltView",
    "LegView",
    "OpsView",
    "PnlView",
    "ProposalView",
    "StructureView",
    "TowerSnapshot",
    "connect_ro",
    "load_snapshot",
    "parse_ts",
]

_FROZEN = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Connection + parsing helpers
# ---------------------------------------------------------------------------


def connect_ro(db_path: Path | str) -> sqlite3.Connection:
    """Open *db_path* read-only. Raises ``FileNotFoundError`` if it does not exist."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        msg = f"audit store not found: {path} (the tower never creates one)"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_ts(text: str | None) -> _dt.datetime | None:
    """Parse any timestamp the store writes (``to_db`` UTC ``Z`` text or ISO with offset).

    Naive values are UTC (the SQLite ``strftime('now')`` defaults). Returned in ET.
    """
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.astimezone(ET)


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------


class PnlView(BaseModel):
    """Account P&L: the last reconciled snapshot plus the latest intraday mark."""

    model_config = _FROZEN

    reconciled_at: _dt.datetime | None = None
    reconciled_day: _dt.date | None = None
    realized: Decimal | None = None
    unrealized: Decimal | None = None
    total: Decimal | None = None
    equity: Decimal | None = None
    day_pnl: Decimal | None = None
    reconcile_clean: bool | None = None
    performance: Performance | None = None
    intraday_at: _dt.datetime | None = None
    intraday_equity: Decimal | None = None
    intraday_day_pnl: Decimal | None = None
    equity_series: list[tuple[_dt.date, Decimal]] = Field(default_factory=list)


class GreeksView(BaseModel):
    """Net portfolio Greeks from the latest monitor run (share-equivalents, as the gate)."""

    model_config = _FROZEN

    at: _dt.datetime | None = None
    valued: bool = False
    positions: int | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    max_loss: float | None = None
    equity: float | None = None
    delta_cap: float | None = Field(
        default=None, description="|Δ| cap, share-eq (cap × equity/100)"
    )
    vega_usd: float | None = Field(default=None, description="ν in $ per vol point (ν/100)")
    vega_cap_usd: float | None = Field(default=None, description="|ν| cap, $ per vol point")


class LegView(BaseModel):
    model_config = _FROZEN

    symbol: str
    qty: Decimal
    side: str
    asset_class: str = "us_option"
    avg_entry_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pl: Decimal | None = None


class StructureView(BaseModel):
    model_config = _FROZEN

    id: str
    ticker: str
    kind: str | None
    contracts: int
    entry_net: Decimal
    opened_at: _dt.datetime | None
    expiration: _dt.date | None
    status: str
    exit_reason: str | None = None
    exit_pending: bool = False
    held: bool | None = Field(default=None, description="Last reconcile found it at the broker")
    unrealized_pl: Decimal | None = Field(default=None, description="Sum over its legs' broker P&L")


class ProposalView(BaseModel):
    model_config = _FROZEN

    proposal_hash: str
    day: str | None
    ticker: str | None
    kind: str
    structure_kind: str | None
    contracts: int | None
    limit: Decimal | None
    ev: Decimal | None
    pop: float | None
    created_at: _dt.datetime | None
    gate_passed: bool | None
    violations: list[str] = Field(default_factory=list)
    approval: str | None = None
    execution: str | None = None
    fill_price: Decimal | None = None


class HaltView(BaseModel):
    model_config = _FROZEN

    id: str
    kind: str
    actor: str
    reason: str
    at: _dt.datetime | None
    cleared_at: _dt.datetime | None
    cleared_by: str | None
    active: bool


class GateViolation(BaseModel):
    model_config = _FROZEN

    decided_at: _dt.datetime | None
    ticker: str | None
    kind: str
    code: str
    detail: str
    proposal_hash: str


class OpsView(BaseModel):
    model_config = _FROZEN

    tick_at: _dt.datetime | None = None
    tick_status: str | None = None
    health_at: _dt.datetime | None = None
    health_status: str | None = None
    open_alerts: list[dict[str, str]] = Field(default_factory=list)


class TowerSnapshot(BaseModel):
    """Everything the dashboard shows, read in one pass."""

    model_config = _FROZEN

    as_of: _dt.datetime
    db_path: str
    pnl: PnlView
    greeks: GreeksView
    structures: list[StructureView]
    legs: list[LegView]
    proposals: list[ProposalView]
    halts: list[HaltView]
    violations: list[GateViolation]
    violation_counts: dict[str, int]
    ops: OpsView

    @property
    def halted(self) -> bool:
        return any(h.active for h in self.halts)


# ---------------------------------------------------------------------------
# Section loaders
# ---------------------------------------------------------------------------


def _latest_heartbeat(conn: sqlite3.Connection, component: str) -> sqlite3.Row | None:
    if not _has_table(conn, "heartbeats"):
        return None
    return conn.execute(
        "SELECT * FROM heartbeats WHERE component = ? ORDER BY at DESC, rowid DESC LIMIT 1",
        (component,),
    ).fetchone()


def _pnl(conn: sqlite3.Connection, monitor: sqlite3.Row | None) -> PnlView:
    row = conn.execute(
        "SELECT * FROM pnl_snapshots ORDER BY snapshot_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    series = daily_equity(conn)
    fields: dict[str, Any] = {"equity_series": [(s.day, s.equity) for s in series]}
    if row is not None:
        d = _json(row["details_json"], {})
        day = d.get("day")
        fields.update(
            reconciled_at=parse_ts(row["snapshot_at"]),
            reconciled_day=_dt.date.fromisoformat(day) if day else None,
            realized=_dec(row["realized"]),
            unrealized=_dec(row["unrealized"]),
            total=_dec(row["total"]),
            equity=_dec(d.get("equity")),
            day_pnl=_dec(d.get("day_pnl")),
            reconcile_clean=d.get("clean"),
        )
        if series:
            fields["performance"] = performance_from(series, series[-1].day)
    if monitor is not None:
        m = _json(monitor["detail"], {})
        eq, last = _dec(m.get("equity")), _dec(m.get("last_equity"))
        fields.update(
            intraday_at=parse_ts(monitor["at"]),
            intraday_equity=eq,
            intraday_day_pnl=eq - last if eq is not None and last is not None else None,
        )
    return PnlView(**fields)


def _greeks(monitor: sqlite3.Row | None, delta_cap: float, vega_cap_pct: float) -> GreeksView:
    if monitor is None:
        return GreeksView()
    m = _json(monitor["detail"], {})
    equity = m.get("equity")
    vega = m.get("vega")
    return GreeksView(
        at=parse_ts(monitor["at"]),
        valued=bool(m.get("valued")),
        positions=m.get("positions"),
        delta=m.get("delta"),
        gamma=m.get("gamma"),
        vega=vega,
        theta=m.get("theta"),
        max_loss=m.get("max_loss"),
        equity=equity,
        delta_cap=None if equity is None else delta_cap * float(equity) / 100.0,
        vega_usd=None if vega is None else float(vega) / 100.0,
        vega_cap_usd=None if equity is None else vega_cap_pct * float(equity),
    )


def _legs(monitor: sqlite3.Row | None) -> list[LegView]:
    if monitor is None:
        return []
    out: list[LegView] = []
    for leg in _json(monitor["detail"], {}).get("legs", []):
        out.append(
            LegView(
                symbol=str(leg["symbol"]),
                qty=_dec(leg.get("qty")) or Decimal(0),
                side=str(leg.get("side", "")),
                asset_class=str(leg.get("asset_class", "us_option")),
                avg_entry_price=_dec(leg.get("avg_entry_price")),
                market_value=_dec(leg.get("market_value")),
                unrealized_pl=_dec(leg.get("unrealized_pl")),
            )
        )
    return out


def _leg_symbols(structure: dict[str, Any]) -> list[str]:
    from arc.structures import parse_occ

    out: list[str] = []
    for leg in structure.get("legs", []):
        try:
            out.append(parse_occ(str(leg["occ_symbol"])).format())
        except (KeyError, ValueError):
            continue
    return out


def _structures(conn: sqlite3.Connection, legs: list[LegView]) -> list[StructureView]:
    if not _has_table(conn, "open_structures"):
        return []
    from arc.structures import parse_occ

    held: dict[str, bool] = {}
    snap = conn.execute(
        "SELECT positions_json FROM positions_snapshots ORDER BY snapshot_at DESC, rowid DESC "
        "LIMIT 1"
    ).fetchone()
    if snap is not None:
        for s in _json(snap["positions_json"], {}).get("structures", []):
            held[str(s.get("structure_id"))] = bool(s.get("held"))

    pl_by_symbol: dict[str, Decimal] = {}
    for leg in legs:
        if leg.unrealized_pl is not None:
            try:
                key = parse_occ(leg.symbol).format()
            except ValueError:
                key = leg.symbol
            pl_by_symbol[key] = pl_by_symbol.get(key, Decimal(0)) + leg.unrealized_pl
    # A leg shared by two open structures cannot be split by symbol: leave P&L blank.
    shared: Counter[str] = Counter()
    rows = conn.execute(
        "SELECT * FROM open_structures WHERE status = 'open' ORDER BY opened_at, rowid"
    ).fetchall()
    parsed = [(r, _json(r["structure_json"], {})) for r in rows]
    for _, st in parsed:
        shared.update(set(_leg_symbols(st)))

    out: list[StructureView] = []
    for r, st in parsed:
        syms = _leg_symbols(st)
        exps = []
        for s in syms:
            try:
                exps.append(parse_occ(s).expiration)
            except ValueError:
                continue
        pl: Decimal | None = None
        if syms and all(shared[s] == 1 and s in pl_by_symbol for s in syms):
            pl = sum((pl_by_symbol[s] for s in syms), start=Decimal(0))
        out.append(
            StructureView(
                id=r["id"],
                ticker=r["ticker"],
                kind=st.get("kind"),
                contracts=int(r["contracts"]),
                entry_net=_dec(r["entry_net"]) or Decimal(0),
                opened_at=parse_ts(r["opened_at"]),
                expiration=min(exps) if exps else None,
                status=r["status"],
                exit_reason=r["exit_reason"],
                exit_pending=r["exit_proposal_hash"] is not None,
                held=held.get(r["id"]),
                unrealized_pl=pl,
            )
        )
    return out


def _proposals(conn: sqlite3.Connection, since_day: str, limit: int) -> list[ProposalView]:
    approvals = _has_table(conn, "approval_requests")
    executions = _has_table(conn, "executions")
    join_a = "LEFT JOIN approval_requests a ON a.proposal_hash = p.proposal_hash"
    join_x = "LEFT JOIN executions x ON x.proposal_hash = p.proposal_hash"
    rows = conn.execute(
        f"""SELECT p.proposal_hash, p.day, p.ticker, p.kind, p.structure_json, p.quant_json,
                   p.sizing_json, p.created_at,
                   g.passed AS gate_passed, g.violations_json AS gate_violations,
                   {"a.status" if approvals else "NULL"} AS approval,
                   {"x.status" if executions else "NULL"} AS execution,
                   {"x.fill_price" if executions else "NULL"} AS fill_price
            FROM proposals p
            LEFT JOIN gate_decisions g ON g.id = (
                SELECT id FROM gate_decisions WHERE proposal_hash = p.proposal_hash
                ORDER BY decided_at DESC, rowid DESC LIMIT 1)
            {join_a if approvals else ""}
            {join_x if executions else ""}
            WHERE COALESCE(p.day, substr(p.created_at, 1, 10)) >= ?""",
        (since_day,),
    ).fetchall()
    out: list[ProposalView] = []
    for r in rows:
        st = _json(r["structure_json"], {})
        quant = _json(r["quant_json"], {})
        sizing = _json(r["sizing_json"], {})
        limit_price = _dec(st.get("net_debit_credit"))
        out.append(
            ProposalView(
                proposal_hash=r["proposal_hash"],
                day=r["day"],
                ticker=r["ticker"],
                kind=r["kind"] or "open",
                structure_kind=st.get("kind"),
                contracts=sizing.get("contracts"),
                limit=limit_price,
                ev=_dec(quant.get("ev")),
                pop=quant.get("pop"),
                created_at=parse_ts(r["created_at"]),
                gate_passed=None if r["gate_passed"] is None else bool(r["gate_passed"]),
                violations=[str(v) for v in _json(r["gate_violations"], [])],
                approval=r["approval"],
                execution=r["execution"],
                fill_price=_dec(r["fill_price"]),
            )
        )
    epoch = _dt.datetime.min.replace(tzinfo=_dt.UTC)
    out.sort(key=lambda p: p.created_at or epoch, reverse=True)
    return out[:limit]


def _halts(conn: sqlite3.Connection, limit: int) -> list[HaltView]:
    rows = conn.execute(
        """SELECT * FROM halts
           ORDER BY (cleared_at IS NULL) DESC, at DESC, rowid DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        HaltView(
            id=r["id"],
            kind=r["kind"],
            actor=r["actor"],
            reason=r["reason"],
            at=parse_ts(r["at"]),
            cleared_at=parse_ts(r["cleared_at"]),
            cleared_by=r["cleared_by"],
            active=r["cleared_at"] is None,
        )
        for r in rows
    ]


def _violations(conn: sqlite3.Connection, since: _dt.datetime, limit: int) -> list[GateViolation]:
    # decided_at is written both as to_db UTC text and as ISO with an offset, so
    # text ordering is unreliable: parse, filter and sort here.
    rows = conn.execute(
        """SELECT g.proposal_hash, g.decided_at, g.violations_json, p.ticker, p.kind
           FROM gate_decisions g LEFT JOIN proposals p ON p.proposal_hash = g.proposal_hash
           WHERE g.passed = 0"""
    ).fetchall()
    dated = [(parse_ts(r["decided_at"]), r) for r in rows]
    recent = sorted(
        ((at, r) for at, r in dated if at is not None and at >= since),
        key=lambda x: x[0],
        reverse=True,
    )[:limit]
    out: list[GateViolation] = []
    for at, r in recent:
        for v in _json(r["violations_json"], []):
            code, sep, detail = str(v).partition(": ")
            out.append(
                GateViolation(
                    decided_at=at,
                    ticker=r["ticker"],
                    kind=r["kind"] or "open",
                    code=code if sep else "other",
                    detail=detail if sep else str(v),
                    proposal_hash=r["proposal_hash"],
                )
            )
    return out


def _ops(conn: sqlite3.Connection) -> OpsView:
    tick = _latest_heartbeat(conn, "tick")
    health = _latest_heartbeat(conn, "health")
    alerts: list[dict[str, str]] = []
    if _has_table(conn, "ops_alerts"):
        for r in conn.execute(
            "SELECT kind, key, message, opened_at FROM ops_alerts WHERE resolved_at IS NULL "
            "ORDER BY opened_at"
        ).fetchall():
            opened = parse_ts(r["opened_at"])
            alerts.append(
                {
                    "kind": r["kind"],
                    "key": r["key"],
                    "message": r["message"],
                    "opened": f"{opened:%Y-%m-%d %H:%M %Z}" if opened else "",
                }
            )
    return OpsView(
        tick_at=parse_ts(tick["at"]) if tick else None,
        tick_status=tick["status"] if tick else None,
        health_at=parse_ts(health["at"]) if health else None,
        health_status=health["status"] if health else None,
        open_alerts=alerts,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_snapshot(
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    db_path: str = "",
    lookback_days: int = 7,
    delta_cap: float = 0.30,
    vega_cap_pct: float = 0.005,
    limit: int = 200,
) -> TowerSnapshot:
    """Read every dashboard section from *conn* (SELECT only) as of *now*."""
    now_et = now.astimezone(ET)
    since = now_et - _dt.timedelta(days=lookback_days)
    since_day = since.date().isoformat()
    monitor = _latest_heartbeat(conn, "monitor")
    violations = _violations(conn, since, limit)
    legs = _legs(monitor)
    return TowerSnapshot(
        as_of=now_et,
        db_path=db_path,
        pnl=_pnl(conn, monitor),
        greeks=_greeks(monitor, delta_cap, vega_cap_pct),
        structures=_structures(conn, legs),
        legs=legs,
        proposals=_proposals(conn, since_day, limit),
        halts=_halts(conn, 50),
        violations=violations,
        violation_counts=dict(Counter(v.code for v in violations).most_common()),
        ops=_ops(conn),
    )
