"""E7.18: analyses and tables of the loss & drawdown anatomy (report only, D84).

Reads the books ``data/backtest/runs/anatomy/<book>/<profile>/<ranker>/`` (trades,
equity, ``paths.parquet``) and ``features.parquet`` (scripts/loss_anatomy_features.py),
writes aggregate CSVs to ``docs/RESEARCH/loss-anatomy/`` and the generated
``tables.md`` beside them. No per-contract quote rows are written (D84 licence).
"""

from __future__ import annotations

import bisect
import datetime as dt
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_attribution_report import (  # noqa: E402
    EQUITY0,
    PROFILES,
    book,
    md_table,
    reconcile,
    run_dir,
)

RUNS_ROOT = Path("data")  # replaced at import below (avoid a cycle with loss_anatomy)
PRIMARY, CHECK = "c500_dev", "c125_dev"
STOP_R = -0.75  # stop trigger in R (credit: 75 % of max loss; debit: 75 % of debit)
DD_MIN = 0.10
SEED = 18
N_BOOT = 2000

#: Feature → how selection treats it today (6c). ``selection`` = used by the backtest
#: walk and live deterministic selection (ranker, filter, screen, gate, stance proxy);
#: ``live_context`` = shown to the live LLM personas / live market guard, not in the
#: backtest walk; ``ignored`` = available in our data, used nowhere; ``not_live`` =
#: not computed in the live pipeline today.
FEATURE_USE: dict[str, str] = {
    "managed_net_ev_unit": "selection",  # ranker + min_managed_net_ev filter
    "managed_pop": "selection",  # carried; no floor (min_managed_pop 0)
    "rorc_day": "selection",  # rorc_day rankers
    "vrp": "selection",  # rorc_day_vrp ranker
    "width": "selection",  # credit_width / debit_width rankers
    "delta": "selection",  # menu deltas
    "dte_entry": "selection",  # menu DTE window
    "spread_pct_premium": "selection",  # spread rule (per leg) + fill model
    "opt_atm_spread_pct": "selection",  # D9 screen max_atm_spread_pct
    "opt_oi_near_atm": "selection",  # D9 screen ATM OI
    "opt_d9_pass": "selection",  # D9 screen
    "px_ret_20d": "selection",  # v2 trend label (vol-scaled 20d trend) picks the stance
    "days_to_earn_proj": "selection",  # earnings blackout
    "open_positions": "selection",  # check_max_open_positions
    "same_name_open": "selection",  # check_per_underlying
    "risk_pct_equity": "selection",  # sizing (max_alloc_pct)
    "opt_iv_rank_252": "live_context",  # personas see IV rank; walk ignores it
    "opt_iv30": "live_context",
    "mkt_vix_proxy": "live_context",  # market_guard: VIX >= 35 blocks (Cboe VIX live)
    "mkt_vix_proxy_pctile": "ignored",
    "mkt_vix_proxy_chg_5d": "ignored",
    "mkt_spy_term_slope": "live_context",  # market_guard: backwardation (VIX futures)
    "mkt_spy_dist_ma50": "live_context",  # SPY regime read in market_guard / Research
    "mkt_spy_dist_ma200": "live_context",
    "mkt_spy_dist_ma20": "live_context",
    "mkt_spy_ret_20d": "live_context",
    "mkt_spy_dd_from_high": "ignored",
    "mkt_spy_rv_minus_iv": "ignored",
    "mkt_credit_hyg_tlt_20d": "ignored",
    "mkt_breadth_above_ma50": "not_live",
    "mkt_sector_dispersion_20d": "not_live",
    "opt_iv_pct_252": "ignored",
    "opt_term_slope": "ignored",
    "opt_front_over_iv30": "ignored",
    "opt_skew_25d": "ignored",
    "opt_rv20": "ignored",
    "opt_rv60": "ignored",
    "opt_vrp20": "ignored",
    "opt_two_sided_pct": "ignored",
    "opt_opt_volume": "ignored",
    "opt_put_call_oi": "ignored",
    "opt_put_call_vol": "ignored",
    "px_ret_5d": "live_context",  # technicals in the Research brief
    "px_ret_60d": "ignored",
    "px_dist_ma20": "live_context",
    "px_dist_ma50": "live_context",
    "px_dist_ma200": "ignored",
    "px_rv20_pctile": "selection",  # v2 vol state label
    "px_gap_pct": "ignored",
    "px_days_since_2sd": "ignored",
    "px_dd_from_high": "ignored",
    "days_since_earn": "ignored",
    "fomc_in_window": "live_context",  # Scout reads the macro calendar
    "cpi_in_window": "live_context",
    "nfp_in_window": "live_context",
    "fomc_within_2s": "live_context",
    "cpi_within_2s": "live_context",
    "open_risk_pct": "ignored",  # no open-risk cap in the gate
    "open_same_dir_share": "ignored",
    "same_group_open": "not_live",  # no sector map live
    "same_cluster_open": "ignored",  # betas exist live; no correlation cap
    "new_risk_today_pct": "ignored",
    "drawdown_at_entry": "ignored",
}
NUMERIC = [k for k in FEATURE_USE if not k.endswith(("_in_window", "_within_2s"))]
BOOLS = [k for k in FEATURE_USE if k.endswith(("_in_window", "_within_2s"))]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_book(run: Path, profile: str) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """(trades + features, equity, paths) of one profile of one book."""
    t, e = book(run, profile)
    f = pd.read_parquet(run / "features.parquet")
    f = f[f["profile"] == profile].reset_index(drop=True)
    if len(f) != len(t):
        msg = f"{run.name}/{profile}: {len(f)} feature rows vs {len(t)} trades"
        raise ValueError(msg)
    paths = pd.read_parquet(run_dir(run, profile) / "paths.parquet")
    paths["day"] = [d if isinstance(d, dt.date) else pd.Timestamp(d).date() for d in paths["day"]]
    for c in ("entry_date", "exit_day", "expiration", "exit_date"):
        if c in f:
            f[c] = [
                x if (x is None or isinstance(x, dt.date)) else pd.Timestamp(x).date() for x in f[c]
            ]
    f["trade"] = [
        f"{u}|{d}|{lg}"
        for u, d, lg in zip(f["underlying"], f["entry_date"], f["legs"], strict=True)
    ]
    return f, e, paths


