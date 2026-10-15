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

**Regime tag (D108 (c), card E25.5).** Every session carries ``regime``: the SPY regime
v2 read (D77) from the ``regime`` context entry visible at that session's close (trend
label, vol state, run length / margin z and the ``transitional`` flag at the E17.2
reference setting run 3 / margin z 0.10), or ``None`` when no entry was visible (the
regime panel buckets it ``unknown``). Each session also carries control's ramp:
``c_open`` and ``c_deployed`` (max-loss capital of its held structures as a fraction of
its virtual equity). :mod:`arc.experiments.regime_panel` pools these across XPs.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
import json
import math
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
    "REGIME_SUBJECT",
    "TRANSITIONAL_MIN_MARGIN_Z",
    "TRANSITIONAL_MIN_RUN",
    "ProductionCompare",
    "ProductionPanel",
    "ProductionSession",
    "SessionRegime",
    "StartBook",
    "StartBookDay",
    "build_production_panel",
    "parse_regime",
    "ramp_flags",
    "regime_at",
]

#: D108: trailing sessions whose median open-structure counts decide ``ramped``.
RAMP_WINDOW = 5
#: E25.5: the market regime every session is tagged with (the market guard's subject).
REGIME_SUBJECT = "SPY"
#: E25.5: ``transitional`` at the E17.2 named reference setting (run 3 / margin z 0.10,
#: ``arc.backtest.regime_compare.reference_settings``), never the shipped-off guard
#: defaults (run 1 / z 0, which flag nothing).
TRANSITIONAL_MIN_RUN = 3
TRANSITIONAL_MIN_MARGIN_Z = 0.10
_FORBID = ConfigDict(extra="forbid", frozen=True)

CompareBasis = Literal["total", "ex_start_book", "post_ramp"]
Trend = Literal["bull", "bear", "sideways"]
VolState = Literal["low", "mid", "high"]


class SessionRegime(BaseModel):
    """The SPY regime read visible at a session's close (D77 v2; v1 rows: trend only)."""

    model_config = _FORBID

    trend: Trend
    vol_state: VolState | None = Field(None, description="v2 realised-vol state; None on v1")
    transitional: bool | None = Field(
        None,
        description="run_length < 3 or margin_z < 0.10 (E17.2 reference); None without both",
    )
    run_length: int | None = None
    margin_z: float | None = None
    model: str | None = Field(None, description="Regime labelling model (v1 / v2)")
    as_of: str | None = Field(None, description="The read's as_of session")
    entry_id: str = Field(..., description="context_entries id the tag was read from")


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
    # -- E25.5 (D108 (c)): descriptive regime study inputs ------------------------
    c_deployed: float | None = Field(
        None,
        description="Control's held max-loss capital / its virtual equity at EOD; None = unknown",
    )
    regime: SessionRegime | None = Field(
        None, description="SPY regime at the session close; None = no entry (bucket unknown)"
    )


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
    regime_at_start: SessionRegime | None = Field(
        None, description="E25.5: SPY regime at the first session's close (the XP's start)"
    )


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


def _held_ids(conn: sqlite3.Connection, aid: str | None) -> dict[_dt.date, list[str]]:
    """ET day -> held structure ids in the arm's latest EOD positions snapshot that day."""
    from arc.experiments.evaluate import _arm_clause  # noqa: PLC0415 - cycle

    where, args = _arm_clause(aid)
    out: dict[_dt.date, list[str]] = {}
    for r in conn.execute(
        f"""SELECT positions_json FROM positions_snapshots
            WHERE {where} AND json_extract(positions_json, '$.day') IS NOT NULL
            ORDER BY snapshot_at, rowid""",  # noqa: S608 - fixed column clause
        args,
    ):
        snap = json.loads(r[0])
        out[_dt.date.fromisoformat(snap["day"])] = [
            str(s.get("structure_id")) for s in snap.get("structures", []) if s.get("held")
        ]
    return out


def _open_counts(conn: sqlite3.Connection, aid: str | None) -> dict[_dt.date, int]:
    """ET day -> held structures in the arm's latest EOD positions snapshot that day."""
    return {d: len(ids) for d, ids in _held_ids(conn, aid).items()}


