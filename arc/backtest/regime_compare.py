"""E17.3 (D77): regime v2 vs v1 report helpers on the E7.5 ranking-backtest output.

Pure functions over the CSVs ``arc backtest rank`` writes plus a close series; no
network, no store. Three pieces, each with its rule fixed in the card before the run:

* :func:`regime_verdict`: keep v2 / roll back to v1 / keep v2 inconclusive, from the
  incumbent ranker's v1-labels run vs v2-labels run in each profile.
* :func:`label_flips`: per ticker, how often the v2 trend / vol label differs from v1.
* :func:`guard_blocked_days` + :func:`guard_split` + :func:`guard_verdict`: the E17.2
  SPY transitional guard (``arc.pipeline.market_guard.transitional_reason``) replayed per
  decision day at a given (min run, min margin z), and the incumbent's trades split into
  opened-on-a-would-be-blocked-day vs the rest, with a block-bootstrap CI of the mean
  difference.

Deterministic: same inputs and seed → same numbers.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from arc.backtest.ranking import BootstrapSpec, RankRun, summarize
from arc.config import ArcSettings
from arc.features.regime import classify_z, margin_to_threshold, vol_scaled_z
from arc.pipeline.market_guard import SpyRegime, transitional_reason

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping, Sequence

    from arc.backtest.regime import BacktestRegimeModel

__all__ = [
    "Verdict",
    "guard_blocked_days",
    "guard_split",
    "guard_verdict",
    "label_flips",
    "regime_verdict",
]

Verdict = Literal["keep v2", "roll back to v1", "keep v2, inconclusive"]


# ---------------------------------------------------------------------------
# v1 vs v2 verdict
# ---------------------------------------------------------------------------


def _daily_diff(base: RankRun, run: RankRun) -> np.ndarray:
    idx = base.equity.index.union(run.equity.index)
    a = run.equity.reindex(idx).ffill().bfill()
    b = base.equity.reindex(idx).ffill().bfill()
    return (a.diff().fillna(0.0) - b.diff().fillna(0.0)).to_numpy()


def _sum_ci(x: np.ndarray, boot: BootstrapSpec) -> tuple[float, float, float]:
    from arc.backtest.ranking import block_bootstrap_ci

    return block_bootstrap_ci(
        x, resamples=boot.resamples, block=boot.block_days, ci=boot.ci, seed=boot.seed
    )


def regime_verdict(
    v1: Mapping[str, RankRun],
    v2: Mapping[str, RankRun],
    *,
    starting_equity: float,
    boot: BootstrapSpec,
) -> tuple[pd.DataFrame, Verdict]:
    """The card's fixed rule over ``profile → incumbent run`` for v1 and v2 labels.

    Keep v2 if, in every profile, v2 is not worse than v1 on net P&L **and** on max
    drawdown, **and** the CI upper bound of the daily P&L difference (v2 − v1) is > 0.
    Roll back if v2 is worse on both net P&L and drawdown in any profile. Otherwise
    keep v2, inconclusive.
    """
    rows: list[dict[str, object]] = []
    for profile in sorted(v1.keys() & v2.keys()):
        a, b = v1[profile], v2[profile]
        sa, sb = summarize(a, starting_equity), summarize(b, starting_equity)
        point, lo, hi = _sum_ci(_daily_diff(a, b), boot)
        pnl_ok = float(sb["net_pnl"]) >= float(sa["net_pnl"])  # type: ignore[arg-type]
        dd_ok = float(sb["max_dd"]) <= float(sa["max_dd"])  # type: ignore[arg-type]
        rows.append(
            {
                "profile": profile,
                "ranker": a.ranker,
                "trades_v1": sa["trades"],
                "trades_v2": sb["trades"],
                "net_pnl_v1": sa["net_pnl"],
                "net_pnl_v2": sb["net_pnl"],
                "max_dd_v1": sa["max_dd"],
                "max_dd_v2": sb["max_dd"],
                "sharpe_v1": sa["sharpe"],
                "sharpe_v2": sb["sharpe"],
                "pnl_diff": point,
                "ci_lo": lo,
                "ci_hi": hi,
                "pnl_not_worse": pnl_ok,
                "dd_not_worse": dd_ok,
            }
        )
    t = pd.DataFrame(rows)
    if t.empty:
        return t, "keep v2, inconclusive"
    worse_both = (~t["pnl_not_worse"]) & (~t["dd_not_worse"])
    if bool(worse_both.any()):
        return t, "roll back to v1"
    keep = t["pnl_not_worse"] & t["dd_not_worse"] & (t["ci_hi"] > 0)
    return t, "keep v2" if bool(keep.all()) else "keep v2, inconclusive"


# ---------------------------------------------------------------------------
# Label flips
# ---------------------------------------------------------------------------


def label_flips(
    labels: Mapping[str, Mapping[BacktestRegimeModel, tuple[pd.Series, pd.Series]]],
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """Per ticker: decision days in [start, end] where both labels are known, and flips.

    *labels*: ticker → model → (trend, vol) series (from ``labels_for``).
    """
    rows: list[dict[str, object]] = []
    for t in sorted(labels):
        t1, v1 = labels[t]["v1"]
        t2, v2 = labels[t]["v2"]
        days = [d for d in t1.index if start <= d <= end and d in t2.index]
        a1, a2 = t1.loc[days], t2.loc[days]
        b1, b2 = v1.loc[days], v2.loc[days]
        kt = (a1 != "unknown") & (a2 != "unknown")
        kv = (b1 != "unknown") & (b2 != "unknown")
        nt, nv = int(kt.sum()), int(kv.sum())
        ft = int((a1[kt] != a2[kt]).sum())
        fv = int((b1[kv] != b2[kv]).sum())
        rows.append(
            {
                "ticker": t,
                "days": nt,
                "trend_flips": ft,
                "trend_flip_pct": ft / nt if nt else math.nan,
                "vol_days": nv,
                "vol_flips": fv,
                "vol_flip_pct": fv / nv if nv else math.nan,
                **{f"v1_{k}": int((a1[kt] == k).sum()) for k in ("bear", "sideways", "bull")},
                **{f"v2_{k}": int((a2[kt] == k).sum()) for k in ("bear", "sideways", "bull")},
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Transitional guard
# ---------------------------------------------------------------------------


def guard_blocked_days(spy_closes: pd.Series, settings: ArcSettings) -> dict[dt.date, bool]:
    """date → would the E17.2 confirmation guard block opens (SPY v2 labels, trailing).

    Same construction as the 2-year replay in ``tests/test_market_guard_confirmation``:
    v2 label from the vol-scaled z, run length of consecutive equal labels, margin z
    to the nearest threshold, then :func:`transitional_reason` with *settings*.
    """
    c = spy_closes.sort_index()
    z = vol_scaled_z(c, vol_window=settings.regime_vol_scale_window)
    labels = [classify_z(float(v), trend_z=settings.regime_trend_z) for v in z]
    out: dict[dt.date, bool] = {}
    run = 0
    for i, (d, zz) in enumerate(z.items()):
        run = run + 1 if i and labels[i - 1] == labels[i] else 1
        reg = SpyRegime(
            current=str(labels[i]),
            run_length=run,
            margin_z=margin_to_threshold(float(zz), settings.regime_trend_z),
        )
        _, why = transitional_reason(reg, settings)
        out[d] = why is not None
    return out


def _pf(p: pd.Series) -> float:
    loss = float(-p[p < 0].sum())
    return float(p[p > 0].sum()) / loss if loss > 0 else math.inf


def _mean_diff_ci(
    trades: pd.DataFrame, blocked: Mapping[dt.date, bool], boot: BootstrapSpec
) -> tuple[float, float, float]:
    """(point, lo, hi) of mean P&L (blocked − rest): moving-block bootstrap over entry days.

    Days (not trades) are resampled in blocks of ``boot.block_days`` consecutive entry
    sessions, carrying every trade opened that day, so same-day and overlapping trades
    keep their correlation.
    """
    by_day = trades.groupby("entry_date")["pnl"]
    days = sorted(by_day.groups)
    s = by_day.sum().reindex(days).to_numpy(dtype=float)
    n = by_day.count().reindex(days).to_numpy(dtype=float)
    blk = np.array([bool(blocked.get(d, False)) for d in days])

    def stat(sel: np.ndarray) -> float:
        sb, nb = float((s * blk)[sel].sum()), float((n * blk)[sel].sum())
        sr, nr = float((s * ~blk)[sel].sum()), float((n * ~blk)[sel].sum())
        if nb == 0 or nr == 0:
            return math.nan
        return sb / nb - sr / nr

    k = len(days)
    if k == 0:
        return math.nan, math.nan, math.nan
    point = stat(np.arange(k))
    b = min(boot.block_days, k)
    m = math.ceil(k / b)
    rng = np.random.default_rng(boot.seed)
    starts = rng.integers(0, k - b + 1, size=(boot.resamples, m))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(boot.resamples, -1)[:, :k]
    vals = np.array([stat(row) for row in idx])
    vals = vals[~np.isnan(vals)]
    if len(vals) == 0:
        return point, math.nan, math.nan
    a = (1 - boot.ci) / 2
    return point, float(np.quantile(vals, a)), float(np.quantile(vals, 1 - a))


def guard_split(
    runs: Mapping[str, RankRun], blocked: Mapping[dt.date, bool], boot: BootstrapSpec
) -> pd.DataFrame:
    """``profile → incumbent run``: blocked-day vs rest stats per profile."""
    rows: list[dict[str, object]] = []
    for profile in sorted(runs):
        t = runs[profile].trades
        if t.empty:
            t = pd.DataFrame({"entry_date": [], "pnl": []})
        mask = t["entry_date"].map(lambda d: bool(blocked.get(d, False))).astype(bool)
        point, lo, hi = _mean_diff_ci(t, blocked, boot)
        for name, g in (("blocked", t[mask]), ("rest", t[~mask])):
            p = g["pnl"].astype(float)
            rows.append(
                {
                    "profile": profile,
                    "ranker": runs[profile].ranker,
                    "group": name,
                    "trades": len(g),
                    "entry_days": int(g["entry_date"].nunique()),
                    "win_rate": float((p > 0).mean()) if len(g) else math.nan,
                    "mean_pnl": float(p.mean()) if len(g) else math.nan,
                    "net_pnl": float(p.sum()),
                    "profit_factor": _pf(p) if len(g) else math.nan,
                    "diff_mean": point,
                    "diff_ci_lo": lo,
                    "diff_ci_hi": hi,
                    # P&L forgone if those days were skipped = blocked trades' net P&L
                    "forgone_if_skipped": float(t[mask]["pnl"].sum()),
                }
            )
    return pd.DataFrame(rows)


def guard_verdict(split: pd.DataFrame, profiles: Sequence[str]) -> Literal["turn on", "keep off"]:
    """Turn on only if, in every profile, blocked-day mean < rest mean and CI hi < 0."""
    for p in profiles:
        g = split[split["profile"] == p]
        if g.empty:
            return "keep off"
        b = g[g["group"] == "blocked"].iloc[0]
        r = g[g["group"] == "rest"].iloc[0]
        if not (float(b["mean_pnl"]) < float(r["mean_pnl"]) and float(b["diff_ci_hi"]) < 0):
            return "keep off"
    return "turn on"


def reference_settings(min_run: int = 3, min_margin_z: float = 0.10) -> ArcSettings:
    """The E17.2 named reference setting (run 3 / margin z 0.10); never the defaults."""
    return ArcSettings(regime_guard_min_run=min_run, regime_guard_min_margin_z=min_margin_z)
