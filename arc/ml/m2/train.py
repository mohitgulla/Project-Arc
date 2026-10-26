"""M2 trainer (E27.3, D126): walk-forward grid search, out-of-fold calibration, final
fit, baselines, metrics and the registry artefacts.

Per family (``credit`` / ``debit``) and per fold (test 2022, test 2023; the harness in
:mod:`arc.ml.validate`):

1. **inner split**: train rows before the last train year (purged + embargoed against
   it) fit; the last train year validates (early stopping, grid selection) — never
   shuffled;
2. **grid**: the fixed :class:`~arc.ml.m2.config.M2Config` grid for ``win`` (log-loss),
   ``stop`` (log-loss) and ``r`` (per-day rank IC of ``e_r`` vs realised ``r_net``);
   the quantile, hold and conditional regressors reuse the ``r`` point. Every config
   tried is logged (``configs_tried.csv``);
3. **calibration**: isotonic on the inner model's out-of-fold predictions of the last
   train year (:mod:`arc.ml.calibrate`), applied to the refit;
4. **refit** on every train row at the selected rounds, then score the test year.

The final (registered) model refits every dev row at fold-2023's selected points; its
calibrators are fitted on fold-2023's test-year raw scores (the model trained before
2023), so they are out-of-fold too.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import resource
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog

from arc.ml import validate as v
from arc.ml.calibrate import FamilyCalibrator, fit_family
from arc.ml.m2 import dataset as ds
from arc.ml.m2.config import FAMILY_PROFILE, RULE_KIND, M2Config

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

PROTOCOL_REV = 1
DEV_YEARS: tuple[int, ...] = (2020, 2021, 2022, 2023)
QUANTILES: tuple[float, ...] = (0.1, 0.5, 0.9)
#: target -> (objective kind, label key)
TARGETS: dict[str, str] = {
    "win": "binary",
    "stop": "binary",
    "r": "huber",
    "q10": "quantile",
    "q50": "quantile",
    "q90": "quantile",
    "days": "regression",
    "r_win": "huber",
    "r_loss": "huber",
}
GRID_TARGETS = ("win", "stop", "r")
#: score columns the predictor publishes
OUTPUTS = ("p_win", "p_stop", "e_r", "r_q10", "r_q50", "r_q90", "e_days")


def load_lightgbm() -> Any:
    """``lightgbm`` (``ml`` extra; on macOS it needs ``brew install libomp``, OPS.md)."""
    import lightgbm

    return lightgbm


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """One family's modelling frame."""

    df: pd.DataFrame
    features: list[str]
    vocab: dict[str, list[str]]
    weights: np.ndarray
    sessions: list[dt.date]
    labels: dict[str, str]

    def x(self, mask: np.ndarray | None = None) -> pd.DataFrame:
        d = self.df if mask is None else self.df[mask]
        return ds.design(d, self.features, self.vocab)


def feature_list(cfg: M2Config, panel_cols: Sequence[str]) -> list[str]:
    feats = [*panel_cols, *ds.CAND_NUMERIC, *ds.cat_names(), *cfg.add_features]
    drop = set(cfg.drop_features)
    return [f for f in dict.fromkeys(feats) if f not in drop]


def panel_columns() -> list[str]:
    from arc.ml.m1.train import model_features

    return model_features()


def target_values(df: pd.DataFrame, labels: dict[str, str], target: str) -> np.ndarray:
    if target == "win":
        return df[labels["win"]].astype(float).to_numpy()
    if target == "stop":
        return df[labels["stop"]].astype(float).to_numpy()
    if target == "days":
        return df[labels["days"]].astype(float).to_numpy()
    return df[labels["r"]].astype(float).to_numpy()


def target_mask(df: pd.DataFrame, labels: dict[str, str], target: str) -> np.ndarray:
    y = target_values(df, labels, target)
    ok = np.isfinite(y)
    if target == "r_win":
        ok &= df[labels["r"]].to_numpy(dtype=float) > 0
    elif target == "r_loss":
        ok &= df[labels["r"]].to_numpy(dtype=float) <= 0
    return ok


