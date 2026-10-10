"""``arc backtest rank``: run every ranker per account profile and write the E7.5 report.

Pipeline (see :mod:`arc.backtest.ranking` for the method):

1. Per ticker (in parallel with ``workers > 1``): prepare the EOD chains once, then
   for every profile × slippage build the decision-time menus and simulate the
   outcome of every candidate any ranker would pick.
2. Per profile × slippage × ranker: run the portfolio (sizing D18, gate caps,
   cash) over all tickers.
3. Metrics, sub-periods, bootstrap, the pre-registered decision, charts, CSVs and
   ``report.md`` in ``out_dir``.

Deterministic: fixed MC seed (``config/exits.yaml``), fixed bootstrap seed
(``config/ranking.yaml``), sorted iteration everywhere. Same inputs → same report.
"""

from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import structlog

from arc.backtest.engine import prepare_chains
from arc.backtest.entry_filter import (
    apply_be_filter,
    apply_entry_filter,
    atr14_days,
    stretched_days,
)
from arc.backtest.ranking import (
    Candidate,
    Outcome,
    RankingFile,
    RankRun,
    apply_stance,
    build_menus,
    build_menus_by_kind,
    clean_chains,
    decide,
    kind_costs,
    labels_for,
    picked_outcomes,
    profile_allows_credit,
    rankers_for,
    remark_chains,
    run_portfolio,
    stance_hit_rate,
    stance_kinds,
    subperiod_stats,
    summarize,
    trend_stances,
)
from arc.backtest.report import format_table
from arc.exits.policy import load_exit_config

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from arc.backtest.costs import CostModel
    from arc.config import ArcSettings
    from arc.data.history.store import ParquetHistoryStore
    from arc.exits.policy import ExitConfig
    from arc.scanner.rank import Ranker

log = structlog.get_logger()

__all__ = ["TickerResult", "root_cause", "run_rank_report", "ticker_job"]

_Key = tuple[str, float]  # (profile, slippage)
# E7.5b: forward window for the trend proxy's hit rate, in sessions (~ the mean
# expected hold of the E7.5 picks, 20-22 calendar days)
HIT_RATE_SESSIONS = 15


class TickerResult:
    """Menus and picked outcomes for one ticker, per (profile, slippage)."""

    def __init__(
        self,
        ticker: str,
        menus: dict[_Key, dict[dt.date, list[Candidate]]],
        outcomes: dict[_Key, dict[tuple[str, dt.date, str], Outcome | None]],
        trend: pd.Series,
        vol: pd.Series,
        filtered: dict[_Key, int] | None = None,
        be_dropped: dict[_Key, int] | None = None,
    ) -> None:
        self.ticker = ticker
        self.menus = menus
        self.outcomes = outcomes
        self.trend = trend
        self.vol = vol
        # E16.3: (profile, slippage) -> decision days the entry filter emptied of long premium
        self.filtered = filtered or {}
        # E16.5: (profile, slippage) -> debit candidates backtest.max_be_atr dropped
        self.be_dropped = be_dropped or {}


