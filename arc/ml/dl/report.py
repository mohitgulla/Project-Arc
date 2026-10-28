"""Render ``docs/RESEARCH/ml-dl-challengers.md`` from an E27.6 run directory."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from pathlib import Path


def _f(x: object, nd: int = 4) -> str:
    if isinstance(x, (bool, np.bool_)):
        return "yes" if x else "no"
    if isinstance(x, (int, np.integer)):
        return str(int(x))
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(x)
    if not np.isfinite(v):
        return "–"
    if v == int(v) and abs(v) >= 1000:  # noqa: PLR2004 (counts)
        return str(int(v))
    return f"{v:.{nd}f}"


def md(df: pd.DataFrame, cols: list[str], nd: int = 4) -> str:
    cols = [c for c in cols if c in df.columns]
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = "".join(
        "| "
        + " | ".join(_f(r[c], nd) if not isinstance(r[c], str) else r[c] for c in cols)
        + " |\n"
        for _, r in df.iterrows()
    )
    return head + body


def _read(run: Path, name: str) -> pd.DataFrame | None:
    p = run / name
    return pd.read_csv(p) if p.is_file() else None


def render(run: Path) -> str:
    man = json.loads((run / "manifest.json").read_text())
    met = _read(run, "eval_metrics.csv")
    parts = [
        "# E27.6 · Deep-learning challengers vs M2 (D126)\n",
        "Research only: nothing here is imported by the live scorer or read by the gate.\n",
        f"Run `{man['run']}` · code `{man['code_sha']}` · feature schema "
        f"`{man['feature_schema']}` · M2 versions {man['m2_versions']} · created "
        f"{man['created']} · holdout reads: {man['holdout_reads']}.\n",
    ]
    dec = _read(run, "eval_decisions.csv")
    if dec is not None:
        parts += ["## Decision (`dl_promote`)\n", md(dec, list(dec.columns)), ""]
    if met is not None:
        m = met[(met.split == "All") & (met.group == "all")]
        parts += [
            "## Test-year metrics (All rows)\n",
            md(
                m,
                [
                    "family",
                    "fold",
                    "model",
                    "n",
                    "win_log_loss",
                    "win_auc",
                    "win_ece",
                    "stop_log_loss",
                    "er_ic_mean",
                    "er_ic_nw_t",
                    "best_dl",
                ],
            ),
            "",
        ]
        for split in ("Stocks", "ETFs"):
            s = met[(met.split == split) & (met.group == "all")]
            if len(s):
                parts += [
                    f"### {split}\n",
                    md(
                        s,
                        [
                            "family",
                            "fold",
                            "model",
                            "n",
                            "win_log_loss",
                            "er_ic_mean",
                            "er_ic_nw_t",
                        ],
                    ),
                    "",
                ]
    rule = _read(run, "eval_rule.csv")
    if rule is not None:
        parts += ["## Rule legs per fold\n", md(rule, list(rule.columns), 3), ""]
    rep = _read(run, "eval_replay.csv")
    if rep is not None:
        parts += [
            "## Decision-value replays\n",
            md(
                rep,
                [
                    c
                    for c in (
                        "family",
                        "year",
                        "model",
                        "use",
                        "trades",
                        "net_pnl",
                        "max_dd_pct",
                        "sharpe",
                        "coverage",
                    )
                    if c in rep.columns
                ],
                2,
            ),
            "",
        ]
    sd = _read(run, "eval_seeds.csv")
    if sd is not None and len(sd):
        g = (
            sd.groupby(["family", "model", "fold"])
            .agg(
                seeds=("seed", "count"),
                ll_mean=("win_log_loss", "mean"),
                ll_sd=("win_log_loss", "std"),
                ic_mean=("er_ic_mean", "mean"),
                ic_sd=("er_ic_mean", "std"),
            )
            .reset_index()
        )
        parts += ["## Seed variance\n", md(g, list(g.columns)), ""]
    m1 = _read(run, "eval_m1_surface.csv")
    if m1 is not None:
        e = m1[m1.seed.astype(str).isin(["ens", "ref"])]
        parts += [
            "## Surface → M1 `move_vs_implied_h21`\n",
            md(e, ["fold", "arm", "split", "n", "ic_mean", "ic_nw_t", "mse", "surface_coverage"]),
            "",
        ]
    cost = _read(run, "eval_cost.csv")
    if cost is not None:
        parts += ["## Compute\n", md(cost, list(cost.columns), 3), ""]
        parts.append(f"Total wall time {man['total_s']:.0f} s.\n")
    return "\n".join(parts)


def write_doc(run: Path, doc: Path, *, extra: Path | None = None) -> Path:
    text = render(run)
    if extra is not None:
        text += "\n" + extra.read_text()
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text)
    return doc
