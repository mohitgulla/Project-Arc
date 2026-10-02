"""Weekly paper scorecard (E7.3): what the pipeline did and what came of it, from the audit store.

Everything here is read-only and deterministic (no LLM, no network, no broker).
One :func:`build_scorecard` call reads the SQLite audit store for a window
(default: the ET trading week that contains ``as_of``) and returns a typed
:class:`Scorecard`; :func:`render_markdown` turns it into the report committed
under ``docs/RESEARCH/weekly/`` and :func:`arc.slack.scorecard.scorecard_card`
into the Friday post in #arc-investor. The tower's Performance page (E8.7c)
calls the same functions.

Sections and where each number comes from:

- **Funnel**: ``proposals`` (open / close) created in the window → their
  ``gate_decisions`` (pass / fail) → ``approval_requests`` decided in the window
  (click-approved, auto-approved D34, rejected, expired, not actionable) →
  ``executions`` started in the window (filled, partial, cancelled, rejected…).
- **Gate violation histogram**: the rule code of every violation string on the
  window's failed gate decisions (``"<code>: <detail>"``).
- **Order budget (D32)**: broker submissions per ET day = ``orders`` rows (one per
  ladder attempt), against the daily cap, the restrictive tier and the open stop.
  The caps are passed in (``ArcSettings`` when E6.5 lands them, else the D32
  defaults); this module never decides them.
- **P&L**: realised P&L of every position fully closed in the window, from the
  journal (``exit:closed`` fills and ``reconcile:expired`` settlements, summed
  per structure, so partial closes count). Fill prices only, before commissions
  and fees. Plus the open positions' latest mark (E6.4 ``position_review``) and
  the account equity change from ``pnl_snapshots``.
- **D19 early exits**: for every position closed *before* expiry, realised P&L
  next to the hold-to-expiry shadow P&L (:func:`arc.journal.attribution.expiry_value`
  at the underlying's settlement), and for each close-to-reallocate swap the net
  of both legs against holding the closed position. Shadow values stay
  ``pending`` until the legs have expired and a settlement price is known.
- **D23 modelled vs realised**: the E2.4 :class:`~arc.exits.model.ExitModelResult`
  frozen with the open proposal (``market_contexts.analytics.exit_model``):
  managed and static (hold-to-expiry) net EV and PoP, against realised P&L and
  win rate.
- **Slippage**: per filled execution, fill − mid of the proposal's structure
  (positive = worse for us), against the modelled entry slippage
  (``ProposalAnalytics.entry_slippage``) and the Quant's ``cost_bps``.
- **Persona calibration**: stated confidence / PoP vs realised hit rate, the E7.4
  :func:`~arc.journal.attribution.calibration` buckets, cumulative up to the end
  of the window (a week alone is too few trades to bucket).
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter, defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.costs import CostModel
from arc.journal.attribution import MULTIPLIER, calibration, expiry_value, realised_pnl, slippage
from arc.journal.reasons import Choice, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.models import Structure
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable

    from arc.config import ArcSettings

__all__ = [
    "SCORECARD_VERSION",
    "ApprovalCounts",
    "BudgetDay",
    "CalibrationRow",
    "ClosedPosition",
    "Funnel",
    "ModelVsRealised",
    "OrderBudgetLimits",
    "PnlSummary",
    "Scorecard",
    "AutoApproveGate",
    "AutoApproveReadiness",
    "KindSlippage",
    "SlippageRow",
    "SlippageSummary",
    "SwapRow",
    "build_scorecard",
    "calibration_points",
    "auto_approve_gate",
    "auto_approve_readiness",
    "closed_positions",
    "execution_costs",
    "funnel",
    "model_vs_realised",
    "render_markdown",
    "slippage_by_kind",
    "slippage_since",
    "week_window",
]

SCORECARD_VERSION = 1
_FORBID = ConfigDict(extra="forbid", frozen=True)

AUTO_APPROVER = "arc:auto-approve"  # == arc.approvals.service.AUTO_APPROVER (no import: layering)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


class OrderBudgetLimits(BaseModel):
    """D32 daily options order budget: the numbers the scorecard reports usage against."""

    model_config = _FORBID

    daily_max: int = Field(200, ge=1)
    restrict_at: int = Field(100, ge=0)
    close_reserve: int = Field(25, ge=0)

    @property
    def open_stop(self) -> int:
        return self.daily_max - self.close_reserve


class BudgetDay(BaseModel):
    model_config = _FORBID

    day: _dt.date
    orders: int = Field(..., ge=0, description="Broker submissions (ladder attempts), open+close")
    opens: int = Field(0, ge=0)
    closes: int = Field(0, ge=0)
    tier: str = Field(..., description="normal | restrictive | opens_exhausted | exhausted")


class ApprovalCounts(BaseModel):
    model_config = _FORBID

    click_approved: int = 0
    auto_approved: int = 0
    rejected: int = 0
    expired: int = 0
    not_actionable: int = 0
    pending: int = 0


class Funnel(BaseModel):
    model_config = _FORBID

    proposals: int = 0
    proposals_open: int = 0
    proposals_close: int = 0
    gate_pass: int = 0
    gate_fail: int = 0
    approvals: ApprovalCounts = Field(default_factory=ApprovalCounts)
    executions: dict[str, int] = Field(default_factory=dict, description="status -> count")
    fills: int = Field(0, description="Executions filled in full or in part")
    contracts_filled: int = 0


class ClosedPosition(BaseModel):
    """One position fully closed in the window."""

    model_config = _FORBID

    structure_id: str
    ticker: str
    kind: str | None = None
    contracts: int
    opened_at: _dt.datetime | None = None
    closed_at: _dt.datetime
    expiration: _dt.date
    early: bool = Field(..., description="Closed before its last leg expired (D19)")
    exit_reason: str
    entry_net: float = Field(..., description="Per share, + debit / - credit")
    realised_pnl: float = Field(..., description="$ for the position, fills only, before fees")
    shadow_hold_pnl: float | None = Field(
        None, description="$ had it been held to expiry; None = pending (not expired / no settle)"
    )
    managed_net_ev: float | None = Field(None, description="E2.4 managed net EV x contracts, $")
    static_net_ev: float | None = Field(None, description="E2.4 hold-to-expiry net EV x contracts")
    managed_pop: float | None = None
    static_pop: float | None = None
    open_proposal_hash: str
    swap_id: str | None = None

    @property
    def early_exit_edge(self) -> float | None:
        """Realised − shadow hold: positive = closing early helped."""
        if self.shadow_hold_pnl is None:
            return None
        return self.realised_pnl - self.shadow_hold_pnl


class SwapRow(BaseModel):
    """A D19 close-to-reallocate swap: both legs against holding the closed position."""

    model_config = _FORBID

    swap_id: str
    day: _dt.date
    status: str
    close_ticker: str
    open_ticker: str
    close_realised: float | None = None
    open_pnl: float | None = Field(None, description="Realised if closed, else latest mark")
    open_marked: bool = Field(False, description="open_pnl is a mark, not realised")
    closed_hold_shadow: float | None = None

    @property
    def net(self) -> float | None:
        if self.close_realised is None or self.open_pnl is None:
            return None
        return self.close_realised + self.open_pnl

    @property
    def vs_hold(self) -> float | None:
        """Swap net − holding the closed position to expiry (positive = the swap helped)."""
        if self.net is None or self.closed_hold_shadow is None:
            return None
        return self.net - self.closed_hold_shadow


class PnlSummary(BaseModel):
    model_config = _FORBID

    realised: float = 0.0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    open_positions: int = 0
    open_marked_pnl: float | None = Field(None, description="Latest position_review mark, $")
    equity_start: float | None = None
    equity_end: float | None = None

    @property
    def equity_change(self) -> float | None:
        if self.equity_start is None or self.equity_end is None:
            return None
        return self.equity_end - self.equity_start

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.closed if self.closed else None


class SlippageRow(BaseModel):
    model_config = _FORBID

    proposal_hash: str
    ticker: str
    kind: str
    contracts: int
    realised_usd: float = Field(..., description="(fill - mid) x 100 x contracts; + = worse")
    realised_bps: float | None = Field(None, description="vs capital at risk")
    expected_usd: float | None = Field(None, description="Modelled entry slippage x contracts")
    cost_bps: float | None = Field(None, description="Quant's expected round-trip cost")
    at: _dt.datetime | None = Field(None, description="Execution start")
    structure_id: str | None = Field(None, description="open_structures.id, when known")
    commission: float = Field(0.0, description="Cost-model commission x contracts, $")
    fees: float = Field(0.0, description="Cost-model regulatory fees x contracts, $")
    fees_from_open: bool = Field(
        False,
        description="A close with no analytics of its own: its open's fee model was used",
    )
    structure_kind: str | None = Field(None, description="vertical_credit, iron_condor, ...")
    spread_usd: float | None = Field(
        None,
        description="Modelled bid-ask spread at proposal time (quote, else cost-model "
        "estimate), all legs x ratio x 100 x contracts (None = no leg analytics)",
    )


class KindSlippage(BaseModel):
    """Realised entry slippage of one structure kind as a fraction of the modelled spread."""

    model_config = _FORBID

    fills: int = Field(0, description="Fills with a known modelled spread")
    realised_usd: float = 0.0
    spread_usd: float = 0.0

    @property
    def frac(self) -> float | None:
        """x in the backtest fill model ``mid ± x·spread`` (realised ÷ modelled spread)."""
        return self.realised_usd / self.spread_usd if self.spread_usd > 0 else None


class SlippageSummary(BaseModel):
    model_config = _FORBID

    fills: int = 0
    realised_usd: float = 0.0
    expected_usd: float | None = Field(None, description="Sum over fills with a modelled value")
    realised_on_modelled_usd: float | None = Field(
        None, description="Realised over the same fills as expected_usd"
    )
    rows: list[SlippageRow] = Field(default_factory=list)

    @property
    def by_kind(self) -> dict[str, KindSlippage]:
        """Realised entry slippage per structure kind (fills with a modelled spread only)."""
        return slippage_by_kind(self.rows)


class ModelVsRealised(BaseModel):
    """D23: E2.4 managed-exit and hold-to-expiry model vs what happened (closed positions)."""

    model_config = _FORBID

    n: int = 0
    realised: float = 0.0
    managed_net_ev: float = 0.0
    static_net_ev: float = 0.0
    shadow_hold: float | None = Field(None, description="Sum of known shadows (n_shadow of n)")
    n_shadow: int = 0
    win_rate: float | None = None
    mean_managed_pop: float | None = None
    mean_static_pop: float | None = None


class CalibrationRow(BaseModel):
    model_config = _FORBID

    persona: str
    lo: float
    hi: float
    n: int
    stated_mean: float
    hit_rate: float


class Scorecard(BaseModel):
    """The weekly paper scorecard (E7.3). Money in $, probabilities as fractions."""

    model_config = _FORBID

    version: int = SCORECARD_VERSION
    start: _dt.datetime
    end: _dt.datetime
    generated_at: _dt.datetime
    funnel: Funnel
    gate_violations: dict[str, int] = Field(default_factory=dict, description="rule code -> n")
    order_budget: OrderBudgetLimits
    budget_days: list[BudgetDay] = Field(default_factory=list)
    pnl: PnlSummary
    closed: list[ClosedPosition] = Field(default_factory=list)
    swaps: list[SwapRow] = Field(default_factory=list)
    model_vs_realised: ModelVsRealised
    slippage: SlippageSummary
    calibration: list[CalibrationRow] = Field(default_factory=list)
    calibration_trades: int = Field(
        0, description="Closed trades behind the calibration (all time)"
    )
    auto_approve: AutoApproveGate | None = Field(
        None, description="E6.6a: scorecard gate state + verdict at generated_at"
    )

    @property
    def early_closed(self) -> list[ClosedPosition]:
        return [c for c in self.closed if c.early]

    @property
    def label(self) -> str:
        last = (self.end - _dt.timedelta(microseconds=1)).date()
        return f"{self.start.date():%b %d} – {last:%b %d, %Y}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def week_window(as_of: _dt.datetime) -> tuple[_dt.datetime, _dt.datetime]:
    """``[Monday 00:00 ET, next Monday 00:00 ET)`` of the week containing *as_of*."""
    d = as_of.astimezone(ET).date()
    monday = d - _dt.timedelta(days=d.weekday())
    start = _dt.datetime.combine(monday, _dt.time(), tzinfo=ET)
    return start, start + _dt.timedelta(days=7)


def _ts(text: str | None) -> _dt.datetime | None:
    """Parse any timestamp the store holds (to_db ``...Z`` or isoformat with offset)."""
    if not text:
        return None
    ts = _dt.datetime.fromisoformat(text)
    return ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts


def _in(ts: _dt.datetime | None, start: _dt.datetime, end: _dt.datetime) -> bool:
    return ts is not None and start <= ts < end


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:
        return None


def _tier(n: int, lim: OrderBudgetLimits) -> str:
    if n >= lim.daily_max:
        return "exhausted"
    if n >= lim.open_stop:
        return "opens_exhausted"
    if n >= lim.restrict_at:
        return "restrictive"
    return "normal"


def _analytics(conn: sqlite3.Connection, phash: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT payload FROM market_contexts WHERE proposal_hash = ?
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (phash,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0]).get("analytics")


def _structure(conn: sqlite3.Connection, phash: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT ticker, kind, structure_json, quant_json, swap_id FROM proposals
           WHERE proposal_hash = ?""",
        (phash,),
    ).fetchone()
    return dict(row) if row else None


