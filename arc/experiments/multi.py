"""Multi-arm experiment evaluation (PLAN D69, card E15.5): Control vs T1..TK.

Pure maths plus read-only SQL through the helpers of :mod:`arc.experiments.evaluate`.
No LLM, no broker, no network; the caller injects ``now``. A spec with one treatment
never comes here: it keeps the v1 report byte for byte (:func:`evaluate.build_report`).

**Pairs.** Every treatment Ti is paired with control exactly as the one-treatment
evaluator pairs ``treatment`` (``evaluate._series``: per-session EOD equity, legacy
book removed from control, virtual equity for the arm), so each Control-vs-Ti pair
has its own series ``d_i,t = (Ti_pnl_t - control_pnl_t) / t0_equity`` and its own
missing sessions. A Ti-vs-Tj pair uses the sessions both pairs share:
``d_t = (Tj_pnl_t - Ti_pnl_t) / t0_equity``.

**Verdicts (owner D69: pairwise Control vs Ti, Holm across the K arms).** Each pair's
always-valid mSPRT p (the dual of its CI, as in v1) is made monotone by taking its
running minimum over the daily looks (an always-valid p's running minimum is still a
valid p), and the K running-minimum p's are Holm-adjusted at the spec's alpha. The
evaluator replays the daily looks from t0 (deterministic, no stored state), and at
each look an undecided arm Ti with ``n_i >= min_sessions`` and Holm p < alpha stops:

- ``win`` when its mean is > 0 and the Sortino is non-inferior within the margin
  (the v1 rule); a positive arm whose Sortino is not yet shown keeps running;
- ``loss`` when its mean is < 0;

and an arm with ``n_i >= max_sessions`` and no win/loss stops as ``futility``. A
stopped arm's numbers freeze at its ``decided_day`` (its p stays at the value it had),
so later sessions never change its verdict, and the others continue. Holm's adjusted
p is monotone in every input p, so every rejection made at any look is also made by
Holm on the final running-minimum p's: the familywise error over the whole run is
<= alpha (``tests/test_experiment_holm.py`` simulates it at K = 4).

The experiment stops when every arm has stopped: ``win`` if any arm won, else
``futility`` (no arm won).

**A/A (kind aa).** Every pair, Control vs Ti and Ti vs Tj, enters one Holm family at
alpha (K + K(K-1)/2 tests); a rejection at any look makes the report ``invalid``
(the v1 harness rule, extended). It ends as ``futility`` once every Control pair has
``max_sessions`` sessions.

**Descriptive.** In an A/B, Ti-vs-Tj pairs carry a CI and p but no verdict and no
Holm p. The omnibus "any treatment differs" p is the minimum Holm-adjusted Control
pair p (= K x the smallest p).

**All-arms view.** ``arms`` holds Control then t1..tK with cumulative net P&L % of t0
equity, daily mean +- sd, Sortino, max drawdown, orders, ``cross_arm_conflict``
refusals and sessions, ranked by the primary (each arm's mean paired difference vs
control; control = 0). Its numbers use all the arm's sessions, not only those up to a
verdict (the owner sees what the arm actually did).

**Spec v3 (D108, E25.3).** Control is the flat arm ``XP-<n>:control`` on the experiment
account: its series is its ``virtual_equity`` (t0 equity before its first EOD row),
nothing is subtracted (no legacy book), and any K (K = 1 included) gets this report
(``report_version`` 3, ``reference: control``). The Holm family is the K Control-vs-Ti
pairs in an A/B **and** an A/A (``invalid`` when any Ti vs C is rejected); Ti vs Tj
stays descriptive. Production's comparison is the descriptive ``production`` panel
(:mod:`arc.experiments.production`), built after the verdicts and read by none of them.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from arc.experiments import evaluate as ev
from arc.experiments import stats
from arc.experiments.models import CONTROL_ARM, ExperimentKind

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable, Mapping

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.covariate import BarsProvider
    from arc.experiments.models import ExperimentState

__all__ = [
    "MULTI_REPORT_VERSION",
    "V3_REPORT_VERSION",
    "build_multi_report",
    "cross_arm_conflicts",
]

MULTI_REPORT_VERSION = 2
#: D108 (E25.3): the report of a spec v3: control arm as reference + production panel.
V3_REPORT_VERSION = 3
#: E15.3's ``ReasonCode.CROSS_ARM_CONFLICT`` is ``order:cross_arm_conflict`` (both the
#: pre-check refusal and the mapped Alpaca 403 ``40310000``); any code named
#: ``cross_arm_conflict`` (bare or namespaced) is counted.
CROSS_ARM_CONFLICT = "cross_arm_conflict"


@dataclass
class _Pair:
    """One pair's daily looks: its rows and the running decision state."""

    kind: str
    a: str
    b: str
    arm_id: str | None
    rows: list[ev.SessionRow]
    d: list[float]
    missing: list[_dt.date] = field(default_factory=list)
    decided_at: int | None = None  # number of sessions at the decision
    verdict: str | None = None
    reason: str | None = None
    p_min: float = 1.0