def _deployed(
    conn: sqlite3.Connection,
    aid: str,
    held: dict[_dt.date, list[str]],
    equity: dict[_dt.date, float],
) -> dict[_dt.date, float | None]:
    """E25.5: ET day -> the arm's held max-loss capital / its equity that day.

    Per held structure: ``max_loss`` per contract (the open proposal's structure) x
    contracts still open at that day's end (filled opens - filled closes finished by
    then, from the arm's ``executions``). ``None`` when a held structure's max loss or
    the day's equity is unknown (never a silent 0)."""
    from arc.context.ttl import from_db
    from arc.experiments.evaluate import _arm_clause  # noqa: PLC0415 - cycle

    where, args = _arm_clause(aid, "e.arm_id")
    unit: dict[str, float | None] = {}
    fills: dict[str, list[tuple[_dt.date, int]]] = {}
    for r in conn.execute(
        f"""SELECT e.structure_id, e.kind, e.filled_qty, e.finished_at,
                   json_extract(p.structure_json, '$.max_loss')
            FROM executions e LEFT JOIN proposals p
              ON p.proposal_hash = e.proposal_hash AND p.arm_id IS e.arm_id
            WHERE {where} AND e.structure_id IS NOT NULL AND e.filled_qty > 0
              AND e.finished_at IS NOT NULL AND e.kind IN ('open', 'close')""",  # noqa: S608
        args,
    ):
        sid, kind, qty, at, max_loss = str(r[0]), r[1], int(r[2]), r[3], r[4]
        sign = 1 if kind == "open" else -1
        fills.setdefault(sid, []).append((from_db(at).date(), sign * qty))
        if kind == "open":
            try:
                ml = float(max_loss) if max_loss is not None else None
            except (TypeError, ValueError):
                ml = None
            unit[sid] = ml if ml is not None and math.isfinite(ml) else unit.get(sid)
    out: dict[_dt.date, float | None] = {}
    for day, ids in held.items():
        eq = equity.get(day)
        total: float | None = 0.0
        for sid in ids:
            ml = unit.get(sid)
            n = sum(q for d, q in fills.get(sid, []) if d <= day)
            if ml is None or n <= 0:
                total = None
                break
            total += ml * n  # type: ignore[operator]
        out[day] = None if total is None or not eq else total / eq
    return out


def parse_regime(entry_id: str, payload: dict[str, object]) -> SessionRegime | None:
    """The :class:`SessionRegime` of a stored ``regime`` payload (None if unreadable)."""
    reg = payload.get("regime")
    if not isinstance(reg, dict) or reg.get("current") not in ("bull", "bear", "sideways"):
        return None

    def num(v: object) -> float | None:
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            return None
        return float(v)

    run = num(reg.get("run_length"))
    margin = num(reg.get("margin_z"))
    vol = reg.get("vol_state")
    return SessionRegime(
        trend=reg["current"],
        vol_state=vol if vol in ("low", "mid", "high") else None,
        transitional=(
            None
            if run is None or margin is None
            else (run < TRANSITIONAL_MIN_RUN or margin < TRANSITIONAL_MIN_MARGIN_Z)
        ),
        run_length=int(run) if run is not None else None,
        margin_z=margin,
        model=str(reg["model"]) if reg.get("model") is not None else None,
        as_of=str(reg.get("as_of") or payload.get("as_of") or "") or None,
        entry_id=entry_id,
    )


def regime_at(
    conn: sqlite3.Connection, days: Sequence[_dt.date], *, subject: str = REGIME_SUBJECT
) -> dict[_dt.date, SessionRegime | None]:
    """ET session -> the *subject* ``regime`` entry valid at that session's close.

    Valid = ``valid_from <= close`` and not expired at the close, any status (a row
    superseded or expired since was still the read in force then); the latest
    ``valid_from`` wins. Read-only (no context snapshot is recorded)."""
    from arc.context.ttl import to_db
    from arc.utils.calendar import session_close

    out: dict[_dt.date, SessionRegime | None] = {}
    for day in days:
        at = to_db(session_close(day))
        row = conn.execute(
            """SELECT id, payload FROM context_entries
               WHERE kind = 'regime' AND subject = ? AND valid_from <= ?
                 AND (expires_at IS NULL OR expires_at >= ?)
               ORDER BY valid_from DESC, created_at DESC, rowid DESC LIMIT 1""",
            (subject, at, at),
        ).fetchone()
        reg = None
        if row is not None:
            try:
                payload = json.loads(row[1])
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                reg = parse_regime(str(row[0]), payload)
        out[day] = reg
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
    # D117: the same first session as the verdict series (v3 counts a partial t0 day);
    # production's own P&L that day runs from its previous close, so it includes the
    # pre-t0 minutes (descriptive panel only)
    first = ev._first_session(run.t0, partial=True)  # noqa: SLF001
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
    c_eq = ev._eod_equity(conn, control_arm, field)  # noqa: SLF001
    c_held = _held_ids(conn, control_arm)
    c_open = {d: len(ids) for d, ids in c_held.items()}
    c_deployed = _deployed(conn, control_arm, c_held, c_eq)
    regimes = regime_at(conn, days)  # E25.5: descriptive tag, read by no verdict

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
                "c_deployed": c_deployed.get(day),
                "regime": regimes.get(day),
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
        regime_at_start=regimes.get(first),
    )
