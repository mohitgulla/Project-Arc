"""E27.6 challenger runs on the E27.3 harness (D126): folds, scoring, the stacker.

Per family (credit, debit) the frame is the registered M2 version's own
(:func:`family_frame`: same ledger rows, subsample, weights, sessions, features and
vocab; the data digest must match the version's manifest). Per walk-forward fold
(:func:`arc.ml.validate.walk_forward`) and challenger:

1. inner split exactly like M2 (:func:`arc.ml.validate.inner_split`, validation block =
   the last train year, inner-train rows purged + embargoed against it);
2. quantile normaliser and aux scalers fitted on the inner-train rows;
3. per seed: early-stopped fit on inner train / validation, then a refit on every train
   row for the best epoch count (M2's refit rule);
4. the seed ensemble's raw ``win`` / ``stop`` outputs on the validation block fit the
   isotonic maps (:func:`arc.ml.calibrate.fit_family`, M2's settings), and ``τ_stop`` is
   the M2 percentile of the train rows' calibrated ``p_stop``;
5. test-year rows are scored (metrics) and so are the test year's replay rows
   (the ledger rows that pass the ranking filters, the population M2's replays score).

The stacker (``stack``) blends M2 and the fold's best DL challenger (highest ``e_r``
per-day IC on that fold's inner-validation block, so the choice reads no test label)
with logistic regressions for ``win`` / ``stop`` (inputs: the two models' logits) and
a linear regression for ``e_r``. It is fitted strictly ex ante on out-of-fold scores:
each calendar quarter of a test year is scored by a blend fitted on the OOS rows (the
earlier test years plus the earlier quarters) whose label closed more than the embargo
before the quarter starts; with fewer than :data:`STACK_MIN_ROWS` such rows the blend is
the plain average (:data:`STACK_PRIOR`). The stack's ``τ_stop`` is the M2 percentile of
the stacked ``p_stop`` over the earlier test years (M2's own fold τ on the first fold).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from arc.ml import validate as v
from arc.ml.dl import prep
from arc.ml.dl.nets import HEADS
from arc.ml.dl.trainer import Batchable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from arc.ml.calibrate import FamilyCalibrator
    from arc.ml.dl.config import DLConfig, NetSpec
    from arc.ml.m2.config import M2Config
    from arc.ml.m2.train import Frame

J = {h: i for i, h in enumerate(HEADS)}
REPLAY_COLS = ("date", "ticker", "key")


class DigestMismatchError(RuntimeError):
    """The rebuilt frame is not the registered M2 version's (different rows)."""


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def m2_manifest(models_root: Path, family: str, version: str) -> dict[str, Any]:
    return json.loads((models_root / family / version / "manifest.json").read_text())


def family_frame(
    data_dir: Path,
    family: str,
    version: str,
    models_root: Path,
    *,
    tickers: Sequence[str] | None = None,
) -> tuple[Frame, M2Config, dict[str, Any]]:
    """The registered M2 version's frame, rebuilt (digest checked on the full universe)."""
    from arc.ml.features.registry import schema_version
    from arc.ml.m2.config import M2Config
    from arc.ml.m2.train import build_frame, data_digest

    man = m2_manifest(models_root, family, version)
    schema = schema_version()
    if man["feature_schema"] != schema:
        msg = f"{family}/{version}: feature schema {man['feature_schema']} != {schema}"
        raise DigestMismatchError(msg)
    cfg = M2Config.model_validate(man["config"])
    fr = build_frame(
        cfg,
        family,
        data_dir / "ml" / "ledger",
        data_dir / "ml" / "features",
        schema,
        tickers=tickers,
    )
    if tickers is None and data_digest(fr) != man["data_digest"]:
        msg = f"{family}: frame digest {data_digest(fr)} != {version}'s {man['data_digest']}"
        raise DigestMismatchError(msg)
    if list(fr.features) != json.loads(
        (models_root / family / version / "features.json").read_text()
    ):
        msg = f"{family}: feature list differs from {version}'s"
        raise DigestMismatchError(msg)
    return fr, cfg, man


