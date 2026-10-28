"""M1 training, walk-forward evaluation and artefacts (E27.2, D126). Dev data only.

Protocol (pre-registered in this file; changing it changes the artefact version):

* **Dataset**: the E27.1 feature store, sessions 2020-01-02 → 2023-12-29 (dev), all C500
  rows, with the M1 labels from OHLC cut at 2023-12-31 (:mod:`arc.ml.m1.dataset`).
  Training rows start 2020-07-01 (the 252-session features need the warm-up).
* **Folds** (walk-forward by year): test 2022 (train 2020-07 → 2021-12) and test 2023
  (train 2020-07 → 2022-12). For a block starting at session b and horizon h, training
  rows are dated before ``sessions[idx(b) − h − 5]``: a purge of h sessions (the label
  window) plus a 5-session embargo.
* **Models** per (kind ∈ {log_ret, rv}, h, level):
  - LightGBM quantile regression, pooled over tickers (no ticker id), on a
    vol-standardised target (``log_ret / scale`` and ``ln(rv / rv_scale)``, scale = HAR
    → rv20 → IV30), fixed params :data:`LGB_PARAMS` and :data:`NUM_ROUNDS`;
  - baselines: historical quantiles (``log_ret``, IV lognormal where missing), HAR-IV
    (``rv``), and the IV lognormal as a third reference for ``log_ret``.
* **CQR**: per fold, the calibration block is the 252 sessions before the purged test
  cutoff, split in two halves. Each half is predicted by a *calibration model* trained
  on every pre-cutoff row outside that half (purge + embargo on both sides, i.e.
  cross-fitted so the 2020-07 start still leaves enough training rows); the per-level
  offsets of the pooled residuals (:mod:`arc.ml.m1.conformal`) are applied to
  the fold's test model. Baselines are calibrated the same way. The shipped model is
  trained on every dev row; its offsets come from the fold-2023 test residuals (the last
  252 dev sessions, all labels inside dev).
* **Keep rule** per target (kind, h): ship LightGBM iff in **both** test years its
  calibrated pinball loss is below the calibrated baseline's with a one-sided
  Diebold-Mariano p < 0.05 (per-day cross-sectional mean differential, Newey-West
  h − 1 lags) **and** its post-CQR coverage error (max |empirical − nominal| over the 7
  levels, all rows) is ≤ 3 points; else ship the baseline.

Determinism: fixed seeds, ``deterministic=True``, one thread per booster (the 7 levels
run on a ≤ 6-process pool); the same inputs give byte-identical LightGBM text dumps
(sha256 in the manifest). One thread per booster is also the fastest on a shared host:
LightGBM's OpenMP threads spin badly under contention (100k × 90 bench: 1 thread
4.7 s, 6 threads 38.6 s with the host busy).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog

from arc.ml.features.compute import M1_HORIZONS, M1_QUANTILES, qtag
from arc.ml.features.registry import (
    FEATURES,
    default_model_features,
    panel_feature_names,
    schema_version,
)
from arc.ml.m1 import metrics as mt
from arc.ml.m1.baselines import HarIv, iv_lognormal_quantiles
from arc.ml.m1.conformal import apply_offsets, fit_offsets, sort_quantiles
from arc.ml.m1.dataset import DEV_END, scale
from arc.ml.m1.predict import (
    KINDS,
    M1Manifest,
    M1Model,
    TargetModel,
    direction,
    feature_matrix,
    p_up_and_abs,
    publish,
    raw_baseline,
    raw_lgbm,
    rv_scale,
)
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

TRAIN_START = dt.date(2020, 7, 1)
EMBARGO = 5
#: Bump on any change to the evaluation or calibration code (it changes the version):
#: 2 = equal-row evaluation, realised-scale lognormal fallback, final offsets from the
#: last 252 dev sessions of fold-2023 OOS residuals.
PROTOCOL_REV = 2
CAL_SESSIONS = 252
TEST_YEARS: tuple[int, ...] = (2022, 2023)
KEEP_P = 0.05
KEEP_COVERAGE = 0.03
LGB_PARAMS: dict[str, Any] = {
    "objective": "quantile",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 1000,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.5,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "max_bin": 63,
    "seed": 20261017,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
    "verbose": -1,
}
NUM_ROUNDS = 300
#: Process pool for the 7 quantile fits of one target (resource etiquette: ≤ 6).
MAX_WORKERS = 6
_ID = ("date", "ticker", "pit_member", "is_etf")


def model_features() -> list[str]:
    """Live-available panel features (registry order); the M1 input list."""
    panel = set(panel_feature_names())
    return [f for f in default_model_features() if f in panel]


def _lgb() -> Any:
    from arc.ml.m2.train import load_lightgbm

    return load_lightgbm()


def _lgb_from_text(text: str) -> Any:
    return _lgb().Booster(model_str=text)


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------


def dev_sessions(end: dt.date = DEV_END) -> list[dt.date]:
    return sessions_between(dt.date(2020, 1, 1), end)


def _idx(sessions: Sequence[dt.date], d: dt.date) -> int:
    """Index of the first session ≥ *d* (len(sessions) past the end)."""
    return next((k for k, x in enumerate(sessions) if x >= d), len(sessions))


def cutoff(sessions: Sequence[dt.date], block_start: dt.date, h: int) -> dt.date:
    """Rows dated before this are purged-and-embargoed relative to *block_start*."""
    return sessions[max(_idx(sessions, block_start) - h - EMBARGO, 0)]


def session_index(df: pd.DataFrame, sessions: Sequence[dt.date]) -> np.ndarray:
    """Per row, the index of its date in *sessions* (−1 when not a session)."""
    pos = {d: k for k, d in enumerate(sessions)}
    return np.array([pos.get(d, -1) for d in df["date"]], dtype=np.int64)


def purged_outside(
    si: np.ndarray, sessions: Sequence[dt.date], b0: dt.date, b1: dt.date, h: int
) -> np.ndarray:
    """Rows clear of block [b0, b1) on both sides: before it by h + embargo (labels end
    before it starts), after it by h + embargo (labels do not overlap the block's)."""
    i0, i1 = _idx(sessions, b0), _idx(sessions, b1)
    return (si < i0 - h - EMBARGO) | (si >= i1 + h + EMBARGO)


@dataclass(frozen=True)
class FoldPlan:
    """One walk-forward fold. The calibration block is the last :data:`CAL_SESSIONS`
    before the purged test cut, split in two halves that are cross-fitted."""

    name: str
    test_start: dt.date
    test_end: dt.date

    def test_cut(self, s: Sequence[dt.date], h: int) -> dt.date:
        return cutoff(s, self.test_start, h)

    def cal_start(self, s: Sequence[dt.date], h: int) -> dt.date:
        i = s.index(self.test_cut(s, h))
        return s[max(i - CAL_SESSIONS, 0)]

    def cal_halves(self, s: Sequence[dt.date], h: int) -> tuple[tuple[dt.date, dt.date], ...]:
        a, b = s.index(self.cal_start(s, h)), s.index(self.test_cut(s, h))
        m = (a + b) // 2
        return ((s[a], s[m]), (s[m], s[b]))


FOLDS: tuple[FoldPlan, ...] = tuple(
    FoldPlan(f"test{y}", dt.date(y, 1, 1), dt.date(y, 12, 31)) for y in TEST_YEARS
)


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def std_target(df: pd.DataFrame, kind: str, h: int) -> np.ndarray:
    if kind == "log_ret":
        return df[f"log_ret_h{h}"].to_numpy(dtype=float) / scale(df, h)
    rv = df[f"rv_h{h}"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(rv > 0, np.log(rv / rv_scale(df, h)), np.nan)


def target(df: pd.DataFrame, kind: str, h: int) -> np.ndarray:
    return df[f"{kind}_h{h}"].to_numpy(dtype=float)


def rows_for(df: pd.DataFrame, kind: str, h: int, lo: dt.date, hi: dt.date) -> pd.DataFrame:
    """Rows dated in [lo, hi) with a finite target and scale."""
    m = (df["date"] >= lo) & (df["date"] < hi)
    sub = df[m]
    ok = np.isfinite(std_target(sub, kind, h))
    return sub[ok]


@dataclass
class FitSet:
    """Boosters per (kind, h, level) and HAR-IV per h, trained on one row window each."""

    boosters: dict[tuple[str, int, float], Any] = field(default_factory=dict)
    har_iv: dict[int, HarIv] = field(default_factory=dict)
    n_rows: dict[tuple[str, int], int] = field(default_factory=dict)
    seconds: float = 0.0


def _fit_one(args: tuple[np.ndarray, np.ndarray, list[str], dict[str, Any], int]) -> str:
    """One quantile booster (process-pool worker): returns its text dump."""
    x, y, names, prm, rounds = args
    lgb = _lgb()
    ds = lgb.Dataset(x, y, feature_name=names, free_raw_data=True)
    return str(lgb.train(prm, ds, num_boost_round=rounds).model_to_string())


def fit_target(
    tr: pd.DataFrame,
    kind: str,
    h: int,
    features: Sequence[str],
    *,
    levels: Sequence[float] = M1_QUANTILES,
    params: dict[str, Any] | None = None,
    rounds: int = NUM_ROUNDS,
    workers: int = MAX_WORKERS,
) -> dict[float, Any]:
    """One booster per level. Each is single-threaded and deterministic; the levels run
    on up to *workers* (≤ :data:`MAX_WORKERS`) processes (a fixed thread count per
    booster keeps the dumps byte-identical regardless of the pool size)."""
    lgb = _lgb()
    x = feature_matrix(tr, features)
    y = std_target(tr, kind, h)
    base = dict(params or LGB_PARAMS)
    jobs = [(x, y, list(features), {**base, "alpha": float(p)}, rounds) for p in levels]
    workers = min(MAX_WORKERS, max(1, workers), len(jobs))
    if workers <= 1 or len(y) < 50_000:  # noqa: PLR2004 - small fits: no pool overhead
        texts = [_fit_one(j) for j in jobs]
    else:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            texts = list(ex.map(_fit_one, jobs))
    return {float(p): lgb.Booster(model_str=t) for p, t in zip(levels, texts, strict=True)}


def fit_set(
    df: pd.DataFrame,
    train_mask: Callable[[int], np.ndarray],
    features: Sequence[str],
    *,
    horizons: Sequence[int] = M1_HORIZONS,
    params: dict[str, Any] | None = None,
    rounds: int = NUM_ROUNDS,
    lgbm: bool = True,
    progress: Callable[[str], None] | None = None,
    workers: int = MAX_WORKERS,
    lgbm_targets: Collection[tuple[str, int]] | None = None,
) -> FitSet:
    """Train everything on the rows of *train_mask(h)* (dated ≥ TRAIN_START) per horizon.

    E27.7: LightGBM boosters only for the (kind, h) in *lgbm_targets* (a study's target
    subset; None = every target); every target keeps its baseline (HAR-IV, row counts)."""
    fs = FitSet()
    t0 = time.monotonic()
    after_start = (df["date"] >= TRAIN_START).to_numpy()
    for h in horizons:
        m = train_mask(h) & after_start
        sub = df[m]
        rv_rows = sub[np.isfinite(std_target(sub, "rv", h))]
        fs.har_iv[h] = HarIv.fit(rv_rows, h)
        for kind in KINDS:
            tr = rv_rows if kind == "rv" else sub[np.isfinite(std_target(sub, kind, h))]
            fs.n_rows[(kind, h)] = len(tr)
            if lgbm and (lgbm_targets is None or (kind, h) in lgbm_targets):
                fitted = fit_target(
                    tr, kind, h, features, params=params, rounds=rounds, workers=workers
                )
                for p, b in fitted.items():
                    fs.boosters[(kind, h, p)] = b
            if progress is not None:
                progress(f"  fit {kind} h{h}: {len(tr)} rows")
    fs.seconds = time.monotonic() - t0
    return fs


def raw_predictions(
    fs: FitSet, df: pd.DataFrame, kind: str, h: int, features: Sequence[str]
) -> dict[str, np.ndarray]:
    """Uncalibrated (n, K) quantiles of every model for *df*'s rows."""
    lv = list(M1_QUANTILES)
    out: dict[str, np.ndarray] = {}
    if any(k == kind and hh == h for k, hh, _p in fs.boosters):
        out["lgbm"] = raw_lgbm(fs.boosters, kind, h, feature_matrix(df, features), df, lv)
    out["baseline"] = raw_baseline(kind, h, df, fs.har_iv.get(h), lv)
    if kind == "log_ret":
        iv = df["iv30"].to_numpy(dtype=float)
        out["iv_lognormal"] = iv_lognormal_quantiles(np.where(iv > 0, iv, np.nan), h, lv)
    return out


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _split_masks(df: pd.DataFrame) -> dict[str, np.ndarray]:
    etf = df["is_etf"].astype(bool).to_numpy()
    return {"All": np.ones(len(df), dtype=bool), "Stocks": ~etf, "ETFs": etf}


def coverage_error(cov: np.ndarray, levels: Sequence[float] = M1_QUANTILES) -> float:
    return float(np.max(np.abs(cov - np.asarray(levels))))


@dataclass
class FoldResult:
    """Per (kind, h): calibrated quantiles per model on the fold's test rows."""

    fold: str
    rows: dict[tuple[str, int], pd.DataFrame] = field(default_factory=dict)
    q: dict[tuple[str, int, str], np.ndarray] = field(default_factory=dict)
    q_raw: dict[tuple[str, int, str], np.ndarray] = field(default_factory=dict)
    offsets: dict[tuple[str, int, str], dict[float, float]] = field(default_factory=dict)
    q_roll: dict[tuple[str, int, str], np.ndarray] = field(default_factory=dict)
    crossing: dict[tuple[str, int], float] = field(default_factory=dict)
    cal_cov_err: dict[tuple[str, int, str], float] = field(default_factory=dict)
    #: E27.7: pinball of the cross-fitted (raw) LightGBM quantiles on the calibration
    #: block, i.e. before the test cut: an ML study's inner selection metric.
    inner_pinball: dict[tuple[str, int], float] = field(default_factory=dict)
    fit_seconds: float = 0.0


#: E27.7 (D133) calibration modes: ``annual`` = the pre-registered CQR (offsets fitted
#: once on the block before the test year); ``rolling_monthly`` = offsets refitted at the
#: start of every test month on the last *cal_sessions* sessions of out-of-sample
#: predictions whose labels end before the month (purge h + EMBARGO), the protocol the
#: ml-m1 diagnostic measured post hoc.
CALIBRATIONS: tuple[str, ...] = ("annual", "rolling_monthly")


def run_fold(
    df: pd.DataFrame,
    plan: FoldPlan,
    features: Sequence[str],
    *,
    horizons: Sequence[int] = M1_HORIZONS,
    params: dict[str, Any] | None = None,
    rounds: int = NUM_ROUNDS,
    progress: Callable[[str], None] | None = None,
    calibration: str = "annual",
    cal_sessions: int = CAL_SESSIONS,
    workers: int = MAX_WORKERS,
    lgbm_targets: Collection[tuple[str, int]] | None = None,
    dev_end: dt.date = DEV_END,
) -> tuple[FoldResult, FitSet]:
    if calibration not in CALIBRATIONS:
        msg = f"calibration {calibration!r}: one of {CALIBRATIONS}"
        raise ValueError(msg)
    s = dev_sessions(dev_end)
    si = session_index(df, s)
    say = progress or (lambda _m: None)
    halves = {h: plan.cal_halves(s, h) for h in horizons}
    cal_fs: list[FitSet] = []
    for k in range(2):
        say(f"{plan.name}: calibration models (cross-fit half {k + 1}/2)")
        cal_fs.append(
            fit_set(
                df,
                lambda h, k=k: (
                    (si >= 0)
                    & (si < s.index(plan.test_cut(s, h)))
                    & purged_outside(si, s, *halves[h][k], h)
                ),
                features,
                horizons=horizons,
                params=params,
                rounds=rounds,
                progress=progress,
                workers=workers,
                lgbm_targets=lgbm_targets,
            )
        )
    say(f"{plan.name}: test models")
    test_fs = fit_set(
        df,
        lambda h: (si >= 0) & (si < s.index(plan.test_cut(s, h))),
        features,
        horizons=horizons,
        params=params,
        rounds=rounds,
        progress=progress,
        workers=workers,
        lgbm_targets=lgbm_targets,
    )
    res = FoldResult(plan.name, fit_seconds=sum(f.seconds for f in cal_fs) + test_fs.seconds)
    lv = list(M1_QUANTILES)
    for h in horizons:
        for kind in KINDS:
            test = rows_for(df, kind, h, plan.test_start, plan.test_end + dt.timedelta(days=1))
            raw_test = raw_predictions(test_fs, test, kind, h, features)
            # evaluate every model on the same rows: target and all forecasts finite
            ok_t = np.isfinite(target(test, kind, h))
            for qt in raw_test.values():
                ok_t &= np.isfinite(qt).all(axis=1)
            test = test[ok_t].reset_index(drop=True)
            raw_test = {k: v[ok_t] for k, v in raw_test.items()}
            res.rows[(kind, h)] = test
            parts = [
                (rows_for(df, kind, h, b0, b1), fs)
                for (b0, b1), fs in zip(halves[h], cal_fs, strict=True)
            ]
            raw_cal: dict[str, np.ndarray] = {}
            for name in raw_test:
                raw_cal[name] = np.vstack(
                    [raw_predictions(fs, c, kind, h, features)[name] for c, fs in parts]
                )
            y_cal = np.concatenate([target(c, kind, h) for c, _ in parts])
            d_cal = np.concatenate([c["date"].to_numpy() for c, _ in parts])
            y_test = target(test, kind, h)
            d_test = test["date"].to_numpy()
            if "lgbm" in raw_cal:
                okc = np.isfinite(y_cal) & np.isfinite(raw_cal["lgbm"]).all(axis=1)
                res.inner_pinball[(kind, h)] = float(
                    mt.pinball(y_cal[okc], np.sort(raw_cal["lgbm"][okc], axis=1), lv).mean()
                )
            for name, qt in raw_test.items():
                res.q_raw[(kind, h, name)] = qt
                if name == "iv_lognormal":
                    res.q[(kind, h, name)] = np.sort(qt, axis=1)
                    continue
                offs = fit_offsets(raw_cal[name], y_cal, lv)
                res.offsets[(kind, h, name)] = offs
                res.q[(kind, h, name)] = apply_offsets(qt, offs, lv)
                ok = np.isfinite(y_cal) & np.isfinite(raw_cal[name]).all(axis=1)
                res.cal_cov_err[(kind, h, name)] = coverage_error(
                    mt.coverage(y_cal[ok], apply_offsets(raw_cal[name][ok], offs, lv))
                )
                # Rolling CQR. Per test month, the last cal_sessions sessions of OOS
                # predictions whose labels end before the month (cal block ∪ earlier test
                # months): what a nightly live refresh of the offsets would hold. A
                # post-hoc DIAGNOSTIC under the pre-registered ``annual`` protocol; the
                # evaluated forecast itself under ``rolling_monthly`` (E27.7 studies).
                pool_q = np.vstack([raw_cal[name], qt])
                pool_y = np.concatenate([y_cal, y_test])
                pool_d = np.concatenate([d_cal, d_test])
                pool_i = session_index(pd.DataFrame({"date": pool_d}), s)
                out = np.empty_like(qt)
                for m0, idx in rolling_blocks(d_test):
                    hi = _idx(s, m0) - h - EMBARGO
                    sel = (pool_i >= hi - cal_sessions) & (pool_i < hi)
                    out[idx] = apply_offsets(qt[idx], fit_offsets(pool_q[sel], pool_y[sel], lv), lv)
                res.q_roll[(kind, h, name)] = out
                if calibration == "rolling_monthly":
                    res.q[(kind, h, name)] = out
            if "lgbm" in raw_test:
                res.crossing[(kind, h)] = sort_quantiles(raw_test["lgbm"])[1]
    return res, test_fs


def rolling_blocks(dates: np.ndarray) -> list[tuple[dt.date, np.ndarray]]:
    """(first day of month, row indices) for each calendar month present in *dates*."""
    keys = np.array([(d.year, d.month) for d in dates], dtype=np.int64).reshape(-1, 2)
    out = []
    for y, m in sorted({(int(a), int(b)) for a, b in keys}):
        idx = np.flatnonzero((keys[:, 0] == y) & (keys[:, 1] == m))
        out.append((dt.date(y, m, 1), idx))
    return out


def final_offsets(
    res: FoldResult,
    kind: str,
    h: int,
    name: str,
    last_n: int = CAL_SESSIONS,
    dev_end: dt.date = DEV_END,
) -> dict[float, float]:
    """Offsets for the shipped model: the fold's test-year OOS rows in the last *last_n*
    sessions with labels inside dev (what the nightly refresh would hold at dev end)."""
    rows = res.rows[(kind, h)]
    s = dev_sessions(dev_end)
    si = session_index(rows, s)
    sel = si >= si.max() - last_n + 1
    return fit_offsets(
        res.q_raw[(kind, h, name)][sel], target(rows, kind, h)[sel], list(M1_QUANTILES)
    )


def metric_rows(res: FoldResult, horizons: Sequence[int] = M1_HORIZONS) -> pd.DataFrame:
    """One row per (kind, h, split, model): pinball, CRPS, coverage error, DM vs baseline."""
    lv = list(M1_QUANTILES)
    recs = []
    for h in horizons:
        for kind in KINDS:
            rows = res.rows[(kind, h)]
            y = target(rows, kind, h)
            dates = rows["date"].to_numpy()
            models = [m for m in ("lgbm", "baseline", "iv_lognormal") if (kind, h, m) in res.q]
            base_pb = mt.pinball(y, res.q[(kind, h, "baseline")], lv).mean(axis=1)
            for split, mask in _split_masks(rows).items():
                if not mask.any():
                    continue
                for m in models:
                    q = res.q[(kind, h, m)]
                    pb = mt.pinball(y, q, lv).mean(axis=1)
                    cov = mt.coverage(y[mask], q[mask])
                    raw_cov = mt.coverage(y[mask], np.sort(res.q_raw[(kind, h, m)][mask], axis=1))
                    diff = pd.Series(pb[mask] - base_pb[mask]).groupby(dates[mask]).mean()
                    stat, p2 = (
                        mt.dm_test(diff.to_numpy(), h) if m != "baseline" else (np.nan, np.nan)
                    )
                    p1 = (
                        p2 / 2
                        if np.isfinite(stat) and stat < 0
                        else (1 - p2 / 2 if np.isfinite(stat) else np.nan)
                    )
                    rec: dict[str, Any] = {
                        "fold": res.fold,
                        "kind": kind,
                        "h": h,
                        "split": split,
                        "model": m,
                        "n": int(mask.sum()),
                        "pinball": float(pb[mask].mean()),
                        "crps": float(mt.crps_from_quantiles(y[mask], q[mask], lv).mean()),
                        "cov_err": coverage_error(cov),
                        "cov_err_raw": coverage_error(raw_cov),
                        "cov_err_cal": res.cal_cov_err.get((kind, h, m), float("nan")),
                        "dm_stat": stat,
                        "dm_p_one_sided": p1,
                    }
                    for p, c in zip(lv, cov, strict=True):
                        rec[f"cov_{qtag(p)}"] = float(c)
                    qr = res.q_roll.get((kind, h, m))
                    if qr is not None:  # post-hoc diagnostic: monthly rolling CQR
                        rec["pinball_roll"] = float(mt.pinball(y[mask], qr[mask], lv).mean())
                        rec["cov_err_roll"] = coverage_error(mt.coverage(y[mask], qr[mask]))
                    med = q[mask, lv.index(0.5)]
                    if kind == "log_ret":
                        nz = med != 0
                        rec["sign_acc"] = float((np.sign(med[nz]) == np.sign(y[mask][nz])).mean())
                        rec["rank_ic"] = mt.daily_rank_ic(dates[mask], med, y[mask])
                    else:
                        rec["qlike"] = float(np.nanmean(mt.qlike(y[mask], med)))
                        rec["rank_ic"] = mt.daily_rank_ic(dates[mask], med, y[mask])
                    recs.append(rec)
            if kind == "rv":  # point forecasts for QLIKE reference
                har = rows[f"har_rv_h{h}"].to_numpy(dtype=float)
                for nm, fc in (("har_point", har), ("iv30_point", rows["iv30"].to_numpy(float))):
                    for split, mask in _split_masks(rows).items():
                        if mask.any():
                            recs.append(
                                {
                                    "fold": res.fold,
                                    "kind": kind,
                                    "h": h,
                                    "split": split,
                                    "model": nm,
                                    "n": int(mask.sum()),
                                    "qlike": float(np.nanmean(mt.qlike(y[mask], fc[mask]))),
                                }
                            )
    return pd.DataFrame(recs)


def keep_decisions(table: pd.DataFrame, horizons: Sequence[int] = M1_HORIZONS) -> pd.DataFrame:
    """The pre-registered keep rule per (kind, h) from the All-split rows of both folds."""
    recs = []
    for kind in KINDS:
        for h in horizons:
            t = table[(table.kind == kind) & (table.h == h) & (table.split == "All")]
            checks = []
            for fold in sorted(t.fold.unique()):
                lg = t[(t.fold == fold) & (t.model == "lgbm")]
                bs = t[(t.fold == fold) & (t.model == "baseline")]
                if lg.empty or bs.empty:
                    checks.append((fold, False, "missing"))
                    continue
                lgr, bsr = lg.iloc[0], bs.iloc[0]
                better = lgr.pinball < bsr.pinball
                sig = bool(np.isfinite(lgr.dm_p_one_sided) and lgr.dm_p_one_sided < KEEP_P)
                cov_ok = lgr.cov_err <= KEEP_COVERAGE
                why = (
                    f"pinball {lgr.pinball:.5f} vs {bsr.pinball:.5f}, "
                    f"DM p {lgr.dm_p_one_sided:.3g}, cov err {100 * lgr.cov_err:.1f} pts"
                )
                checks.append((fold, bool(better and sig and cov_ok), why))
            keep = bool(checks) and all(c[1] for c in checks)
            recs.append(
                {
                    "kind": kind,
                    "h": h,
                    "source": "lgbm" if keep else "baseline",
                    **{f"{f}_pass": ok for f, ok, _ in checks},
                    **{f"{f}_detail": why for f, _, why in checks},
                }
            )
    return pd.DataFrame(recs)


# ---------------------------------------------------------------------------
# Diagnostics: importance, regimes, other targets
# ---------------------------------------------------------------------------


def gain_importance(fs: FitSet, features: Sequence[str], kind: str, h: int) -> pd.Series:
    tot = pd.Series(0.0, index=list(features))
    for p in M1_QUANTILES:
        b = fs.boosters.get((kind, h, float(p)))
        if b is not None:
            tot += pd.Series(b.feature_importance("gain"), index=list(features))
    return tot / tot.sum() if tot.sum() > 0 else tot


def permutation_importance(
    fs: FitSet,
    test: pd.DataFrame,
    kind: str,
    h: int,
    features: Sequence[str],
    cands: Iterable[str],
    *,
    level: float = 0.5,
    n_rows: int = 40_000,
    seed: int = 20261017,
) -> pd.Series:
    """Rise in standardised pinball of the *level* model when one feature is shuffled."""
    rng = np.random.default_rng(seed)
    sub = test.iloc[np.sort(rng.choice(len(test), min(n_rows, len(test)), replace=False))]
    x = feature_matrix(sub, features)
    y = std_target(sub, kind, h)
    b = fs.boosters[(kind, h, float(level))]
    base = float(mt.pinball(y, b.predict(x)[:, None], [level]).mean())
    out = {}
    for f in cands:
        j = list(features).index(f)
        xp = x.copy()
        xp[:, j] = rng.permutation(xp[:, j])
        out[f] = float(mt.pinball(y, b.predict(xp)[:, None], [level]).mean()) - base
    return pd.Series(out).sort_values(ascending=False)


def regime_labels(rows: pd.DataFrame, h: int) -> pd.DataFrame:
    trend = rows["spy_trend"].map({1.0: "bull", -1.0: "bear", 0.0: "sideways"}).fillna("unknown")
    vp = rows["vix_proxy_pct_252"]
    vix = np.where(vp >= 0.8, "high", np.where(vp <= 0.2, "low", "mid"))  # noqa: PLR2004
    vix = np.where(vp.isna(), "unknown", vix)
    earn = (~rows["is_etf"].astype(bool)) & (rows["sess_to_earn"] <= h)
    return pd.DataFrame(
        {
            "spy_trend": trend.to_numpy(),
            "vix_pct": vix,
            "earnings_in_h": np.where(earn, "yes", "no"),
            "class": np.where(rows["is_etf"].astype(bool), "ETF", "Stock"),
        },
        index=rows.index,
    )


def regime_table(res: FoldResult, kind: str, h: int, model: str) -> pd.DataFrame:
    rows = res.rows[(kind, h)]
    y = target(rows, kind, h)
    lv = list(M1_QUANTILES)
    q, qb = res.q[(kind, h, model)], res.q[(kind, h, "baseline")]
    pb = mt.pinball(y, q, lv).mean(axis=1)
    pbb = mt.pinball(y, qb, lv).mean(axis=1)
    lab = regime_labels(rows, h)
    recs = []
    for col in lab.columns:
        for val, idx in lab.groupby(col).groups.items():
            m = rows.index.isin(idx)
            if m.sum() < 500:  # noqa: PLR2004
                continue
            cov = mt.coverage(y[m], q[m])
            recs.append(
                {
                    "fold": res.fold,
                    "kind": kind,
                    "h": h,
                    "regime": col,
                    "value": val,
                    "n": int(m.sum()),
                    "pinball": float(pb[m].mean()),
                    "pinball_vs_base": float(pb[m].mean() - pbb[m].mean()),
                    "cov_err": coverage_error(cov),
                    "cov_05": float(cov[0]),
                    "cov_95": float(cov[-1]),
                }
            )
    return pd.DataFrame(recs)


def other_targets(res: FoldResult, h: int, model: str) -> dict[str, float]:
    """Rank IC of the M1-implied width/|move| vs excursion and move-vs-implied labels."""
    rows = res.rows[("log_ret", h)]
    q = res.q[("log_ret", h, model)]
    lv = list(M1_QUANTILES)
    _, eabs = p_up_and_abs(q, lv)
    iv = rows["iv30"].to_numpy(dtype=float)
    tau = math.sqrt(h / 252)
    with np.errstate(divide="ignore", invalid="ignore"):
        mvi = eabs / (iv * tau)
        up = (q[:, lv.index(0.95)] - q[:, lv.index(0.5)]) / (iv * tau)
        dn = (q[:, lv.index(0.5)] - q[:, lv.index(0.05)]) / (iv * tau)
    d = rows["date"].to_numpy()
    y_mvi = rows[f"move_vs_implied_h{h}"].to_numpy(dtype=float)
    return {
        "move_vs_implied_ic": mt.daily_rank_ic(d, mvi, y_mvi),
        "move_vs_implied_mean_fc": float(np.nanmean(np.where(iv > 0, mvi, np.nan))),
        "move_vs_implied_mean_real": float(np.nanmean(y_mvi)),
        "max_up_ic": mt.daily_rank_ic(d, up, rows[f"max_up_h{h}"].to_numpy(dtype=float)),
        "max_dn_ic": mt.daily_rank_ic(d, dn, rows[f"max_dn_h{h}"].to_numpy(dtype=float)),
    }


# ---------------------------------------------------------------------------
# Artefacts
# ---------------------------------------------------------------------------


def data_digest(df: pd.DataFrame, features: Sequence[str]) -> str:
    cols = [*_ID, *features, *(c for c in df.columns if c.startswith(("log_ret_h", "rv_h")))]
    h = pd.util.hash_pandas_object(df[cols], index=False).to_numpy()
    return hashlib.sha256(h.tobytes()).hexdigest()[:16]


def protocol_hash() -> str:
    body = {
        "rev": PROTOCOL_REV,
        "train_start": str(TRAIN_START),
        "embargo": EMBARGO,
        "cal_sessions": CAL_SESSIONS,
        "test_years": TEST_YEARS,
        "keep_p": KEEP_P,
        "keep_cov": KEEP_COVERAGE,
        "levels": M1_QUANTILES,
        "horizons": M1_HORIZONS,
        "dev_end": str(DEV_END),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def version_of(
    feature_schema: str,
    digest: str,
    params: dict[str, Any],
    rounds: int,
    extra: dict[str, Any] | None = None,
) -> str:
    """The version id. *extra* (E27.7: a study's training overlay beyond params/rounds,
    e.g. its calibration protocol) joins the hash only when set, so a plain
    ``arc ml m1 train`` keeps its id."""
    body: list[Any] = [feature_schema, digest, params, rounds, protocol_hash()]
    if extra:
        body.append(extra)
    blob = json.dumps(body, sort_keys=True, default=str).encode()
    return "m1-" + hashlib.sha256(blob).hexdigest()[:12]


def booster_file(kind: str, h: int, p: float) -> str:
    return f"lgbm/{kind}_h{h}_q{qtag(p)}.txt"


def write_model(
    out_dir: Path,
    fs: FitSet,
    targets: Sequence[TargetModel],
    *,
    features: Sequence[str],
    digest: str,
    params: dict[str, Any],
    rounds: int,
    code_sha: str,
    feature_manifest_sha: str,
    train_window: dict[str, str],
    calibration_window: dict[str, str],
    extra: dict[str, Any] | None = None,
    study: str | None = None,
) -> M1Manifest:
    """Write ``lgbm/*.txt`` (text dumps), ``manifest.json``; returns the manifest.

    E27.7: *extra* is the study's overlay beyond params/rounds (in the version id and
    the manifest's ``overlay``); *study* stamps ``study: ML-<n>``."""
    fschema = schema_version()
    version = version_of(fschema, digest, params, rounds, extra)
    vdir = out_dir / version
    (vdir / "lgbm").mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for (kind, h, p), b in sorted(fs.boosters.items()):
        name = booster_file(kind, h, p)
        text = b.model_to_string()
        (vdir / name).write_text(text)
        files[name] = hashlib.sha256(text.encode()).hexdigest()
    man = M1Manifest(
        version=version,
        feature_schema=fschema,
        features=list(features),
        targets=list(targets),
        lgb_params=params,
        num_rounds=rounds,
        train_window=train_window,
        calibration_window=calibration_window,
        code_sha=code_sha,
        config_hashes={
            "protocol": protocol_hash(),
            "lgb_params": hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[
                :16
            ],
            "feature_store_manifest": feature_manifest_sha,
        },
        data_digest=digest,
        files=files,
        study=study,
        overlay=extra or None,
    )
    (vdir / "manifest.json").write_text(man.model_dump_json(indent=2) + "\n")
    (vdir / "FEATURE_SCHEMA_VERSION").write_text(fschema + "\n")
    (vdir / "features.json").write_text(json.dumps(list(features), indent=2) + "\n")
    return man


def load_model(vdir: Path, *, check_schema: bool = True, text: bool = False) -> M1Model:
    """Read an artefact directory (text dumps only; refuses a stale feature schema).

    ``text=True`` (E27.4 live serving) evaluates the dumps with the pure-numpy
    :class:`~arc.ml.lgbm_text.TextBooster` instead of ``lightgbm`` (absent in prod/CI).
    """
    from arc.ml.features.registry import require_schema

    man = M1Manifest.model_validate_json((vdir / "manifest.json").read_text())
    if check_schema:
        require_schema(man.feature_schema)
    make: Callable[[str], Any]
    if text:
        from arc.ml.lgbm_text import TextBooster

        make = TextBooster
    else:
        make = _lgb_from_text
    boosters = {}
    for t in man.targets:
        if t.source != "lgbm":
            continue
        for p in man.levels:
            name = booster_file(t.kind, t.h, p)
            text_ = (vdir / name).read_text()
            if hashlib.sha256(text_.encode()).hexdigest() != man.files[name]:
                msg = f"{name}: sha256 mismatch with manifest"
                raise ValueError(msg)
            boosters[(t.kind, t.h, float(p))] = make(text_)
    return M1Model(man, boosters)


def fold_model(
    res: FoldResult, fs: FitSet, decisions: pd.DataFrame, features: Sequence[str]
) -> M1Model:
    """An in-memory M1 for one fold's test block (its offsets, its test boosters)."""
    targets = []
    for r in decisions.itertuples():
        src = str(r.source)
        offs = res.offsets[(str(r.kind), int(r.h), src)]
        targets.append(
            TargetModel(
                kind=r.kind,
                h=int(r.h),
                source=src,  # type: ignore[arg-type]
                offsets={qtag(p): v for p, v in offs.items()},
                har_iv=fs.har_iv.get(int(r.h)) if r.kind == "rv" else None,
            )
        )
    man = M1Manifest(
        version=f"oos-{res.fold}",
        feature_schema=schema_version(),
        features=list(features),
        targets=targets,
        lgb_params=LGB_PARAMS,
        num_rounds=NUM_ROUNDS,
        train_window={},
        calibration_window={},
        code_sha="",
        config_hashes={},
        data_digest="",
    )
    return M1Model(man, fs.boosters)


__all__ = [
    "FEATURES",
    "FOLDS",
    "LGB_PARAMS",
    "NUM_ROUNDS",
    "FitSet",
    "FoldPlan",
    "FoldResult",
    "TrainRun",
    "direction",
    "fit_set",
    "keep_decisions",
    "load_dev_dataset",
    "load_model",
    "metric_rows",
    "model_features",
    "publish",
    "run_fold",
    "train_m1",
    "write_model",
]


# ---------------------------------------------------------------------------
# The whole run
# ---------------------------------------------------------------------------


@dataclass
class TrainRun:
    """Everything one ``arc ml m1 train`` produced (aggregates feed the report)."""

    manifest: M1Manifest
    metrics: pd.DataFrame
    decisions: pd.DataFrame
    regimes: pd.DataFrame
    gain: pd.DataFrame
    permutation: pd.DataFrame
    others: pd.DataFrame
    crossing: pd.DataFrame
    timings: dict[str, float]
    sizes: dict[str, int]
    rows: dict[str, int]
    model_dir: Path
    oos_path: Path | None = None


def _feature_manifest_sha(features_root: Path) -> str:
    from arc.ml.features.store import version_dir

    p = version_dir(features_root) / "manifest.json"
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else "none"


def load_dev_dataset(
    data_dir: Path,
    features_root: Path,
    *,
    tickers: Sequence[str] | None = None,
    horizons: Sequence[int] = M1_HORIZONS,
    dev_end: dt.date = DEV_END,
    holdout_reason: str | None = None,
) -> pd.DataFrame:
    """The M1 dev panel + labels (2020 → *dev_end*, default :data:`DEV_END`; the
    loaders refuse the holdout unless the D140 retrain passes its *holdout_reason*, D84)."""
    from arc.ml.m1.dataset import attach_targets, load_panel

    panel = load_panel(features_root, dt.date(2020, 1, 1), dev_end, holdout_reason=holdout_reason)
    if tickers is not None:
        panel = panel[panel["ticker"].isin(set(tickers))].reset_index(drop=True)
    return attach_targets(
        panel, data_dir, dev_end, horizons=horizons, holdout_reason=holdout_reason
    )


def train_m1(
    data_dir: Path,
    features_root: Path,
    out_dir: Path,
    *,
    code_sha: str,
    tickers: Sequence[str] | None = None,
    params: dict[str, Any] | None = None,
    rounds: int = NUM_ROUNDS,
    horizons: Sequence[int] = M1_HORIZONS,
    progress: Callable[[str], None] | None = None,
    features: Sequence[str] | None = None,
    calibration: str = "annual",
    cal_sessions: int = CAL_SESSIONS,
    lgbm_targets: Collection[tuple[str, int]] | None = None,
    extra: dict[str, Any] | None = None,
    study: str | None = None,
    workers: int = MAX_WORKERS,
    df: pd.DataFrame | None = None,
    dev_end: dt.date = DEV_END,
    folds: Sequence[FoldPlan] = FOLDS,
    holdout_reason: str | None = None,
) -> TrainRun:
    """Walk-forward evaluation, keep rule, final fit and artefacts (dev data only).

    The D140 monthly retrain passes a later *dev_end* (its rolling train end), its own
    *folds* and the *holdout_reason* that lets the dataset read past the D84 boundary.

    E27.7 (D133) study overlays: *features* (default :func:`model_features`),
    *calibration* (:data:`CALIBRATIONS`) with its *cal_sessions*, *lgbm_targets* (the
    (kind, h) LightGBM may serve; the rest stay on their baseline), *extra* (the overlay
    beyond params/rounds, in the version id) and *study* (``ML-<n>`` in the manifest).
    Every default reproduces a plain ``arc ml m1 train`` and its version id. *df* is a
    pre-loaded dev dataset (a study loads it once for all its variants)."""
    say = progress or (lambda _m: None)
    prm = dict(params or LGB_PARAMS)
    t0 = time.monotonic()
    if df is None:
        df = load_dev_dataset(
            data_dir,
            features_root,
            tickers=tickers,
            horizons=horizons,
            dev_end=dev_end,
            holdout_reason=holdout_reason,
        )
    features = list(features) if features is not None else model_features()
    digest = data_digest(df, features)
    t_load = time.monotonic() - t0
    say(f"dataset: {len(df)} rows, {df['ticker'].nunique()} tickers, digest {digest}")

    results: list[FoldResult] = []
    fitsets: list[FitSet] = []
    for plan in folds:
        res, fs = run_fold(
            df,
            plan,
            features,
            horizons=horizons,
            params=prm,
            rounds=rounds,
            progress=progress,
            calibration=calibration,
            cal_sessions=cal_sessions,
            workers=workers,
            lgbm_targets=lgbm_targets,
            dev_end=dev_end,
        )
        results.append(res)
        fitsets.append(fs)
    table = pd.concat([metric_rows(r, horizons) for r in results], ignore_index=True)
    decisions = keep_decisions(table, horizons)
    say("keep decisions:\n" + decisions[["kind", "h", "source"]].to_string(index=False))

    # final model: every dev row; offsets from the last fold's test residuals
    say("final model")
    s = dev_sessions(dev_end)
    si = session_index(df, s)
    final = fit_set(
        df,
        lambda _h: si >= 0,
        features,
        horizons=horizons,
        params=prm,
        rounds=rounds,
        progress=progress,
        workers=workers,
        lgbm_targets=lgbm_targets,
    )
    last = results[-1]
    targets = [
        TargetModel(
            kind=r.kind,
            h=int(r.h),
            source=r.source,  # type: ignore[arg-type]
            offsets={
                qtag(p): v
                for p, v in final_offsets(
                    last, str(r.kind), int(r.h), str(r.source), cal_sessions, dev_end=dev_end
                ).items()
            },
            har_iv=final.har_iv[int(r.h)] if r.kind == "rv" else None,
        )
        for r in decisions.itertuples()
    ]
    man = write_model(
        out_dir,
        final,
        targets,
        features=features,
        digest=digest,
        params=prm,
        rounds=rounds,
        code_sha=code_sha,
        feature_manifest_sha=_feature_manifest_sha(features_root),
        train_window={"start": str(TRAIN_START), "end": str(dev_end), "purge": "labels inside dev"},
        calibration_window={
            "start": str(folds[-1].test_start),
            "end": str(dev_end),
            "source": f"fold {folds[-1].name} residuals of the model trained before it",
        },
        extra=extra,
        study=study,
    )
    vdir = out_dir / man.version

    # out-of-sample m1 features (the test folds) for M2 / the backtest
    oos = []
    for res, fs in zip(results, fitsets, strict=True):
        plan = next(p for p in folds if p.name == res.fold)
        m = (df["date"] >= plan.test_start) & (df["date"] <= plan.test_end)
        oos.append(publish(fold_model(res, fs, decisions, features), df[m].reset_index(drop=True)))
    oos_df = pd.concat(oos, ignore_index=True)
    oos_path = vdir / "oos_m1_features.parquet"
    oos_df.to_parquet(oos_path, index=False)

    # diagnostics
    regimes = pd.concat(
        [
            regime_table(r, kind, h, "lgbm")
            for r in results
            for kind in KINDS
            for h in horizons
            if (kind, h, "lgbm") in r.q
        ]
        or [pd.DataFrame()],
        ignore_index=True,
    )
    gains, perms, others = [], [], []
    for kind in KINDS:
        for h in horizons:
            g = gain_importance(final, features, kind, h)
            gains.append(pd.DataFrame({"kind": kind, "h": h, "feature": g.index, "gain": g.values}))
    for res, fs in zip(results, fitsets, strict=True):
        for kind in KINDS:
            if (kind, 21, 0.5) not in fs.boosters:  # a study's target subset without it
                continue
            g = gain_importance(fs, features, kind, 21).sort_values(ascending=False)
            top = list(g.index[:15])
            for lvl in (0.05, 0.5, 0.95):
                pi = permutation_importance(
                    fs, res.rows[(kind, 21)], kind, 21, features, top, level=lvl
                )
                perms.append(
                    pd.DataFrame(
                        {
                            "fold": res.fold,
                            "kind": kind,
                            "h": 21,
                            "level": lvl,
                            "feature": pi.index,
                            "pinball_rise": pi.values,
                        }
                    )
                )
        for h in horizons:
            src = str(decisions[(decisions.kind == "log_ret") & (decisions.h == h)].source.iloc[0])
            others.append({"fold": res.fold, "h": h, "model": src, **other_targets(res, h, src)})
    crossing = pd.DataFrame(
        [
            {"fold": r.fold, "kind": k, "h": h, "crossing_rate": v}
            for r in results
            for (k, h), v in r.crossing.items()
        ]
    )

    # timings and sizes
    t_pred = time.monotonic()
    model = load_model(vdir)
    last_day = max(df["date"])
    day = df[df["date"] == last_day].reset_index(drop=True)
    publish(model, day)
    t_pred = time.monotonic() - t_pred
    # the all-LightGBM path (what a future version that keeps every target would cost)
    t_lgbm = time.monotonic()
    all_lgbm = M1Model(
        man.model_copy(
            update={
                "targets": [
                    t.model_copy(
                        update={
                            "source": "lgbm",
                            "offsets": {
                                qtag(p): v
                                for p, v in final_offsets(
                                    last, t.kind, t.h, "lgbm", dev_end=dev_end
                                ).items()
                            },
                        }
                    )
                    for t in man.targets
                    if (t.kind, t.h, "lgbm") in last.q_raw
                ]
            }
        ),
        final.boosters,
    )
    publish(all_lgbm, day)
    t_lgbm = time.monotonic() - t_lgbm
    sizes = {p.name: p.stat().st_size for p in sorted((vdir / "lgbm").glob("*.txt"))}
    timings = {
        "load_dataset_s": t_load,
        "fold_fits_s": sum(r.fit_seconds for r in results),
        "final_fit_s": final.seconds,
        "predict_one_day_s": t_pred,
        "predict_one_day_all_lgbm_s": t_lgbm,
        "predict_one_day_rows": float(len(day)),
        "total_s": time.monotonic() - t0,
    }
    rows = {
        "dataset": len(df),
        "tickers": int(df["ticker"].nunique()),
        **{f"final_{k}_h{h}": n for (k, h), n in final.n_rows.items()},
    }
    run = TrainRun(
        manifest=man,
        metrics=table,
        decisions=decisions,
        regimes=regimes,
        gain=pd.concat(gains, ignore_index=True),
        permutation=pd.concat(perms or [pd.DataFrame()], ignore_index=True),
        others=pd.DataFrame(others),
        crossing=crossing,
        timings=timings,
        sizes=sizes,
        rows=rows,
        model_dir=vdir,
        oos_path=oos_path,
    )
    for name, frame in (
        ("metrics", table),
        ("decisions", decisions),
        ("regimes", regimes),
        ("gain", run.gain),
        ("permutation", run.permutation),
        ("others", run.others),
        ("crossing", crossing),
    ):
        frame.to_csv(vdir / f"eval_{name}.csv", index=False)
    (vdir / "timings.json").write_text(
        json.dumps({**timings, "sizes": sizes, "rows": rows}, indent=2) + "\n"
    )
    log.info("ml.m1.trained", version=man.version, seconds=round(timings["total_s"], 1))
    return run
