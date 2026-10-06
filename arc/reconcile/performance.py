"""Day / MTD / YTD performance from ``pnl_snapshots`` (E6.3, D28 Auditor card).

Pure over the store: :func:`performance` reads the daily snapshots the
reconciler writes (one per ET day, ``details_json.day``; a re-run the same day
supersedes the earlier row) and returns :class:`arc.models.Performance`.

Definitions (owner, E5.5 review: "% = pnl / equity at the start of the period"):

- a day's equity is its snapshot's closing ``equity``;
- the start of a period is the closing equity of the last snapshot *before* the
  period (day: the previous snapshot; month: the last one before the 1st; year:
  the last one before Jan 1). When history began inside the period, the start is
  the first snapshot in it, i.e. P&L since inception;
- P&L = equity(day) - start; % = P&L / start.

With no snapshot *before* ``day`` (no history, or the first day of history) the
helper returns ``None`` and the card shows n/a.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from arc.models import Performance
from arc.store.repos import PnlSnapshotRepo

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "Drawdown",
    "DailyEquity",
    "TRADING_DAYS",
    "daily_equity",
    "daily_returns",
    "drawdown",
    "performance",
    "performance_from",
    "period_return",
    "sharpe",
    "sortino",
]


@dataclass(frozen=True)
class DailyEquity:
    """One ET day's closing equity."""

    day: _dt.date
    equity: Decimal


def daily_equity(conn: sqlite3.Connection) -> list[DailyEquity]:
    """Daily closing equity from ``pnl_snapshots`` (latest row per day, oldest first)."""
    out: list[DailyEquity] = []
    for row in PnlSnapshotRepo(conn).daily():
        d = json.loads(row["details_json"])
        if d.get("equity") in (None, ""):
            continue
        out.append(DailyEquity(_dt.date.fromisoformat(d["day"]), Decimal(str(d["equity"]))))
    return out


def _period(
    before: list[DailyEquity], inside: list[DailyEquity], last: DailyEquity
) -> tuple[float, float | None]:
    start = before[-1].equity if before else inside[0].equity
    pnl = last.equity - start
    return float(pnl), (float(pnl / start) if start > 0 else None)


def performance_from(series: list[DailyEquity], day: _dt.date) -> Performance | None:
    """Performance as of *day* from a daily series (pure). ``None`` without prior history."""
    today = [s for s in series if s.day == day]
    prior = [s for s in series if s.day < day]
    if not today or not prior:
        return None
    last = today[-1]
    month0 = day.replace(day=1)
    year0 = day.replace(month=1, day=1)
    upto = [*prior, last]

    day_pnl, day_pct = _period(prior, [last], last)
    mtd, mtd_pct = _period(
        [s for s in prior if s.day < month0], [s for s in upto if s.day >= month0], last
    )
    ytd, ytd_pct = _period(
        [s for s in prior if s.day < year0], [s for s in upto if s.day >= year0], last
    )
    return Performance(
        day_pnl=day_pnl,
        day_pct=day_pct,
        mtd_pnl=mtd,
        mtd_pct=mtd_pct,
        ytd_pnl=ytd,
        ytd_pct=ytd_pct,
        equity=float(last.equity),
    )


def performance(conn: sqlite3.Connection, day: _dt.date) -> Performance | None:
    """Day / MTD / YTD P&L and % of starting equity as of ET *day* (``None``: no history)."""
    return performance_from(daily_equity(conn), day)


# ---------------------------------------------------------------------------
# Equity-curve statistics (E8.7c Performance page; pure over a DailyEquity series)
# ---------------------------------------------------------------------------

TRADING_DAYS = 252
"""Annualisation factor for daily Sharpe: sessions per year."""


@dataclass(frozen=True)
class Drawdown:
    """The deepest peak-to-trough fall of daily closing equity.

    ``amount`` is ≤ 0 ($, trough − peak); ``pct`` is ``amount / peak`` (≤ 0).
    ``recovered`` is the first day equity closed back at or above the peak (``None``
    while still under water).
    """

    amount: Decimal
    pct: float | None
    peak_day: _dt.date | None
    trough_day: _dt.date | None
    recovered: _dt.date | None


def drawdown(series: list[DailyEquity]) -> Drawdown:
    """Maximum drawdown of *series* (oldest first): running peak vs each later close."""
    best = Drawdown(Decimal(0), None, None, None, None)
    peak: DailyEquity | None = None
    for s in series:
        if peak is None or s.equity >= peak.equity:
            peak = s
            continue
        amount = s.equity - peak.equity
        if amount < best.amount:
            pct = float(amount / peak.equity) if peak.equity > 0 else None
            best = Drawdown(amount, pct, peak.day, s.day, None)
    if best.peak_day is None:
        return best
    level = next(s.equity for s in series if s.day == best.peak_day)
    rec = next((s.day for s in series if s.day > best.trough_day and s.equity >= level), None)  # type: ignore[operator]
    return Drawdown(best.amount, best.pct, best.peak_day, best.trough_day, rec)


def daily_returns(series: list[DailyEquity]) -> list[float]:
    """Close-to-close returns ``e[i] / e[i-1] − 1`` (a non-positive prior close is skipped)."""
    return [
        float(b.equity / a.equity - 1)
        for a, b in zip(series, series[1:], strict=False)
        if a.equity > 0
    ]


def sharpe(returns: list[float], *, periods: int = TRADING_DAYS) -> float | None:
    """Annualised Sharpe of daily *returns*: ``mean / sample stdev × √periods``.

    Risk-free rate 0, daily closes of account equity (so deposits and withdrawals count
    as returns). ``None`` with fewer than two returns or zero dispersion.
    """
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return None
    return mean / var**0.5 * periods**0.5


def sortino(returns: list[float], *, periods: int = TRADING_DAYS) -> float | None:
    """Annualised Sortino of daily *returns*: ``mean / downside deviation × √periods``.

    Target and risk-free rate 0; downside deviation = ``sqrt(mean(min(r, 0)²))`` over all
    returns (the same definition as :func:`arc.experiments.stats.sortino`). ``None`` with
    fewer than two returns or no losing day (no downside to divide by).
    """
    n = len(returns)
    if n < 2:
        return None
    downside = sum(min(r, 0.0) ** 2 for r in returns) / n
    if downside <= 0:
        return None
    return sum(returns) / n / downside**0.5 * periods**0.5


def period_return(
    series: list[DailyEquity], first: _dt.date, last: _dt.date
) -> tuple[Decimal | None, Decimal | None, float | None]:
    """(start equity, end equity, return) over ET days ``[first, last]``.

    Same start rule as :func:`performance_from`: the last close *before* ``first``,
    else the first close inside (P&L since inception); ``None`` without closes inside.
    """
    before = [s for s in series if s.day < first]
    inside = [s for s in series if first <= s.day <= last]
    if not inside:
        return None, None, None
    start = before[-1].equity if before else inside[0].equity
    end = inside[-1].equity
    return start, end, (float(end / start - 1) if start > 0 else None)
