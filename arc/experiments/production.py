"""Production panel of a spec v3 report (PLAN D108, card E25.3). Descriptive only.

From spec v3 the verdict compares every treatment Ti with the flat **control** arm
``XP-<n>:control`` (:mod:`arc.experiments.multi`). Production (``arm_id`` NULL, the
production account) stays the permanent baseline, and this module reports how it
compares, **never** as a verdict, stop or promotion input: nothing in the verdict
path reads :class:`ProductionPanel` (``tests/test_experiment_production.py`` perturbs
the production series and asserts the verdict does not move).

Per session ``t`` (an ET session after t0 with production's and control's EOD rows):

- ``p_pnl`` = production's broker equity delta (``details_json.equity``);
- ``p_start_book_pnl`` = the day's P&L of the **start book** (production's open
  structures at t0, :attr:`RunningDetail.production_book`): the change of their
  marks in production's EOD ``positions_snapshots`` plus the cash of their closes,
  the same maths the v1/v2 evaluator used for the legacy book;
- ``p_ex_start_pnl`` = ``p_pnl - p_start_book_pnl``;
- ``c_pnl`` and each ``ti_pnl`` = the arm's virtual-equity delta (t0 equity before
  the first EOD row, as in the verdict series);
- ``start_book = p_start_book_pnl / t0``, ``residual = (p_ex_start_pnl - c_pnl) / t0``
  and ``d_p_c = start_book + residual``. ``d_p_c`` is stored as that sum, so the
  decomposition ``d_p_c == start_book + residual`` holds exactly; it equals
  ``(p_pnl - c_pnl) / t0`` up to float rounding. The residual (capital tied in the
  start book, the account/fill gap, the order budget, pairing lag) should go to 0
  once the start book has closed and control has ramped (D108 Sentinel check);
- ``ramped``: True from the first session where the median over the trailing
  :data:`RAMP_WINDOW` sessions of control's open-structure count is >= production's
  (counts from each store's EOD ``positions_snapshots``, held structures). Sticky.

``compare`` holds, per treatment (and control), the always-valid CI of ``(arm - P)/t0``
on three bases: ``total``, ``ex_start_book`` (production without its start book) and
``post_ramp`` (total over the ramped sessions only). Descriptive: no Holm, no verdict.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
import json
import statistics
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.experiments import stats  # noqa: TC001 - pydantic resolves stats.Interval

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.models import ExperimentState

__all__ = [
    "RAMP_WINDOW",
    "ProductionCompare",
    "ProductionPanel",
    "ProductionSession",
    "StartBook",
    "StartBookDay",
    "build_production_panel",
    "ramp_flags",
]

#: D108: trailing sessions whose median open-structure counts decide ``ramped``.
RAMP_WINDOW = 5
_FORBID = ConfigDict(extra="forbid", frozen=True)

CompareBasis = Literal["total", "ex_start_book", "post_ramp"]


class ProductionSession(BaseModel):
    """One session of the production panel ($ amounts; fractions of t0 equity)."""

    model_config = _FORBID

    day: _dt.date
    p_pnl: float = Field(..., description="Production broker equity delta ($)")
    p_start_book_pnl: float = Field(..., description="Start book's day P&L ($)")
    p_ex_start_pnl: float = Field(..., description="p_pnl - p_start_book_pnl ($)")
    c_pnl: float = Field(..., description="Control arm's virtual-equity delta ($)")
    ti_pnl: dict[str, float] = Field(
        default_factory=dict, description="Treatment -> virtual-equity delta ($); absent = missing"
    )
    d_p_c: float = Field(..., description="start_book + residual (~ (p_pnl - c_pnl) / t0)")
    start_book: float = Field(..., description="p_start_book_pnl / t0")
    residual: float = Field(..., description="(p_ex_start_pnl - c_pnl) / t0")
    p_open: int | None = Field(None, description="Production's open structures at EOD")
    c_open: int | None = Field(None, description="Control's open structures at EOD")
    ramped: bool = False


class StartBookDay(BaseModel):
    model_config = _FORBID

    day: _dt.date
    open: int = Field(..., description="Start-book structures still held at EOD")
    closed: int = Field(..., description="Start-book structures closed by this day")
    pnl: float = Field(..., description="Start book's day P&L ($)")
    cum_pnl: float = Field(..., description="Cumulative start-book P&L since t0 ($)")


class StartBook(BaseModel):
    """Production's open book at t0, tracked to its last close (reporting only)."""

    model_config = _FORBID

    ids: list[str]
    close_dates: dict[str, _dt.date | None] = Field(
        default_factory=dict, description="Structure id -> ET close day (None = still open)"
    )
    days: list[StartBookDay] = Field(default_factory=list)
    cum_pnl: float = 0.0