def cross_arm_conflicts(conn: sqlite3.Connection, aid: str | None, t0: _dt.datetime) -> int:
    """``cross_arm_conflict`` refusals journalled by the arm since t0 (D69, E15.3)."""
    from arc.context.ttl import to_db

    where, args = ev._arm_clause(aid)  # noqa: SLF001 - same module family
    r = conn.execute(
        f"""SELECT COUNT(*) FROM decisions
            WHERE {where} AND at >= ?
              AND (reason_code = ? OR reason_code LIKE ?)""",  # noqa: S608
        (*args, to_db(t0), CROSS_ARM_CONFLICT, f"%:{CROSS_ARM_CONFLICT}"),
    ).fetchone()
    return int(r[0] or 0)


def _between(a_rows: list[ev.SessionRow], b_rows: list[ev.SessionRow]) -> list[ev.SessionRow]:
    """Ti-vs-Tj rows on the sessions both Control pairs have (a = Ti as 'control')."""
    by_day = {r.day: r for r in a_rows}
    out: list[ev.SessionRow] = []
    for rb in b_rows:
        ra = by_day.get(rb.day)
        if ra is None:
            continue
        out.append(
            ev.SessionRow(
                day=rb.day,
                control_equity=ra.treatment_equity,
                treatment_equity=rb.treatment_equity,
                control_pnl=ra.treatment_pnl,
                legacy_pnl=0.0,
                treatment_pnl=rb.treatment_pnl,
                d=rb.d - ra.d,  # (Tj - C) - (Ti - C) = (Tj - Ti) / t0_equity
                spy_ret=rb.spy_ret,  # D119: the same SPY day for every pair
                vix_chg=rb.vix_chg,  # D120: the same ΔVIX day for every pair
            )
        )
    return out


def _primary_at(
    rows: list[ev.SessionRow],
    *,
    alpha: float,
    known: float | None,
    q: float,
    mde: float | None,
    min_s: int,
    method: ev.Method | None = None,
    check: bool = False,
) -> ev.Primary:
    """The primary of the pair's first sessions *rows* (D119: under *method*).

    *check*: also compute the D120 descriptive ΔVIX check (the reported numbers only;
    the daily-look replay never needs it).
    """
    vs = [r.vix_chg for r in rows] if any(r.vix_chg is not None for r in rows) else None
    return ev._primary_from(  # noqa: SLF001
        [r.d for r in rows], alpha=alpha, known=known, sigma_upper_q=q, mde=mde,
        min_sessions=min_s, xs=[r.spy_ret for r in rows], days=[r.day for r in rows],
        method=method, vs=vs, check=check,
    )  # fmt: skip


def _p_at(
    rows: list[ev.SessionRow],
    *,
    alpha: float,
    known: float | None,
    q: float,
    mde: float | None,
    min_s: int,
    method: ev.Method | None = None,
) -> float:
    """Always-valid p of the pair's first sessions *rows* (1.0 while no CI can be formed).

    D119: a pre-registered adjusted method missing a SPY return in *rows* gives 1.0
    (that look waits; the replay redoes it once the return is stored).
    """
    p = _primary_at(rows, alpha=alpha, known=known, q=q, mde=mde, min_s=min_s, method=method)
    if ev.covariate_blocked(p):
        return 1.0
    return 1.0 if p.p_value is None else p.p_value


