"""E7.17: write the generated tables (CSV + markdown) behind docs/RESEARCH/loss-attribution.md.

Every number in the narrative report comes from a file this writes under
``docs/RESEARCH/loss-attribution/``. Aggregates only (no vendor rows).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_attribution import RUN_SPECS  # noqa: E402
from scripts.loss_attribution_report import (  # noqa: E402
    BT3,
    BT8,
    CUTS,
    PROFILES,
    book,
    calibration,
    collect,
    concentration,
    costs,
    decompose,
    drawdown_path,
    earnings_events,
    exposure_profile,
    live_trades,
    md_table,
    metrics,
    reconcile,
    sanity,
    tag_earnings,
    worst_days,
)

SANITY_COLS = [
    "profile",
    "sample",
    "underlying",
    "kind",
    "entry_date",
    "contracts",
    "sim_entry_net",
    "rp_entry_net",
    "sim_exit_reason",
    "exit_date",
    "rp_exit_reason",
    "rp_exit_day",
    "quoted_sessions",
    "sim_pnl_unit",
    "rp_pnl_unit",
    "d_pnl_unit",
    "sim_fees_unit",
    "rp_fees_unit",
    "risk_pct_equity",
    "splits_in_hold",
    "exit_match",
]


def _w(out: Path, name: str, df: pd.DataFrame, md: list[str], title: str) -> None:
    df.to_csv(out / f"{name}.csv", index=False)
    md += [f"## {title}", "", f"`{name}.csv`", "", md_table(df), ""]


def render(out_root: Path) -> None:
    out = out_root / "loss-attribution"
    out.mkdir(parents=True, exist_ok=True)
    md: list[str] = ["# E7.17 loss attribution: generated tables", ""]
    md += ["Written by `scripts/loss_attribution.py report`. Aggregates only.", ""]
    res = collect()

    # 0. runs
    runs = pd.DataFrame(
        [
            {"run": n, "profile": p, "what": RUN_SPECS.get(n, ("", "", ""))[2], **m}
            for (n, p), m in res["runs"].items()
        ]
    )
    _w(out, "runs", runs, md, "Runs and counterfactuals")

    # 1. sanity on the E7.14 base book and on the fixed book
    for label, run, arm in [("bt3", BT3, "control"), ("fixed", None, None)]:
        if run is None:
            from scripts.loss_attribution import RUNS

            run = RUNS / "fixed"
            if not (run / "report.md").exists():
                continue
        s = sanity(run, arm)
        _w(out, f"sanity_{label}", s[[c for c in SANITY_COLS if c in s]], md, f"Sanity ({label})")
        s["entry_exact"] = (s["rp_entry_net"] - s["sim_entry_net"]).abs() < 1e-3
        s["pnl_exact"] = s["d_pnl_unit"].abs() < 0.05
        summ = (
            s.groupby("profile")
            .agg(
                n=("exit_match", "size"),
                entry_exact=("entry_exact", "sum"),
                exit_match=("exit_match", "sum"),
                pnl_exact=("pnl_exact", "sum"),
                sum_d_pnl_unit=("d_pnl_unit", "sum"),
            )
            .reset_index()
        )
        _w(out, f"sanity_{label}_summary", summ, md, f"Sanity summary ({label})")
    rec = []
    for (n, p), (t, e) in res["books"].items():
        rec.append({"book": n, "profile": p, **reconcile(t, e)})
    _w(out, "reconcile", pd.DataFrame(rec), md, "Equity reconciliation (sum pnl vs delta equity)")

    # 2-5 on the base books (bt3 = E7.14 control, fixed = corrected)
    names = sorted({t for (n, _), (tt, _) in res["books"].items() for t in tt["underlying"]})
    events = earnings_events(names)
    for key in ("bt3", "fixed"):
        for p in PROFILES:
            if (key, p) not in res["books"]:
                continue
            t, e = res["books"][(key, p)]
            t = tag_earnings(t, events)
            parts = []
            for cut, col in CUTS.items():
                d = decompose(t, col)
                d.insert(0, "cut", cut)
                parts.append(d)
            _w(out, f"decomp_{key}_{p}", pd.concat(parts), md, f"Decomposition {key} {p}")
            _w(out, f"costs_{key}_{p}", costs(t), md, f"Costs {key} {p}")
            c, dec = calibration(t)
            _w(out, f"calib_{key}_{p}", c, md, f"Calibration by kind {key} {p}")
            _w(out, f"calib_decile_{key}_{p}", dec, md, f"Calibration by EV decile {key} {p}")
            tail: dict[str, Any] = {
                **concentration(t),
                **drawdown_path(t, e),
                **exposure_profile(t, e),
            }
            _w(out, f"tail_{key}_{p}", pd.DataFrame([tail]), md, f"Tail {key} {p}")
            _w(out, f"worst_days_{key}_{p}", worst_days(t, e), md, f"Worst days {key} {p}")

    # BT-8 (2024-03 -> 2026-07, existing results only)
    b8 = []
    for p in PROFILES:
        for arm in ("control", "t1"):
            t, e = book(BT8, p, arm)
            m = metrics(t, e)
            by = t.groupby("exit_reason")["pnl"].sum().round(0).to_dict()
            b8.append({"profile": p, "arm": arm, **m, **{f"net_{k}": v for k, v in by.items()}})
    _w(out, "bt8", pd.DataFrame(b8), md, "BT-8 (6 tickers, 2024-03 -> 2026-07, existing run)")

    # 7. live
    lv = live_trades()
    if len(lv):
        _w(out, "live_closed", lv.drop(columns=["contracts"]), md, "Live closed trades")
    (out / "tables.md").write_text("\n".join(md))
    (out / "meta.json").write_text(json.dumps({"runs": sorted({n for n, _ in res["runs"]})}))