class ProductionCompare(BaseModel):
    """``arm - production`` (fraction of t0 equity per session): descriptive CI only."""

    model_config = _FORBID

    arm: str
    basis: CompareBasis
    n: int
    mean: float | None
    ci: stats.Interval | None = Field(None, description="Always-valid (mSPRT) CI, 1 - alpha")
    p_value: float | None = Field(None, description="Always-valid p (descriptive, no Holm)")


class ProductionPanel(BaseModel):
    """D108: production vs control vs treatments. Never a verdict, stop or promotion input."""

    model_config = _FORBID

    descriptive: Literal[True] = True
    ramp_window: int = RAMP_WINDOW
    ramped_from: _dt.date | None = Field(None, description="First ramped session")
    sessions: list[ProductionSession] = Field(default_factory=list)
    missing_sessions: list[_dt.date] = Field(default_factory=list)
    cum_p_pnl: float = 0.0
    cum_c_pnl: float = 0.0
    cum_d_p_c: float = 0.0
    cum_start_book: float = 0.0
    cum_residual: float = 0.0
    start_book: StartBook
    compare: list[ProductionCompare] = Field(default_factory=list)


def ramp_flags(
    p_open: Sequence[int | None], c_open: Sequence[int | None], *, window: int = RAMP_WINDOW
) -> list[bool]:
    """``ramped`` per session: from the first session whose trailing-*window* median
    of control's open count is >= production's (sessions with an unknown count are
    left out of the window); sticky once reached."""
    out: list[bool] = []
    ramped = False
    for i in range(len(p_open)):
        if not ramped:
            lo = max(0, i - window + 1)
            pairs = [
                (p, c)
                for p, c in zip(p_open[lo : i + 1], c_open[lo : i + 1], strict=True)
                if p is not None and c is not None
            ]
            if pairs and p_open[i] is not None and c_open[i] is not None:
                ramped = statistics.median(c for _, c in pairs) >= statistics.median(
                    p for p, _ in pairs
                )
        out.append(ramped)
    return out


def _open_counts(conn: sqlite3.Connection, aid: str | None) -> dict[_dt.date, int]:
    """ET day -> held structures in the arm's latest EOD positions snapshot that day."""
    from arc.experiments.evaluate import _arm_clause  # noqa: PLC0415 - cycle

    where, args = _arm_clause(aid)
    out: dict[_dt.date, int] = {}
    for r in conn.execute(
        f"""SELECT positions_json FROM positions_snapshots
            WHERE {where} AND json_extract(positions_json, '$.day') IS NOT NULL
            ORDER BY snapshot_at, rowid""",  # noqa: S608 - fixed column clause
        args,
    ):
        snap = json.loads(r[0])
        out[_dt.date.fromisoformat(snap["day"])] = sum(
            1 for s in snap.get("structures", []) if s.get("held")
        )
    return out


def _close_dates(conn: sqlite3.Connection, ids: list[str]) -> dict[str, _dt.date | None]:
    """Start-book id -> ET day it closed (last filled close order, else the
    structure's ``closed_at``), None while open."""
    from arc.context.ttl import from_db

    out: dict[str, _dt.date | None] = dict.fromkeys(ids)
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for r in conn.execute(
        f"""SELECT id, closed_at FROM open_structures
            WHERE id IN ({marks}) AND status = 'closed' AND closed_at IS NOT NULL""",  # noqa: S608
        tuple(ids),
    ):
        out[r[0]] = from_db(r[1]).date()
    for r in conn.execute(
        f"""SELECT structure_id, MAX(finished_at) FROM executions
            WHERE kind = 'close' AND structure_id IN ({marks}) AND filled_qty > 0
              AND finished_at IS NOT NULL
            GROUP BY structure_id""",  # noqa: S608
        tuple(ids),
    ):
        if out.get(r[0]) is None:
            continue  # partially closed: still open until open_structures says so
        out[r[0]] = from_db(r[1]).date()
    return out


def _deltas(
    equity: dict[_dt.date, float],
    days: list[_dt.date],
    *,
    first: _dt.date,
    t0_equity: float | None,
) -> dict[_dt.date, float]:
    """Day -> equity delta vs the previous session (*t0_equity* before *first*'s row)."""
    from arc.utils.calendar import previous_session

    out: dict[_dt.date, float] = {}
    for day in days:
        prev = previous_session(day)
        e = equity.get(day)
        e_prev = equity.get(prev, t0_equity if day == first else None)
        if e is not None and e_prev is not None:
            out[day] = e - e_prev
    return out


