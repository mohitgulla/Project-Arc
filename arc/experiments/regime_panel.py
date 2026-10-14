"""Regime panel: production vs control (and Ti vs C) pooled across XPs by regime (D108, E25.5).

Every spec v3 XP starts a fresh, flat control arm on a different date. Pooled, their
sessions say how the production strategy (control runs production's exact config)
behaves by market regime and by start date. **Descriptive only:** nothing here is a
verdict, stop or promotion input. No module on the verdict path (``evaluate``,
``multi``, ``verdicts``, ``adopt``) imports this one, and the session
regime tag lives in the descriptive production panel, which the verdict ignores
(``tests/test_experiment_regime_panel.py``).

Input: the latest stored report of every v3 XP (running and concluded), read from
``experiment_reports`` by :func:`load_v3_reports`. :func:`build_regime_panel` is pure.

Per session ``s`` of an XP (its :class:`~arc.experiments.production.ProductionSession`):

- ``d_p_c`` and ``residual`` (fractions of the XP's t0 equity, E25.3);
- each treatment's ``(ti_pnl - c_pnl) / t0`` keyed ``XP-<n>:<ti>`` (treatments differ
  per XP, so they are never pooled by name across XPs) plus ``all``, every treatment
  session pooled;
- the regime tag (SPY regime v2 at the session close); a session without one goes to
  the bucket ``unknown`` (counted, never dropped).

Buckets come in four groupings: ``trend`` (bull/bear/sideways), ``vol`` (low/mid/high),
``trend_vol`` (``bull/low`` ...; a v1 read without a vol state is ``bull/na``) and
``transitional`` (``confirmed`` / ``transitional`` at the E17.2 reference run 3 /
margin z 0.10; ``na`` without run length or margin z). Each bucket gives n sessions,
the XPs it draws on, and mean + a 95% normal CI (``mean +- 1.96 sd/sqrt(n)``; sessions
treated as independent, so the CI is descriptive, not an always-valid test). A bucket
with fewer than :data:`THIN_N` sessions is shown and marked ``thin``.

Start date: one row per XP (t0, regime at its first session, cumulative ``d_p_c`` /
residual) and control's ramp curve (open structures, deployed max-loss capital as a
fraction of its equity, by session index since t0), averaged per regime-at-start
bucket (``trend_vol`` of the first session).
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
import json
import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.experiments import stats
from arc.experiments.production import SessionRegime  # noqa: TC001 - pydantic field type

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Sequence

    from arc.experiments.evaluate import ExperimentReport

__all__ = [
    "GROUPINGS",
    "PANEL_VERSION",
    "THIN_N",
    "UNKNOWN",
    "Bucket",
    "MeanCI",
    "RampPoint",
    "RegimePanel",
    "StartRow",
    "build_regime_panel",
    "load_v3_reports",
    "panel_json",
    "panel_lines",
]

#: Version of the JSON shape (the Analyst context reads it; bump on a breaking change).
PANEL_VERSION = 1
#: D108 / E25.5: a bucket with fewer sessions is shown but marked ``thin``.
THIN_N = 10
UNKNOWN = "unknown"
ALL_TREATMENTS = "all"
_Z95 = 1.959963984540054
_FORBID = ConfigDict(extra="forbid", frozen=True)

Grouping = Literal["trend", "vol", "trend_vol", "transitional"]
GROUPINGS: tuple[Grouping, ...] = ("trend", "vol", "trend_vol", "transitional")


class MeanCI(BaseModel):
    """Mean of a per-session series (fraction of t0 equity) with a 95% normal CI."""

    model_config = _FORBID

    n: int
    mean: float | None
    ci: stats.Interval | None = Field(None, description="mean +- 1.96 sd/sqrt(n); None if n < 2")


class Bucket(BaseModel):
    model_config = _FORBID

    grouping: Grouping
    key: str
    n: int = Field(..., description="Sessions in the bucket (P - C rows)")
    xps: list[str] = Field(default_factory=list, description="XPs contributing sessions")
    thin: bool = Field(..., description=f"n < {THIN_N}")
    d_p_c: MeanCI
    residual: MeanCI
    ti_minus_c: dict[str, MeanCI] = Field(
        default_factory=dict, description="XP-<n>:<ti> (and 'all') -> (Ti - C)/t0 per session"
    )


class RampPoint(BaseModel):
    """Control's ramp at one session index since t0 (0 = first session)."""

    model_config = _FORBID

    index: int
    n_xps: int
    c_open: float | None = Field(None, description="Mean open structures over the XPs")
    c_deployed: float | None = Field(None, description="Mean deployed fraction of equity")


