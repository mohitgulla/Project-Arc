"""M1 dev dataset (E27.2): feature-store rows + forward labels, dev window only.

Reads (read-only, all under ``data/``):

* ``ml/features/v<FEATURE_SCHEMA_VERSION>/date=<d>.parquet`` (E27.1 store) for
  sessions in [``start``, ``dev_end``];
* ``underlying_ohlc/<T>.parquet`` **cut at** ``dev_end`` before any label is computed,
  so a label never reads a holdout session (D84: dev = 2020-01-01 → 2023-12-31). Rows
  whose h-session window runs past ``dev_end`` get NaN labels and are dropped per target.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from arc.ml.features.adapters.history import load_ohlc
from arc.ml.features.compute import M1_HORIZONS, TRADING_DAYS
from arc.ml.features.store import date_path, version_dir
from arc.ml.m1.targets import ticker_targets
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

DEV_START = dt.date(2020, 1, 1)
DEV_END = dt.date(2023, 12, 31)
#: Holdout starts here (D84); nothing in this card reads a row on or after it.
HOLDOUT_START = dt.date(2024, 1, 1)


class HoldoutReadError(RuntimeError):
    """A dev-only M1 run asked for a session in the holdout."""


def load_panel(
    features_root: Path,
    start: dt.date,
    end: dt.date,
    schema: str | None = None,
    *,
    holdout_reason: str | None = None,
) -> pd.DataFrame:
    """Feature-store rows for every stored session in [start, end].

    Sessions in the D84 holdout are refused unless *holdout_reason* names the read
    (the D140 monthly retrain, whose rolling window moves past 2024-01-01).
    """
    if end >= HOLDOUT_START and not holdout_reason:
        msg = f"M1 dev dataset end {end} reaches the holdout (≥ {HOLDOUT_START}); D84"
        raise HoldoutReadError(msg)
    vdir = version_dir(features_root, schema)
    frames = []
    for d in sessions_between(start, end):
        p = date_path(vdir, d)
        if p.is_file():
            frames.append(pd.read_parquet(p))
    if not frames:
        msg = f"no feature panels in {vdir} for {start}..{end}; run `arc ml features build`"
        raise FileNotFoundError(msg)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = [x if isinstance(x, dt.date) else pd.Timestamp(x).date() for x in df["date"]]
    return df


def attach_targets(
    panel: pd.DataFrame,
    data_dir: Path,
    dev_end: dt.date = DEV_END,
    horizons: Sequence[int] = M1_HORIZONS,
    *,
    holdout_reason: str | None = None,
) -> pd.DataFrame:
    """*panel* + the M1 labels (``log_ret_h`` … ``end_h``) from OHLC cut at *dev_end*."""
    if dev_end >= HOLDOUT_START and not holdout_reason:
        msg = f"labels may not read past {DEV_END} (holdout from {HOLDOUT_START})"
        raise HoldoutReadError(msg)
    lo = min(panel["date"])
    parts = []
    for t, g in panel.groupby("ticker", sort=True):
        px = load_ohlc(data_dir, str(t), lo, dev_end)
        if px is None or px.empty:
            continue
        g = g.set_index("date")
        sig = g["iv30"].where(g["iv30"] > 0, g["rv20"])
        lab = ticker_targets(px, sigma_ref=sig, iv30=g["iv30"], horizons=horizons)
        lab = lab.reindex(g.index)
        lab["ticker"] = t
        parts.append(lab.reset_index(names="date"))
    labels = pd.concat(parts, ignore_index=True)
    return panel.merge(labels, on=["date", "ticker"], how="left", validate="one_to_one")


def scale(df: pd.DataFrame, h: int) -> np.ndarray:
    """Per row, the vol scale of the h-session target: HAR → rv20 → IV30 (× √(h/252))."""
    tau = np.sqrt(h / TRADING_DAYS)
    s = df[f"har_rv_h{h}"].to_numpy(dtype=float)
    for c in ("rv20", "iv30"):
        alt = df[c].to_numpy(dtype=float)
        s = np.where(np.isfinite(s) & (s > 0), s, alt)
    return np.where(np.isfinite(s) & (s > 0), s * tau, np.nan)
