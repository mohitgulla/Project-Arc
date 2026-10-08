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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.models import Leg, Performance  # noqa: TC001 - pydantic field
from arc.reconcile.baseline import BaselineSource, start_of_day_equity
from arc.reconcile.performance import daily_equity, performance_from
from arc.utils.calendar import ET

__all__ = [
    "GateViolation",
    "GreeksView",
    "HaltView",
    "LegView",
    "OpsView",
    "PnlView",
    "Direction",
    "ProposalView",
    "StructureView",
    "TowerSnapshot",
    "connect_ro",
    "direction_of",
    "load_snapshot",
    "monitor_stale_after",
    "parse_ts",
]

_FROZEN = ConfigDict(extra="forbid", frozen=True)

# Monitor marks are stale after this many monitor cadences (E5.3a, D35). The
# cadence comes from ``personas.monitor.every`` (plus control-panel overrides),
# so the CLI snapshot and the E8.7 API share one rule.
STALE_CADENCES = 3
# Used only when routines.yaml cannot be read: 3 x the D35 5-min cadence.
DEFAULT_STALE_AFTER = _dt.timedelta(minutes=15)


def monitor_stale_after(conn: sqlite3.Connection | None = None) -> _dt.timedelta:
    """How old the latest ``monitor`` heartbeat may be before the tower flags it stale.

    ``STALE_CADENCES`` x the monitor's ``every`` from the effective routines config
    (``config/routines.yaml`` + D26 overrides read from *conn*, SELECT only).
    """
    from arc.control.effective import effective_routines

    try:
        found = effective_routines(conn).job("monitor")
    except (OSError, ValueError):  # unreadable/invalid YAML: never break the page
        return DEFAULT_STALE_AFTER
    if found is None or found[1].every is None:
        return DEFAULT_STALE_AFTER
    return STALE_CADENCES * found[1].every


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


def prev_close_of(
    conn: sqlite3.Connection, detail: dict[str, Any], day: _dt.date
) -> tuple[Decimal | None, BaselineSource | None]:
    """Start-of-day equity (E5.9b, D43) for a monitor heartbeat or snapshot *detail*.

    The baseline the writer recorded (``prev_close`` / ``prev_close_source``) wins, so
    the tower shows the same number as the loop root for the same tick. Rows written
    before E5.9b carry only the broker's raw ``last_equity``; for those the baseline
    is recomputed with :func:`arc.reconcile.baseline.start_of_day_equity` (pure read).
    """
    recorded = _dec(detail.get("prev_close"))
    source = detail.get("prev_close_source")
    if recorded is not None and source in ("arc_close", "broker_last_equity"):
        return recorded, source
    base = start_of_day_equity(conn, day, broker_last_equity=_dec(detail.get("last_equity")))
    return (base.value, base.source) if base is not None else (None, None)


def _json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


Direction = Literal["bullish", "bearish", "neutral"]


def direction_of(legs: Any, *, closing: bool = False) -> Direction | None:
    """Trade direction from stored legs (JSON text or a list of leg dicts), D50.

    Deterministic, from leg sides and strikes (:func:`arc.structures.legs_direction`),
    never the LLM or the candidate stance. *closing* flips a close proposal's sides
    back so it reads like the structure it exits. No legs -> ``None``; malformed -> neutral.
    """
    from arc.structures import legs_direction

    raw = _json(legs, []) if isinstance(legs, str) or legs is None else legs
    if not isinstance(raw, list) or not raw:
        return None
    try:
        parsed = [Leg.model_validate(leg) for leg in raw]
    except ValueError:
        return "neutral"
    stance = legs_direction(parsed, closing=closing)
    return None if stance is None else stance.value  # type: ignore[return-value]


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
    intraday_day_pnl: Decimal | None = Field(
        default=None, description="intraday_equity − intraday_prev_close (E5.9b, D43)"
    )
    intraday_prev_close: Decimal | None = Field(
        default=None, description="Start-of-day equity: Arc's prior-session close (D43)"
    )
    intraday_prev_close_source: BaselineSource | None = Field(
        default=None, description="arc_close, or broker_last_equity when no Arc close exists"
    )
    equity_series: list[tuple[_dt.date, Decimal]] = Field(default_factory=list)


class BetaDeltaRow(BaseModel):
    """D62: one underlying's share of the book's dollar delta and its β weighting."""

    model_config = _FROZEN

    dollar_delta: float
    beta: float = Field(description="β used: max(1y β vs SPY, 1.0); 1.0 when missing/stale")
    beta_dollar_delta: float
    beta_source: Literal["stored", "default"] = "default"