class StartRow(BaseModel):
    model_config = _FORBID

    experiment_id: str
    status_verdict: str = Field(..., description="Latest report's verdict")
    t0: _dt.datetime
    first_session: _dt.date | None
    start_regime: str = Field(..., description="trend_vol bucket at the first session")
    regime_at_start: SessionRegime | None
    sessions: int
    cum_d_p_c: float
    cum_residual: float
    ramped_from: _dt.date | None
    ramp: list[RampPoint] = Field(default_factory=list)


class RegimePanel(BaseModel):
    """E25.5 (D108 (c)): pooled across v3 XPs. Never a verdict, stop or promotion input."""

    model_config = _FORBID

    panel_version: Literal[1] = PANEL_VERSION
    descriptive: Literal[True] = True
    thin_n: int = THIN_N
    xps: list[str] = Field(default_factory=list)
    sessions: int = 0
    unknown_sessions: int = Field(0, description="Sessions without a regime tag")
    buckets: list[Bucket] = Field(default_factory=list)
    starts: list[StartRow] = Field(default_factory=list)
    ramp_by_start_regime: dict[str, list[RampPoint]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure pooling
# ---------------------------------------------------------------------------


def _mean_ci(xs: Sequence[float]) -> MeanCI:
    n = len(xs)
    if n == 0:
        return MeanCI(n=0, mean=None, ci=None)
    mean = math.fsum(xs) / n
    if n < 2:
        return MeanCI(n=n, mean=mean, ci=None)
    half = _Z95 * stats.sample_sd(xs) / math.sqrt(n)
    return MeanCI(
        n=n,
        mean=mean,
        ci=stats.Interval(estimate=mean, lo=mean - half, hi=mean + half, level=0.95),
    )


def _keys(reg: SessionRegime | None) -> dict[Grouping, str]:
    if reg is None:
        return dict.fromkeys(GROUPINGS, UNKNOWN)
    trans = (
        "na" if reg.transitional is None else ("transitional" if reg.transitional else "confirmed")
    )
    return {
        "trend": reg.trend,
        "vol": reg.vol_state or "na",
        "trend_vol": f"{reg.trend}/{reg.vol_state or 'na'}",
        "transitional": trans,
    }


def _mean_or_none(xs: Iterable[float | int | None]) -> float | None:
    vals = [float(x) for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def _ramp(points: list[list[tuple[int | None, float | None]]]) -> list[RampPoint]:
    """Session index -> mean control ramp over the XPs that reached that index."""
    out: list[RampPoint] = []
    for i in range(max((len(p) for p in points), default=0)):
        at = [p[i] for p in points if i < len(p)]
        out.append(
            RampPoint(
                index=i,
                n_xps=len(at),
                c_open=_mean_or_none(o for o, _ in at),
                c_deployed=_mean_or_none(d for _, d in at),
            )
        )
    return out


@dataclass
class _Acc:
    xps: list[str] = field(default_factory=list)
    d_p_c: list[float] = field(default_factory=list)
    residual: list[float] = field(default_factory=list)
    ti: dict[str, list[float]] = field(default_factory=dict)


def build_regime_panel(reports: Iterable[ExperimentReport]) -> RegimePanel:
    """Pool the v3 reports' production panels by regime and start date (pure)."""
    rows: dict[tuple[Grouping, str], _Acc] = {}
    starts: list[StartRow] = []
    ramp_groups: dict[str, list[list[tuple[int | None, float | None]]]] = {}
    xps: list[str] = []
    n_sessions = unknown = 0
    for rep in sorted(reports, key=lambda r: (r.t0, r.experiment_id)):
        pp = rep.production
        if rep.report_version != 3 or pp is None:
            continue
        xps.append(rep.experiment_id)
        t0 = rep.t0_equity
        for s in pp.sessions:
            n_sessions += 1
            unknown += s.regime is None
            diffs = {
                f"{rep.experiment_id}:{name}": (pnl - s.c_pnl) / t0
                for name, pnl in sorted(s.ti_pnl.items())
            }
            for g, key in _keys(s.regime).items():
                b = rows.setdefault((g, key), _Acc())
                if rep.experiment_id not in b.xps:
                    b.xps.append(rep.experiment_id)
                b.d_p_c.append(s.d_p_c)
                b.residual.append(s.residual)
                for k, v in diffs.items():
                    b.ti.setdefault(k, []).append(v)
                    b.ti.setdefault(ALL_TREATMENTS, []).append(v)
        start_reg = pp.regime_at_start
        start_key = _keys(start_reg)["trend_vol"]
        curve = [(s.c_open, s.c_deployed) for s in pp.sessions]
        ramp_groups.setdefault(start_key, []).append(curve)
        starts.append(
            StartRow(
                experiment_id=rep.experiment_id,
                status_verdict=rep.verdict,
                t0=rep.t0,
                first_session=pp.sessions[0].day if pp.sessions else None,
                start_regime=start_key,
                regime_at_start=start_reg,
                sessions=len(pp.sessions),
                cum_d_p_c=pp.cum_d_p_c,
                cum_residual=pp.cum_residual,
                ramped_from=pp.ramped_from,
                ramp=_ramp([curve]),
            )
        )

    order = {g: i for i, g in enumerate(GROUPINGS)}
    buckets = [
        Bucket(
            grouping=g,
            key=key,
            n=len(b.d_p_c),
            xps=list(b.xps),
            thin=len(b.d_p_c) < THIN_N,
            d_p_c=_mean_ci(b.d_p_c),
            residual=_mean_ci(b.residual),
            ti_minus_c={
                k: _mean_ci(v)
                for k, v in sorted(b.ti.items(), key=lambda kv: (kv[0] != ALL_TREATMENTS, kv[0]))
            },
        )
        for (g, key), b in sorted(
            rows.items(), key=lambda kv: (order[kv[0][0]], kv[0][1] == UNKNOWN, kv[0][1])
        )
    ]
    return RegimePanel(
        xps=xps,
        sessions=n_sessions,
        unknown_sessions=unknown,
        buckets=buckets,
        starts=starts,
        ramp_by_start_regime={k: _ramp(v) for k, v in sorted(ramp_groups.items())},
    )


# ---------------------------------------------------------------------------
# I/O edge (read-only)
# ---------------------------------------------------------------------------


def load_v3_reports(conn: sqlite3.Connection) -> list[ExperimentReport]:
    """The latest stored report of every experiment whose latest report is v3."""
    from arc.experiments.evaluate import ExperimentReport

    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'experiment_reports'"
    ).fetchone():
        return []
    out: list[ExperimentReport] = []
    for r in conn.execute(
        """SELECT payload FROM experiment_reports
           WHERE id IN (SELECT MAX(id) FROM experiment_reports GROUP BY experiment_id)
             AND report_version = 3
           ORDER BY id"""
    ):
        rep = ExperimentReport.model_validate_json(r[0])
        if rep.production is not None:
            out.append(rep)
    return out


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.3f}%"