def _realised_by_structure(
    conn: sqlite3.Connection,
) -> dict[str, list[tuple[_dt.datetime, Decimal, str, dict[str, Any]]]]:
    """structure id -> [(at, realised $, reason code, payload)] from the journal (oldest first)."""
    out: dict[str, list[tuple[_dt.datetime, Decimal, str, dict[str, Any]]]] = defaultdict(list)
    for r in conn.execute(
        """SELECT reason_code, payload, at FROM decisions
           WHERE reason_code IN (?, ?) ORDER BY at, rowid""",
        (str(ReasonCode.EXIT_CLOSED), str(ReasonCode.RECONCILE_EXPIRED)),
    ):
        p = json.loads(r["payload"])
        sid, pnl = p.get("structure_id"), _dec(p.get("realized_pnl"))
        at = _ts(r["at"])
        if sid and pnl is not None and at is not None:
            out[sid].append((at, pnl, r["reason_code"], p))
    return out


def _latest_marks(conn: sqlite3.Connection) -> dict[str, float]:
    """structure id -> latest E6.4 ``position_review`` mark (pnl_total, $ at mid)."""
    marks: dict[str, float] = {}
    for r in conn.execute(
        """SELECT subject, payload FROM context_entries WHERE kind = 'position_review'
           ORDER BY created_at, rowid"""
    ):
        try:
            marks[r["subject"]] = float(json.loads(r["payload"])["pnl_total"])
        except (KeyError, TypeError, ValueError):
            continue
    return marks


