"""Surface → ``move_vs_implied`` (E27.6, the M1 side of the IV-surface challenger).

Question: does the per (ticker, day) IV surface (:mod:`arc.ml.dl.surface`) add to the
panel features when forecasting ``move_vs_implied_h21`` = ``|ln(C[t+21]/C[t])| /
(IV30_t·√(21/252))`` (the E27.2 label, :mod:`arc.ml.m1.targets`)?

Same net, same rows, same seeds, two arms:

* ``panel``: the ResNet-MLP on the M1 panel features (:func:`arc.ml.m1.train.model_features`);
* ``panel+surface``: the same plus the surface CNN embedding.

Rows: the E27.1 panel (dev only) with the E27.2 labels (OHLC cut at the dev end); test
years 2022 and 2023 with the E27.2 walk-forward purge (train rows dated before
``cutoff(test_start, h)`` = test start − h − 5 sessions; inner validation = the last
train year, purged the same way). The E27.2 published ``m1_move_vs_implied_h21``
(OOS, from that fold's M1) is reported alongside as the reference. Metrics per split
(All / Stocks / ETFs): per-day rank IC (+ Newey-West t) and MSE against the label.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from arc.ml import validate as v
from arc.ml.dl import prep
from arc.ml.dl.config import LossWeights, NetSpec
from arc.ml.dl.nets import HEADS
from arc.ml.dl.trainer import Batchable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from arc.ml.dl.config import DLConfig

R = HEADS.index("r")
LOSS = LossWeights(win=0.0, stop=0.0, r=1.0, quantiles=0.0, days=0.0, conditional=0.0)


def _metrics(arm: str, fold: str, d: pd.DataFrame, pred: np.ndarray, y: np.ndarray) -> list[dict]:
    rows = []
    etf = d["is_etf"].astype(bool).to_numpy()
    for split, m in (("All", np.ones(len(d), bool)), ("Stocks", ~etf), ("ETFs", etf)):
        ok = m & np.isfinite(pred) & np.isfinite(y)
        ic = v.daily_ic(d["date"].to_numpy()[ok].tolist(), pred[ok], y[ok])
        rows.append(
            {
                "arm": arm,
                "fold": fold,
                "split": split,
                "n": int(ok.sum()),
                "ic_mean": float(ic.mean()) if len(ic) else float("nan"),
                "ic_nw_t": v.newey_west_t(ic.to_numpy()) if len(ic) > 2 else float("nan"),  # noqa: PLR2004
                "mse": float(np.mean((pred[ok] - y[ok]) ** 2)) if ok.any() else float("nan"),
            }
        )
    return rows


def run(
    data_dir: Path,
    dl: DLConfig,
    *,
    tickers: Sequence[str] | None = None,
    workers: int = 6,
    say: Callable[[str], None] = lambda _m: None,
) -> pd.DataFrame:
    from arc.ml.dl import surface as sf
    from arc.ml.dl import trainer
    from arc.ml.features.registry import schema_version
    from arc.ml.m1.dataset import DEV_START, attach_targets, load_panel
    from arc.ml.m1.train import FOLDS, dev_sessions, model_features

    h = dl.m1_horizon
    target = f"move_vs_implied_h{h}"
    feats = model_features()
    panel = load_panel(
        data_dir / "ml" / "features", DEV_START, dt.date(2023, 12, 31), schema_version()
    )
    if tickers is not None:
        panel = panel[panel["ticker"].isin(list(tickers))]
    df = attach_targets(panel, data_dir, horizons=(h,)).reset_index(drop=True)
    df = df[np.isfinite(df[target].to_numpy(dtype=float))].reset_index(drop=True)
    say(f"m1-surface: {len(df)} rows with {target}")
    names = sorted(set(df["ticker"].astype(str)))
    sf.build_cache(data_dir, names, DEV_START, dt.date(2023, 12, 31), workers=workers, say=say)
    surf = sf.load_cache(data_dir, names)
    ref = None
    rp = data_dir / "ml" / "models" / "m1" / dl.m1_version / "oos_m1_features.parquet"
    if rp.is_file():
        ref = pd.read_parquet(rp, columns=["date", "ticker", f"m1_move_vs_implied_h{h}"])
        ref["date"] = pd.to_datetime(ref["date"]).dt.date
    sess = dev_sessions()
    si = {d: i for i, d in enumerate(sess)}
    pos = np.array([si.get(d, -1) for d in df["date"]])
    y = df[target].to_numpy(dtype=float)
    ymat = np.full((len(df), 4), np.nan)
    ymat[:, 2] = y
    w = np.ones(len(df))
    base = NetSpec(trunk="resmlp", seeds=dl.m1_seeds, max_epochs=20, patience=3, device="auto")
    rows: list[dict] = []
    for plan in FOLDS:
        cut = si[plan.test_cut(sess, h)]
        val_lo = dt.date(plan.test_start.year - 1, 1, 1)
        vcut = si[min(s for s in sess if s >= val_lo)] - h - 5
        train = (pos >= 0) & (pos < cut)
        fit = train & (pos < vcut)
        val = train & (pos >= si[min(s for s in sess if s >= val_lo)])
        test = np.array([plan.test_start <= d <= plan.test_end for d in df["date"]])
        inputs = prep.fit_inputs(df, fit, feats, {}, bins=dl.quantile_bins)
        z, miss, cat = prep.transform(inputs, df)
        c, s = sf.log_iv_stats(surf, val_lo)
        g = sf.gather_for(surf, df["ticker"].tolist(), df["date"].tolist(), c, s)
        cov = float(np.mean(g.pos[test] < len(surf))) if test.any() else 0.0
        d_te = df[test]
        for arm, spec in (
            ("panel", base),
            ("panel+surface", base.model_copy(update={"aux": "surface"})),
        ):
            full = Batchable(
                z,
                miss,
                cat,
                ymat,
                w,
                g if spec.aux == "surface" else None,
                g.shape if spec.aux == "surface" else None,
            )
            preds = []
            for k in range(spec.seeds):
                seed = dl.seed + 1000 * k
                inner = trainer.fit(
                    spec,
                    LOSS,
                    full.take(np.flatnonzero(fit)),
                    val=full.take(np.flatnonzero(val)),
                    seed=seed,
                    huber=1.0,
                    threads=dl.torch_threads,
                    cards=[],
                )
                final = trainer.fit(
                    spec,
                    LOSS,
                    full.take(np.flatnonzero(train)),
                    epochs=inner.epochs,
                    seed=seed,
                    huber=1.0,
                    threads=dl.torch_threads,
                    cards=[],
                )
                p = trainer.predict(
                    spec,
                    final.state,
                    full.take(np.flatnonzero(test)),
                    cards=[],
                    device=final.device,
                )[:, R]
                preds.append(p)
                rows += [
                    r | {"seed": k}
                    for r in _metrics(arm, plan.name, d_te, p, y[test])
                    if r["split"] == "All"
                ]
                say(f"  m1-surface {plan.name} {arm} seed {k}: epoch {inner.epochs}")
            rows += [
                r | {"seed": "ens", "surface_coverage": cov}
                for r in _metrics(arm, plan.name, d_te, np.mean(preds, axis=0), y[test])
            ]
        if ref is not None:
            j = d_te[["date", "ticker"]].merge(ref, on=["date", "ticker"], how="left")
            rows += [
                r | {"seed": "ref"}
                for r in _metrics(
                    f"m1 {dl.m1_version}",
                    plan.name,
                    d_te,
                    j[f"m1_move_vs_implied_h{h}"].to_numpy(dtype=float),
                    y[test],
                )
            ]
    return pd.DataFrame(rows)
