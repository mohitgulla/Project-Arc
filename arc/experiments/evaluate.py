"""Daily evaluation of running experiments (PLAN D44, card E10.3).

Deterministic and read-only towards everything but the experiment tables: no LLM,
no broker, no network. The caller injects ``now``.

**Series.** For each ET session ``t`` after t0 that both arms have an EOD
``pnl_snapshots`` row for (the reconcile's latest row per ``details_json.day``,
by ``arm_id``; NULL = control)::

    treat_pnl_t = virtual_t - virtual_{t-1}          (t0_equity before the first session)
    ctrl_pnl_t  = equity_t - equity_{t-1} - legacy_pnl_t
    d_t         = (treat_pnl_t - ctrl_pnl_t) / t0_equity

``legacy_pnl_t`` is the day's change in value of control's legacy book (the
structures open at t0, :attr:`RunningDetail.legacy_book`): the market value of
their legs in control's EOD ``positions_snapshots`` plus the cash from any of
their closes, so the legacy book is excluded from control's series. The
treatment arm never holds the legacy book (its virtual account opens flat).
A session missing for either arm (or its previous session, or a needed legacy
mark) is left out and listed in ``missing_sessions``.

**Arm equity basis (contract with the E10.2 runner).** Control's equity is
``details_json.equity`` (its broker equity). The treatment arm's is
``details_json.virtual_equity`` *only*: the arm's virtual account (E10.2), which
by definition equals ``RunningDetail.t0_equity`` at t0 and then moves only with
the arm's own P&L. The experiment paper account's broker equity is never reset
(e.g. $95k broker vs a $10k control), so a treatment row's ``equity`` is never
read and never used as a fallback: a treatment row without ``virtual_equity`` is
treated as absent and its session is listed in ``missing_sessions``. That is what
makes ``t0_equity`` a valid previous value before the arm's first EOD row.

**Verdict** (first that applies):

Owner decision (2026-10-03, PR #102): the evaluation keeps the primary and
secondary metrics only; there are no guardrail (harm) stops. Harm is watched by
the owner through the report's per-arm drawdown / worst day / orders, which are
reported but never decide. The owner can still stop an experiment by hand.

1. ``invalid``: an A/A whose always-valid CI excludes 0 (the arms differ when
   they should not: the harness is broken) -> stop(invalid), alert.
2. ``win`` (ab only): ``n >= min_sessions``, CI lower bound > 0 *and* Sortino
   non-inferior within the spec's margin -> stop(win).
3. ``futility``: ``n >= max_sessions`` without a win -> stop(futility). For an
   A/A that is the normal end; its sigma is recorded on the stop event, which
   unlocks ab starts (:meth:`ExperimentStore.aa_sigma`).
4. ``continue``.

Every evaluation appends one :class:`ExperimentReport` to ``experiment_reports``
(canonical JSON + sha256 + spec/config hashes + shas) and journals
``experiment:evaluated``. Breakdowns by regime and structure kind are reported
only; nothing reads them for a decision.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db
from arc.experiments import stats
from arc.experiments.models import (
    CONTROL_ARM,
    ExperimentKind,
    ExperimentState,
    ExperimentStatus,
    StopDetail,
    StopReason,
    arm_id,
)
from arc.experiments.store import ExperimentError
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.utils.calendar import (
    ET,
    is_session,
    next_session,
    previous_session,
    session_open,
    sessions_between,
)

if TYPE_CHECKING:
    import sqlite3

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.store import ExperimentStore

__all__ = [
    "EVALUATOR_ACTOR",
    "REPORT_VERSION",
    "ArmSummary",
    "BreakdownRow",
    "Calibration",
    "ExperimentReport",
    "Primary",
    "Secondary",
    "SessionRow",
    "Verdict",
    "build_report",
    "evaluate",
    "evaluate_running",
    "latest_report",
    "store_report",
]

log = structlog.get_logger(__name__)

REPORT_VERSION = 1
EVALUATOR_ACTOR = "arc.experiments"
TREATMENT_ARM = "treatment"
# the treatment arm's pnl_snapshots.details_json key the evaluator reads (module
# docstring: equals t0_equity at t0; the broker ``equity`` is never a fallback)
TREATMENT_EQUITY_FIELD = "virtual_equity"
MULTIPLIER = Decimal(100)
_FORBID = ConfigDict(extra="forbid", frozen=True)

Verdict = Literal["continue", "win", "futility", "invalid"]
_STOP: dict[str, StopReason] = {
    "win": StopReason.WIN,
    "futility": StopReason.FUTILITY,
    "invalid": StopReason.INVALID,
}


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


class SessionRow(BaseModel):
    model_config = _FORBID

    day: _dt.date
    control_equity: float
    treatment_equity: float = Field(..., description="Treatment arm's virtual equity ($)")
    control_pnl: float = Field(..., description="Day P&L, legacy book excluded ($)")
    legacy_pnl: float = Field(..., description="Legacy book's day P&L removed from control ($)")
    treatment_pnl: float
    d: float = Field(..., description="(treatment_pnl - control_pnl) / t0_equity")


class Primary(BaseModel):
    """Paired daily net P&L difference (fraction of t0 equity) and its always-valid CI."""

    model_config = _FORBID

    metric: Literal["paired_daily_net_pnl_pct"] = "paired_daily_net_pnl_pct"
    n: int
    mean: float | None
    ci: stats.Interval | None = Field(None, description="Always-valid (mSPRT) two-sided CI")
    sigma: float | None
    sigma_source: Literal["aa", "running_corrected"] | None
    tau: float | None = Field(None, description="Normal-mixture scale (spec mde or MDE@min)")
    p_value: float | None = Field(
        None,
        description="Always-valid mSPRT p of no difference "
        "(dual of ci: p < alpha iff ci excludes 0)",
    )
    lower_bound_positive: bool = False


class Secondary(BaseModel):
    model_config = _FORBID

    metric: Literal["sortino"] = "sortino"
    sortino_control: float | None
    sortino_treatment: float | None
    diff_ci: stats.Interval | None = Field(
        None, description="Paired bootstrap CI of treatment - control (level 1 - 2 alpha)"
    )
    margin: float | None = Field(None, description="Non-inferiority margin (spec)")
    p_value: float | None = Field(
        None,
        description="Bootstrap p (same resamples as diff_ci): one-sided H0 diff <= -margin; "
        "two-sided vs 0 when there is no margin (aa)",
    )
    non_inferior: bool | None = Field(None, description="None for aa (no margin)")


class ArmSummary(BaseModel):
    """Per-arm numbers from t0 (reported for the owner; no verdict reads them)."""

    model_config = _FORBID

    arm: str
    arm_id: str | None
    sessions: int
    total_pnl: float
    max_drawdown: float
    worst_day: float | None = Field(None, description="Worst daily return (fraction)")
    orders: int
    filled_executions: int
    executions: int
    mean_slippage_bps: float | None


class Calibration(BaseModel):
    """What an A/A measures (reported for every experiment; decisive for none)."""

    model_config = _FORBID

    sigma: float | None = Field(None, description="Sample sd of d_t (fraction of t0 equity)")
    mde_fixed: dict[int, float] = Field(
        default_factory=dict, description="sessions -> 2.8 sigma / sqrt(n)"
    )
    mde_always_valid: dict[int, float] = Field(
        default_factory=dict, description="sessions -> effect detected with `power` (peeking)"
    )
    slippage_gap_bps: float | None = Field(None, description="treatment - control mean slippage")
    fill_rate_gap: float | None = Field(None, description="treatment - control fill rate")
    paired_chains: int = 0
    divergent_chains: int = 0
    llm_divergence_rate: float | None = Field(
        None, description="Share of paired chains whose arm decisions differ"
    )


class BreakdownRow(BaseModel):
    model_config = _FORBID

    by: Literal["regime", "structure_kind"]
    key: str
    arm: str
    trades: int
    realised_pnl: float


class ExperimentReport(BaseModel):
    """One evaluation of one experiment (stored append-only in ``experiment_reports``)."""

    model_config = _FORBID

    report_version: Literal[1] = REPORT_VERSION
    experiment_id: str
    kind: ExperimentKind
    area: str
    status: ExperimentStatus = Field(..., description="Status when evaluated")
    evaluated_at: _dt.datetime
    t0: _dt.datetime
    t0_equity: float
    legacy_book: list[str]
    as_of_day: _dt.date | None
    sessions: int
    min_sessions: int
    max_sessions: int
    alpha: float
    power: float
    series: list[SessionRow]
    missing_sessions: list[_dt.date]
    primary: Primary
    secondary: Secondary
    arms: list[ArmSummary]
    calibration: Calibration
    breakdowns: list[BreakdownRow]
    verdict: Verdict
    verdict_reason: str
    spec_hash: str
    registered_hash: str | None
    config_hash: str
    config: dict[str, Any] = Field(..., description="Effective experiments config used")
    control_sha: str
    treatment_sha: str | None
    evaluator_sha: str | None

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def report_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _f(v: object) -> float | None:
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return float(d) if d.is_finite() else None


def _arm_clause(aid: str | None, col: str = "arm_id") -> tuple[str, tuple[object, ...]]:
    return (f"{col} IS NULL", ()) if aid is None else (f"{col} = ?", (aid,))


def _eod_equity(
    conn: sqlite3.Connection, aid: str | None, field: str = "equity"
) -> dict[_dt.date, float]:
    """ET day -> *field* of the arm's latest ``pnl_snapshots`` row that day.

    A row without a finite *field* is skipped (the day stays absent; no fallback).
    """
    where, args = _arm_clause(aid)
    rows = conn.execute(
        f"""SELECT details_json FROM pnl_snapshots
            WHERE {where} AND json_extract(details_json, '$.day') IS NOT NULL
            ORDER BY snapshot_at, rowid""",  # noqa: S608 - fixed column clause
        args,
    ).fetchall()
    out: dict[_dt.date, float] = {}
    for r in rows:
        d = json.loads(r[0])
        eq = _f(d.get(field))
        if eq is not None:
            out[_dt.date.fromisoformat(d["day"])] = eq
    return out


def _legacy_marks(conn: sqlite3.Connection, legacy: set[str]) -> dict[_dt.date, dict[str, float]]:
    """ET day -> {legacy structure id: market value of its legs} from control's EOD snapshot."""
    if not legacy:
        return {}
    rows = conn.execute(
        """SELECT positions_json FROM positions_snapshots
           WHERE arm_id IS NULL AND json_extract(positions_json, '$.day') IS NOT NULL
           ORDER BY snapshot_at, rowid"""
    ).fetchall()
    out: dict[_dt.date, dict[str, float]] = {}
    for r in rows:
        snap = json.loads(r[0])
        broker = {
            b["symbol"]: (_f(b.get("qty")) or 0.0, _f(b.get("market_value")) or 0.0)
            for b in snap.get("broker", [])
        }
        marks: dict[str, float] = {}
        for s in snap.get("structures", []):
            sid = s.get("structure_id")
            if sid not in legacy or not s.get("held"):
                continue
            value = 0.0
            for sym, qty in (s.get("legs") or {}).items():
                bq, mv = broker.get(sym, (0.0, 0.0))
                if bq:
                    value += mv * min(1.0, abs(float(qty)) / abs(bq))
            marks[sid] = value
        out[_dt.date.fromisoformat(snap["day"])] = marks
    return out