def build_production_panel(
    conn: sqlite3.Connection,
    st: ExperimentState,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    control_arm: str,
    arm_ids: dict[str, str],
) -> ProductionPanel:
    """The D108 production panel of a spec v3 experiment (read-only)."""
    from arc.experiments import evaluate as ev  # noqa: PLC0415 - evaluate imports this module
    from arc.utils.calendar import ET, previous_session, sessions_between

    sp, run = st.spec, st.running
    assert run is not None  # noqa: S101 - build_report checks
    assert sp.alpha is not None and sp.min_sessions is not None  # noqa: S101
    t0_eq = run.t0_equity
    book = sorted(run.production_book)
    book_set = set(book)
    first = ev._first_session(run.t0)  # noqa: SLF001
    last = now.astimezone(ET).date()
    days = sessions_between(first, last)

    p_eq = ev._eod_equity(conn, None)  # noqa: SLF001
    marks = ev._legacy_marks(conn, book_set)  # noqa: SLF001
    cash = ev._legacy_cash(conn, book_set)  # noqa: SLF001
    field = ev.TREATMENT_EQUITY_FIELD
    c_pnl = _deltas(ev._eod_equity(conn, control_arm, field), days, first=first, t0_equity=t0_eq)  # noqa: SLF001
    t_pnl = {
        name: _deltas(ev._eod_equity(conn, aid, field), days, first=first, t0_equity=t0_eq)  # noqa: SLF001
        for name, aid in arm_ids.items()
    }
    p_open = _open_counts(conn, None)
    c_open = _open_counts(conn, control_arm)

    def book_value(day: _dt.date) -> float | None:
        if not book_set:
            return 0.0
        m = marks.get(day)
        return None if m is None else sum(m.values())

    rows: list[dict[str, object]] = []
    missing: list[_dt.date] = []
    for day in days:
        prev = previous_session(day)
        p, p_prev = p_eq.get(day), p_eq.get(prev)
        bv, bv_prev = book_value(day), book_value(prev)
        c = c_pnl.get(day)
        if p is None or p_prev is None or bv is None or bv_prev is None or c is None:
            if day < last or p is not None or c is not None:
                missing.append(day)
            continue
        sb = bv - bv_prev + cash.get(day, 0.0)
        pp = p - p_prev
        ex = pp - sb
        start_book = sb / t0_eq
        residual = (ex - c) / t0_eq
        rows.append(
            {
                "day": day,
                "p_pnl": pp,
                "p_start_book_pnl": sb,
                "p_ex_start_pnl": ex,
                "c_pnl": c,
                "ti_pnl": {n: s[day] for n, s in t_pnl.items() if day in s},
                "d_p_c": start_book + residual,
                "start_book": start_book,
                "residual": residual,
                "p_open": p_open.get(day),
                "c_open": c_open.get(day),
            }
        )
    flags = ramp_flags([r["p_open"] for r in rows], [r["c_open"] for r in rows])  # type: ignore[misc]
    sessions = [
        ProductionSession(**r, ramped=f)  # type: ignore[arg-type]
        for r, f in zip(rows, flags, strict=True)
    ]

    # -- start book ------------------------------------------------------------------
    closes = _close_dates(conn, book)
    cum = 0.0
    sb_days: list[StartBookDay] = []
    for s in sessions:
        cum += s.p_start_book_pnl
        held = marks.get(s.day, {})
        sb_days.append(
            StartBookDay(
                day=s.day,
                open=sum(1 for i in book if i in held),
                closed=sum(1 for d in closes.values() if d is not None and d <= s.day),
                pnl=s.p_start_book_pnl,
                cum_pnl=cum,
            )
        )

    # -- descriptive arm - production CIs ------------------------------------------
    q = cfg.stats.sigma_upper_q

    def compare(arm: str, pnl: dict[_dt.date, float], basis: CompareBasis) -> ProductionCompare:
        if basis == "ex_start_book":
            d = [(pnl[s.day] - s.p_ex_start_pnl) / t0_eq for s in sessions if s.day in pnl]
        else:
            d = [
                (pnl[s.day] - s.p_pnl) / t0_eq
                for s in sessions
                if s.day in pnl and (basis == "total" or s.ramped)
            ]
        prim = ev._primary_from(  # noqa: SLF001
            d, alpha=sp.alpha, known=None, sigma_upper_q=q, mde=sp.mde,  # type: ignore[arg-type]
            min_sessions=sp.min_sessions,  # type: ignore[arg-type]
        )  # fmt: skip
        return ProductionCompare(
            arm=arm, basis=basis, n=prim.n, mean=prim.mean, ci=prim.ci, p_value=prim.p_value
        )

    bases: tuple[CompareBasis, ...] = ("total", "ex_start_book", "post_ramp")
    arms = {ev.CONTROL_ARM: {s.day: s.c_pnl for s in sessions}, **t_pnl}
    return ProductionPanel(
        ramped_from=next((s.day for s in sessions if s.ramped), None),
        sessions=sessions,
        missing_sessions=missing,
        cum_p_pnl=sum(s.p_pnl for s in sessions),
        cum_c_pnl=sum(s.c_pnl for s in sessions),
        cum_d_p_c=sum(s.d_p_c for s in sessions),
        cum_start_book=sum(s.start_book for s in sessions),
        cum_residual=sum(s.residual for s in sessions),
        start_book=StartBook(ids=book, close_dates=closes, days=sb_days, cum_pnl=cum),
        compare=[compare(a, pnl, b) for a, pnl in arms.items() for b in bases],
    )
