"""Decision-value replays (E27.3, D126): the E7.19 baseline sim re-walked with M2.

Research replays, not BT studies (D121): one :func:`arc.backtest.sim_report.build_sim`
per test year (every C500 name, the profile's live config), then per family profile:

* ``incumbent``: the profile's incumbent ranker (``managed_net_ev``);
* ``ml_er``: rank by M2 ``e_r`` (selection; candidates without a score drop out);
* ``ml_er_pos``: the incumbent ranker, skipping candidates with ``e_r ≤ 0``;
* ``ml_pstop``: the incumbent ranker, skipping candidates with ``p_stop ≥ τ`` (τ = the
  80th percentile of the fold model's train-window ``p_stop``);
* the width ranker (``credit_width`` / ``debit_width``) as one more baseline.

Scores come from the fold model trained before that year (out of sample), joined to
the sim's candidates on ``(day, ticker, candidate key)`` through the E7.21 ledger
(built from the same sim menus). The coverage of that join is reported.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from arc.backtest.sim import SimCandidate, SimRun

ScoreMap = dict[tuple[dt.date, str, str], tuple[float, float]]

USES = ("incumbent", "ml_er", "ml_er_pos", "ml_pstop")


def score_map(rows: pd.DataFrame, scores: pd.DataFrame) -> ScoreMap:
    """``(day, ticker, key) -> (e_r, p_stop)``."""
    out: ScoreMap = {}
    for d, t, k, er, ps in zip(
        rows["date"], rows["ticker"], rows["key"], scores["e_r"], scores["p_stop"], strict=True
    ):
        out[(d, str(t), str(k))] = (float(er), float(ps))
    return out


class Override:
    """A ``key_override`` for :func:`arc.backtest.sim.run_sim` that counts its lookups."""

    def __init__(self, use: str, scores: ScoreMap, tau: float) -> None:
        self.use = use
        self.scores = scores
        self.tau = tau
        self.hit = 0
        self.miss = 0

    def __call__(self, sc: SimCandidate, k: tuple[Any, ...]) -> tuple[Any, ...] | None:
        c = sc.candidate
        s = self.scores.get((c.day, c.underlying, c.inputs.key))
        if s is None:
            self.miss += 1
            return None if self.use == "ml_er" else k
        self.hit += 1
        er, ps = s
        if self.use == "ml_er":
            if not math.isfinite(er):
                return None
            return (0, -er, k[2], k[3])
        if self.use == "ml_er_pos":
            return k if er > 0 else None
        if self.use == "ml_pstop":
            return k if ps < self.tau else None
        return k


def run_risk(run: SimRun) -> float:
    t = run.trades
    if not len(t):
        return 0.0
    return float((t["max_loss_unit"].astype(float) * t["contracts"].astype(float)).sum())


def summarize(
    run: SimRun, equity0: float, *, use: str, year: int, cov: float | None
) -> dict[str, Any]:
    from arc.backtest.sim_report import run_summary

    s = run_summary(run, equity0)
    risk = run_risk(run)
    net = float(s["net_pnl"])  # type: ignore[arg-type]
    t = run.trades
    by = {}
    if len(t):
        etf = t["etf"].fillna(False).astype(bool) if "etf" in t else pd.Series(False, index=t.index)
        by = {
            "net_stocks": round(float(t.loc[~etf, "pnl"].sum()), 2),
            "net_etfs": round(float(t.loc[etf, "pnl"].sum()), 2),
            "trades_stocks": int((~etf).sum()),
            "trades_etfs": int(etf.sum()),
        }
    return {
        "year": year,
        "use": use,
        "profile": s["profile"],
        "trades": s["trades"],
        "net_pnl": net,
        "max_dd_pct": s["max_dd_pct"],
        "return_pct": s["return_pct"],
        "win_rate": s["win_rate"],
        "risk_usd": round(risk, 2),
        "net_per_risk": round(net / risk, 4) if risk > 0 else None,
        "score_coverage": None if cov is None else round(cov, 4),
        **by,
    }


def replay_year(
    *,
    data_dir: Path,
    year: int,
    families: Mapping[str, tuple[str, ScoreMap, float]],
    workers: int = 6,
    end: dt.date | None = None,
    tickers: Sequence[str] | None = None,
    say: Callable[[str], None] = lambda _m: None,
) -> pd.DataFrame:
    """Walk every use for every family (``family -> (profile, scores, tau)``) on *year*."""
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.sim_report import build_sim, walk
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.scanner.rank import Ranker

    cfg = load_ranking_file()
    start = dt.date(year, 1, 1)
    stop = end or dt.date(year, 12, 31)
    profiles = [p for p, _s, _t in families.values()]
    b = build_sim(
        data_dir=data_dir,
        universe="c500",
        start=start,
        end=stop,
        data_end=stop + dt.timedelta(days=95),
        profiles=profiles,
        rankers=[Ranker.CREDIT_WIDTH, Ranker.DEBIT_WIDTH],
        cfg=cfg,
        cost=load_cost_model(),
        exits=load_exit_config(),
        settings=ArcSettings(),
        tickers=tickers,
        workers=workers,
    )
    say(f"  sim build {year}: {len(b.names)} names, {b.build_s:.0f}s")
    eq0 = float(cfg.backtest.sim.equity)
    rows = []
    for fam, (profile, scores, tau) in families.items():
        inc = b.rankers[profile][0]
        for use in USES:
            ov = None if use == "incumbent" else Override(use, scores, tau)
            run = walk(b, profile=profile, ranker=inc, cfg=cfg, end=stop, key_override=ov)
            cov = None if ov is None else ov.hit / max(ov.hit + ov.miss, 1)
            rows.append({"family": fam, **summarize(run, eq0, use=use, year=year, cov=cov)})
        for r in b.rankers[profile][1:]:
            run = walk(b, profile=profile, ranker=r, cfg=cfg, end=stop)
            rows.append({"family": fam, **summarize(run, eq0, use=r.value, year=year, cov=None)})
    return pd.DataFrame(rows)


def replay_checks(rep: pd.DataFrame) -> pd.DataFrame:
    """Per family × year: does ``ml_er`` beat the incumbent on net **and** max DD?"""
    out = []
    for (fam, year), g in rep.groupby(["family", "year"]):
        inc = g[g.use == "incumbent"].iloc[0]
        ml = g[g.use == "ml_er"].iloc[0]
        out.append(
            {
                "family": fam,
                "year": int(year),
                "net_incumbent": inc.net_pnl,
                "net_ml_er": ml.net_pnl,
                "dd_incumbent": inc.max_dd_pct,
                "dd_ml_er": ml.max_dd_pct,
                "replay_pass": bool(ml.net_pnl > inc.net_pnl and ml.max_dd_pct > inc.max_dd_pct),
            }
        )
    return pd.DataFrame(out)


def apply_rule(checks: pd.DataFrame, rchecks: pd.DataFrame) -> pd.DataFrame:
    """The full ``m2_promote`` rule per fold: log-loss, IC and replay legs, both years."""
    rc = rchecks.copy()
    rc["fold"] = "test" + rc["year"].astype(str)
    j = checks.merge(
        rc[["fold", "replay_pass", "net_incumbent", "net_ml_er", "dd_incumbent", "dd_ml_er"]],
        on="fold",
        how="left",
    )
    j["replay_pass"] = j["replay_pass"].fillna(False).astype(bool)
    j["year_pass"] = j["beats_logistic"] & j["beats_managed_pop"] & j["ic_pass"] & j["replay_pass"]
    return j


def rule_passed(rule: pd.DataFrame) -> bool:
    return bool(len(rule) >= 2 and rule["year_pass"].all())  # noqa: PLR2004


def coverage_note(rep: pd.DataFrame) -> str:
    c = rep["score_coverage"].dropna()
    return f"{c.min():.1%}–{c.max():.1%}" if len(c) else "n/a"