def _settles_from_journal(
    realised: dict[str, list[tuple[_dt.datetime, Decimal, str, dict[str, Any]]]],
    rows: dict[str, dict[str, Any]],
) -> dict[tuple[str, _dt.date], Decimal]:
    """(root, expiration) -> settle price recorded by the reconciler's expiry settlements."""
    out: dict[tuple[str, _dt.date], Decimal] = {}
    for sid, events in realised.items():
        row = rows.get(sid)
        if row is None:
            continue
        for _, _, code, p in events:
            settle = _dec(p.get("settle"))
            if code == ReasonCode.RECONCILE_EXPIRED and settle is not None:
                out[(row["ticker"], _expiration(row["structure_json"]))] = settle
    return out


def _expiration(structure_json: str) -> _dt.date:
    st = Structure.model_validate_json(structure_json)
    return max(parse_occ(leg.occ_symbol).expiration for leg in st.legs)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _funnel(
    conn: sqlite3.Connection, start: _dt.datetime, end: _dt.datetime
) -> tuple[Funnel, dict[str, int]]:
    f: dict[str, Any] = {"proposals": 0, "proposals_open": 0, "proposals_close": 0}
    for r in conn.execute("SELECT kind, created_at FROM proposals"):
        if _in(_ts(r["created_at"]), start, end):
            f["proposals"] += 1
            f["proposals_close" if r["kind"] == "close" else "proposals_open"] += 1
    violations: Counter[str] = Counter()
    passed = failed = 0
    for r in conn.execute("SELECT passed, violations_json, decided_at FROM gate_decisions"):
        if not _in(_ts(r["decided_at"]), start, end):
            continue
        if r["passed"]:
            passed += 1
            continue
        failed += 1
        for v in json.loads(r["violations_json"] or "[]"):
            violations[str(v).split(":", 1)[0].strip() or "unknown"] += 1
    appr: Counter[str] = Counter()
    for r in conn.execute(
        "SELECT status, decided_by, decided_at, created_at FROM approval_requests"
    ):
        status = r["status"]
        when = _ts(r["decided_at"]) if r["decided_at"] else _ts(r["created_at"])
        if not _in(when, start, end):
            continue
        if status == "approved":
            appr["auto_approved" if r["decided_by"] == AUTO_APPROVER else "click_approved"] += 1
        elif status in ("rejected", "expired", "not_actionable", "pending"):
            appr[status] += 1
    execs: Counter[str] = Counter()
    fills = contracts = 0
    for r in conn.execute("SELECT status, filled_qty, started_at FROM executions"):
        if not _in(_ts(r["started_at"]), start, end):
            continue
        execs[r["status"]] += 1
        if r["filled_qty"]:
            fills += 1
            contracts += int(r["filled_qty"])
    funnel = Funnel(
        **f,
        gate_pass=passed,
        gate_fail=failed,
        approvals=ApprovalCounts(**appr),
        executions=dict(sorted(execs.items())),
        fills=fills,
        contracts_filled=contracts,
    )
    return funnel, dict(violations.most_common())


def _budget(
    conn: sqlite3.Connection, start: _dt.datetime, end: _dt.datetime, lim: OrderBudgetLimits
) -> list[BudgetDay]:
    per: dict[_dt.date, Counter[str]] = defaultdict(Counter)
    for r in conn.execute(
        """SELECT o.created_at, COALESCE(p.kind, 'open') AS kind FROM orders o
           LEFT JOIN proposals p ON p.proposal_hash = o.proposal_hash"""
    ):
        ts = _ts(r["created_at"])
        if _in(ts, start, end):
            assert ts is not None
            per[ts.astimezone(ET).date()][r["kind"]] += 1
    return [
        BudgetDay(
            day=d,
            orders=sum(c.values()),
            opens=c["open"],
            closes=c["close"],
            tier=_tier(sum(c.values()), lim),
        )
        for d, c in sorted(per.items())
    ]


def _outcome_shadow(conn: sqlite3.Connection, phash: str) -> Decimal | None:
    """The latest recorded ``outcomes.hold_to_expiry_shadow_pnl`` for *phash* (D19)."""
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outcomes'"
    ).fetchone()
    if has is None:  # an older store without the journal tables
        return None
    row = conn.execute(
        """SELECT hold_to_expiry_shadow_pnl FROM outcomes WHERE proposal_hash = ?
           ORDER BY at DESC, rowid DESC LIMIT 1""",
        (phash,),
    ).fetchone()
    return _dec(row[0]) if row else None


