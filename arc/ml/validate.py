"""The ML validation harness (E27.3, D126): purged walk-forward folds, sample weights,
probabilistic / ranking metrics and the All / Stocks / ETFs × kind × sleeve tables.

Reused by every model family (M2 credit/debit, E27.6's DL challengers, E27.7 ML-<n>
studies). Pure numpy / pandas: no LightGBM or scikit-learn import, so the harness and
its tests run without the ``ml`` extra.

Protocol (pre-registered; a change here is a protocol change):

* **Folds**: expanding train windows, test years 2022 and 2023 (:data:`TEST_YEARS`).
  A row is a training row of the fold whose test block starts at session ``b`` iff its
  label window ends strictly before the **embargo start** ``sessions[idx(b) − 10]``:
  the purge (``label_end_date ≥`` test start) plus a 10-session embargo
  (:data:`EMBARGO`). Rows after the test block never train an earlier year.
* **Inner split** (early stopping / grid selection): the last
  :data:`INNER_FRACTION` of the train window's sessions is the validation block, and
  the inner train rows are purged + embargoed against it the same way. Never shuffled.
* **Weights**: ``1 / (1 + n_concurrent)`` (label uniqueness, López de Prado ch. 4;
  the ledger's ``n_concurrent`` excludes the row itself) × the ledger's
  ``sample_weight`` (inverse sampling probability) × the subsample's inverse keep
  fraction, then normalised so every session sums to the same total (each day counts
  equally) and the mean weight is 1.
* **Ranking IC**: per-day Spearman of the score vs realised ``r_net`` on days with at
  least :data:`MIN_DAY_ROWS` rows; mean and Newey-West t with :data:`NW_LAGS` lags
  (labels of neighbouring days overlap).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

DEV_START = dt.date(2020, 1, 1)
DEV_END = dt.date(2023, 12, 31)
TEST_YEARS: tuple[int, ...] = (2022, 2023)
EMBARGO = 10
INNER_FRACTION = 0.2
NW_LAGS = 10
MIN_DAY_ROWS = 20
ECE_BINS = 10
EPS = 1e-6


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------


def _idx(sessions: Sequence[dt.date], d: dt.date) -> int:
    """Index of the first session ≥ *d* (``len(sessions)`` past the end)."""
    lo, hi = 0, len(sessions)
    while lo < hi:
        mid = (lo + hi) // 2
        if sessions[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    return lo


def embargo_start(
    sessions: Sequence[dt.date], block_start: dt.date, embargo: int = EMBARGO
) -> dt.date:
    """The first date a training label may not reach for a block starting *block_start*."""
    i = _idx(sessions, block_start) - embargo
    if i <= 0:
        return sessions[0] if sessions else block_start
    return sessions[i]


def purged_before(
    dates: pd.Series,
    label_end: pd.Series,
    sessions: Sequence[dt.date],
    block_start: dt.date,
    *,
    lo: dt.date | None = None,
    embargo: int = EMBARGO,
) -> np.ndarray:
    """Rows usable to train a model tested on the block starting *block_start*.

    True iff the row's decision date is ≥ *lo* (when given), before the block, and its
    label window ``[date, label_end]`` ends strictly before :func:`embargo_start`.
    """
    cut = np.datetime64(embargo_start(sessions, block_start, embargo), "D")
    d = pd.to_datetime(dates).to_numpy("datetime64[D]")
    e = pd.to_datetime(label_end).to_numpy("datetime64[D]")
    ok = (d < np.datetime64(block_start, "D")) & (e < cut)
    if lo is not None:
        ok &= d >= np.datetime64(lo, "D")
    return ok


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: train on the purged rows before ``test_start``."""

    name: str
    test_start: dt.date
    test_end: dt.date

    def train_mask(
        self, df: pd.DataFrame, sessions: Sequence[dt.date], *, lo: dt.date | None = None
    ) -> np.ndarray:
        return purged_before(df["date"], df["label_end_date"], sessions, self.test_start, lo=lo)

    def test_mask(self, df: pd.DataFrame) -> np.ndarray:
        d = pd.to_datetime(df["date"]).dt.date
        return ((d >= self.test_start) & (d <= self.test_end)).to_numpy()


def walk_forward(years: Sequence[int] = TEST_YEARS) -> tuple[Fold, ...]:
    return tuple(Fold(f"test{y}", dt.date(y, 1, 1), dt.date(y, 12, 31)) for y in years)