def _legacy_cash(conn: sqlite3.Connection, legacy: set[str]) -> dict[_dt.date, float]:
    """ET day -> cash from closing legacy structures that day (close fills + expiries)."""
    out: dict[_dt.date, float] = defaultdict(float)
    if not legacy:
        return out
    marks = ",".join("?" * len(legacy))
    covered: dict[str, int] = defaultdict(int)
    for r in conn.execute(
        f"""SELECT structure_id, fill_price, filled_qty, finished_at FROM executions
            WHERE kind = 'close' AND structure_id IN ({marks}) AND filled_qty > 0
              AND fill_price IS NOT NULL AND finished_at IS NOT NULL""",  # noqa: S608
        tuple(sorted(legacy)),
    ):
        px = Decimal(r["fill_price"])
        out[from_db(r["finished_at"]).date()] += float(-px * MULTIPLIER * int(r["filled_qty"]))
        covered[r["structure_id"]] += int(r["filled_qty"])
    for r in conn.execute(
        f"""SELECT id, contracts, close_net, closed_at FROM open_structures
            WHERE status = 'closed' AND id IN ({marks})
              AND close_net IS NOT NULL AND closed_at IS NOT NULL""",  # noqa: S608
        tuple(sorted(legacy)),
    ):
        if covered.get(r["id"]):
            continue  # closed by a filled close order, already counted
        px = Decimal(r["close_net"])  # expiry settle: no execution row
        out[from_db(r["closed_at"]).date()] += float(-px * MULTIPLIER * int(r["contracts"]))
    return out