class GreeksView(BaseModel):
    """Net portfolio Greeks from the latest monitor run (share-equivalents), plus the
    gate's D57 dollar delta (Σ Δ × spot) against its dollar cap."""

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
    dollar_delta: float | None = Field(
        default=None,
        description="D57: net dollar delta Σ (Δ share-eq × spot), $; None on a heartbeat "
        "written before D57 (rendered —)",
    )
    dollar_delta_cap: float | None = Field(
        default=None, description="D57: |$Δ| cap, $ (portfolio_dollar_delta_cap_pct × equity)"
    )
    beta_dollar_delta: float | None = Field(
        default=None,
        description="D62: beta-weighted net dollar delta Σ (Δ × spot × max(β vs SPY, 1)), $ "
        "(SPY-equivalent); None on a heartbeat written before D62 (rendered —)",
    )
    beta_delta_cap: float | None = Field(
        default=None, description="D62: |β$Δ| cap, $ (portfolio_beta_delta_cap_pct × equity)"
    )
    delta_by_underlying: dict[str, BetaDeltaRow] = Field(
        default_factory=dict,
        description="D62: per-underlying $Δ, β used, β$Δ and β source (stored | default)",
    )
    vega_usd: float | None = Field(default=None, description="ν in $ per vol point (ν/100)")
    vega_cap_usd: float | None = Field(default=None, description="|ν| cap, $ per vol point")
    stale_after_s: int = Field(
        default=int(DEFAULT_STALE_AFTER.total_seconds()),
        description="Monitor marks older than this are stale: 3x the monitor cadence (E5.3a)",
    )


class LegView(BaseModel):
    model_config = _FROZEN

    symbol: str
    qty: Decimal
    side: str
    asset_class: str = "us_option"
    avg_entry_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pl: Decimal | None = None
    current_price: Decimal | None = None  # E5.3a: broker mark per share
    lastday_price: Decimal | None = None
    change_today: Decimal | None = None


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
    direction: Direction | None = None
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


class OrderBudgetView(BaseModel):
    """D32 daily order budget as the last monitor heartbeat saw it."""

    model_config = _FROZEN

    used: int
    limit: int
    tier: str
    as_of: _dt.datetime | None = None


class OpsView(BaseModel):
    model_config = _FROZEN

    tick_at: _dt.datetime | None = None
    tick_status: str | None = None
    health_at: _dt.datetime | None = None
    health_status: str | None = None
    open_alerts: list[dict[str, str]] = Field(default_factory=list)
    order_budget: OrderBudgetView | None = None


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
        snap_eq = _dec(d.get("equity"))
        prev, _src = prev_close_of(conn, d, _dt.date.fromisoformat(day)) if day else (None, None)
        fields.update(
            reconciled_at=parse_ts(row["snapshot_at"]),
            reconciled_day=_dt.date.fromisoformat(day) if day else None,
            realized=_dec(row["realized"]),
            unrealized=_dec(row["unrealized"]),
            total=_dec(row["total"]),
            equity=snap_eq,
            # E5.9b (D43): equity − Arc's prior close (legacy rows recomputed the same way)
            day_pnl=snap_eq - prev if snap_eq is not None and prev is not None else None,
            reconcile_clean=d.get("clean"),
        )
        if series:
            fields["performance"] = performance_from(series, series[-1].day)
    if monitor is not None:
        m = _json(monitor["detail"], {})
        at = parse_ts(monitor["at"])
        eq = _dec(m.get("equity"))
        prev, source = prev_close_of(conn, m, at.date()) if at is not None else (None, None)
        fields.update(
            intraday_at=at,
            intraday_equity=eq,
            intraday_prev_close=prev,
            intraday_prev_close_source=source,
            intraday_day_pnl=eq - prev if eq is not None and prev is not None else None,
        )
    return PnlView(**fields)


def _greeks(
    monitor: sqlite3.Row | None,
    dollar_delta_cap_pct: float,
    vega_cap_pct: float,
    stale_after: _dt.timedelta,
    beta_delta_cap_pct: float = 2.00,
) -> GreeksView:
    stale_s = int(stale_after.total_seconds())
    if monitor is None:
        return GreeksView(stale_after_s=stale_s)
    m = _json(monitor["detail"], {})
    equity = m.get("equity")
    vega = m.get("vega")
    by_under: dict[str, BetaDeltaRow] = {}
    for t, row in (m.get("delta_by_underlying") or {}).items():
        try:
            by_under[str(t)] = BetaDeltaRow.model_validate(row)
        except ValidationError:  # a malformed row is left out, never a 500
            continue
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
        dollar_delta=m.get("dollar_delta"),
        dollar_delta_cap=None if equity is None else dollar_delta_cap_pct * float(equity),
        beta_dollar_delta=m.get("beta_dollar_delta"),
        beta_delta_cap=None if equity is None else beta_delta_cap_pct * float(equity),
        delta_by_underlying=by_under,
        vega_usd=None if vega is None else float(vega) / 100.0,
        vega_cap_usd=None if equity is None else vega_cap_pct * float(equity),
        stale_after_s=stale_s,
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
                current_price=_dec(leg.get("current_price")),
                lastday_price=_dec(leg.get("lastday_price")),
                change_today=_dec(leg.get("change_today")),
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


