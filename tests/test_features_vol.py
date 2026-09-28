"""Tests for arc.features.vol — HV, IV/HV, IV rank & percentile, ATM IV from chain."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.features._series import InsufficientHistoryError
from arc.features.vol import (
    atm_iv_from_chain,
    compute_vol_features,
    historical_vol,
    iv_percentile,
    iv_rank,
    log_returns,
    rolling_historical_vol,
)


def _dates(n: int) -> list[dt.date]:
    return [d.date() for d in pd.bdate_range("2024-01-02", periods=n)]


def _series(values: list[float] | np.ndarray) -> pd.Series:
    return pd.Series(list(values), index=_dates(len(values)), dtype=float)


def _alternating(n: int, r: float = 0.01) -> pd.Series:
    """Closes whose log returns alternate +r, -r."""
    logs = np.cumsum([0.0] + [r if i % 2 == 0 else -r for i in range(n - 1)])
    return _series(100.0 * np.exp(logs))


# ---------------------------------------------------------------------------
# HV
# ---------------------------------------------------------------------------


class TestHistoricalVol:
    def test_constant_prices_zero_vol(self) -> None:
        assert historical_vol(_series([100.0] * 30), 20) == 0.0

    def test_known_value(self) -> None:
        closes = _alternating(61)
        rets = np.array([0.01 if i % 2 == 0 else -0.01 for i in range(60)])
        expected = float(np.std(rets[-20:], ddof=1) * math.sqrt(252))
        assert historical_vol(closes, 20) == pytest.approx(expected)
        assert historical_vol(closes, 60) == pytest.approx(
            float(np.std(rets, ddof=1) * math.sqrt(252))
        )

    def test_min_history(self) -> None:
        assert historical_vol(_alternating(21), 20) > 0
        with pytest.raises(InsufficientHistoryError):
            historical_vol(_alternating(20), 20)

    def test_as_of_truncates(self) -> None:
        closes = _alternating(100)
        as_of = closes.index[40]
        shocked = closes.copy()
        shocked.iloc[41:] *= 3
        assert historical_vol(shocked, 20, as_of) == historical_vol(closes.iloc[:41], 20)

    def test_bad_window(self) -> None:
        with pytest.raises(ValueError):
            historical_vol(_alternating(30), 1)
        with pytest.raises(ValueError):
            rolling_historical_vol(_alternating(30), 1)

    def test_non_positive_closes(self) -> None:
        with pytest.raises(ValueError):
            log_returns(_series([1.0, -1.0]))

    def test_rolling_matches_point(self) -> None:
        rng = np.random.default_rng(3)
        closes = _series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 90))))
        roll = rolling_historical_vol(closes, 20)
        assert roll.index[0] == closes.index[20]
        for d in (roll.index[0], roll.index[35], roll.index[-1]):
            assert roll[d] == pytest.approx(historical_vol(closes, 20, d))

    @given(st.floats(1.5, 4.0))
    def test_scale_invariant(self, k: float) -> None:
        closes = _alternating(40, 0.013)
        assert historical_vol(closes * k, 20) == pytest.approx(historical_vol(closes, 20))


# ---------------------------------------------------------------------------
# IV rank / percentile
# ---------------------------------------------------------------------------


class TestIvRankPercentile:
    def test_rank_known(self) -> None:
        iv = _series([0.10, 0.30, 0.20, 0.15] * 5 + [0.25])
        assert iv_rank(iv, iv.index[-1]) == pytest.approx((0.25 - 0.10) / 0.20)

    def test_percentile_known(self) -> None:
        iv = _series([0.10, 0.30, 0.20, 0.15] * 5 + [0.25])
        # Prior 20 obs: 0.10, 0.20, 0.15 are below 0.25 -> 15/20
        assert iv_percentile(iv, iv.index[-1]) == pytest.approx(0.75)

    def test_extremes(self) -> None:
        iv = _series(list(np.linspace(0.1, 0.3, 30)))
        assert iv_rank(iv, iv.index[-1]) == 1.0
        assert iv_percentile(iv, iv.index[-1]) == 1.0
        low = _series(list(np.linspace(0.3, 0.1, 30)))
        assert iv_rank(low, low.index[-1]) == 0.0
        assert iv_percentile(low, low.index[-1]) == 0.0

    def test_flat_window(self) -> None:
        iv = _series([0.2] * 25)
        assert iv_rank(iv, iv.index[-1]) == 0.5
        assert iv_percentile(iv, iv.index[-1]) == 0.0

    def test_lookback_window(self) -> None:
        # A spike older than the lookback must not affect the rank.
        iv = _series([0.9] + [0.1 + 0.001 * i for i in range(30)])
        assert iv_rank(iv, iv.index[-1], lookback=25) == 1.0
        assert iv_rank(iv, iv.index[-1], lookback=31) < 0.1

    def test_requires_today_and_min_obs(self) -> None:
        iv = _series([0.2] * 25)
        with pytest.raises(InsufficientHistoryError):
            iv_rank(iv, iv.index[-1] + dt.timedelta(days=7))
        with pytest.raises(InsufficientHistoryError):
            iv_rank(iv, iv.index[10])
        with pytest.raises(InsufficientHistoryError):
            iv_percentile(iv, iv.index[10])
        with pytest.raises(ValueError):
            iv_rank(iv, iv.index[-1], lookback=1)
        with pytest.raises(ValueError):
            iv_rank(_series([-0.1] * 25), dt.date(2024, 2, 5))

    @given(st.lists(st.floats(0.01, 2.0), min_size=21, max_size=300), st.integers(0, 60))
    @settings(max_examples=150)
    def test_bounds_and_no_look_ahead(self, vals: list[float], cut_back: int) -> None:
        iv = _series(vals)
        cut = max(len(vals) - 1 - cut_back, 20)
        as_of = iv.index[cut]
        r, p = iv_rank(iv, as_of), iv_percentile(iv, as_of)
        assert 0.0 <= r <= 1.0 and 0.0 <= p <= 1.0
        future = iv.copy()
        future.iloc[cut + 1 :] = 5.0
        assert iv_rank(future, as_of) == r
        assert iv_percentile(future, as_of) == p


# ---------------------------------------------------------------------------
# ATM IV from chain
# ---------------------------------------------------------------------------


@dataclass
class _C:
    expiration: dt.date
    strike: float
    implied_volatility: float | None


AS_OF = dt.date(2026, 9, 25)


class TestAtmIv:
    def test_exact_expiry_nearest_strike_avg_call_put(self) -> None:
        exp = AS_OF + dt.timedelta(days=30)
        chain = [
            _C(exp, 95, 0.30),
            _C(exp, 100, 0.20),
            _C(exp, 100, 0.22),
            _C(exp, 105, 0.18),
        ]
        assert atm_iv_from_chain(chain, 101.0, AS_OF) == pytest.approx(0.21)

    def test_variance_interpolation(self) -> None:
        e1, e2 = AS_OF + dt.timedelta(days=20), AS_OF + dt.timedelta(days=40)
        chain = [_C(e1, 100, 0.20), _C(e2, 100, 0.30)]
        expected = math.sqrt((0.04 * 20 + (0.09 * 40 - 0.04 * 20) * 0.5) / 30)
        assert atm_iv_from_chain(chain, 100.0, AS_OF) == pytest.approx(expected)

    def test_flat_extrapolation(self) -> None:
        near = [_C(AS_OF + dt.timedelta(days=10), 100, 0.25)]
        far = [_C(AS_OF + dt.timedelta(days=60), 100, 0.35)]
        assert atm_iv_from_chain(near, 100.0, AS_OF) == 0.25
        assert atm_iv_from_chain(far, 100.0, AS_OF) == 0.35

    def test_filters_bad_contracts(self) -> None:
        exp = AS_OF + dt.timedelta(days=30)
        chain = [
            _C(exp, 100, None),
            _C(exp, 100, 0.0),
            _C(exp, 100, float("nan")),
            _C(AS_OF, 100, 0.9),
            _C(exp, 110, 0.4),
        ]
        assert atm_iv_from_chain(chain, 100.0, AS_OF) == 0.4
        assert atm_iv_from_chain(chain[:4], 100.0, AS_OF) is None

    def test_bad_spot(self) -> None:
        with pytest.raises(ValueError):
            atm_iv_from_chain([], 0.0, AS_OF)

    def test_accepts_option_contract_shape(self) -> None:
        """Duck-types against the E1.4 OptionContract fields used here."""
        from pydantic import BaseModel

        class OptionContract(BaseModel):
            symbol: str
            expiration: dt.date
            strike: float
            implied_volatility: float | None = None

        exp = AS_OF + dt.timedelta(days=30)
        chain = [OptionContract(symbol="SPY", expiration=exp, strike=500, implied_volatility=0.15)]
        assert atm_iv_from_chain(chain, 500.0, AS_OF) == 0.15


# ---------------------------------------------------------------------------
# compute_vol_features
# ---------------------------------------------------------------------------


class TestComputeVolFeatures:
    def test_full(self) -> None:
        closes = _alternating(80)
        as_of = closes.index[-1]
        iv = pd.Series(np.linspace(0.1, 0.3, 80), index=closes.index)
        f = compute_vol_features(closes, as_of, iv_history=iv)
        assert f.missing == []
        assert f.hv20 == pytest.approx(historical_vol(closes, 20))
        assert f.iv == pytest.approx(0.3)
        assert f.iv_hv20_ratio == pytest.approx(0.3 / f.hv20)
        assert f.iv_hv60_ratio == pytest.approx(0.3 / f.hv60)
        assert f.iv_rank == 1.0 and f.iv_percentile == 1.0
        assert f.iv_observations == 80

    def test_current_iv_overrides_today(self) -> None:
        closes = _alternating(80)
        as_of = closes.index[-1]
        iv = pd.Series(np.linspace(0.1, 0.3, 80), index=closes.index)
        f = compute_vol_features(closes, as_of, iv_history=iv, current_iv=0.1)
        assert f.iv == 0.1 and f.iv_rank == 0.0

    def test_current_iv_without_history(self) -> None:
        closes = _alternating(80)
        f = compute_vol_features(closes, closes.index[-1], current_iv=0.2)
        assert f.iv == 0.2
        assert f.iv_rank is None and f.iv_observations == 1
        assert any("IV rank" in m for m in f.missing)

    def test_missing_everything(self) -> None:
        closes = _alternating(10)
        f = compute_vol_features(closes, closes.index[-1])
        assert f.hv20 is None and f.hv60 is None and f.iv is None
        assert f.iv_hv20_ratio is None
        assert len(f.missing) == 3

    def test_zero_hv_ratio_is_none(self) -> None:
        closes = _series([100.0] * 70)
        f = compute_vol_features(closes, closes.index[-1], current_iv=0.2)
        assert f.hv20 == 0.0 and f.iv_hv20_ratio is None

    def test_bad_current_iv(self) -> None:
        closes = _alternating(30)
        with pytest.raises(ValueError):
            compute_vol_features(closes, closes.index[-1], current_iv=float("nan"))

    def test_stale_iv_history_reported(self) -> None:
        closes = _alternating(80)
        iv = pd.Series(np.linspace(0.1, 0.3, 79), index=closes.index[:79])
        f = compute_vol_features(closes, closes.index[-1], iv_history=iv)
        assert f.iv is None
        assert any("no IV observation" in m for m in f.missing)