# ---------------------------------------------------------------------------
# Equity from per-trade values (the walk's own accounting)
# ---------------------------------------------------------------------------


def trade_values(t: pd.DataFrame, paths: pd.DataFrame, days: list[dt.date]) -> pd.DataFrame:
    """days × trades matrix of each trade's cumulative $ P&L as the walk marks it.

    ``v(d) = pnl`` once ``exit_day <= d``; else the last daily mark ``<= d`` × contracts;
    0 before the first mark. ``EQUITY0 + v.sum(axis=1)`` is the walk's marked equity.
    """
    idx = pd.Index(days)
    by = {k: g.sort_values("day") for k, g in paths.groupby("trade")}
    cols: dict[str, np.ndarray] = {}
    for r in t.itertuples():
        v = np.zeros(len(days))
        g = by.get(r.trade)
        if g is not None and len(g):
            s = pd.Series(g["mark_unit"].to_numpy() * r.contracts, index=list(g["day"]))
            s = s.reindex(idx.union(s.index)).ffill().reindex(idx).fillna(0.0)
            v = s.to_numpy()
        done = np.array([d >= r.exit_day for d in days])
        v = np.where(done, r.pnl, v)
        cols[str(r.Index)] = v
    return pd.DataFrame(cols, index=idx)


def rebuild_equity(values: pd.DataFrame, keep: Iterable[bool] | None = None) -> pd.Series:
    v = values if keep is None else values.loc[:, np.asarray(list(keep), dtype=bool)]
    return EQUITY0 + v.sum(axis=1)


# ---------------------------------------------------------------------------
# Generic bucket table
# ---------------------------------------------------------------------------


def bucket_table(t: pd.DataFrame, col: str, check: pd.DataFrame | None = None) -> pd.DataFrame:
    """Net / per-$-risk / mean R / win per bucket, All/Stocks/ETFs, with a C125 sign check."""
    total = float(t["pnl"].sum())
    rows = []
    for cls, g0 in [("All", t), *((c, t[t["class"] == c]) for c in ("Stocks", "ETFs"))]:
        for k, g in g0.groupby(col, dropna=False, observed=True):
            row: dict[str, Any] = {
                "class": cls,
                col: k,
                "n": len(g),
                "net": round(float(g["pnl"].sum()), 0),
                "net_at_mid": round(float(g["pnl_mid"].sum()), 0),
                "per_risk": round(float(g["pnl"].sum() / g["risk"].sum()), 3),
                "mean_R": round(float(g["R"].mean()), 3),
                "win": round(float(g["win"].mean()), 2),
                "share_of_net": round(float(g["pnl"].sum()) / abs(total), 2) if total else None,
            }
            if check is not None:
                c0 = check if cls == "All" else check[check["class"] == cls]
                c = c0[c0[col] == k] if k == k else c0[c0[col].isna()]  # noqa: PLR0124
                row["c125_n"] = len(c)
                row["c125_per_risk"] = (
                    round(float(c["pnl"].sum() / c["risk"].sum()), 3) if len(c) else None
                )
                row["flip"] = bool(
                    len(c) >= 20
                    and len(g) >= 20
                    and np.sign(row["per_risk"]) != np.sign(row["c125_per_risk"])
                )
            rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. Drawdown episodes