def _closed_positions(
    conn: sqlite3.Connection,
    start: _dt.datetime,
    end: _dt.datetime,
    realised: dict[str, list[tuple[_dt.datetime, Decimal, str, dict[str, Any]]]],
    settle_price: Callable[[str, _dt.date], Decimal | None] | None,
    *,
    today: _dt.date,
    all_time: bool = False,
) -> list[ClosedPosition]:
    rows = {
        r["id"]: dict(r)
        for r in conn.execute("SELECT * FROM open_structures WHERE status = 'closed'")
    }
    journal_settles = _settles_from_journal(realised, rows)
    out: list[ClosedPosition] = []
    for sid, row in rows.items():
        closed_at = _ts(row["closed_at"])
        in_scope = closed_at is not None and (
            closed_at < end if all_time else _in(closed_at, start, end)
        )
        if closed_at is None or not in_scope:
            continue
        events = realised.get(sid, [])
        entry = Decimal(row["entry_net"])
        exp = _expiration(row["structure_json"])
        opened = conn.execute(
            "SELECT filled_qty FROM executions WHERE proposal_hash = ?",
            (row["open_proposal_hash"],),
        ).fetchone()
        n = int(opened["filled_qty"]) if opened and opened["filled_qty"] else int(row["contracts"])
        if events:
            pnl = sum((e[1] for e in events), Decimal(0))
        else:  # no journal row (older data): the final close only
            close_net = _dec(row["close_net"]) or Decimal(0)
            pnl = -(entry + close_net) * MULTIPLIER * int(row["contracts"])
        # early = at least one contract was closed by an order (an exit / swap close)
        # rather than settled at expiry by the reconciler
        if events:
            early = any(e[2] == ReasonCode.EXIT_CLOSED for e in events)
        else:
            early = closed_at.astimezone(ET).date() < exp
        reason = row["exit_reason"] or ("closed" if early else "expiry")
        st = Structure.model_validate_json(row["structure_json"])
        shadow: Decimal | None = None
        if not early:
            shadow = pnl  # held to expiry: the shadow is what happened
        elif exp < today:
            settle = journal_settles.get((row["ticker"], exp))
            if settle is None and settle_price is not None:
                settle = settle_price(row["ticker"], exp)
            if settle is not None:
                shadow = realised_pnl(entry=entry, exit_=expiry_value(st.legs, settle), contracts=n)
        if shadow is None:  # not derivable here: the recorded D19 outcome, if any
            shadow = _outcome_shadow(conn, row["open_proposal_hash"])
        prop = _structure(conn, row["open_proposal_hash"]) or {}
        model = (_analytics(conn, row["open_proposal_hash"]) or {}).get("exit_model") or {}
        managed, static = model.get("managed") or {}, model.get("static") or {}
        close_prop = (
            _structure(conn, row["exit_proposal_hash"]) if row["exit_proposal_hash"] else None
        )
        out.append(
            ClosedPosition(
                structure_id=sid,
                ticker=row["ticker"],
                kind=str(st.kind) if st.kind is not None else None,
                contracts=n,
                opened_at=_ts(row["opened_at"]),
                closed_at=closed_at,
                expiration=exp,
                early=early,
                exit_reason=reason,
                entry_net=float(entry),
                realised_pnl=float(pnl),
                shadow_hold_pnl=None if shadow is None else float(shadow),
                managed_net_ev=managed["net_ev"] * n if "net_ev" in managed else None,
                static_net_ev=static["net_ev"] * n if "net_ev" in static else None,
                managed_pop=managed.get("pop"),
                static_pop=static.get("pop"),
                open_proposal_hash=row["open_proposal_hash"],
                swap_id=(close_prop or {}).get("swap_id") or prop.get("swap_id"),
            )
        )
    return sorted(out, key=lambda c: (c.closed_at, c.structure_id))


def _swaps(
    conn: sqlite3.Connection,
    start: _dt.datetime,
    end: _dt.datetime,
    realised: dict[str, list[tuple[_dt.datetime, Decimal, str, dict[str, Any]]]],
    closed: dict[str, ClosedPosition],
    marks: dict[str, float],
) -> list[SwapRow]:
    rows: list[SwapRow] = []
    first, last = (
        start.astimezone(ET).date(),
        (end - _dt.timedelta(microseconds=1)).astimezone(ET).date(),
    )
    for r in conn.execute("SELECT * FROM swaps ORDER BY day, created_at"):
        day = _dt.date.fromisoformat(r["day"])
        if not first <= day <= last:
            continue
        sid = r["close_structure_id"]
        close_events = [e for e in realised.get(sid, []) if e[2] == ReasonCode.EXIT_CLOSED]
        close_realised = (
            float(sum((e[1] for e in close_events), Decimal(0))) if close_events else None
        )
        shadow = closed[sid].shadow_hold_pnl if sid in closed else None
        open_pnl: float | None = None
        marked = False
        if r["open_proposal_hash"]:
            srow = conn.execute(
                "SELECT id, status FROM open_structures WHERE open_proposal_hash = ?",
                (r["open_proposal_hash"],),
            ).fetchone()
            if srow is not None:
                done = realised.get(srow["id"], [])
                if srow["status"] == "closed" and done:
                    open_pnl = float(sum((e[1] for e in done), Decimal(0)))
                elif srow["id"] in marks:
                    open_pnl, marked = marks[srow["id"]], True
        rows.append(
            SwapRow(
                swap_id=r["id"],
                day=day,
                status=r["status"],
                close_ticker=r["close_ticker"],
                open_ticker=r["open_ticker"],
                close_realised=close_realised,
                open_pnl=open_pnl,
                open_marked=marked,
                closed_hold_shadow=shadow,
            )
        )
    return rows


def _fees(analytics: dict[str, Any]) -> tuple[float, float] | None:
    """(commission, regulatory) $ per unit from a stored ``ProposalAnalytics.entry_fees``."""
    fees = analytics.get("entry_fees")
    if not isinstance(fees, dict):
        return None
    commission = float(fees.get("commission") or 0.0)
    regulatory = sum(float(fees.get(k) or 0.0) for k in ("orf", "occ", "cat", "taf", "sec"))
    return commission, regulatory


def _spread_usd(analytics: dict[str, Any], contracts: int) -> float | None:
    """Modelled bid-ask spread of every leg (x ratio x 100 x contracts) at proposal time.

    Per leg: the quoted spread when the NBBO was valid, else the cost model's estimate
    (``CostModel.spread``), i.e. the spread the proposal's Net EV was priced with.
    ``None`` when there are no leg analytics or a leg had no mid.
    """
    legs = analytics.get("legs") or []
    if not legs:
        return None
    try:
        model = CostModel.model_validate(analytics.get("cost_model") or {})
    except ValueError:
        return None
    total = 0.0
    for leg in legs:
        mid = leg.get("mid")
        if mid is None:
            return None
        total += model.spread(float(mid), leg.get("bid"), leg.get("ask")) * int(
            leg.get("ratio") or 1
        )
    return total * float(MULTIPLIER) * contracts


def _slippage_row(conn: sqlite3.Connection, r: sqlite3.Row) -> SlippageRow | None:
    """One filled execution → its slippage row (None when the proposal is unknown)."""
    prop = _structure(conn, r["proposal_hash"])
    if prop is None:
        return None
    st = Structure.model_validate_json(prop["structure_json"])
    n = int(r["filled_qty"])
    usd, bps = slippage(
        limit=st.net_debit_credit,
        fill=Decimal(r["fill_price"]),
        contracts=n,
        max_loss_per_contract=st.max_loss,
    )
    analytics = _analytics(conn, r["proposal_hash"]) or {}
    exp_unit = analytics.get("entry_slippage")
    quant = json.loads(prop["quant_json"] or "{}")
    fees = _fees(analytics)
    from_open = False
    if fees is None and r["kind"] == "close" and r["open_proposal_hash"]:
        fees = _fees(_analytics(conn, r["open_proposal_hash"]) or {})
        from_open = fees is not None
    commission, regulatory = fees or (0.0, 0.0)
    return SlippageRow(
        proposal_hash=r["proposal_hash"],
        ticker=prop["ticker"] or "",
        kind=r["kind"],
        contracts=n,
        realised_usd=float(usd),
        realised_bps=bps,
        expected_usd=None if exp_unit is None else float(exp_unit) * n,
        cost_bps=quant.get("cost_bps"),
        structure_kind=str(st.kind) if st.kind is not None else None,
        spread_usd=_spread_usd(analytics, n),
        at=_ts(r["started_at"]),
        structure_id=r["structure_id"],
        commission=commission * n,
        fees=regulatory * n,
        fees_from_open=from_open,
    )