def _first_session(t0: _dt.datetime) -> _dt.date:
    """The first ET session that opens at or after t0 (a mid-session t0 skips that day)."""
    day = t0.astimezone(ET).date()
    if is_session(day) and session_open(day) >= t0:
        return day
    return next_session(day)


def _series(
    conn: sqlite3.Connection, st: ExperimentState, *, now: _dt.datetime
) -> tuple[list[SessionRow], list[_dt.date]]:
    run = st.running
    assert run is not None  # noqa: S101 - caller checks
    t_arm = arm_id(st.experiment_id, TREATMENT_ARM)
    ctrl = _eod_equity(conn, None)
    treat = _eod_equity(conn, t_arm, TREATMENT_EQUITY_FIELD)
    legacy = set(run.legacy_book)
    marks = _legacy_marks(conn, legacy)
    cash = _legacy_cash(conn, legacy)
    first = _first_session(run.t0)
    last = now.astimezone(ET).date()
    days = sessions_between(first, last)
    t0_eq = run.t0_equity

    def legacy_value(day: _dt.date) -> float | None:
        """Mark of the legacy structures still held at EOD *day* (None = no snapshot)."""
        if not legacy:
            return 0.0
        m = marks.get(day)
        return None if m is None else sum(m.values())

    rows: list[SessionRow] = []
    missing: list[_dt.date] = []
    for day in days:
        prev = previous_session(day)
        c, c_prev, t = ctrl.get(day), ctrl.get(prev), treat.get(day)
        # the arm's virtual equity is t0_equity at t0 by definition (module
        # docstring), so before its first EOD row the previous value is t0's
        t_prev = treat.get(prev, t0_eq if day == first else None)
        lv, lv_prev = legacy_value(day), legacy_value(prev)
        if None in (c, c_prev, t, t_prev, lv, lv_prev):
            if day < last or t is not None or c is not None:
                missing.append(day)
            continue
        assert c is not None and c_prev is not None and t is not None and t_prev is not None  # noqa: S101
        assert lv is not None and lv_prev is not None  # noqa: S101
        leg = lv - lv_prev + cash.get(day, 0.0)
        c_pnl = c - c_prev - leg
        t_pnl = t - t_prev
        rows.append(
            SessionRow(
                day=day,
                control_equity=c,
                treatment_equity=t,
                control_pnl=c_pnl,
                legacy_pnl=leg,
                treatment_pnl=t_pnl,
                d=(t_pnl - c_pnl) / t0_eq,
            )
        )
    # an unfinished today (no EOD row yet for either arm) is not "missing"
    return rows, missing


