"""``arc backtest sim``: run the E7.11 live-shaped simulator and write its outputs.

Per (profile, ranker) run, under ``<out>/<profile>/<ranker>/``:

* ``trades.csv``: one row per open (size, fill, exit, exit delays, capacity clip);
* ``skips.csv``: every skip with its reason code (:data:`arc.backtest.sim.SKIP_REASONS`);
* ``equity.csv``: the daily marked equity series;
* ``decisions.csv``: per day eligible / screened / active / candidates / opened;
* ``sleeves.csv`` (E7.19): per entry-DTE sleeve n, net, per-$-risk, win rate, exit
  mix, mean DTE at entry and mean days held (:func:`sleeve_table`).

Plus ``<out>/manifest.json`` (git sha, config hashes, universe version + checkpoint,
partitions, cache hit rate, runtime, peak RSS) and ``<out>/report.md``. Aggregates
only: no raw quote rows are written (ThetaData retail terms).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic fields
import hashlib
import json
import resource
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.rank_report import _stances
from arc.backtest.ranking import live_incumbent, profile_allows_credit, rankers_for
from arc.backtest.sim import (
    GATE_RULES_APPLIED,
    GATE_RULES_NOT_APPLIED,
    NOT_REPLAYED,
    SKIP_REASONS,
    SimRun,
    SimTicker,
    ViewSpec,
    build_view,
    market_trends,
    run_sim,
    sim_ticker_job,
)
from arc.config import ArcSettings  # noqa: TC001 - pydantic field
from arc.gate.inputs import MarketTrend  # noqa: TC001 - pydantic field
from arc.scanner.rank import Ranker  # noqa: TC001 - pydantic field

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from arc.backtest.costs import CostModel
    from arc.backtest.ranking import RankingFile
    from arc.exits.policy import ExitConfig

log = structlog.get_logger()

__all__ = ["SimBuild", "SimManifest", "build_sim", "run_sim_report", "sleeve_table", "walk"]


class SimManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git_sha: str
    config_hashes: dict[str, str]
    universe: str
    universe_version: str
    provider: str
    fallback_provider: str | None
    partitions: dict[str, int] = Field(..., description="Tickers per data partition")
    start: dt.date
    end: dt.date
    data_end: dt.date
    profiles: list[str]
    rankers: dict[str, list[str]]
    equity: float
    tickers: int
    workers: int
    cache_hits: int
    cache_misses: int
    cache_hit_rate: float | None
    runtime_s: dict[str, float]
    peak_rss_mb: float
    gate_rules_applied: list[str]
    gate_rules_not_applied: list[str]
    not_replayed: str


def _sha(obj: object) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError:  # pragma: no cover - git missing
        return "unknown"
    return out.stdout.strip() or "unknown"


def peak_rss_mb() -> float:
    """Peak RSS of this process and its finished children, MB (macOS bytes, Linux KB)."""
    scale = 1.0 / (1024 * 1024) if sys.platform == "darwin" else 1.0 / 1024
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return round(max(own, kids) * scale, 1)


def _chunks(names: Sequence[str], n: int) -> list[list[str]]:
    n = max(1, min(n, len(names)))
    return [list(names[i::n]) for i in range(n)]


def build_tickers(
    names: Sequence[str],
    *,
    spec: ViewSpec,
    workers: int,
    job_kw: dict[str, object],
) -> dict[str, SimTicker]:
    """Per-ticker work (menus, outcomes, eligibility) on *workers* processes."""
    if workers > 1 and len(names) > 1:
        # several small batches per worker so a slow name does not idle the pool
        batches = _chunks(names, min(len(names), workers * 4))
        out: dict[str, SimTicker] = {}
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(sim_ticker_job, b, spec=spec, **job_kw) for b in batches]  # type: ignore[arg-type]
            for f in futs:
                out |= {r.ticker: r for r in f.result()}
        return out
    return {r.ticker: r for r in sim_ticker_job(list(names), spec=spec, **job_kw)}  # type: ignore[arg-type]


def _write_run(run: SimRun, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    run.trades.to_csv(d / "trades.csv", index=False)
    run.skips.to_csv(d / "skips.csv", index=False)
    run.equity.rename_axis("day").to_csv(d / "equity.csv")
    run.decisions.to_csv(d / "decisions.csv", index=False)
    sleeve_table(run.trades).to_csv(d / "sleeves.csv", index=False)


def sleeve_table(trades: pd.DataFrame) -> pd.DataFrame:
    """E7.19: one row per entry-DTE sleeve (plus ``all``) of a run's trades.

    ``per_risk`` = net / Σ max loss (contracts x ``max_loss_unit``); ``exit_mix`` =
    ``reason:count`` joined by spaces, most frequent first (ties by name).
    """
    cols = [
        "sleeve",
        "n",
        "net",
        "per_risk",
        "win_rate",
        "exit_mix",
        "mean_dte_entry",
        "mean_days_held",
    ]
    if trades.empty or "sleeve" not in trades:
        return pd.DataFrame(columns=cols)

    def row(name: str, g: pd.DataFrame) -> dict[str, object]:
        risk = float((g["contracts"] * g["max_loss_unit"]).sum())
        net = float(g["pnl"].sum())
        vc = g["exit_reason"].astype(str).value_counts()
        mix = sorted(vc.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
        return {
            "sleeve": name,
            "n": len(g),
            "net": round(net, 2),
            "per_risk": round(net / risk, 4) if risk else None,
            "win_rate": round(float((g["pnl"] > 0).mean()), 3),
            "exit_mix": " ".join(f"{k}:{int(v)}" for k, v in mix),
            "mean_dte_entry": round(float(g["dte_entry"].mean()), 1),
            "mean_days_held": round(float(g["days_held"].mean()), 1),
        }

    rows = [row(str(k), g) for k, g in trades.groupby("sleeve", sort=True)]
    rows.append(row("all", trades))
    return pd.DataFrame(rows, columns=cols)


def run_summary(run: SimRun, equity0: float) -> dict[str, object]:
    t = run.trades
    dec = run.decisions[run.decisions["decision"]] if len(run.decisions) else run.decisions
    eq = run.equity
    dd = float((eq / eq.cummax() - 1.0).min()) if len(eq) else 0.0
    end_eq = float(eq.iloc[-1]) if len(eq) else equity0
    return {
        "profile": run.profile,
        "ranker": run.ranker,
        "trades": len(t),
        "names": int(t["underlying"].nunique()) if len(t) else 0,
        "net_pnl": round(float(t["pnl"].sum()), 2) if len(t) else 0.0,
        "return_pct": round((end_eq / equity0 - 1.0) * 100, 2),
        "max_dd_pct": round(dd * 100, 2),
        "win_rate": round(float((t["pnl"] > 0).mean()), 3) if len(t) else None,
        "clipped": int(t["capacity_clipped"].sum()) if len(t) else 0,
        "mean_eligible": round(float(dec["eligible"].mean()), 1) if len(dec) else 0.0,
        "mean_screened": round(float(dec["screened"].mean()), 1) if len(dec) else 0.0,
        "mean_active": round(float(dec["active"].mean()), 1) if len(dec) else 0.0,
        "mean_candidates": round(float(dec["candidates"].mean()), 1) if len(dec) else 0.0,
        "opened": int(dec["opened"].sum()) if len(dec) else 0,
    }


def sleeve_frame(runs: Sequence[SimRun]) -> pd.DataFrame:
    """:func:`sleeve_table` of every run, with its profile and ranker."""
    parts = [sleeve_table(r.trades).assign(profile=r.profile, ranker=r.ranker) for r in runs]
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    return df[["profile", "ranker", *[c for c in df.columns if c not in ("profile", "ranker")]]]


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_none_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines) + "\n"


def render_report(
    manifest: SimManifest,
    summaries: pd.DataFrame,
    skips: pd.DataFrame,
    sim_cfg: object,
    sleeves: pd.DataFrame | None = None,
) -> str:
    m = manifest
    hit = "n/a" if m.cache_hit_rate is None else f"{m.cache_hit_rate:.1%}"
    return (
        f"# Live-shaped portfolio simulation (E7.11)\n\n"
        f"> {m.not_replayed}\n\n"
        f"- universe **{m.universe}** ({m.universe_version}), {m.tickers} names; partitions "
        f"{m.partitions}\n"
        f"- decisions {m.start} -> {m.end} (exits settle by {m.data_end}); profiles "
        f"{', '.join(m.profiles)}; equity ${m.equity:,.0f}\n"
        f"- git {m.git_sha}; config hashes {m.config_hashes}\n"
        f"- runtime {m.runtime_s} s; peak RSS {m.peak_rss_mb} MB; workers {m.workers}; "
        f"menu cache {m.cache_hits} hits / {m.cache_misses} misses ({hit})\n"
        f"- sim settings: `{json.dumps(sim_cfg, default=str)}`\n\n"
        f"## Runs\n\n{_md_table(summaries)}\n"
        f"## Skip reasons (count per run)\n\n{_md_table(skips)}\n"
        + (f"## Entry-DTE sleeves (E7.19)\n\n{_md_table(sleeves)}\n" if sleeves is not None else "")
        + "## Gate rules\n\nApplied (pure `arc.gate.rules`):\n\n"
        + "".join(f"- {r}\n" for r in m.gate_rules_applied)
        + "\nNot applied:\n\n"
        + "".join(f"- {r}\n" for r in m.gate_rules_not_applied)
    )


class SimBuild(BaseModel):
    """Everything one config's walks share: the built tickers and the run frame."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    built: dict[str, SimTicker]
    sessions: list[dt.date]
    names: list[str]
    partitions: dict[str, int]
    universe_version: str
    per_profile: dict[str, ArcSettings]
    rankers: dict[str, list[Ranker]]
    build_s: float
    #: D131 (E28.2): SPY close vs its SMA per session for short_premium_regime; None =
    #: the rule is off or no SPY closes in the data dir (rule not applied, logged).
    trends: dict[dt.date, MarketTrend] | None = None