def _replay(
    pairs: list[_Pair],
    family: list[_Pair],
    *,
    is_aa: bool,
    alpha: float,
    known: float | None,
    q: float,
    mde: float | None,
    min_s: int,
    max_s: int,
    secondary_ok: dict[int, Callable[[int], bool]],
    method: ev.Method | None = None,
) -> int | None:
    """Replay the daily looks; set each control pair's verdict. Returns the A/A invalid look.

    *family* = the Holm family (A/B: the control pairs; A/A: every pair). Looks are
    indexed by calendar day over every pair's sessions.
    """
    days = sorted({r.day for p in family for r in p.rows})
    for look, day in enumerate(days, start=1):
        for p in family:
            if p.decided_at is not None:
                continue
            k = sum(1 for r in p.rows if r.day <= day)
            if k == 0:
                continue
            p.p_min = min(p.p_min, _p_at(p.rows[:k], alpha=alpha, known=known, q=q, mde=mde,
                                         min_s=min_s, method=method))  # fmt: skip
        adj = stats.holm_adjust([p.p_min for p in family])
        if is_aa:
            if bool(np.any(adj < alpha)):
                return look
            continue
        for p, a in zip(family, adj, strict=True):
            if p.decided_at is not None:
                continue
            k = sum(1 for r in p.rows if r.day <= day)
            blocked = method is not None and any(ev.row_waits(r, method) for r in p.rows[:k])
            if blocked:
                continue  # D119: no win/loss/futility until SPY's return is in
            if k >= min_s and a < alpha:
                mean = _estimate(p.rows[:k], method)
                if mean < 0:
                    p.decided_at, p.verdict = k, "loss"
                    p.reason = f"Holm p {a:.4f} < {alpha} with mean {mean:+.4%}/day after {k}"
                    continue
                check = secondary_ok[id(p)]
                if check(k):
                    p.decided_at, p.verdict = k, "win"
                    p.reason = (
                        f"Holm p {a:.4f} < {alpha} with mean {mean:+.4%}/day and Sortino "
                        f"non-inferior after {k}"
                    )
                    continue
            if k >= max_s:
                p.decided_at, p.verdict = k, "futility"
                p.reason = f"max_sessions {max_s} reached without a win or loss"
    for p in pairs:
        if p.verdict is None:
            n = len(p.rows)
            p.verdict = "continue"
            p.reason = f"{n}/{min_s}-{max_s} sessions"
    return None


_D119_FIELDS = (
    "adjustment",
    "hac",
    "sigma_raw",
    "sigma_adj",
    "sigma_lr",
    "primary_unadjusted",
    "covariate_check",  # D120
)


def _d119(p: ev.Primary | ev.PairReport) -> dict[str, Any]:
    """The D119 fields of *p* (copied between Primary and PairReport)."""
    return {k: getattr(p, k) for k in _D119_FIELDS}


def _estimate(rows: list[ev.SessionRow], method: ev.Method | None) -> float:
    """The verdict's point estimate: the pre-registered intercept (D119), else the mean."""
    if method is not None and method.pre_registered:
        lr = ev._long_run(  # noqa: SLF001
            [r.d for r in rows], [r.spy_ret for r in rows], [r.day for r in rows], method,
            alpha=0.05, sigma_upper_q=0.05, mde=None, min_sessions=1,
            vs=[r.vix_chg for r in rows],
        )  # fmt: skip
        if lr is not None and lr["adjustment"].estimate is not None:
            return float(lr["adjustment"].estimate)
    return float(np.mean([r.d for r in rows]))