def target_matrix(fr: Frame) -> np.ndarray:
    """``(n, 4)``: win, stop, r, days (NaN = missing)."""
    from arc.ml.m2.train import target_values

    return np.column_stack(
        [target_values(fr.df, fr.labels, t) for t in ("win", "stop", "r", "days")]
    ).astype(np.float64)


# ---------------------------------------------------------------------------
# Aux inputs (sequence / surface)
# ---------------------------------------------------------------------------


@dataclass
class AuxSource:
    """Raw aux data for every frame ticker; :meth:`gather` fits its scaler on the rows
    dated before *before* only."""

    kind: str  # "none" | "sequence" | "surface"
    seq: Any = None  # sequence.SequenceTable
    surf: pd.DataFrame | None = None
    window: int = 60

    def gather(
        self, tickers: Sequence[object], dates: Sequence[object], before: dt.date
    ) -> tuple[Callable[[np.ndarray], np.ndarray] | None, tuple[int, ...] | None, dict]:
        from arc.ml.dl import sequence as sq
        from arc.ml.dl import surface as sf

        if self.kind == "sequence":
            scaler = sq.ChannelScaler.fit(sq.train_vectors(self.seq, before))
            g = sq.gather_for(self.seq, tickers, dates, self.window, scaler)
            cov = float(np.mean(g.pos >= 0)) if len(g.pos) else 0.0
            return g, g.shape, {"scaler": scaler.model_dump(), "coverage": cov}
        if self.kind == "surface":
            assert self.surf is not None
            c, s = sf.log_iv_stats(self.surf, before)
            g = sf.gather_for(self.surf, tickers, dates, c, s)
            cov = float(np.mean(g.pos < len(self.surf))) if len(g.pos) else 0.0
            return g, g.shape, {"center": c, "scale": s, "coverage": cov}
        return None, None, {}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def outputs(
    raw: np.ndarray, calib: dict[str, FamilyCalibrator], kinds: np.ndarray, index: Any = None
) -> pd.DataFrame:
    """M2's output columns (:func:`arc.ml.m2.predict.score_rows`) from raw head outputs."""
    out = pd.DataFrame(index=index if index is not None else np.arange(len(raw)))
    out["p_win"] = calib["win"].predict(raw[:, J["win"]], kinds)
    out["p_stop"] = calib["stop"].predict(raw[:, J["stop"]], kinds)
    out["e_r"] = raw[:, J["r"]]
    qs = np.sort(np.vstack([raw[:, J["q10"]], raw[:, J["q50"]], raw[:, J["q90"]]]), axis=0)
    out["r_q10"], out["r_q50"], out["r_q90"] = qs[0], qs[1], qs[2]
    out["e_days"] = np.maximum(raw[:, J["days"]], 0.0)
    out["e_r_mix"] = out["p_win"] * raw[:, J["r_win"]] + (1.0 - out["p_win"]) * raw[:, J["r_loss"]]
    out["raw_win"] = raw[:, J["win"]]
    out["raw_stop"] = raw[:, J["stop"]]
    return out


@dataclass
class FoldFit:
    """One challenger on one fold (states per seed + calibration)."""

    name: str
    fold: str
    spec: NetSpec
    inputs: prep.TabularInputs
    states: list[dict[str, Any]]
    epochs: list[int]
    calib: dict[str, FamilyCalibrator]
    seed_calib: list[dict[str, FamilyCalibrator]]
    tau_stop: float
    val_ic: float
    aux_meta: dict[str, Any]
    device: str
    seconds: float
    cards: list[int]
    val_curves: list[list[float]] = field(default_factory=list)