def build_frame(
    cfg: M2Config,
    family: str,
    ledger_root: Path,
    features_root: Path,
    schema: str,
    *,
    years: Sequence[int] = DEV_YEARS,
    tickers: Sequence[str] | None = None,
) -> Frame:
    from arc.utils.calendar import sessions_between

    rows = ds.load_family(
        ledger_root,
        FAMILY_PROFILE[family],
        years,
        cfg.label_x,
        cap_per_day=cfg.cap_per_day,
        tickers=tickers,
    )
    rows = rows.reset_index(drop=True)
    pcols = panel_columns()
    panel = ds.load_panel_range(
        features_root, dt.date(min(years), 1, 1), dt.date(max(years), 12, 31), schema, pcols
    )
    df = ds.attach_features(rows, panel, pcols)
    feats = feature_list(cfg, pcols)
    vocab = ds.build_vocab(df)
    w = v.sample_weights(df, df["inv_keep"].to_numpy(dtype=float))
    sess = sessions_between(dt.date(min(years) - 1, 1, 1), dt.date(max(years) + 1, 3, 31))
    return Frame(df, feats, vocab, w, sess, ds.label_cols(cfg.label_x))


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def lgb_params(
    cfg: M2Config, target: str, point: dict[str, int], features: Sequence[str]
) -> dict[str, Any]:
    p = {**cfg.lgb_base(), **point}
    kind = TARGETS[target]
    if kind == "binary":
        p["objective"] = "binary"
    elif kind == "huber":
        p["objective"] = "huber"
        p["alpha"] = cfg.huber_alpha
    elif kind == "quantile":
        p["objective"] = "quantile"
        p["alpha"] = int(target[1:]) / 100.0
    else:
        p["objective"] = "regression"
    return p


def monotone_vector(cfg: M2Config, features: Sequence[str]) -> list[int]:
    return [int(cfg.monotone_r.get(f, 0)) for f in features]


def _dataset(fr: Frame, mask: np.ndarray, target: str, ref: Any = None) -> Any:
    lgb = load_lightgbm()
    m = mask & target_mask(fr.df, fr.labels, target)
    x = fr.x(m)
    y = target_values(fr.df[m], fr.labels, target)
    cats = [f for f in fr.features if f.startswith("cat_")]
    return lgb.Dataset(
        x.to_numpy(dtype=np.float64),
        y,
        weight=fr.weights[m],
        feature_name=list(fr.features),
        categorical_feature=cats,
        reference=ref,
        free_raw_data=True,
        params={"max_bin": 63, "verbose": -1},
    ), m


def predict_raw(booster: Any, x: pd.DataFrame) -> np.ndarray:
    return np.asarray(booster.predict(x.to_numpy(dtype=np.float64), raw_score=False), dtype=float)


@dataclass
class Trial:
    target: str
    point: dict[str, int]
    monotone: bool
    best_iter: int
    metric: str
    value: float
    seconds: float


def fit_one(
    cfg: M2Config,
    fr: Frame,
    target: str,
    point: dict[str, int],
    train: np.ndarray,
    val: np.ndarray | None,
    *,
    rounds: int | None = None,
    monotone: bool = False,
) -> tuple[Any, int]:
    """One booster; early-stopped on *val* when given, else exactly *rounds* rounds."""
    lgb = load_lightgbm()
    prm = lgb_params(cfg, target, point, fr.features)
    if monotone and target in ("r", "r_win", "r_loss"):
        prm["monotone_constraints"] = monotone_vector(cfg, fr.features)
        prm["monotone_constraints_method"] = "basic"
    dtr, _ = _dataset(fr, train, target)
    if val is not None and val.any():
        dva, _ = _dataset(fr, val, target, ref=dtr)
        b = lgb.train(
            prm,
            dtr,
            num_boost_round=cfg.max_rounds,
            valid_sets=[dva],
            callbacks=[lgb.early_stopping(cfg.early_stopping, verbose=False)],
        )
        best = int(b.best_iteration or b.current_iteration())
        return b, max(best, 1)
    b = lgb.train(prm, dtr, num_boost_round=int(rounds or cfg.max_rounds))
    return b, int(rounds or cfg.max_rounds)


def _val_metric(fr: Frame, target: str, pred: np.ndarray, m: np.ndarray) -> tuple[str, float]:
    """Selection metric (lower = better): log-loss for classifiers, −IC for ``r``."""
    y = target_values(fr.df[m], fr.labels, target)
    w = fr.weights[m]
    if TARGETS[target] == "binary":
        return "log_loss", v.log_loss(y, pred, w)
    if target == "r":
        ic = v.daily_ic(fr.df.loc[m, "date"].tolist(), pred, y)
        return "neg_ic", -float(ic.mean()) if len(ic) else math.inf
    if TARGETS[target] == "quantile":
        a = int(target[1:]) / 100.0
        e = y - pred
        return "pinball", float(np.average(np.maximum(a * e, (a - 1) * e), weights=w))
    return "mse", float(np.average((y - pred) ** 2, weights=w))


@dataclass
class FoldModel:
    boosters: dict[str, Any]
    calib: dict[str, FamilyCalibrator]
    points: dict[str, dict[str, int]]
    rounds: dict[str, int]
    tau_stop: float
    trials: list[Trial] = field(default_factory=list)
    calib_report: dict[str, Any] = field(default_factory=dict)
    raw_val: dict[str, np.ndarray] = field(default_factory=dict)
    monotone: bool = False


