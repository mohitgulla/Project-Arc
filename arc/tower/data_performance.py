"""Performance page loader for control tower v2 (E8.7c, D35).

Every number comes from the functions the weekly scorecard (E7.3) and the reconciler use,
so the tower and the scorecard can never disagree:

==========================  =========================================================
Card                        Source
==========================  =========================================================
Net P&L                     daily closing equity (:func:`~arc.reconcile.performance.
                            daily_equity`): realised + unrealised change per ET day;
                            shadow overlay adds Σ(D19 shadow − realised) of early exits
Equity curve                the same series; :func:`~arc.reconcile.performance.drawdown`,
                            :func:`~arc.reconcile.performance.sharpe`,
                            :func:`~arc.reconcile.performance.period_return`
Costs                       :func:`arc.journal.scorecard.execution_costs` (fill vs mid,
                            the stored ``ProposalAnalytics`` slippage + fee model)
Win / loss                  :func:`arc.journal.tradestats.trade_stats` over
                            :func:`arc.journal.scorecard.closed_positions`
Modelled vs realised        :func:`arc.journal.scorecard.model_vs_realised` (D23)
Breakdowns                  :func:`arc.journal.tradestats.breakdown`
Persona calibration         :func:`arc.journal.scorecard.calibration_points` +
                            :func:`arc.journal.attribution.calibration` (E7.4), all
                            closed trades up to the end of the period, as in the scorecard
Gate & funnel               :func:`arc.journal.scorecard.funnel`
==========================  =========================================================

Periods are ET days ``[first, last]``; the scorecard's half-open ``[start, end)`` is
``[first 00:00 ET, last + 1 day 00:00 ET)``. The page's figures run to today; a period
that ends later (this week, MTD, QTD, YTD) still shows its future bar slots, empty.

Paper test legs (``include_tests=false``, the default): a structure whose open or close
proposal has a broker order with an ``arc-<hex>`` client id (the broker smoke test, not
an Arc ladder attempt ``arc2.<token>.s<k>``) is left out of every trade-based card, and its
realised P&L is taken out of the equity-based Net P&L on the day it closed.
"""

from __future__ import annotations

import calendar
import datetime as _dt
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.journal import legacy
from arc.journal.attribution import calibration
from arc.journal.reasons import JournalPersona, reason_label
from arc.journal.scorecard import (
    calibration_points,
    closed_positions,
    execution_costs,
    funnel,
    model_vs_realised,
)
from arc.journal.tradestats import (
    BreakdownRow,
    TradeStats,
    breakdown,
    hold_hit_rate,
    trade_stats,
)
from arc.reconcile.performance import (
    DailyEquity,
    daily_equity,
    daily_returns,
    drawdown,
    period_return,
    sharpe,
    sortino,
)
from arc.routines.config import TIMELINE_PERSONAS
from arc.tower.data import _has_table, _json
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from arc.journal.scorecard import ClosedPosition, SlippageRow

__all__ = [
    "BREAKDOWNS",
    "BreakdownBy",
    "BreakdownResponse",
    "Compare",
    "PerformanceResponse",
    "Preset",
    "PeriodError",
    "load_breakdown",
    "load_performance",
    "smoke_test_hashes",
    "resolve_period",
]

# ``1d`` / ``7d`` back the page range selector's 1D / 1W (E8.8c); 1M = ``30d``, 3M = ``90d``.
Preset = Literal["1d", "7d", "week", "mtd", "qtd", "ytd", "30d", "90d", "all", "custom"]
Compare = Literal["prev", "yoy", "none"]
BreakdownBy = Literal["ticker", "structure", "exit_reason", "reason_code", "profile", "regime"]
BREAKDOWNS: tuple[BreakdownBy, ...] = (
    "ticker",
    "structure",
    "exit_reason",
    "reason_code",
    "profile",
    "regime",
)
Bucket = Literal["day", "week", "month"]

