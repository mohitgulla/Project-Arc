"""E7.21: ``docs/RESEARCH/candidate-ledger.md`` — aggregates of the ledger only.

* label base rates by profile × sleeve × kind × anchor-Δ bucket × year;
* the rank IC (Spearman per (day, profile, sleeve) menu, averaged) of each current
  ranker key against ``r_net`` — the baseline an ML model must beat;
* the cost share ``r_at_mid − r_net`` per x.

Dev rows only (D84); the caller passes what :func:`arc.ml.ledger.build.load_ledger`
returned with the default ``split="dev"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The ranker keys stored on every row (the live/baseline rankers' sort keys).
RANK_KEYS: tuple[str, ...] = (
    "managed_net_ev",
    "credit_width",
    "debit_width",
    "ev_proxy",
    "rorc_day",
    "static_net_ev",
)
DELTA_BUCKETS = (0.0, 0.15, 0.25, 0.35, 0.5, 0.7, 1.01)


def delta_bucket(s: pd.Series) -> pd.Series:
    labels = [f"{a:.2f}-{b:.2f}" for a, b in zip(DELTA_BUCKETS, DELTA_BUCKETS[1:], strict=False)]
    return pd.cut(s.astype(float), DELTA_BUCKETS, labels=labels, right=False).astype(str)


def _wmean(v: pd.Series, w: pd.Series) -> float:
    m = v.notna()
    if not m.any():
        return float("nan")
    return float(np.average(v[m].astype(float), weights=w[m].astype(float)))


def base_rates(df: pd.DataFrame, x: str = "x025") -> pd.DataFrame:
    """Weighted label base rates per profile × sleeve × kind × Δ bucket × year."""
    d = df.assign(
        year=pd.to_datetime(df["date"]).dt.year,
        dbucket=delta_bucket(df["anchor_delta"]),
        w=df.get("sample_weight", pd.Series(1, index=df.index)).astype(float),
    )
    rows = []
    keys = ["profile", "sleeve", "kind", "dbucket", "year"]
    for k, g in d.groupby(keys, sort=True):
        rows.append(
            {
                **dict(zip(keys, k, strict=True)),
                "n": len(g),
                "win": _wmean(g[f"label_win_{x}"].astype(float), g["w"]),
                "r_net": _wmean(g[f"label_r_net_{x}"], g["w"]),
                "stop": _wmean(g["label_hit_stop"].astype(float), g["w"]),
                "tp": _wmean(g["label_hit_tp"].astype(float), g["w"]),
                "days": _wmean(g["label_days_held"].astype(float), g["w"]),
            }
        )
    return pd.DataFrame(rows)


def rank_ic(
    df: pd.DataFrame, keys: Sequence[str] = RANK_KEYS, x: str = "x025", min_n: int = 5
) -> pd.DataFrame:
    """Mean per-menu Spearman of each key vs ``r_net`` (menus = date × ticker-free
    profile × sleeve, i.e. the cross-name pool the ranker sorts), with its t-stat."""
    target = f"label_r_net_{x}"
    out = []
    for (p, s), g in df.groupby(["profile", "sleeve"], sort=True):
        for k in keys:
            if k not in g:
                continue
            ics = []
            for _, day in g.groupby("date", sort=False):
                sub = day[[k, target]].dropna()
                if len(sub) < min_n or sub[k].nunique() < 2:
                    continue
                ic = sub[k].rank().corr(sub[target].rank())
                if np.isfinite(ic):
                    ics.append(ic)
            a = np.asarray(ics, dtype=float)
            sd = a.std(ddof=1) if len(a) > 2 else 0.0
            t = a.mean() / (sd / np.sqrt(len(a))) if sd > 0 else float("nan")
            out.append(
                {
                    "profile": p,
                    "sleeve": s,
                    "key": k,
                    "days": len(a),
                    "ic": a.mean() if len(a) else float("nan"),
                    "t": t,
                }
            )
    return pd.DataFrame(out)


def cost_share(df: pd.DataFrame, xs: Sequence[str]) -> pd.DataFrame:
    """Mean ``r_at_mid − r_net`` per profile × kind × x (what fills cost in R)."""
    rows = []
    for (p, k), g in df.groupby(["profile", "kind"], sort=True):
        r = {"profile": p, "kind": k, "n": len(g), "r_at_mid": g["label_r_at_mid"].mean()}
        for x in xs:
            r[f"cost_{x}"] = (g["label_r_at_mid"] - g[f"label_r_net_{x}"]).mean()
        rows.append(r)
    return pd.DataFrame(rows)


def _md(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    if df.empty:
        return "_(none)_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        cells = []
        for v in r:
            if isinstance(v, float):
                cells.append("" if np.isnan(v) else format(v, floatfmt))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render_report(df: pd.DataFrame, manifest: Mapping[str, object]) -> str:
    xs = sorted(c.removeprefix("label_r_net_") for c in df.columns if c.startswith("label_r_net_"))
    br = base_rates(df)
    ic = rank_ic(df)
    cs = cost_share(df, xs)
    years = sorted(pd.to_datetime(df["date"]).dt.year.unique().tolist())
    parts = [
        "# Candidate outcome ledger (E7.21, D125/D126)\n",
        "Aggregates only, **dev rows** (D84: holdout from 2024-01-01 is never read here). "
        "One row per menu candidate × session × profile × sleeve, labelled under the live "
        "exits of its sleeve on quoted marks (`arc ml ledger build`).\n",
        f"- rows: {len(df):,} · years {years} · universe {manifest.get('universe')} "
        f"(version {manifest.get('universe_version')})",
        f"- code {str(manifest.get('git_sha'))[:12]} · feature schema "
        f"v{manifest.get('feature_schema')} · sampling {manifest.get('sampling')}",
        f"- x grid {manifest.get('xs')} (fills at mid ± x·spread)\n",
        "## Rank IC vs r_net (x = 0.25)\n",
        "Spearman of each stored ranker key against the candidate's realised `r_net`, per "
        "(session, profile, sleeve) pool, averaged over sessions; `t` = mean / s.e. "
        "This is the baseline an ML ranker must beat.\n",
        _md(ic),
        "## Cost share (r_at_mid − r_net)\n",
        "How much of each candidate's R is spent on fills, per slippage x.\n",
        _md(cs),
        "## Label base rates (x = 0.25) by profile × sleeve × kind × anchor Δ × year\n",
        _md(br),
    ]
    return "\n".join(parts)
