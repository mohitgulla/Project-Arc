"""E27.5 (D126): report-only comparison of the ML arm options, one study, own dirs.

    ARC_ENV=paper nice -n 10 .venv/bin/python scripts/ml_backtest_arms.py run \\
        --universe c125 --workers 6
    .venv/bin/python scripts/ml_backtest_arms.py tables --universe c125

``run`` writes a throwaway spec (``BT-9000``, never committed: D121, cards draft no BT
specs) to ``<out>/specs/`` and runs it with :func:`arc.backtest.study.run_study` on the
card's own ledger / runs dir (``data/backtest/runs/e27_5/``), never
``data/backtest/ledger.jsonl``. Arms (both profiles, the live incumbent ranker
``managed_net_ev`` = ``pipeline.menu_measure: managed_net_ev_full``, D128):

- control: the incumbent ranker;
- t1: ``ml_ranker: ml_er``;
- t2: the incumbent + ``ml_filter: {min_e_r: 0.0}`` (keep e_r ≥ 0);
- t3: ``ml_ranker: ml_er`` + ``ml_size: {mode: shrink}``.

The window is the M2 walk-forward test years (2022-01-03 → 2023-12-29, dev only): before
2022 no model may serve (``model_asof``), so every arm would equal the control. The grid is
the E7.20 default (``trend_v2 / noisy_0.35 / lagged_5 / market_v2`` × x 0 / 0.25 / 0.5).

``tables`` reads the run dir and writes ``tables.md`` (per arm: All / Stocks / ETFs, per
sleeve, the grid table, the score coverage) next to the E27.3 decision-value replay
numbers (``eval_replay.csv``), flagging any sign disagreement. Aggregates only (D84).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data" if (ROOT / "data").exists() else ROOT.parents[1] / "data"
OUT = DATA / "backtest" / "runs" / "e27_5"
STUDY = "BT-9000"
RANKER = "managed_net_ev"
START, END = dt.date(2022, 1, 3), dt.date(2023, 12, 29)
ARMS: dict[str, dict[str, Any]] = {
    "control": {"ranker": RANKER},
    "t1": {"ranker": RANKER, "overlay": {"ranking": {"backtest": {"ml_ranker": "ml_er"}}}},
    "t2": {
        "ranker": RANKER,
        "overlay": {"ranking": {"backtest": {"ml_filter": {"min_e_r": 0.0}}}},
    },
    "t3": {
        "ranker": RANKER,
        "overlay": {"ranking": {"backtest": {"ml_ranker": "ml_er", "ml_size": {"mode": "shrink"}}}},
    },
}
LABEL = {
    "control": "control (incumbent managed_net_ev)",
    "t1": "ml_er ranker",
    "t2": "incumbent + ml_filter e_r ≥ 0",
    "t3": "ml_er + ml_size shrink",
}
#: E27.3 replay use that matches each arm (eval_replay.csv ``use``).
REPLAY_USE = {"t1": "ml_er", "t2": "ml_er_pos", "t3": "ml_er"}
FAMILY = {"margin": "credit", "cash_debit": "debit"}


def spec_doc() -> dict[str, Any]:
    return {
        "id": STUDY,
        "hypothesis": (
            "E27.5 report-only: the M2 e_r ranker / filter / size beat the incumbent "
            "managed_net_ev on the 2022-2023 walk-forward test years (not a registered BT)."
        ),
        "owner": "owner",
        "created": "2026-10-27",
        "arms": {
            "control": ARMS["control"],
            "treatments": {k: ARMS[k] for k in ("t1", "t2", "t3")},
        },
        "profiles": ["margin", "cash_debit"],
        "universe_ladder": ["c125", "c500"],
        "period": {
            "dev_start": START.isoformat(),
            "dev_end": END.isoformat(),
            "holdout_start": "2024-01-02",
            "holdout_end": None,
        },
        "decision_rule": {
            "kind": "d25_challenge",
            "min_subperiod_wins": 2,
            "subperiods": ["bear", "sideways", "bull"],
            "bootstrap": {"resamples": 2000, "block_days": 20, "ci": 0.90, "seed": 7},
        },
        "robustness": {},
        "primary_metric": "net_pnl",
        "max_variants": 3,
    }


def _run(args: argparse.Namespace) -> int:
    from arc.backtest.study import load_study, run_study
    from arc.config import ArcSettings
    from arc.utils.calendar import now_et, previous_session

    specs = args.out / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    path = specs / f"{STUDY}.yaml"
    path.write_text(yaml.safe_dump(spec_doc(), sort_keys=False))
    spec = load_study(path)
    now = now_et()
    e = run_study(
        spec,
        phase="dev",
        universe=args.universe,
        now=now,
        latest=previous_session(now.date()),
        data_dir=args.data_dir,
        ledger=args.out / "ledger.jsonl",
        runs_dir=args.out / "runs",
        report_dir=None,
        settings=ArcSettings(),
        workers=args.workers,
        allow_overfit_warning=True,
        tickers=args.tickers.split(",") if args.tickers else None,
    )
    (args.out / f"latest-{args.universe}.txt").write_text(e.run_dir + "\n")
    sys.stdout.write(f"run dir: {e.run_dir}\n")
    return 0


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _per_risk(t: pd.DataFrame) -> float | None:
    risk = float((t["contracts"] * t["max_loss_unit"]).sum()) if len(t) else 0.0
    return round(float(t["pnl"].sum()) / risk, 4) if risk else None


def _row(t: pd.DataFrame) -> dict[str, Any]:
    return {
        "trades": len(t),
        "net": round(float(t["pnl"].sum()), 0) if len(t) else 0.0,
        "per_risk": _per_risk(t),
        "win": round(float((t["pnl"] > 0).mean()), 3) if len(t) else None,
    }


def split_table(run_dir: Path, profile: str) -> pd.DataFrame:
    """arm × (All / Stocks / ETFs) × sleeve on the control cell walks."""
    rows = []
    for arm in ARMS:
        f = run_dir / profile / arm / "trades.csv"
        t = pd.read_csv(f) if f.is_file() and f.stat().st_size > 1 else pd.DataFrame()
        if t.empty or "pnl" not in t:
            t = pd.DataFrame(columns=["pnl", "contracts", "max_loss_unit", "etf", "sleeve"])
        etf = t["etf"].astype(str).str.lower().isin(["true", "1"]) if len(t) else t["pnl"]
        for split, part in (("All", t), ("Stocks", t[~etf]), ("ETFs", t[etf])):
            sleeves = ["all", *sorted(part["sleeve"].dropna().astype(str).unique())]
            for sl in sleeves:
                g = part if sl == "all" else part[part["sleeve"].astype(str) == sl]
                rows.append({"arm": arm, "split": split, "sleeve": sl, **_row(g)})
    return pd.DataFrame(rows)


def year_table(run_dir: Path, profile: str) -> pd.DataFrame:
    """arm × entry year (net / trades / net per risk), All / Stocks / ETFs nets."""
    rows = []
    for arm in ARMS:
        f = run_dir / profile / arm / "trades.csv"
        t = pd.read_csv(f) if f.is_file() and f.stat().st_size > 1 else pd.DataFrame()
        if t.empty or "pnl" not in t:
            continue
        t["year"] = t["entry_date"].astype(str).str[:4]
        etf = t["etf"].astype(str).str.lower().isin(["true", "1"])
        for y, g in t.groupby("year"):
            e = etf.loc[g.index]
            rows.append(
                {
                    "arm": arm,
                    "year": y,
                    **_row(g),
                    "net_stocks": round(float(g.loc[~e, "pnl"].sum()), 0),
                    "net_etfs": round(float(g.loc[e, "pnl"].sum()), 0),
                }
            )
    return pd.DataFrame(rows)


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        out.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |")
    return "\n".join(out)


def replay_rows(models: Path, profile: str) -> pd.DataFrame:
    """E27.3's eval_replay.csv of the family's newest version: per year + both years summed.

    E27.3 walked a fresh book per test year on C500; this study walks one book through
    both years, so only the 2022 rows start from the same state.
    """
    from arc.ml.registry import pick_version

    fam = FAMILY[profile]
    ver = pick_version(models, fam)
    df = pd.read_csv(models / fam / ver / "eval_replay.csv")
    df["year"] = df["year"].astype(str)
    per = df[["year", "use", "trades", "net_pnl"]].rename(columns={"net_pnl": "net"})
    tot = per.groupby("use").agg(trades=("trades", "sum"), net=("net", "sum")).reset_index()
    tot.insert(0, "year", "2022+2023")
    out = pd.concat([per, tot], ignore_index=True)
    out["net"] = out["net"].round(0)
    out.insert(0, "version", ver)
    return out


def sign_checks(rr: pd.DataFrame, years: pd.DataFrame, verdicts: dict[str, Any]) -> list[str]:
    """Sim treatment − control vs replay use − incumbent: 2022 (same start) and total."""
    flags = []
    for arm, use in REPLAY_USE.items():
        for label in ("2022", "2022+2023"):
            r = rr[rr["year"] == label].set_index("use")["net"]
            if "incumbent" not in r or use not in r:
                continue
            rdiff = float(r[use] - r["incumbent"])
            if label == "2022":
                y = years[years["year"] == "2022"].set_index("arm")["net"] if len(years) else None
                if y is None or "control" not in y:
                    continue
                sdiff = float(y.get(arm, 0.0) - y["control"])
            else:
                d = (verdicts.get(arm) or {}).get("pnl_diff")
                if d is None:
                    continue
                sdiff = float(d)
            agree = (rdiff > 0) == (sdiff > 0)
            flags.append(
                f"- {label} · {arm} vs replay `{use}`: sim diff {sdiff:+.0f}, replay diff "
                f"{rdiff:+.0f} → {'agree' if agree else '**DISAGREE**'}"
            )
    return flags


def _tables(args: argparse.Namespace) -> int:
    latest = args.out / f"latest-{args.universe}.txt"
    run_dir = Path(args.run_dir or latest.read_text().strip())
    rep = json.loads((run_dir / "report.json").read_text())
    ml = (
        json.loads((run_dir / "ml_arms.json").read_text())
        if (run_dir / "ml_arms.json").is_file()
        else {}
    )
    lines = [
        f"# E27.5 ML arm comparison · {args.universe} · {START} → {END} (dev, report-only)",
        "",
        f"Run dir: `{run_dir.name}` (under `data/backtest/runs/e27_5/runs/{STUDY}/`)",
        "",
    ]
    for p in ("margin", "cash_debit"):
        lines += [f"## {p}", ""]
        v = rep.get("verdicts", {}).get(p, {})
        m = rep.get("metrics", {}).get(p, {})
        head = []
        for arm in ARMS:
            mm = m.get(arm, {})
            vv = v.get(arm, {})
            head.append(
                {
                    "arm": f"{arm}: {LABEL[arm]}",
                    "trades": mm.get("trades"),
                    "net": mm.get("net_pnl"),
                    "max_dd_pct": mm.get("max_dd_pct"),
                    "diff_vs_control": vv.get("pnl_diff"),
                    "ci_lo": vv.get("ci_lo"),
                    "ci_hi": vv.get("ci_hi"),
                    "robust": vv.get("robust_call"),
                    "cell_win_frac": (vv.get("grid") or {}).get("cell_win_frac"),
                    "coverage": (ml.get(p, {}).get(arm) or {}).get("coverage"),
                }
            )
        lines += [_md(pd.DataFrame(head)), ""]
        cells = (rep.get("robustness") or {}).get("cells", {}).get(p, {})
        if cells:
            grid = []
            for arm, by in cells.items():
                for cid, c in by.items():
                    grid.append(
                        {
                            "arm": arm,
                            "cell": cid,
                            "diff": c.get("pnl_diff"),
                            "ci_hi": c.get("ci_hi"),
                            "net_t": c.get("net_pnl_treatment"),
                            "net_c": c.get("net_pnl_control"),
                        }
                    )
            g = pd.DataFrame(grid).pivot(index="cell", columns="arm", values="diff")
            lines += ["Grid: treatment − control net P&L per cell", "", _md(g.reset_index()), ""]
        lines += [
            "All / Stocks / ETFs × sleeve (control cell)",
            "",
            _md(split_table(run_dir, p)),
            "",
            "Per entry year (control cell; one book across both years)",
            "",
            _md(year_table(run_dir, p)),
            "",
        ]
        rr = replay_rows(args.data_dir / "ml" / "models", p)
        lines += ["E27.3 decision-value replay (fresh C500 book per year)", "", _md(rr), ""]
        flags = sign_checks(rr, year_table(run_dir, p), v)
        lines += ["Sign check vs E27.3", "", *flags, ""]
    out = run_dir / "tables.md"
    out.write_text("\n".join(lines) + "\n")
    sys.stdout.write(out.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "tables"):
        p = sub.add_parser(name)
        p.add_argument("--universe", default="c125")
        p.add_argument("--data-dir", type=Path, default=DATA)
        p.add_argument("--out", type=Path, default=OUT)
        if name == "run":
            p.add_argument("--workers", type=int, default=6)
            p.add_argument("--tickers", default=None)
        else:
            p.add_argument("--run-dir", default=None)
    args = ap.parse_args(argv)
    return _run(args) if args.cmd == "run" else _tables(args)


if __name__ == "__main__":
    raise SystemExit(main())