def slippage_by_kind(rows: Iterable[SlippageRow]) -> dict[str, KindSlippage]:
    """Entry fills grouped by structure kind; only fills with a modelled spread count."""
    acc: dict[str, list[SlippageRow]] = defaultdict(list)
    for r in rows:
        if r.kind == "open" and r.structure_kind and r.spread_usd is not None:
            acc[r.structure_kind].append(r)
    return {
        k: KindSlippage(
            fills=len(v),
            realised_usd=sum(x.realised_usd for x in v),
            spread_usd=sum(x.spread_usd or 0.0 for x in v),
        )
        for k, v in sorted(acc.items())
    }


def _filled_executions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT e.proposal_hash, e.kind, e.filled_qty, e.fill_price, e.started_at,
                      COALESCE(e.structure_id, s.id) AS structure_id, s2.open_proposal_hash
               FROM executions e
               LEFT JOIN open_structures s ON s.open_proposal_hash = e.proposal_hash
               LEFT JOIN open_structures s2 ON s2.id = e.structure_id
               WHERE e.filled_qty > 0 AND e.fill_price IS NOT NULL
               ORDER BY e.started_at, e.rowid"""
        )
    )


def _slippage(conn: sqlite3.Connection, start: _dt.datetime, end: _dt.datetime) -> SlippageSummary:
    rows: list[SlippageRow] = []
    for r in _filled_executions(conn):
        if not _in(_ts(r["started_at"]), start, end):
            continue
        row = _slippage_row(conn, r)
        if row is not None:
            rows.append(row)
    modelled = [x for x in rows if x.expected_usd is not None]
    return SlippageSummary(
        fills=len(rows),
        realised_usd=sum(x.realised_usd for x in rows),
        expected_usd=sum(x.expected_usd or 0.0 for x in modelled) if modelled else None,
        realised_on_modelled_usd=sum(x.realised_usd for x in modelled) if modelled else None,
        rows=rows,
    )


def _model_vs_realised(closed: Iterable[ClosedPosition]) -> ModelVsRealised:
    rows = [c for c in closed if c.managed_net_ev is not None and c.static_net_ev is not None]
    if not rows:
        return ModelVsRealised()
    shadows = [c.shadow_hold_pnl for c in rows if c.shadow_hold_pnl is not None]
    mpop = [c.managed_pop for c in rows if c.managed_pop is not None]
    spop = [c.static_pop for c in rows if c.static_pop is not None]
    return ModelVsRealised(
        n=len(rows),
        realised=sum(c.realised_pnl for c in rows),
        managed_net_ev=sum(c.managed_net_ev or 0.0 for c in rows),
        static_net_ev=sum(c.static_net_ev or 0.0 for c in rows),
        shadow_hold=sum(shadows) if shadows else None,
        n_shadow=len(shadows),
        win_rate=sum(1 for c in rows if c.realised_pnl > 0) / len(rows),
        mean_managed_pop=sum(mpop) / len(mpop) if mpop else None,
        mean_static_pop=sum(spop) / len(spop) if spop else None,
    )


def _pnl(
    conn: sqlite3.Connection,
    start: _dt.datetime,
    end: _dt.datetime,
    closed: list[ClosedPosition],
    marks: dict[str, float],
) -> PnlSummary:
    open_ids = [r["id"] for r in conn.execute("SELECT id FROM open_structures WHERE status='open'")]
    known = [marks[i] for i in open_ids if i in marks]
    series: list[tuple[_dt.date, float]] = []
    for r in conn.execute("SELECT details_json FROM pnl_snapshots ORDER BY snapshot_at, rowid"):
        d = json.loads(r["details_json"] or "{}")
        if d.get("day") and d.get("equity") not in (None, ""):
            series.append((_dt.date.fromisoformat(d["day"]), float(d["equity"])))
    daily = dict(series)  # latest snapshot per day wins
    first, last = start.astimezone(ET).date(), end.astimezone(ET).date()
    before = [v for d, v in sorted(daily.items()) if d < first]
    inside = [v for d, v in sorted(daily.items()) if first <= d < last]
    eq_start = before[-1] if before else (inside[0] if inside else None)
    return PnlSummary(
        realised=sum(c.realised_pnl for c in closed),
        closed=len(closed),
        wins=sum(1 for c in closed if c.realised_pnl > 0),
        losses=sum(1 for c in closed if c.realised_pnl <= 0),
        open_positions=len(open_ids),
        open_marked_pnl=sum(known) if known else None,
        equity_start=eq_start,
        equity_end=inside[-1] if inside else None,
    )


def closed_positions(
    conn: sqlite3.Connection,
    *,
    start: _dt.datetime,
    end: _dt.datetime,
    now: _dt.datetime,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None = None,
    all_time: bool = False,
) -> list[ClosedPosition]:
    """Every position fully closed in ``[start, end)`` (``all_time``: closed before *end*),
    exactly as the scorecard counts them (public for the tower's Performance page, E8.7c)."""
    today = now.astimezone(ET).date()
    return _closed_positions(
        conn, start, end, _realised_by_structure(conn), settle_price, today=today,
        all_time=all_time,
    )  # fmt: skip


def funnel(
    conn: sqlite3.Connection, start: _dt.datetime, end: _dt.datetime
) -> tuple[Funnel, dict[str, int]]:
    """The window's funnel and its gate violation histogram (rule code -> n)."""
    return _funnel(conn, start, end)


def execution_costs(
    conn: sqlite3.Connection, start: _dt.datetime, end: _dt.datetime
) -> SlippageSummary:
    """Per filled execution started in ``[start, end)``: realised fill − mid, modelled
    slippage, and the cost model's commission and fees (the scorecard's slippage section)."""
    return _slippage(conn, start, end)


def model_vs_realised(closed: Iterable[ClosedPosition]) -> ModelVsRealised:
    """D23: managed and hold-to-expiry model vs realised over *closed*."""
    return _model_vs_realised(closed)


def build_scorecard(
    conn: sqlite3.Connection,
    *,
    start: _dt.datetime,
    end: _dt.datetime,
    now: _dt.datetime,
    limits: OrderBudgetLimits | None = None,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None = None,
    auto_approve: AutoApproveGate | None = None,
) -> Scorecard:
    """The scorecard for ``[start, end)`` from the audit store (read-only).

    *auto_approve* (E6.6a, from :func:`auto_approve_gate`) puts the scorecard gate
    state in the header and the funnel's approval row.

    *settle_price* ``(root, expiration) -> close`` prices the D19 hold-to-expiry
    shadow of early-closed positions whose legs have expired; ``None`` (or an
    unknown price) leaves the shadow pending unless the reconciler already
    recorded that settlement.
    """
    limits = limits or OrderBudgetLimits()
    funnel, violations = _funnel(conn, start, end)
    realised = _realised_by_structure(conn)
    marks = _latest_marks(conn)
    today = now.astimezone(ET).date()
    closed = _closed_positions(conn, start, end, realised, settle_price, today=today)
    by_id = {c.structure_id: c for c in closed}
    # swaps can close a position in an earlier week: resolve their closed legs too
    everything = {
        c.structure_id: c
        for c in _closed_positions(
            conn, start, end, realised, settle_price, today=today, all_time=True
        )
    }
    swaps = _swaps(conn, start, end, realised, {**everything, **by_id}, marks)
    history = list(everything.values())
    buckets = calibration(
        calibration_points(conn, [(c.open_proposal_hash, c.realised_pnl > 0) for c in history])
    )
    return Scorecard(
        start=start,
        end=end,
        generated_at=now,
        funnel=funnel,
        gate_violations=violations,
        order_budget=limits,
        budget_days=_budget(conn, start, end, limits),
        pnl=_pnl(conn, start, end, closed, marks),
        closed=closed,
        swaps=swaps,
        model_vs_realised=_model_vs_realised(closed),
        slippage=_slippage(conn, start, end),
        calibration=[
            CalibrationRow(
                persona=b.persona,
                lo=b.lo,
                hi=b.hi,
                n=b.n,
                stated_mean=b.stated_mean,
                hit_rate=b.hit_rate,
            )
            for b in buckets
        ],
        calibration_trades=len(history),
        auto_approve=auto_approve,
    )


class AutoApproveReadiness(BaseModel):
    """E7.5a: may D34 auto-approve open a new position right now? (fail closed)

    Built by :func:`auto_approve_readiness` from the same closed positions and fills
    the weekly scorecard reports. ``failing`` holds one code per unmet criterion:

    - ``min_closed_trades``: fewer closed trades than the configured minimum;
    - ``negative_realised_ev``: mean realised P&L per trade, net of fees, < 0 over
      the window (the latest ``min_closed_trades`` closed trades);
    - ``slippage_over_tolerance``: realised entry slippage over the window's fills
      > their modelled half-spread x tolerance;
    - ``slippage_unknown``: no fill in the window has a modelled spread to compare to.
    """

    model_config = _FORBID

    ok: bool
    failing: list[str] = Field(default_factory=list)
    closed_trades: int = Field(..., description="Closed trades in the store, all time")
    min_closed_trades: int
    window_trades: int = Field(0, description="Latest closed trades the EV / slippage use")
    realised_pnl: float = Field(0.0, description="$ over the window, fills only, before fees")
    fees: float = Field(0.0, description="$ fees over the window (entry + close legs)")
    realised_net_ev: float | None = Field(None, description="$ per trade, net of fees")
    slippage_fills: int = Field(0, description="Window entry fills with a modelled spread")
    realised_slippage: float | None = Field(None, description="$ fill - mid over those fills")
    half_spread: float | None = Field(None, description="$ modelled half-spread, same fills")
    slippage_tolerance: float

    @property
    def slippage_limit(self) -> float | None:
        return None if self.half_spread is None else self.half_spread * self.slippage_tolerance

    def summary(self) -> str:
        """One line for the journal, the card and the log."""
        if self.ok:
            return (
                f"scorecard gate met: {self.closed_trades} closed trades, net EV "
                f"{_usd(self.realised_net_ev)}/trade, slippage {_usd(self.realised_slippage)} "
                f"<= {_usd(self.slippage_limit, signed=False)}"
            )
        parts: list[str] = []
        for code in self.failing:
            if code == "min_closed_trades":
                parts.append(
                    f"{self.closed_trades} closed trades < {self.min_closed_trades} required"
                )
            elif code == "negative_realised_ev":
                parts.append(
                    f"realised net EV {_usd(self.realised_net_ev)}/trade over the last "
                    f"{self.window_trades} trades < $0"
                )
            elif code == "slippage_over_tolerance":
                parts.append(
                    f"realised slippage {_usd(self.realised_slippage)} > half-spread "
                    f"{_usd(self.half_spread, signed=False)} x {self.slippage_tolerance:g}"
                )
            else:
                parts.append("no fill with a modelled spread to check slippage against")
        return "; ".join(parts)

    def gate_state(self, gate_on: bool) -> str:
        """E6.6a: ``off`` (opt-out), ``met`` or ``unmet`` (holding opens)."""
        if not gate_on:
            return "off"
        return "met" if self.ok else "unmet"

    def gate_line(self, gate_on: bool) -> str:
        """E6.6a: one line naming the gate state and what it would say.

        ``scorecard gate: OFF (opt-out) — 3 closed trades < 30 required; realised net
        EV -$154.95/trade``. Shown in the scorecard header and funnel, the tower Ops
        page and ``arc approve auto status``.
        """
        state = {"off": "OFF (opt-out)", "met": "met", "unmet": "holding opens"}[
            self.gate_state(gate_on)
        ]
        text = self.summary()
        if self.ok:
            text = text.removeprefix("scorecard gate met: ")
        elif "negative_realised_ev" not in self.failing and self.realised_net_ev is not None:
            text += (
                f"; realised net EV {_usd(self.realised_net_ev)}/trade over "
                f"{self.window_trades} trade{'s' if self.window_trades != 1 else ''}"
            )
        return f"scorecard gate: {state} — {text}"

    def snapshot(self, gate_on: bool) -> dict[str, Any]:
        """E6.6a: what the gate said at this approval, for the AUTO_APPROVE journal row."""
        return {
            "scorecard_gate": self.gate_state(gate_on),
            "failing": list(self.failing),
            "closed_trades": self.closed_trades,
            "min_closed_trades": self.min_closed_trades,
            "window_trades": self.window_trades,
            "realised_net_ev": self.realised_net_ev,
            "realised_slippage": self.realised_slippage,
            "half_spread": self.half_spread,
            "slippage_tolerance": self.slippage_tolerance,
        }


class AutoApproveGate(BaseModel):
    """E6.6a: the scorecard gate switch and its verdict, as the weekly scorecard shows it."""

    model_config = _FORBID

    scorecard_gate: bool = Field(..., description="auto_approve.scorecard_gate (effective)")
    readiness: AutoApproveReadiness

    @property
    def line(self) -> str:
        return self.readiness.gate_line(self.scorecard_gate)


Scorecard.model_rebuild()  # resolves the forward reference to AutoApproveGate


def auto_approve_gate(
    conn: sqlite3.Connection, settings: ArcSettings, *, now: _dt.datetime
) -> AutoApproveGate:
    """E6.6a: the gate switch and its verdict at *now* under *settings* (effective).

    The one place the scorecard, the tower and ``arc approve auto status`` build it,
    so all three report the same numbers for the same *now*.
    """
    return AutoApproveGate(
        scorecard_gate=bool(settings.auto_approve_scorecard_gate),
        readiness=auto_approve_readiness(
            conn,
            now=now,
            min_closed_trades=settings.auto_approve_min_closed_trades,
            slippage_tolerance=settings.auto_approve_slippage_tolerance,
        ),
    )


def auto_approve_readiness(
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    min_closed_trades: int,
    slippage_tolerance: float,
) -> AutoApproveReadiness:
    """E7.5a scorecard gate for D34 auto-approve, from the audit store (read-only).

    The window is the latest ``min_closed_trades`` positions closed before *now*.
    Net EV = mean over the window of realised P&L (journal fills) minus fees: the
    entry fees frozen with the open proposal's analytics, charged again for a
    position closed by an order (expiry settlements pay no close fees). Slippage
    compares the window's entry fills (fill − mid, + = worse) with half the modelled
    bid-ask spread of the same fills x *slippage_tolerance*. Every unknown fails
    closed: no fills with a modelled spread → ``slippage_unknown``.
    """
    realised = _realised_by_structure(conn)
    closed = _closed_positions(
        conn,
        _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC),
        now,
        realised,
        None,
        today=now.astimezone(ET).date(),
        all_time=True,
    )
    window = closed[-min_closed_trades:] if min_closed_trades > 0 else []
    failing: list[str] = []
    if len(closed) < min_closed_trades:
        failing.append("min_closed_trades")
    pnl = sum(c.realised_pnl for c in window)
    fees = 0.0
    fills: list[SlippageRow] = []
    by_open = {r["proposal_hash"]: r for r in _filled_executions(conn) if r["kind"] == "open"}
    for c in window:
        per_unit = ((_analytics(conn, c.open_proposal_hash) or {}).get("entry_fees")) or {}
        unit_fees = sum(float(v) for v in per_unit.values())
        fees += unit_fees * c.contracts * (2 if c.early else 1)
        ex = by_open.get(c.open_proposal_hash)
        row = _slippage_row(conn, ex) if ex is not None else None
        if row is not None and row.spread_usd is not None:
            fills.append(row)
    net_ev = (pnl - fees) / len(window) if window else None
    if net_ev is not None and net_ev < 0:
        failing.append("negative_realised_ev")
    slip = sum(r.realised_usd for r in fills) if fills else None
    half = sum((r.spread_usd or 0.0) for r in fills) / 2 if fills else None
    if slip is not None and half is not None and slip > half * slippage_tolerance:
        failing.append("slippage_over_tolerance")
    elif (slip is None or half is None) and "min_closed_trades" not in failing:
        failing.append("slippage_unknown")
    return AutoApproveReadiness(
        ok=not failing,
        failing=failing,
        closed_trades=len(closed),
        min_closed_trades=min_closed_trades,
        window_trades=len(window),
        realised_pnl=pnl,
        fees=fees,
        realised_net_ev=net_ev,
        slippage_fills=len(fills),
        realised_slippage=slip,
        half_spread=half,
        slippage_tolerance=slippage_tolerance,
    )