def _calibrate(
    raw_val: np.ndarray, y: np.ndarray, w: np.ndarray, kinds: np.ndarray, cfg: M2Config
) -> dict[str, FamilyCalibrator]:
    from arc.ml.calibrate import fit_family

    out = {}
    for t, col in (("win", 0), ("stop", 1)):
        m = np.isfinite(y[:, col])
        out[t], _ = fit_family(
            raw_val[m, J[t]],
            y[m, col],
            w[m],
            kinds[m],
            kind_ece_ratio=cfg.calibration.kind_ece_ratio,
            shrink_k=cfg.calibration.shrink_k,
        )
    return out


def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(q / (1 - q))


def predict_rows(
    ff: FoldFit,
    df: pd.DataFrame,
    aux: AuxSource,
    *,
    before: dt.date,
    per_seed: bool = False,
) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    """Ensemble outputs (and optionally each seed's) for arbitrary candidate rows."""
    from arc.ml.dl import trainer

    z, miss, cat = prep.transform(ff.inputs, df)
    g, shape, _ = aux.gather(df["ticker"].tolist(), df["date"].tolist(), before)
    n = len(df)
    d = Batchable(z, miss, cat, np.full((n, 4), np.nan), np.ones(n), g, shape)
    raws = [trainer.predict(ff.spec, s, d, cards=ff.cards, device=ff.device) for s in ff.states]
    kinds = df["kind"].astype(str).to_numpy()
    ens = outputs(np.mean(raws, axis=0), ff.calib, kinds, df.index)
    seeds = (
        [outputs(r, c, kinds, df.index) for r, c in zip(raws, ff.seed_calib, strict=True)]
        if per_seed
        else []
    )
    return ens, seeds


def fit_fold(
    name: str,
    spec: NetSpec,
    dl: DLConfig,
    cfg: M2Config,
    fr: Frame,
    fold: v.Fold,
    aux: AuxSource,
    *,
    say: Callable[[str], None] = lambda _m: None,
) -> tuple[FoldFit, pd.DataFrame, list[pd.DataFrame]]:
    """Fit *name* on *fold*; returns the fit + calibrated test-row scores (ensemble,
    per seed), index-aligned to ``fr.df[test]``."""
    from arc.ml.dl import trainer

    t0 = time.monotonic()
    train = fold.train_mask(fr.df, fr.sessions)
    test = fold.test_mask(fr.df)
    val_start = dt.date(fold.test_start.year - 1, 1, 1)
    fit, val = v.inner_split(fr.df, train, fr.sessions, val_start=val_start)
    inputs = prep.fit_inputs(fr.df, fit, fr.features, fr.vocab, bins=dl.quantile_bins)
    z, miss, cat = prep.transform(inputs, fr.df)
    g, shape, meta = aux.gather(fr.df["ticker"].tolist(), fr.df["date"].tolist(), val_start)
    y = target_matrix(fr)
    full = Batchable(z, miss, cat, y, fr.weights.astype(np.float64), g, shape)
    i_fit, i_val = np.flatnonzero(fit), np.flatnonzero(val)
    i_tr, i_te = np.flatnonzero(train), np.flatnonzero(test)
    cards = inputs.cardinalities
    kinds = fr.df["kind"].astype(str).to_numpy()
    states, epochs, curves, raw_val, raw_te, raw_tr = [], [], [], [], [], []
    seed_cal = []
    device = "cpu"
    for k in range(spec.seeds):
        seed = dl.seed + 1000 * k
        inner = trainer.fit(
            spec,
            dl.loss,
            full.take(i_fit),
            val=full.take(i_val),
            seed=seed,
            huber=cfg.huber_alpha,
            threads=dl.torch_threads,
            cards=cards,
            say=say,
        )
        rv = trainer.predict(spec, inner.state, full.take(i_val), cards=cards, device=inner.device)
        final = trainer.fit(
            spec,
            dl.loss,
            full.take(i_tr),
            epochs=inner.epochs,
            seed=seed,
            huber=cfg.huber_alpha,
            threads=dl.torch_threads,
            cards=cards,
        )
        device = final.device
        states.append(final.state)
        epochs.append(inner.epochs)
        curves.append(inner.val_curve)
        raw_val.append(rv)
        raw_te.append(
            trainer.predict(spec, final.state, full.take(i_te), cards=cards, device=device)
        )
        raw_tr.append(
            trainer.predict(spec, final.state, full.take(i_tr), cards=cards, device=device)
        )
        seed_cal.append(_calibrate(rv, y[i_val], fr.weights[i_val], kinds[i_val], cfg))
        say(f"    {name} seed {k}: epoch {inner.epochs} ({time.monotonic() - t0:.0f}s, {device})")
    rv_ens = np.mean(raw_val, axis=0)
    calib = _calibrate(rv_ens, y[i_val], fr.weights[i_val], kinds[i_val], cfg)
    tr_scores = outputs(np.mean(raw_tr, axis=0), calib, kinds[i_tr])
    tau = float(np.percentile(tr_scores["p_stop"], cfg.p_stop_pct))
    ic = v.daily_ic(fr.df["date"].to_numpy()[i_val].tolist(), rv_ens[:, J["r"]], y[i_val, 2])
    idx = fr.df.index[i_te]
    ens = outputs(np.mean(raw_te, axis=0), calib, kinds[i_te], idx)
    seeds = [outputs(r, c, kinds[i_te], idx) for r, c in zip(raw_te, seed_cal, strict=True)]
    ff = FoldFit(
        name,
        fold.name,
        spec,
        inputs,
        states,
        epochs,
        calib,
        seed_cal,
        tau,
        float(ic.mean()) if len(ic) else float("nan"),
        meta,
        device,
        time.monotonic() - t0,
        cards,
        curves,
    )
    return ff, ens, seeds