def ticker_job(
    ticker: str,
    raw: pd.DataFrame,
    closes: pd.Series,
    *,
    start: dt.date,
    end: dt.date,
    cfg: RankingFile,
    exits: ExitConfig,
    cost: CostModel,
    windows: Mapping[str, tuple[int, int]],
    rankers: Mapping[str, Sequence[Ranker]],
    slippages: Sequence[float],
    stances: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
    ohlc: pd.DataFrame | None = None,
) -> TickerResult:
    """Everything that depends on one ticker only (safe to run in a worker process).

    *ohlc* (split-adjusted daily bars) feeds the E16.3 entry filter; required when
    ``backtest.entry_filter`` is not ``none`` (an empty frame filters nothing).
    """
    closes = closes.sort_index()
    bt = cfg.backtest
    mc = exits.model.model_copy(update={"n_paths": bt.n_paths})
    chains = prepare_chains(raw, closes, cost=cost, r=bt.risk_free_rate, dte_min=1, dte_max=70)
    if bt.marks == "smile":
        chains = remark_chains(chains, closes, r=bt.risk_free_rate, cost=cost)
    else:
        chains = clean_chains(chains, max_dev=bt.max_leg_iv_dev, window=bt.smile_window)
    days = [d for d in sorted(chains) if start <= d <= end and d in closes.index]
    trend, vol = labels_for(closes)
    stretched: dict[dt.date, frozenset[str]] | None = None
    if bt.entry_filter != "none":
        stretched = stretched_days(
            ohlc if ohlc is not None else pd.DataFrame(),
            days,
            bt.anti_chase,
            vwap=bt.entry_filter == "anti_chase_vwap",
        )
    filtered: dict[_Key, int] = {}
    # E16.5: ATR14 per decision day (raw price units) for backtest.max_be_atr
    atr = (
        atr14_days(ohlc if ohlc is not None else pd.DataFrame(), closes, days)
        if bt.max_be_atr is not None
        else None
    )
    be_dropped: dict[_Key, int] = {}
    # E7.5b: the tilted rankers' stance per session (the same trend proxy as the menu)
    day_stance = trend_stances(trend) if bt.direction_tilt > 0 else None
    menus: dict[_Key, dict[dt.date, list[Candidate]]] = {}
    outcomes: dict[_Key, dict[tuple[str, dt.date, str], Outcome | None]] = {}
    for profile, (lo, hi) in sorted(windows.items()):
        specs = bt.specs_for(profile, lo, hi)
        for x in slippages:
            c = cost.model_copy(update={"slippage_frac": x})
            # E7.5a: the base run (x = costs.yaml) uses the measured per-kind slippage.
            ck = (
                kind_costs(specs, c, bt.slippage_by_kind)
                if bt.slippage_by_kind and x == cost.slippage_frac
                else {}
            )
            if ck:
                m = build_menus_by_kind(
                    chains,
                    closes,
                    days=days,
                    underlying=ticker,
                    specs=specs,
                    cost=c,
                    cost_by_kind=ck,
                    exits=exits,
                    mc=mc,
                    r=bt.risk_free_rate,
                    stances=day_stance,
                    tilt=bt.direction_tilt,
                )
            else:
                m = build_menus(
                    chains,
                    closes,
                    days=days,
                    underlying=ticker,
                    specs=specs,
                    cost=c,
                    exits=exits,
                    mc=mc,
                    r=bt.risk_free_rate,
                    stances=day_stance,
                    tilt=bt.direction_tilt,
                )
            if stances is not None:
                m = apply_stance(m, trend, stances[profile])
            if stretched is not None:  # E16.3: after the stance, before any ranker picks
                m, filtered[(profile, x)] = apply_entry_filter(m, trend, stretched)
            if atr is not None and bt.max_be_atr is not None:  # E16.5: same place
                m, be_dropped[(profile, x)] = apply_be_filter(m, closes, atr, bt.max_be_atr)
            menus[(profile, x)] = m
            outcomes[(profile, x)] = picked_outcomes(
                m,
                rankers=rankers[profile],
                filters=cfg.ranking.filters,
                vrp_threshold=cfg.ranking.vrp_threshold,
                chains=chains,
                closes=closes,
                cost=c,
                exits=exits,
                cost_by_kind=ck,
            )
    log.info("backtest.rank_ticker", ticker=ticker, sessions=len(days))
    return TickerResult(ticker, menus, outcomes, trend, vol, filtered, be_dropped)


# ---------------------------------------------------------------------------
# Root cause of a bad trade (deterministic, E7.4 RootCause vocabulary)
# ---------------------------------------------------------------------------


