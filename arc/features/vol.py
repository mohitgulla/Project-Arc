"""Volatility features: historical vol, IV/HV ratio, IV rank and IV percentile.

Conventions:

- **HV** (close-to-close): sample standard deviation (ddof=1) of daily log
  returns over the last *window* sessions, annualised by ``sqrt(252)``.
  HV20 needs 21 closes; HV60 needs 61.
- **IV** is the at-the-money ~30-DTE implied vol of the underlying, as a
  decimal (0.20 = 20 vol). :func:`atm_iv_from_chain` derives it from a
  chain snapshot; historical IV comes from the caller (E7.1 history store)
  because the Alpaca live feed only provides the current chain.
- **IV/HV ratio**: ``IV / HV20`` (and ``IV / HV60``).
- **IV rank**: ``(IV_t - min) / (max - min)`` over the trailing *lookback*
  observations **including** today, in [0, 1].
- **IV percentile**: share of the trailing *lookback* observations
  **before** today whose IV is strictly below ``IV_t``, in [0, 1].

All functions take an ``as_of`` date and ignore data after it.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in pydantic models
import math
from typing import TYPE_CHECKING, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from arc.features._series import InsufficientHistoryError, to_daily_series, truncate

if TYPE_CHECKING:
    from collections.abc import Iterable

TRADING_DAYS_PER_YEAR = 252
HV_SHORT = 20
HV_LONG = 60
IV_LOOKBACK = 252
MIN_IV_HISTORY = 20
TARGET_DTE = 30


# ---------------------------------------------------------------------------
# Historical volatility
# ---------------------------------------------------------------------------


def log_returns(closes: pd.Series) -> pd.Series:
    """Daily log returns of a close series (first row dropped)."""
    s = to_daily_series(closes, name="close")
    if (s <= 0).any():
        raise ValueError("closes must be strictly positive")
    return np.log(s / s.shift(1)).dropna().rename("log_return")


def historical_vol(closes: pd.Series, window: int, as_of: dt.date | None = None) -> float:
    """Annualised close-to-close HV over the last *window* sessions up to *as_of*."""
    if window < 2:
        raise ValueError("window must be >= 2")
    s = to_daily_series(closes, name="close")
    if as_of is not None:
        s = truncate(s, as_of)
    rets = log_returns(s)
    if len(rets) < window:
        raise InsufficientHistoryError(f"HV{window} needs {window + 1} closes, got {len(s)}")
    return float(rets.iloc[-window:].std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def rolling_historical_vol(closes: pd.Series, window: int) -> pd.Series:
    """HV{window} for every date that has enough history (each value uses only past data)."""
    if window < 2:
        raise ValueError("window must be >= 2")
    rets = log_returns(closes)
    hv = rets.rolling(window).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
    return hv.dropna().rename(f"hv{window}")


# ---------------------------------------------------------------------------
# IV rank / percentile
# ---------------------------------------------------------------------------


def _iv_window(iv: pd.Series, as_of: dt.date, lookback: int) -> pd.Series:
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    s = truncate(to_daily_series(iv, name="iv"), as_of)
    if (s < 0).any():
        raise ValueError("implied vols must be non-negative")
    if s.empty or s.index[-1] != as_of:
        raise InsufficientHistoryError(f"no IV observation on {as_of}")
    return s.iloc[-lookback:]


def iv_rank(
    iv: pd.Series, as_of: dt.date, *, lookback: int = IV_LOOKBACK, min_obs: int = MIN_IV_HISTORY
) -> float:
    """IV rank of the *as_of* observation over the trailing *lookback* window (0..1).

    A flat window (max == min) returns 0.5.
    """
    w = _iv_window(iv, as_of, lookback)
    if len(w) < min_obs:
        raise InsufficientHistoryError(f"IV rank needs {min_obs} obs, got {len(w)}")
    lo, hi, cur = float(w.min()), float(w.max()), float(w.iloc[-1])
    if hi - lo <= 1e-12:
        return 0.5
    return (cur - lo) / (hi - lo)


def iv_percentile(
    iv: pd.Series, as_of: dt.date, *, lookback: int = IV_LOOKBACK, min_obs: int = MIN_IV_HISTORY
) -> float:
    """Share of the prior observations in the window with IV strictly below today's (0..1)."""
    w = _iv_window(iv, as_of, lookback)
    if len(w) < min_obs:
        raise InsufficientHistoryError(f"IV percentile needs {min_obs} obs, got {len(w)}")
    cur = float(w.iloc[-1])
    prior = w.iloc[:-1].to_numpy()
    return float((prior < cur).sum() / len(prior))


# ---------------------------------------------------------------------------
# ATM IV from a chain snapshot
# ---------------------------------------------------------------------------


class ChainContractLike(Protocol):
    """Subset of ``arc.data.base.OptionContract`` used here."""

    @property
    def expiration(self) -> dt.date: ...

    @property
    def strike(self) -> float: ...

    @property
    def implied_volatility(self) -> float | None: ...


