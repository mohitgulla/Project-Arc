"""E7.19 (D125, D128/D129/D131): why the post-D128 re-baseline is worse than the D125 books.

    ARC_ENV=paper nice -n 10 .venv/bin/python scripts/sim_alignment_ablation.py \
        --book c125_dev --workers 6

Between ``data/backtest/runs/baseline_d125`` (pre-D128 config) and
``.../baseline_d125_post128`` four things moved at once:

1. the incumbent ranker (``credit_width`` / ``debit_width`` -> the live
   ``pipeline.menu_measure`` ranker ``managed_net_ev``);
2. the margin menu (credit only, one 21-45 window -> the D128 superset: credit +
   debit + iron condor, sleeves 10-35 / 36-60) -- a profile change, not a sim knob;
3. ``sim.entry_filters: live`` (the D78 anti-chase, debit-scoped);
4. ``sim.margin_buying_power`` (D129) and the D131 rules on main
   (``short_premium_regime``, ``open_risk_cap``).

The intraday proxy is already separated by the ``<book>_noproxy`` books. This script
builds the book twice (``entry_filters`` live / none; the menu cache is warm, so only
the outcome paths of the extra width ranker are new) and walks a grid over the other
toggles. Each cell is one walk on the same menus, so the deltas are attributable.
Writes ``<runs>/ablation/<book>/<profile>/<cell>/`` (trades, equity, ...) and
``<runs>/ablation/<book>/cells.csv``. Aggregates only (D84).
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_anatomy import BOOKS, DATA_END, END, PROFILES, START  # noqa: E402
from scripts.loss_attribution import DATA  # noqa: E402

RUNS = DATA / "backtest" / "runs" / "baseline_d125_post128"
DEBIT_KINDS = ("bull_call", "bear_put", "long_call", "long_put")


def _cells(profile: str, incumbent: str, width: str) -> list[dict[str, object]]:
    rankers = [incumbent, width]
    if profile == "margin":
        grid = itertools.product(rankers, (False, True), (True, False), (True, False))
        return [{"ranker": r, "credit_only": co, "bp": bp, "d131": d} for r, co, bp, d in grid]
    return [
        {"ranker": r, "credit_only": False, "bp": False, "d131": d}
        for r, d in itertools.product(rankers, (True, False))
    ]


def _name(filt: str, c: dict[str, object]) -> str:
    return (
        f"f-{filt}__{c['ranker']}__{'credit' if c['credit_only'] else 'all'}"
        f"__bp-{'on' if c['bp'] else 'off'}__d131-{'on' if c['d131'] else 'off'}"
    )


def _eq_at(eq: pd.Series, day: dt.date) -> float | None:
    s = eq[[d <= day for d in eq.index]]
    return round(float(s.iloc[-1]), 0) if len(s) else None


def run(book: str, workers: int, out_root: Path) -> pd.DataFrame:
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.sim import run_sim
    from arc.backtest.sim_report import _write_run, build_sim, run_summary
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.scanner.rank import Ranker
    from arc.universe.config import MomentumConfig

    start, end, data_end = (dt.date.fromisoformat(x) for x in (START, END, DATA_END))
    cost, exits = load_cost_model(), load_exit_config()
    rows: list[dict[str, object]] = []
    for filt in ("live", "none"):
        cfg = load_ranking_file()
        sim0 = cfg.backtest.sim.model_copy(update={"entry_filters": filt})
        cfg = cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"sim": sim0})})
        t0 = time.monotonic()
        b = build_sim(
            data_dir=DATA,
            universe=BOOKS[book],
            start=start,
            end=end,
            data_end=data_end,
            profiles=list(PROFILES),
            rankers=[Ranker.CREDIT_WIDTH, Ranker.DEBIT_WIDTH],
            cfg=cfg,
            cost=cost,
            exits=exits,
            settings=ArcSettings(),
            workers=workers,
        )
        sys.stdout.write(f"== {book} filter={filt} build {time.monotonic() - t0:.0f}s\n")
        sys.stdout.flush()
        for p in PROFILES:
            inc = b.rankers[p][0]
            width = Ranker.CREDIT_WIDTH if p == "margin" else Ranker.DEBIT_WIDTH
            for c in _cells(p, inc.value, width.value):
                sim = sim0.model_copy(
                    update={
                        "margin_buying_power": bool(c["bp"]),
                        "exclude_kinds": DEBIT_KINDS if c["credit_only"] else (),
                    }
                )
                st = b.per_profile[p]
                if not c["d131"]:
                    off = {"regime_gate_enabled": False, "max_open_risk_pct": 1.0}
                    st = st.model_copy(update=off)
                r = run_sim(
                    b.built,
                    ranker=Ranker(str(c["ranker"])),
                    profile=p,
                    settings=st,
                    sim=sim,
                    filters=cfg.ranking.filters,
                    vrp_threshold=cfg.ranking.vrp_threshold,
                    sessions=b.sessions,
                    end=end,
                    aliases=MomentumConfig().share_class_aliases,
                    trends=b.trends if c["d131"] else None,
                )
                name = _name(filt, c)
                _write_run(r, out_root / book / p / name)
                t = r.trades
                risk = float((t["contracts"] * t["max_loss_unit"]).sum()) if len(t) else 0.0
                rows.append(
                    {
                        "book": book,
                        "profile": p,
                        "cell": name,
                        "entry_filters": filt,
                        **c,
                        **run_summary(r, sim.equity),
                        "net_at_mid": round(float(t["pnl_mid"].sum()), 0) if len(t) else 0.0,
                        "per_risk": round(float(t["pnl"].sum()) / risk, 4) if risk else None,
                        "eq_2021_12_31": _eq_at(r.equity, dt.date(2021, 12, 31)),
                        **{f"skip_{k}": v for k, v in r.skip_counts.items() if v},
                    }
                )
    df = pd.DataFrame(rows)
    (out_root / book).mkdir(parents=True, exist_ok=True)
    df.to_csv(out_root / book / "cells.csv", index=False)
    return df


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E7.19 post-D128 ablation")
    ap.add_argument("--book", choices=sorted(BOOKS), required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=Path, default=RUNS / "ablation")
    args = ap.parse_args(argv)
    os.environ.setdefault("ARC_ENV", "paper")
    if (DATA / "backtest" / "queue.json").exists():
        sys.stderr.write("refused: data/backtest/queue.json exists (E26.3 runner owns it)\n")
        return 3
    df = run(args.book, args.workers, args.out)
    cols = ["profile", "cell", "trades", "net_pnl", "max_dd_pct", "per_risk", "net_at_mid"]
    sys.stdout.write(df[cols].to_string() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