def root_cause(row: Mapping[str, object]) -> tuple[str, float]:
    """(E7.4 ``RootCause`` value, underlying move in implied σ) for one trade row.

    Heuristic, stated in the report:
    - costs larger than the mid P&L loss → ``execution_slippage``;
    - underlying moved ≥ 1.5 implied σ over the hold → ``regime_misread``;
    - closed by the stop on a smaller move → ``exit_management``;
    - otherwise → ``strike_selection``.
    """
    pnl, pnl_mid = float(row["pnl"]), float(row["pnl_mid"])  # type: ignore[arg-type]
    iv = float(row["atm_iv"])  # type: ignore[arg-type]
    days = max(int(row["days_held"]), 1)  # type: ignore[call-overload]
    s0, s1 = float(row["spot_entry"]), float(row["spot_exit"])  # type: ignore[arg-type]
    move = math.log(s1 / s0) if s0 > 0 and s1 > 0 else 0.0
    sig = iv * math.sqrt(days / 365.0)
    z = move / sig if sig > 0 else 0.0
    cost = pnl_mid - pnl
    if pnl < 0 and cost > abs(min(pnl_mid, 0.0)):
        return "execution_slippage", z
    if abs(z) >= 1.5:
        return "regime_misread", z
    if row["exit_reason"] == "stop":
        return "exit_management", z
    return "strike_selection", z


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _stances(p: str, cfg: RankingFile, settings: ArcSettings) -> dict[str, frozenset[str]]:
    """Trend label → allowed structures: ``backtest.stance_menus`` override, else profile."""
    override = cfg.backtest.stance_menus.get(p)
    if override is None:
        return stance_kinds(p, settings.account_profiles_file)
    return {lab: frozenset(str(k) for k in kinds) for lab, kinds in override.items()}


def _sessions(closes: Mapping[str, pd.Series], start: dt.date) -> list[dt.date]:
    days: set[dt.date] = set()
    for s in closes.values():
        days |= {d for d in s.index if d >= start}
    return sorted(days)