DAILY_BARS_MAX_DAYS = 45
"""Longer periods show one bar per week (≤ 400 days) or per month."""
WEEKLY_BARS_MAX_DAYS = 400
_TEST_ORDER_PREFIX = "arc-"
# Personas whose reason codes the breakdown groups by (not gate/system/owner bookkeeping).
# E13.14: the current keys come from the persona catalogue (TIMELINE_PERSONAS, minus the
# deterministic monitor); the stored pre-rename names that read as one of them come from
# arc.journal.legacy (D54/D56: "sweep" = Scalp, "director" = Research, "investor" /
# "auditor" = Broker/Quant). "scout" before D54 is the Scalp, after it the Scout.
_PERSONAS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *(p for p in TIMELINE_PERSONAS if p != "monitor"),
            *(
                hop.old
                for hop in legacy.RENAME_CHAIN
                if not hop.job_only and "." not in hop.old and "." not in hop.new
            ),
            *(  # auditor -> Broker (reconcile): a persona hop onto a job name
                hop.old
                for hop in legacy.RENAME_CHAIN
                if hop.persona and hop.old in {p.value for p in JournalPersona}
            ),
        ]
    )
)

_FORBID = ConfigDict(extra="forbid", frozen=True)


class PeriodError(ValueError):
    """An unusable period (custom without dates, from after to)."""


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


class Period(BaseModel):
    model_config = _FORBID

    first: _dt.date
    last: _dt.date = Field(description="Last day with figures (≤ today)")
    slot_end: _dt.date = Field(description="Last bar slot (the period's calendar end)")
    days: int = Field(description="Calendar days first..last, inclusive")