def slippage_since(
    conn: sqlite3.Connection, *, start: _dt.datetime, end: _dt.datetime
) -> dict[str, KindSlippage]:
    """Realised entry slippage per structure kind over ``[start, end)`` (all fills)."""
    return _slippage(conn, start, end).by_kind


def calibration_points(
    conn: sqlite3.Connection, results: Iterable[tuple[str, bool]]
) -> list[tuple[str, float, bool]]:
    """``(persona, stated probability, hit)`` for each ``(proposal_hash, hit)`` result.

    ``hit`` is the caller's outcome (realised P&L > 0). Stated probabilities: the
    Quant's PoP on the ``proposed`` decision (``quant_pop``) and the confidence of
    every persona that *selected* the ticker in the proposal's chain (shortlist /
    structure stage). Shared by ``arc journal gaps`` and the weekly scorecard (E7.3).
    """
    j = JournalStore(conn)
    points: list[tuple[str, float, bool]] = []
    for phash, hit in results:
        for d in j.decisions(proposal_hash=phash):
            if d.reason_code is ReasonCode.PROPOSED and "quant" in d.payload:
                points.append(("quant_pop", float(d.payload["quant"]["pop"]), hit))
        chain = j.chain_for_proposal(phash)
        ticker = conn.execute(
            "SELECT ticker FROM proposals WHERE proposal_hash = ?", (phash,)
        ).fetchone()
        for d in j.decisions(chain_run_id=chain) if chain and ticker else []:
            if (
                d.subject == ticker[0]
                and d.choice is Choice.SELECTED
                and d.confidence is not None
                and d.stage in (Stage.SHORTLIST, Stage.STRUCTURE)
            ):
                points.append((d.persona.value, d.confidence, hit))
    return points