def build_sim(
    *,
    data_dir: Path,
    universe: str,
    start: dt.date,
    end: dt.date,
    data_end: dt.date,
    profiles: Sequence[str],
    rankers: Sequence[Ranker],
    cfg: RankingFile,
    cost: CostModel,
    exits: ExitConfig,
    settings: ArcSettings,
    tickers: Sequence[str] | None = None,
    workers: int = 1,
    cache: bool = True,
    cache_dir: Path | None = None,
) -> SimBuild:
    """Build every ticker of *universe* (parallel) for *profiles* × their rankers.

    The profile's incumbent ranker always runs; *rankers* adds applicable ones
    (:func:`arc.backtest.ranking.rankers_for`). Shared by ``arc backtest sim`` and the
    E7.12 study runner (:mod:`arc.backtest.study`).
    """
    t0 = time.monotonic()
    bt = cfg.backtest
    spec = ViewSpec(
        data_dir=str(data_dir),
        universe=universe,
        start=start,
        end=end,
        data_end=data_end,
        flat_rate=bt.risk_free_rate,
        min_iv_obs=settings.iv_min_obs_rank,
    )
    view = build_view(spec, cfg, cost)
    if tickers is None:  # E7.9: refuse a checkpoint the pull has not completed
        from arc.backtest.pull import require_checkpoint

        require_checkpoint(data_dir, universe, provider=bt.data.provider, version=view.version)
    names = sorted({t.upper() for t in (tickers or view.symbols)})
    names = [t for t in names if view.provider_for(t) is not None]
    partitions: dict[str, int] = {}
    for t in names:
        p = view.provider_for(t) or "none"
        partitions[p] = partitions.get(p, 0) + 1
    per_profile = {p: settings.with_profile(p) for p in profiles}
    windows = {p: bt.window_for(p, s.entry_dte_window) for p, s in per_profile.items()}
    allows = {p: profile_allows_credit(p, settings.account_profiles_file) for p in profiles}
    # E7.19 (D128): the live menu measure's ranker is the incumbent (first) ranker
    rk = {
        p: rankers_for(
            rankers,
            allows_credit=allows[p],
            incumbent=live_incumbent(exits.pipeline.menu_measure, allows_credit=allows[p]),
        )
        for p in profiles
    }
    job_kw: dict[str, object] = {
        "cfg": cfg,
        "exits": exits,
        "cost": cost,
        "settings": per_profile,
        "windows": windows,
        "rankers": rk,
        "stances": {p: _stances(p, cfg, settings) for p in profiles}
        if bt.stance == "trend"
        else None,
        "sim": bt.sim,
        "cache_dir": str(cache_dir or data_dir) if cache else None,
    }
    built = build_tickers(names, spec=spec, workers=workers, job_kw=job_kw)
    sessions = sorted({d for r in built.values() for d in r.closes.index if d >= start})
    trends = _market_trends(data_dir, settings, sessions, start, end)
    return SimBuild(
        built=built,
        sessions=sessions,
        names=names,
        partitions=partitions,
        universe_version=view.version,
        per_profile=per_profile,
        rankers=rk,
        build_s=time.monotonic() - t0,
        trends=trends,
    )