def _orders(
    conn: sqlite3.Connection, aid: str | None, t0: _dt.datetime, legacy: set[str]
) -> tuple[int, int, int]:
    """(orders sent, filled executions, executions) of the arm since t0, legacy excluded."""
    where, args = _arm_clause(aid)
    sent = filled = n = 0
    for r in conn.execute(
        f"""SELECT structure_id, attempts, status FROM executions
            WHERE {where} AND started_at >= ?""",  # noqa: S608
        (*args, to_db(t0)),
    ):
        if r["structure_id"] in legacy:
            continue
        n += 1
        sent += int(r["attempts"] or 0)
        filled += r["status"] in ("filled", "partially_filled")
    return sent, filled, n


def _closed_outcomes(
    conn: sqlite3.Connection, aid: str | None, t0: _dt.datetime, legacy_hashes: set[str]
) -> list[dict[str, Any]]:
    """Latest outcome per proposal for the arm since t0 (legacy excluded), with regime/kind."""
    where, args = _arm_clause(aid, "o.arm_id")
    rows = conn.execute(
        f"""SELECT o.proposal_hash, o.status, o.realised_pnl, o.slippage_bps, o.at,
                   p.regime, p.structure_json
            FROM outcomes o LEFT JOIN proposals p ON p.proposal_hash = o.proposal_hash
            WHERE {where} AND o.at >= ?
            ORDER BY o.at, o.rowid""",  # noqa: S608
        (*args, to_db(t0)),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["proposal_hash"] in legacy_hashes:
            continue
        latest[r["proposal_hash"]] = dict(r)
    return list(latest.values())


def _structure_kind(structure_json: str | None) -> str:
    if not structure_json:
        return "unknown"
    try:
        return str(json.loads(structure_json).get("kind") or "unknown")
    except (ValueError, AttributeError):
        return "unknown"


def _legacy_hashes(conn: sqlite3.Connection, legacy: set[str]) -> set[str]:
    if not legacy:
        return set()
    marks = ",".join("?" * len(legacy))
    return {
        r[0]
        for r in conn.execute(
            f"SELECT open_proposal_hash FROM open_structures WHERE id IN ({marks})",  # noqa: S608
            tuple(sorted(legacy)),
        )
    }


def _divergence(conn: sqlite3.Connection, t_arm: str, t0: _dt.datetime) -> tuple[int, int]:
    """(paired chains, chains whose decisions differ) from the arm's manifests.

    The treatment runner (E10.2) records ``paired_chain_run_id`` in each arm
    manifest's payload; a pair diverges when the multiset of
    ``(persona, stage, subject, choice)`` decisions differs between the chains.
    """
    pairs = {
        (r[0], r[1])
        for r in conn.execute(
            """SELECT chain_run_id, json_extract(payload, '$.paired_chain_run_id')
               FROM run_manifests
               WHERE arm_id = ? AND created_at >= ? AND chain_run_id IS NOT NULL
                 AND json_extract(payload, '$.paired_chain_run_id') IS NOT NULL""",
            (t_arm, to_db(t0)),
        )
    }

    def decided(chain: str, aid: str | None) -> Counter[tuple[str, ...]]:
        where, args = _arm_clause(aid)
        return Counter(
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                f"""SELECT persona, stage, subject, choice FROM decisions
                    WHERE chain_run_id = ? AND {where}""",  # noqa: S608
                (chain, *args),
            )
        )

    diverged = sum(decided(t, t_arm) != decided(c, None) for t, c in pairs)
    return len(pairs), diverged


