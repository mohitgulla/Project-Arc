"""E7.5a: compare an experiment's ranking backtest with the baseline run.

An experiment is a partial ranking file (``config/experiments/*.yaml``) deep-merged
over ``config/ranking.yaml`` by ``arc backtest rank --experiment``: pure config, no
code path of its own. ``arc backtest rank-compare`` then reads the two output
directories (``trades.csv``, ``equity.csv``) and applies the same pre-registered
D25 rule as the ranker decision (:func:`arc.backtest.ranking.challenge`): the
experiment's run of a ranker vs the baseline's run of the *same* ranker, per
account profile. A "switch" needs >= ``min_subperiod_wins`` trend sub-periods won
on net P&L *and* max DD, and a bootstrap CI of the P&L difference above 0.

Deterministic: same CSVs and ``config/ranking.yaml`` → same table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from arc.backtest.ranking import RankRun, challenge, summarize

if TYPE_CHECKING:
    from pathlib import Path

    from arc.backtest.ranking import RankingFile

__all__ = ["compare_dirs", "load_runs"]


def load_runs(out_dir: Path) -> dict[tuple[str, str], RankRun]:
    """``(profile, ranker)`` → the base-cost run written by ``arc backtest rank``."""
    eq = pd.read_csv(out_dir / "equity.csv", parse_dates=["date"])
    tr_path = out_dir / "trades.csv"
    trades = (
        pd.read_csv(tr_path, parse_dates=["entry_date", "exit_date"])
        if (tr_path.exists() and tr_path.stat().st_size > 1)
        else pd.DataFrame(columns=["profile", "ranker", "pnl", "trend", "entry_date", "exit_date"])
    )
    for c in ("entry_date", "exit_date"):
        if c in trades:
            trades[c] = pd.to_datetime(trades[c]).dt.date
    out: dict[tuple[str, str], RankRun] = {}
    for (p, r), g in eq.groupby(["profile", "ranker"], sort=True):
        series = pd.Series(g["equity"].to_numpy(dtype=float), index=g["date"].dt.date.to_list())
        t = trades[(trades["profile"] == p) & (trades["ranker"] == r)].reset_index(drop=True)
        out[(str(p), str(r))] = RankRun(
            ranker=str(r), profile=str(p), slippage=0.0, trades=t, equity=series, skipped={}
        )
    return out


def compare_dirs(
    baseline: Path, experiment: Path, cfg: RankingFile, *, name: str = "experiment"
) -> pd.DataFrame:
    """One row per (profile, ranker) present in both runs: metrics side by side + the rule."""
    bt = cfg.backtest
    base, exp = load_runs(baseline), load_runs(experiment)
    rows: list[dict[str, object]] = []
    for key in sorted(base.keys() & exp.keys()):
        b, e = base[key], exp[key]
        sb, se = summarize(b, bt.starting_equity), summarize(e, bt.starting_equity)
        rows.append(
            {
                "experiment": name,
                "profile": key[0],
                "ranker": key[1],
                "trades_base": sb["trades"],
                "trades_exp": se["trades"],
                "net_pnl_base": sb["net_pnl"],
                "net_pnl_exp": se["net_pnl"],
                "max_dd_base": sb["max_dd"],
                "max_dd_exp": se["max_dd"],
                "sharpe_base": sb["sharpe"],
                "sharpe_exp": se["sharpe"],
                **challenge(b, e, rule=bt.decision, boot=bt.bootstrap),
            }
        )
    return pd.DataFrame(rows)
