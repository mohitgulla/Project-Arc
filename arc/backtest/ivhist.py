"""IV30, IV rank and IV percentile per ticker-day from history (E7.10, D84).

No second implementation: every number comes from the live code paths.

* **Term points** per session: :func:`arc.features.vol.atm_term_points` on the
  session's marked chain (the mean IV of the contracts at the strike nearest the raw
  close, per expiry), exactly what ``iv.record`` runs on the live Alpaca chain.
* **IV30**: :func:`arc.features.vol.constant_maturity_iv` (cm30 total-variance
  interpolation) over the points inside the live chain request window
  (``arc.iv.record.CHAIN_DTE_MIN``–``CHAIN_DTE_MAX`` = 7–75 DTE).
* **IV rank / percentile**: :func:`arc.features.vol.iv_rank` / ``iv_percentile`` over
  the trailing ``lookback`` (252) observations including the day, requiring
  ``min_obs`` (``ArcSettings.iv_min_obs_rank``, 120) — the live D55 rule.

Each day's IV reads only that day's chain; the rank reads only days ≤ d.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from arc.features._series import InsufficientHistoryError
from arc.features.vol import (
    IV_LOOKBACK,
    MIN_IV_HISTORY,
    TARGET_DTE,
    atm_term_points,
    constant_maturity_iv,
    iv_percentile,
    iv_rank,
)
from arc.iv.record import CHAIN_DTE_MAX, CHAIN_DTE_MIN

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "IvPoint",
    "TermPoint",
    "iv30_from_points",
    "iv_points",
    "term_points",
    "term_points_by_day",
]


class TermPoint(BaseModel):
    """ATM IV of one expiry on one session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expiration: dt.date
    dte: int
    iv: float


class IvPoint(BaseModel):
    """IV30 and its rank/percentile for one ticker-day (``None`` = not computable)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    day: dt.date
    iv30: float | None = None
    iv_rank: float | None = Field(None, ge=0.0, le=1.0)
    iv_percentile: float | None = Field(None, ge=0.0, le=1.0)
    n_obs: int = Field(0, description="IV30 observations in the trailing lookback window")
    expiries: int = Field(0, description="Expiries with an ATM IV inside 7-75 DTE")


@dataclass(frozen=True)
class _Contract:
    """The ``ChainContractLike`` view of one marked-chain row."""

    expiration: dt.date
    strike: float
    implied_volatility: float | None


def term_points(chain: pd.DataFrame, spot: float, day: dt.date) -> list[TermPoint]:
    """Per-expiry ATM IV of one session's marked chain (live ``atm_term_points``)."""
    if chain.empty or spot <= 0:
        return []
    contracts = [
        _Contract(e, float(k), None if pd.isna(v) else float(v))
        for e, k, v in zip(chain["expiration"], chain["strike"], chain["iv"], strict=True)
    ]
    return [
        TermPoint(expiration=day + dt.timedelta(days=d), dte=d, iv=v)
        for d, v in atm_term_points(contracts, spot, day)
    ]


def term_points_by_day(
    chains: Mapping[dt.date, pd.DataFrame], closes: pd.Series
) -> dict[dt.date, list[TermPoint]]:
    out: dict[dt.date, list[TermPoint]] = {}
    for d in sorted(chains):
        spot = closes.get(d)
        if spot is None or not pd.notna(spot):
            continue
        out[d] = term_points(chains[d], float(spot), d)
    return out


def iv30_from_points(
    points: list[TermPoint],
    *,
    target_dte: int = TARGET_DTE,
    dte_min: int = CHAIN_DTE_MIN,
    dte_max: int = CHAIN_DTE_MAX,
) -> tuple[float | None, int]:
    """(IV30, expiries used): live cm30 over the points inside [dte_min, dte_max]."""
    pts = [(p.dte, p.iv) for p in points if dte_min <= p.dte <= dte_max]
    if not pts:
        return None, 0
    return constant_maturity_iv(pts, target_dte=target_dte), len(pts)


def iv_points(
    ticker: str,
    points_by_day: Mapping[dt.date, list[TermPoint]],
    *,
    lookback: int = IV_LOOKBACK,
    min_obs: int = MIN_IV_HISTORY,
    target_dte: int = TARGET_DTE,
) -> dict[dt.date, IvPoint]:
    """IvPoint per session: IV30 from that day's chain, rank/percentile from days ≤ d."""
    iv30: dict[dt.date, tuple[float | None, int]] = {
        d: iv30_from_points(p, target_dte=target_dte) for d, p in sorted(points_by_day.items())
    }
    series = pd.Series(
        {d: v for d, (v, _) in iv30.items() if v is not None}, dtype=float
    ).sort_index()
    out: dict[dt.date, IvPoint] = {}
    for d, (v, n_exp) in iv30.items():
        rank = pct = None
        n_obs = 0
        if v is not None:
            n_obs = min(int((pd.Index(series.index) <= d).sum()), lookback)
            try:
                rank = iv_rank(series, d, lookback=lookback, min_obs=min_obs)
                pct = iv_percentile(series, d, lookback=lookback, min_obs=min_obs)
            except InsufficientHistoryError:
                rank = pct = None
        out[d] = IvPoint(
            ticker=ticker.upper(),
            day=d,
            iv30=v,
            iv_rank=rank,
            iv_percentile=pct,
            n_obs=n_obs,
            expiries=n_exp,
        )
    return out