def run_rank_report(
    *,
    store: ParquetHistoryStore,
    closes_by_ticker: Mapping[str, pd.Series],
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    profiles: Sequence[str],
    rankers: Sequence[Ranker],
    out_dir: Path,
    cfg: RankingFile,
    cost: CostModel,
    settings: ArcSettings,
    provider: str = "alpaca",
    exits: ExitConfig | None = None,
    workers: int = 1,
    charts: bool = True,
    ohlc_by_ticker: Mapping[str, pd.DataFrame] | None = None,
) -> dict[str, pd.DataFrame]:
    """Run the ranking backtest and write ``report.md`` + CSVs (+ PNG charts) to *out_dir*.

    *ohlc_by_ticker*: split-adjusted daily bars for the E16.3 entry filter
    (``backtest.entry_filter`` not ``none``); a missing ticker filters nothing.
    """
    exits = exits or load_exit_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    bt = cfg.backtest
    slippages = sorted({*bt.slippage_grid, cost.slippage_frac})
    per_profile = {p: settings.with_profile(p) for p in profiles}
    windows = {p: bt.window_for(p, s.entry_dte_window) for p, s in per_profile.items()}
    allows = {p: profile_allows_credit(p, settings.account_profiles_file) for p in profiles}
    rk = {p: rankers_for(rankers, allows_credit=allows[p]) for p in profiles}

    def job_args(t: str) -> tuple[object, ...]:
        raw = store.read(provider, t, start, end)
        return (t, raw, closes_by_ticker[t])

    def job_kw(t: str) -> dict[str, object]:
        if bt.entry_filter == "none" and bt.max_be_atr is None:
            return kw
        return {**kw, "ohlc": (ohlc_by_ticker or {}).get(t)}

    kw = {
        "start": start,
        "end": end,
        "cfg": cfg,
        "exits": exits,
        "cost": cost,
        "windows": windows,
        "rankers": rk,
        "slippages": slippages,
        "stances": {p: _stances(p, cfg, settings) for p in profiles}
        if bt.stance == "trend"
        else None,
    }
    results: dict[str, TickerResult] = {}
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {t: ex.submit(ticker_job, *job_args(t), **job_kw(t)) for t in tickers}  # type: ignore[arg-type]
            results = {t: f.result() for t, f in futs.items()}
    else:
        results = {t: ticker_job(*job_args(t), **job_kw(t)) for t in tickers}  # type: ignore[arg-type]

    sessions = _sessions(closes_by_ticker, start)
    trend = {t: r.trend for t, r in results.items()}
    vol = {t: r.vol for t, r in results.items()}
    runs: dict[tuple[str, float, str], RankRun] = {}
    for p in profiles:
        for x in slippages:
            menus = {t: r.menus[(p, x)] for t, r in results.items()}
            outcomes: dict[tuple[str, dt.date, str], Outcome | None] = {}
            for r in results.values():
                outcomes.update(r.outcomes[(p, x)])
            for ranker in rk[p]:
                runs[(p, x, ranker.value)] = run_portfolio(
                    menus,
                    outcomes,
                    ranker=ranker,
                    profile=p,
                    settings=per_profile[p],
                    starting_equity=bt.starting_equity,
                    filters=cfg.ranking.filters,
                    vrp_threshold=cfg.ranking.vrp_threshold,
                    sessions=sessions,
                    slippage=x,
                    trend=trend,
                    vol=vol,
                )

    frames = _tables(
        runs, profiles=profiles, rk=rk, allows=allows, cfg=cfg, base_x=cost.slippage_frac
    )
    menu_sizes = {
        p: float(
            np.mean(
                [
                    len(v)
                    for r in results.values()
                    for v in r.menus[(p, cost.slippage_frac)].values()
                ]
                or [0]
            )
        )
        for p in profiles
    }
    frames["equity"] = _equity_frame(runs, profiles=profiles, rk=rk, x=cost.slippage_frac)
    if bt.entry_filter != "none":  # E16.3: decision days the filter emptied of long premium
        frames["entry_filter_days"] = pd.DataFrame(
            [
                {
                    "ticker": t,
                    "profile": p,
                    "days_filtered": r.filtered.get((p, cost.slippage_frac), 0),
                }
                for t, r in sorted(results.items())
                for p in profiles
            ]
        )
    if bt.max_be_atr is not None:  # E16.5: debit candidates the realism filter dropped
        frames["be_filter_drops"] = pd.DataFrame(
            [
                {
                    "ticker": t,
                    "profile": p,
                    "max_be_atr": bt.max_be_atr,
                    "candidates_dropped": r.be_dropped.get((p, cost.slippage_frac), 0),
                }
                for t, r in sorted(results.items())
                for p in profiles
            ]
        )
    for name, df in frames.items():
        if isinstance(df, pd.DataFrame):
            df.to_csv(out_dir / f"{name}.csv", index=False)
    pngs: list[str] = []
    if charts:
        pngs = _charts(runs, profiles=profiles, rk=rk, x=cost.slippage_frac, out_dir=out_dir)
    hits = {
        t: stance_hit_rate(
            closes_by_ticker[t],
            r.trend,
            horizon=HIT_RATE_SESSIONS,
            start=start,
            end=end,
        )
        for t, r in results.items()
    }
    (out_dir / "report.md").write_text(
        _render(
            frames,
            tickers=tickers,
            start=start,
            end=end,
            profiles=profiles,
            cfg=cfg,
            cost=cost,
            windows=windows,
            menu_sizes=menu_sizes,
            pngs=pngs,
            coverage={
                t: len(r.menus[(profiles[0], cost.slippage_frac)]) for t, r in results.items()
            },
            hits=hits,
        )
    )
    return frames