# ---------------------------------------------------------------------------
# Stacker
# ---------------------------------------------------------------------------


def quarter_blocks(dates: Sequence[dt.date], year: int) -> list[tuple[dt.date, dt.date]]:
    del dates
    return [
        (dt.date(year, 1, 1), dt.date(year, 3, 31)),
        (dt.date(year, 4, 1), dt.date(year, 6, 30)),
        (dt.date(year, 7, 1), dt.date(year, 9, 30)),
        (dt.date(year, 10, 1), dt.date(year, 12, 31)),
    ]


def _stack_x(m2: pd.DataFrame, dl: pd.DataFrame, target: str) -> np.ndarray:
    if target in ("win", "stop"):
        c = f"p_{target}"
        return np.column_stack([_logit(m2[c].to_numpy()), _logit(dl[c].to_numpy())])
    return np.column_stack([m2["e_r"].to_numpy(), dl["e_r"].to_numpy()])


@dataclass
class Stacker:
    """Per quarter block: coefficients fitted only on rows whose label ended before the
    block started (minus the embargo): earlier OOS years plus earlier quarters."""

    blocks: list[tuple[dt.date, dt.date]]
    coefs: dict[str, list[list[float]]]  # target -> per block [intercept, b_m2, b_dl]
    n_fit: list[int] = field(default_factory=list)

    def apply(self, dates: np.ndarray, m2: pd.DataFrame, dl: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=m2.index)
        n = len(m2)
        for t in ("win", "stop", "r"):
            x = _stack_x(m2, dl, t)
            pred = np.full(n, np.nan)
            for b, (lo, hi) in enumerate(self.blocks):
                m = (dates >= lo) & (dates <= hi)
                c = np.asarray(self.coefs[t][b])
                lin = c[0] + x[m] @ c[1:]
                pred[m] = 1 / (1 + np.exp(-lin)) if t != "r" else lin
            out["p_win" if t == "win" else "p_stop" if t == "stop" else "e_r"] = pred
        return out


#: blend used when fewer than :data:`STACK_MIN_ROWS` ex-ante rows exist (plain average
#: of the two models' logits / e_r)
STACK_PRIOR = [0.0, 0.5, 0.5]
STACK_MIN_ROWS = 1000


