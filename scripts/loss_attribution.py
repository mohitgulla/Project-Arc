"""E7.17 (D84): loss attribution of the live-shaped backtest (report only).

Two steps, both reproducible from this file:

    # 1. the attribution sims (simulator fix + one-variable counterfactuals)
    nice -n 10 .venv/bin/python scripts/loss_attribution.py runs --workers 4
    nice -n 10 .venv/bin/python scripts/loss_attribution.py walks --workers 4
    # 2. the report: docs/RESEARCH/loss-attribution.md + aggregate CSVs
    .venv/bin/python scripts/loss_attribution.py report

Runs land in ``data/backtest/runs/attribution/<name>/`` (C125, dev period
2020-01-01 -> 2023-12-31, data end 2024-03-30, both profiles, incumbent ranker),
each driven by a generated partial ranking overlay. The holdout (2024-01-01 on) is
read only as the settlement tail for trades opened by 2023-12-31, exactly as E7.14.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _default_data() -> Path:
    """``data/`` of this checkout, else the primary checkout's (card worktrees share it)."""
    own = REPO / "data"
    if (own / "options_eod").exists():
        return own
    common = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return Path(common).parent / "data" if common else own


DATA = Path(os.environ.get("ARC_ATTR_DATA") or _default_data())
RUNS = DATA / "backtest" / "runs" / "attribution"
START, END, DATA_END = "2020-01-01", "2023-12-31", "2024-03-30"

#: name -> (sim overlay, extra env, what it isolates). One variable each vs ``fixed``.
RUN_SPECS: dict[str, tuple[dict[str, object], dict[str, str], str]] = {
    "legacy": (
        {"exit_marks": "entry_chain", "splits": "ignore"},
        {},
        "E7.11/E7.14 simulator (reproduces the BT-3 control book)",
    ),
    "marks_only": ({"splits": "ignore"}, {}, "exit-book fix only (splits still ignored)"),
    "fixed": ({}, {}, "corrected simulator: base book for attribution"),
    "est_spread": (
        {"fill_spread": "estimated"},
        {},
        "fills at the costs.yaml estimated spread (4 % of mid, min $0.03) not the quote",
    ),
    "half_size": ({"size_scale": 0.5}, {}, "contracts x 0.5"),
    "half_slots": ({}, {"ARC_MAX_OPEN_POSITIONS": "5"}, "max_open_positions 10 -> 5"),
    "day_risk_cap": (
        {"max_new_risk_pct_day": 0.15},
        {},
        "new max loss per day <= 15 % of equity (one max_alloc_pct position)",
    ),
    # Kinds are disjoint across profiles, so each run removes one kind per profile.
    "no_bull_put__no_bull_call": (
        {"exclude_kinds": ["bull_put", "bull_call"]},
        {},
        "minus bull_put (margin) / bull_call (cash_debit)",
    ),
    "no_bear_call__no_bear_put": (
        {"exclude_kinds": ["bear_call", "bear_put"]},
        {},
        "minus bear_call (margin) / bear_put (cash_debit)",
    ),
    "no_iron_condor__no_long_call": (
        {"exclude_kinds": ["iron_condor", "long_call"]},
        {},
        "minus iron_condor (margin) / long_call (cash_debit)",
    ),
    "no_long_put": ({"exclude_kinds": ["long_put"]}, {}, "minus long_put (cash_debit)"),
}


def _run(name: str, workers: int) -> int:
    sim, env_extra, _ = RUN_SPECS[name]
    out = RUNS / name
    out.mkdir(parents=True, exist_ok=True)
    overlay = out / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"experiment": f"E7.17 {name}", "backtest": {"sim": sim}}, sort_keys=True)
    )
    cmd = [
        str(REPO / ".venv" / "bin" / "arc"),
        "backtest",
        "sim",
        "--universe",
        "c125",
        "--from",
        START,
        "--to",
        END,
        "--data-end",
        DATA_END,
        "--data-dir",
        str(DATA),
        "--out",
        str(out),
        "--experiment",
        str(overlay),
        "--workers",
        str(workers),
    ]
    env = {**os.environ, "ARC_ENV": "paper", **env_extra}
    sys.stdout.write(f"== {name}: {' '.join(cmd)} {env_extra or ''}\n")
    sys.stdout.flush()
    with (out / "run.log").open("w") as log:
        return subprocess.run(
            cmd, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
        ).returncode