def _quarter_start(d: _dt.date) -> _dt.date:
    return d.replace(month=3 * ((d.month - 1) // 3) + 1, day=1)


def _month_end(d: _dt.date) -> _dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _period(first: _dt.date, last: _dt.date, slot_end: _dt.date | None = None) -> Period:
    return Period(
        first=first, last=last, slot_end=max(slot_end or last, last), days=(last - first).days + 1
    )


def resolve_period(
    preset: Preset,
    today: _dt.date,
    *,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
    inception: _dt.date | None = None,
) -> Period:
    """The ET days a *preset* covers as of *today* (``all`` starts at *inception*)."""
    if preset == "week":
        first = today - _dt.timedelta(days=today.weekday())
        return _period(first, today, first + _dt.timedelta(days=6))
    if preset == "mtd":
        return _period(today.replace(day=1), today, _month_end(today))
    if preset == "qtd":
        q0 = _quarter_start(today)
        q_end = _month_end(q0.replace(month=q0.month + 2))
        return _period(q0, today, q_end)
    if preset == "ytd":
        return _period(today.replace(month=1, day=1), today, today.replace(month=12, day=31))
    if preset in ("1d", "7d", "30d", "90d"):
        return _period(today - _dt.timedelta(days=int(preset[:-1]) - 1), today)
    if preset == "all":
        return _period(min(inception or today, today), today)
    if date_from is None:
        msg = "a custom period needs date_from"
        raise PeriodError(msg)
    to = date_to or today
    if to < date_from:
        msg = f"date_from {date_from} is after date_to {to}"
        raise PeriodError(msg)
    return _period(date_from, min(to, today), to)


def _year_back(d: _dt.date) -> _dt.date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - 1, day=28)


def comparison_period(p: Period, compare: Compare) -> Period | None:
    """The same-length prior range (``prev``) or the same dates a year back (``yoy``)."""
    if compare == "none":
        return None
    if compare == "yoy":
        return _period(_year_back(p.first), _year_back(p.last))
    last = p.first - _dt.timedelta(days=1)
    return _period(last - _dt.timedelta(days=p.days - 1), last)


def _bounds(p: Period) -> tuple[_dt.datetime, _dt.datetime]:
    start = _dt.datetime.combine(p.first, _dt.time(), tzinfo=ET)
    end = _dt.datetime.combine(p.last + _dt.timedelta(days=1), _dt.time(), tzinfo=ET)
    return start, end


def bucket_for(days: int) -> Bucket:
    if days <= DAILY_BARS_MAX_DAYS:
        return "day"
    return "week" if days <= WEEKLY_BARS_MAX_DAYS else "month"


def _bucket_start(d: _dt.date, bucket: Bucket) -> _dt.date:
    if bucket == "week":
        return d - _dt.timedelta(days=d.weekday())
    if bucket == "month":
        return d.replace(day=1)
    return d


def _slots(p: Period, bucket: Bucket) -> list[tuple[_dt.date, _dt.date]]:
    """``[(start, end)]`` bar slots covering first..slot_end (clipped to the period)."""
    out: list[tuple[_dt.date, _dt.date]] = []
    d = p.first
    while d <= p.slot_end:
        s = _bucket_start(d, bucket)
        if bucket == "day":
            e = d
        elif bucket == "week":
            e = s + _dt.timedelta(days=6)
        else:
            e = _month_end(s)
        e = min(e, p.slot_end)
        out.append((d, e))
        d = e + _dt.timedelta(days=1)
    return out


def _label(start: _dt.date, bucket: Bucket) -> str:
    if bucket == "month":
        return start.strftime("%b %y")
    return f"{start:%b} {start.day}"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PeriodView(BaseModel):
    model_config = _FORBID

    first: _dt.date
    last: _dt.date
    slot_end: _dt.date
    days: int


class PnlBar(BaseModel):
    model_config = _FORBID

    start: _dt.date
    end: _dt.date
    label: str
    future: bool = Field(description="Starts after today: an empty slot")
    pnl: float | None = Field(description="Σ daily equity change; None = no close in the slot")
    cumulative: float | None
    shadow_cumulative: float | None = Field(
        description="cumulative + Σ(D19 hold-to-expiry shadow − realised) of trades closed so far"
    )


class NetPnlCard(BaseModel):
    model_config = _FORBID

    empty: bool
    source: Literal["equity", "realised", "none"] = Field(
        description="equity = daily closes (realised + unrealised); realised = no equity "
        "history, closed trades only"
    )
    net: float | None = None
    realised: float = Field(default=0.0, description="Σ realised of trades closed in the period")
    unrealised_change: float | None = Field(default=None, description="net − realised")
    compare_net: float | None = None
    change: float | None = Field(default=None, description="net − compare_net, $")
    bucket: Bucket
    bars: list[PnlBar] = Field(default_factory=list)
    now_label: str | None = Field(default=None, description="Label of the slot holding today")
    shadow_known: int = Field(default=0, description="Closed trades with a known D19 shadow")
    shadow_delta: float | None = Field(default=None, description="Σ(shadow − realised) over them")
    tests_excluded_pnl: float = Field(default=0.0, description="Test-leg realised taken out, $")


class EquityPoint(BaseModel):
    model_config = _FORBID

    day: _dt.date
    equity: float


class EquityCard(BaseModel):
    model_config = _FORBID

    empty: bool
    points: list[EquityPoint] = Field(default_factory=list)
    start_equity: float | None = None
    end_equity: float | None = None
    return_pct: float | None = None
    max_drawdown: float | None = Field(default=None, description="$ (≤ 0)")
    max_drawdown_pct: float | None = None
    drawdown_peak: _dt.date | None = None
    drawdown_trough: _dt.date | None = None
    drawdown_recovered: _dt.date | None = None
    sharpe: float | None = None
    sortino: float | None = None
    returns: int = Field(default=0, description="Daily returns behind the Sharpe and Sortino")


class CostBar(BaseModel):
    model_config = _FORBID

    start: _dt.date
    end: _dt.date
    label: str
    commission: float = 0.0
    fees: float = 0.0
    spread: float = 0.0
    slippage: float = 0.0


class CostsCard(BaseModel):
    model_config = _FORBID

    empty: bool
    fills: int = 0
    commission: float = 0.0
    fees: float = 0.0
    spread: float = Field(default=0.0, description="Modelled crossing cost, $")
    slippage: float = Field(default=0.0, description="Fill vs mid beyond the model (+ = worse)")
    total: float = 0.0
    gross_pnl: float | None = Field(default=None, description="Σ realised of trades closed")
    cost_pct_of_gross: float | None = Field(default=None, description="total / |gross_pnl|")
    compare_total: float | None = None
    change: float | None = None
    fees_from_open: int = Field(default=0, description="Close fills priced with their open's fees")
    unmodelled: int = Field(default=0, description="Fills with no stored cost model")
    bars: list[CostBar] = Field(default_factory=list)


class WinLossCard(BaseModel):
    model_config = _FORBID

    empty: bool
    stats: TradeStats
    compare_win_rate: float | None = None
    compare_expectancy: float | None = None


class ModelPoint(BaseModel):
    model_config = _FORBID

    open_proposal_hash: str
    ticker: str
    closed_at: _dt.datetime
    net_ev: float = Field(description="Managed net EV x contracts, $")
    realised: float
    shadow: float | None = None


class ModelCard(BaseModel):
    model_config = _FORBID

    empty: bool
    n: int = 0
    sum_managed_ev: float = 0.0
    sum_static_ev: float = 0.0
    sum_realised: float = 0.0
    sum_shadow: float | None = None
    n_shadow: int = 0
    win_rate: float | None = Field(default=None, description="Realised win rate (managed exits)")
    mean_managed_pop: float | None = None
    hold_win_rate: float | None = Field(default=None, description="Shadow > 0 share")
    mean_static_pop: float | None = None
    points: list[ModelPoint] = Field(default_factory=list)


class BreakdownItem(BaseModel):
    model_config = _FORBID

    key: str
    label: str
    count: int
    wins: int
    pnl: float
    win_rate: float
    share: float
    filter: dict[str, str] | None = Field(default=None, description="Trades list query for the row")


class Breakdowns(BaseModel):
    model_config = _FORBID

    ticker: list[BreakdownItem] = Field(default_factory=list)
    structure: list[BreakdownItem] = Field(default_factory=list)
    exit_reason: list[BreakdownItem] = Field(default_factory=list)
    reason_code: list[BreakdownItem] = Field(default_factory=list)
    profile: list[BreakdownItem] = Field(default_factory=list)
    regime: list[BreakdownItem] = Field(default_factory=list)


class CalibrationItem(BaseModel):
    model_config = _FORBID

    persona: str
    lo: float
    hi: float
    n: int
    stated_mean: float
    hit_rate: float
    gap: float = Field(description="hit_rate − stated_mean (negative = over-confident)")


class CalibrationCard(BaseModel):
    model_config = _FORBID

    empty: bool
    trades: int = Field(default=0, description="Closed trades to the period end (all time)")
    rows: list[CalibrationItem] = Field(default_factory=list)


class FunnelStep(BaseModel):
    model_config = _FORBID

    key: str
    label: str
    count: int


class FunnelCard(BaseModel):
    model_config = _FORBID

    empty: bool
    steps: list[FunnelStep] = Field(default_factory=list)
    gate_fail: int = 0
    violations: dict[str, int] = Field(default_factory=dict, description="rule code -> n")


class PerformanceResponse(BaseModel):
    model_config = _FORBID

    as_of: _dt.datetime
    preset: Preset
    compare: Compare
    include_tests: bool
    period: PeriodView
    compare_period: PeriodView | None
    net_pnl: NetPnlCard
    equity: EquityCard
    costs: CostsCard
    win_loss: WinLossCard
    model: ModelCard
    breakdowns: Breakdowns
    calibration: CalibrationCard
    funnel: FunnelCard


class BreakdownResponse(BaseModel):
    model_config = _FORBID

    as_of: _dt.datetime
    by: BreakdownBy
    period: PeriodView
    include_tests: bool
    rows: list[BreakdownItem]


# ---------------------------------------------------------------------------
# Store reads
# ---------------------------------------------------------------------------


def smoke_test_hashes(conn: sqlite3.Connection) -> set[str]:
    """Proposal hashes of broker smoke-test trades: an ``arc-<hex>`` client id on the open
    or the close, plus the other side of the same structure."""
    rows = conn.execute(
        "SELECT DISTINCT proposal_hash FROM orders WHERE client_order_id LIKE ?",
        (_TEST_ORDER_PREFIX + "%",),
    )
    direct = {r[0] for r in rows}
    if not direct:
        return direct
    exits = _exit_hashes(conn)
    family = set(direct)
    for sid, open_hash in conn.execute("SELECT id, open_proposal_hash FROM open_structures"):
        close = exits.get(sid)
        if open_hash in direct or close in direct:
            family.add(open_hash)
            if close:
                family.add(close)
    return family


def _exit_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    """structure id -> the proposal hash of the close that closed it."""
    out: dict[str, str] = {}
    for r in conn.execute(
        """SELECT e.structure_id, e.proposal_hash FROM executions e
           WHERE e.kind = 'close' AND e.structure_id IS NOT NULL AND e.filled_qty > 0
           ORDER BY e.started_at, e.rowid"""
    ):
        out[r[0]] = r[1]
    for r in conn.execute(
        "SELECT id, exit_proposal_hash FROM open_structures WHERE exit_proposal_hash IS NOT NULL"
    ):
        out.setdefault(r[0], r[1])
    return out


def _is_test(c: ClosedPosition, tests: set[str], exits: dict[str, str]) -> bool:
    return c.open_proposal_hash in tests or exits.get(c.structure_id) in tests


def _inception(conn: sqlite3.Connection, series: list[DailyEquity]) -> _dt.date | None:
    days: list[_dt.date] = [series[0].day] if series else []
    row = conn.execute("SELECT MIN(day) FROM proposals WHERE day IS NOT NULL").fetchone()
    if row and row[0]:
        days.append(_dt.date.fromisoformat(row[0]))
    return min(days) if days else None


def _attrs(conn: sqlite3.Connection, hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Per open proposal: account profile, regime at entry, persona reason codes."""
    out: dict[str, dict[str, Any]] = {}
    exits = _exit_hashes(conn)
    by_open = {
        r[0]: r[1] for r in conn.execute("SELECT open_proposal_hash, id FROM open_structures")
    }
    for h in hashes:
        p = conn.execute("SELECT regime FROM proposals WHERE proposal_hash = ?", (h,)).fetchone()
        mc = conn.execute(
            """SELECT payload FROM market_contexts WHERE proposal_hash = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (h,),
        ).fetchone()
        payload = _json(mc[0], {}) if mc else {}
        analytics = payload.get("analytics") or {}
        linked = [h]
        close = exits.get(by_open.get(h, ""))
        if close:
            linked.append(close)
        marks = ",".join("?" * len(linked))
        codes = sorted(
            {
                legacy.reason_code(r[0])
                for r in conn.execute(
                    f"""SELECT DISTINCT reason_code FROM decisions
                        WHERE proposal_hash IN ({marks})
                          AND persona IN ({",".join("?" * len(_PERSONAS))})""",  # noqa: S608
                    [*linked, *_PERSONAS],
                )
            }
        )
        out[h] = {
            "profile": analytics.get("account_profile"),
            "regime": (p[0] if p and p[0] else None) or payload.get("regime"),
            "codes": codes,
        }
    return out


# ---------------------------------------------------------------------------
# Card builders (pure over the reads)
# ---------------------------------------------------------------------------


def _daily_change(series: list[DailyEquity]) -> dict[_dt.date, float]:
    return {b.day: float(b.equity - a.equity) for a, b in zip(series, series[1:], strict=False)}


def _local_day(ts: _dt.datetime) -> _dt.date:
    return ts.astimezone(ET).date()


def _net_pnl(  # noqa: PLR0913 - the card joins several reads
    p: Period,
    series: list[DailyEquity],
    closed: list[ClosedPosition],
    tests_closed: list[ClosedPosition],
    today: _dt.date,
    compare_net: float | None,
) -> NetPnlCard:
    bucket = bucket_for(p.days if p.slot_end == p.last else (p.slot_end - p.first).days + 1)
    changes = _daily_change(series)
    test_by_day: dict[_dt.date, float] = defaultdict(float)
    for c in tests_closed:
        test_by_day[_local_day(c.closed_at)] += c.realised_pnl
    shadow_by_day: dict[_dt.date, float] = defaultdict(float)
    known = [c for c in closed if c.shadow_hold_pnl is not None]
    for c in known:
        shadow_by_day[_local_day(c.closed_at)] += (c.shadow_hold_pnl or 0.0) - c.realised_pnl
    realised_by_day: dict[_dt.date, float] = defaultdict(float)
    for c in closed:
        realised_by_day[_local_day(c.closed_at)] += c.realised_pnl
    has_equity = any(p.first <= d <= p.last for d in changes)
    source: Literal["equity", "realised", "none"] = (
        "equity" if has_equity else ("realised" if closed else "none")
    )
    per_day = (
        {d: v - test_by_day.get(d, 0.0) for d, v in changes.items()}
        if source == "equity"
        else dict(realised_by_day)
    )
    bars: list[PnlBar] = []
    cum = shadow_cum = 0.0
    now_label = None
    for s, e in _slots(p, bucket):
        label = _label(s, bucket)
        if s <= today <= e:
            now_label = label
        future = s > today
        vals = [v for d, v in per_day.items() if s <= d <= e and d <= p.last]
        pnl = sum(vals) if vals else None
        if not future:
            cum += pnl or 0.0
            shadow_cum += (pnl or 0.0) + sum(v for d, v in shadow_by_day.items() if s <= d <= e)
        bars.append(
            PnlBar(
                start=s,
                end=e,
                label=label,
                future=future,
                pnl=None if future else pnl,
                cumulative=None if future else cum,
                shadow_cumulative=None if future or not known else shadow_cum,
            )
        )
    realised = sum(c.realised_pnl for c in closed)
    net = sum(v for d, v in per_day.items() if p.first <= d <= p.last) if source != "none" else None
    return NetPnlCard(
        empty=source == "none",
        source=source,
        net=net,
        realised=realised,
        unrealised_change=None if net is None or source != "equity" else net - realised,
        compare_net=compare_net,
        change=None if net is None or compare_net is None else net - compare_net,
        bucket=bucket,
        bars=bars,
        now_label=now_label,
        shadow_known=len(known),
        shadow_delta=sum((c.shadow_hold_pnl or 0.0) - c.realised_pnl for c in known)
        if known
        else None,
        tests_excluded_pnl=sum(c.realised_pnl for c in tests_closed),
    )


def _net_value(
    p: Period, series: list[DailyEquity], closed: list[ClosedPosition], tests: list[ClosedPosition]
) -> float | None:
    """The Net P&L hero alone (for the comparison period)."""
    changes = {d: v for d, v in _daily_change(series).items() if p.first <= d <= p.last}
    if changes:
        return sum(changes.values()) - sum(c.realised_pnl for c in tests)
    return sum(c.realised_pnl for c in closed) if closed else None


def _equity(p: Period, series: list[DailyEquity]) -> EquityCard:
    before = [s for s in series if s.day < p.first]
    inside = [s for s in series if p.first <= s.day <= p.last]
    if not inside:
        return EquityCard(empty=True)
    window = ([before[-1]] if before else []) + inside
    dd = drawdown(window)
    rets = daily_returns(window)
    start, end, ret = period_return(series, p.first, p.last)
    return EquityCard(
        empty=False,
        points=[EquityPoint(day=s.day, equity=float(s.equity)) for s in window],
        start_equity=None if start is None else float(start),
        end_equity=None if end is None else float(end),
        return_pct=ret,
        max_drawdown=float(dd.amount) if dd.peak_day else 0.0,
        max_drawdown_pct=dd.pct if dd.peak_day else 0.0,
        drawdown_peak=dd.peak_day,
        drawdown_trough=dd.trough_day,
        drawdown_recovered=dd.recovered,
        sharpe=sharpe(rets),
        sortino=sortino(rets),
        returns=len(rets),
    )


def _split_cost(r: SlippageRow) -> tuple[float, float]:
    """(spread, slippage): the modelled crossing cost and the fill beyond it."""
    if r.expected_usd is None:
        return 0.0, r.realised_usd
    return r.expected_usd, r.realised_usd - r.expected_usd


def _costs(
    p: Period,
    rows: list[SlippageRow],
    bucket: Bucket,
    gross: float | None,
    compare_total: float | None,
) -> CostsCard:
    if not rows:
        return CostsCard(empty=True, gross_pnl=gross, compare_total=compare_total)
    bars = []
    for s, e in _slots(p, bucket):
        if s > p.last:
            break
        inside = [r for r in rows if r.at is not None and s <= _local_day(r.at) <= e]
        sp = [_split_cost(r) for r in inside]
        bars.append(
            CostBar(
                start=s,
                end=e,
                label=_label(s, bucket),
                commission=sum(r.commission for r in inside),
                fees=sum(r.fees for r in inside),
                spread=sum(x[0] for x in sp),
                slippage=sum(x[1] for x in sp),
            )
        )
    parts = [_split_cost(r) for r in rows]
    commission = sum(r.commission for r in rows)
    fees = sum(r.fees for r in rows)
    spread = sum(x[0] for x in parts)
    slip = sum(x[1] for x in parts)
    total = commission + fees + spread + slip
    return CostsCard(
        empty=False,
        fills=len(rows),
        commission=commission,
        fees=fees,
        spread=spread,
        slippage=slip,
        total=total,
        gross_pnl=gross,
        cost_pct_of_gross=total / abs(gross) if gross else None,
        compare_total=compare_total,
        change=None if compare_total is None else total - compare_total,
        fees_from_open=sum(1 for r in rows if r.fees_from_open),
        unmodelled=sum(1 for r in rows if r.expected_usd is None),
        bars=bars,
    )


def _cost_total(rows: list[SlippageRow]) -> float | None:
    if not rows:
        return None
    return sum(r.commission + r.fees + r.realised_usd for r in rows)


def _model(closed: list[ClosedPosition]) -> ModelCard:
    mv = model_vs_realised(closed)
    if mv.n == 0:
        return ModelCard(empty=True)
    rows = [c for c in closed if c.managed_net_ev is not None and c.static_net_ev is not None]
    hold, _ = hold_hit_rate(rows)
    return ModelCard(
        empty=False,
        n=mv.n,
        sum_managed_ev=mv.managed_net_ev,
        sum_static_ev=mv.static_net_ev,
        sum_realised=mv.realised,
        sum_shadow=mv.shadow_hold,
        n_shadow=mv.n_shadow,
        win_rate=mv.win_rate,
        mean_managed_pop=mv.mean_managed_pop,
        hold_win_rate=hold,
        mean_static_pop=mv.mean_static_pop,
        points=[
            ModelPoint(
                open_proposal_hash=c.open_proposal_hash,
                ticker=c.ticker,
                closed_at=c.closed_at,
                net_ev=c.managed_net_ev or 0.0,
                realised=c.realised_pnl,
                shadow=c.shadow_hold_pnl,
            )
            for c in rows
        ],
    )


# Title Case on the Tower (D50); Slack keeps sentence case (D22).
_STRUCTURE_LABEL = {
    "long_call": "Long Call",
    "long_put": "Long Put",
    "vertical_debit": "Debit Vertical",
    "vertical_credit": "Credit Vertical",
    "iron_condor": "Iron Condor",
    "covered_call": "Covered Call",
    "cash_secured_put": "Cash-Secured Put",
    "other": "Other",
}


def structure_label(kind: str) -> str:
    """``vertical_debit`` -> ``Debit Vertical``; unknown kinds Title Case each word."""
    return _STRUCTURE_LABEL.get(kind) or " ".join(w.capitalize() for w in kind.split("_"))


def _humanise(code: str) -> str:
    text = code.replace("_", " ").replace(":", ": ")
    return text[:1].upper() + text[1:]


def _items(by: BreakdownBy, rows: list[BreakdownRow]) -> list[BreakdownItem]:
    param = {
        "ticker": "ticker",
        "structure": "structure",
        "exit_reason": "exit_reason",
        "reason_code": "reason_code",
        "profile": "account_profile",
        "regime": None,
    }[by]
    out = []
    for r in rows:
        if not r.key:
            label = "Not recorded"
        elif by == "structure":
            label = structure_label(r.key)
        elif by == "reason_code":
            label = reason_label(r.key)
        elif by == "ticker":
            label = r.key
        else:
            label = _humanise(r.key)
        if not (param and r.key):
            filt = None
        elif by == "reason_code":  # exit codes sit on the close proposal, not the closed open
            filt = {param: r.key}
        else:
            filt = {param: r.key, "stage": "closed"}
        out.append(BreakdownItem(label=label, filter=filt, **r.model_dump()))
    return out


def _breakdown(
    by: BreakdownBy, closed: list[ClosedPosition], attrs: dict[str, dict[str, Any]]
) -> list[BreakdownItem]:
    pairs: list[tuple[str | None, ClosedPosition]]
    if by == "ticker":
        pairs = [(c.ticker, c) for c in closed]
    elif by == "structure":
        pairs = [(c.kind, c) for c in closed]
    elif by == "exit_reason":
        pairs = [(c.exit_reason, c) for c in closed]
    elif by == "profile":
        pairs = [(attrs[c.open_proposal_hash]["profile"], c) for c in closed]
    elif by == "regime":
        pairs = [(attrs[c.open_proposal_hash]["regime"], c) for c in closed]
    else:  # a trade counts under each persona reason code on its open / close decisions
        pairs = [(code, c) for c in closed for code in attrs[c.open_proposal_hash]["codes"]]
    return _items(by, breakdown(pairs))


def _calibration(conn: sqlite3.Connection, history: list[ClosedPosition]) -> CalibrationCard:
    points = calibration_points(conn, [(c.open_proposal_hash, c.realised_pnl > 0) for c in history])
    rows = [
        CalibrationItem(
            persona=b.persona,
            lo=b.lo,
            hi=b.hi,
            n=b.n,
            stated_mean=b.stated_mean,
            hit_rate=b.hit_rate,
            gap=b.gap,
        )
        for b in calibration(points)
    ]
    return CalibrationCard(empty=not rows, trades=len(history), rows=rows)


def _funnel(
    conn: sqlite3.Connection, start: _dt.datetime, end: _dt.datetime, closed: int
) -> FunnelCard:
    f, violations = funnel(conn, start, end)
    steps = [
        FunnelStep(key="proposed", label="Proposed", count=f.proposals),
        FunnelStep(key="gate_pass", label="Gate pass", count=f.gate_pass),
        FunnelStep(
            key="approved",
            label="Approved",
            count=f.approvals.click_approved + f.approvals.auto_approved,
        ),
        FunnelStep(key="filled", label="Filled", count=f.fills),
        FunnelStep(key="closed", label="Closed", count=closed),
    ]
    return FunnelCard(
        empty=f.proposals == 0 and f.gate_pass + f.gate_fail == 0 and closed == 0,
        steps=steps,
        gate_fail=f.gate_fail,
        violations=violations,
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


class _Scope:
    """Closed trades and fills of one period, test legs split out."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        p: Period,
        now: _dt.datetime,
        tests: set[str],
        exits: dict[str, str],
        *,
        include_tests: bool,
    ) -> None:
        self.start, self.end = _bounds(p)
        everything = closed_positions(conn, start=self.start, end=self.end, now=now)
        drop = set() if include_tests else tests
        self.closed = [c for c in everything if not _is_test(c, drop, exits)]
        self.tests = [c for c in everything if _is_test(c, drop, exits)]
        self.fills = [
            r
            for r in execution_costs(conn, self.start, self.end).rows
            if r.proposal_hash not in drop
        ]


def _view(p: Period) -> PeriodView:
    return PeriodView(first=p.first, last=p.last, slot_end=p.slot_end, days=p.days)


def load_performance(  # noqa: PLR0913 - one argument per URL parameter
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    preset: Preset = "90d",
    compare: Compare = "prev",
    include_tests: bool = False,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
) -> PerformanceResponse:
    """Every Performance card for *preset* (as of *now*), read-only."""
    today = now.astimezone(ET).date()
    series = daily_equity(conn) if _has_table(conn, "pnl_snapshots") else []
    p = resolve_period(
        preset, today, date_from=date_from, date_to=date_to, inception=_inception(conn, series)
    )
    cp = comparison_period(p, compare)
    tests, exits = smoke_test_hashes(conn), _exit_hashes(conn)
    cur = _Scope(conn, p, now, tests, exits, include_tests=include_tests)
    prev = _Scope(conn, cp, now, tests, exits, include_tests=include_tests) if cp else None
    net = _net_pnl(
        p,
        series,
        cur.closed,
        cur.tests,
        today,
        _net_value(cp, series, prev.closed, prev.tests) if cp and prev else None,
    )
    history = [
        c
        for c in closed_positions(conn, start=cur.start, end=cur.end, now=now, all_time=True)
        if not _is_test(c, set() if include_tests else tests, exits)
    ]
    attrs = _attrs(conn, {c.open_proposal_hash for c in cur.closed})
    stats = trade_stats(cur.closed)
    prev_stats = trade_stats(prev.closed) if prev else None
    gross = sum(c.realised_pnl for c in cur.closed) if cur.closed else None
    return PerformanceResponse(
        as_of=now.astimezone(ET),
        preset=preset,
        compare=compare,
        include_tests=include_tests,
        period=_view(p),
        compare_period=_view(cp) if cp else None,
        net_pnl=net,
        equity=_equity(p, series),
        costs=_costs(p, cur.fills, net.bucket, gross, _cost_total(prev.fills) if prev else None),
        win_loss=WinLossCard(
            empty=stats.closed == 0,
            stats=stats,
            compare_win_rate=prev_stats.win_rate if prev_stats else None,
            compare_expectancy=prev_stats.expectancy if prev_stats else None,
        ),
        model=_model(cur.closed),
        breakdowns=Breakdowns(**{by: _breakdown(by, cur.closed, attrs) for by in BREAKDOWNS}),
        calibration=_calibration(conn, history),
        funnel=_funnel(conn, cur.start, cur.end, len(cur.closed)),
    )


def load_breakdown(  # noqa: PLR0913 - one argument per URL parameter
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    by: BreakdownBy,
    preset: Preset = "90d",
    include_tests: bool = False,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
) -> BreakdownResponse:
    """One breakdown tab (the same rows :func:`load_performance` returns)."""
    today = now.astimezone(ET).date()
    series = daily_equity(conn) if _has_table(conn, "pnl_snapshots") else []
    p = resolve_period(
        preset, today, date_from=date_from, date_to=date_to, inception=_inception(conn, series)
    )
    tests, exits = smoke_test_hashes(conn), _exit_hashes(conn)
    cur = _Scope(conn, p, now, tests, exits, include_tests=include_tests)
    attrs = _attrs(conn, {c.open_proposal_hash for c in cur.closed})
    return BreakdownResponse(
        as_of=now.astimezone(ET),
        by=by,
        period=_view(p),
        include_tests=include_tests,
        rows=_breakdown(by, cur.closed, attrs),
    )