# ---------------------------------------------------------------------------
# Markdown (docs/RESEARCH/weekly/<monday>.md)
# ---------------------------------------------------------------------------


def _usd(v: float | None, *, signed: bool = True) -> str:
    if v is None:
        return "pending"
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}${abs(v):,.2f}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _md_cell(text: str) -> str:
    return text.replace("|", "/")


def render_markdown(sc: Scorecard) -> str:
    """The committed weekly report. Plain Markdown tables, one section per metric group."""
    f, a, p = sc.funnel, sc.funnel.approvals, sc.pnl
    lim = sc.order_budget
    out = [
        f"# Paper scorecard: week of {sc.label}",
        "",
        f"Window `{sc.start.isoformat()}` to `{sc.end.isoformat()}` (ET). "
        f"Generated {sc.generated_at.astimezone(ET):%Y-%m-%d %H:%M %Z} from the audit store "
        f"(scorecard v{sc.version}). Paper account.",
        "",
        *([f"**{_md_cell(sc.auto_approve.line)}**", ""] if sc.auto_approve else []),
        "## Funnel",
        "",
        "| Stage | Count |",
        "|---|---|",
        f"| Proposals (open / close) | {f.proposals} ({f.proposals_open} / {f.proposals_close}) |",
        f"| Gate pass / fail | {f.gate_pass} / {f.gate_fail} |",
        f"| Approved: click / auto (D34) | {a.click_approved} / {a.auto_approved}"
        + (f" ({_md_cell(sc.auto_approve.line)})" if sc.auto_approve else "")
        + " |",
        f"| Rejected / expired / not actionable | {a.rejected} / {a.expired} / "
        f"{a.not_actionable} |",
        f"| Executions | {', '.join(f'{k} {v}' for k, v in f.executions.items()) or 'none'} |",
        f"| Fills (contracts) | {f.fills} ({f.contracts_filled}) |",
        "",
        "## Gate violations",
        "",
    ]
    if sc.gate_violations:
        out += ["| Rule | Count |", "|---|---|"]
        out += [f"| `{k}` | {v} |" for k, v in sc.gate_violations.items()]
    else:
        out.append("None.")
    out += [
        "",
        f"## Order budget (D32: {lim.daily_max}/day, restrictive at {lim.restrict_at}, "
        f"opens stop at {lim.open_stop})",
        "",
    ]
    if sc.budget_days:
        out += ["| Day | Orders | Opens / closes | Tier |", "|---|---|---|---|"]
        out += [
            f"| {d.day:%a %b %d} | {d.orders}/{lim.daily_max} | {d.opens} / {d.closes} | {d.tier} |"
            for d in sc.budget_days
        ]
        hits = Counter(d.tier for d in sc.budget_days if d.tier != "normal")
        out += ["", f"Tier hits: {', '.join(f'{k} {v}' for k, v in hits.items()) or 'none'}."]
    else:
        out.append("No broker orders.")
    out += [
        "",
        "## P&L",
        "",
        "Realised P&L is from fill prices, before commissions and fees.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Realised (closed positions) | {_usd(p.realised)} |",
        f"| Closed: wins / losses (win rate) | {p.wins} / {p.losses} ({_pct(p.win_rate)}) |",
        f"| Open positions (latest mark) | {p.open_positions} "
        f"({_usd(p.open_marked_pnl) if p.open_marked_pnl is not None else 'no mark'}) |",
        f"| Equity change | {_usd(p.equity_change) if p.equity_change is not None else 'n/a'} |",
        "",
        "## Early exits vs hold to expiry (D19)",
        "",
    ]
    early = sc.early_closed
    if early:
        out += [
            "| Ticker | Closed | Reason | Realised | Hold to expiry | Early-exit edge |",
            "|---|---|---|---|---|---|",
        ]
        out += [
            f"| {c.ticker} | {c.closed_at.astimezone(ET):%b %d} | {c.exit_reason} | "
            f"{_usd(c.realised_pnl)} | {_usd(c.shadow_hold_pnl)} | {_usd(c.early_exit_edge)} |"
            for c in early
        ]
        known = [c.early_exit_edge for c in early if c.early_exit_edge is not None]
        out += [
            "",
            f"Net early-exit edge: {_usd(sum(known)) if known else 'pending'} "
            f"({len(known)} of {len(early)} known; the rest have not expired yet).",
        ]
    else:
        out.append("No positions closed early.")
    out += ["", "### Close-to-reallocate swaps", ""]
    if sc.swaps:
        out += [
            "| Day | Close → open | Status | Close leg | Open leg | Net | Hold closed "
            "| Swap vs hold |",
            "|---|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {s.day:%b %d} | {s.close_ticker} → {s.open_ticker} | {s.status} | "
            f"{_usd(s.close_realised)} | {_usd(s.open_pnl)}{' (mark)' if s.open_marked else ''} | "
            f"{_usd(s.net)} | {_usd(s.closed_hold_shadow)} | {_usd(s.vs_hold)} |"
            for s in sc.swaps
        ]
    else:
        out.append("No swaps.")
    m = sc.model_vs_realised
    out += ["", "## Modelled vs realised (D23, E2.4 exit model)", ""]
    if m.n:
        shadow = (
            f"{_usd(m.shadow_hold)} ({m.n_shadow} of {m.n})"
            if m.shadow_hold is not None
            else "pending"
        )
        out += [
            "| | Managed exits | Hold to expiry | Realised |",
            "|---|---|---|---|",
            f"| Net EV / P&L | {_usd(m.managed_net_ev)} | {_usd(m.static_net_ev)} "
            f"(shadow {shadow}) | {_usd(m.realised)} |",
            f"| PoP / win rate | {_pct(m.mean_managed_pop)} | {_pct(m.mean_static_pop)} | "
            f"{_pct(m.win_rate)} |",
            "",
            f"{m.n} closed position(s) with a stored exit model. EVs are net of spread, "
            "slippage and fees (the shared cost model); realised is fills only.",
        ]
    else:
        out.append("No closed positions with a stored exit model.")
    s = sc.slippage
    n_modelled = sum(1 for r in s.rows if r.expected_usd is not None)
    out += ["", "## Slippage: realised vs expected", ""]
    if s.fills:
        out += [
            f"Realised {_usd(s.realised_usd)} over {s.fills} fill(s) (fill − mid; + = worse). "
            + (
                f"Modelled {_usd(s.expected_usd)} vs realised "
                f"{_usd(s.realised_on_modelled_usd)} on the {n_modelled} fill(s) with a model."
                if s.expected_usd is not None
                else "No modelled slippage stored for these fills."
            ),
            "",
            "| Ticker | Kind | Contracts | Realised | bps of risk | Modelled | Quant cost bps |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {r.ticker} | {r.kind} | {r.contracts} | {_usd(r.realised_usd)} | "
            f"{'n/a' if r.realised_bps is None else f'{r.realised_bps:.0f}'} | "
            f"{'n/a' if r.expected_usd is None else _usd(r.expected_usd)} | "
            f"{'n/a' if r.cost_bps is None else f'{r.cost_bps:.0f}'} |"
            for r in s.rows
        ]
        kinds = s.by_kind
        if kinds:
            out += [
                "",
                "Entry slippage by structure kind (x of the modelled spread; feeds "
                "`arc backtest rank --slippage-from-scorecard`):",
                "",
                "| Structure | Fills | Realised | Modelled spread | x |",
                "|---|---|---|---|---|",
            ]
            out += [
                f"| {k} | {v.fills} | {_usd(v.realised_usd)} | {_usd(v.spread_usd, signed=False)} "
                f"| {'n/a' if v.frac is None else f'{v.frac:.2f}'} |"
                for k, v in kinds.items()
            ]
    else:
        out.append("No fills.")
    out += [
        "",
        f"## Persona calibration (all closed trades to date: {sc.calibration_trades})",
        "",
    ]
    if sc.calibration:
        out += ["| Persona | Bucket | n | Stated | Realised | Gap |", "|---|---|---|---|---|---|"]
        out += [
            f"| {b.persona} | [{b.lo:.1f}, {b.hi:.1f}) | {b.n} | {b.stated_mean:.0%} | "
            f"{b.hit_rate:.0%} | {b.hit_rate - b.stated_mean:+.0%} |"
            for b in sc.calibration
        ]
        out += ["", "Gap = realised − stated; negative = over-confident."]
    else:
        out.append("n/a: no closed trades yet.")
    return "\n".join(out) + "\n"
