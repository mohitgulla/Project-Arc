"""Overview + Positions loaders for control tower v2 (E8.7a, D35).

Pure reads over the audit store (``mode=ro`` connection from
:func:`arc.tower.data.connect_ro`), built on the E8.3 section loaders in
:mod:`arc.tower.data` (extended, not forked). Every section carries the
timestamp of the row it came from, so the SPA can show an age next to every
number (``docs/TOWER_DESIGN.md`` §7).

========================  ==========================================================
Section                   Source
========================  ==========================================================
Status strip              active ``halts`` row, ``tick`` / ``health`` heartbeats,
                          open ``ops_alerts``, D32 ``order_budget`` in the latest
                          ``monitor`` heartbeat
Equity                    ``1D``: today's ``monitor`` heartbeats (:func:`equity_intraday`);
                          other ranges: ``pnl_snapshots`` daily equity
                          (:func:`arc.reconcile.performance.daily_equity`). D87 benchmarks:
                          SPY/QQQ marks on the same heartbeats (``benchmarks``), daily
                          closes from ``iv_daily`` (``spot_basis = last_close``)
Account split             latest ``monitor`` heartbeat: ``cash`` (available) vs
                          ``equity − cash`` (held in open positions), D87
P&L today                 latest ``monitor`` heartbeat (``equity − prev_close``, D43), else
                          the reconciled ``pnl_snapshots`` row; MTD / YTD from
                          :func:`arc.reconcile.performance.performance_from`
Positions                 ``open_structures`` + broker legs of the latest ``monitor``
                          heartbeat + reconcile ``held`` flag (``positions_snapshots``)
Greeks vs caps            latest ``monitor`` heartbeat + the gate's caps (PLAN §5)
Today's proposals         ``proposals`` + gate / approval / execution (last 24 h), net
                          EV after costs from ``market_contexts`` analytics
Movers                    open structures' legs: ``change_today`` + today's marks
Recent activity           ``fills``, ``executions``, close ``proposals``, ``halts``,
                          ``pnl_snapshots.details.clean``, ``ops_alerts``
========================  ==========================================================
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import to_db
from arc.models import Performance  # noqa: TC001 - pydantic field
from arc.reconcile.baseline import BaselineSource  # noqa: TC001 - pydantic field
from arc.reconcile.performance import DailyEquity, daily_equity, performance_from
from arc.routines.config import AdvisoryBand, GreekAdvisorySettings
from arc.tower.data import (
    Direction,
    GreeksView,
    HaltView,
    LegView,
    OrderBudgetView,
    ProposalView,
    _dec,
    _greeks,
    _halts,
    _has_table,
    _json,
    _latest_heartbeat,
    _legs,
    _order_budget,
    _proposals,
    direction_of,
    parse_ts,
    prev_close_of,
)
from arc.tower.data_exits import (
    ExitCaseView,
    ExitPathMode,
    ExitPathStrip,
    ExitVerdictView,
    ExitWatchView,
    load_exit_path,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "RANGES",
    "ActivityEntry",
    "ActivityItem",
    "AlertView",
    "DayPnlSection",
    "EquityPoint",
    "EquitySection",
    "GreeksSection",
    "IntradayMark",
    "MoverTile",
    "OverviewRange",
    "OverviewResponse",
    "PositionRow",
    "PositionsResponse",
    "ProposalRow",
    "StatusSection",
    "equity_intraday",
    "load_overview",
    "load_positions",
    "range_start",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

OverviewRange = Literal["1D", "1W", "1M", "3M", "YTD", "ALL"]
RANGES: tuple[OverviewRange, ...] = ("1D", "1W", "1M", "3M", "YTD", "ALL")
_RANGE_DAYS = {"1W": 7, "1M": 30, "3M": 91}
PositionStatus = Literal["open", "closed", "all"]

ACTIVITY_HOURS = 24  # default window; config `tower.overview.activity_hours` (E8.8b)
ACTIVITY_MAX_HOURS = 168
# Rows returned after grouping: the window bounds the list, this bounds the payload.
ACTIVITY_LIMIT = 200
PROPOSAL_WINDOW = _dt.timedelta(hours=24)
# Rows read per activity source before the window filter + merge (newest by rowid). Large
# enough for a week of 5-min alerts per source; a busier source undercounts its group.
_PER_SOURCE = 500
# A reconciled day's equity is plotted at the close.
_CLOSE = _dt.time(16, 0)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AlertView(BaseModel):
    model_config = _STRICT

    kind: str
    key: str
    message: str
    opened_at: _dt.datetime | None


class StatusSection(BaseModel):
    """Full-width strip: trading state, scheduler health, alerts, D32 budget."""

    model_config = _STRICT

    halted: bool
    halt: HaltView | None = Field(default=None, description="Most recent active halt")
    active_halts: int = 0
    tick_at: _dt.datetime | None = None
    tick_status: str | None = None
    health_at: _dt.datetime | None = None
    health_status: str | None = None
    alerts: list[AlertView] = Field(default_factory=list)
    order_budget: OrderBudgetView | None = Field(
        default=None, description="Absent until the monitor heartbeat carries it (E6.5)"
    )


class EquityPoint(BaseModel):
    model_config = _STRICT

    t: _dt.datetime
    v: Decimal


class EquitySection(BaseModel):
    """Hero equity, its change over the selected range and the series to plot."""

    model_config = _STRICT

    range: OverviewRange
    value: Decimal | None = None
    value_at: _dt.datetime | None = None
    source: Literal["intraday", "reconciled"] | None = Field(
        default=None, description="intraday = latest monitor mark; reconciled = pnl_snapshots"
    )
    start_value: Decimal | None = Field(default=None, description="Equity at the range start")
    start_at: _dt.datetime | None = None
    start_label: str | None = Field(default=None, description="'prev close' or the start day")
    start_source: BaselineSource | None = Field(
        default=None,
        description="For 'prev close': arc_close (Arc's prior EOD mark) or broker_last_equity",
    )
    change: Decimal | None = None
    change_pct: float | None = Field(default=None, description="change / start_value (fraction)")
    series_source: Literal["intraday", "daily"] = "daily"
    series: list[EquityPoint] = Field(default_factory=list)
    benchmarks: list[BenchmarkSeries] = Field(
        default_factory=list, description="D87: SPY / QQQ over the same range (when known)"
    )


class BenchmarkSeries(BaseModel):
    """D87: a market benchmark (SPY / QQQ) over the Equity card's range, in its own prices.

    ``start_value`` is the benchmark's close on the day the portfolio range starts (the
    same day as ``EquitySection.start_at``), so ``change_pct`` compares like for like with
    the portfolio's ``change_pct``. The SPA rebases ``series`` onto the equity line.
    """

    model_config = _STRICT

    symbol: str
    start_value: Decimal
    value: Decimal
    value_at: _dt.datetime
    change_pct: float = Field(description="value / start_value − 1 (fraction)")
    series: list[EquityPoint] = Field(
        description="Benchmark prices at the equity series' timestamps (subset)"
    )


class AccountSplit(BaseModel):
    """D87: how much of the account is free cash vs held in open positions."""

    model_config = _STRICT

    at: _dt.datetime
    equity: Decimal
    cash: Decimal = Field(description="Broker cash: available to open new debit trades")
    in_positions: Decimal = Field(
        description="equity − cash: the open positions' marked value (locked up)"
    )
    cash_pct: float | None = Field(default=None, description="cash / equity (fraction)")
    options_buying_power: Decimal | None = Field(
        default=None, description="Broker options buying power (cash account: = cash)"
    )


GreekRisk = Literal["low", "med", "high"]


class GreekAdvisory(BaseModel):
    """D87: an uncapped Greek in dollars with an info-only Low / Med / High label.

    The label is display only (no gate, no halt): ``|value| / equity`` against the
    ``tower.overview.greek_advisory`` bands (``med_pct`` / ``high_pct``)."""

    model_config = _STRICT

    value: float | None = None
    pct_of_equity: float | None = Field(
        default=None, description="|value| / equity (Θ: decay paid only, max(−Θ, 0))"
    )
    risk: GreekRisk | None = Field(default=None, description="None = no value (rendered —)")
    med_pct: float
    high_pct: float


class DayPnlSection(BaseModel):
    """Day P&L (equity − start-of-day equity, D43), realized vs unrealized, MTD / YTD."""

    model_config = _STRICT

    as_of: _dt.datetime | None = None
    source: Literal["intraday", "reconciled"] | None = None
    day_pnl: Decimal | None = None
    day_pct: float | None = None
    prev_equity: Decimal | None = Field(
        default=None,
        description="Start-of-day equity: Arc's prior-session close (E5.9b, D43)",
    )
    prev_close_source: BaselineSource | None = Field(
        default=None,
        description="arc_close, or broker_last_equity when no Arc close exists (fallback)",
    )
    realized: Decimal | None = Field(default=None, description="Realized today (reconciled)")
    unrealized: Decimal | None = Field(default=None, description="Open legs' broker P&L")
    unrealized_at: _dt.datetime | None = None
    realized_at: _dt.datetime | None = None
    performance: Performance | None = None
    performance_day: _dt.date | None = Field(default=None, description="Day MTD/YTD refer to")


class PositionRow(BaseModel):
    """One structure: entry vs mark per share (+ debit / − credit), P&L from its legs."""

    model_config = _STRICT

    id: str
    ticker: str
    kind: str | None
    status: Literal["open", "closed"]
    legs: list[str] = Field(description="Compact OCC symbols")
    contracts: int
    entry_net: Decimal
    mark_net: Decimal | None = Field(default=None, description="Σ legs' broker mark, per share")
    unrealized_pl: Decimal | None = None
    unrealized_pct: float | None = Field(default=None, description="P&L / |entry| × 100 × n")
    day_change: Decimal | None = Field(default=None, description="$ change today (broker marks)")
    mark_at: _dt.datetime | None = None
    expiration: _dt.date | None = None
    dte: int | None = None
    opened_at: _dt.datetime | None = None
    closed_at: _dt.datetime | None = None
    close_net: Decimal | None = None
    realized_pl: Decimal | None = Field(default=None, description="Closed: −(entry+close)×100×n")
    exit_pending: bool = False
    exit_reason: str | None = None
    exit_proposal_hash: str | None = None
    held: bool | None = Field(default=None, description="Last reconcile found it at the broker")
    held_at: _dt.datetime | None = None
    max_loss: Decimal | None = Field(default=None, description="Structure max loss × contracts")
    open_proposal_hash: str
    direction: Direction | None = Field(
        default=None, description="D50: bullish / bearish / neutral from the opening legs"
    )
    # E13.14 (D56): the exit path (arc.tower.data_exits); None = no exit watch yet.
    exit_watch: ExitWatchView | None = None
    exit_case: ExitCaseView | None = None
    exit_review: ExitVerdictView | None = None
    mandatory_signal: str | None = Field(
        default=None, description="stop | dte_exit | expiry on the latest position_review"
    )


class GreeksSection(BaseModel):
    model_config = _STRICT

    greeks: GreeksView
    max_loss_by_underlying: dict[str, Decimal] = Field(default_factory=dict)
    per_underlying_cap: Decimal | None = Field(
        default=None, description="max_alloc_pct × equity (gate rule)"
    )
    max_alloc_pct: float
    theta: GreekAdvisory | None = Field(
        default=None, description="D87: Θ, $ per day (uncapped; advisory band only)"
    )
    gamma: GreekAdvisory | None = Field(
        default=None,
        description="D87: dollar gamma, $Δ change for a 1% move (Σ Γ × spot² / 100 per "
        "underlying; uncapped; advisory band only)",
    )


class ProposalRow(ProposalView):
    """A proposal with its net EV after costs (E2.4 managed model × contracts)."""

    net_ev: float | None = Field(default=None, description="Managed net EV × contracts, $")
    pop_managed: float | None = None


class MoverTile(BaseModel):
    model_config = _STRICT

    structure_id: str
    ticker: str
    kind: str | None
    direction: Direction | None = None
    open_proposal_hash: str
    unrealized_pct: float | None = None
    change_today: float | None = Field(
        default=None, description="Structure day change: legs' change_today weighted by value"
    )
    spark: list[float] = Field(default_factory=list, description="Unrealized $ over today's marks")
    at: _dt.datetime | None = None


ActivityKind = Literal["fill", "execution", "exit", "halt", "resume", "reconcile", "alert"]
ActivityTone = Literal["neutral", "neg", "warn", "pos"]


class ActivityEntry(BaseModel):
    """One event inside a grouped activity row."""

    model_config = _STRICT

    at: _dt.datetime
    tone: ActivityTone = "neutral"
    text: str


class ActivityItem(BaseModel):
    """One Recent Activity row. Repeats of one alert kind in the window collapse into a
    single row (``count`` > 1, newest ``at``, worst tone) with every event in ``entries``."""

    model_config = _STRICT

    at: _dt.datetime
    kind: ActivityKind
    tone: ActivityTone = "neutral"
    text: str
    ref: str | None = Field(default=None, description="proposal hash for a drill-down link")
    group: str | None = Field(
        default=None, description="alert kind the row groups (`missed_window`), if grouped"
    )
    count: int = Field(default=1, description="events in this row (> 1 when grouped)")
    entries: list[ActivityEntry] = Field(
        default_factory=list, description="each grouped event, newest first (empty if count 1)"
    )


class OverviewResponse(BaseModel):
    """``GET /api/overview``: every Overview card in one read."""

    model_config = _STRICT

    as_of: _dt.datetime
    range: OverviewRange
    stale_after_s: int = Field(description="Monitor marks older than this are stale")
    marks_at: _dt.datetime | None = Field(default=None, description="Latest monitor heartbeat")
    marks_stale: bool = Field(description="No monitor mark, or older than stale_after_s at as_of")
    status: StatusSection
    equity: EquitySection
    day_pnl: DayPnlSection
    account: AccountSplit | None = Field(default=None, description="D87: cash vs in positions")
    positions: list[PositionRow]
    greeks: GreeksSection
    proposals: list[ProposalRow]
    proposals_since: _dt.datetime
    movers: list[MoverTile]
    activity: list[ActivityItem]
    activity_hours: int = Field(description="Recent Activity window (rolling hours)")
    activity_since: _dt.datetime = Field(description="as_of − activity_hours")


class PositionsResponse(BaseModel):
    """``GET /api/positions``: open and/or closed structures."""

    model_config = _STRICT

    as_of: _dt.datetime
    status: PositionStatus
    stale_after_s: int
    marks_at: _dt.datetime | None = None
    items: list[PositionRow]
    exit_path: ExitPathStrip = Field(
        default_factory=lambda: ExitPathStrip.model_validate({"mode": "deterministic"}),
        description="E13.14 (D56): the Exit path strip",
    )


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------


class IntradayMark(BaseModel):
    """One monitor heartbeat with an equity mark."""

    model_config = _STRICT

    at: _dt.datetime
    equity: Decimal
    prev_close: Decimal | None = Field(
        default=None, description="Start-of-day equity for this mark (E5.9b, D43)"
    )
    prev_close_source: BaselineSource | None = None
    legs: list[LegView] = Field(default_factory=list)
    benchmarks: dict[str, Decimal] = Field(
        default_factory=dict, description="D87: benchmark price at this mark"
    )
    benchmark_prev_close: dict[str, Decimal] = Field(default_factory=dict)


def _mark(conn: sqlite3.Connection, row: sqlite3.Row) -> IntradayMark | None:
    at = parse_ts(row["at"])
    d = _json(row["detail"], {})
    eq = _dec(d.get("equity"))
    if at is None or eq is None:
        return None
    prev, source = prev_close_of(conn, d, at.date())
    bench: dict[str, Decimal] = {}
    bench_prev: dict[str, Decimal] = {}
    raw = d.get("benchmarks")
    for sym, b in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(b, dict):
            continue
        price, prior = _dec(b.get("price")), _dec(b.get("prev_close"))
        if price is not None and price > 0:
            bench[str(sym)] = price
        if prior is not None and prior > 0:
            bench_prev[str(sym)] = prior
    return IntradayMark(
        at=at,
        equity=eq,
        prev_close=prev,
        prev_close_source=source,
        legs=_legs(row),
        benchmarks=bench,
        benchmark_prev_close=bench_prev,
    )


def _day_bounds(day: _dt.date) -> tuple[str, str]:
    start = _dt.datetime.combine(day, _dt.time(0), tzinfo=ET)
    return to_db(start), to_db(start + _dt.timedelta(days=1))


def equity_intraday(conn: sqlite3.Connection, day: _dt.date) -> list[IntradayMark]:
    """Every ``monitor`` heartbeat on ET *day* that carries equity, oldest first.

    ``heartbeats.at`` is ``to_db`` UTC text, so the day window is a text range
    served by ``idx_heartbeats_component (component, at)``.
    """
    if not _has_table(conn, "heartbeats"):
        return []
    lo, hi = _day_bounds(day)
    rows = conn.execute(
        """SELECT * FROM heartbeats WHERE component = 'monitor' AND at >= ? AND at < ?
           ORDER BY at, rowid""",
        (lo, hi),
    ).fetchall()
    out: list[IntradayMark] = []
    for r in rows:
        mark = _mark(conn, r)
        if mark is not None:
            out.append(mark)
    return out


def range_start(rng: OverviewRange, today: _dt.date) -> _dt.date | None:
    """First day inside *rng* ending *today* (``None`` = all history; ``1D`` = today)."""
    if rng == "1D":
        return today
    if rng == "YTD":
        return today.replace(month=1, day=1)
    if rng == "ALL":
        return None
    return today - _dt.timedelta(days=_RANGE_DAYS[rng])


def _close_at(day: _dt.date) -> _dt.datetime:
    return _dt.datetime.combine(day, _CLOSE, tzinfo=ET)


def _pct(change: Decimal | None, base: Decimal | None) -> float | None:
    if change is None or base is None or base == 0:
        return None
    return float(change / base)


def _equity(
    conn: sqlite3.Connection,
    rng: OverviewRange,
    today: _dt.date,
    daily: list[DailyEquity],
    reconciled_at: _dt.datetime | None,
    marks: list[IntradayMark],
    latest: IntradayMark | None,
) -> EquitySection:
    # Hero: the latest intraday mark unless a reconcile is newer.
    value: Decimal | None = None
    value_at: _dt.datetime | None = None
    source: Literal["intraday", "reconciled"] | None = None
    if latest is not None and (reconciled_at is None or latest.at >= reconciled_at):
        value, value_at, source = latest.equity, latest.at, "intraday"
    elif daily:
        value, value_at, source = daily[-1].equity, reconciled_at, "reconciled"

    series: list[EquityPoint] = []
    start_value: Decimal | None = None
    start_at: _dt.datetime | None = None
    start_label: str | None = None
    start_source: BaselineSource | None = None
    series_source: Literal["intraday", "daily"] = "daily"

    if rng == "1D" and marks:
        series_source = "intraday"
        series = [EquityPoint(t=m.at, v=m.equity) for m in marks]
        prev = marks[-1].prev_close
        if prev is not None:
            start_value, start_label = prev, "prev close"
            start_source = marks[-1].prev_close_source
            before = [d for d in daily if d.day < marks[-1].at.date()]
            start_at = _close_at(before[-1].day) if before else None
        else:
            start_value, start_at, start_label = marks[0].equity, marks[0].at, "open"
    else:
        if rng == "1D":  # no marks today: the last reconciled day vs the one before it
            pts = daily[-2:]
            base = pts[0] if pts else None
        else:
            start = range_start(rng, today)
            inside = [d for d in daily if start is None or d.day >= start]
            before = [d for d in daily if start is not None and d.day < start]
            # The range starts at the last close before it (or the first day in it).
            base = before[-1] if before else (inside[0] if inside else None)
            pts = ([before[-1]] if before else []) + inside
        series = [EquityPoint(t=_close_at(d.day), v=d.equity) for d in pts]
        if base is not None:
            start_value, start_at = base.equity, _close_at(base.day)
            start_label = "prev close" if rng == "1D" else f"{base.day:%m-%d}"
            if rng == "1D":
                start_source = "arc_close"
        # Today's live mark extends the daily line past the last reconciled close.
        if latest is not None and source == "intraday" and (not pts or latest.at > series[-1].t):
            series.append(EquityPoint(t=latest.at, v=latest.equity))

    change = value - start_value if value is not None and start_value is not None else None
    benchmarks = _benchmarks(
        conn,
        rng,
        series,
        series_source,
        marks,
        latest if source == "intraday" else None,
        start_at,
        start_label,
    )
    return EquitySection(
        range=rng,
        value=value,
        value_at=value_at,
        source=source,
        start_value=start_value,
        start_at=start_at,
        start_label=start_label,
        start_source=start_source,
        change=change,
        change_pct=_pct(change, start_value),
        series_source=series_source,
        series=series,
        benchmarks=benchmarks,
    )


#: D87: the benchmarks drawn next to the portfolio (the monitor marks the same list).
BENCHMARKS = ("SPY", "QQQ")


def _daily_closes(conn: sqlite3.Connection, symbol: str) -> dict[_dt.date, Decimal]:
    """*symbol*'s daily closes: ``iv_daily`` ``last_close`` spots, filled from the last
    monitor mark of each day at or after 15:50 ET (D87)."""
    out: dict[_dt.date, Decimal] = {}
    if _has_table(conn, "heartbeats"):
        for r in conn.execute(
            """SELECT at, json_extract(detail, '$.benchmarks') AS b FROM heartbeats
               WHERE component = 'monitor' AND json_extract(detail, '$.benchmarks') IS NOT NULL
               ORDER BY at, rowid"""
        ).fetchall():
            at = parse_ts(r["at"])
            row = _json(r["b"], {}).get(symbol)
            if at is None or not isinstance(row, dict):
                continue
            et = at.astimezone(ET)
            price = _dec(row.get("price"))
            if price is not None and price > 0 and et.time() >= _dt.time(15, 50):
                out[et.date()] = price
            prior = _dec(row.get("prev_close"))
            if prior is not None and prior > 0:
                # the prior session's close: keyed on the latest day before *et* we know
                out.setdefault(_prev_session(et.date()), prior)
    if _has_table(conn, "iv_daily"):
        for r in conn.execute(
            """SELECT day, spot FROM iv_daily
               WHERE ticker = ? AND spot_basis = 'last_close' AND spot > 0""",
            (symbol,),
        ).fetchall():
            v = _dec(r["spot"])
            if v is not None:
                out[_dt.date.fromisoformat(str(r["day"])[:10])] = v
    return out


def _prev_session(day: _dt.date) -> _dt.date:
    from arc.utils.calendar import previous_session

    return previous_session(day)


def _benchmarks(
    conn: sqlite3.Connection,
    rng: OverviewRange,
    series: list[EquityPoint],
    series_source: Literal["intraday", "daily"],
    marks: list[IntradayMark],
    live: IntradayMark | None,
    start_at: _dt.datetime | None,
    start_label: str | None,
) -> list[BenchmarkSeries]:
    """D87: SPY / QQQ over the Equity card's range, starting where the portfolio starts.

    ``1D`` uses the monitor marks (start = the benchmark's prior close when the portfolio
    starts at prev close, else its first mark). Longer ranges use daily closes (start = the
    close on the portfolio's start day) plus today's live mark. A benchmark with no start
    price is left out, never guessed.
    """
    out: list[BenchmarkSeries] = []
    for sym in BENCHMARKS:
        pts: list[EquityPoint] = []
        start: Decimal | None = None
        if series_source == "intraday":
            pts = [EquityPoint(t=m.at, v=m.benchmarks[sym]) for m in marks if sym in m.benchmarks]
            if start_label == "prev close" and marks and sym in marks[-1].benchmark_prev_close:
                start = marks[-1].benchmark_prev_close[sym]
            elif pts:
                start = pts[0].v
        else:
            closes = _daily_closes(conn, sym)
            for p in series:
                day = p.t.astimezone(ET).date()
                if live is not None and p.t == live.at:
                    if sym in live.benchmarks:
                        pts.append(EquityPoint(t=p.t, v=live.benchmarks[sym]))
                elif day in closes:
                    pts.append(EquityPoint(t=p.t, v=closes[day]))
            if start_at is not None:
                start = closes.get(start_at.astimezone(ET).date())
        if start is None or start <= 0 or not pts:
            continue
        last = pts[-1]
        out.append(
            BenchmarkSeries(
                symbol=sym,
                start_value=start,
                value=last.v,
                value_at=last.t,
                change_pct=float(last.v / start - 1),
                series=pts,
            )
        )
    return out


# ---------------------------------------------------------------------------
# P&L today
# ---------------------------------------------------------------------------


def _day_pnl(
    conn: sqlite3.Connection,
    daily: list[DailyEquity],
    latest: IntradayMark | None,
    positions: list[PositionRow],
) -> DayPnlSection:
    snap = conn.execute(
        "SELECT * FROM pnl_snapshots ORDER BY snapshot_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    rec_at = parse_ts(snap["snapshot_at"]) if snap is not None else None
    details = _json(snap["details_json"], {}) if snap is not None else {}
    perf: Performance | None = None
    perf_day: _dt.date | None = None
    if daily:
        perf_day = daily[-1].day
        perf = performance_from(daily, perf_day)

    open_pl = [p.unrealized_pl for p in positions if p.unrealized_pl is not None]
    leg_pl = [leg.unrealized_pl for leg in (latest.legs if latest else [])]
    unrealized: Decimal | None = None
    if latest is not None and any(x is not None for x in leg_pl):
        unrealized = sum((x for x in leg_pl if x is not None), start=Decimal(0))
    elif open_pl:
        unrealized = sum(open_pl, start=Decimal(0))

    fields: dict[str, Any] = {
        "performance": perf,
        "performance_day": perf_day,
        "realized": _dec(snap["realized"]) if snap is not None else None,
        "realized_at": rec_at,
        "unrealized": unrealized if latest is not None else _dec(details.get("unrealized")),
        "unrealized_at": latest.at if latest is not None else rec_at,
    }
    if fields["unrealized"] is None and snap is not None:
        fields["unrealized"] = _dec(snap["unrealized"])
    if (
        latest is not None
        and latest.prev_close is not None
        and (rec_at is None or latest.at >= rec_at)
    ):
        day = latest.equity - latest.prev_close
        fields.update(
            as_of=latest.at,
            source="intraday",
            day_pnl=day,
            prev_equity=latest.prev_close,
            prev_close_source=latest.prev_close_source,
            day_pct=_pct(day, latest.prev_close),
        )
    elif snap is not None:
        eq = _dec(details.get("equity"))
        prev, prev_source = (None, None)
        snap_day = details.get("day")
        on = _dt.date.fromisoformat(snap_day) if snap_day else (rec_at.date() if rec_at else None)
        if on is not None:
            prev, prev_source = prev_close_of(conn, details, on)
        day_pnl = eq - prev if eq is not None and prev is not None else None
        fields.update(
            as_of=rec_at,
            source="reconciled",
            day_pnl=day_pnl,
            prev_equity=prev,
            prev_close_source=prev_source,
            day_pct=_pct(day_pnl, prev),
        )
    return DayPnlSection(**fields)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def _occ_key(symbol: str) -> str:
    from arc.structures import parse_occ

    try:
        return parse_occ(symbol).format()
    except ValueError:
        return symbol


def _lastday(leg: LegView) -> Decimal | None:
    """The leg's prior-close mark: broker ``lastday_price``, else from ``change_today``."""
    if leg.lastday_price is not None:
        return leg.lastday_price
    if leg.current_price is not None and leg.change_today is not None:
        denom = 1 + leg.change_today
        return leg.current_price / denom if denom else None
    return None


def _held(conn: sqlite3.Connection) -> tuple[dict[str, bool], _dt.datetime | None]:
    if not _has_table(conn, "positions_snapshots"):
        return {}, None
    snap = conn.execute(
        "SELECT positions_json, snapshot_at FROM positions_snapshots "
        "ORDER BY snapshot_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if snap is None:
        return {}, None
    held = {
        str(s.get("structure_id")): bool(s.get("held"))
        for s in _json(snap["positions_json"], {}).get("structures", [])
    }
    return held, parse_ts(snap["snapshot_at"])


def _position_rows(
    conn: sqlite3.Connection,
    latest: IntradayMark | None,
    today: _dt.date,
    status: PositionStatus,
) -> list[PositionRow]:
    if not _has_table(conn, "open_structures"):
        return []
    from arc.structures import parse_occ

    where = "" if status == "all" else "WHERE status = ?"
    params: tuple[str, ...] = () if status == "all" else (status,)
    rows = conn.execute(
        f"SELECT * FROM open_structures {where} ORDER BY opened_at DESC, rowid DESC",  # noqa: S608
        params,
    ).fetchall()
    held, held_at = _held(conn)
    legs = {_occ_key(leg.symbol): leg for leg in (latest.legs if latest else [])}

    # A leg shared by two open structures cannot be split by symbol: leave P&L blank.
    open_rows = conn.execute(
        "SELECT structure_json FROM open_structures WHERE status = 'open'"
    ).fetchall()
    uses: dict[str, int] = {}
    for r in open_rows:
        for leg in _json(r["structure_json"], {}).get("legs", []):
            key = _occ_key(str(leg.get("occ_symbol", "")))
            uses[key] = uses.get(key, 0) + 1

    out: list[PositionRow] = []
    for r in rows:
        st = _json(r["structure_json"], {})
        n = int(r["contracts"])
        entry = _dec(r["entry_net"]) or Decimal(0)
        syms: list[str] = []
        sides: list[tuple[str, int, int]] = []  # (occ, +1 long / -1 short, ratio)
        exps: list[_dt.date] = []
        for leg in st.get("legs", []):
            try:
                occ = parse_occ(str(leg["occ_symbol"]))
            except (KeyError, ValueError):
                continue
            key = occ.format()
            syms.append(key)
            exps.append(occ.expiration)
            sign = 1 if str(leg.get("side", "long")) == "long" else -1
            sides.append((key, sign, int(leg.get("ratio", 1) or 1)))
        max_loss_unit = _dec(st.get("max_loss"))
        is_open = r["status"] == "open"

        mark: Decimal | None = None
        pl: Decimal | None = None
        day: Decimal | None = None
        if is_open and syms and all(uses.get(s, 0) == 1 and s in legs for s in syms):
            marked = [legs[s] for s in syms]
            if all(leg.current_price is not None for leg in marked):
                mark = sum(
                    (
                        sign * ratio * (legs[s].current_price or Decimal(0))
                        for s, sign, ratio in sides
                    ),
                    start=Decimal(0),
                )
            if all(leg.unrealized_pl is not None for leg in marked):
                pl = sum((leg.unrealized_pl or Decimal(0) for leg in marked), start=Decimal(0))
            prev = [_lastday(leg) for leg in marked]
            if all(leg.current_price is not None for leg in marked) and all(
                x is not None for x in prev
            ):
                day = sum(
                    (
                        ((leg.current_price or Decimal(0)) - (p or Decimal(0))) * leg.qty * 100
                        for leg, p in zip(marked, prev, strict=True)
                    ),
                    start=Decimal(0),
                )
            # D87: a structure opened on the mark's day has no prior close of its own; the
            # broker's change_today is vs the contract's prior close (before we held it), so
            # its day change is the change since entry.
            opened = parse_ts(r["opened_at"])
            if (
                pl is not None
                and opened is not None
                and latest is not None
                and opened.astimezone(ET).date() == latest.at.astimezone(ET).date()
            ):
                day = pl
        close_net = _dec(r["close_net"])
        realized = -(entry + close_net) * 100 * n if close_net is not None else None
        basis = abs(entry) * 100 * n
        expiration = min(exps) if exps else None
        out.append(
            PositionRow(
                id=r["id"],
                ticker=r["ticker"],
                kind=st.get("kind"),
                status=r["status"],
                legs=syms,
                contracts=n,
                entry_net=entry,
                mark_net=mark,
                unrealized_pl=pl,
                unrealized_pct=_pct(pl, basis),
                day_change=day,
                mark_at=latest.at if latest is not None and pl is not None else None,
                expiration=expiration,
                dte=(expiration - today).days if expiration and is_open else None,
                opened_at=parse_ts(r["opened_at"]),
                closed_at=parse_ts(r["closed_at"]),
                close_net=close_net,
                realized_pl=realized,
                exit_pending=is_open and r["exit_proposal_hash"] is not None,
                exit_reason=r["exit_reason"],
                exit_proposal_hash=r["exit_proposal_hash"],
                held=held.get(r["id"]) if is_open else None,
                held_at=held_at if is_open and r["id"] in held else None,
                max_loss=max_loss_unit * n if max_loss_unit is not None else None,
                open_proposal_hash=r["open_proposal_hash"],
                direction=direction_of(st.get("legs")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Proposals, movers, activity, status
# ---------------------------------------------------------------------------


def _analytics(conn: sqlite3.Connection, hashes: list[str]) -> dict[str, dict[str, Any]]:
    if not hashes or not _has_table(conn, "market_contexts"):
        return {}
    marks = ",".join("?" * len(hashes))
    rows = conn.execute(
        f"""SELECT proposal_hash, payload FROM market_contexts
            WHERE proposal_hash IN ({marks}) ORDER BY created_at, rowid""",  # noqa: S608
        hashes,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:  # oldest first: the latest context per proposal wins
        a = _json(r["payload"], {}).get("analytics")
        if isinstance(a, dict):
            out[r["proposal_hash"]] = a
    return out


def _proposal_rows(conn: sqlite3.Connection, now: _dt.datetime) -> list[ProposalRow]:
    since = now - PROPOSAL_WINDOW
    views = [
        p
        for p in _proposals(conn, since.date().isoformat(), 500)
        if p.created_at is not None and p.created_at >= since
    ]
    analytics = _analytics(conn, [p.proposal_hash for p in views])
    out: list[ProposalRow] = []
    for p in views:
        managed = ((analytics.get(p.proposal_hash) or {}).get("exit_model") or {}).get("managed")
        managed = managed or {}
        n = p.contracts or 1
        net = managed.get("net_ev")
        out.append(
            ProposalRow(
                **p.model_dump(),
                net_ev=None if net is None else float(net) * n,
                pop_managed=managed.get("pop"),
            )
        )
    return out


def _movers(positions: list[PositionRow], marks: list[IntradayMark]) -> list[MoverTile]:
    out: list[MoverTile] = []
    for p in positions:
        if p.status != "open":
            continue
        spark: list[float] = []
        for m in marks:
            by = {_occ_key(leg.symbol): leg for leg in m.legs}
            vals = [by[s].unrealized_pl for s in p.legs if s in by]
            if p.legs and len(vals) == len(p.legs) and all(v is not None for v in vals):
                spark.append(float(sum((v or Decimal(0) for v in vals), start=Decimal(0))))
        change: float | None = None
        if p.day_change is not None and p.mark_net is not None:
            prev_value = p.mark_net * 100 * p.contracts - p.day_change
            change = float(p.day_change / abs(prev_value)) if prev_value else None
        out.append(
            MoverTile(
                structure_id=p.id,
                ticker=p.ticker,
                kind=p.kind,
                direction=p.direction,
                open_proposal_hash=p.open_proposal_hash,
                unrealized_pct=p.unrealized_pct,
                change_today=change,
                spark=spark,
                at=p.mark_at,
            )
        )
    return out


def _recent(conn: sqlite3.Connection, sql: str, order: str = "rowid") -> list[sqlite3.Row]:
    return conn.execute(f"{sql} ORDER BY {order} DESC LIMIT {_PER_SOURCE}").fetchall()


def _activity(conn: sqlite3.Connection, since: _dt.datetime) -> list[ActivityItem]:  # noqa: C901 - one branch per source
    """Every event at or after *since*, newest first, alert repeats grouped by kind."""
    items: list[ActivityItem] = []
    alert_kind: dict[int, str] = {}  # id(item) -> alert group key

    def add(at: _dt.datetime | None, group: str | None = None, **kw: Any) -> None:
        if at is not None and at >= since:
            item = ActivityItem(at=at, **kw)
            items.append(item)
            if group is not None:
                alert_kind[id(item)] = group

    if _has_table(conn, "fills") and _has_table(conn, "orders"):
        for r in _recent(
            conn,
            """SELECT f.qty, f.price, f.filled_at, o.proposal_hash, p.ticker, p.kind
               FROM fills f JOIN orders o ON o.id = f.order_id
               LEFT JOIN proposals p ON p.proposal_hash = o.proposal_hash""",
            "f.rowid",
        ):
            add(
                parse_ts(r["filled_at"]),
                kind="fill",
                text=(
                    f"Filled {r['qty']} × {r['ticker'] or '?'} @ {r['price']} "
                    f"({r['kind'] or 'open'})"
                ),
                ref=r["proposal_hash"],
            )
    if _has_table(conn, "executions"):
        for r in _recent(
            conn,
            """SELECT x.proposal_hash, x.kind, x.status, x.attempts, x.contracts, x.filled_qty,
                      x.started_at, x.finished_at, p.ticker
               FROM executions x LEFT JOIN proposals p ON p.proposal_hash = x.proposal_hash
               WHERE x.status != 'filled'""",
            "x.rowid",
        ):
            bad = r["status"] in ("rejected", "unconfirmed")
            add(
                parse_ts(r["finished_at"] or r["started_at"]),
                kind="execution",
                tone="warn" if bad else "neutral",
                text=(
                    f"Execution {r['status'].replace('_', ' ')}: {r['ticker'] or '?'} "
                    f"{r['kind']} ({r['filled_qty']}/{r['contracts']} filled, "
                    f"{r['attempts']} attempt(s))"
                ),
                ref=r["proposal_hash"],
            )
    for r in _recent(
        conn,
        """SELECT p.proposal_hash, p.ticker, p.created_at, s.exit_reason
           FROM proposals p LEFT JOIN open_structures s ON s.exit_proposal_hash = p.proposal_hash
           WHERE p.kind = 'close'""",
        "p.rowid",
    ):
        reason = f" ({r['exit_reason']})" if r["exit_reason"] else ""
        add(
            parse_ts(r["created_at"]),
            kind="exit",
            text=f"Exit proposed: {r['ticker'] or '?'}{reason}",
            ref=r["proposal_hash"],
        )
    for r in _recent(conn, "SELECT * FROM halts"):
        add(
            parse_ts(r["at"]),
            kind="halt",
            tone="neg",
            text=f"Trading halted by {r['actor']}: {r['reason']}",
        )
        if r["cleared_at"]:
            add(
                parse_ts(r["cleared_at"]),
                kind="resume",
                text=f"Halt cleared by {r['cleared_by'] or '?'}",
            )
    for r in _recent(conn, "SELECT snapshot_at, details_json FROM pnl_snapshots"):
        d = _json(r["details_json"], {})
        clean = d.get("clean")
        if clean is None:
            continue
        add(
            parse_ts(r["snapshot_at"]),
            kind="reconcile",
            tone="neutral" if clean else "neg",
            text=f"Reconcile {d.get('day', '')}: " + ("clean" if clean else "MISMATCH"),
        )
    if _has_table(conn, "ops_alerts"):
        for r in _recent(conn, "SELECT kind, message, opened_at, resolved_at FROM ops_alerts"):
            one_off = r["resolved_at"] is not None and r["resolved_at"] == r["opened_at"]
            add(
                parse_ts(r["opened_at"]),
                group=r["kind"],
                kind="alert",
                tone="neutral" if one_off else "warn",
                text=f"Alert {r['kind']}: {r['message']}",
            )
            if r["resolved_at"] and not one_off:
                add(
                    parse_ts(r["resolved_at"]),
                    group=f"{r['kind']} resolved",
                    kind="alert",
                    text=f"Alert {r['kind']} resolved",
                )

    return _group_alerts(items, alert_kind)[:ACTIVITY_LIMIT]


_TONE_RANK: dict[str, int] = {"neutral": 0, "pos": 1, "warn": 2, "neg": 3}


def _group_alerts(items: list[ActivityItem], groups: dict[int, str]) -> list[ActivityItem]:
    """Newest first; repeats of one alert kind collapse into one row ``<kind> ×n``."""
    by_group: dict[str, list[ActivityItem]] = {}
    for it in items:
        g = groups.get(id(it))
        if g is not None:
            by_group.setdefault(g, []).append(it)
    out: list[ActivityItem] = [it for it in items if groups.get(id(it)) is None]
    for g, members in by_group.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        members.sort(key=lambda i: i.at, reverse=True)
        out.append(
            ActivityItem(
                at=members[0].at,
                kind="alert",
                tone=max((m.tone for m in members), key=_TONE_RANK.__getitem__),
                text=f"{g} ×{len(members)}",
                group=g,
                count=len(members),
                entries=[ActivityEntry(at=m.at, tone=m.tone, text=m.text) for m in members],
            )
        )
    out.sort(key=lambda i: i.at, reverse=True)
    return out


def _status(conn: sqlite3.Connection, monitor: sqlite3.Row | None) -> StatusSection:
    active = [h for h in _halts(conn, 50) if h.active]
    epoch = _dt.datetime.min.replace(tzinfo=_dt.UTC)
    active.sort(key=lambda h: h.at or epoch, reverse=True)
    tick = _latest_heartbeat(conn, "tick")
    health = _latest_heartbeat(conn, "health")
    alerts: list[AlertView] = []
    if _has_table(conn, "ops_alerts"):
        alerts = [
            AlertView(
                kind=r["kind"],
                key=r["key"],
                message=r["message"],
                opened_at=parse_ts(r["opened_at"]),
            )
            for r in conn.execute(
                "SELECT kind, key, message, opened_at FROM ops_alerts WHERE resolved_at IS NULL "
                "ORDER BY opened_at DESC"
            ).fetchall()
        ]
    return StatusSection(
        halted=bool(active),
        halt=active[0] if active else None,
        active_halts=len(active),
        tick_at=parse_ts(tick["at"]) if tick else None,
        tick_status=tick["status"] if tick else None,
        health_at=parse_ts(health["at"]) if health else None,
        health_status=health["status"] if health else None,
        alerts=alerts,
        order_budget=_order_budget(monitor),
    )


def _greeks_section(
    monitor: sqlite3.Row | None,
    positions: list[PositionRow],
    *,
    dollar_delta_cap_pct: float,
    vega_cap_pct: float,
    max_alloc_pct: float,
    stale_after: _dt.timedelta,
    beta_delta_cap_pct: float = 2.00,
    advisory: GreekAdvisorySettings | None = None,
) -> GreeksSection:
    advisory = advisory or GreekAdvisorySettings()
    g = _greeks(
        monitor,
        dollar_delta_cap_pct,
        vega_cap_pct,
        stale_after,
        beta_delta_cap_pct=beta_delta_cap_pct,
    )
    by: dict[str, Decimal] = {}
    for p in positions:
        if p.status == "open" and p.max_loss is not None:
            by[p.ticker] = by.get(p.ticker, Decimal(0)) + p.max_loss
    cap = Decimal(str(max_alloc_pct)) * Decimal(str(g.equity)) if g.equity is not None else None
    return GreeksSection(
        greeks=g,
        max_loss_by_underlying=dict(sorted(by.items(), key=lambda kv: kv[1], reverse=True)),
        per_underlying_cap=cap,
        max_alloc_pct=max_alloc_pct,
        theta=_advisory(g.theta, g.equity, advisory.theta, cost_only=True),
        gamma=_advisory(_dollar_gamma(monitor), g.equity, advisory.gamma),
    )


def _dollar_gamma(monitor: sqlite3.Row | None) -> float | None:
    """D87: Σ over underlyings of Γ (share-eq) × spot² / 100: how many dollars the book's
    dollar delta moves when every underlying moves 1%. ``None`` on a heartbeat written
    before D87 (no per-root Γ / spot), never a guess from the net Γ."""
    if monitor is None:
        return None
    rows = (_json(monitor["detail"], {}).get("delta_by_underlying") or {}).values()
    total = 0.0
    seen = False
    for r in rows:
        if not isinstance(r, dict) or r.get("gamma") is None or not r.get("spot"):
            return None
        total += float(r["gamma"]) * float(r["spot"]) ** 2 / 100.0
        seen = True
    return total if seen else (0.0 if not rows else None)


def _advisory(
    value: float | None, equity: float | None, band: AdvisoryBand, *, cost_only: bool = False
) -> GreekAdvisory:
    """D87: Low / Med / High from |value| / equity against *band* (info only).

    *cost_only* (Θ): only a negative value (time decay paid) counts; collecting Θ is Low."""
    size = None if value is None else (max(-value, 0.0) if cost_only else abs(value))
    pct = None if size is None or not equity else size / float(equity)
    risk: GreekRisk | None = None
    if pct is not None:
        risk = "high" if pct >= band.high_pct else "med" if pct >= band.med_pct else "low"
    return GreekAdvisory(
        value=value, pct_of_equity=pct, risk=risk, med_pct=band.med_pct, high_pct=band.high_pct
    )


def _account(monitor: sqlite3.Row | None) -> AccountSplit | None:
    """D87: cash (available) vs equity − cash (held in open positions), latest monitor."""
    if monitor is None:
        return None
    d = _json(monitor["detail"], {})
    eq, cash, at = _dec(d.get("equity")), _dec(d.get("cash")), parse_ts(monitor["at"])
    if eq is None or cash is None or at is None:
        return None
    return AccountSplit(
        at=at,
        equity=eq,
        cash=cash,
        in_positions=eq - cash,
        cash_pct=float(cash / eq) if eq else None,
        options_buying_power=_dec(d.get("options_buying_power")),
    )


def _latest_mark(conn: sqlite3.Connection, monitor: sqlite3.Row | None) -> IntradayMark | None:
    return None if monitor is None else _mark(conn, monitor)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def load_positions(
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    status: PositionStatus = "open",
    stale_after: _dt.timedelta,
    exit_mode: ExitPathMode = "research",
) -> PositionsResponse:
    """Structures by *status* with the latest monitor marks (SELECT only).

    E13.14: open rows carry the exit path (watch / case / Risk verdict / mandatory
    signal) and the response the Exit path strip (*exit_mode*: ``research`` since E13.15).
    """
    now_et = now.astimezone(ET)
    latest = _latest_mark(conn, _latest_heartbeat(conn, "monitor"))
    items = _position_rows(conn, latest, now_et.date(), status)
    strip, views = load_exit_path(
        conn, [r.id for r in items if r.status == "open"], mode=exit_mode, now=now_et
    )
    items = [r.model_copy(update=dict(views[r.id])) if r.id in views else r for r in items]
    return PositionsResponse(
        as_of=now_et,
        status=status,
        stale_after_s=int(stale_after.total_seconds()),
        marks_at=latest.at if latest else None,
        items=items,
        exit_path=strip,
    )


def load_overview(
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    rng: OverviewRange = "1D",
    dollar_delta_cap_pct: float = 1.00,
    vega_cap_pct: float = 0.010,
    max_alloc_pct: float = 0.05,
    stale_after: _dt.timedelta,
    activity_hours: int = ACTIVITY_HOURS,
    beta_delta_cap_pct: float = 2.00,
    greek_advisory: GreekAdvisorySettings | None = None,
) -> OverviewResponse:
    """Every Overview section read in one pass as of *now* (SELECT only).

    Recent Activity covers the rolling *activity_hours* before *now* (1–168).
    """
    if not 1 <= activity_hours <= ACTIVITY_MAX_HOURS:
        msg = f"activity_hours must be 1..{ACTIVITY_MAX_HOURS}, got {activity_hours}"
        raise ValueError(msg)
    now_et = now.astimezone(ET)
    activity_since = now_et - _dt.timedelta(hours=activity_hours)
    today = now_et.date()
    monitor = _latest_heartbeat(conn, "monitor")
    latest = _latest_mark(conn, monitor)
    marks = equity_intraday(conn, latest.at.date()) if latest is not None else []
    daily = daily_equity(conn)
    rec = conn.execute(
        "SELECT snapshot_at FROM pnl_snapshots ORDER BY snapshot_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    reconciled_at = parse_ts(rec["snapshot_at"]) if rec is not None else None
    positions = _position_rows(conn, latest, today, "open")
    return OverviewResponse(
        as_of=now_et,
        range=rng,
        stale_after_s=int(stale_after.total_seconds()),
        marks_at=latest.at if latest else None,
        marks_stale=latest is None or now_et - latest.at > stale_after,
        status=_status(conn, monitor),
        equity=_equity(conn, rng, today, daily, reconciled_at, marks, latest),
        day_pnl=_day_pnl(conn, daily, latest, positions),
        account=_account(monitor),
        positions=positions,
        greeks=_greeks_section(
            monitor,
            positions,
            dollar_delta_cap_pct=dollar_delta_cap_pct,
            vega_cap_pct=vega_cap_pct,
            max_alloc_pct=max_alloc_pct,
            stale_after=stale_after,
            beta_delta_cap_pct=beta_delta_cap_pct,
            advisory=greek_advisory,
        ),
        proposals=_proposal_rows(conn, now_et),
        proposals_since=now_et - PROPOSAL_WINDOW,
        movers=_movers(positions, marks),
        activity=_activity(conn, activity_since),
        activity_hours=activity_hours,
        activity_since=activity_since,
    )