def _treatment_sha(conn: sqlite3.Connection, t_arm: str) -> str | None:
    r = conn.execute(
        """SELECT json_extract(payload, '$.git_sha') FROM run_manifests
           WHERE arm_id = ? AND json_extract(payload, '$.git_sha') IS NOT NULL
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (t_arm,),
    ).fetchone()
    return r[0] if r else None


def _evaluator_sha() -> str | None:
    from arc.routines.manifest import _git

    return _git()[0]


def config_hash(cfg: ExperimentsConfig) -> str:
    blob = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _arm_summary(
    name: str,
    aid: str | None,
    pnl: list[float],
    t0_equity: float,
    orders: tuple[int, int, int],
    outcomes: list[dict[str, Any]],
) -> ArmSummary:
    """Drawdown and worst day on the arm's own curve from t0 (``t0_equity + cum P&L``).

    The curve, not the snapshot equity: control's equity carries the legacy book.
    The treatment's P&L is on the virtual-equity basis, so its curve is its
    virtual equity (never the broker equity with its reserved excess).
    """
    curve = t0_equity + np.concatenate([[0.0], np.cumsum(pnl)])
    before = curve[:-1]
    rets = [float(p / e) for p, e in zip(pnl, before, strict=True) if e > 0]
    slips = [o["slippage_bps"] for o in outcomes if o.get("slippage_bps") is not None]
    return ArmSummary(
        arm=name,
        arm_id=aid,
        sessions=len(pnl),
        total_pnl=float(sum(pnl)),
        max_drawdown=stats.max_drawdown(curve),
        worst_day=min(rets) if rets else None,
        orders=orders[0],
        filled_executions=orders[1],
        executions=orders[2],
        mean_slippage_bps=float(sum(slips) / len(slips)) if slips else None,
    )


def _breakdowns(arms: dict[str, list[dict[str, Any]]]) -> list[BreakdownRow]:
    out: list[BreakdownRow] = []
    closed = ("closed", "expired_worthless")
    for by in ("regime", "structure_kind"):
        agg: dict[tuple[str, str], list[float]] = defaultdict(list)
        for arm, rows in arms.items():
            for r in rows:
                if r["status"] not in closed:
                    continue
                key = (
                    (r.get("regime") or "unknown")
                    if by == "regime"
                    else _structure_kind(r.get("structure_json"))
                )
                agg[(key, arm)].append(_f(r.get("realised_pnl")) or 0.0)
        out.extend(
            BreakdownRow(by=by, key=k, arm=a, trades=len(v), realised_pnl=float(sum(v)))  # type: ignore[arg-type]
            for (k, a), v in sorted(agg.items())
        )
    return out


def build_report(
    conn: sqlite3.Connection,
    st: ExperimentState,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    aa_sigma: float | None = None,
) -> ExperimentReport:
    """Evaluate *st* (running, or stopped with a t0) as of *now*. Read-only."""
    sp, run = st.spec, st.running
    if run is None:
        msg = f"{st.experiment_id} has no t0 (never started): nothing to evaluate"
        raise ExperimentError(msg)
    assert sp.alpha is not None and sp.power is not None  # noqa: S101 - registered spec
    assert sp.min_sessions is not None and sp.max_sessions is not None  # noqa: S101
    t_arm = arm_id(st.experiment_id, TREATMENT_ARM)
    assert t_arm is not None  # noqa: S101
    legacy = set(run.legacy_book)
    legacy_hashes = _legacy_hashes(conn, legacy)
    rows, missing = _series(conn, st, now=now)
    d = [r.d for r in rows]
    n = len(d)
    is_aa = sp.kind is ExperimentKind.AA

    # -- primary -------------------------------------------------------------
    known = None if is_aa else aa_sigma
    ci, sigma, tau = stats.confidence_sequence(
        d,
        alpha=sp.alpha,
        sigma=known,
        sigma_upper_q=cfg.stats.sigma_upper_q,
        mde=sp.mde,
        min_sessions=sp.min_sessions,
    )
    primary = Primary(
        n=n,
        mean=float(sum(d) / n) if n else None,
        ci=ci,
        sigma=sigma,
        sigma_source=(
            None if sigma is None else ("aa" if known is not None else "running_corrected")
        ),
        tau=tau,
        p_value=(
            None
            if ci is None or sigma is None or tau is None
            else stats.msprt_p_value(n, ci.estimate, sigma, tau)
        ),
        lower_bound_positive=ci is not None and ci.lo > 0.0,
    )

    # -- arms, secondary -----------------------------------------------------
    c_pnl = [r.control_pnl for r in rows]
    t_pnl = [r.treatment_pnl for r in rows]
    c_out = _closed_outcomes(conn, None, run.t0, legacy_hashes)
    t_out = _closed_outcomes(conn, t_arm, run.t0, legacy_hashes)
    ctrl = _arm_summary(
        CONTROL_ARM, None, c_pnl, run.t0_equity,
        _orders(conn, None, run.t0, legacy), c_out,
    )  # fmt: skip
    treat = _arm_summary(
        TREATMENT_ARM, t_arm, t_pnl, run.t0_equity,
        _orders(conn, t_arm, run.t0, set()), t_out,
    )  # fmt: skip
    # Sortino on returns vs t0 equity (same denominator as d_t, so the arms compare 1:1)
    c_r = [p / run.t0_equity for p in c_pnl]
    t_r = [p / run.t0_equity for p in t_pnl]
    diff_ci = stats.sortino_diff_ci(
        t_r,
        c_r,
        level=1.0 - 2.0 * sp.alpha,
        resamples=cfg.stats.bootstrap_resamples,
        seed=stats.seed_for(st.experiment_id, n),
    )
    margin = sp.non_inferiority_margin
    secondary = Secondary(
        sortino_control=stats.sortino(c_r),
        sortino_treatment=stats.sortino(t_r),
        diff_ci=diff_ci,
        margin=margin,
        p_value=stats.sortino_diff_p(
            t_r,
            c_r,
            margin=margin,
            resamples=cfg.stats.bootstrap_resamples,
            seed=stats.seed_for(st.experiment_id, n),
        ),
        non_inferior=None if margin is None else stats.non_inferior(diff_ci, margin),
    )

    # -- calibration ---------------------------------------------------------
    sd = stats.sample_sd(d) if n >= 2 else None
    horizons = sorted(
        {cfg.defaults.min_sessions, cfg.defaults.max_sessions, sp.min_sessions, sp.max_sessions}
    )
    paired, diverged = _divergence(conn, t_arm, run.t0)
    fill = [(a.filled_executions / a.executions) if a.executions else None for a in (ctrl, treat)]
    cal = Calibration(
        sigma=sd or None,
        mde_fixed={h: stats.mde_fixed(sd, h) for h in horizons} if sd else {},
        mde_always_valid=(
            {
                h: stats.mde_always_valid(
                    sd,
                    h,
                    tau=stats.mixing_tau(sd, mde=sp.mde, min_sessions=sp.min_sessions),
                    alpha=sp.alpha,
                    power=sp.power,
                )
                for h in horizons
            }
            if sd
            else {}
        ),
        slippage_gap_bps=(
            treat.mean_slippage_bps - ctrl.mean_slippage_bps
            if treat.mean_slippage_bps is not None and ctrl.mean_slippage_bps is not None
            else None
        ),
        fill_rate_gap=fill[1] - fill[0] if None not in fill else None,  # type: ignore[operator]
        paired_chains=paired,
        divergent_chains=diverged,
        llm_divergence_rate=diverged / paired if paired else None,
    )

    # -- verdict -------------------------------------------------------------
    verdict, why = _verdict(
        is_aa=is_aa,
        n=n,
        min_sessions=sp.min_sessions,
        max_sessions=sp.max_sessions,
        primary=primary,
        secondary=secondary,
    )
    return ExperimentReport(
        experiment_id=st.experiment_id,
        kind=sp.kind,
        area=sp.area.value,
        status=st.status,
        evaluated_at=now,
        t0=run.t0,
        t0_equity=run.t0_equity,
        legacy_book=sorted(legacy),
        as_of_day=rows[-1].day if rows else None,
        sessions=n,
        min_sessions=sp.min_sessions,
        max_sessions=sp.max_sessions,
        alpha=sp.alpha,
        power=sp.power,
        series=rows,
        missing_sessions=missing,
        primary=primary,
        secondary=secondary,
        arms=[ctrl, treat],
        calibration=cal,
        breakdowns=_breakdowns({CONTROL_ARM: c_out, TREATMENT_ARM: t_out}),
        verdict=verdict,
        verdict_reason=why,
        spec_hash=st.spec_hash,
        registered_hash=st.registered_hash,
        config_hash=config_hash(cfg),
        config=cfg.model_dump(mode="json"),
        control_sha=run.control_sha,
        treatment_sha=_treatment_sha(conn, t_arm),
        evaluator_sha=_evaluator_sha(),
    )


def _verdict(
    *,
    is_aa: bool,
    n: int,
    min_sessions: int,
    max_sessions: int,
    primary: Primary,
    secondary: Secondary,
) -> tuple[Verdict, str]:
    ci = primary.ci
    if is_aa and ci is not None and ci.excludes_zero:
        return "invalid", (
            f"A/A arms differ: always-valid CI [{ci.lo:+.4%}, {ci.hi:+.4%}] excludes 0 "
            f"after {n} sessions; the harness is broken"
        )
    if not is_aa and n >= min_sessions and primary.lower_bound_positive:
        if secondary.non_inferior:
            assert ci is not None  # noqa: S101
            return "win", (
                f"primary CI [{ci.lo:+.4%}, {ci.hi:+.4%}] > 0 after {n} sessions and "
                f"Sortino non-inferior (margin {secondary.margin})"
            )
        if n < max_sessions:
            return "continue", "primary CI > 0 but Sortino not yet shown non-inferior"
    if n >= max_sessions:
        if is_aa:
            return "futility", f"A/A complete after {n} sessions (no difference, as expected)"
        return "futility", f"max_sessions {max_sessions} reached without a win"
    lo = f"{ci.lo:+.4%}" if ci else "-"
    return "continue", f"{n}/{min_sessions}-{max_sessions} sessions; CI lower bound {lo}"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def store_report(
    conn: sqlite3.Connection, report: ExperimentReport, *, run_id: str | None = None
) -> int:
    """Append *report* to ``experiment_reports`` (no commit). Returns the row id."""
    payload = report.canonical_json()
    cur = conn.execute(
        """INSERT INTO experiment_reports
           (experiment_id, report_version, evaluated_at, as_of_day, sessions, verdict,
            spec_hash, config_hash, control_sha, treatment_sha, evaluator_sha, payload,
            report_hash, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            report.experiment_id,
            report.report_version,
            to_db(report.evaluated_at),
            report.as_of_day.isoformat() if report.as_of_day else None,
            report.sessions,
            report.verdict,
            report.spec_hash,
            report.config_hash,
            report.control_sha,
            report.treatment_sha,
            report.evaluator_sha,
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
            run_id,
        ),
    )
    return int(cur.lastrowid or 0)