# ---------------------------------------------------------------------------


def episodes(e: pd.Series, min_depth: float = DD_MIN) -> pd.DataFrame:
    """Peak → trough → recovery episodes deeper than *min_depth* of the peak equity."""
    out, peak_d, peak_v = [], e.index[0], float(e.iloc[0])
    trough_d, trough_v = peak_d, peak_v
    for d, v in e.items():
        v = float(v)
        if v >= peak_v:
            if (peak_v - trough_v) / peak_v > min_depth:
                out.append((peak_d, peak_v, trough_d, trough_v, d))
            peak_d, peak_v, trough_d, trough_v = d, v, d, v
        elif v < trough_v:
            trough_d, trough_v = d, v
    if (peak_v - trough_v) / peak_v > min_depth:
        out.append((peak_d, peak_v, trough_d, trough_v, None))
    days = list(e.index)
    rows = []
    for pk, pv, tr, tv, rec in out:
        rows.append(
            {
                "peak": pk,
                "trough": tr,
                "recovered": rec,
                "peak_equity": round(pv, 0),
                "trough_equity": round(tv, 0),
                "depth_pct": round((pv - tv) / pv * 100, 1),
                "slide_sessions": days.index(tr) - days.index(pk),
                "length_sessions": (days.index(rec) if rec else len(days) - 1) - days.index(pk),
            }
        )
    return pd.DataFrame(rows).sort_values("depth_pct", ascending=False).reset_index(drop=True)


def slides(e: pd.Series, down: float = DD_MIN, up: float = DD_MIN) -> pd.DataFrame:
    """Zig-zag decline legs: a fall of > *down* from a local high, where the local high
    resets once equity rallies > *up* off the running trough.

    A book that never regains its all-time high is one long peak→trough episode (margin
    2020-02 → 2023-04); its slides are the separable legs inside it. Recovery here means
    regaining the leg's local high.
    """
    days = list(e.index)
    v = e.to_numpy(dtype=float)
    legs: list[tuple[int, int]] = []
    hi_i, lo_i = 0, 0
    for i in range(1, len(v)):
        if v[i] < v[lo_i]:
            lo_i = i
        elif lo_i > hi_i and v[i] > v[lo_i] * (1 + up):
            if (v[hi_i] - v[lo_i]) / v[hi_i] > down:
                legs.append((hi_i, lo_i))
            hi_i = lo_i = i
        if v[i] > v[hi_i]:
            hi_i = lo_i = i
    if (v[hi_i] - v[lo_i]) / v[hi_i] > down and lo_i > hi_i:
        legs.append((hi_i, lo_i))
    rows = []
    for a, b in legs:
        rec = next((j for j in range(b, len(v)) if v[j] >= v[a]), None)
        rows.append(
            {
                "peak": days[a],
                "trough": days[b],
                "recovered": days[rec] if rec is not None else None,
                "peak_equity": round(float(v[a]), 0),
                "trough_equity": round(float(v[b]), 0),
                "depth_pct": round((v[a] - v[b]) / v[a] * 100, 1),
                "loss_usd": round(float(v[a] - v[b]), 0),
                "slide_sessions": b - a,
                "length_sessions": (rec if rec is not None else len(v) - 1) - a,
            }
        )
    cols = [
        "peak",
        "trough",
        "recovered",
        "peak_equity",
        "trough_equity",
        "depth_pct",
        "loss_usd",
        "slide_sessions",
        "length_sessions",
    ]
    return pd.DataFrame(rows, columns=cols)


