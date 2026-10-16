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
structures open at t0, :attr:`RunningDetail.production_book`): the market value of
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
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db
from arc.experiments import stats
from arc.experiments.covariate_check import CovariateCheck  # noqa: TC001 - pydantic field
from arc.experiments.models import (
    CONTROL_ARM,
    Analysis,
    ExperimentKind,
    ExperimentState,
    ExperimentStatus,
    StopDetail,
    StopReason,
    arm_id,
)
from arc.experiments.production import ProductionPanel  # noqa: TC001 - pydantic field type
from arc.experiments.store import ExperimentError
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.utils.calendar import (
    ET,
    is_session,
    next_session,
    previous_session,
    session_close,
    session_open,
    sessions_between,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from arc.experiments.arms import ArmIdentity
    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.covariate import BarsProvider
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
#: D69: one treatment arm's verdict in a multi-arm experiment (each arm stops alone).
ArmVerdict = Literal["continue", "win", "loss", "futility"]
_STOP: dict[str, StopReason] = {
    "win": StopReason.WIN,
    "futility": StopReason.FUTILITY,
    "invalid": StopReason.INVALID,
}


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


def _absent(v: object) -> bool:
    return v is None


class SessionRow(BaseModel):
    model_config = _FORBID

    day: _dt.date
    control_equity: float
    treatment_equity: float = Field(..., description="Treatment arm's virtual equity ($)")
    control_pnl: float = Field(..., description="Day P&L, legacy book excluded ($)")
    legacy_pnl: float = Field(..., description="Legacy book's day P&L removed from control ($)")
    treatment_pnl: float
    d: float = Field(..., description="(treatment_pnl - control_pnl) / t0_equity")
    spy_ret: float | None = Field(
        None,
        exclude_if=_absent,
        description="D119: SPY's return that session (close to close; partial first session "
        "t0 to close). Persisted so a re-evaluation reproduces the adjusted primary offline",
    )
    vix_chg: float | None = Field(
        None,
        exclude_if=_absent,
        description="D120: VIX close minus the previous session's close (vol points); "
        "absent on the D117 partial first session. Persisted like spy_ret",
    )


class Unadjusted(BaseModel):
    """D119: the D44 iid unadjusted primary, kept next to a pre-registered one."""

    model_config = _FORBID

    n: int
    mean: float | None
    ci: stats.Interval | None
    sigma: float | None
    sigma_source: Literal["aa", "running_corrected"] | None
    tau: float | None
    p_value: float | None


class Adjustment(BaseModel):
    """D119 (E25.7): the SPY regression of one paired series, ``d_t = a + b x_t + e_t``.

    ``pre_registered`` true: the spec's ``analysis`` block; the primary's ``mean`` /
    ``ci`` / ``p_value`` / ``sigma`` / ``tau`` are these numbers and the verdict reads
    them. False: descriptive only (a spec without ``analysis``, e.g. XP-1), computed
    with ``covariates: [spy_ret]`` and ``experiments.stats.hac_lag``; the verdict stays
    the D44 iid unadjusted one.
    """

    model_config = _FORBID

    pre_registered: bool
    status: Literal["fitted", "insufficient_n", "unadjusted"] = Field(
        ...,
        description="fitted = OLS on SPY; insufficient_n = fewer than 3 sessions with a SPY "
        "return or no SPY variance (plain mean); unadjusted = covariates []",
    )
    covariates: list[str] = Field(..., description="[spy_ret] = adjusted; [] = plain mean")
    n: int = Field(..., description="Sessions in the fit (those with a SPY return)")
    beta: float | None = Field(None, description="SPY beta of the daily difference (b)")
    beta_se: float | None = None
    r2: float | None = Field(None, description="Share of the diff's variance SPY explains")
    estimate: float | None = Field(
        None, description="Intercept a: the daily edge on a flat-market day (or the mean)"
    )
    ci: stats.Interval | None = Field(None, description="Always-valid (mSPRT) CI on estimate")
    p_value: float | None = None
    tau: float | None = None
    sigma_source: Literal["aa", "running_corrected"] | None = None
    covariate_missing: list[_dt.date] = Field(
        default_factory=list,
        description="Paired sessions without a SPY return: left out of the fit; an adjusted "
        "pre-registered verdict waits on them",
    )


class Hac(BaseModel):
    """D119: the Bartlett (Newey-West) long-run variance factor of the residuals."""

    model_config = _FORBID

    lag: int | None = Field(..., description="Bartlett lag L (sessions); None = iid")
    factor: float | None = Field(None, description="f used (>= 1; sigma_lr = ... sqrt(f))")
    factor_running: float | None = Field(None, description="f from the series itself")
    rho: list[float] = Field(default_factory=list, description="Residual rho_1..rho_L (raw)")
    source: stats.HacSource | None = Field(
        None,
        description="iid (no lag) | running (n >= 2L+2) | aa (the A/A's recorded f) | "
        "cap (n < 2L+2, no A/A f: hac_max_factor)",
    )


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
    # -- D119: omitted when absent (reports without a SPY series stay byte-identical) --
    adjustment: Adjustment | None = Field(
        None, exclude_if=_absent, description="D119: SPY regression (verdict iff pre_registered)"
    )
    hac: Hac | None = Field(None, exclude_if=_absent, description="D119: long-run factor")
    sigma_raw: float | None = Field(
        None, exclude_if=_absent, description="D119: sample sd of d (unadjusted)"
    )
    sigma_adj: float | None = Field(
        None, exclude_if=_absent, description="D119: plug-in residual sd sqrt(SSE / df)"
    )
    sigma_lr: float | None = Field(
        None,
        exclude_if=_absent,
        description="D119: long-run sigma of the estimate: base * sqrt(f) * intercept factor",
    )
    primary_unadjusted: Unadjusted | None = Field(
        None, exclude_if=_absent, description="D119: the D44 iid unadjusted primary"
    )
    covariate_check: CovariateCheck | None = Field(
        None,
        exclude_if=_absent,
        description="D120: SPY-only vs SPY + ΔVIX (descriptive; never a verdict)",
    )


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
    """Per-arm numbers from t0 (reported for the owner; no verdict reads them).

    D69 (E15.5): in a multi-arm (``report_version`` 2) report this is the all-arms row
    (:data:`ArmReport`); the fields below ``mean_slippage_bps`` are filled there and
    omitted from a one-treatment (v1) report, whose JSON stays byte-identical.
    """

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
    # -- v2 (all-arms view) only -------------------------------------------------
    cum_pnl_pct: float | None = Field(
        None, exclude_if=_absent, description="v2: total P&L / t0 equity (fraction)"
    )
    daily_mean: float | None = Field(
        None, exclude_if=_absent, description="v2: mean daily return (fraction of t0 equity)"
    )
    daily_sd: float | None = Field(
        None, exclude_if=_absent, description="v2: sd of daily returns (fraction)"
    )
    sortino: float | None = Field(None, exclude_if=_absent, description="v2: annualised")
    cross_arm_conflicts: int | None = Field(
        None, exclude_if=_absent, description="v2: cross_arm_conflict refusals since t0"
    )
    rank: int | None = Field(
        None, exclude_if=_absent, description="v2: 1 = best cumulative net P&L %"
    )
    verdict: ArmVerdict | None = Field(
        None, exclude_if=_absent, description="v2: the arm's Control-vs-arm verdict"
    )


#: D69 (E15.5): the all-arms row of a multi-arm report (same model, v2 fields filled).
ArmReport = ArmSummary


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
    # -- D119: pooled over the pairs; omitted when no SPY series / HAC was computed --
    sigma_adj: float | None = Field(
        None, exclude_if=_absent, description="D119: pooled SPY-adjusted residual sd"
    )
    sigma_lr: float | None = Field(
        None, exclude_if=_absent, description="D119: sigma_adj * sqrt(hac_factor)"
    )
    hac_lag: int | None = Field(None, exclude_if=_absent, description="D119: Bartlett lag")
    hac_factor: float | None = Field(
        None, exclude_if=_absent, description="D119: pooled long-run variance factor f"
    )
    rho: list[float] | None = Field(None, exclude_if=_absent, description="D119: pooled rho_k")
    beta_spread: float | None = Field(
        None, exclude_if=_absent, description="D119: max - min SPY beta across the pairs"
    )
    mde_fixed_adj: dict[int, float] | None = Field(None, exclude_if=_absent)
    mde_always_valid_adj: dict[int, float] | None = Field(None, exclude_if=_absent)
    mde_fixed_lr: dict[int, float] | None = Field(None, exclude_if=_absent)
    mde_always_valid_lr: dict[int, float] | None = Field(None, exclude_if=_absent)
    covariate_check: CovariateCheck | None = Field(
        None,
        exclude_if=_absent,
        description="D120: the ΔVIX check pooled over the Ti - C pairs (descriptive)",
    )


class BreakdownRow(BaseModel):
    model_config = _FORBID

    by: Literal["regime", "structure_kind"]
    key: str
    arm: str
    trades: int
    realised_pnl: float


class PairReport(BaseModel):
    """D69 (E15.5): one pairwise comparison of a multi-arm experiment.

    ``control_vs_treatment`` pairs carry the verdict: the primary metric (paired daily
    net P&L diff of ``b`` minus ``a``, fraction of t0 equity), its always-valid CI with
    the p next to it, the Holm-adjusted p across the K treatments, and the Sortino
    non-inferiority. ``treatment_vs_treatment`` pairs (Ti vs Tj) are descriptive only
    (``descriptive`` true, ``verdict`` None), except in an A/A, where every pair enters
    the harness check (``holm_p`` is then adjusted over all pairs).
    """

    model_config = _FORBID

    kind: Literal["control_vs_treatment", "treatment_vs_treatment"]
    a: str = Field(..., description="Baseline arm (control, or Ti for Ti vs Tj)")
    b: str = Field(..., description="Compared arm; the difference is b - a")
    arm_id: str | None = Field(None, description="b's arm_id")
    descriptive: bool = Field(..., description="True = no verdict (Ti vs Tj)")
    n: int
    mean: float | None
    ci: stats.Interval | None = Field(None, description="Always-valid (mSPRT) CI, level 1-alpha")
    sigma: float | None
    sigma_source: Literal["aa", "running_corrected"] | None
    tau: float | None
    p_value: float | None = Field(None, description="Always-valid mSPRT p (dual of ci)")
    holm_p: float | None = Field(
        None, description="Holm-adjusted p (control pairs: over the K treatments; A/A: all pairs)"
    )
    sample_sd: float | None = Field(None, description="Sample sd of the daily diff (calibration)")
    adjustment: Adjustment | None = Field(
        None, exclude_if=_absent, description="D119: SPY regression (verdict iff pre_registered)"
    )
    hac: Hac | None = Field(None, exclude_if=_absent, description="D119: long-run factor")
    sigma_raw: float | None = Field(
        None, exclude_if=_absent, description="D119: sample sd of d (unadjusted)"
    )
    sigma_adj: float | None = Field(
        None, exclude_if=_absent, description="D119: plug-in residual sd sqrt(SSE / df)"
    )
    sigma_lr: float | None = Field(
        None,
        exclude_if=_absent,
        description="D119: long-run sigma of the estimate: base * sqrt(f) * intercept factor",
    )
    primary_unadjusted: Unadjusted | None = Field(
        None, exclude_if=_absent, description="D119: the D44 iid unadjusted primary"
    )
    covariate_check: CovariateCheck | None = Field(
        None,
        exclude_if=_absent,
        description="D120: SPY-only vs SPY + ΔVIX (descriptive; never a verdict)",
    )
    secondary: Secondary | None = Field(None, description="Sortino (control pairs only)")
    verdict: ArmVerdict | None = Field(None, description="None for descriptive pairs")
    verdict_reason: str | None = None
    decided_day: _dt.date | None = Field(
        None, description="Session the arm stopped on (its series ends there)"
    )
    missing_sessions: list[_dt.date] = Field(default_factory=list)
    series: list[SessionRow] = Field(
        default_factory=list, description="Control pairs: the paired sessions (control = a)"
    )


class Omnibus(BaseModel):
    """D69: "does any treatment differ from control?" = the Holm-adjusted minimum p."""

    model_config = _FORBID

    k: int = Field(..., description="Treatments tested")
    p_value: float | None = Field(None, description="min Holm-adjusted p over the K pairs")
    differs: bool = False


class ExperimentReport(BaseModel):
    """One evaluation of one experiment (stored append-only in ``experiment_reports``).

    ``report_version`` 1: one treatment (D44). ``report_version`` 2 (D69, E15.5): K > 1
    treatments. ``report_version`` 3 (D108, E25.3): a spec v3 (any K), whose reference
    is the control arm ``XP-<n>:control`` (``reference``) and which adds the descriptive
    ``production`` panel; otherwise shaped like v2. A v2 report adds ``pairwise``,
    ``omnibus`` and ``headline_arm`` (omitted
    from v1 JSON) and fills the all-arms fields of ``arms`` (Control first, then t1..tK).
    Its top-level ``primary`` / ``secondary`` / ``series`` are the **headline** pair
    (Control vs the best-ranked arm, ``headline_arm``) so one-line surfaces keep a
    single number; verdicts live per arm in ``pairwise``. ``calibration`` is pooled
    over the Control-vs-Ti pairs.
    """

    model_config = _FORBID

    report_version: Literal[1, 2, 3] = REPORT_VERSION
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
    # -- v2 (D69 multi-arm) only: omitted from v1 JSON ---------------------------
    headline_arm: str | None = Field(
        None, exclude_if=_absent, description="v2: the arm the top-level primary compares"
    )
    pairwise: list[PairReport] | None = Field(
        None,
        exclude_if=_absent,
        description="v2: Control vs Ti (verdicts, Holm) then Ti vs Tj (descriptive)",
    )
    omnibus: Omnibus | None = Field(None, exclude_if=_absent, description="v2")
    # -- v3 (D108) only: omitted from v1/v2 JSON ---------------------------------
    reference: Literal["control"] | None = Field(
        None,
        exclude_if=_absent,
        description="v3: the verdict's reference series is the control arm XP-<n>:control",
    )
    production: ProductionPanel | None = Field(
        None,
        exclude_if=_absent,
        description="v3: production vs control vs treatments (descriptive, never a verdict)",
    )
    # -- D119 (E25.7): omitted when absent ---------------------------------------------
    analysis: Analysis | None = Field(
        None, exclude_if=_absent, description="D119: the spec's pre-registered analysis"
    )
    covariate_missing: list[_dt.date] | None = Field(
        None,
        exclude_if=_absent,
        description="D119: paired sessions with no SPY return (an adjusted verdict waits)",
    )

    @property
    def multi_arm(self) -> bool:
        return self.pairwise is not None

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


def _first_session(t0: _dt.datetime, *, partial: bool = False) -> _dt.date:
    """The first ET session of an experiment started at *t0*.

    A t0 at or before the open always counts that day. A mid-session t0 skips that
    day unless *partial* (D117): then the day counts as a partial session, t0 to the
    close. Only a spec v3 series is *partial*: every arm (control included) opens flat
    at the same t0 equity at the same instant, so t0 to the close is still a paired
    observation. Up to spec v2 control is production's broker equity, whose previous
    EOD row includes the pre-t0 morning, so those keep the full-session rule. A t0 at
    or after the close (or on a non-session day) starts at the next session.
    """
    day = t0.astimezone(ET).date()
    if is_session(day) and (session_open(day) >= t0 or (partial and t0 < session_close(day))):
        return day
    return next_session(day)


def control_arm_id(st: ExperimentState) -> str | None:
    """The ``arm_id`` of the verdict's reference series: production (NULL) up to spec v2,
    the control arm ``XP-<n>:control`` from spec v3 (D108)."""
    return st.spec.arm_id_for(CONTROL_ARM)


def _series(
    conn: sqlite3.Connection,
    st: ExperimentState,
    *,
    now: _dt.datetime,
    t_arm: str | None = None,
) -> tuple[list[SessionRow], list[_dt.date]]:
    """Control vs one treatment arm (*t_arm*; default the v1 ``XP-<n>:treatment``).

    D108 (spec v3): control is the flat arm ``XP-<n>:control``. Its series is read
    on the treatments' basis (``virtual_equity``, t0 equity before its first EOD row)
    and nothing is subtracted from it (no legacy book: production's start book is
    reported in the descriptive production panel only).
    """
    run = st.running
    assert run is not None  # noqa: S101 - caller checks
    if t_arm is None:
        t_arm = arm_id(st.experiment_id, TREATMENT_ARM)
    v3 = st.spec.control_runs
    ctrl = (
        _eod_equity(conn, control_arm_id(st), TREATMENT_EQUITY_FIELD)
        if v3
        else _eod_equity(conn, None)
    )
    treat = _eod_equity(conn, t_arm, TREATMENT_EQUITY_FIELD)
    legacy: set[str] = set() if v3 else set(run.production_book)
    marks = _legacy_marks(conn, legacy)
    cash = _legacy_cash(conn, legacy)
    first = _first_session(run.t0, partial=v3)
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
        c, t = ctrl.get(day), treat.get(day)
        c_prev = ctrl.get(prev, t0_eq if v3 and day == first else None)
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


def report_config(cfg: ExperimentsConfig) -> dict[str, Any]:
    """The experiments config a report records: everything that can change a verdict."""
    return cfg.model_dump(mode="json")


def config_hash(cfg: ExperimentsConfig) -> str:
    blob = json.dumps(report_config(cfg), sort_keys=True, separators=(",", ":"))
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


@dataclass(frozen=True)
class Method:
    """D119: how the long-run primary of one experiment is computed.

    *analysis* = the spec's ``analysis`` (``pre_registered``: the verdict reads it) or,
    for a spec without one, the descriptive ``[spy_ret]`` + ``experiments.stats.hac_lag``.
    *aa_sigma* / *aa_factor* = the A/A's sigma on the analysis' footing and its long-run
    factor (None for an A/A experiment itself, or when none is on record).
    """

    analysis: Analysis
    pre_registered: bool
    max_factor: float
    aa_sigma: float | None = None
    aa_factor: float | None = None
    #: D120: the D117 partial first session (no ΔVIX on the free feed): a missing
    #: ``vix_chg`` there is left out of the fit and never holds a verdict
    partial_day: _dt.date | None = None

    @property
    def waits_on_covariate(self) -> bool:
        return self.pre_registered and self.analysis.adjusted


def method_for(
    st: ExperimentState,
    cfg: ExperimentsConfig,
    *,
    aa_sigma: float | None,
    aa_cal: StopDetail | None,
) -> Method:
    """The D119 method of *st* (*aa_sigma*: the raw A/A sigma; *aa_cal*: the A/A stop)."""
    sp = st.spec
    an = sp.analysis or Analysis(covariates=["spy_ret"], hac_lag=cfg.stats.hac_lag)
    known: float | None = None
    factor: float | None = None
    if sp.kind is not ExperimentKind.AA:
        if an.adjusted:
            known = None if aa_cal is None else aa_cal.sigma_adj
        else:
            known = aa_sigma if aa_cal is None else aa_cal.sigma
        if (
            known is not None
            and aa_cal is not None
            and an.adjusted
            and an.hac_lag is not None
            and aa_cal.hac_lag == an.hac_lag
        ):
            factor = aa_cal.hac_factor
    partial = None
    if st.running is not None and sp.control_runs:
        d0 = st.running.t0.astimezone(ET).date()
        if session_open(d0) < st.running.t0 < session_close(d0):
            partial = d0
    return Method(
        analysis=an,
        pre_registered=sp.analysis is not None,
        max_factor=cfg.stats.hac_max_factor,
        aa_sigma=known,
        aa_factor=factor,
        partial_day=partial,
    )


def _long_run(
    d: list[float],
    xs: list[float | None],
    days: list[_dt.date],
    m: Method,
    *,
    alpha: float,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
    vs: list[float | None] | None = None,
) -> dict[str, Any] | None:
    """The D119 fields of one series (``adjustment``, ``hac``, ``sigma_*``).

    *xs* = SPY returns, *vs* = ΔVIX (D120; only read when the analysis names
    ``vix_chg``). None: descriptive and nothing to fit.
    """
    an = m.analysis
    xx: Any
    if an.adjusted:
        cols = {"spy_ret": xs, "vix_chg": vs if vs is not None else [None] * len(d)}
        use = [cols[c] for c in an.covariates]
        idx, missing = [], []
        for i in range(len(d)):
            vals = [c[i] for c in use]
            if all(v is not None for v in vals):
                idx.append(i)
                continue
            exempt = days[i] == m.partial_day and all(
                v is not None or name == "vix_chg"
                for name, v in zip(an.covariates, vals, strict=True)
            )
            if not exempt:  # D120: no ΔVIX on the partial first session holds nothing
                missing.append(days[i])
        dd = [d[i] for i in idx]
        if len(use) == 1:
            xx = [float(use[0][i]) for i in idx]  # type: ignore[arg-type]
        else:
            xx = np.asarray([[float(c[i]) for c in use] for i in idx]).reshape(len(idx), len(use))  # type: ignore[arg-type]
    else:
        dd, xx, missing = list(d), None, []
    if not m.pre_registered and len(dd) < 2:
        return None  # descriptive: nothing to show (n < 3 shows status insufficient_n)
    lr = stats.long_run_primary(
        dd,
        xx,
        hac_lag=an.hac_lag,
        alpha=alpha,
        sigma_upper_q=sigma_upper_q,
        mde=mde,
        min_sessions=min_sessions,
        max_factor=m.max_factor,
        aa_sigma=m.aa_sigma,
        aa_factor=m.aa_factor,
    )
    fitted = lr is not None and lr.fit.adjusted
    status = "unadjusted" if not an.adjusted else ("fitted" if fitted else "insufficient_n")
    adj: dict[str, Any] = {
        "pre_registered": m.pre_registered,
        "status": status,
        "covariates": list(an.covariates),
        "n": len(dd),
        "covariate_missing": missing,
    }
    out: dict[str, Any] = {"sigma_raw": stats.sample_sd(dd) if len(dd) >= 2 else None}
    if lr is None:
        return out | {"adjustment": Adjustment(**adj), "hac": Hac(lag=an.hac_lag)}
    f = lr.fit
    return out | {
        "adjustment": Adjustment(
            **adj,
            beta=f.beta,
            beta_se=f.beta_se,
            r2=f.r2,
            estimate=f.estimate,
            ci=lr.ci,
            p_value=lr.p_value,
            tau=lr.tau,
            sigma_source=lr.sigma_source,
        ),
        "hac": Hac(
            lag=an.hac_lag,
            factor=lr.factor,
            factor_running=lr.factor_running,
            rho=lr.rho,
            source=lr.hac_source,
        ),
        "sigma_adj": f.s_e,
        "sigma_lr": lr.sigma_lr,
    }


def covariate_check_of(
    d: list[float],
    xs: list[float | None] | None,
    vs: list[float | None],
    m: Method,
    *,
    alpha: float,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
) -> CovariateCheck | None:
    """D120: the descriptive ΔVIX check of one series (None: fewer than 4 days with both)."""
    from arc.experiments.covariate_check import covariate_check

    if xs is None or not any(v is not None for v in vs):
        return None
    return covariate_check(
        d, xs, vs, hac_lag=m.analysis.hac_lag, alpha=alpha, sigma_upper_q=sigma_upper_q,
        mde=mde, min_sessions=min_sessions, max_factor=m.max_factor, aa_factor=m.aa_factor,
    )  # fmt: skip


def pooled_covariate_check(
    cal: Calibration,
    series: list[tuple[list[float], list[float | None], list[float | None]]],
    *,
    method: Method,
    st: ExperimentState,
    cfg: ExperimentsConfig,
) -> Calibration:
    """D120: *cal* plus the ΔVIX check pooled over the Ti - C *series* (unchanged if none)."""
    from arc.experiments.covariate_check import pooled_check

    if not any(v is not None for _, _, vs in series for v in vs):
        return cal
    sp = st.spec
    assert sp.alpha is not None and sp.min_sessions is not None  # noqa: S101
    chk = pooled_check(
        series, hac_lag=method.analysis.hac_lag or cfg.stats.hac_lag, alpha=sp.alpha,
        sigma_upper_q=cfg.stats.sigma_upper_q, mde=sp.mde, min_sessions=sp.min_sessions,
        max_factor=method.max_factor,
    )  # fmt: skip
    return cal if chk is None else cal.model_copy(update={"covariate_check": chk})


def row_waits(row: SessionRow, m: Method) -> bool:
    """A pre-registered adjusted *m* lacks a covariate it names on *row* (D119/D120).

    ΔVIX on the D117 partial first session is exempt (it never exists there).
    """
    if not m.waits_on_covariate:
        return False
    for c in m.analysis.covariates:
        if c == "spy_ret" and row.spy_ret is None:
            return True
        if c == "vix_chg" and row.vix_chg is None and row.day != m.partial_day:
            return True
    return False


def covariate_blocked(primary: Primary | PairReport) -> bool:
    """D119: a pre-registered adjusted primary missing a SPY return (the verdict waits)."""
    a = primary.adjustment
    return bool(a is not None and a.pre_registered and a.covariate_missing)


def _primary_from(
    d: list[float],
    *,
    alpha: float,
    known: float | None,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
    xs: list[float | None] | None = None,
    days: list[_dt.date] | None = None,
    method: Method | None = None,
    vs: list[float | None] | None = None,
    check: bool = True,
) -> Primary:
    """The primary metric of one paired series *d* (shared by v1 and every v2 pair).

    D119: with a *method*, the adjusted + HAC fields are attached (``adjustment``,
    ``hac``, ``sigma_raw/adj/lr``); when the method is pre-registered they **are** the
    primary (``mean`` / ``ci`` / ``p_value`` / ``sigma`` / ``tau``) and the D44 iid
    unadjusted numbers move to ``primary_unadjusted``.
    *xs* = SPY returns aligned with *d* (None = missing), *days* their sessions.
    """
    base = _iid_primary(
        d, alpha=alpha, known=known, sigma_upper_q=sigma_upper_q, mde=mde,
        min_sessions=min_sessions,
    )  # fmt: skip
    if method is None:
        return base
    xs = xs if xs is not None else [None] * len(d)
    days = days if days is not None else [_dt.date.min] * len(d)
    lr = _long_run(
        d, xs, days, method, alpha=alpha, sigma_upper_q=sigma_upper_q, mde=mde,
        min_sessions=min_sessions, vs=vs,
    )  # fmt: skip
    cov_check = (
        None
        if vs is None or not check
        else covariate_check_of(
            d,
            xs,
            vs,
            method,
            alpha=alpha,
            sigma_upper_q=sigma_upper_q,
            mde=mde,
            min_sessions=min_sessions,
        )  # fmt: skip
    )
    if cov_check is not None:
        lr = (lr or {}) | {"covariate_check": cov_check}
    if not method.pre_registered:
        return base if lr is None else base.model_copy(update=lr)
    assert lr is not None  # noqa: S101 - a pre-registered method always reports one
    a: Adjustment = lr["adjustment"]
    unadjusted = Unadjusted(
        n=base.n, mean=base.mean, ci=base.ci, sigma=base.sigma,
        sigma_source=base.sigma_source, tau=base.tau, p_value=base.p_value,
    )  # fmt: skip
    return Primary(
        n=base.n,
        mean=a.estimate,
        ci=a.ci,
        sigma=lr.get("sigma_lr"),
        sigma_source=a.sigma_source,
        tau=a.tau,
        p_value=a.p_value,
        lower_bound_positive=a.ci is not None and a.ci.lo > 0.0 and not a.covariate_missing,
        primary_unadjusted=unadjusted,
        **lr,
    )


def _iid_primary(
    d: list[float],
    *,
    alpha: float,
    known: float | None,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
) -> Primary:
    """The D44 iid unadjusted primary of *d*."""
    n = len(d)
    ci, sigma, tau = stats.confidence_sequence(
        d,
        alpha=alpha,
        sigma=known,
        sigma_upper_q=sigma_upper_q,
        mde=mde,
        min_sessions=min_sessions,
    )
    return Primary(
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


def _secondary_from(
    t_r: list[float],
    c_r: list[float],
    *,
    alpha: float,
    margin: float | None,
    resamples: int,
    seed: int,
) -> Secondary:
    """Sortino of both arms and the paired-bootstrap non-inferiority (v1 and v2 pairs)."""
    diff_ci = stats.sortino_diff_ci(
        t_r,
        c_r,
        level=1.0 - 2.0 * alpha,
        resamples=resamples,
        seed=seed,
    )
    return Secondary(
        sortino_control=stats.sortino(c_r),
        sortino_treatment=stats.sortino(t_r),
        diff_ci=diff_ci,
        margin=margin,
        p_value=stats.sortino_diff_p(
            t_r,
            c_r,
            margin=margin,
            resamples=resamples,
            seed=seed,
        ),
        non_inferior=None if margin is None else stats.non_inferior(diff_ci, margin),
    )


def _treatment_arm_ids(conn: sqlite3.Connection, st: ExperimentState) -> dict[str, str]:
    """Spec treatment (t1..tK) -> the ``arm_id`` its runner arm's rows carry (D69).

    From the arm stores recorded at t0 (each store's ``arm_identity.spec_arm``); a
    treatment with no store falls back to the naming convention: ``XP-<n>:treatment``
    for a one-treatment spec (the v1 runner arm), ``XP-<n>:<tN>`` otherwise (the
    ``treatments`` template expands one arm per treatment, named after it).
    """
    from arc.experiments.arms import arm_stores
    from arc.experiments.models import spec_arm_name

    names = st.spec.arms.names
    out: dict[str, str] = {}
    try:
        stores = arm_stores(conn, st.experiment_id)
    except sqlite3.OperationalError:
        stores = {}
    for path in stores.values():
        ident = _store_identity(path)
        if ident is None or ident.experiment_id != st.experiment_id:
            continue
        spec_arm = spec_arm_name(ident.spec_arm)
        if spec_arm in names and spec_arm not in out:
            out[spec_arm] = ident.arm_id
    for name in names:
        if name not in out:
            runner_arm = TREATMENT_ARM if len(names) == 1 and not st.spec.control_runs else name
            out[name] = f"{st.experiment_id}:{runner_arm}"
    return {name: out[name] for name in names}


def _store_identity(path: Path) -> ArmIdentity | None:
    """The arm identity of the store at *path* (read-only), or None."""
    from arc.experiments.arms import read_identity

    if not path.is_file():
        return None
    c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return read_identity(c)
    finally:
        c.close()


def _horizons(cfg: ExperimentsConfig, min_sessions: int, max_sessions: int) -> list[int]:
    """The MDE table's session counts: the default window's ends and midpoint, plus the spec's."""
    lo_s, hi_s = cfg.defaults.min_sessions, cfg.defaults.max_sessions
    return sorted({lo_s, (lo_s + hi_s) // 2, hi_s, min_sessions, max_sessions})


def _calibration(
    sd: float | None,
    cfg: ExperimentsConfig,
    st: ExperimentState,
    ctrl: ArmSummary,
    treat: ArmSummary,
    *,
    paired: int,
    diverged: int,
) -> Calibration:
    """Calibration from the daily-diff sd *sd* and the control / treatment arm rows."""
    sp = st.spec
    assert sp.alpha is not None and sp.power is not None  # noqa: S101 - registered spec
    assert sp.min_sessions is not None and sp.max_sessions is not None  # noqa: S101
    horizons = _horizons(cfg, sp.min_sessions, sp.max_sessions)
    fill = [(a.filled_executions / a.executions) if a.executions else None for a in (ctrl, treat)]
    return Calibration(
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


def hac_calibration(
    cal: Calibration,
    series: list[tuple[list[float], list[float | None]]],
    *,
    lag: int,
    st: ExperimentState,
    cfg: ExperimentsConfig,
) -> Calibration:
    """D119: *cal* plus the pooled SPY-adjusted and long-run sigma of the *series*.

    Each ``(d, x)`` pair is fitted on its sessions with a SPY return (>= 3); sigma_adj
    and the factor pool over the fitted pairs (:func:`stats.pooled_hac`). *cal* is
    returned unchanged when nothing can be fitted (no SPY series: byte-identical JSON).
    """
    resids: list[list[float]] = []
    dfs: list[int] = []
    betas: list[float] = []
    for d, xs in series:
        pts = [(v, x) for v, x in zip(d, xs, strict=True) if x is not None]
        if len(pts) < 3:
            continue
        fit = stats.ols_fit([v for v, _ in pts], [x for _, x in pts])
        if fit is None or not fit.adjusted:
            continue
        resids.append(fit.resid)
        dfs.append(fit.df)
        betas.append(float(fit.beta or 0.0))
    sigma_adj, factor, rho = stats.pooled_hac(resids, dfs, lag)
    if sigma_adj is None or factor is None:
        return cal
    sp = st.spec
    assert sp.alpha is not None and sp.power is not None  # noqa: S101 - registered spec
    assert sp.min_sessions is not None and sp.max_sessions is not None  # noqa: S101
    horizons = _horizons(cfg, sp.min_sessions, sp.max_sessions)
    sigma_lr = sigma_adj * math.sqrt(factor)

    def tables(sd: float) -> tuple[dict[int, float], dict[int, float]]:
        tau = stats.mixing_tau(sd, mde=sp.mde, min_sessions=sp.min_sessions)  # type: ignore[arg-type]
        return (
            {h: stats.mde_fixed(sd, h) for h in horizons},
            {
                h: stats.mde_always_valid(sd, h, tau=tau, alpha=sp.alpha, power=sp.power)  # type: ignore[arg-type]
                for h in horizons
            },
        )

    fa, va = tables(sigma_adj)
    fl, vl = tables(sigma_lr)
    return cal.model_copy(
        update={
            "sigma_adj": sigma_adj,
            "sigma_lr": sigma_lr,
            "hac_lag": lag,
            "hac_factor": factor,
            "rho": rho,
            "beta_spread": max(betas) - min(betas) if betas else None,
            "mde_fixed_adj": fa,
            "mde_always_valid_adj": va,
            "mde_fixed_lr": fl,
            "mde_always_valid_lr": vl,
        }
    )


def with_spy(
    conn: sqlite3.Connection,
    st: ExperimentState,
    rows: list[SessionRow],
    *,
    spy: Mapping[_dt.date, float] | None,
    vix: Mapping[_dt.date, float] | None = None,
) -> list[SessionRow]:
    """*rows* with ``spy_ret`` (D119) and ``vix_chg`` (D120) filled; others stay bare."""
    if not spy and not vix:
        return rows
    out: list[SessionRow] = []
    for r in rows:
        upd: dict[str, float] = {}
        if spy and r.day in spy:
            upd["spy_ret"] = spy[r.day]
        if vix and r.day in vix:
            upd["vix_chg"] = vix[r.day]
        out.append(r.model_copy(update=upd) if upd else r)
    return out


def vix_series(
    conn: sqlite3.Connection,
    st: ExperimentState,
    days: list[_dt.date],
    *,
    vix: Callable[[], Mapping[_dt.date, float]] | None,
) -> dict[_dt.date, float]:
    """ΔVIX for *days* (D120): stored reports first, then *vix* (VIX closes) for the rest."""
    from arc.experiments.covariate import stored_vix_changes, vix_changes

    run = st.running
    assert run is not None  # noqa: S101 - caller checks
    if not days:
        return {}
    return vix_changes(
        sorted(set(days)),
        t0=run.t0,
        partial_first=st.spec.control_runs,
        stored=stored_vix_changes(conn, st.experiment_id),
        closes=vix,
    )


def covariate_series(
    conn: sqlite3.Connection,
    st: ExperimentState,
    days: list[_dt.date],
    *,
    market: BarsProvider | None,
) -> dict[_dt.date, float]:
    """SPY returns for *days* (D119): stored reports first, then *market* for the rest."""
    from arc.experiments.covariate import spy_returns, stored_spy_returns

    run = st.running
    assert run is not None  # noqa: S101 - caller checks
    if not days:
        return {}
    return spy_returns(
        sorted(set(days)),
        t0=run.t0,
        partial_first=st.spec.control_runs,
        stored=stored_spy_returns(conn, st.experiment_id),
        market=market,
    )


def build_report(
    conn: sqlite3.Connection,
    st: ExperimentState,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    aa_sigma: float | None = None,
    aa_cal: StopDetail | None = None,
    market: BarsProvider | None = None,
    vix: Callable[[], Mapping[_dt.date, float]] | None = None,
) -> ExperimentReport:
    """Evaluate *st* (running, or stopped with a t0) as of *now*. Read-only.

    D69: a spec with K > 1 treatments gets the multi-arm (``report_version`` 2) report
    (:func:`arc.experiments.multi.build_multi_report`); K = 1 is the v1 report, byte for
    byte as before E15.5.

    D119: *market* supplies SPY's daily bars for sessions no stored report carries
    (None: stored values only); *aa_cal* = the latest A/A stop (adjusted + long-run sigma).
    D120: *vix* returns VIX daily closes for ΔVIX days no stored report carries (None:
    stored only); ΔVIX feeds the descriptive ``covariate_check`` only.
    """
    method = method_for(st, cfg, aa_sigma=aa_sigma, aa_cal=aa_cal)
    sp, run = st.spec, st.running
    if run is None:
        msg = f"{st.experiment_id} has no t0 (never started): nothing to evaluate"
        raise ExperimentError(msg)
    assert sp.alpha is not None and sp.power is not None  # noqa: S101 - registered spec
    assert sp.min_sessions is not None and sp.max_sessions is not None  # noqa: S101
    arm_ids = _treatment_arm_ids(conn, st)
    if len(arm_ids) > 1 or sp.control_runs:
        # D69: K > 1 -> v2; D108: every spec v3 (any K) -> v3, control arm as reference
        from arc.experiments.multi import build_multi_report

        return build_multi_report(
            conn, st, cfg, now=now, aa_sigma=aa_sigma, arm_ids=arm_ids, method=method,
            market=market, vix=vix,
        )  # fmt: skip
    t_arm = next(iter(arm_ids.values()))
    legacy = set(run.production_book)
    legacy_hashes = _legacy_hashes(conn, legacy)
    rows, missing = _series(conn, st, now=now, t_arm=t_arm)
    spy = covariate_series(conn, st, [r.day for r in rows], market=market)
    dvix = vix_series(conn, st, [r.day for r in rows], vix=vix)
    rows = with_spy(conn, st, rows, spy=spy, vix=dvix)
    vs = [r.vix_chg for r in rows] if dvix else None
    d = [r.d for r in rows]
    n = len(d)
    is_aa = sp.kind is ExperimentKind.AA

    # -- primary -------------------------------------------------------------
    known = None if is_aa else aa_sigma
    primary = _primary_from(
        d,
        alpha=sp.alpha,
        known=known,
        sigma_upper_q=cfg.stats.sigma_upper_q,
        mde=sp.mde,
        min_sessions=sp.min_sessions,
        xs=[r.spy_ret for r in rows],
        days=[r.day for r in rows],
        method=method,
        vs=vs,
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
    secondary = _secondary_from(
        t_r,
        c_r,
        alpha=sp.alpha,
        margin=sp.non_inferiority_margin,
        resamples=cfg.stats.bootstrap_resamples,
        seed=stats.seed_for(st.experiment_id, n),
    )

    # -- calibration ---------------------------------------------------------
    sd = stats.sample_sd(d) if n >= 2 else None
    # the A/A's MDE table (E10.4): the default window's ends and midpoint (20/40/60)
    # plus this spec's own window
    paired, diverged = _divergence(conn, t_arm, run.t0)
    cal = _calibration(sd, cfg, st, ctrl, treat, paired=paired, diverged=diverged)
    cal = hac_calibration(
        cal, [(d, [r.spy_ret for r in rows])], lag=method.analysis.hac_lag or cfg.stats.hac_lag,
        st=st, cfg=cfg,
    )  # fmt: skip
    if vs is not None:
        cal = pooled_covariate_check(
            cal, [(d, [r.spy_ret for r in rows], vs)], method=method, st=st, cfg=cfg
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
        config=report_config(cfg),
        control_sha=run.control_sha,
        treatment_sha=_treatment_sha(conn, t_arm),
        evaluator_sha=_evaluator_sha(),
        analysis=sp.analysis,
        covariate_missing=(
            primary.adjustment.covariate_missing
            if method.waits_on_covariate and primary.adjustment is not None
            else None
        ),
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
    if covariate_blocked(primary):
        assert primary.adjustment is not None  # noqa: S101
        miss = primary.adjustment.covariate_missing
        return "continue", (
            f"waiting on SPY's return for {len(miss)} session(s) "
            f"({', '.join(d.isoformat() for d in miss[:5])}): the pre-registered adjusted "
            "primary never falls back to the unadjusted CI"
        )
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


def _build_paired(
    conn: sqlite3.Connection,
    st: ExperimentState,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    aa_sigma: float | None,
    aa_cal: StopDetail | None = None,
    market: BarsProvider | None = None,
    vix: Callable[[], Mapping[_dt.date, float]] | None = None,
) -> ExperimentReport:
    """:func:`build_report` on the control store with the arm stores unioned in (E10.2)."""
    from arc.experiments.paired import paired_view

    with paired_view(conn) as view:
        return build_report(
            view, st, cfg, now=now, aa_sigma=aa_sigma, aa_cal=aa_cal, market=market, vix=vix
        )


def evaluate(
    store: ExperimentStore,
    experiment_id: str,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    run_id: str | None = None,
    market: BarsProvider | None = None,
    vix: Callable[[], Mapping[_dt.date, float]] | None = None,
) -> ExperimentReport:
    """Evaluate a running experiment, store the report and apply its verdict (stop).

    D119: *market* = SPY's daily bars for the covariate (None: stored values only).
    D120: *vix* = VIX daily closes for the descriptive ΔVIX check (None: stored only).
    """
    conn = store.conn
    st = store.require(experiment_id)
    if st.status is not ExperimentStatus.RUNNING:
        msg = f"{experiment_id} is {st.status.value}, not running"
        raise ExperimentError(msg)
    report = _build_paired(
        conn, st, cfg, now=now, aa_sigma=store.aa_sigma(), aa_cal=store.aa_calibration(),
        market=market, vix=vix,
    )  # fmt: skip
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
        cal = report.calibration
        sigma = cal.sigma if report.kind is ExperimentKind.AA else None
        record = report.verdict == "futility" and bool(sigma)
        store.stop(
            experiment_id,
            _STOP[report.verdict],
            actor=EVALUATOR_ACTOR,
            detail=StopDetail(
                sigma=sigma if record else None,
                sessions=report.sessions,
                note=report.verdict_reason[:500],
                # D119: the A/A also records the adjusted and long-run calibration
                sigma_adj=cal.sigma_adj if record else None,
                hac_factor=cal.hac_factor if record else None,
                hac_lag=cal.hac_lag if record else None,
                beta_spread=cal.beta_spread if record else None,
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
    market: BarsProvider | None = None,
    vix: Callable[[], Mapping[_dt.date, float]] | None = None,
) -> list[ExperimentReport]:
    """Evaluate every running experiment (oldest first)."""
    return [
        evaluate(store, s.experiment_id, cfg, now=now, run_id=run_id, market=market, vix=vix)
        for s in store.all(status=ExperimentStatus.RUNNING)
    ]