def inner_split(
    df: pd.DataFrame,
    train: np.ndarray,
    sessions: Sequence[dt.date],
    frac: float = INNER_FRACTION,
    *,
    val_start: dt.date | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """(inner train, inner validation) masks inside *train*.

    The validation block is the train rows dated ≥ *val_start* (M2: the last train
    year) or, when it is None, the last *frac* of the train window's sessions. Inner
    train rows are purged + embargoed against that block exactly like a fold.
    """
    d = pd.to_datetime(df["date"]).dt.date
    if val_start is None:
        days = sorted(set(d[train]))
        if len(days) < 2:  # noqa: PLR2004
            return train.copy(), np.zeros(len(df), dtype=bool)
        val_start = days[max(int(len(days) * (1.0 - frac)), 1)]
    val = train & (d >= val_start).to_numpy()
    fit = train & purged_before(df["date"], df["label_end_date"], sessions, val_start)
    return fit, val


# ---------------------------------------------------------------------------
# Sampling and weights
# ---------------------------------------------------------------------------


def row_uniform(*cols: pd.Series) -> np.ndarray:
    """A deterministic U[0, 1) per row from a sha256 of the given identity columns."""
    joined = cols[0].astype(str)
    for c in cols[1:]:
        joined = joined + "|" + c.astype(str)
    out = np.empty(len(joined), dtype=np.float64)
    for i, s in enumerate(joined.to_numpy()):
        h = hashlib.sha256(s.encode()).digest()
        out[i] = int.from_bytes(h[:8], "big") / 2**64
    return out


def stratified_day_sample(
    df: pd.DataFrame,
    cap_per_day: int | None,
    u: np.ndarray,
    strata: Sequence[str] = ("kind", "sleeve"),
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic per-day stratified subsample: (keep mask, inverse keep fraction).

    A day with n > *cap_per_day* rows keeps the fraction f = cap / n of every
    ``(day, *strata)`` stratum (at least one row each; the lowest-*u* rows), so the
    day's kind × sleeve mix is preserved. ``None`` keeps everything (weight 1).
    """
    n = len(df)
    if cap_per_day is None or n == 0:
        return np.ones(n, dtype=bool), np.ones(n)
    day = pd.to_datetime(df["date"]).dt.date
    per_day = day.map(day.value_counts())
    frac = np.minimum(1.0, cap_per_day / per_day.to_numpy(dtype=float))
    keys = [day, *(df[s] for s in strata)]
    g = pd.DataFrame({"u": u, "f": frac}).assign(
        **{f"k{i}": k.to_numpy() for i, k in enumerate(keys)}
    )
    kcols = [f"k{i}" for i in range(len(keys))]
    rank = g.groupby(kcols, sort=False)["u"].rank(method="first").to_numpy()
    size = g.groupby(kcols, sort=False)["u"].transform("size").to_numpy()
    keep_n = np.maximum(1, np.round(size * frac)).astype(int)
    keep = rank <= keep_n
    inv = size / keep_n
    return keep, np.where(keep, inv, 0.0)


def sample_weights(df: pd.DataFrame, inv_keep: np.ndarray | None = None) -> np.ndarray:
    """Uniqueness × inverse sampling × per-day normaliser (mean 1); see the module doc."""
    n = len(df)
    if n == 0:
        return np.zeros(0)
    nc = df["n_concurrent"].to_numpy(dtype=float) if "n_concurrent" in df else np.zeros(n)
    w = 1.0 / (1.0 + np.nan_to_num(np.maximum(nc, 0.0)))
    if "sample_weight" in df:
        w = w * df["sample_weight"].to_numpy(dtype=float)
    if inv_keep is not None:
        w = w * inv_keep
    day = pd.to_datetime(df["date"]).dt.date.to_numpy()
    tot = pd.Series(w).groupby(day).transform("sum").to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(tot > 0, w / tot, 0.0)
    m = w.mean()
    return w / m if m > 0 else w


# ---------------------------------------------------------------------------
# Probabilistic metrics
# ---------------------------------------------------------------------------


def _w(w: np.ndarray | None, n: int) -> np.ndarray:
    return np.ones(n) if w is None else np.asarray(w, dtype=float)


def log_loss(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> float:
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), EPS, 1.0 - EPS)
    ww = _w(w, len(y))
    ll = -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
    return float(np.sum(ww * ll) / np.sum(ww)) if ww.sum() > 0 else float("nan")


def brier(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> float:
    y = np.asarray(y, dtype=float)
    ww = _w(w, len(y))
    return float(np.sum(ww * (np.asarray(p, dtype=float) - y) ** 2) / np.sum(ww))


def reliability_table(
    y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None, bins: int = ECE_BINS
) -> pd.DataFrame:
    """Equal-width bins of *p*: weight share, mean forecast, observed rate."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ww = _w(w, len(y))
    b = np.minimum((p * bins).astype(int), bins - 1)
    rows = []
    tot = ww.sum()
    for k in range(bins):
        m = b == k
        wk = ww[m].sum()
        if wk <= 0:
            continue
        rows.append(
            {
                "bin": f"{k / bins:.1f}-{(k + 1) / bins:.1f}",
                "n": int(m.sum()),
                "weight": round(float(wk / tot), 6),
                "mean_p": float(np.sum(ww[m] * p[m]) / wk),
                "observed": float(np.sum(ww[m] * y[m]) / wk),
            }
        )
    return pd.DataFrame(rows, columns=["bin", "n", "weight", "mean_p", "observed"])


def ece(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None, bins: int = ECE_BINS) -> float:
    t = reliability_table(y, p, w, bins)
    if t.empty:
        return float("nan")
    return float(np.sum(t["weight"] * np.abs(t["mean_p"] - t["observed"])))


def brier_decomposition(
    y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None, bins: int = ECE_BINS
) -> dict[str, float]:
    """Murphy: Brier ≈ reliability − resolution + uncertainty (binned)."""
    y = np.asarray(y, dtype=float)
    ww = _w(w, len(y))
    base = float(np.sum(ww * y) / np.sum(ww))
    t = reliability_table(y, p, w, bins)
    rel = float(np.sum(t["weight"] * (t["mean_p"] - t["observed"]) ** 2))
    res = float(np.sum(t["weight"] * (t["observed"] - base) ** 2))
    return {"reliability": rel, "resolution": res, "uncertainty": base * (1.0 - base)}


def auc(y: np.ndarray, s: np.ndarray) -> float:
    """Unweighted ROC AUC via the Mann-Whitney rank sum (ties averaged)."""
    y = np.asarray(y, dtype=bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = pd.Series(np.asarray(s, dtype=float)).rank(method="average").to_numpy()
    return float((r[y].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def prob_metrics(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> dict[str, float]:
    dec = brier_decomposition(y, p, w)
    return {
        "n": float(len(y)),
        "base_rate": float(np.average(np.asarray(y, dtype=float), weights=_w(w, len(y)))),
        "log_loss": log_loss(y, p, w),
        "brier": brier(y, p, w),
        "brier_reliability": dec["reliability"],
        "brier_resolution": dec["resolution"],
        "ece": ece(y, p, w),
        "auc": auc(y, p),
    }


# ---------------------------------------------------------------------------
# Ranking metrics
# ---------------------------------------------------------------------------


def newey_west_t(x: np.ndarray, lags: int = NW_LAGS) -> float:
    """t-stat of mean(x) = 0 with a Bartlett-kernel Newey-West variance."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:  # noqa: PLR2004
        return float("nan")
    d = x - x.mean()
    v = float(d @ d) / n
    for k in range(1, min(lags, n - 1) + 1):
        v += 2.0 * (1.0 - k / (lags + 1.0)) * float(d[k:] @ d[:-k]) / n
    if v <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(v / n))


def daily_ic(
    dates: Iterable[object], score: np.ndarray, y: np.ndarray, min_n: int = MIN_DAY_ROWS
) -> pd.Series:
    """Per-day Spearman correlation of *score* and *y* (days with ≥ *min_n* rows)."""
    df = pd.DataFrame({"d": list(dates), "s": score, "y": y}).dropna()
    df = df[df.groupby("d")["s"].transform("size") >= min_n]
    if df.empty:
        return pd.Series(dtype=float)
    df["rs"] = df.groupby("d")["s"].rank()
    df["ry"] = df.groupby("d")["y"].rank()
    out = df.groupby("d", sort=True).apply(lambda g: g["rs"].corr(g["ry"]), include_groups=False)
    return out[np.isfinite(out)]


def decile_spread(
    dates: Iterable[object], score: np.ndarray, y: np.ndarray, min_n: int = MIN_DAY_ROWS
) -> float:
    """Mean over days of (top-decile mean y − bottom-decile mean y) within the day."""
    df = pd.DataFrame({"d": list(dates), "s": score, "y": y}).dropna()
    df = df[df.groupby("d")["s"].transform("size") >= min_n]
    if df.empty:
        return float("nan")
    pct = df.groupby("d")["s"].rank(pct=True, method="first")
    top = df[pct > 0.9].groupby("d")["y"].mean()
    bot = df[pct <= 0.1].groupby("d")["y"].mean()
    j = (top - bot).dropna()
    return float(j.mean()) if len(j) else float("nan")


def rank_metrics(dates: Sequence[object], score: np.ndarray, y: np.ndarray) -> dict[str, float]:
    ic = daily_ic(dates, score, y)
    return {
        "ic_mean": float(ic.mean()) if len(ic) else float("nan"),
        "ic_nw_t": newey_west_t(ic.to_numpy()) if len(ic) else float("nan"),
        "ic_days": float(len(ic)),
        "decile_spread": decile_spread(dates, score, y),
    }


# ---------------------------------------------------------------------------
# Split tables
# ---------------------------------------------------------------------------


def split_masks(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """All / Stocks / ETFs (owner rule): ``ctx_etf`` from the ledger."""
    etf = (
        df["ctx_etf"].fillna(False).to_numpy(dtype=bool)
        if "ctx_etf" in df
        else np.zeros(len(df), bool)
    )
    return {"All": np.ones(len(df), dtype=bool), "Stocks": ~etf, "ETFs": etf}


def group_slices(
    df: pd.DataFrame, by: Sequence[str] = ("kind", "sleeve")
) -> Iterable[tuple[dict[str, str], np.ndarray]]:
    """(labels, mask) for every split × (all, each value of each *by* column)."""
    for split, m in split_masks(df).items():
        yield {"split": split, "group": "all", "value": "all"}, m
        for col in by:
            if col not in df:
                continue
            vals = df[col].astype(str).to_numpy()
            for v in sorted(set(vals[m])):
                yield {"split": split, "group": col, "value": v}, m & (vals == v)
