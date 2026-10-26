"""E28.6 (D131): report-only baseline of the margin book, all D131 rules on vs pre-D131.

    ARC_ENV=paper nice -n 10 .venv/bin/python scripts/d131_baseline.py \\
        --book c125_dev --workers 6

Two menu builds of the margin profile on the C125 dev window (2020-01-01 -> 2023-12-31),
each walked with the D131 gate rules on and off, so menu and gate effects separate:

- ``d131``: the shipped config (E28.1 wing: widest gap <= 5 % of spot fitting the
  per-underlying cap, credit/width floor 0.10) walked with the shipped rules
  (``short_premium_regime`` 200-SMA, ``open_risk_cap`` 40 %, ``cluster_cap`` 0.7 / 2);
- ``pre``: the pre-D131 sim credit menu (``backtest.credit_wings``: nearest 2 % wing,
  no cap fit, no floor) walked with every D131 rule off (``backtest.sim.gate``:
  ``regime_gate_enabled: false``, ``max_open_risk_pct: 1.0``,
  ``cluster_cap_enabled: false``) = main before D131;
- the two cross cells (``d131`` menu / rules off, ``pre`` menu / rules on);
- each of the four again with the debit kinds excluded (``sim.exclude_kinds``, suffix
  ``__credit``): a diagnostic, not an arm. All four full cells ruin the $25K book, and
  E7.19 traced that ruin to the D128 debit kinds on the margin menu; the credit cells
  show what the D131 rules do to the credit book they were written for.

Every arm is the E28.6 overlay knobs, not code edits. Writes
``<runs>/d131_baseline/<book>/<cell>/`` (trades, equity, skips, sleeves), plus
``cells.csv``, ``by_kind.csv`` and ``by_sleeve.csv`` under the book. Relative A/B only
(E7.18 harness caveats); aggregates only (D84).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_anatomy import BOOKS, DATA_END, END, START  # noqa: E402
from scripts.loss_attribution import DATA  # noqa: E402

RUNS = DATA / "backtest" / "runs" / "d131_baseline"
PROFILE = "margin"
#: The pre-D131 sim credit menu (tests/test_backtest_sim.py legacy note).
PRE_WINGS = {"mode": "pct_nearest", "pct": 0.02, "min_credit_width": 0.0}
#: Every D131 gate rule off (the E28.2/E28.3/E28.5 rollback values).
RULES_OFF = {"regime_gate_enabled": False, "max_open_risk_pct": 1.0, "cluster_cap_enabled": False}
CELLS = (("d131", True), ("d131", False), ("pre", False), ("pre", True))
#: The margin menu's debit kinds (D128 superset); excluded in the ``__credit`` cells.
DEBIT_KINDS = ("bull_call", "bear_put", "long_call", "long_put")


def _cell(menu: str, rules: bool, credit_only: bool = False) -> str:
    base = f"menu-{menu}__rules-{'on' if rules else 'off'}"
    return f"{base}__credit" if credit_only else base


def _per_risk(t: pd.DataFrame) -> float | None:
    risk = float((t["contracts"] * t["max_loss_unit"]).sum()) if len(t) else 0.0
    return round(float(t["pnl"].sum()) / risk, 4) if risk else None


def _group(t: pd.DataFrame, col: str, cell: str) -> pd.DataFrame:
    rows = []
    for k, g in t.groupby(col) if len(t) else []:
        rows.append(
            {
                "cell": cell,
                col: k,
                "n": len(g),
                "net": round(float(g["pnl"].sum()), 2),
                "win_rate": round(float((g["pnl"] > 0).mean()), 3),
                "per_risk": _per_risk(g),
            }
        )
    return pd.DataFrame(rows)


def run(book: str, workers: int, out_root: Path) -> pd.DataFrame:
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import CreditWings, load_ranking_file
    from arc.backtest.sim import run_sim
    from arc.backtest.sim_config import SimGateOverrides
    from arc.backtest.sim_report import _write_run, build_sim, run_summary
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.universe.config import MomentumConfig

    start, end, data_end = (dt.date.fromisoformat(x) for x in (START, END, DATA_END))
    cost, exits = load_cost_model(), load_exit_config()
    base = load_ranking_file()
    out = out_root / book
    rows: list[dict[str, object]] = []
    kinds: list[pd.DataFrame] = []
    sleeves: list[pd.DataFrame] = []
    for menu in ("d131", "pre"):
        wings = CreditWings.model_validate(PRE_WINGS if menu == "pre" else {})
        cfg = base.model_copy(
            update={"backtest": base.backtest.model_copy(update={"credit_wings": wings})}
        )
        t0 = time.monotonic()
        b = build_sim(
            data_dir=DATA,
            universe=BOOKS[book],
            start=start,
            end=end,
            data_end=data_end,
            profiles=[PROFILE],
            rankers=[],
            cfg=cfg,
            cost=cost,
            exits=exits,
            settings=ArcSettings(),
            workers=workers,
        )
        build_s = time.monotonic() - t0
        sys.stdout.write(f"== {book} menu={menu} build {build_s:.0f}s\n")
        sys.stdout.flush()
        ranker = b.rankers[PROFILE][0]
        for (m, rules), credit_only in ((c, co) for co in (False, True) for c in CELLS):
            if m != menu:
                continue
            gate = SimGateOverrides() if rules else SimGateOverrides.model_validate(RULES_OFF)
            excl = DEBIT_KINDS if credit_only else ()
            sim = cfg.backtest.sim.model_copy(update={"gate": gate, "exclude_kinds": excl})
            r = run_sim(
                b.built,
                ranker=ranker,
                profile=PROFILE,
                settings=b.per_profile[PROFILE],
                sim=sim,
                filters=cfg.ranking.filters,
                vrp_threshold=cfg.ranking.vrp_threshold,
                sessions=b.sessions,
                end=end,
                aliases=MomentumConfig().share_class_aliases,
                trends=b.trends,
            )
            name = _cell(menu, rules, credit_only)
            _write_run(r, out / name)
            t = r.trades
            rows.append(
                {
                    "cell": name,
                    "menu": menu,
                    "rules": rules,
                    "credit_only": credit_only,
                    **run_summary(r, sim.equity),
                    "per_risk": _per_risk(t),
                    "net_mid": round(float(t["pnl_mid"].sum()), 2) if len(t) else 0.0,
                    "build_s": round(build_s, 0),
                    **{f"skip_{k}": v for k, v in r.skip_counts.items() if v},
                }
            )
            kinds.append(_group(t, "kind", name))
            sleeves.append(_group(t, "sleeve", name))
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out / "cells.csv", index=False)
    pd.concat(kinds, ignore_index=True).to_csv(out / "by_kind.csv", index=False)
    pd.concat(sleeves, ignore_index=True).to_csv(out / "by_sleeve.csv", index=False)
    return df


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E28.6 D131 baseline (margin, report-only)")
    ap.add_argument("--book", choices=sorted(BOOKS), default="c125_dev")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=Path, default=RUNS)
    args = ap.parse_args(argv)
    os.environ.setdefault("ARC_ENV", "paper")
    if (DATA / "backtest" / "queue.json").exists():
        sys.stderr.write("refused: data/backtest/queue.json exists (E26.3 runner owns it)\n")
        return 3
    df = run(args.book, args.workers, args.out)
    cols = ["cell", "trades", "net_pnl", "net_mid", "max_dd_pct", "win_rate", "per_risk"]
    sys.stdout.write(df[cols].to_string() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
