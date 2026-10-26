"""Index closes for the backtest's market-regime knobs (card E28.6, D131).

The E28.6 regime-gate variant ("SMA50 with VIX percentile > 80") needs VIX's daily
closes over the whole backtest window. The live market-health job reads them from
Cboe's free ``VIX_History.csv`` (:func:`arc.ingest.options_data.fetch_index_closes`);
the backtest reads an offline copy, ``<data-dir>/market_index/<INDEX>.parquet``
(columns ``date``, ``close``), written once by ``scripts/fetch_vix_history.py``. The
sim never touches the network.

:func:`vix_high_days` applies the live ``vix_pct_1y`` definition
(:func:`arc.features.market_health.percentile_prior`: share of the prior ≤ 252 closes
strictly below the session's close, ≥ 120 prior observations) point in time: a
session's value reads closes on or before that session only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from arc.features.market_health import percentile_prior

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping, Sequence

__all__ = [
    "index_path",
    "read_index_closes",
    "vix_high_days",
    "vix_percentiles",
    "write_index_closes",
]


def index_path(data_dir: Path | str, index: str) -> Path:
    return Path(data_dir) / "market_index" / f"{index.upper()}.parquet"


def write_index_closes(data_dir: Path | str, index: str, closes: Mapping[dt.date, float]) -> Path:
    """Write ``{date: close}`` (sorted, positive closes only); returns the path."""
    p = index_path(data_dir, index)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted((d, float(v)) for d, v in closes.items() if v and v > 0)
    pd.DataFrame(rows, columns=["date", "close"]).to_parquet(p, index=False)
    return p


def read_index_closes(data_dir: Path | str, index: str) -> pd.Series | None:
    """The stored closes as a date-indexed series, or ``None`` when no file exists."""
    p = index_path(data_dir, index)
    if not p.is_file():
        return None
    df = pd.read_parquet(p)
    s = pd.Series(
        pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float),
        index=[pd.Timestamp(d).date() for d in df["date"]],
        name="close",
    )
    return s.dropna().sort_index()


def vix_percentiles(closes: pd.Series, sessions: Sequence[dt.date]) -> dict[dt.date, float | None]:
    """VIX 1-year percentile per session (``None`` = no close that day or < 120 prior)."""
    s = closes.dropna().sort_index()
    idx = list(s.index)
    vals = [float(v) for v in s.to_numpy()]
    pos = {d: i for i, d in enumerate(idx)}
    out: dict[dt.date, float | None] = {}
    for d in sessions:
        i = pos.get(d)
        out[d] = None if i is None else percentile_prior(vals[: i + 1])
    return out


def vix_high_days(
    closes: pd.Series, sessions: Sequence[dt.date], above: float
) -> dict[dt.date, bool]:
    """``True`` when the session's VIX percentile is strictly above *above*.

    A session without a percentile counts as high (fail closed, like the live regime
    rule's missing input), so a VIX gap can only block, never open, short premium.
    """
    pct = vix_percentiles(closes, sessions)
    return {d: (p is None or p > above) for d, p in pct.items()}