def episode_attribution(
    t: pd.DataFrame, values: pd.DataFrame, ep: Mapping[str, Any], market: pd.DataFrame
) -> dict[str, Any]:
    """Who lost the peak → trough move, held-through vs opened during the slide."""
    pk, tr = ep["peak"], ep["trough"]
    dv = values.loc[tr] - values.loc[pk]
    x = t.copy()
    x["dv"] = dv.to_numpy()
    x = x[(x["entry_date"] <= tr) & (x["exit_day"] > pk)]
    x["when"] = np.where(x["entry_date"] <= pk, "held_through", "opened_in_slide")
    top = {}
    for col in ("kind", "underlying", "group", "direction", "when", "class"):
        g = x.groupby(col)["dv"].sum().sort_values()
        top[col] = g.round(0).head(6).to_dict()
    at_peak = x[x["when"] == "held_through"]
    eq_pk = float(ep["peak_equity"])

    def reg(d: dt.date) -> dict[str, Any]:
        m = market[market.index <= d].iloc[-1]
        return {
            "spy_trend": m["spy_trend"],
            "vix_proxy": round(float(m["vix_proxy"]), 3),
            "spy_dd": round(float(m["spy_dd_from_high"]), 3),
            "breadth": round(float(m["breadth_above_ma50"]), 2),
        }

    return {
        **ep,
        "move": round(float(dv.sum()), 0),
        "open_at_peak": len(at_peak),
        "risk_at_peak_pct": round(float(at_peak["risk"].sum()) / eq_pk * 100, 0),
        "dir_at_peak": at_peak["direction"].value_counts().to_dict(),
        "opened_in_slide": int((x["when"] == "opened_in_slide").sum()),
        "regime_peak": reg(pk),
        "regime_trough": reg(tr),
        "by": top,
    }


# ---------------------------------------------------------------------------
# 4. Trade paths
# ---------------------------------------------------------------------------


def path_stats(t: pd.DataFrame, paths: pd.DataFrame) -> pd.DataFrame:
    """Per trade: MAE/MFE in R, first −0.5R session, ever ≥ +0.3R, last mark before exit."""
    by = {k: g.sort_values("day") for k, g in paths.groupby("trade")}
    rows = []
    for r in t.itertuples():
        g = by.get(r.trade)
        ml = float(r.max_loss_unit)
        marks = (g["mark_unit"].to_numpy() / ml) if g is not None else np.array([])
        path = np.append(marks, r.R)
        hit = np.nonzero(marks <= -0.5)[0]
        green = np.nonzero(marks > 0)[0]
        rows.append(
            {
                "mae_R": float(path.min()),
                "mfe_R": float(path.max()),
                "marks": len(marks),
                "first_m05R_session": int(hit[0]) + 1 if len(hit) else None,
                "first_green_session": int(green[0]) + 1 if len(green) else None,
                "ever_p03R": bool((marks >= 0.3).any()),
                "last_mark_R": float(marks[-1]) if len(marks) else None,
            }
        )
    return pd.DataFrame(rows, index=t.index)