def atm_iv_from_chain(
    chain: Iterable[ChainContractLike],
    spot: float,
    as_of: dt.date,
    *,
    target_dte: int = TARGET_DTE,
) -> float | None:
    """Constant-maturity ATM IV at *target_dte* calendar days from a chain snapshot.

    Per expiry: average the IVs of all contracts (calls and puts) at the
    strike nearest *spot*. Across expiries: linear interpolation in total
    variance (``iv^2 * T``) between the expiries bracketing *target_dte*;
    flat extrapolation beyond the listed range. Contracts with missing or
    non-positive IV, and expiries on/before *as_of*, are ignored. Returns
    ``None`` if nothing usable remains.
    """
    if spot <= 0:
        raise ValueError("spot must be positive")
    by_exp: dict[dt.date, list[tuple[float, float]]] = {}
    for c in chain:
        iv = c.implied_volatility
        if iv is None or not math.isfinite(iv) or iv <= 0 or c.expiration <= as_of:
            continue
        by_exp.setdefault(c.expiration, []).append((float(c.strike), float(iv)))
    if not by_exp:
        return None

    points: list[tuple[int, float]] = []
    for exp, rows in sorted(by_exp.items()):
        nearest = min(abs(k - spot) for k, _ in rows)
        ivs = [iv for k, iv in rows if abs(abs(k - spot) - nearest) <= 1e-9]
        points.append(((exp - as_of).days, float(np.mean(ivs))))

    below = [p for p in points if p[0] <= target_dte]
    above = [p for p in points if p[0] >= target_dte]
    if not below:
        return above[0][1]
    if not above:
        return below[-1][1]
    (d1, v1), (d2, v2) = below[-1], above[0]
    if d1 == d2:
        return v1
    w1, w2 = v1 * v1 * d1, v2 * v2 * d2
    wt = w1 + (w2 - w1) * (target_dte - d1) / (d2 - d1)
    return math.sqrt(max(wt, 0.0) / target_dte)


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class VolFeatures(BaseModel):
    """Volatility features for one underlying as of one session close.

    Fields are ``None`` when the underlying history is too short; the
    reason is recorded in ``missing``.
    """

    as_of: dt.date
    hv20: float | None = Field(None, description="Annualised 20-session close-to-close HV")
    hv60: float | None = Field(None, description="Annualised 60-session close-to-close HV")
    iv: float | None = Field(None, description="ATM ~30-DTE implied vol at as_of")
    iv_hv20_ratio: float | None = None
    iv_hv60_ratio: float | None = None
    iv_rank: float | None = Field(None, ge=0.0, le=1.0)
    iv_percentile: float | None = Field(None, ge=0.0, le=1.0)
    iv_lookback: int = IV_LOOKBACK
    iv_observations: int = Field(0, description="IV observations in the rank/percentile window")
    missing: list[str] = Field(default_factory=list)


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def compute_vol_features(
    closes: pd.Series,
    as_of: dt.date,
    *,
    iv_history: pd.Series | None = None,
    current_iv: float | None = None,
    iv_lookback: int = IV_LOOKBACK,
    min_iv_obs: int = MIN_IV_HISTORY,
) -> VolFeatures:
    """Compute :class:`VolFeatures` from closes and (optional) IV history up to *as_of*.

    ``current_iv`` (e.g. from :func:`atm_iv_from_chain`) is the IV at
    ``as_of``; it replaces any ``iv_history`` value on that date.
    """
    missing: list[str] = []

    def _hv(window: int) -> float | None:
        try:
            return historical_vol(closes, window, as_of)
        except InsufficientHistoryError as exc:
            missing.append(str(exc))
            return None

    hv20, hv60 = _hv(HV_SHORT), _hv(HV_LONG)

    ivs = (
        truncate(to_daily_series(iv_history, name="iv"), as_of) if iv_history is not None else None
    )
    if current_iv is not None:
        if not math.isfinite(current_iv) or current_iv < 0:
            raise ValueError("current_iv must be a non-negative finite number")
        data = {} if ivs is None else {d: float(v) for d, v in ivs.items()}
        data[as_of] = float(current_iv)
        ivs = to_daily_series(pd.Series(data, dtype=float), name="iv")

    iv: float | None = None
    rank: float | None = None
    pct: float | None = None
    n_obs = 0
    if ivs is None or ivs.empty or ivs.index[-1] != as_of:
        missing.append(f"no IV observation on {as_of}")
    else:
        iv = float(ivs.iloc[-1])
        n_obs = min(len(ivs), iv_lookback)
        try:
            rank = iv_rank(ivs, as_of, lookback=iv_lookback, min_obs=min_iv_obs)
            pct = iv_percentile(ivs, as_of, lookback=iv_lookback, min_obs=min_iv_obs)
        except InsufficientHistoryError as exc:
            missing.append(str(exc))

    return VolFeatures(
        as_of=as_of,
        hv20=hv20,
        hv60=hv60,
        iv=iv,
        iv_hv20_ratio=_ratio(iv, hv20),
        iv_hv60_ratio=_ratio(iv, hv60),
        iv_rank=rank,
        iv_percentile=pct,
        iv_lookback=iv_lookback,
        iv_observations=n_obs,
        missing=missing,
    )