def _proposal_direction(
    kind: str | None, st: dict[str, Any], exited: str | None
) -> Direction | None:
    """D50: an open reads its own legs; a close reads the structure it exits, else its own
    (reversed-side) legs flipped back."""
    if kind != "close":
        return direction_of(st.get("legs"))
    ex = _json(exited, {}) or {}
    if isinstance(ex, dict) and ex.get("legs"):
        return direction_of(ex["legs"])
    return direction_of(st.get("legs"), closing=True)


def _proposals(conn: sqlite3.Connection, since_day: str, limit: int) -> list[ProposalView]:
    approvals = _has_table(conn, "approval_requests")
    executions = _has_table(conn, "executions")
    join_a = "LEFT JOIN approval_requests a ON a.proposal_hash = p.proposal_hash"
    join_x = "LEFT JOIN executions x ON x.proposal_hash = p.proposal_hash"
    # D50: a close inherits the direction of the structure it exits.
    exited = (
        "(SELECT structure_json FROM open_structures WHERE exit_proposal_hash = p.proposal_hash"
        " LIMIT 1)"
        if _has_table(conn, "open_structures")
        else "NULL"
    )
    rows = conn.execute(
        f"""SELECT p.proposal_hash, p.day, p.ticker, p.kind, p.structure_json, p.quant_json,
                   p.sizing_json, p.created_at,
                   g.passed AS gate_passed, g.violations_json AS gate_violations,
                   {"a.status" if approvals else "NULL"} AS approval,
                   {"x.status" if executions else "NULL"} AS execution,
                   {"x.fill_price" if executions else "NULL"} AS fill_price,
                   {exited} AS exited_json
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
                direction=_proposal_direction(r["kind"], st, r["exited_json"]),
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


def _order_budget(monitor: sqlite3.Row | None) -> OrderBudgetView | None:
    if not monitor:
        return None
    ob = _json(monitor["detail"], {}).get("order_budget")
    if not isinstance(ob, dict) or not {"used", "limit", "tier"} <= set(ob):
        return None
    return OrderBudgetView(
        used=int(ob["used"]), limit=int(ob["limit"]), tier=str(ob["tier"]),
        as_of=parse_ts(monitor["at"]),
    )  # fmt: skip


def _ops(conn: sqlite3.Connection) -> OpsView:
    tick = _latest_heartbeat(conn, "tick")
    health = _latest_heartbeat(conn, "health")
    monitor = _latest_heartbeat(conn, "monitor")
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
        order_budget=_order_budget(monitor),
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
    dollar_delta_cap_pct: float = 1.00,
    vega_cap_pct: float = 0.010,
    limit: int = 200,
    stale_after: _dt.timedelta | None = None,
    beta_delta_cap_pct: float = 2.00,
) -> TowerSnapshot:
    """Read every dashboard section from *conn* (SELECT only) as of *now*.

    *stale_after* defaults to :func:`monitor_stale_after` (3x the monitor cadence).
    """
    now_et = now.astimezone(ET)
    since = now_et - _dt.timedelta(days=lookback_days)
    since_day = since.date().isoformat()
    monitor = _latest_heartbeat(conn, "monitor")
    violations = _violations(conn, since, limit)
    legs = _legs(monitor)
    stale = stale_after if stale_after is not None else monitor_stale_after(conn)
    return TowerSnapshot(
        as_of=now_et,
        db_path=db_path,
        pnl=_pnl(conn, monitor),
        greeks=_greeks(
            monitor,
            dollar_delta_cap_pct,
            vega_cap_pct,
            stale,
            beta_delta_cap_pct=beta_delta_cap_pct,
        ),
        structures=_structures(conn, legs),
        legs=legs,
        proposals=_proposals(conn, since_day, limit),
        halts=_halts(conn, 50),
        violations=violations,
        violation_counts=dict(Counter(v.code for v in violations).most_common()),
        ops=_ops(conn),
    )