def path_tables(
    t: pd.DataFrame, ps: pd.DataFrame, paths: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    x = pd.concat([t, ps], axis=1)
    x["loser"] = x["R"] <= -0.5
    out: dict[str, pd.DataFrame] = {}
    rows = []
    for k, g in [("all", x), *x.groupby("kind")]:
        stop = g[g["exit_reason"] == "stop"]
        tp = g[g["exit_reason"] == "take_profit"]
        los = g[g["loser"]]
        gap = stop["last_mark_R"] > -0.5
        rows.append(
            {
                "kind": k,
                "n": len(g),
                "losers_05R": len(los),
                "loss_from_losers": round(float(los["pnl"].sum()), 0),
                "median_mae_R": round(float(g["mae_R"].median()), 2),
                "median_mfe_R": round(float(g["mfe_R"].median()), 2),
                "loser_median_session_to_m05R": float(los["first_m05R_session"].median())
                if los["first_m05R_session"].notna().any()
                else None,
                "stops": len(stop),
                "stops_were_p03R_first": round(float(stop["ever_p03R"].mean()), 2)
                if len(stop)
                else None,
                "stop_mean_R": round(float(stop["R"].mean()), 2) if len(stop) else None,
                "stop_overshoot_R": round(float((STOP_R - stop["R"]).mean()), 2)
                if len(stop)
                else None,
                "stops_gap_through": int(gap.sum()),
                "gap_through_loss": round(float(stop.loc[gap, "pnl"].sum()), 0),
                "gap_through_overshoot_R": round(float((STOP_R - stop.loc[gap, "R"]).mean()), 2)
                if gap.any()
                else None,
                "drift_overshoot_R": round(float((STOP_R - stop.loc[~gap, "R"]).mean()), 2)
                if (~gap).any()
                else None,
                "tp_rate": round(len(tp) / len(g), 2),
                "tp_median_days": float(tp["days_held"].median()) if len(tp) else None,
            }
        )
    out["path_by_kind"] = pd.DataFrame(rows)
    rows = []
    for n in (3, 5, 10):
        seen = x[x["marks"] >= n]
        never = seen["first_green_session"].isna() | (seen["first_green_session"] > n)
        at_n = mark_at(paths, seen, n) / seen["max_loss_unit"] if paths is not None else None
        for lab, g in (("never_green", seen[never]), ("green_by_N", seen[~never])):
            # descriptive "close at session N's EOD mark" (mid mark, net of open fees, no
            # exit slippage) vs what the trade actually returned
            r_n = at_n.loc[g.index] if at_n is not None else None
            rows.append(
                {
                    "N": n,
                    "group": lab,
                    "n": len(g),
                    "mean_R_final": round(float(g["R"].mean()), 3) if len(g) else None,
                    "mean_R_at_N_mark": round(float(r_n.mean()), 3)
                    if r_n is not None and len(g)
                    else None,
                    "loser_rate": round(float(g["loser"].mean()), 2) if len(g) else None,
                    "net": round(float(g["pnl"].sum()), 0),
                }
            )
    out["never_green"] = pd.DataFrame(rows)
    return out


def mark_at(paths: pd.DataFrame, trades: pd.DataFrame, n: int) -> pd.Series:
    """Per-unit mark at the trade's *n*-th marked session (NaN if it has fewer)."""
    by = {k: g.sort_values("day")["mark_unit"].to_numpy() for k, g in paths.groupby("trade")}
    return pd.Series(
        [by[k][n - 1] if k in by and len(by[k]) >= n else np.nan for k in trades["trade"]],
        index=trades.index,
    )


# ---------------------------------------------------------------------------
# 5. Leaderboard
# ---------------------------------------------------------------------------


def leaderboard(t: pd.DataFrame, n: int = 25) -> dict[str, pd.DataFrame]:
    rows = []
    for u, g in t.groupby("underlying"):
        yearly = g.groupby("year")["pnl"].sum()
        neg = int((yearly < 0).sum())
        rows.append(
            {
                "name": u,
                "class": g["class"].iloc[0],
                "group": g["group"].iloc[0],
                "n": len(g),
                "net": round(float(g["pnl"].sum()), 0),
                "per_risk": round(float(g["pnl"].sum() / g["risk"].sum()), 3),
                "win": round(float(g["win"].mean()), 2),
                "kinds": ",".join(g["kind"].value_counts().index[:3]),
                "spread_pct_premium": round(float(g["spread_pct_premium"].median()) * 100, 1),
                "years": len(yearly),
                "losing_years": neg,
                "worst_year_share_of_loss": round(float(yearly.min() / g["pnl"].sum()), 2)
                if g["pnl"].sum() < 0 and yearly.min() < 0
                else None,
                "pattern": "persistent" if neg >= 2 and neg / len(yearly) >= 0.6 else "episodic",
            }
        )
    lb = pd.DataFrame(rows)
    out = {}
    for cls in ("Stocks", "ETFs"):
        g = lb[lb["class"] == cls]
        out[f"{cls.lower()}_bottom_net"] = g.nsmallest(n, "net")
        out[f"{cls.lower()}_top_net"] = g.nlargest(n, "net")
        q = g[g["n"] >= 5]
        out[f"{cls.lower()}_bottom_per_risk"] = q.nsmallest(n, "per_risk")
        out[f"{cls.lower()}_top_per_risk"] = q.nlargest(n, "per_risk")
    out["groups"] = (
        lb.groupby("group")
        .agg(names=("name", "size"), n=("n", "sum"), net=("net", "sum"))
        .sort_values("net")
        .reset_index()
    )
    return out


# ---------------------------------------------------------------------------
# 6. Information audit
# ---------------------------------------------------------------------------


def _week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-{w:02d}"


def quintile_spread(
    t: pd.DataFrame, col: str, *, n_boot: int = N_BOOT, seed: int = SEED
) -> dict[str, Any] | None:
    """Top − bottom quintile mean R with a week-clustered bootstrap CI (90 %)."""
    x = t[[col, "R", "loser_05R", "entry_date", "pnl", "risk"]].dropna(subset=[col]).copy()
    x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna(subset=[col])
    if len(x) < 100 or x[col].nunique() < 5:
        return None
    x["q"] = pd.qcut(x[col].rank(method="first"), 5, labels=False) + 1
    q = x.groupby("q").agg(
        n=("R", "size"),
        lo=(col, "min"),
        hi=(col, "max"),
        mean_R=("R", "mean"),
        loser_rate=("loser_05R", "mean"),
        pnl=("pnl", "sum"),
        risk=("risk", "sum"),
    )
    x["wk"] = [_week(d) for d in x["entry_date"]]
    weeks = x["wk"].unique()
    groups = {w: g for w, g in x.groupby("wk")}
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        s = pd.concat([groups[w] for w in rng.choice(weeks, len(weeks))])
        a, b = s.loc[s["q"] == 5, "R"], s.loc[s["q"] == 1, "R"]
        if len(a) and len(b):
            diffs.append(float(a.mean() - b.mean()))
    lo, hi = np.percentile(diffs, [5, 95])
    spread = float(q.loc[5, "mean_R"] - q.loc[1, "mean_R"])
    worst = int(q["mean_R"].idxmin())
    return {
        "feature": col,
        "use": FEATURE_USE.get(col, "?"),
        "n": len(x),
        "q1_range": f"{q.loc[1, 'lo']:.3g}..{q.loc[1, 'hi']:.3g}",
        "q5_range": f"{q.loc[5, 'lo']:.3g}..{q.loc[5, 'hi']:.3g}",
        "q1_mean_R": round(float(q.loc[1, "mean_R"]), 3),
        "q5_mean_R": round(float(q.loc[5, "mean_R"]), 3),
        "q5_minus_q1_R": round(spread, 3),
        "ci90_lo": round(float(lo), 3),
        "ci90_hi": round(float(hi), 3),
        "ci_excludes_0": bool(lo > 0 or hi < 0),
        "q1_loser_rate": round(float(q.loc[1, "loser_rate"]), 2),
        "q5_loser_rate": round(float(q.loc[5, "loser_rate"]), 2),
        "worst_quintile": worst,
        "worst_q_net": round(float(q.loc[worst, "pnl"]), 0),
        "worst_q_per_risk": round(float(q.loc[worst, "pnl"] / q.loc[worst, "risk"]), 3),
    }


def bool_split(t: pd.DataFrame, col: str) -> dict[str, Any] | None:
    x = t.dropna(subset=[col])
    a, b = x[x[col].astype(bool)], x[~x[col].astype(bool)]
    if len(a) < 20 or len(b) < 20:
        return None
    return {
        "feature": col,
        "use": FEATURE_USE.get(col, "?"),
        "n_true": len(a),
        "true_mean_R": round(float(a["R"].mean()), 3),
        "false_mean_R": round(float(b["R"].mean()), 3),
        "diff_R": round(float(a["R"].mean() - b["R"].mean()), 3),
        "true_net": round(float(a["pnl"].sum()), 0),
        "true_per_risk": round(float(a["pnl"].sum() / a["risk"].sum()), 3),
        "false_per_risk": round(float(b["pnl"].sum() / b["risk"].sum()), 3),
    }


def univariate(t: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    num = [r for c in NUMERIC if c in t and (r := quintile_spread(t, c)) is not None]
    bo = [r for c in BOOLS if c in t and (r := bool_split(t, c)) is not None]
    u = pd.DataFrame(num)
    u["abs_spread"] = u["q5_minus_q1_R"].abs()
    return u.sort_values("abs_spread", ascending=False).drop(columns="abs_spread"), pd.DataFrame(bo)


def time_folds(years: pd.Series, exits: pd.Series) -> list[tuple[np.ndarray, np.ndarray, int]]:
    """Expanding-window folds: train on years < Y (trades that also *exited* before Y,
    so no label overlaps the test year), test on Y. Never shuffled."""
    out = []
    ys = sorted(set(years))
    for y in ys[1:]:
        start = dt.date(y, 1, 1)
        train = np.asarray((years < y) & np.array([d < start for d in exits]))
        test = np.asarray(years == y)
        if train.sum() >= 100 and test.sum() >= 50:
            out.append((np.nonzero(train)[0], np.nonzero(test)[0], y))
    return out


def classifier(t: pd.DataFrame, top: int = 10) -> dict[str, pd.DataFrame]:
    """Shallow GBM (depth ≤ 3) on entry-time features; time-ordered CV; OOS AUC,
    permutation importance and partial dependence (interpretability only)."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import partial_dependence, permutation_importance
    from sklearn.metrics import roc_auc_score

    cols = [c for c in NUMERIC + BOOLS if c in t]
    x = t[cols].apply(pd.to_numeric, errors="coerce").astype(float)
    kinds = pd.get_dummies(t["kind"], prefix="kind").astype(float)
    x = pd.concat([x, kinds], axis=1)
    y = t["loser_05R"].astype(int).to_numpy()
    folds = time_folds(t["year"], t["exit_day"])

    def model() -> HistGradientBoostingClassifier:
        return HistGradientBoostingClassifier(
            max_depth=3,
            learning_rate=0.05,
            max_iter=200,
            l2_regularization=1.0,
            min_samples_leaf=40,
            random_state=SEED,
        )

    aucs, imps = [], []
    oos = np.full(len(t), np.nan)
    for tr, te, yr in folds:
        m = model().fit(x.iloc[tr], y[tr])
        p = m.predict_proba(x.iloc[te])[:, 1]
        oos[te] = p
        auc = roc_auc_score(y[te], p) if len(set(y[te])) == 2 else float("nan")
        base = float(y[te].mean())
        aucs.append(
            {
                "test_year": yr,
                "train_n": len(tr),
                "test_n": len(te),
                "test_loser_rate": round(base, 3),
                "auc": round(float(auc), 3),
            }
        )
        pi = permutation_importance(
            m, x.iloc[te], y[te], scoring="roc_auc", n_repeats=5, random_state=SEED
        )
        imps.append(pd.Series(pi.importances_mean, index=x.columns, name=yr))
    ok = ~np.isnan(oos)
    pooled = roc_auc_score(y[ok], oos[ok]) if ok.any() else float("nan")
    aucs.append(
        {
            "test_year": "pooled",
            "train_n": None,
            "test_n": int(ok.sum()),
            "test_loser_rate": round(float(y[ok].mean()), 3),
            "auc": round(float(pooled), 3),
        }
    )
    imp = pd.concat(imps, axis=1)
    imp["mean"] = imp.mean(axis=1)
    imp = imp.sort_values("mean", ascending=False)
    imp_df = imp.round(4).reset_index(names="feature")
    imp_df["use"] = [
        FEATURE_USE.get(f, "kind" if f.startswith("kind_") else "?") for f in imp_df["feature"]
    ]
    # partial dependence on the model trained on every fold-eligible year but the last
    last_tr = folds[-1][0] if folds else np.arange(len(t))
    m = model().fit(x.iloc[last_tr], y[last_tr])
    pd_rows = []
    for f in [c for c in imp.index if not c.startswith("kind_")][:top]:
        col = x.columns.get_loc(f)
        xx = x.iloc[last_tr]
        xx = xx[xx[f].notna()]  # NaN rows make the percentile grid NaN
        res = partial_dependence(m, xx, [col], grid_resolution=5, kind="average")
        grid = res["grid_values"][0]
        avg = res["average"][0]
        pd_rows.append(
            {
                "feature": f,
                **{
                    f"x{i}": f"{g:.3g}→{a:.2f}"
                    for i, (g, a) in enumerate(zip(grid, avg, strict=False))
                },
            }
        )
    return {"auc": pd.DataFrame(aucs), "importance": imp_df.head(25), "pdp": pd.DataFrame(pd_rows)}


# ---------------------------------------------------------------------------
# 7. Calibration by regime
# ---------------------------------------------------------------------------


def calibration_slices(t: pd.DataFrame) -> pd.DataFrame:
    x = t.dropna(subset=["managed_net_ev_unit"]).copy()
    x["ivr_q"] = pd.qcut(x["opt_iv_rank_252"].rank(method="first"), 5, labels=False) + 1
    x.loc[x["opt_iv_rank_252"].isna(), "ivr_q"] = np.nan
    rows = []
    cuts = {
        "all": None,
        "kind": "kind",
        "mkt_spy_trend": "mkt_spy_trend",
        "mkt_vix_bucket": "mkt_vix_bucket",
        "ticker_trend": "trend",
        "iv_rank_q": "ivr_q",
    }
    for name, col in cuts.items():
        groups = [("all", x)] if col is None else list(x.groupby(col))
        for k, g in groups:
            if len(g) < 30:
                continue
            rows.append(
                {
                    "cut": name,
                    "bucket": k,
                    "n": len(g),
                    "pred_ev_unit": round(float(g["managed_net_ev_unit"].mean()), 1),
                    "real_pnl_unit": round(float(g["pnl_unit"].mean()), 1),
                    "pred_pop": round(float(g["managed_pop"].mean()), 2),
                    "real_win": round(float(g["win"].mean()), 2),
                    "spearman_ev_pnl": round(
                        float(g["managed_net_ev_unit"].corr(g["pnl_unit"], method="spearman")), 3
                    ),
                    "spearman_pop_win": round(
                        float(g["managed_pop"].corr(g["win"].astype(float), method="spearman")), 3
                    ),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 8. Counterfactual guards (descriptive, same path)
# ---------------------------------------------------------------------------

Guard = Callable[[pd.DataFrame], pd.Series]


def guard_table(t: pd.DataFrame, values: pd.DataFrame, guards: Mapping[str, Guard]) -> pd.DataFrame:
    """Drop the trades a guard blocks and re-sum the rest on the same marks/sizes.

    Same path: kept trades keep their size and dates; freed slots are not refilled.
    """
    rows = []
    for name, g in {"baseline": lambda x: pd.Series(False, index=x.index), **guards}.items():
        block = g(t).fillna(False).astype(bool).to_numpy()
        eq = rebuild_equity(values, ~block)
        kept = t[~block]
        peak = eq.cummax()
        row: dict[str, Any] = {
            "guard": name,
            "blocked": int(block.sum()),
            "blocked_net": round(float(t.loc[block, "pnl"].sum()), 0),
            "net": round(float(kept["pnl"].sum()), 0),
            "max_dd_pct": round(float(((peak - eq) / peak).max()) * 100, 1),
            "per_risk": round(float(kept["pnl"].sum() / kept["risk"].sum()), 4)
            if len(kept)
            else None,
            # size-free view: the book compounds (risk is a % of equity), so $ re-sums
            # over-weight early trades; Σ R and mean R treat every trade as one unit
            "sum_R": round(float(kept["R"].sum()), 1),
            "mean_R": round(float(kept["R"].mean()), 4) if len(kept) else None,
            "blocked_mean_R": round(float(t.loc[block, "R"].mean()), 4) if block.any() else None,
        }
        for y in (2020, 2021, 2022, 2023):
            s = eq[eq.index <= dt.date(y, 12, 31)]
            row[f"eq_{y}"] = round(float(s.iloc[-1]), 0) if len(s) else None
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Missing-information evidence (6d)
# ---------------------------------------------------------------------------


def missing_info(t: pd.DataFrame, ps: pd.DataFrame) -> pd.DataFrame:
    x = pd.concat([t, ps], axis=1)
    stop = x[x["exit_reason"] == "stop"]
    gap = stop[stop["last_mark_R"] > -0.5]
    overshoot = float(
        ((STOP_R - stop["R"]).clip(lower=0) * stop["max_loss_unit"] * stop["contracts"]).sum()
    )
    stocks = x[x["class"] == "Stocks"]
    earn = stocks[stocks["post_earn_in_hold"].fillna(False).astype(bool)]
    macro = x[x["fomc_within_2s"].astype(bool) | x["cpi_within_2s"].astype(bool)]
    flip = x[x["post_regime_change_10s"].astype(bool)]
    rows = [
        (
            "intraday path (stop fills at EOD)",
            "stops gapping through −0.5R→stop between closes",
            len(gap),
            float(gap["pnl"].sum()),
            overshoot,
        ),
        (
            "earnings calendar (real dates)",
            "stocks with a detected earnings day inside the hold "
            "the projected-date blackout missed",
            len(earn),
            float(earn["pnl"].sum()),
            None,
        ),
        (
            "macro calendar in selection",
            "entries with FOMC/CPI within 2 sessions",
            len(macro),
            float(macro["pnl"].sum()),
            None,
        ),
        (
            "market regime stance (news/LLM)",
            "entries ≤10 sessions before the SPY trend label changed",
            len(flip),
            float(flip["pnl"].sum()),
            None,
        ),
    ]
    return pd.DataFrame(
        rows, columns=["information", "evidence", "n", "net", "eod_stop_overshoot_$"]
    ).round(0)


__all__ = [
    "CHECK",
    "FEATURE_USE",
    "PRIMARY",
    "PROFILES",
    "bucket_table",
    "calibration_slices",
    "classifier",
    "episode_attribution",
    "episodes",
    "guard_table",
    "leaderboard",
    "load_book",
    "md_table",
    "missing_info",
    "path_stats",
    "path_tables",
    "quintile_spread",
    "rebuild_equity",
    "reconcile",
    "time_folds",
    "trade_values",
    "univariate",
]


def _bisect_le(days: list[dt.date], d: dt.date) -> int:  # pragma: no cover - helper
    return bisect.bisect_right(days, d) - 1
