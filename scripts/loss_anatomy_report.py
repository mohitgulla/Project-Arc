"""E7.18: write the loss-anatomy tables (CSVs + generated tables.md) from the books.

``docs/RESEARCH/loss-anatomy.md`` (the narrative) quotes these tables; every number in
it comes from a CSV written here. Aggregates only (D84 licence): no quote rows.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import loss_anatomy_analysis as la  # noqa: E402
from scripts.loss_anatomy import RUNS  # noqa: E402
from scripts.loss_attribution_report import md_table  # noqa: E402

#: Counterfactual guards (8): one variable each, picked from the largest buckets of
#: analyses 1-6 (see the report). Descriptive: same path, freed slots not refilled.
GUARDS: dict[str, la.Guard] = {
    "skip_spy_below_ma50_and_vix_pct_gt_80": lambda x: (
        (x["mkt_spy_dist_ma50"] < 0) & (x["mkt_vix_proxy_pctile"] > 0.8)
    ),
    "no_same_dir_risk_in_dd_gt_10pct": lambda x: (
        (x["drawdown_at_entry"] < -0.10)
        & (x["direction"] != "neutral")
        & (x["open_same_dir_share"] >= 0.5)
    ),
    "cluster_cap_2": lambda x: x["same_cluster_open"] >= 2,
    "skip_spread_gt_20pct_premium": lambda x: x["spread_pct_premium"] > 0.20,
    "skip_fomc_cpi_within_2s": lambda x: (
        x["fomc_within_2s"].astype(bool) | x["cpi_within_2s"].astype(bool)
    ),
    "open_risk_cap_30pct": lambda x: x["open_risk_pct"] > 0.30,
}
GRIDS = {
    "market_regime": ["mkt_spy_trend", "mkt_vix_bucket"],
    "ticker_regime": ["trend", "vol"],
}


def _bucket(x: pd.DataFrame) -> pd.DataFrame:
    x = x.copy()
    x["dte_bucket"] = pd.cut(x["dte_entry"], [0, 21, 31, 46, 61, 999], right=False).astype(str)
    x["hold_bucket"] = pd.cut(x["days_held"], [0, 3, 8, 15, 29, 999], right=False).astype(str)
    x["macro_2s"] = np.where(
        x["fomc_within_2s"].astype(bool) | x["cpi_within_2s"].astype(bool), "fomc/cpi<=2s", "no"
    )
    x["regime_cell"] = x["mkt_spy_trend"].astype(str) + " × VIX " + x["mkt_vix_bucket"].astype(str)
    x["ticker_cell"] = x["trend"].astype(str) + " × " + x["vol"].astype(str)
    x["kind_regime"] = x["kind"] + " | " + x["mkt_spy_trend"].astype(str)
    x["kind_ticker_trend"] = x["kind"] + " | " + x["trend"].astype(str)
    x["transition"] = np.where(x["post_regime_change_10s"].astype(bool), "label flips ≤10s", "no")
    x["etf_group"] = np.where(x["class"] == "ETFs", x["group"], "stock")
    return x


TIME_CUTS = (
    "dte_bucket",
    "hold_bucket",
    "weekday",
    "month",
    "half",
    "year",
    "macro_2s",
    "gap_day",
)
REGIME_CUTS = ("regime_cell", "ticker_cell", "kind_regime", "kind_ticker_trend", "transition")
OTHER_CUTS = ("kind", "direction", "group", "etf_group", "exit_reason", "tier")


def _w(out: Path, name: str, df: pd.DataFrame, md: list[str], title: str) -> None:
    df.to_csv(out / f"{name}.csv", index=False)
    md += [f"### {title}", f"`{name}.csv`", "", md_table(df), ""]


def _episode_cards(
    t: pd.DataFrame, e: pd.Series, paths: pd.DataFrame, market: pd.DataFrame, n: int = 5
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    days = list(e.index)
    vals = la.trade_values(t, paths, days)
    rebuilt = la.rebuild_equity(vals)
    gap = float((rebuilt - e).abs().max())
    if gap > 1.0:
        msg = f"rebuilt equity differs from the walk's by ${gap:,.2f}"
        raise ValueError(msg)
    ep = la.episodes(e)
    sl = la.slides(e).sort_values("loss_usd", ascending=False).reset_index(drop=True)
    cards = [la.episode_attribution(t, vals, r, market) for r in sl.head(n).to_dict("records")]
    return ep, sl, cards


def collect(book_name: str, profile: str) -> dict[str, Any]:
    run = RUNS / book_name
    t, e, paths = la.load_book(run, profile)
    t = _bucket(t)
    market = pd.read_parquet(run / "market.parquet")
    market.index = [d if isinstance(d, dt.date) else pd.Timestamp(d).date() for d in market.index]
    return {"t": t, "e": e, "paths": paths, "market": market, "run": run}


def write_report(out_root: Path) -> None:  # pragma: no cover - driven by the CLI
    out = out_root / "loss-anatomy"
    out.mkdir(parents=True, exist_ok=True)
    md: list[str] = [
        "# E7.18 loss anatomy: generated tables",
        "",
        "Written by `scripts/loss_anatomy.py report`; the narrative is `../loss-anatomy.md`.",
        "C500 dev is primary; `c125_*` columns are the same bucket on the C125 book and",
        "`flip` marks a per-$-risk sign change with n ≥ 20 on both.",
        "",
    ]
    head: dict[str, Any] = {}
    for p in la.PROFILES:
        A = collect(la.PRIMARY, p)
        B = collect(la.CHECK, p)
        t, e, paths, market = A["t"], A["e"], A["paths"], A["market"]
        c = B["t"]
        md += [f"## {p}", ""]
        rec = {k: la.reconcile(X["t"], X["e"]) for k, X in (("c500", A), ("c125", B))}
        head[p] = {"reconcile": rec}
        from scripts.loss_attribution_report import metrics

        mt = pd.DataFrame(
            [{"book": k, **metrics(X["t"], X["e"])} for k, X in (("c500_dev", A), ("c125_dev", B))]
        )
        mt["net_at_mid"] = [round(float(X["t"]["pnl_mid"].sum()), 0) for X in (A, B)]
        _w(out, f"{p}_books", mt, md, "Books (c500_mid_gross = net_at_mid)")
        rc = pd.DataFrame([{"book": k, **v} for k, v in rec.items()])
        _w(out, f"{p}_reconcile", rc, md, "Σ pnl vs Δequity")

        # 1. episodes
        ep, sl, cards = _episode_cards(t, e, paths, market)
        _w(out, f"{p}_episodes", ep, md, "Drawdown episodes > 10 % (peak → trough → new high)")
        _w(out, f"{p}_slides", sl, md, "Decline legs > 10 % (zig-zag, local high resets on +10 %)")
        ep_c, sl_c, _ = _episode_cards(c, B["e"], B["paths"], B["market"], n=0)
        _w(out, f"{p}_episodes_c125", ep_c, md, "Drawdown episodes > 10 % (C125)")
        _w(out, f"{p}_slides_c125", sl_c, md, "Decline legs > 10 % (C125)")
        flat = []
        for k, cd in enumerate(cards, 1):
            for dim, d in cd["by"].items():
                for name, v in d.items():
                    flat.append(
                        {"episode": k, "peak": cd["peak"], "dim": dim, "bucket": name, "move": v}
                    )
        _w(
            out,
            f"{p}_episode_attribution",
            pd.DataFrame(flat),
            md,
            "Decline-leg attribution (top 5 by $)",
        )
        head[p]["episode_cards"] = [
            {k: v for k, v in cd.items() if k != "by"} | {"by": cd["by"]} for cd in cards
        ]

        # 2./3./others: bucket tables
        for cut in (*REGIME_CUTS, *TIME_CUTS, *OTHER_CUTS):
            if cut not in t or cut not in c:
                continue
            tb = la.bucket_table(t, cut, c)
            _w(out, f"{p}_by_{cut}", tb, md, f"By {cut}")

        # 4. paths
        ps = la.path_stats(t, paths)
        pt = la.path_tables(t, ps, paths)
        for k, v in pt.items():
            _w(out, f"{p}_{k}", v, md, f"Trade paths: {k}")
        ps_c = la.path_stats(c, B["paths"])
        _w(
            out,
            f"{p}_path_by_kind_c125",
            la.path_tables(c, ps_c)["path_by_kind"],
            md,
            "Trade paths by kind (C125)",
        )

        # 5. leaderboard
        lb = la.leaderboard(t)
        for k, v in lb.items():
            _w(out, f"{p}_names_{k}", v, md, f"Names: {k}")

        # 6. information audit
        uni, bo = la.univariate(t)
        _w(out, f"{p}_features_univariate", uni, md, "6a. Feature quintiles (mean R, 90 % CI)")
        _w(out, f"{p}_features_flags", bo, md, "6a. Event flags")
        uni_c, _ = la.univariate(c)
        sign = uni[["feature", "q5_minus_q1_R"]].merge(
            uni_c[["feature", "q5_minus_q1_R"]], on="feature", suffixes=("", "_c125")
        )
        sign["flip"] = np.sign(sign["q5_minus_q1_R"]) != np.sign(sign["q5_minus_q1_R_c125"])
        _w(out, f"{p}_features_c125_sign", sign, md, "6a. C125 sign check")
        clf = la.classifier(t)
        for k, v in clf.items():
            _w(out, f"{p}_classifier_{k}", v, md, f"6b. Classifier: {k}")
        head[p]["auc"] = clf["auc"].to_dict("records")
        mi = la.missing_info(t, ps)
        _w(out, f"{p}_missing_info", mi, md, "6d. Evidence for missing information")

        # 7. calibration
        _w(out, f"{p}_calibration_slices", la.calibration_slices(t), md, "7. Calibration slices")

        # 8. guards
        vals = la.trade_values(t, paths, list(e.index))
        x = pd.concat([t, ps], axis=1)
        gt = la.guard_table(x, vals, GUARDS)
        _w(out, f"{p}_guards", gt, md, "8. Counterfactual guards (descriptive, same path)")

        # exposure + costs headline
        head[p]["cost"] = {
            "net": round(float(t["pnl"].sum()), 0),
            "at_mid": round(float(t["pnl_mid"].sum()), 0),
            "fees": round(float(t["fees"].sum()), 0),
            "slippage": round(float(t["slippage"].sum()), 0),
        }
        head[p]["share_stocks"] = round(float(t.loc[t["class"] == "Stocks", "pnl"].sum()), 0)
        head[p]["share_etfs"] = round(float(t.loc[t["class"] == "ETFs", "pnl"].sum()), 0)
    (out / "tables.md").write_text("\n".join(md) + "\n")
    (out / "headline.json").write_text(json.dumps(head, indent=1, default=str))
    sys.stdout.write(f"wrote {out}\n")