def build_multi_report(
    conn: sqlite3.Connection,
    st: ExperimentState,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    aa_sigma: float | None,
    arm_ids: dict[str, str],
    method: ev.Method | None = None,
    market: BarsProvider | None = None,
    vix: Callable[[], Mapping[_dt.date, float]] | None = None,
) -> ev.ExperimentReport:
    """The v2 report of a K-treatment experiment (*arm_ids*: t1..tK -> arm_id). Read-only.

    D119: *method* = the long-run primary's method (pre-registered: the verdicts read it);
    *market* = SPY's daily bars for sessions no stored report carries.
    """
    if method is None:
        method = ev.method_for(st, cfg, aa_sigma=aa_sigma, aa_cal=None)
    sp, run = st.spec, st.running
    assert run is not None  # noqa: S101 - build_report checks
    assert sp.alpha is not None and sp.power is not None  # noqa: S101
    assert sp.min_sessions is not None and sp.max_sessions is not None  # noqa: S101
    is_aa = sp.kind is ExperimentKind.AA
    known = None if is_aa else aa_sigma
    q = cfg.stats.sigma_upper_q
    t0_eq = run.t0_equity
    # D108: a spec v3's reference is the flat control arm XP-<n>:control (nothing is
    # excluded from it); up to v2 it is production minus the legacy book
    v3 = sp.control_runs
    c_aid = ev.control_arm_id(st)
    legacy: set[str] = set() if v3 else set(run.production_book)
    legacy_hashes = ev._legacy_hashes(conn, legacy)  # noqa: SLF001

    # -- pairs -----------------------------------------------------------------
    ctrl_pairs: list[_Pair] = []
    series = {aid: ev._series(conn, st, now=now, t_arm=aid) for aid in arm_ids.values()}  # noqa: SLF001
    spy = ev.covariate_series(
        conn, st, [r.day for rows, _ in series.values() for r in rows], market=market
    )
    dvix = ev.vix_series(conn, st, [r.day for rows, _ in series.values() for r in rows], vix=vix)
    for name, aid in arm_ids.items():
        rows, missing = series[aid]
        rows = ev.with_spy(conn, st, rows, spy=spy, vix=dvix)
        ctrl_pairs.append(
            _Pair(
                "control_vs_treatment", CONTROL_ARM, name, aid, rows, [r.d for r in rows], missing
            )  # fmt: skip
        )
    tt_pairs = [
        _Pair(
            "treatment_vs_treatment",
            a.b,
            b.b,
            b.arm_id,
            rows := _between(a.rows, b.rows),
            [r.d for r in rows],
        )  # fmt: skip
        for a, b in itertools.combinations(ctrl_pairs, 2)
    ]

    def seed(name: str, n: int) -> int:
        return stats.seed_for(st.experiment_id, name, n)

    def secondary(p: _Pair, n: int) -> ev.Secondary:
        rows = p.rows[:n]
        return ev._secondary_from(  # noqa: SLF001
            [r.treatment_pnl / t0_eq for r in rows],
            [r.control_pnl / t0_eq for r in rows],
            alpha=sp.alpha,  # type: ignore[arg-type]
            margin=sp.non_inferiority_margin,
            resamples=cfg.stats.bootstrap_resamples,
            seed=seed(p.b, n),
        )

    checks: dict[int, Callable[[int], bool]] = {
        id(p): (lambda n, p=p: bool(secondary(p, n).non_inferior)) for p in ctrl_pairs
    }
    # D108: a spec v3's family is the K control pairs in an A/A too (Ti vs Tj stays
    # descriptive); v2 A/As put every pair in the family (D69)
    family = [*ctrl_pairs, *tt_pairs] if is_aa and not v3 else ctrl_pairs
    invalid_look = _replay(
        ctrl_pairs, family, is_aa=is_aa, alpha=sp.alpha, known=known, q=q, mde=sp.mde,
        min_s=sp.min_sessions, max_s=sp.max_sessions, secondary_ok=checks, method=method,
    )  # fmt: skip

    # -- pair reports (numbers as of each arm's decision, or now) -------------
    def pair_report(p: _Pair, holm: float | None) -> ev.PairReport:
        n = p.decided_at if p.decided_at is not None else len(p.rows)
        prim = _primary_at(
            p.rows[:n], alpha=sp.alpha, known=known, q=q, mde=sp.mde,  # type: ignore[arg-type]
            min_s=sp.min_sessions, method=method, check=True,  # type: ignore[arg-type]
        )  # fmt: skip
        is_ctrl = p.kind == "control_vs_treatment"
        return ev.PairReport(
            kind=p.kind,  # type: ignore[arg-type]
            a=p.a,
            b=p.b,
            arm_id=p.arm_id,
            descriptive=not is_ctrl,
            n=prim.n,
            mean=prim.mean,
            ci=prim.ci,
            sigma=prim.sigma,
            sigma_source=prim.sigma_source,
            tau=prim.tau,
            p_value=prim.p_value,
            holm_p=holm,
            sample_sd=stats.sample_sd(p.d[:n]) if n >= 2 else None,
            **_d119(prim),
            secondary=secondary(p, n) if is_ctrl else None,
            verdict=p.verdict if is_ctrl else None,  # type: ignore[arg-type]
            verdict_reason=p.reason if is_ctrl else None,
            decided_day=p.rows[n - 1].day if p.decided_at is not None and n else None,
            missing_sessions=p.missing,
            series=p.rows[:n] if is_ctrl else [],
        )

    # Holm p reported = Holm over the (frozen) running-minimum p's of the family
    if is_aa:
        # the replay may have stopped early on invalid: finish each family pair's
        # running minimum (v3: the control pairs only; Ti vs Tj stays descriptive)
        for p in family:
            p.p_min = min(
                [p.p_min]
                + [
                    _p_at(
                        p.rows[:k],
                        alpha=sp.alpha,
                        known=known,
                        q=q,
                        mde=sp.mde,  # type: ignore[arg-type]
                        min_s=sp.min_sessions,
                        method=method,  # type: ignore[arg-type]
                    )  # fmt: skip
                    for k in range(1, len(p.d) + 1)
                ]
            )
    fam_adj = stats.holm_adjust([p.p_min for p in family])
    holm_of = {id(p): float(a) for p, a in zip(family, fam_adj, strict=True)}
    pairwise = [pair_report(p, holm_of.get(id(p))) for p in [*ctrl_pairs, *tt_pairs]]
    ctrl_reports = pairwise[: len(ctrl_pairs)]

    # -- all-arms view -----------------------------------------------------------
    ctrl_pnl_by_day = {r.day: r.control_pnl for p in ctrl_pairs for r in p.rows}
    c_pnl = [ctrl_pnl_by_day[d] for d in sorted(ctrl_pnl_by_day)]
    c_out = ev._closed_outcomes(conn, c_aid, run.t0, legacy_hashes)  # noqa: SLF001
    outcomes = {CONTROL_ARM: c_out}
    rows_all = [
        _arm_row(
            CONTROL_ARM,
            c_aid,
            c_pnl,
            t0_eq,
            ev._orders(conn, c_aid, run.t0, legacy),
            c_out,  # noqa: SLF001
            conflicts=cross_arm_conflicts(conn, c_aid, run.t0),
            primary=0.0,
            verdict=None,
        )  # fmt: skip
    ]
    for p, pr in zip(ctrl_pairs, ctrl_reports, strict=True):
        t_out = ev._closed_outcomes(conn, p.arm_id, run.t0, legacy_hashes)  # noqa: SLF001
        outcomes[p.b] = t_out
        full_mean = float(np.mean(p.d)) if p.d else None
        rows_all.append(
            _arm_row(
                p.b,
                p.arm_id,
                [r.treatment_pnl for r in p.rows],
                t0_eq,
                ev._orders(conn, p.arm_id, run.t0, set()),
                t_out,  # noqa: SLF001
                conflicts=cross_arm_conflicts(conn, p.arm_id, run.t0),
                primary=full_mean,
                verdict=pr.verdict,
            )  # fmt: skip
        )
    ranked = sorted(
        rows_all,
        key=lambda a: (a[1] is None, -(a[1] or 0.0), _arm_order(a[0].arm)),
    )
    rank_of = {a.arm: i for i, (a, _) in enumerate(ranked, start=1)}
    arms = [a.model_copy(update={"rank": rank_of[a.arm]}) for a, _ in rows_all]

    # -- headline pair, experiment verdict --------------------------------------
    headline_name = next(
        (a.arm for a, _ in ranked if a.arm != CONTROL_ARM),
        ctrl_pairs[0].b,
    )
    h_idx = next(i for i, p in enumerate(ctrl_pairs) if p.b == headline_name)
    h_pair, h_rep = ctrl_pairs[h_idx], ctrl_reports[h_idx]
    primary = ev.Primary(
        n=h_rep.n,
        mean=h_rep.mean,
        ci=h_rep.ci,
        sigma=h_rep.sigma,
        sigma_source=h_rep.sigma_source,
        tau=h_rep.tau,
        p_value=h_rep.p_value,
        lower_bound_positive=(
            h_rep.ci is not None and h_rep.ci.lo > 0.0 and not ev.covariate_blocked(h_rep)
        ),
        **_d119(h_rep),
    )
    assert h_rep.secondary is not None  # noqa: S101 - control pairs carry one
    ctrl_holm = [r.holm_p for r in ctrl_reports if r.holm_p is not None]
    omni_p = min(stats.holm_adjust([p.p_min for p in ctrl_pairs]).tolist()) if ctrl_holm else None
    omnibus = ev.Omnibus(
        k=len(ctrl_pairs), p_value=omni_p, differs=omni_p is not None and omni_p < sp.alpha
    )
    verdict, why = _experiment_verdict(
        ctrl_reports, pairwise, is_aa=is_aa, invalid_look=invalid_look, alpha=sp.alpha,
        max_s=sp.max_sessions,
    )  # fmt: skip

    # -- calibration (pooled over the control pairs) ----------------------------
    pooled_sd = _pooled_sd([p.d for p in ctrl_pairs])
    t_rows = arms[1:]
    pooled_t = _pooled_arm(t_rows)
    paired = diverged = 0
    for p in ctrl_pairs:
        a, b = ev._divergence(conn, p.arm_id, run.t0)  # type: ignore[arg-type]  # noqa: SLF001
        paired, diverged = paired + a, diverged + b
    cal = ev._calibration(  # noqa: SLF001
        pooled_sd, cfg, st, arms[0], pooled_t, paired=paired, diverged=diverged
    )
    cal = ev.hac_calibration(
        cal, [(p.d, [r.spy_ret for r in p.rows]) for p in ctrl_pairs],
        lag=method.analysis.hac_lag or cfg.stats.hac_lag, st=st, cfg=cfg,
    )  # fmt: skip
    if dvix:
        cal = ev.pooled_covariate_check(
            cal, [(p.d, [r.spy_ret for r in p.rows], [r.vix_chg for r in p.rows])
                  for p in ctrl_pairs],
            method=method, st=st, cfg=cfg,
        )  # fmt: skip
    cov_missing = sorted(
        {
            d
            for r in ctrl_reports
            if r.adjustment is not None
            for d in r.adjustment.covariate_missing
        }
    )
    all_days = [r.day for p in ctrl_pairs for r in p.rows]
    missing = sorted({d for p in ctrl_pairs for d in p.missing})
    sha = next(
        (s for s in (ev._treatment_sha(conn, p.arm_id) for p in ctrl_pairs) if s),  # type: ignore[arg-type]  # noqa: SLF001
        None,
    )
    production = None
    if v3:
        from arc.experiments.production import build_production_panel

        assert c_aid is not None  # noqa: S101 - a v3 control is an arm
        # descriptive only: built after every verdict above and read by none of them
        production = build_production_panel(
            conn, st, cfg, now=now, control_arm=c_aid, arm_ids=arm_ids
        )
    return ev.ExperimentReport(
        report_version=V3_REPORT_VERSION if v3 else MULTI_REPORT_VERSION,
        experiment_id=st.experiment_id,
        kind=sp.kind,
        area=sp.area.value,
        status=st.status,
        evaluated_at=now,
        t0=run.t0,
        t0_equity=t0_eq,
        legacy_book=sorted(legacy),
        as_of_day=max(all_days) if all_days else None,
        sessions=max((len(p.rows) for p in ctrl_pairs), default=0),
        min_sessions=sp.min_sessions,
        max_sessions=sp.max_sessions,
        alpha=sp.alpha,
        power=sp.power,
        series=h_pair.rows,
        missing_sessions=missing,
        primary=primary,
        secondary=h_rep.secondary,
        arms=arms,
        calibration=cal,
        breakdowns=ev._breakdowns(outcomes),  # noqa: SLF001
        verdict=verdict,
        verdict_reason=why,
        spec_hash=st.spec_hash,
        registered_hash=st.registered_hash,
        config_hash=ev.config_hash(cfg),
        config=ev.report_config(cfg),
        control_sha=run.control_sha,
        treatment_sha=sha,
        evaluator_sha=ev._evaluator_sha(),  # noqa: SLF001
        headline_arm=headline_name,
        pairwise=pairwise,
        omnibus=omnibus,
        reference=CONTROL_ARM if v3 else None,
        production=production,
        analysis=sp.analysis,
        covariate_missing=cov_missing if method.waits_on_covariate else None,
    )


