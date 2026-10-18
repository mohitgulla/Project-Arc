"""Point-in-time correlation matrices for the backtest (E28.4, D131).

The live ``betas`` job stores, on row day *D*, each pair's correlation of the last 60
aligned daily log returns of closes **strictly before** *D* (it runs pre-market).
:func:`pit_matrix` rebuilds exactly that from cached daily closes for a sim
session, through the same :func:`arc.betas.corr_store.compute_rows`, so the sim's
cluster cap (E28.5/E28.6) sees what the live gate would have seen that morning: no
close on or after *D* is ever read.

The cached underlying closes (:mod:`arc.backtest.underlying`) are raw, while the live
job reads split/dividend-adjusted bars; a split inside the window therefore shows
as one outlier return in the sim. Callers that need adjusted closes pass them in.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - runtime in index comparisons
from typing import TYPE_CHECKING

from arc.betas.corr import CorrMatrix
from arc.betas.corr_store import compute_rows
from arc.features.correlation import MIN_OBS, WINDOW

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import pandas as pd

__all__ = ["pit_closes", "pit_matrices", "pit_matrix"]

#: Closes kept per name before the cut (3x the window: room for halts / missing days).
_KEEP_FACTOR = 3


def pit_closes(
    closes: Mapping[str, pd.Series], day: dt.date, window: int = WINDOW
) -> dict[str, dict[dt.date, float]]:
    """Each name's closes strictly before *day*, the last ``3 * (window + 1)`` of them."""
    keep = _KEEP_FACTOR * (window + 1)
    out: dict[str, dict[dt.date, float]] = {}
    for t, s in closes.items():
        pts = sorted((d, float(c)) for d, c in s.dropna().items() if d < day)
        out[t.upper()] = dict(pts[-keep:])
    return out


def pit_matrix(
    closes: Mapping[str, pd.Series],
    day: dt.date,
    *,
    window: int = WINDOW,
    min_obs: int = MIN_OBS,
) -> CorrMatrix:
    """The matrix the live gate would read on *day* (closes strictly before *day*)."""
    series = pit_closes(closes, day, window)
    rows = compute_rows(series, day, window=window, min_obs=min_obs)
    return CorrMatrix(
        day=day,
        pairs={(r.a, r.b): r.corr for r in rows},
        window=window,
        tickers=frozenset(series),
    )


def pit_matrices(
    closes: Mapping[str, pd.Series],
    sessions: Iterable[dt.date],
    *,
    window: int = WINDOW,
    min_obs: int = MIN_OBS,
) -> dict[dt.date, CorrMatrix]:
    """:func:`pit_matrix` for every session in *sessions*."""
    return {d: pit_matrix(closes, d, window=window, min_obs=min_obs) for d in sessions}
