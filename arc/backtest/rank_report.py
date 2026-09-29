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
from arc.backtest.ranking import (
    Candidate,
    Outcome,
    RankingFile,
    RankRun,
    build_menus,
    clean_chains,
    decide,
    labels_for,
    picked_outcomes,
    profile_allows_credit,
    rankers_for,
    remark_chains,
    run_portfolio,
    subperiod_stats,
    summarize,
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


class TickerResult:
    """Menus and picked outcomes for one ticker, per (profile, slippage)."""

    def __init__(
        self,
        ticker: str,
        menus: dict[_Key, dict[dt.date, list[Candidate]]],
        outcomes: dict[_Key, dict[tuple[str, dt.date, str], Outcome | None]],
        trend: pd.Series,
        vol: pd.Series,
    ) -> None:
        self.ticker = ticker
        self.menus = menus
        self.outcomes = outcomes
        self.trend = trend
        self.vol = vol


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
) -> TickerResult:
    """Everything that depends on one ticker only (safe to run in a worker process)."""
    closes = closes.sort_index()
    bt = cfg.backtest
    mc = exits.model.model_copy(update={"n_paths": bt.n_paths})
    chains = prepare_chains(raw, closes, cost=cost, r=bt.risk_free_rate, dte_min=1, dte_max=70)
    if bt.marks == "smile":
        chains = remark_chains(chains, closes, r=bt.risk_free_rate, cost=cost)
    else:
        chains = clean_chains(chains, max_dev=bt.max_leg_iv_dev, window=bt.smile_window)
    days = [d for d in sorted(chains) if start <= d <= end and d in closes.index]
    menus: dict[_Key, dict[dt.date, list[Candidate]]] = {}
    outcomes: dict[_Key, dict[tuple[str, dt.date, str], Outcome | None]] = {}
    for profile, (lo, hi) in sorted(windows.items()):
        specs = bt.specs_for(profile, lo, hi)
        for x in slippages:
            c = cost.model_copy(update={"slippage_frac": x})
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
            )
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
            )
    trend, vol = labels_for(closes)
    log.info("backtest.rank_ticker", ticker=ticker, sessions=len(days))
    return TickerResult(ticker, menus, outcomes, trend, vol)


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
) -> dict[str, pd.DataFrame]:
    """Run the ranking backtest and write ``report.md`` + CSVs (+ PNG charts) to *out_dir*."""
    exits = exits or load_exit_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    bt = cfg.backtest
    slippages = sorted({*bt.slippage_grid, cost.slippage_frac})
    per_profile = {p: settings.with_profile(p) for p in profiles}
    windows = {p: s.entry_dte_window for p, s in per_profile.items()}
    allows = {p: profile_allows_credit(p, settings.account_profiles_file) for p in profiles}
    rk = {p: rankers_for(rankers, allows_credit=allows[p]) for p in profiles}

    def job_args(t: str) -> tuple[object, ...]:
        raw = store.read(provider, t, start, end)
        return (t, raw, closes_by_ticker[t])

    kw = {
        "start": start,
        "end": end,
        "cfg": cfg,
        "exits": exits,
        "cost": cost,
        "windows": windows,
        "rankers": rk,
        "slippages": slippages,
    }
    results: dict[str, TickerResult] = {}
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {t: ex.submit(ticker_job, *job_args(t), **kw) for t in tickers}  # type: ignore[arg-type]
            results = {t: f.result() for t, f in futs.items()}
    else:
        results = {t: ticker_job(*job_args(t), **kw) for t in tickers}  # type: ignore[arg-type]

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
    for name, df in frames.items():
        if isinstance(df, pd.DataFrame):
            df.to_csv(out_dir / f"{name}.csv", index=False)
    pngs: list[str] = []
    if charts:
        pngs = _charts(runs, profiles=profiles, rk=rk, x=cost.slippage_frac, out_dir=out_dir)
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
) -> str:
    bt = cfg.backtest
    rule = bt.decision.text(bt.bootstrap.ci)
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
        "- Daily EOD decisions and marks only: stops and take-profits are checked on closes, "
        "so intraday paths are not seen (matches the owner's relaxed, end-of-day stop "
        "preference).\n"
        "- Menus hold one expiration (nearest the middle of the profile's DTE window) and "
        "a fixed delta grid, not the full live scanner menu; contracts with no trade that "
        "session are missing. ThetaData EOD was not used (no coverage in this store).\n"
        "- At most one new position per ticker per session (open positions stack up to the "
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