def _arm_order(name: str) -> int:
    return 0 if name == CONTROL_ARM else int(name[1:]) if name[1:].isdigit() else 99


def _arm_row(
    name: str,
    aid: str | None,
    pnl: list[float],
    t0_equity: float,
    orders: tuple[int, int, int],
    outcomes: list[dict[str, object]],
    *,
    conflicts: int,
    primary: float | None,
    verdict: str | None,
) -> tuple[ev.ArmSummary, float | None]:
    base = ev._arm_summary(name, aid, pnl, t0_equity, orders, outcomes)  # type: ignore[arg-type]  # noqa: SLF001
    r = [p / t0_equity for p in pnl]
    row = base.model_copy(
        update={
            "cum_pnl_pct": float(sum(pnl)) / t0_equity,
            "daily_mean": float(np.mean(r)) if r else None,
            "daily_sd": stats.sample_sd(r) if len(r) >= 2 else None,
            "sortino": stats.sortino(r),
            "cross_arm_conflicts": conflicts,
            "verdict": verdict,
        }
    )
    return row, primary


def _pooled_sd(series: list[list[float]]) -> float | None:
    """sqrt(sum((n_i - 1) s_i^2) / sum(n_i - 1)) over the pairs with >= 2 sessions."""
    num = den = 0.0
    for d in series:
        if len(d) >= 2:
            num += (len(d) - 1) * stats.sample_sd(d) ** 2
            den += len(d) - 1
    return (num / den) ** 0.5 if den > 0 else None


