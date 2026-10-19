"""E7.19 (D125): old (one-expiry) vs sleeve-aligned books, written as CSVs + tables.md.

    .venv/bin/python scripts/sim_alignment_report.py

Reads the E7.18 books (``data/backtest/runs/anatomy/<book>``, pre-D125 sim), the D125
books on pre-D128 config (``data/backtest/runs/baseline_d125``, variant ``pre128``) and
the incumbent re-baseline (``data/backtest/runs/baseline_d125_post128/<book>`` and
``<book>_noproxy``, the same sim with ``intraday_proxy: none``); writes
``docs/RESEARCH/sim-alignment/``.
Aggregates only (D84 licence): no quote rows. Every table splits All / Stocks / ETFs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_attribution import DATA  # noqa: E402
from scripts.loss_attribution_report import book, md_table, metrics, reconcile  # noqa: E402

OLD = DATA / "backtest" / "runs" / "anatomy"
#: The D125 books on pre-D128 config (credit-only margin, width incumbents).
PRE128 = DATA / "backtest" / "runs" / "baseline_d125"
#: The incumbent: the same sim on post-D128 config (margin superset, the live
#: ``menu_measure`` ranker, debit-scoped anti-chase, margin buying power, D131 rules).
NEW = DATA / "backtest" / "runs" / "baseline_d125_post128"
BOOKS = ("c500_dev", "c125_dev")
PROFILES = ("margin", "cash_debit")
#: Entry-DTE buckets: the short sleeve (10-35) split at 21, the swing sleeve (36-60).
DTE_BINS = [0, 10, 21, 36, 46, 61, 999]
CLASSES = ("All", "Stocks", "ETFs")


def _variants(name: str) -> dict[str, Path]:
    out = {
        "old": OLD / name,
        "pre128": PRE128 / name,
        "pre128_noproxy": PRE128 / f"{name}_noproxy",
        "new": NEW / name,
        "new_noproxy": NEW / f"{name}_noproxy",
    }
    return {k: v for k, v in out.items() if (v / "manifest.json").exists()}


def _cls(t: pd.DataFrame, c: str) -> pd.DataFrame:
    return t if c == "All" else t[t["class"] == c]


def _dte(t: pd.DataFrame) -> pd.Series:
    return pd.Series(
        [(e - d).days for e, d in zip(t["expiration"], t["entry_date"], strict=True)],
        index=t.index,
        dtype=float,
    )


def collect() -> dict[str, pd.DataFrame]:
    books, recon, sleeves, dte, exits, skips, runtime = [], [], [], [], [], [], []
    for name in BOOKS:
        for var, run in _variants(name).items():
            man = json.loads((run / "manifest.json").read_text())
            runtime.append(
                {
                    "book": name,
                    "variant": var,
                    "git": man["git_sha"][:7],
                    **{f"{k}_s": v for k, v in man["runtime_s"].items()},
                    "peak_rss_mb": man["peak_rss_mb"],
                    "cache_hit_rate": man["cache_hit_rate"],
                }
            )
            sk = pd.read_csv(run / "skip_counts.csv")
            for p in PROFILES:
                t, e = book(run, p)
                row = sk[sk["profile"] == p].iloc[0].to_dict()
                skips.append(
                    {
                        "book": name,
                        "variant": var,
                        "profile": p,
                        "sleeve_full": int(row.get("sleeve_full", 0) or 0),
                        "gate_dte_window": int(row.get("gate_dte_window", 0) or 0),
                        "gate_max_open_positions": int(row["gate_max_open_positions"]),
                        "gate_daily_loss": int(row["gate_daily_loss"]),
                        **{
                            k: int(row.get(k, 0) or 0)
                            for k in (
                                "buying_power",
                                "cash",
                                "sizing",
                                "gate_open_risk_cap",
                                "gate_short_premium_regime",
                            )
                        },
                    }
                )
                rc = reconcile(t, e)
                recon.append({"book": name, "variant": var, "profile": p, **rc})
                t = t.assign(dte_entry=_dte(t))
                if "sleeve" not in t:
                    t["sleeve"] = "pre-D125"
                for c in CLASSES:
                    x = _cls(t, c)
                    m = metrics(x, e) if c == "All" else {"trades": len(x)}
                    books.append(
                        {
                            "book": name,
                            "variant": var,
                            "profile": p,
                            "class": c,
                            **m,
                            "net": round(float(x["pnl"].sum()), 0),
                            "net_at_mid": round(float(x["pnl_mid"].sum()), 0),
                            "per_risk": round(float(x["pnl"].sum() / x["risk"].sum()), 4)
                            if len(x)
                            else None,
                            "win": round(float(x["win"].mean()), 3) if len(x) else None,
                        }
                    )
                    if var == "old":
                        continue
                    for s, g in x.groupby("sleeve", sort=True):
                        mix = g["exit_reason"].value_counts()
                        sleeves.append(
                            {
                                "book": name,
                                "variant": var,
                                "profile": p,
                                "class": c,
                                "sleeve": s,
                                "n": len(g),
                                "net": round(float(g["pnl"].sum()), 0),
                                "per_risk": round(float(g["pnl"].sum() / g["risk"].sum()), 4),
                                "win": round(float(g["win"].mean()), 3),
                                "mean_dte_entry": round(float(g["dte_entry"].mean()), 1),
                                "mean_days_held": round(float(g["days_held"].mean()), 1),
                                "exit_mix": " ".join(f"{k}:{v}" for k, v in mix.items()),
                            }
                        )
                b = pd.cut(t["dte_entry"], DTE_BINS, right=False).astype(str)
                for k, g in t.groupby(b, sort=True):
                    dte.append(
                        {
                            "book": name,
                            "variant": var,
                            "profile": p,
                            "dte_bucket": k,
                            "n": len(g),
                            "net": round(float(g["pnl"].sum()), 0),
                            "per_risk": round(float(g["pnl"].sum() / g["risk"].sum()), 4),
                        }
                    )
                for r, g in t.groupby("exit_reason", sort=True):
                    exits.append(
                        {
                            "book": name,
                            "variant": var,
                            "profile": p,
                            "exit_reason": r,
                            "n": len(g),
                            "share": round(len(g) / len(t), 3),
                            "net": round(float(g["pnl"].sum()), 0),
                        }
                    )
    return {
        "books": pd.DataFrame(books),
        "reconcile": pd.DataFrame(recon),
        "sleeves": pd.DataFrame(sleeves),
        "dte_buckets": pd.DataFrame(dte),
        "exit_mix": pd.DataFrame(exits),
        "skips": pd.DataFrame(skips),
        "runtime": pd.DataFrame(runtime),
        "ablation": ablation(),
    }


#: The attribution grid (``scripts/sim_alignment_ablation.py``), aggregates only.
ABLATION_COLS = (
    "book",
    "profile",
    "entry_filters",
    "ranker",
    "credit_only",
    "bp",
    "d131",
    "trades",
    "net_pnl",
    "max_dd_pct",
    "per_risk",
    "net_at_mid",
    "win_rate",
    "eq_2021_12_31",
)


def ablation() -> pd.DataFrame:
    parts = [
        pd.read_csv(f) for name in BOOKS if (f := NEW / "ablation" / name / "cells.csv").exists()
    ]
    if not parts:
        return pd.DataFrame(columns=list(ABLATION_COLS))
    df = pd.concat(parts, ignore_index=True)
    return df[list(ABLATION_COLS)].sort_values(
        ["book", "profile", "entry_filters", "ranker", "credit_only", "bp", "d131"]
    )


def main() -> int:
    out = ROOT / "docs" / "RESEARCH" / "sim-alignment"
    out.mkdir(parents=True, exist_ok=True)
    md = [
        "# E7.19 sim alignment: generated tables",
        "",
        "Written by `scripts/sim_alignment_report.py`; the narrative is `../sim-alignment.md`.",
        "`old` = E7.18 books (one expiry nearest the window middle); `pre128` = the D125",
        "sleeve-aligned sim on pre-D128 config (credit-only margin, width incumbents);",
        "`new` = the incumbent: the same sim on post-D128 config (margin superset, the",
        "live `menu_measure` ranker `managed_net_ev`, debit-scoped anti-chase, margin",
        "buying power, D131 rules); `*_noproxy` = the same with `intraday_proxy: none`.",
        "",
    ]
    for k, df in collect().items():
        df.to_csv(out / f"{k}.csv", index=False)
        md += [f"## {k}", f"`{k}.csv`", "", md_table(df), ""]
    (out / "tables.md").write_text("\n".join(md) + "\n")
    sys.stdout.write(f"wrote {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
