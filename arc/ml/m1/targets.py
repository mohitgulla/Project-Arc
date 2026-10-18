"""M1 targets (E27.2, D126): the forward labels per (ticker, day t, horizon h).

Every label at t reads the ticker's own sessions t+1 … t+h (forward by design) and
nothing after t+h; a row whose window runs past the last available session is NaN. The
dev loader cuts the OHLC at the dev end before calling these, so no label ever reads a
holdout session (D84).

For horizon *h* (sessions of the ticker's own row index, split-adjusted closes):

* ``log_ret_h``: ``ln(C[t+h] / C[t])``.
* ``rv_h``: realised vol over (t, t+h], annualised, zero-mean:
  ``√(252/h · Σ_{i=1..h} r²_{t+i})`` with ``r`` the daily log close return (the HAR
  target definition of :func:`arc.ml.features.compute.har_forecast`).
* ``max_up_h`` / ``max_dn_h``: the largest excursion above / below ``C[t]`` over the path
  (``max ln(H[t+i]/C[t])`` and ``−min ln(L[t+i]/C[t])``, both ≥ 0 when the path crosses),
  in σ units: divided by ``σ_t·√(h/252)``, σ_t = IV30 at t when known, else rv20 at t.
* ``move_vs_implied_h``: ``|log_ret_h| / (IV30_t·√(h/252))`` (NaN without an IV30).
* ``end_h``: the session date t+h (the label end; the walk-forward purge reads it).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from arc.ml.features.compute import M1_HORIZONS, TRADING_DAYS

if TYPE_CHECKING:
    from collections.abc import Sequence

TARGET_KINDS: tuple[str, ...] = ("log_ret", "rv", "max_up", "max_dn", "move_vs_implied")


def target_names(horizons: Sequence[int] = M1_HORIZONS) -> list[str]:
    return [f"{k}_h{h}" for h in horizons for k in TARGET_KINDS]


def _fwd_window(a: np.ndarray, h: int) -> np.ndarray:
    """(n, h) array: row t holds a[t+1 … t+h] (NaN past the end)."""
    n = len(a)
    out = np.full((n, h), np.nan)
    for i in range(1, min(h, n - 1) + 1):
        out[: n - i, i - 1] = a[i:]
    return out


def ticker_targets(
    px: pd.DataFrame,
    sigma_ref: pd.Series | None = None,
    iv30: pd.Series | None = None,
    horizons: Sequence[int] = M1_HORIZONS,
) -> pd.DataFrame:
    """The M1 labels of one ticker, indexed like *px* (``high low close`` by date).

    *sigma_ref*: the annualised vol known at t that scales the excursions (IV30, else
    rv20); *iv30*: IV30 at t (``move_vs_implied``). Both are features dated t.
    """
    px = px.sort_index()
    c = px["close"].astype(float).to_numpy()
    hi = px["high"].astype(float).to_numpy() if "high" in px else c
    lo = px["low"].astype(float).to_numpy() if "low" in px else c
    n = len(c)
    lc = np.log(c)
    r = np.diff(lc, prepend=np.nan)  # r[i] = ln(C[i]/C[i-1])
    idx = px.index
    sig = (
        np.full(n, np.nan) if sigma_ref is None else sigma_ref.reindex(idx).astype(float).to_numpy()
    )
    iv = np.full(n, np.nan) if iv30 is None else iv30.reindex(idx).astype(float).to_numpy()
    dates = np.array(list(idx), dtype=object)
    out: dict[str, np.ndarray] = {}
    for h in horizons:
        tau = math.sqrt(h / TRADING_DAYS)
        rw = _fwd_window(r, h)
        complete = np.isfinite(rw).all(axis=1)
        lr = np.full(n, np.nan)
        if n > h:
            lr[: n - h] = lc[h:] - lc[: n - h]
        lr[~complete] = np.nan
        rv = np.sqrt(TRADING_DAYS / h * np.nansum(rw**2, axis=1))
        rv[~complete] = np.nan
        up = np.nanmax(np.log(_fwd_window(hi, h) / c[:, None]), axis=1, initial=-np.inf)
        dn = -np.nanmin(np.log(_fwd_window(lo, h) / c[:, None]), axis=1, initial=np.inf)
        up[~complete] = np.nan
        dn[~complete] = np.nan
        scale = sig * tau
        with np.errstate(divide="ignore", invalid="ignore"):
            out[f"log_ret_h{h}"] = lr
            out[f"rv_h{h}"] = rv
            out[f"max_up_h{h}"] = np.where(scale > 0, up / scale, np.nan)
            out[f"max_dn_h{h}"] = np.where(scale > 0, dn / scale, np.nan)
            out[f"move_vs_implied_h{h}"] = np.where(iv > 0, np.abs(lr) / (iv * tau), np.nan)
        end = np.full(n, None, dtype=object)
        if n > h:
            end[: n - h] = dates[h:]
        end[~complete] = None
        out[f"end_h{h}"] = end
    return pd.DataFrame(out, index=idx)