#: D131 (E28.2): calendar days of SPY history read before the walk's start (SMA warm-up).
_TREND_WARMUP_DAYS = 400


def _market_trends(
    data_dir: Path,
    settings: ArcSettings,
    sessions: Sequence[dt.date],
    start: dt.date,
    end: dt.date,
) -> dict[dt.date, MarketTrend] | None:
    """D131 (E28.2): SPY's point-in-time close vs SMA(``regime_gate_ma``) per session.

    Read offline from the data dir's cached daily closes. ``None`` when the rule is
    off or no SPY closes are cached (the walk then does not apply the rule; logged so
    a report never silently drops it).
    """
    if not settings.regime_gate_enabled:
        return None
    from arc.backtest.underlying import UnderlyingStore, load_closes

    lo = start - dt.timedelta(days=_TREND_WARMUP_DAYS)
    closes = load_closes(UnderlyingStore(data_dir), "SPY", lo, end)
    if closes.empty:
        log.warning("backtest.sim.regime_gate_no_spy", data_dir=str(data_dir))
        return None
    return market_trends(closes, settings.regime_gate_ma, sessions)


def walk(b: SimBuild, *, profile: str, ranker: Ranker, cfg: RankingFile, end: dt.date) -> SimRun:
    """One profile × ranker walk over a :class:`SimBuild`."""
    from arc.universe.config import MomentumConfig

    return run_sim(
        b.built,
        ranker=ranker,
        profile=profile,
        settings=b.per_profile[profile],
        sim=cfg.backtest.sim,
        filters=cfg.ranking.filters,
        vrp_threshold=cfg.ranking.vrp_threshold,
        sessions=b.sessions,
        end=end,
        aliases=MomentumConfig().share_class_aliases,
        trends=b.trends,
    )