def _pooled_arm(rows: list[ev.ArmSummary]) -> ev.ArmSummary:
    """The treatments as one arm for the calibration gaps (fills summed, slippage averaged)."""
    slips = [a.mean_slippage_bps for a in rows if a.mean_slippage_bps is not None]
    return ev.ArmSummary(
        arm="treatments",
        arm_id=None,
        sessions=max((a.sessions for a in rows), default=0),
        total_pnl=sum(a.total_pnl for a in rows),
        max_drawdown=max((a.max_drawdown for a in rows), default=0.0),
        orders=sum(a.orders for a in rows),
        filled_executions=sum(a.filled_executions for a in rows),
        executions=sum(a.executions for a in rows),
        mean_slippage_bps=float(sum(slips) / len(slips)) if slips else None,
    )


def _experiment_verdict(
    ctrl: list[ev.PairReport],
    pairwise: list[ev.PairReport],
    *,
    is_aa: bool,
    invalid_look: int | None,
    alpha: float,
    max_s: int,
) -> tuple[ev.Verdict, str]:
    if is_aa:
        if invalid_look is not None:
            bad = [p for p in pairwise if p.holm_p is not None and p.holm_p < alpha]
            names = ", ".join(f"{p.b} vs {p.a}" for p in bad) or "a pair"
            return "invalid", (
                f"A/A arms differ: {names} rejected at Holm alpha {alpha} (look "
                f"{invalid_look}); the harness is broken"
            )
        if all(len(p.series) >= max_s for p in ctrl):
            return "futility", (
                f"A/A complete after {max(len(p.series) for p in ctrl)} sessions: every pair "
                "null, as expected"
            )
        return "continue", _arms_summary(ctrl)
    if all(p.verdict != "continue" for p in ctrl):
        winners = [p.b for p in ctrl if p.verdict == "win"]
        if winners:
            return "win", f"{', '.join(winners)} won; " + _arms_summary(ctrl)
        return "futility", "no arm won; " + _arms_summary(ctrl)
    return "continue", _arms_summary(ctrl)


def _arms_summary(ctrl: list[ev.PairReport]) -> str:
    return "; ".join(f"{p.b} {p.verdict} (n={len(p.series)})" for p in ctrl)