def _tables(
    runs: Mapping[tuple[str, float, str], RankRun],
    *,
    profiles: Sequence[str],
    rk: Mapping[str, Sequence[Ranker]],
    allows: Mapping[str, bool],
    cfg: RankingFile,
    base_x: float,
) -> dict[str, pd.DataFrame]:
    bt = cfg.backtest
    summary, sens, sub_rows, dec_rows, verdicts, structs, worst, trades = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for p in profiles:
        base_runs = {r.value: runs[(p, base_x, r.value)] for r in rk[p]}
        for (pp, _x, _), run in sorted(runs.items()):
            if pp != p:
                continue
            row = summarize(run, bt.starting_equity)
            sens.append(
                {
                    k: row[k]
                    for k in (
                        "profile",
                        "ranker",
                        "slippage",
                        "trades",
                        "net_pnl",
                        "max_dd",
                        "sharpe",
                    )
                }
            )
        for name, run in base_runs.items():
            summary.append(summarize(run, bt.starting_equity))
            for lab, (pnl, dd, n) in subperiod_stats(run, bt.decision.subperiods).items():
                sub_rows.append(
                    {
                        "profile": p,
                        "ranker": name,
                        "subperiod": lab,
                        "trades": n,
                        "net_pnl": pnl,
                        "max_dd": dd,
                    }
                )
            t = run.trades
            if len(t):
                trades.append(t)
                for kind, g in t.groupby("kind", sort=True):
                    structs.append(
                        {
                            "profile": p,
                            "ranker": name,
                            "kind": kind,
                            "trades": len(g),
                            "net_pnl": float(g["pnl"].sum()),
                            "win_rate": float((g["pnl"] > 0).mean()),
                            "mean_managed_pop": float(g["managed_pop"].mean()),
                            "avg_days_held": float(g["days_held"].mean()),
                        }
                    )
                w = t.nsmallest(10, "pnl").copy()
                rc = [root_cause(r) for r in w.to_dict("records")]
                w["root_cause"] = [c for c, _ in rc]
                w["move_sigma"] = [z for _, z in rc]
                worst.append(w)
        table, verdict = decide(
            base_runs, allows_credit=allows[p], rule=bt.decision, boot=bt.bootstrap
        )
        if len(table):
            table.insert(0, "profile", p)
            dec_rows.append(table)
        verdicts.append({"profile": p, "verdict": verdict})
    cols_worst = [
        "profile", "ranker", "underlying", "kind", "entry_date", "exit_date", "contracts", "pnl",
        "exit_reason", "managed_net_ev_unit", "managed_pop", "vrp", "trend", "move_sigma",
        "root_cause", "legs",
    ]  # fmt: skip
    return {
        "summary": pd.DataFrame(summary),
        "sensitivity": pd.DataFrame(sens),
        "subperiods": pd.DataFrame(sub_rows),
        "decision": pd.concat(dec_rows, ignore_index=True) if dec_rows else pd.DataFrame(),
        "verdicts": pd.DataFrame(verdicts),
        "by_structure": pd.DataFrame(structs),
        "worst_trades": pd.concat(worst, ignore_index=True)[cols_worst]
        if worst
        else pd.DataFrame(),
        "trades": pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(),
    }