def run_sim_report(
    *,
    data_dir: Path,
    universe: str,
    start: dt.date,
    end: dt.date,
    data_end: dt.date | None,
    profiles: Sequence[str],
    rankers: Sequence[Ranker],
    out_dir: Path,
    cfg: RankingFile,
    cost: CostModel,
    exits: ExitConfig,
    settings: ArcSettings,
    tickers: Sequence[str] | None = None,
    workers: int = 1,
    cache: bool = True,
    cache_dir: Path | None = None,
) -> SimManifest:
    """Build every ticker (parallel), walk each profile × ranker, write the outputs."""
    t0 = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    bt = cfg.backtest
    sim = bt.sim
    ds = bt.data
    data_end = data_end or end
    b = build_sim(
        data_dir=data_dir,
        universe=universe,
        start=start,
        end=end,
        data_end=data_end,
        profiles=profiles,
        rankers=rankers,
        cfg=cfg,
        cost=cost,
        exits=exits,
        settings=settings,
        tickers=tickers,
        workers=workers,
        cache=cache,
        cache_dir=cache_dir,
    )
    t_build = b.build_s
    rk = b.rankers
    runs: list[SimRun] = []
    t1 = time.monotonic()
    for p in profiles:
        for r in rk[p]:
            run = walk(b, profile=p, ranker=r, cfg=cfg, end=end)
            _write_run(run, out_dir / p / r.value)
            runs.append(run)
    t_walk = time.monotonic() - t1
    built = b.built
    hits = sum(r.cache_hits for r in built.values())
    misses = sum(r.cache_misses for r in built.values())
    manifest = SimManifest(
        git_sha=_git_sha(),
        config_hashes={
            "ranking": _sha(cfg.model_dump(mode="json")),
            "exits": _sha(exits.model_dump(mode="json")),
            "costs": _sha(cost.model_dump(mode="json")),
            "sim": _sha(sim.model_dump(mode="json")),
        },
        universe=universe,
        universe_version=b.universe_version,
        provider=ds.provider,
        fallback_provider=ds.fallback_provider,
        partitions=b.partitions,
        start=start,
        end=end,
        data_end=data_end,
        profiles=list(profiles),
        rankers={p: [r.value for r in v] for p, v in rk.items()},
        equity=sim.equity,
        tickers=len(b.names),
        workers=workers,
        cache_hits=hits,
        cache_misses=misses,
        cache_hit_rate=(hits / (hits + misses)) if hits + misses else None,
        runtime_s={
            "build": round(t_build, 1),
            "walk": round(t_walk, 1),
            "total": round(time.monotonic() - t0, 1),
        },
        peak_rss_mb=peak_rss_mb(),
        gate_rules_applied=list(GATE_RULES_APPLIED),
        gate_rules_not_applied=list(GATE_RULES_NOT_APPLIED),
        not_replayed=NOT_REPLAYED,
    )
    summaries = pd.DataFrame([run_summary(r, sim.equity) for r in runs])
    skips = pd.DataFrame(
        [{"profile": r.profile, "ranker": r.ranker, **r.skip_counts} for r in runs],
        columns=["profile", "ranker", *SKIP_REASONS],
    )
    sleeves = sleeve_frame(runs)
    summaries.to_csv(out_dir / "summary.csv", index=False)
    skips.to_csv(out_dir / "skip_counts.csv", index=False)
    (out_dir / "manifest.json").write_text(manifest.model_dump_json(indent=1))
    (out_dir / "report.md").write_text(
        render_report(manifest, summaries, skips, sim.model_dump(mode="json"), sleeves)
    )
    log.info("backtest.sim.done", out=str(out_dir), **manifest.runtime_s)
    return manifest
