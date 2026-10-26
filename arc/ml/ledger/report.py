"""E7.21: ``docs/RESEARCH/candidate-ledger.md`` — aggregates of the ledger only.

* label base rates by profile × sleeve × kind × anchor-Δ bucket × year;
* the rank IC (Spearman per (day, profile, sleeve) menu, averaged) of each current
  ranker key against ``r_net`` — the baseline an ML model must beat;
* the cost share ``r_at_mid − r_net`` per x;
* the summaries split All / Stocks / ETFs (owner rule; ``ctx_etf``).

Dev rows only (D84). The C500 ledger (~56 M rows) does not fit in memory at once, so
:class:`ReportAccumulator` takes one ``(profile, year)`` partition at a time
(:func:`arc.ml.ledger.build.iter_ledger`) and keeps only sums and per-day ICs. Every
aggregate is keyed by profile and year (a rank-IC pool is one profile × sleeve × day)
or is a sum, so partition-wise accumulation equals one pass over all rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

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
CLASSES: tuple[str, ...] = ("All", "Stocks", "ETFs")

#: Columns the report reads (everything else stays on disk).
BASE_COLUMNS: tuple[str, ...] = (
    "date",
    "profile",
    "sleeve",
    "kind",
    "anchor_delta",
    "sample_weight",
    "ctx_etf",
    "ctx_in_topk",
    "label_hit_stop",
    "label_hit_tp",
    "label_days_held",
    "label_r_at_mid",
    "label_exit_reason",
)


def report_columns(xs: Sequence[str]) -> list[str]:
    """The ledger columns :class:`ReportAccumulator` reads for slippage tags *xs*."""
    cols = [*BASE_COLUMNS, *RANK_KEYS]
    for x in xs:
        cols += [f"label_r_net_{x}", f"label_win_{x}"]
    return cols


def delta_bucket(s: pd.Series) -> pd.Series:
    labels = [f"{a:.2f}-{b:.2f}" for a, b in zip(DELTA_BUCKETS, DELTA_BUCKETS[1:], strict=False)]
    return pd.cut(s.astype(float), DELTA_BUCKETS, labels=labels, right=False).astype(str)


def _weights(df: pd.DataFrame) -> pd.Series:
    if "sample_weight" in df:
        return df["sample_weight"].astype(float)
    return pd.Series(1.0, index=df.index)


def _by_class(df: pd.DataFrame) -> Iterable[tuple[str, pd.DataFrame]]:
    yield "All", df
    if "ctx_etf" not in df:
        return
    etf = df["ctx_etf"].astype("boolean").fillna(False).astype(bool)
    yield "Stocks", df[~etf]
    yield "ETFs", df[etf]


def _wsums(df: pd.DataFrame, keys: list[str], vals: Mapping[str, str]) -> pd.DataFrame:
    """Per group: row count and, per value, Σw·v and Σw over its non-null rows."""
    w = _weights(df)
    d = pd.DataFrame({k: df[k] for k in keys})
    d["n"] = 1
    for name, col in vals.items():
        v = df[col].astype(float)
        m = v.notna()
        d[f"{name}__wv"] = (v * w).where(m, 0.0)
        d[f"{name}__w"] = w.where(m, 0.0)
    return d.groupby(keys, sort=True, observed=True).sum(numeric_only=True).reset_index()


def _finish_wsums(s: pd.DataFrame, keys: list[str], names: Sequence[str]) -> pd.DataFrame:
    g = s.groupby(keys, sort=True, observed=True).sum(numeric_only=True).reset_index()
    out = g[[*keys, "n"]].copy()
    for name in names:
        den = g[f"{name}__w"].replace(0.0, np.nan)
        out[name] = g[f"{name}__wv"] / den
    return out


_BASE_VALS = {
    "win": "label_win_{x}",
    "r_net": "label_r_net_{x}",
    "stop": "label_hit_stop",
    "tp": "label_hit_tp",
    "days": "label_days_held",
}
_BASE_KEYS = ["profile", "sleeve", "kind", "dbucket", "year"]


def _base_partial(df: pd.DataFrame, x: str) -> pd.DataFrame:
    d = df.assign(
        year=pd.to_datetime(df["date"]).dt.year,
        dbucket=delta_bucket(df["anchor_delta"]),
    )
    return _wsums(d, _BASE_KEYS, {k: v.format(x=x) for k, v in _BASE_VALS.items()})


def base_rates(df: pd.DataFrame, x: str = "x025") -> pd.DataFrame:
    """Weighted label base rates per profile × sleeve × kind × Δ bucket × year."""
    return _finish_wsums(_base_partial(df, x), _BASE_KEYS, list(_BASE_VALS))


def daily_ic(
    df: pd.DataFrame,
    keys: Sequence[str] = RANK_KEYS,
    x: str = "x025",
    min_n: int = 5,
    group: Sequence[str] = ("profile", "sleeve"),
) -> pd.DataFrame:
    """One Spearman per (group…, date, key), vectorised: average ranks within the
    day's pool, then the Pearson of the ranks (equal to ``Series.rank().corr``).
    Pools with fewer than *min_n* rows or a constant key or label are skipped."""
    target = f"label_r_net_{x}"
    gk = [*group, "date"]
    out = []
    for k in keys:
        if k not in df or target not in df:
            continue
        sub = df[[*gk, k, target]].dropna(subset=[k, target])
        if sub.empty:
            continue
        grp = sub.groupby(gk, sort=False, observed=True)
        a = grp[k].rank().to_numpy(float)
        b = grp[target].rank().to_numpy(float)
        t = sub[gk].copy()
        t["n"] = 1.0
        t["a"], t["b"] = a, b
        t["aa"], t["bb"], t["ab"] = a * a, b * b, a * b
        s = t.groupby(gk, sort=True, observed=True).sum(numeric_only=True)
        n = s["n"]
        cov = s["ab"] - s["a"] * s["b"] / n
        va = s["aa"] - s["a"] ** 2 / n
        vb = s["bb"] - s["b"] ** 2 / n
        ok = (n >= min_n) & (va > 1e-9) & (vb > 1e-9)
        ic = (cov[ok] / np.sqrt(va[ok] * vb[ok])).rename("ic").reset_index()
        ic = ic[np.isfinite(ic["ic"])]
        ic["key"] = k
        out.append(ic)
    if not out:
        return pd.DataFrame(columns=[*gk, "ic", "key"])
    return pd.concat(out, ignore_index=True)


def summarize_ic(
    daily: pd.DataFrame,
    keys: Sequence[str] = RANK_KEYS,
    group: Sequence[str] = ("profile", "sleeve"),
    groups: Iterable[tuple[object, ...]] | None = None,
) -> pd.DataFrame:
    """Mean of the per-day ICs with its t-stat (mean / s.e.); every key appears for
    every group, with ``days = 0`` where it never ranked a pool."""
    group = list(group)
    if groups is None:
        groups = [tuple(g) for g in daily[group].drop_duplicates().itertuples(index=False)]
    by = {
        tuple(g): v.to_numpy(float) for g, v in daily.groupby([*group, "key"], observed=True)["ic"]
    }
    rows = []
    for g in sorted(set(groups)):
        for k in keys:
            a = by.get((*g, k), np.empty(0))
            sd = a.std(ddof=1) if len(a) > 2 else 0.0
            t = a.mean() / (sd / np.sqrt(len(a))) if sd > 0 else float("nan")
            rows.append(
                {
                    **dict(zip(group, g, strict=True)),
                    "key": k,
                    "days": len(a),
                    "ic": a.mean() if len(a) else float("nan"),
                    "t": t,
                }
            )
    return pd.DataFrame(rows)


def rank_ic(
    df: pd.DataFrame, keys: Sequence[str] = RANK_KEYS, x: str = "x025", min_n: int = 5
) -> pd.DataFrame:
    """Mean per-menu Spearman of each key vs ``r_net`` (menus = date × ticker-free
    profile × sleeve, i.e. the cross-name pool the ranker sorts), with its t-stat."""
    groups = [tuple(g) for g in df[["profile", "sleeve"]].drop_duplicates().itertuples(index=False)]
    return summarize_ic(daily_ic(df, keys, x, min_n), keys, groups=groups)


def _cost_partial(df: pd.DataFrame, xs: Sequence[str], keys: list[str]) -> pd.DataFrame:
    d = pd.DataFrame({k: df[k] for k in keys})
    mid = df["label_r_at_mid"].astype(float)
    d["n"] = 1
    d["r_at_mid__s"] = mid
    for x in xs:
        d[f"cost_{x}__s"] = mid - df[f"label_r_net_{x}"].astype(float)
    return d.groupby(keys, sort=True, observed=True).sum(numeric_only=True).reset_index()


def _finish_cost(s: pd.DataFrame, xs: Sequence[str], keys: list[str]) -> pd.DataFrame:
    g = s.groupby(keys, sort=True, observed=True).sum(numeric_only=True).reset_index()
    out = g[[*keys, "n"]].copy()
    out["r_at_mid"] = g["r_at_mid__s"] / g["n"]
    for x in xs:
        out[f"cost_{x}"] = g[f"cost_{x}__s"] / g["n"]
    return out


def cost_share(df: pd.DataFrame, xs: Sequence[str]) -> pd.DataFrame:
    """Mean ``r_at_mid − r_net`` per profile × kind × x (what fills cost in R)."""
    keys = ["profile", "kind"]
    return _finish_cost(_cost_partial(df, xs, keys), xs, keys)


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


def _xs_of(columns: Iterable[str]) -> list[str]:
    return sorted(c.removeprefix("label_r_net_") for c in columns if c.startswith("label_r_net_"))


def _order(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    df = df.copy()
    df["cls"] = pd.Categorical(df["cls"], CLASSES, ordered=True)
    return df.sort_values(keys, kind="stable").reset_index(drop=True)


class ReportAccumulator:
    """Partition-wise aggregates behind :func:`render_report` (bounded memory)."""

    def __init__(self, xs: Sequence[str] | None = None) -> None:
        self.xs = list(xs) if xs is not None else None
        self.rows = 0
        self.years: set[int] = set()
        self._base: list[pd.DataFrame] = []
        self._ic: list[pd.DataFrame] = []
        self._ic_groups: set[tuple[object, ...]] = set()
        self._cost: list[pd.DataFrame] = []
        self._head: list[pd.DataFrame] = []
        self._kind_year: list[pd.DataFrame] = []
        self._exit: list[pd.DataFrame] = []

    def add(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        if self.xs is None:
            self.xs = _xs_of(df.columns)
        xs = self.xs
        self.rows += len(df)
        df = df.assign(year=pd.to_datetime(df["date"]).dt.year)
        self.years.update(int(y) for y in df["year"].unique())
        self._base.append(_base_partial(df, "x025"))
        head_vals: dict[str, str] = {"r_at_mid": "label_r_at_mid"}
        for x in xs:
            head_vals[f"r_{x}"] = f"label_r_net_{x}"
        for x in xs:
            head_vals[f"win_{x}"] = f"label_win_{x}"
        if "ctx_in_topk" in df:
            head_vals["topk_share"] = "ctx_in_topk"
        for cls, part in _by_class(df):
            if part.empty:
                continue
            p = part.assign(cls=cls)
            self._ic.append(daily_ic(p, group=("cls", "profile", "sleeve")))
            self._ic_groups.update(
                tuple(g)
                for g in p[["cls", "profile", "sleeve"]].drop_duplicates().itertuples(index=False)
            )
            self._cost.append(_cost_partial(p, xs, ["cls", "profile", "kind"]))
            self._head.append(_wsums(p, ["cls", "profile"], head_vals))
            self._kind_year.append(
                _wsums(
                    p,
                    ["cls", "profile", "kind", "year"],
                    {"r_net": "label_r_net_x025", "win": "label_win_x025"},
                )
            )
            if "label_exit_reason" in p and cls == "All":
                ex = p.groupby(["profile", "label_exit_reason"], observed=True).size()
                self._exit.append(ex.rename("n").reset_index())

    def tables(self) -> dict[str, pd.DataFrame]:
        xs = self.xs or []
        empty = pd.DataFrame()
        out: dict[str, pd.DataFrame] = {}
        out["base"] = (
            _finish_wsums(pd.concat(self._base), _BASE_KEYS, list(_BASE_VALS))
            if self._base
            else empty
        )
        if self._ic:
            daily = pd.concat(self._ic, ignore_index=True)
            ic = summarize_ic(daily, group=("cls", "profile", "sleeve"), groups=self._ic_groups)
            out["ic"] = _order(ic, ["cls", "profile", "sleeve"])
        else:
            out["ic"] = empty
        head_names = [
            "r_at_mid",
            *[f"r_{x}" for x in xs],
            *[f"win_{x}" for x in xs],
        ]
        if any("topk_share__w" in h for h in self._head):
            head_names.append("topk_share")
        for name, parts, keys, names in (
            ("head", self._head, ["cls", "profile"], head_names),
            ("kind_year", self._kind_year, ["cls", "profile", "kind", "year"], ["r_net", "win"]),
        ):
            out[name] = (
                _order(_finish_wsums(pd.concat(parts), keys, names), keys) if parts else empty
            )
        keys = ["cls", "profile", "kind"]
        out["cost"] = (
            _order(_finish_cost(pd.concat(self._cost), xs, keys), keys) if self._cost else empty
        )
        if self._exit:
            e = (
                pd.concat(self._exit)
                .groupby(["profile", "label_exit_reason"], observed=True)["n"]
                .sum()
                .reset_index()
            )
            e["share"] = e["n"] / e.groupby("profile")["n"].transform("sum")
            out["exit"] = e.sort_values(["profile", "n"], ascending=[True, False]).reset_index(
                drop=True
            )
        else:
            out["exit"] = empty
        return out

    def render(self, manifest: Mapping[str, object]) -> str:
        t = self.tables()
        kind_year = t["kind_year"]
        if not kind_year.empty:
            kind_year = kind_year[kind_year["cls"] == "All"].drop(columns="cls")
        parts = [
            "# Candidate outcome ledger (E7.21, D125/D126)\n",
            "Aggregates only, **dev rows** (D84: holdout from 2024-01-01 is never read here). "
            "One row per menu candidate × session × profile × sleeve, labelled under the live "
            "exits of its sleeve on quoted marks (`arc ml ledger build`). Class splits: "
            "All / Stocks / ETFs (`ctx_etf`).\n",
            f"- dev rows: {self.rows:,} · years {sorted(self.years)} · universe "
            f"{manifest.get('universe')} (version {manifest.get('universe_version')})",
            f"- code {str(manifest.get('git_sha'))[:12]} · feature schema "
            f"v{manifest.get('feature_schema')} · sampling {manifest.get('sampling')}",
            f"- x grid {manifest.get('xs')} (fills at mid ± x·spread)\n",
            "## Headline by class × profile (weighted means)\n",
            "`r_<x>` = mean `r_net` at slippage x; `win_<x>` = share with `r_net > 0`; "
            "`topk_share` = share of rows in some ranker's top-k pool.\n",
            _md(t["head"]),
            "## Rank IC vs r_net (x = 0.25)\n",
            "Spearman of each stored ranker key against the candidate's realised `r_net`, per "
            "(session, profile, sleeve) pool, averaged over sessions; `t` = mean / s.e. "
            "This is the baseline an ML ranker must beat.\n",
            _md(t["ic"]),
            "## Cost share (r_at_mid − r_net)\n",
            "How much of each candidate's R is spent on fills, per slippage x.\n",
            _md(t["cost"]),
            "## Mean r_net and win rate (x = 0.25) by profile × kind × year (All)\n",
            _md(kind_year),
            "## Exit mix (All)\n",
            _md(t["exit"]),
            "## Label base rates (x = 0.25) by profile × sleeve × kind × anchor Δ × year\n",
            _md(t["base"]),
        ]
        return "\n".join(parts)


def render_report(df: pd.DataFrame, manifest: Mapping[str, object]) -> str:
    """The report for an in-memory frame of dev rows (tests, small ledgers)."""
    acc = ReportAccumulator()
    acc.add(df)
    return acc.render(manifest)