def score(
    model: FoldModel, fr: Frame, mask: np.ndarray | None = None, df: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Calibrated outputs (:data:`OUTPUTS`) for *df* (or *fr.df[mask]*)."""
    from arc.ml.m2.predict import score_rows

    d = df if df is not None else (fr.df if mask is None else fr.df[mask])
    return score_rows(model.boosters, model.calib, fr.features, fr.vocab, d)


def fit_fold(
    cfg: M2Config,
    fr: Frame,
    train: np.ndarray,
    val_start: dt.date,
    *,
    monotone: bool = False,
    grid: bool = True,
    fixed: dict[str, dict[str, int]] | None = None,
    say: Callable[[str], None] = lambda _m: None,
) -> FoldModel:
    """Grid on the inner split, OOF calibration, refit on all of *train*."""
    fit, val = v.inner_split(fr.df, train, fr.sessions, val_start=val_start)
    points: dict[str, dict[str, int]] = {}
    rounds: dict[str, int] = {}
    trials: list[Trial] = []
    inner: dict[str, Any] = {}
    grid_pts = [g.model_dump() for g in cfg.grid] if grid else []
    for target in TARGETS:
        cands = (
            grid_pts
            if (target in GRID_TARGETS and grid_pts)
            else [(fixed or points).get(target) or points.get("r") or cfg.grid[0].model_dump()]
        )
        best: tuple[float, dict[str, int], int, Any] | None = None
        for pt in cands:
            t0 = time.monotonic()
            b, it = fit_one(cfg, fr, target, pt, fit, val, monotone=monotone)
            m = val & target_mask(fr.df, fr.labels, target)
            name, val_metric = _val_metric(fr, target, predict_raw(b, fr.x(m)), m)
            trials.append(Trial(target, pt, monotone, it, name, val_metric, time.monotonic() - t0))
            if best is None or val_metric < best[0]:
                best = (val_metric, pt, it, b)
        assert best is not None
        points[target], rounds[target], inner[target] = best[1], best[2], best[3]
        say(f"    {target}: {best[1]} it={best[2]} {trials[-1].metric}={best[0]:.5f}")
    # OOF calibration on the last train year (inner model predictions)
    calib: dict[str, FamilyCalibrator] = {}
    rep: dict[str, Any] = {}
    raw_val: dict[str, np.ndarray] = {}
    kinds = fr.df["kind"].astype(str).to_numpy()
    for t in ("win", "stop"):
        m = val & target_mask(fr.df, fr.labels, t)
        s = predict_raw(inner[t], fr.x(m))
        y = target_values(fr.df[m], fr.labels, t)
        calib[t], rep[t] = fit_family(
            s,
            y,
            fr.weights[m],
            kinds[m],
            kind_ece_ratio=cfg.calibration.kind_ece_ratio,
            shrink_k=cfg.calibration.shrink_k,
        )
        raw_val[t] = s
    # refit every target on the full train window (the isotonic maps stay the ones fitted
    # on the inner model's out-of-fold scores; protocol rev 2, which shipped the inner
    # classifier instead, scored worse in 3 of 4 dev fold-years: docs/RESEARCH/ml-m2.md)
    boosters = {}
    for t in TARGETS:
        boosters[t], _ = fit_one(
            cfg, fr, t, points[t], train, None, rounds=rounds[t], monotone=monotone
        )
    fm = FoldModel(boosters, calib, points, rounds, 0.0, trials, rep, raw_val, monotone)
    tr_scores = score(fm, fr, train)
    fm.tau_stop = float(np.percentile(tr_scores["p_stop"], cfg.p_stop_pct))
    return fm


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def logistic_baseline(fr: Frame, train: np.ndarray, test: np.ndarray, target: str) -> np.ndarray:
    """L2 logistic regression on the same features (median-imputed + missing flags,
    standardised; categories one-hot). sklearn lives in the ``ml`` extra."""
    from sklearn.linear_model import LogisticRegression

    num = [f for f in fr.features if not f.startswith("cat_")]
    cats = [c for c in ds.CAND_CATEGORICAL]

    def mat(m: np.ndarray, med: pd.Series | None, mu: pd.Series | None, sd: pd.Series | None):
        d = fr.df.loc[m, num].apply(pd.to_numeric, errors="coerce").astype(float)
        miss = d.isna().astype(float).add_suffix("__na")
        if med is None:
            med = d.median()
        d = d.fillna(med).fillna(0.0)
        if mu is None:
            mu, sd = d.mean(), d.std().replace(0, 1.0).fillna(1.0)
        d = ((d - mu) / sd).clip(-8, 8)
        oh = []
        for c in cats:
            s = fr.df.loc[m, c].astype("string").fillna("__none__")
            vals = fr.vocab.get(c, [])
            oh.append(
                pd.DataFrame(
                    {f"{c}={x}": (s == x).astype(float).to_numpy() for x in vals}, index=d.index
                )
            )
        return pd.concat([d, miss, *oh], axis=1).to_numpy(), med, mu, sd

    mt = train & target_mask(fr.df, fr.labels, target)
    xtr, med, mu, sd = mat(mt, None, None, None)
    ytr = target_values(fr.df[mt], fr.labels, target)
    clf = LogisticRegression(C=1.0, max_iter=400, solver="lbfgs")
    clf.fit(xtr, ytr, sample_weight=fr.weights[mt])
    xte, *_ = mat(test, med, mu, sd)
    return clf.predict_proba(xte)[:, 1]


#: incumbent-ranker key columns (larger = better) reported as ranking baselines
RANK_BASELINES: dict[str, str] = {
    "managed_net_ev": "managed_ev_per_risk",
    "managed_net_ev_usd": "managed_net_ev",
    "rorc_day": "rorc_day",
    "credit_width": "credit_width",
    "debit_width": "debit_width",
    "ev_proxy": "ev_proxy_per_risk",
}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def eval_rows(
    fold: str,
    d: pd.DataFrame,
    s: pd.DataFrame,
    labels: dict[str, str],
    w: np.ndarray,
    base: dict[str, np.ndarray],
) -> pd.DataFrame:
    """Metric rows for every split × group slice of one test block."""
    yw = d[labels["win"]].astype(float).to_numpy()
    ys = d[labels["stop"]].astype(float).to_numpy()
    yr = d[labels["r"]].astype(float).to_numpy()
    dates = d["date"].tolist()
    rows = []
    for lab, m in v.group_slices(d):
        if m.sum() < 50:  # noqa: PLR2004
            continue
        dm = [dates[i] for i in np.flatnonzero(m)]
        r: dict[str, Any] = {"fold": fold, **lab, "n": int(m.sum()), "n_eff": v.kish_n_eff(w[m])}
        for k, val in v.prob_metrics(yw[m], s["p_win"].to_numpy()[m], w[m]).items():
            r[f"win_{k}"] = val
        for k, val in v.prob_metrics(ys[m], s["p_stop"].to_numpy()[m], w[m]).items():
            r[f"stop_{k}"] = val
        for name, p in base.items():
            if name.startswith("p_win:"):
                r[f"win_log_loss_{name[6:]}"] = v.log_loss(yw[m], p[m], w[m])
            elif name.startswith("p_stop:"):
                r[f"stop_log_loss_{name[7:]}"] = v.log_loss(ys[m], p[m], w[m])
        # the uncalibrated booster output: shows what the isotonic map added or cost
        for t, y in (("win", yw), ("stop", ys)):
            if f"raw_{t}" in s:
                raw = np.clip(s[f"raw_{t}"].to_numpy()[m], 1e-4, 1 - 1e-4)
                r[f"{t}_log_loss_raw"] = v.log_loss(y[m], raw, w[m])
                r[f"{t}_ece_raw"] = v.ece(y[m], raw, w[m])
        for k, val in v.rank_metrics(dm, s["e_r"].to_numpy()[m], yr[m]).items():
            r[f"er_{k}"] = val
        for name, col in RANK_BASELINES.items():
            if col in d:
                sc = pd.to_numeric(d[col], errors="coerce").to_numpy(dtype=float)[m]
                ic = v.daily_ic(dm, sc, yr[m])
                r[f"ic_{name}"] = float(ic.mean()) if len(ic) else float("nan")
        r["er_mean_pred"] = float(np.average(s["e_r"].to_numpy()[m], weights=w[m]))
        r["r_mean_real"] = float(np.average(yr[m], weights=w[m]))
        r["er_mix_gap"] = float(
            np.average(np.abs(s["e_r"].to_numpy()[m] - s["e_r_mix"].to_numpy()[m]), weights=w[m])
        )
        q10, q90 = s["r_q10"].to_numpy()[m], s["r_q90"].to_numpy()[m]
        r["q10_90_cover"] = float(np.average((yr[m] >= q10) & (yr[m] <= q90), weights=w[m]))
        rows.append(r)
    return pd.DataFrame(rows)


def reliability_rows(
    fold: str, d: pd.DataFrame, s: pd.DataFrame, labels: dict[str, str], w: np.ndarray
) -> pd.DataFrame:
    parts = []
    for tgt, col, lab in (("win", "p_win", labels["win"]), ("stop", "p_stop", labels["stop"])):
        for split, m in v.split_masks(d).items():
            t = v.reliability_table(
                d.loc[m, lab].astype(float).to_numpy(), s[col].to_numpy()[m], w[m]
            )
            parts.append(t.assign(fold=fold, target=tgt, split=split))
    return pd.concat(parts, ignore_index=True)


def promote_checks(metrics: pd.DataFrame) -> pd.DataFrame:
    """The pre-registered dev rule per test year, from the All/all rows (replay legs are
    added by :mod:`arc.ml.m2.replay`)."""
    out = []
    for fold, g in metrics[(metrics.split == "All") & (metrics.group == "all")].groupby("fold"):
        r = g.iloc[0]
        out.append(
            {
                "fold": fold,
                "ll_m2": r["win_log_loss"],
                "ll_logistic": r.get("win_log_loss_logistic"),
                "ll_managed_pop": r.get("win_log_loss_managed_pop"),
                "beats_logistic": bool(
                    r["win_log_loss"] < r.get("win_log_loss_logistic", math.inf)
                ),
                "beats_managed_pop": bool(
                    r["win_log_loss"] < r.get("win_log_loss_managed_pop", math.inf)
                ),
                "er_ic": r["er_ic_mean"],
                "er_ic_t": r["er_ic_nw_t"],
                "ic_pass": bool(r["er_ic_mean"] > 0 and r["er_ic_nw_t"] > 2),  # noqa: PLR2004
            }
        )
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Importance / PD / regimes
# ---------------------------------------------------------------------------


def gain_table(model: FoldModel, features: Sequence[str]) -> pd.DataFrame:
    rows = []
    for t, b in model.boosters.items():
        g = b.feature_importance(importance_type="gain")
        tot = float(g.sum()) or 1.0
        rows += [
            {"target": t, "feature": f, "gain_share": float(x) / tot}
            for f, x in zip(features, g, strict=True)
        ]
    return pd.DataFrame(rows)


def permutation_table(
    model: FoldModel, fr: Frame, test: np.ndarray, top: Sequence[str], fold: str, n: int = 40_000
) -> pd.DataFrame:
    """Rise in p_win log-loss and drop in e_r IC when one feature is shuffled (test fold)."""
    idx = np.flatnonzero(test)
    rng = np.random.default_rng(20261026)
    if len(idx) > n:
        idx = np.sort(rng.choice(idx, n, replace=False))
    d = fr.df.iloc[idx]
    w = fr.weights[idx]
    yw = d[fr.labels["win"]].astype(float).to_numpy()
    yr = d[fr.labels["r"]].astype(float).to_numpy()
    base = score(model, fr, df=d)
    ll0 = v.log_loss(yw, base["p_win"].to_numpy(), w)
    ic0 = v.daily_ic(d["date"].tolist(), base["e_r"].to_numpy(), yr).mean()
    rows = []
    for f in top:
        dd = d.copy()
        dd[f.removeprefix("cat_") if f.startswith("cat_") else f] = rng.permutation(
            dd[f.removeprefix("cat_") if f.startswith("cat_") else f].to_numpy()
        )
        s = score(model, fr, df=dd)
        rows.append(
            {
                "fold": fold,
                "feature": f,
                "win_ll_rise": v.log_loss(yw, s["p_win"].to_numpy(), w) - ll0,
                "er_ic_drop": float(
                    ic0 - v.daily_ic(dd["date"].tolist(), s["e_r"].to_numpy(), yr).mean()
                ),
            }
        )
    return pd.DataFrame(rows)


def partial_dependence(
    model: FoldModel, fr: Frame, mask: np.ndarray, feats: Sequence[str], n: int = 4000
) -> pd.DataFrame:
    idx = np.flatnonzero(mask)
    rng = np.random.default_rng(7)
    if len(idx) > n:
        idx = np.sort(rng.choice(idx, n, replace=False))
    d = fr.df.iloc[idx]
    rows = []
    for f in feats:
        if f.startswith("cat_"):
            col = f.removeprefix("cat_")
            grid = fr.vocab.get(col, [])[:12]
        else:
            col = f
            vals = pd.to_numeric(d[col], errors="coerce").dropna()
            if vals.empty:
                continue
            grid = sorted(set(np.round(np.quantile(vals, np.linspace(0.05, 0.95, 9)), 6)))
        for gv in grid:
            dd = d.copy()
            dd[col] = gv
            s = score(model, fr, df=dd)
            rows.append(
                {
                    "feature": f,
                    "value": str(gv),
                    "p_win": float(s["p_win"].mean()),
                    "e_r": float(s["e_r"].mean()),
                }
            )
    return pd.DataFrame(rows)


def regime_rows(
    fold: str, d: pd.DataFrame, s: pd.DataFrame, labels: dict[str, str], w: np.ndarray
) -> pd.DataFrame:
    rows = []
    yw = d[labels["win"]].astype(float).to_numpy()
    yr = d[labels["r"]].astype(float).to_numpy()
    for col in ("ctx_trend", "ctx_vol"):
        vals = d[col].astype("string").fillna("none").to_numpy()
        for val in sorted(set(vals)):
            m = vals == val
            if m.sum() < 500:  # noqa: PLR2004
                continue
            ic = v.daily_ic(d.loc[m, "date"].tolist(), s["e_r"].to_numpy()[m], yr[m])
            rows.append(
                {
                    "fold": fold,
                    "regime": f"{col}={val}",
                    "n": int(m.sum()),
                    "win_ece": v.ece(yw[m], s["p_win"].to_numpy()[m], w[m]),
                    "er_ic": float(ic.mean()) if len(ic) else float("nan"),
                    "er_bias": float(np.average(s["e_r"].to_numpy()[m] - yr[m], weights=w[m])),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def version_id(family: str, digest: str, cfg: M2Config) -> str:
    body = f"{family}|{digest}|{cfg.digest()}|{PROTOCOL_REV}"
    return "m2-" + hashlib.sha256(body.encode()).hexdigest()[:12]


def data_digest(fr: Frame) -> str:
    cols = ["date", "ticker", "key", *fr.labels.values()]
    h = pd.util.hash_pandas_object(fr.df[cols], index=False).to_numpy()
    return hashlib.sha256(h.tobytes()).hexdigest()[:16]


def write_model(vdir: Path, model: FoldModel, fr: Frame, meta: dict[str, Any]) -> dict[str, str]:
    """Booster texts, calibrators, feature list/groups/vocab and the manifest (+ sha256)."""
    from arc.ml.features.registry import registry
    from arc.ml.registry import hash_files

    (vdir / "lgbm").mkdir(parents=True, exist_ok=True)
    names = []
    for t, b in sorted(model.boosters.items()):
        p = vdir / "lgbm" / f"{t}.txt"
        p.write_text(b.model_to_string())
        names.append(f"lgbm/{t}.txt")
    (vdir / "calibrators.json").write_text(
        json.dumps(
            {t: c.model_dump(mode="json") for t, c in sorted(model.calib.items())},
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    reg = registry()
    groups = {f: (reg[f].group if f in reg else "candidate_ledger") for f in fr.features}
    (vdir / "features.json").write_text(json.dumps(fr.features, indent=1) + "\n")
    (vdir / "feature_groups.json").write_text(json.dumps(groups, indent=1, sort_keys=True) + "\n")
    (vdir / "vocab.json").write_text(json.dumps(fr.vocab, indent=1, sort_keys=True) + "\n")
    names += ["calibrators.json", "features.json", "feature_groups.json", "vocab.json"]
    files = hash_files(vdir, names)
    man = {
        **meta,
        "points": model.points,
        "rounds": model.rounds,
        "tau_stop": model.tau_stop,
        "monotone": model.monotone,
        "files": files,
    }
    (vdir / "manifest.json").write_text(
        json.dumps(man, indent=2, sort_keys=True, default=str) + "\n"
    )
    return files


@dataclass
class TrainRun:
    family: str
    version: str
    vdir: Path
    metrics: pd.DataFrame
    checks: pd.DataFrame
    timings: dict[str, float]


def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if r > 1e9 else r / 1e3  # macOS bytes, Linux KiB


def train_family(
    family: str,
    cfg: M2Config,
    *,
    ledger_root: Path,
    features_root: Path,
    out_root: Path,
    code_sha: str,
    now: dt.datetime,
    tickers: Sequence[str] | None = None,
    folds: Sequence[v.Fold] | None = None,
    monotone_check: bool = True,
    progress: Callable[[str], None] | None = None,
) -> TrainRun:
    """Walk-forward evaluation, final fit and registry artefacts for one family (dev only)."""
    from arc.ml.features.registry import schema_version

    say = progress or (lambda _m: None)
    t0 = time.monotonic()
    schema = schema_version()
    fr = build_frame(cfg, family, ledger_root, features_root, schema, tickers=tickers)
    digest = data_digest(fr)
    t_load = time.monotonic() - t0
    say(
        f"{family}: {len(fr.df)} rows, n_eff {v.kish_n_eff(fr.weights):.0f}, "
        f"{len(fr.features)} features, digest {digest}"
    )
    folds = tuple(folds or v.walk_forward())
    metrics, rel, regimes, trials, perms, oos = [], [], [], [], [], []
    fold_models: dict[str, FoldModel] = {}
    calib_reports = {}
    t_fit = 0.0
    for fold in folds:
        say(f"  fold {fold.name}")
        tf = time.monotonic()
        train = fold.train_mask(fr.df, fr.sessions)
        test = fold.test_mask(fr.df)
        fm = fit_fold(
            cfg,
            fr,
            train,
            dt.date(fold.test_start.year - 1, 1, 1),
            monotone=cfg.monotone_default,
            say=say,
        )
        t_fit += time.monotonic() - tf
        fold_models[fold.name] = fm
        calib_reports[fold.name] = fm.calib_report
        trials += [t.__dict__ | {"fold": fold.name} for t in fm.trials]
        d = fr.df[test]
        s = score(fm, fr, test)
        w = fr.weights[test]
        base = {
            "p_win:logistic": logistic_baseline(fr, train, test, "win"),
            "p_win:managed_pop": np.clip(
                pd.to_numeric(d["managed_pop"], errors="coerce").fillna(0.5).to_numpy(),
                1e-4,
                1 - 1e-4,
            ),
            "p_win:static_pop": np.clip(
                pd.to_numeric(d["static_pop"], errors="coerce").fillna(0.5).to_numpy(),
                1e-4,
                1 - 1e-4,
            ),
            "p_stop:logistic": logistic_baseline(fr, train, test, "stop"),
        }
        metrics.append(eval_rows(fold.name, d, s, fr.labels, w, base))
        rel.append(reliability_rows(fold.name, d, s, fr.labels, w))
        regimes.append(regime_rows(fold.name, d, s, fr.labels, w))
        oos.append(
            pd.concat(
                [
                    d[["date", "ticker", "key", "kind", "sleeve"]].reset_index(drop=True),
                    s.reset_index(drop=True),
                ],
                axis=1,
            ).assign(fold=fold.name)
        )
        if monotone_check:
            # the e_r regressor with the other monotone setting, same point and rounds
            alt_b, _ = fit_one(
                cfg,
                fr,
                "r",
                fm.points["r"],
                train,
                None,
                rounds=fm.rounds["r"],
                monotone=not cfg.monotone_default,
            )
            er_alt = predict_raw(alt_b, fr.x(test))
            ic = v.daily_ic(d["date"].tolist(), er_alt, d[fr.labels["r"]].to_numpy(dtype=float))
            metrics[-1].loc[
                (metrics[-1].split == "All") & (metrics[-1].group == "all"),
                "er_ic_mean_alt_monotone",
            ] = float(ic.mean())
            trials.append(
                {
                    "target": "r",
                    "point": fm.points["r"],
                    "monotone": not cfg.monotone_default,
                    "best_iter": fm.rounds["r"],
                    "metric": "test_ic",
                    "value": float(ic.mean()),
                    "seconds": 0.0,
                    "fold": fold.name,
                }
            )
        g = gain_table(fm, fr.features)
        top = (
            g[g.target.isin(["win", "r"])]
            .groupby("feature")
            .gain_share.sum()
            .sort_values(ascending=False)
            .index[:15]
        )
        perms.append(permutation_table(fm, fr, test, list(top), fold.name))
        say(f"  fold {fold.name} done ({time.monotonic() - tf:.0f}s, rss {_rss_mb():.0f} MB)")
    table = pd.concat(metrics, ignore_index=True)
    checks = promote_checks(table)

    # final model: every dev row at fold-2023's points; calibrators = fold-2023 test raw
    # scores of the fold model (out of sample for it)
    say("  final fit")
    tf = time.monotonic()
    last_name = folds[-1].name
    last = fold_models[last_name]
    every = np.ones(len(fr.df), dtype=bool)
    boosters = {
        t: fit_one(
            cfg,
            fr,
            t,
            last.points[t],
            every,
            None,
            rounds=last.rounds[t],
            monotone=cfg.monotone_default,
        )[0]
        for t in TARGETS
    }
    lt = folds[-1].test_mask(fr.df)
    kinds = fr.df["kind"].astype(str).to_numpy()
    calib, final_rep = {}, {}
    for t in ("win", "stop"):
        m = lt & target_mask(fr.df, fr.labels, t)
        sraw = predict_raw(last.boosters[t], fr.x(m))
        calib[t], final_rep[t] = fit_family(
            sraw,
            target_values(fr.df[m], fr.labels, t),
            fr.weights[m],
            kinds[m],
            kind_ece_ratio=cfg.calibration.kind_ece_ratio,
            shrink_k=cfg.calibration.shrink_k,
        )
    final = FoldModel(
        boosters, calib, last.points, last.rounds, 0.0, [], final_rep, {}, cfg.monotone_default
    )
    final.tau_stop = float(np.percentile(score(final, fr, every)["p_stop"], cfg.p_stop_pct))
    t_final = time.monotonic() - tf
    gain = gain_table(final, fr.features)
    topf = (
        gain[gain.target.isin(["win", "r"])]
        .groupby("feature")
        .gain_share.sum()
        .sort_values(ascending=False)
        .index[:8]
    )
    pdp = partial_dependence(final, fr, lt, list(topf))

    version = version_id(family, digest, cfg)
    vdir = out_root / family / version
    vdir.mkdir(parents=True, exist_ok=True)
    tp = time.monotonic()
    one_day = fr.df[fr.df["date"] == fr.df["date"].max()]
    score(final, fr, df=one_day)
    t_pred = time.monotonic() - tp
    timings = {
        "load_s": round(t_load, 1),
        "fold_fit_s": round(t_fit, 1),
        "final_fit_s": round(t_final, 1),
        "predict_one_day_s": round(t_pred, 3),
        "predict_one_day_rows": len(one_day),
        "total_s": round(time.monotonic() - t0, 1),
        "peak_rss_mb": round(_rss_mb(), 0),
    }
    m1_dirs = (
        sorted((features_root.parent / "models" / "m1").glob("m1-*"))
        if (features_root.parent / "models" / "m1").is_dir()
        else []
    )
    meta = {
        "version": version,
        "family": family,
        "profile": FAMILY_PROFILE[family],
        "rule": RULE_KIND,
        "protocol_rev": PROTOCOL_REV,
        "feature_schema": schema,
        "m1_version": m1_dirs[-1].name if m1_dirs else None,
        "m1_features_used": False,
        "label_slippage": cfg.label_x,
        "config": cfg.model_dump(mode="json"),
        "config_digest": cfg.digest(),
        "data_digest": digest,
        "code_sha": code_sha,
        "created": now.isoformat(),
        "train_window": {
            "start": "2020-01-01",
            "end": "2023-12-31",
            "purge": "label_end_date inside dev",
        },
        "calibration_window": {
            "start": str(folds[-1].test_start),
            "end": str(folds[-1].test_end),
            "source": f"fold {last_name} raw scores of the model trained before it",
        },
        "folds": [f.name for f in folds],
        "n_rows": len(fr.df),
        "n_eff": v.kish_n_eff(fr.weights),
        "sum_weights": float(fr.weights.sum()),
        "n_raw_ledger_rows": int(fr.df["inv_keep"].sum()),
        "n_raw_rows_per_day_cap": cfg.cap_per_day,
        "configs_tried": len(trials),
        "timings": timings,
        "holdout_reads": 0,
    }
    files = write_model(vdir, final, fr, meta)
    del files
    for name, frame in (
        ("metrics", table),
        ("checks", checks),
        ("reliability", pd.concat(rel, ignore_index=True)),
        ("regimes", pd.concat(regimes, ignore_index=True)),
        ("configs_tried", pd.DataFrame(trials)),
        ("permutation", pd.concat(perms, ignore_index=True)),
        ("gain", gain),
        ("pdp", pdp),
    ):
        frame.to_csv(vdir / f"eval_{name}.csv", index=False)
    (vdir / "eval_calibration.json").write_text(
        json.dumps({**calib_reports, "final": final_rep}, indent=1, default=float) + "\n"
    )
    (vdir / "fold_taus.json").write_text(
        json.dumps({k: m.tau_stop for k, m in fold_models.items()}, indent=1) + "\n"
    )
    pd.concat(oos, ignore_index=True).to_parquet(vdir / "oos_scores.parquet", index=False)
    for name, fm in fold_models.items():
        fd = vdir / "folds" / name
        (fd / "lgbm").mkdir(parents=True, exist_ok=True)
        for t, b in fm.boosters.items():
            (fd / "lgbm" / f"{t}.txt").write_text(b.model_to_string())
        (fd / "calibrators.json").write_text(
            json.dumps({t: c.model_dump(mode="json") for t, c in fm.calib.items()}, sort_keys=True)
            + "\n"
        )
        (fd / "meta.json").write_text(
            json.dumps(
                {"tau_stop": fm.tau_stop, "points": fm.points, "rounds": fm.rounds}, indent=1
            )
            + "\n"
        )
    say(
        f"{family}: {version} in {timings['total_s']:.0f}s, "
        f"peak rss {timings['peak_rss_mb']:.0f} MB"
    )
    return TrainRun(family, version, vdir, table, checks, timings)