def fit_stacker(
    dates: np.ndarray,
    label_end: np.ndarray,
    sessions: Sequence[dt.date],
    m2: pd.DataFrame,
    dl: pd.DataFrame,
    y: np.ndarray,
    w: np.ndarray,
    year: int,
) -> Stacker:
    """Expanding, strictly ex-ante blend (see the module doc).

    The rows passed may span earlier OOS test years plus the test year *year*; block *b*
    of *year* is scored by a blend fitted on the rows whose ``label_end`` falls more than
    the embargo before the block's first session. No row of the block or after it (and no
    label still open at its start) is read.
    """
    from sklearn.linear_model import LinearRegression, LogisticRegression

    blocks = quarter_blocks([], year)
    coefs: dict[str, list[list[float]]] = {"win": [], "stop": [], "r": []}
    n_fit = []
    sidx = {d: i for i, d in enumerate(sessions)}
    ei = np.array([sidx.get(d, len(sessions)) for d in label_end])
    xs = {t: _stack_x(m2, dl, t) for t in ("win", "stop", "r")}
    del dates
    for lo, _hi in blocks:
        b0 = sidx[min(s for s in sessions if s >= lo)]
        keep = ei < b0 - v.EMBARGO
        n_fit.append(int(keep.sum()))
        for t, col in (("win", 0), ("stop", 1), ("r", 2)):
            x = xs[t]
            ok = keep & np.isfinite(y[:, col]) & np.isfinite(x).all(axis=1)
            if ok.sum() < STACK_MIN_ROWS or (t != "r" and len(np.unique(y[ok, col])) < 2):  # noqa: PLR2004
                coefs[t].append(list(STACK_PRIOR))
                continue
            if t == "r":
                lr = LinearRegression().fit(x[ok], y[ok, col], sample_weight=w[ok])
                coefs[t].append([float(lr.intercept_), *map(float, lr.coef_)])
            else:
                lg = LogisticRegression(C=1.0, max_iter=400).fit(
                    x[ok], y[ok, col].astype(int), sample_weight=w[ok]
                )
                coefs[t].append([float(lg.intercept_[0]), *map(float, lg.coef_[0])])
    return Stacker(blocks, coefs, n_fit)


def stacked_outputs(m2: pd.DataFrame, dl: pd.DataFrame, st: Stacker, dates: np.ndarray):
    """Full output frame for the stack: blended p_win / p_stop / e_r, the rest from M2."""
    b = st.apply(dates, m2, dl)
    out = m2.copy()
    for c in ("p_win", "p_stop", "e_r"):
        out[c] = b[c].to_numpy()
    out["p_win"] = np.clip(out["p_win"], 1e-4, 1 - 1e-4)
    out["p_stop"] = np.clip(out["p_stop"], 1e-4, 1 - 1e-4)
    return out


# ---------------------------------------------------------------------------
# Replay rows
# ---------------------------------------------------------------------------


def replay_rows(
    data_dir: Path,
    family: str,
    year: int,
    cfg: M2Config,
    tickers: Sequence[str] | None = None,
) -> pd.DataFrame:
    """The test year's ledger rows that pass the ranking filters, with features (the
    population :func:`arc.ml.m2.pipeline.scored_rows` scores for M2's replays)."""
    from arc.ml.features.registry import schema_version
    from arc.ml.m2 import dataset as ds
    from arc.ml.m2.config import FAMILY_PROFILE
    from arc.ml.m2.train import panel_columns

    rows = ds.load_family(
        data_dir / "ml" / "ledger",
        FAMILY_PROFILE[family],
        [year],
        cfg.label_x,
        cap_per_day=None,
        filter_rows="passes_filters",
        for_scoring=True,
        tickers=tickers,
    )
    pcols = panel_columns()
    panel = ds.load_panel_range(
        data_dir / "ml" / "features",
        dt.date(year, 1, 1),
        dt.date(year, 12, 31),
        schema_version(),
        pcols,
    )
    return ds.attach_features(rows, panel, pcols).reset_index(drop=True)
