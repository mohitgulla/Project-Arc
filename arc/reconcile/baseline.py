"""Start-of-day equity ("prev close"), the one baseline for day P&L (E5.9b, D43).

Day P&L is ``equity − start_of_day_equity``. Every surface uses this module: the
loop root and the Director digest (``portfolio_context``), the gate's daily-loss
rule and :meth:`arc.gate.halt.HaltSwitch.check_daily_loss` (via
:func:`arc.pipeline.market.account_snapshot`), the monitor heartbeat, the EOD
reconcile snapshot (and the Auditor journal reading it) and the control tower.

Why not the broker's ``last_equity``: Alpaca values the book at official closing
prices, while Arc's ``equity`` each tick (and its EOD reconcile mark) comes from
live quotes. On wide option spreads the two disagree by hundreds of dollars per
leg, so ``equity − last_equity`` carries a constant offset all day (Fri
2026-10-02: −$1,004 at 09:40 with nothing traded).

Rule (deterministic, a pure read of ``pnl_snapshots``):

1. **Primary** (``source = arc_close``): the newest ``pnl_snapshots`` row whose
   ET ``details_json.day`` is the previous trading session before *day*
   (:func:`arc.utils.calendar.previous_session`, so weekends and market holidays
   roll back), using its ``details_json.equity``: Arc's own EOD mark, valued the
   same way as the intraday ticks.
2. **Fallback** (``source = broker_last_equity``): the broker's ``last_equity``,
   only when there is no Arc close for that session (first day, or a missed
   reconcile, i.e. a gap of more than one session).
3. Neither available: ``None`` (callers show no day P&L; the gate's daily-loss
   rule fails closed on a 0 baseline).

Not pinned per day in ``routine_state``: the reconciler writes a row for the ET
day of its own run, so a previous-session close cannot land during the current
day (only a manual backfill could), and the read-only tower recomputes the same
number from the same rows. Every caller resolves the identical value all day.
"""

from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.utils.calendar import previous_session

if TYPE_CHECKING:
    import sqlite3

__all__ = ["Baseline", "BaselineSource", "day_pnl", "start_of_day_equity"]

BaselineSource = Literal["arc_close", "broker_last_equity"]


class Baseline(BaseModel):
    """Start-of-day equity for ET *day* and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Decimal = Field(..., description="Start-of-day equity (prev close), dollars")
    source: BaselineSource = Field(
        ..., description="arc_close = Arc's prior-session EOD mark; broker_last_equity = fallback"
    )
    as_of: _dt.datetime | None = Field(
        None, description="snapshot_at of the Arc close row; None for the broker fallback"
    )
    day: _dt.date = Field(..., description="The ET day this baseline is the start of")
    prev_session: _dt.date = Field(..., description="The trading session whose close it is")


def _dec(v: object) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        out = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _parse_ts(v: str) -> _dt.datetime | None:
    try:
        ts = _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.UTC)


def _arc_close(conn: sqlite3.Connection, session: _dt.date) -> tuple[Decimal, str] | None:
    """Equity and ``snapshot_at`` of the newest snapshot for ET *session* (or ``None``)."""
    try:
        rows = conn.execute(
            """SELECT snapshot_at, details_json FROM pnl_snapshots
               WHERE json_extract(details_json, '$.day') = ?
               ORDER BY snapshot_at DESC, rowid DESC""",
            (session.isoformat(),),
        ).fetchall()
    except Exception:  # noqa: BLE001 - no table yet (fresh / foreign DB): no Arc close
        return None
    for r in rows:
        try:
            eq = _dec(json.loads(r[1]).get("equity"))
        except (TypeError, ValueError, AttributeError):
            continue
        if eq is not None and eq > 0:
            return eq, str(r[0])
    return None


def start_of_day_equity(
    conn: sqlite3.Connection, day: _dt.date, *, broker_last_equity: Decimal | None
) -> Baseline | None:
    """The start-of-day equity for ET *day* (see module doc). ``None`` when unknown."""
    prev = previous_session(day)
    close = _arc_close(conn, prev)
    if close is not None:
        value, at = close
        return Baseline(value=value, source="arc_close", as_of=_parse_ts(at), day=day,
                        prev_session=prev)  # fmt: skip
    if broker_last_equity is not None and broker_last_equity > 0:
        return Baseline(value=broker_last_equity, source="broker_last_equity", as_of=None,
                        day=day, prev_session=prev)  # fmt: skip
    return None


def day_pnl(equity: Decimal | None, baseline: Baseline | None) -> Decimal | None:
    """``equity − baseline`` (``None`` when either is unknown)."""
    if equity is None or baseline is None:
        return None
    return equity - baseline.value