#: Variants that change only the portfolio walk: one build (``fixed``), many walks.
WALK_ONLY = (
    "half_size",
    "half_slots",
    "day_risk_cap",
    "no_bull_put__no_bull_call",
    "no_bear_call__no_bear_put",
    "no_iron_condor__no_long_call",
    "no_long_put",
)


def _walks(names: list[str], workers: int) -> int:
    """Build the ``fixed`` book once, then walk each walk-only variant on it."""
    import datetime as dt

    import structlog

    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.sim_report import _write_run, build_sim, run_summary, walk
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config

    log = structlog.get_logger()
    cfg = load_ranking_file()
    start, end, data_end = (dt.date.fromisoformat(x) for x in (START, END, DATA_END))
    settings = ArcSettings()
    b = build_sim(
        data_dir=DATA,
        universe="c125",
        start=start,
        end=end,
        data_end=data_end,
        profiles=["margin", "cash_debit"],
        rankers=[],
        cfg=cfg,
        cost=load_cost_model(),
        exits=load_exit_config(),
        settings=settings,
        workers=workers,
    )
    base = b
    for name in names:
        sim_upd, env_extra, what = RUN_SPECS[name]
        sim = cfg.backtest.sim.model_validate({**cfg.backtest.sim.model_dump(), **sim_upd})
        vcfg = cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"sim": sim})})
        upd: dict[str, object] = {}
        if "ARC_MAX_OPEN_POSITIONS" in env_extra:
            upd["max_open_positions"] = int(env_extra["ARC_MAX_OPEN_POSITIONS"])
        b = base.model_copy(
            update={
                "per_profile": {k: v.model_copy(update=upd) for k, v in base.per_profile.items()}
            }
        )
        out = RUNS / name
        rows = []
        for p in ("margin", "cash_debit"):
            for r in b.rankers[p]:
                run = walk(b, profile=p, ranker=r, cfg=vcfg, end=end)
                _write_run(run, out / p / r.value)
                rows.append(run_summary(run, sim.equity))
        (out / "report.md").write_text(
            f"# E7.17 walk variant `{name}`\n\n{what}\n\nBuild: `fixed` (one build, "
            f"walk-only change).\n\n" + "\n".join(str(x) for x in rows) + "\n"
        )
        log.info("e717.walk.done", name=name)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E7.17 loss attribution")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("runs", help="Full sim builds (legacy, marks_only, fixed, est_spread)")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--only", action="append", choices=sorted(RUN_SPECS))
    r.add_argument("--skip-done", action="store_true", help="Skip runs with a report.md")
    w = sub.add_parser("walks", help="Walk-only counterfactuals on one fixed build")
    w.add_argument("--workers", type=int, default=4)
    w.add_argument("--only", action="append", choices=list(WALK_ONLY))
    rep = sub.add_parser("report")
    rep.add_argument("--out", type=Path, default=REPO / "docs" / "RESEARCH")
    args = ap.parse_args(argv)
    if args.cmd == "runs":
        rc = 0
        default = [n for n in RUN_SPECS if n not in WALK_ONLY]
        for name in args.only or default:
            if args.skip_done and (RUNS / name / "report.md").exists():
                continue
            rc |= _run(name, args.workers)
        return rc
    if args.cmd == "walks":
        return _walks(args.only or list(WALK_ONLY), args.workers)
    from scripts.loss_attribution_report import write_report

    write_report(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