def _mc(m: MeanCI) -> str:
    ci = f" [{_pct(m.ci.lo)}, {_pct(m.ci.hi)}]" if m.ci else ""
    return f"{_pct(m.mean)}{ci}"


def panel_lines(panel: RegimePanel, *, out: Callable[[str], None] | None = None) -> list[str]:
    """The ``arc experiment regime-panel`` text (also the Analyst context fallback)."""
    if not panel.sessions:
        lines = ["no v3 sessions yet"]
    else:
        lines = [
            f"regime panel v{panel.panel_version} (D108, descriptive only: never a verdict, "
            "stop or promotion input)",
            f"  XPs {len(panel.xps)} ({', '.join(panel.xps)}); sessions {panel.sessions}; "
            f"untagged (unknown) {panel.unknown_sessions}; thin = n < {panel.thin_n}",
            "  per session, % of t0 equity: mean [95% CI]",
        ]
        current = None
        for b in panel.buckets:
            if b.grouping != current:
                current = b.grouping
                lines.append(f"  by {b.grouping}:")
            all_ti = b.ti_minus_c.get(ALL_TREATMENTS)
            lines.append(
                f"    {b.key:22} n {b.n:3}{' thin' if b.thin else '     '}  "
                f"P-C {_mc(b.d_p_c)}  residual {_mc(b.residual)}"
                + (f"  Ti-C(all) {_mc(all_ti)}" if all_ti else "")
                + f"  xps {','.join(b.xps)}"
            )
        lines.append("  by start date (t0, regime at start, cum P-C, cum residual, ramped from):")
        for s in panel.starts:
            last = s.ramp[-1] if s.ramp else None
            lines.append(
                f"    {s.experiment_id:7} {s.t0:%Y-%m-%d} {s.start_regime:15} sessions "
                f"{s.sessions:3}  P-C {_pct(s.cum_d_p_c)}  residual {_pct(s.cum_residual)}  "
                f"ramped {s.ramped_from or 'not yet'}"
                + (
                    f"  C open {last.c_open if last.c_open is not None else '-'} "
                    f"deployed {_pct(last.c_deployed)}"
                    if last
                    else ""
                )
                + f"  [{s.status_verdict}]"
            )
        lines.append("  control ramp by regime at start (index: XPs, mean open, deployed):")
        for key, pts in panel.ramp_by_start_regime.items():
            shown = ", ".join(
                f"{p.index}:{p.n_xps}/"
                f"{'-' if p.c_open is None else f'{p.c_open:.1f}'}/{_pct(p.c_deployed)}"
                for p in pts[:10]
            )
            lines.append(f"    {key:15} {shown}" + (" ..." if len(pts) > 10 else ""))
    if out is not None:
        for line in lines:
            out(line)
    return lines


def panel_json(panel: RegimePanel) -> str:
    return json.dumps(panel.model_dump(mode="json"), indent=2)
