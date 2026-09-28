"""Tests for arc.features.regime — labelling, Markov chain maths, walk-forward safety."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.features._series import InsufficientHistoryError
from arc.features.regime import (
    STATES,
    Regime,
    classify_return,
    estimate_regime,
    expected_duration,
    forecast,
    label_regimes,
    stationary_distribution,
    stickiness,
    trailing_returns,
    transition_counts,
    transition_matrix,
    walk_forward,
)

B, S, U = Regime.BEAR, Regime.SIDEWAYS, Regime.BULL


def _dates(n: int, start: dt.date = dt.date(2024, 1, 2)) -> list[dt.date]:
    return [d.date() for d in pd.bdate_range(start, periods=n)]


def _closes(values: list[float] | np.ndarray, start: dt.date = dt.date(2024, 1, 2)) -> pd.Series:
    return pd.Series(list(values), index=_dates(len(values), start), dtype=float)


def _gbm(n: int, seed: int = 7, drift: float = 0.0, vol: float = 0.2) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift / 252, vol / np.sqrt(252), n - 1)
    return _closes(100.0 * np.exp(np.concatenate([[0.0], np.cumsum(rets)])))


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        ("r", "expected"),
        [
            (0.05, U),
            (0.0500001, U),
            (0.0499999, S),
            (0.0, S),
            (-0.0499999, S),
            (-0.05, B),
            (-0.2, B),
        ],
    )
    def test_thresholds_are_inclusive(self, r: float, expected: Regime) -> None:
        assert classify_return(r) is expected

    def test_bad_thresholds(self) -> None:
        with pytest.raises(ValueError):
            classify_return(0.0, bull=-0.1, bear=0.1)


class TestLabelling:
    def test_trailing_return_uses_20_sessions(self) -> None:
        closes = _closes([100.0] * 20 + [106.0])
        r = trailing_returns(closes)
        assert len(r) == 1
        assert r.iloc[0] == pytest.approx(0.06)
        assert label_regimes(closes).iloc[0] is U

    def test_labels_three_states(self) -> None:
        up = list(np.linspace(100, 120, 30))
        flat = [120.0] * 30
        down = list(np.linspace(120, 90, 30))
        labels = label_regimes(_closes(up + flat + down))
        assert set(labels) == {B, S, U}
        assert labels.iloc[0] is U
        assert labels.iloc[-1] is B

    def test_requires_positive_closes(self) -> None:
        with pytest.raises(ValueError):
            trailing_returns(_closes([1.0, 0.0, 2.0]), lookback=1)

    def test_bad_lookback(self) -> None:
        with pytest.raises(ValueError):
            trailing_returns(_closes([1.0, 2.0]), lookback=0)

    def test_accepts_tz_aware_timestamps(self) -> None:
        idx = pd.date_range("2024-01-02 16:00", periods=25, freq="B", tz="America/New_York")
        s = pd.Series(np.linspace(100, 110, 25), index=idx)
        labels = label_regimes(s)
        assert isinstance(labels.index[0], dt.date)
        assert len(labels) == 5


# ---------------------------------------------------------------------------
# Transition matrix
# ---------------------------------------------------------------------------


class TestTransitionMatrix:
    def test_counts(self) -> None:
        c = transition_counts([S, S, U, U, U, S, B])
        # S->S, S->U, U->U, U->U, U->S, S->B
        assert c[1, 1] == 1 and c[1, 2] == 1 and c[1, 0] == 1
        assert c[2, 2] == 2 and c[2, 1] == 1
        assert c.sum() == 6

    def test_counts_with_step(self) -> None:
        c = transition_counts([S, U, S, U], step=2)
        assert c[1, 1] == 1 and c[2, 2] == 1 and c.sum() == 2

    def test_bad_step(self) -> None:
        with pytest.raises(ValueError):
            transition_counts([S, S], step=0)

    def test_known_matrix(self) -> None:
        p = transition_matrix([S, S, U, U, U, S, B])
        np.testing.assert_allclose(p[1], [1 / 3, 1 / 3, 1 / 3])
        np.testing.assert_allclose(p[2], [0, 1 / 3, 2 / 3])
        # Bear never left -> uniform row
        np.testing.assert_allclose(p[0], [1 / 3] * 3)

    def test_smoothing(self) -> None:
        p = transition_matrix([U, U, U], alpha=1.0)
        np.testing.assert_allclose(p[2], [1 / 5, 1 / 5, 3 / 5])
        np.testing.assert_allclose(p[0], [1 / 3] * 3)

    def test_bad_alpha(self) -> None:
        with pytest.raises(ValueError):
            transition_matrix([S, S], alpha=-1)

    @given(st.lists(st.sampled_from(STATES), min_size=0, max_size=200), st.floats(0, 5))
    def test_always_row_stochastic(self, labels: list[Regime], alpha: float) -> None:
        p = transition_matrix(labels, alpha=alpha)
        assert p.shape == (3, 3)
        assert (p >= 0).all()
        np.testing.assert_allclose(p.sum(axis=1), 1.0)


# ---------------------------------------------------------------------------
# Derived quantities
# ---------------------------------------------------------------------------

stochastic_matrices = st.lists(
    st.lists(st.floats(0, 1, allow_nan=False), min_size=3, max_size=3).filter(
        lambda r: sum(r) > 1e-6
    ),
    min_size=3,
    max_size=3,
).map(lambda rows: np.array([np.array(r) / sum(r) for r in rows]))


class TestDerived:
    P = np.array([[0.8, 0.2, 0.0], [0.1, 0.8, 0.1], [0.0, 0.3, 0.7]])

    def test_stickiness_and_duration(self) -> None:
        stick = stickiness(self.P)
        assert stick[B] == pytest.approx(0.8)
        assert stick[U] == pytest.approx(0.7)
        dur = expected_duration(self.P)
        assert dur[S] == pytest.approx(5.0)
        assert dur[U] == pytest.approx(1 / 0.3)

    def test_absorbing_duration_is_none(self) -> None:
        p = np.eye(3)
        assert expected_duration(p)[B] is None

    def test_forecast(self) -> None:
        np.testing.assert_allclose(forecast(self.P, S, 0), [0, 1, 0])
        np.testing.assert_allclose(forecast(self.P, S, 1), self.P[1])
        np.testing.assert_allclose(forecast(self.P, B, 2), (self.P @ self.P)[0])

    def test_forecast_negative(self) -> None:
        with pytest.raises(ValueError):
            forecast(self.P, S, -1)

    def test_stationary_known(self) -> None:
        # Solve pi P = pi analytically via the left eigenvector.
        w, v = np.linalg.eig(self.P.T)
        ref = np.real(v[:, np.argmin(np.abs(w - 1))])
        ref = ref / ref.sum()
        np.testing.assert_allclose(stationary_distribution(self.P), ref, atol=1e-9)

    def test_stationary_periodic_chain(self) -> None:
        p = np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]])
        np.testing.assert_allclose(stationary_distribution(p), [1 / 3] * 3, atol=1e-9)

    def test_stationary_very_sticky(self) -> None:
        p = np.array([[0.9999, 0.0001, 0.0], [0.0001, 0.9998, 0.0001], [0.0, 0.0001, 0.9999]])
        np.testing.assert_allclose(stationary_distribution(p), [1 / 3] * 3, atol=1e-6)

    @pytest.mark.parametrize(
        "bad",
        [np.eye(2), np.array([[0.5, 0.5, 0.5], [0, 1, 0], [0, 0, 1]]), -np.eye(3) + 2 / 3],
    )
    def test_rejects_non_stochastic(self, bad: np.ndarray) -> None:
        with pytest.raises(ValueError):
            stickiness(bad)

    @given(stochastic_matrices)
    @settings(max_examples=200)
    def test_stationary_is_fixed_point(self, p: np.ndarray) -> None:
        pi = stationary_distribution(p)
        assert pi.sum() == pytest.approx(1.0)
        assert (pi >= 0).all()
        np.testing.assert_allclose(pi @ p, pi, atol=1e-7)

    @given(stochastic_matrices, st.sampled_from(STATES), st.integers(0, 60))
    def test_forecast_is_distribution(self, p: np.ndarray, s: Regime, n: int) -> None:
        f = forecast(p, s, n)
        assert f.sum() == pytest.approx(1.0)
        assert (f >= -1e-12).all()


# ---------------------------------------------------------------------------
# estimate_regime
# ---------------------------------------------------------------------------


class TestEstimateRegime:
    def test_structure(self) -> None:
        closes = _gbm(300)
        as_of = closes.index[-1]
        f = estimate_regime(closes, as_of)
        assert f.as_of == as_of
        assert f.n_transitions == 300 - 20 - 1
        assert [fc.horizon for fc in f.forecasts] == [1, 5, 20]
        assert sum(f.stationary.values()) == pytest.approx(1.0)
        for row in f.transition_matrix.values():
            assert sum(row.values()) == pytest.approx(1.0)
        assert f.stickiness == f.transition_matrix[f.current][f.current]
        assert f.forecasts[0].probabilities == f.transition_matrix[f.current]
        # Daily 20d-return labels are highly autocorrelated -> sticky.
        assert f.stickiness > 0.7
        # JSON round-trip
        assert type(f).model_validate_json(f.model_dump_json()) == f

    def test_current_regime_matches_trailing_return(self) -> None:
        closes = _closes(list(np.linspace(100, 100, 40)) + [100 * 1.01**k for k in range(1, 21)])
        f = estimate_regime(closes, closes.index[-1])
        assert f.current is U
        assert f.trailing_return >= 0.05

    def test_insufficient_history(self) -> None:
        closes = _gbm(21)
        with pytest.raises(InsufficientHistoryError):
            estimate_regime(closes, closes.index[-1])
        # 22 closes -> 2 labels -> 1 transition: OK
        closes = _gbm(22)
        assert estimate_regime(closes, closes.index[-1]).n_transitions == 1

    def test_fit_window(self) -> None:
        closes = _gbm(300)
        f = estimate_regime(closes, closes.index[-1], fit_window=100)
        assert f.n_transitions == 99
        with pytest.raises(ValueError):
            estimate_regime(closes, closes.index[-1], fit_window=1)

    def test_step(self) -> None:
        closes = _gbm(300)
        f = estimate_regime(closes, closes.index[-1], step=20)
        assert f.step == 20 and f.n_transitions == 280 - 20

    def test_as_of_between_sessions_uses_prior_close(self) -> None:
        closes = _gbm(100)
        friday = next(d for d in closes.index if d.weekday() == 4 and d > closes.index[50])
        saturday = friday + dt.timedelta(days=1)
        assert estimate_regime(closes, saturday).model_dump(exclude={"as_of"}) == estimate_regime(
            closes, friday
        ).model_dump(exclude={"as_of"})


class TestWalkForward:
    def test_no_look_ahead_explicit(self) -> None:
        """Changing any data after as_of must not change the estimate."""
        closes = _gbm(400, seed=1)
        as_of = closes.index[250]
        base = estimate_regime(closes, as_of)
        crashed = closes.copy()
        crashed.iloc[251:] = crashed.iloc[251:] * 0.3
        assert estimate_regime(crashed, as_of) == base
        assert estimate_regime(closes.iloc[:251], as_of) == base

    @given(
        seed=st.integers(0, 10_000),
        cut=st.integers(30, 150),
        shock=st.floats(0.2, 5.0),
    )
    @settings(max_examples=40, deadline=None)
    def test_no_look_ahead_property(self, seed: int, cut: int, shock: float) -> None:
        closes = _gbm(200, seed=seed)
        as_of = closes.index[cut]
        future = closes.copy()
        future.iloc[cut + 1 :] = future.iloc[cut + 1 :] * shock
        assert estimate_regime(future, as_of) == estimate_regime(closes, as_of)

    def test_walk_forward_skips_short_history(self) -> None:
        closes = _gbm(60)
        out = walk_forward(closes, closes.index)
        assert len(out) == 60 - 21
        assert out[0].as_of == closes.index[21]
        assert [o.n_transitions for o in out] == list(range(1, 40))