def latest_report(conn: sqlite3.Connection, experiment_id: str) -> ExperimentReport | None:
    r = conn.execute(
        """SELECT payload FROM experiment_reports WHERE experiment_id = ?
           ORDER BY id DESC LIMIT 1""",
        (experiment_id,),
    ).fetchone()
    return ExperimentReport.model_validate_json(r[0]) if r else None


def evaluate(
    store: ExperimentStore,
    experiment_id: str,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    run_id: str | None = None,
) -> ExperimentReport:
    """Evaluate a running experiment, store the report and apply its verdict (stop)."""
    conn = store.conn
    st = store.require(experiment_id)
    if st.status is not ExperimentStatus.RUNNING:
        msg = f"{experiment_id} is {st.status.value}, not running"
        raise ExperimentError(msg)
    report = build_report(conn, st, cfg, now=now, aa_sigma=store.aa_sigma())
    journal = JournalStore(conn)
    with conn:
        rid = store_report(conn, report, run_id=run_id)
        journal.record(
            persona=JournalPersona.SYSTEM,
            stage=Stage.EXPERIMENT,
            subject=experiment_id,
            choice=Choice.NOTED,
            reason_code=ReasonCode.EXPERIMENT_EVALUATED,
            reason_text=f"{report.verdict}: {report.verdict_reason}"[:2000],
            at=now,
            run_id=run_id,
            payload={
                "experiment_id": experiment_id,
                "report_id": rid,
                "verdict": report.verdict,
                "sessions": report.sessions,
                "mean": report.primary.mean,
                "ci": None if report.primary.ci is None else report.primary.ci.model_dump(),
                "spec_hash": report.spec_hash,
                "config_hash": report.config_hash,
            },
        )
        if report.verdict == "invalid":
            journal.record(
                persona=JournalPersona.SYSTEM,
                stage=Stage.EXPERIMENT,
                subject=experiment_id,
                choice=Choice.FAILED,
                reason_code=ReasonCode.EXPERIMENT_INVALID,
                reason_text=report.verdict_reason[:2000],
                at=now,
                run_id=run_id,
                payload={"experiment_id": experiment_id, "report_id": rid},
            )
    if report.verdict != "continue":
        sigma = report.calibration.sigma if report.kind is ExperimentKind.AA else None
        store.stop(
            experiment_id,
            _STOP[report.verdict],
            actor=EVALUATOR_ACTOR,
            detail=StopDetail(
                sigma=sigma if report.verdict == "futility" and sigma else None,
                sessions=report.sessions,
                note=report.verdict_reason[:500],
            ),
        )
    log.info(
        "experiments.evaluated",
        experiment_id=experiment_id,
        verdict=report.verdict,
        sessions=report.sessions,
        mean=report.primary.mean,
    )
    return report


def evaluate_running(
    store: ExperimentStore,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    run_id: str | None = None,
) -> list[ExperimentReport]:
    """Evaluate every running experiment (oldest first)."""
    return [
        evaluate(store, s.experiment_id, cfg, now=now, run_id=run_id)
        for s in store.all(status=ExperimentStatus.RUNNING)
    ]