def _equity_frame(
    runs: Mapping[tuple[str, float, str], RankRun],
    *,
    profiles: Sequence[str],
    rk: Mapping[str, Sequence[Ranker]],
    x: float,
) -> pd.DataFrame:
    """Daily equity per (profile, ranker) at the base cost (feeds ``rank-compare``)."""
    parts: list[pd.DataFrame] = []
    for p in profiles:
        for r in rk[p]:
            eq = runs[(p, x, r.value)].equity
            parts.append(
                pd.DataFrame(
                    {
                        "profile": p,
                        "ranker": r.value,
                        "date": list(eq.index),
                        "equity": eq.to_numpy(dtype=float),
                    }
                )
            )
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _charts(
    runs: Mapping[tuple[str, float, str], RankRun],
    *,
    profiles: Sequence[str],
    rk: Mapping[str, Sequence[Ranker]],
    x: float,
    out_dir: Path,
) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out: list[str] = []
    for p in profiles:
        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
        for r in rk[p]:
            run = runs[(p, x, r.value)]
            if len(run.equity):
                ax.plot(
                    pd.to_datetime(run.equity.index), run.equity.to_numpy(), label=r.value, lw=1.2
                )
        ax.set_title(f"Equity by ranker: {p} (slippage x={x})")
        ax.set_ylabel("equity ($)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        fig.autofmt_xdate()
        fig.tight_layout()
        name = f"equity_{p}.png"
        fig.savefig(out_dir / name, metadata={"Software": None, "CreationDate": None})
        plt.close(fig)
        out.append(name)
    return out


def _knobs(cfg: RankingFile) -> list[str]:
    """E7.5a experiment settings that differ from the E7.5 primary run (report header)."""
    bt, f = cfg.backtest, cfg.ranking.filters
    out: list[str] = []
    if bt.stance_menus:
        out.append(
            "Regime-conditional menu (backtest.stance_menus): "
            + "; ".join(
                f"{p}: "
                + ", ".join(f"{lab} → {'/'.join(map(str, k)) or 'none'}" for lab, k in m.items())
                for p, m in bt.stance_menus.items()
            )
            + "\n"
        )
    if f.min_net_ev_to_cost is not None:
        out.append(f"Net EV ÷ est. cost filter: ≥ {f.min_net_ev_to_cost:g}\n")
    if bt.entry_filter != "none":
        a = bt.anti_chase
        out.append(
            f"Entry filter (E16.3): {bt.entry_filter}, combine {a.combine}, stretch ≥ "
            f"{a.max_stretch_atr:g} ATR, RSI ≥ {a.rsi_overbought:g} / ≤ {a.rsi_oversold:g}"
            + (
                f", VWAP stretch ≥ {a.max_vwap_stretch_atr:g} ATR"
                if bt.entry_filter == "anti_chase_vwap"
                else ""
            )
            + "\n"
        )
    if bt.max_be_atr is not None:
        out.append(
            f"Breakeven realism (E16.5): drop debit candidates whose directional breakeven "
            f"is > {bt.max_be_atr:g} ATR14·√DTE from the decision close\n"
        )
    if bt.direction_tilt > 0:
        out.append(
            f"Direction tilt (E7.5b, D79): {bt.direction_tilt:g} of a 1σ hold move, trend "
            "stance, read only by the `*_tilted` rankers\n"
        )
    if bt.slippage_by_kind:
        out.append(
            "Measured slippage x by structure (base run): "
            + ", ".join(f"{k} {v:g}" for k, v in bt.slippage_by_kind.items())
            + "\n"
        )
    return out


def _render(
    f: Mapping[str, pd.DataFrame],
    *,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    profiles: Sequence[str],
    cfg: RankingFile,
    cost: CostModel,
    windows: Mapping[str, tuple[int, int]],
    menu_sizes: Mapping[str, float],
    pngs: Sequence[str],
    coverage: Mapping[str, int],
    hits: Mapping[str, tuple[float | None, int]] | None = None,
) -> str:
    bt = cfg.backtest
    rule = bt.decision.text(bt.bootstrap.ci)
    all_exp = any(m.expiry_mode == "all" for specs in bt.menus.values() for m in specs)
    hit_lines: list[str] = []
    if hits:
        n_all = sum(n for _, n in hits.values())
        h_all = sum((h or 0.0) * n for h, n in hits.values())
        overall = f"{h_all / n_all:.1%}" if n_all else "n/a"
        per = ", ".join(
            f"{t} {'n/a' if h is None else f'{h:.0%}'} (n={n})" for t, (h, n) in hits.items()
        )
        hit_lines = [
            f"- **Trend-proxy hit rate** (a bull/bear label followed by a close "
            f"{HIT_RATE_SESSIONS} sessions later in the labelled direction; diagnostic, "
            f"reads the future): {overall} overall · {per}. "
            + (
                f"The `*_tilted` rankers (tilt {bt.direction_tilt:g} of a 1σ hold move) "
                "can only help when this is above 50%.\n"
                if bt.direction_tilt > 0
                else "\n"
            )
        ]
    parts = [
        "# Ranking backtest run (E7.5)\n",
        f"Tickers: {', '.join(tickers)} · entries {start} → {end} · profiles: "
        f"{', '.join(profiles)} · equity ${bt.starting_equity:,.0f} · MC paths {bt.n_paths} · "
        f"cost x={cost.slippage_frac}, est. spread max({cost.spread_min}, "
        f"{cost.spread_pct}·mid)\n",
        "Decision sessions with a menu per ticker: "
        + ", ".join(f"{t} {n}" for t, n in coverage.items())
        + "\n",
        "Mean menu size: " + ", ".join(f"{p} {menu_sizes[p]:.1f}" for p in profiles) + "\n",
        "Entry DTE windows: "
        + ", ".join(f"{p} {lo}–{hi}" for p, (lo, hi) in windows.items())
        + "\n",
        *_knobs(cfg),
        f"## Decision rule (fixed before the run)\n\n{rule}\n",
        "## Data caveats\n",
        "- Alpaca options history has **no historical quotes**: `mid` is the session's last "
        "trade close and the bid/ask spread is *estimated* as "
        f"max({cost.spread_min}, {cost.spread_pct}·mid) (E7.1 finding). Costs, slippage and "
        "the cost-sensitivity grid are therefore modelled, not observed.\n"
        "- A trade close can be hours stale, so neighbouring strikes on one Alpaca EOD row set "
        "routinely break monotonicity and put-call parity (on sampled SPY/QQQ/IWM sessions, "
        "13–65% of near-the-money strikes sat in a non-monotone pair). "
        + (
            "Every leg is therefore **re-marked from a same-session fitted IV smile** "
            "(volume-weighted quadratic in log-moneyness over OTM IVs, 3-MAD outlier trim, "
            "no extrapolation) for entries, daily marks and early exits alike. Expiry "
            "settles on the underlying close. This removes the stale-close noise but also "
            "any real skew kinks the quadratic cannot follow.\n"
            if bt.marks == "smile"
            else "Legs whose IV is more than "
            f"{'off' if bt.max_leg_iv_dev is None else f'{bt.max_leg_iv_dev:.0%}'} from the "
            f"median of their {bt.smile_window} neighbouring strikes are dropped "
            "(same-session data only); remaining marks are raw trade closes.\n"
        ),
        (
            "- **Stance** (which structures are on the menu) is a deterministic Research "
            "proxy: the 20-session trend label at the decision close (bull → bullish, "
            "bear → bearish, sideways → neutral) picks the profile's `stance_strategies`. "
            "`cash_debit` has no neutral structure, so it does not trade in sideways "
            "sessions. The live Research uses more information; its stance quality is "
            "outside this test. Every ranker sees the same stance-filtered menu.\n"
            if bt.stance == "trend"
            else "- **No stance filter**: every profile structure is on every menu, so the "
            "ranker also chooses direction (the live Research does that).\n"
        ),
        "- Daily EOD decisions and marks only: stops and take-profits are checked on closes, "
        "so intraday paths are not seen (matches the owner's relaxed, end-of-day stop "
        "preference).\n"
        + (
            "- **Live-shaped menus** (E7.5b): every expiration in the profile's DTE window "
            "and the scanner's delta bands; contracts with no trade that session are "
            "missing. ThetaData EOD was not used (no coverage in this store).\n"
            if all_exp
            else "- Menus hold one expiration (nearest the middle of the profile's DTE "
            "window) and a fixed delta grid, not the full live scanner menu; contracts with "
            "no trade that session are missing. ThetaData EOD was not used (no coverage in "
            "this store).\n"
        )
        + "".join(hit_lines)
        + "- At most one new position per ticker per session (open positions stack up to the "
        "gate's per-underlying and max-open caps), sized by D18 with no Risk persona "
        "(the equity cap binds).\n",
        "## Verdict\n",
        format_table(f["verdicts"]),
        "## Challengers vs incumbent\n",
        format_table(f["decision"]) if len(f["decision"]) else "_none_\n",
        "## Summary by ranker (default costs)\n",
        format_table(
            f["summary"],
            [
                "profile",
                "ranker",
                "trades",
                "net_pnl",
                "cagr",
                "max_dd",
                "max_dd_pct",
                "sharpe",
                "sortino",
                "win_rate",
                "mean_managed_pop",
                "realised_net_ev_unit",
                "modelled_net_ev_unit",
                "avg_days_held",
                "turnover_per_month",
                "cost_share_of_gross",
                "hold_to_expiry_pnl",
            ],  # fmt: skip
        )
        if len(f["summary"])
        else "_no runs_\n",
        "## Sub-periods (trend regime at entry)\n",
        format_table(f["subperiods"]) if len(f["subperiods"]) else "_none_\n",
        "## Cost sensitivity (slippage x of the spread)\n",
        format_table(f["sensitivity"]) if len(f["sensitivity"]) else "_none_\n",
        "## By structure\n",
        format_table(f["by_structure"]) if len(f["by_structure"]) else "_none_\n",
        "## Worst 10 trades per ranker\n",
        format_table(f["worst_trades"]) if len(f["worst_trades"]) else "_none_\n",
    ]
    parts += [f"![{p}]({p})\n" for p in pngs]
    return "\n".join(parts)
