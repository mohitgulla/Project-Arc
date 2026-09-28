"""Day / MTD / YTD performance from ``pnl_snapshots`` (E6.3, D28 Auditor card).

Pure over the store: :func:`performance` reads the daily snapshots the
reconciler writes (one per ET day, ``details_json.day``; a re-run the same day
supersedes the earlier row) and returns :class:`arc.slack.digests.Performance`.

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

from arc.slack.digests import Performance
from arc.store.repos import PnlSnapshotRepo

if TYPE_CHECKING:
    import sqlite3

__all__ = ["DailyEquity", "daily_equity", "performance", "performance_from"]


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
