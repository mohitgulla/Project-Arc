"""Real-data parity of the two adapters (E27.1): live store vs history store on one day.

Both panels come from the same :func:`~arc.ml.features.compute.compute_panel`; any
difference is an **input** difference. Features fed by the same facts (daily OHLC, the
macro calendar, the detected earnings events) must agree to :data:`EXACT_TOL`.
Option-derived features read a different vendor live (Alpaca/Cboe) than in history
(ThetaData EOD), so they carry a documented tolerance in :data:`SOURCE_TOL`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping, Sequence

EXACT_TOL = 1e-9

#: feature -> (abs tolerance, reason). Everything not listed must match to EXACT_TOL.
SOURCE_TOL: dict[str, tuple[float, str]] = {
    "iv30": (0.03, "live iv_daily (Alpaca cm30) vs ThetaData EOD cm30: different quotes/time"),
    "vix_proxy": (0.03, "SPY iv30, same vendor difference as iv30"),
    "iv_rank": (0.15, "live iv_daily mixes forward + backfill sources; rank is window-sensitive"),
    "iv_pct": (0.15, "as iv_rank"),
    "vix_proxy_pct_252": (0.15, "as iv_rank, on SPY"),
    "vrp20": (0.03, "iv30 difference passes through (rv20 is exact)"),
    "iv30_over_har": (0.15, "iv30 difference passes through (HAR is exact)"),
    "atm_spread_pct": (0.10, "live = Cboe delayed ATM book at the last scan; history = EOD"),
    "opt_volume": (np.inf, "live = Cboe session-to-date volume at the last scan, before the close"),
    "put_call_vol": (np.inf, "as opt_volume"),
    "opt_vol_ratio_20": (np.inf, "live needs 20 sessions of snapshots"),
    "sess_to_earn": (np.inf, "live uses the Finnhub date (earn_src=2), history projects (1); "
                     "compared only where earn_src agrees"),
    "earn_src": (np.inf, "2 live when Finnhub knows the date; history has no calendar"),
}  # fmt: skip


def input_diffs(
    live: Mapping[str, pd.DataFrame], hist: Mapping[str, pd.DataFrame], end: dt.date
) -> pd.DataFrame:
    """Per ticker: OHLC input rows (≤ *end*, common dates) that differ beyond 1e-9 relative.

    A non-exact price feature traces back to one of these rows (e.g. a vendor revising a
    daily bar after the history pull), not to a formula.
    """
    rows = []
    for t in sorted(set(live) & set(hist)):
        a, b = live[t], hist[t]
        idx = a.index.intersection(b.index)
        idx = idx[idx <= end]
        if not len(idx):
            continue
        cols = [c for c in ("open", "high", "low", "close") if c in a and c in b]
        rel = ((a.loc[idx, cols] - b.loc[idx, cols]).abs() / b.loc[idx, cols].abs()).max(axis=1)
        bad = rel[rel > EXACT_TOL]
        if len(bad):
            rows.append(
                {
                    "ticker": t,
                    "rows": len(bad),
                    "first": min(bad.index),
                    "last": max(bad.index),
                    "max_rel_diff": float(bad.max()),
                }
            )
    return pd.DataFrame(rows, columns=["ticker", "rows", "first", "last", "max_rel_diff"])


def compare_panels(live: pd.DataFrame, hist: pd.DataFrame, features: Sequence[str]) -> pd.DataFrame:
    """Per feature: rows with both values, max abs diff, tolerance, verdict, reason."""
    key = ["date", "ticker"]
    a = live.set_index(key)
    b = hist.set_index(key)
    idx = a.index.intersection(b.index)
    a, b = a.loc[idx], b.loc[idx]
    same_src = (a["earn_src"] == b["earn_src"]) if "earn_src" in a else pd.Series(True, idx)
    rows = []
    for f in features:
        x = pd.to_numeric(a[f], errors="coerce").astype(float)
        y = pd.to_numeric(b[f], errors="coerce").astype(float)
        both = x.notna() & y.notna()
        if f == "sess_to_earn":
            both &= same_src
        tol, reason = SOURCE_TOL.get(f, (EXACT_TOL, ""))
        diff = (x - y).abs()[both]
        mx = float(diff.max()) if len(diff) else float("nan")
        ok = bool(len(diff) == 0 or mx <= tol)
        rows.append(
            {
                "feature": f,
                "both": int(both.sum()),
                "live_only": int((x.notna() & y.isna()).sum()),
                "hist_only": int((x.isna() & y.notna()).sum()),
                "max_abs_diff": mx,
                "tol": tol,
                "ok": ok,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows)
